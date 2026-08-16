"""Reading an uploaded PDF, and normalising it without trusting it.

Two jobs, deliberately separate:

**Inspection** decides whether a file can be printed at all and how long it is.
It uses pikepdf (libqpdf), which parses the structure without rendering, so it
costs milliseconds and cannot be made to execute anything. The backend being
replaced ran a Ghostscript "preflight" here whose every failure was re-raised as
`RuntimeError` and swallowed at the call site, so a broken original was printed
regardless -- a check that never rejected anything.

**Normalisation** shrinks image-heavy files so a Pi on a shop's broadband can
download them. It is an optimisation, and it fails soft: if Ghostscript is
missing, slow, or unhappy, the original prints perfectly well. Nothing about an
upload depends on it succeeding.

Ghostscript is a remote-code-execution surface being handed attacker-controlled
input, so it runs `-dSAFER`, with no shell, with a hard timeout, writing into a
scratch directory it is thrown away with. Only a result that has been verified
-- smaller, and with exactly the same number of pages -- is moved into storage.
"""

import io
import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pikepdf

from app.core.config import get_settings
from app.core.errors import BadRequest

logger = logging.getLogger(__name__)

PDF_MAGIC = b"%PDF-"

# Ghostscript's name differs by platform and neither is wrong.
GHOSTSCRIPT_NAMES = ("gs", "gswin64c", "gswin32c")


@dataclass(frozen=True)
class PdfFacts:
    """What inspection established about an uploaded file."""

    page_count: int
    byte_size: int


def inspect_pdf(
    data: bytes,
    *,
    max_bytes: int | None = None,
    max_pages: int | None = None,
) -> PdfFacts:
    """Accept a PDF and report its length, or refuse with a sentence.

    Every rejection message is written for the student who is holding the phone,
    because that is who sees it -- `{"detail": ...}` is rendered verbatim.
    """
    settings = get_settings()
    max_bytes = settings.max_upload_bytes if max_bytes is None else max_bytes
    max_pages = settings.MAX_DOCUMENT_PAGES if max_pages is None else max_pages

    if not data:
        raise BadRequest("That file is empty.")

    # Length first. Handing several hundred megabytes to a PDF parser to
    # discover it is too big is the denial of service, not the defence.
    if len(data) > max_bytes:
        raise BadRequest(
            f"That file is too large. The limit is {max_bytes // (1024 * 1024)} MB."
        )

    # By contents, never by filename or Content-Type -- both come from the
    # client. The old backend decided a file was a PDF by looking at the name.
    if not data.startswith(PDF_MAGIC):
        raise BadRequest("That is not a PDF. Please upload a PDF file.")

    try:
        with pikepdf.open(io.BytesIO(data)) as pdf:
            pages = len(pdf.pages)
    except pikepdf.PasswordError as exc:
        raise BadRequest(
            "This PDF is password-protected. Remove the password and try again."
        ) from exc
    except pikepdf.PdfError as exc:
        logger.info("rejected malformed pdf: %s", exc)
        raise BadRequest(
            "This PDF appears to be damaged. Please re-export it from a PDF "
            "viewer or scanner app and try again."
        ) from exc

    if pages < 1:
        raise BadRequest("This PDF has no pages.")
    if pages > max_pages:
        raise BadRequest(
            f"This PDF has {pages} pages, which is more than the "
            f"{max_pages}-page limit for a single upload."
        )

    return PdfFacts(page_count=pages, byte_size=len(data))


# ── normalisation ───────────────────────────────────────────────────────────


def ghostscript_executable() -> str | None:
    """The Ghostscript binary to run, or None if there is not one.

    Resolved rather than assumed, because `GHOSTSCRIPT_PATH=gs` is correct on
    the VPS and wrong on a Windows development machine, and a single .env
    should work on both.
    """
    configured = get_settings().GHOSTSCRIPT_PATH
    for candidate in (configured, *GHOSTSCRIPT_NAMES):
        if not candidate:
            continue
        found = shutil.which(candidate)
        if found:
            return found
        # An absolute path that exists but is not on PATH is still valid.
        if Path(candidate).is_file():
            return candidate
    return None


def ghostscript_command(executable: str, *, src: str | Path, dst: str | Path) -> list[str]:
    """The argument list. A list, never a string: no shell ever sees a filename.

    `-dSAFER` is the load-bearing flag. Without it a crafted PDF can use
    PostScript file operators to read and write arbitrary paths as the server
    user. Everything else here is about output size.
    """
    dpi = get_settings().PDF_NORMALISE_DPI
    return [
        executable,
        "-dSAFER",
        "-dNOPAUSE",
        "-dBATCH",
        "-dQUIET",
        "-sDEVICE=pdfwrite",
        "-dCompatibilityLevel=1.4",
        "-dDownsampleColorImages=true",
        f"-dColorImageResolution={dpi}",
        "-dColorImageDownsampleType=/Bicubic",
        "-dDownsampleGrayImages=true",
        f"-dGrayImageResolution={dpi}",
        "-dGrayImageDownsampleType=/Bicubic",
        # Mono images are left alone: they are usually 300dpi scans of text, and
        # downsampling them puts jagged edges on the letters.
        "-dDownsampleMonoImages=false",
        f"-sOutputFile={dst}",
        str(src),
    ]


def should_normalise(path: Path) -> bool:
    """Below the threshold the saving is not worth the CPU."""
    try:
        return path.stat().st_size >= get_settings().PDF_NORMALISE_MIN_BYTES
    except OSError:
        return False


def _page_count(path: Path) -> int | None:
    try:
        with pikepdf.open(path) as pdf:
            return len(pdf.pages)
    except Exception:  # noqa: BLE001 - any failure means "do not trust this file"
        return None


def normalise_pdf(src: Path, dst: Path) -> bool:
    """Write a smaller, equivalent PDF to `dst`. True only if one was written.

    Never raises. A failed normalisation is not a failed upload: the caller
    keeps pointing at the original, which prints just as well.
    """
    executable = ghostscript_executable()
    if executable is None:
        logger.warning("normalise: no ghostscript found, keeping the original")
        return False

    timeout = get_settings().GHOSTSCRIPT_TIMEOUT_SECONDS

    with tempfile.TemporaryDirectory(prefix="printvendo-gs-") as scratch:
        # Ghostscript writes here, not into the storage tree. A crashed or
        # half-finished run therefore leaves nothing that could be served to a
        # printer, and the whole directory goes away with the context manager.
        candidate = Path(scratch) / "normalised.pdf"

        try:
            completed = subprocess.run(  # noqa: S603 - list args, no shell
                ghostscript_command(executable, src=src, dst=candidate),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            logger.warning("normalise: ghostscript timed out on %s", src.name)
            return False
        except OSError:
            logger.warning("normalise: could not run ghostscript", exc_info=True)
            return False

        if completed.returncode != 0 or not candidate.is_file():
            logger.warning(
                "normalise: ghostscript exit %s on %s: %s",
                completed.returncode,
                src.name,
                (completed.stderr or "")[:300],
            )
            return False

        if candidate.stat().st_size >= src.stat().st_size:
            logger.info("normalise: no size win on %s, keeping the original", src.name)
            return False

        # The page range is applied once, at the printer, against this file. A
        # copy with a different number of pages would silently make "pages 5-10"
        # mean something else.
        before, after = _page_count(src), _page_count(candidate)
        if after is None or before != after:
            logger.error(
                "normalise: page count changed on %s (%s -> %s), discarding",
                src.name,
                before,
                after,
            )
            return False

        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(candidate), dst)

    logger.info("normalise: %s shrunk to %s bytes", src.name, dst.stat().st_size)
    return True
