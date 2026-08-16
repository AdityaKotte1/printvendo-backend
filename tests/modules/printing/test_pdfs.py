"""Accepting a PDF, counting it, and normalising it without trusting it.

Ghostscript is an RCE surface and an uploaded PDF is attacker-controlled, so
these tests care as much about what the pipeline refuses as about what it does.
"""

import io
import subprocess

import pikepdf
import pytest

from app.core.errors import BadRequest
from app.modules.printing import pdfs
from app.modules.printing.pdfs import (
    ghostscript_command,
    ghostscript_executable,
    inspect_pdf,
    normalise_pdf,
)


def make_pdf(pages: int = 1, *, password: str | None = None) -> bytes:
    pdf = pikepdf.new()
    for _ in range(pages):
        pdf.add_blank_page(page_size=(595, 842))
    buffer = io.BytesIO()
    if password:
        pdf.save(buffer, encryption=pikepdf.Encryption(owner=password, user=password))
    else:
        pdf.save(buffer)
    return buffer.getvalue()


# ── what counts as a printable PDF ──────────────────────────────────────────


def test_a_plain_pdf_reports_its_page_count():
    assert inspect_pdf(make_pdf(pages=3)).page_count == 3


def test_the_byte_size_is_reported_alongside():
    data = make_pdf(pages=1)

    assert inspect_pdf(data).byte_size == len(data)


@pytest.mark.parametrize(
    "not_a_pdf",
    [
        pytest.param(b"MZ\x90\x00this is a windows executable", id="exe"),
        pytest.param(b"\x89PNG\r\n\x1a\n" + b"0" * 400, id="png"),
        pytest.param(b"PK\x03\x04" + b"0" * 400, id="docx"),
    ],
)
def test_something_that_is_not_a_pdf_is_told_so_plainly(not_a_pdf):
    """By magic bytes, not by extension or Content-Type -- both are supplied by
    the client, and the old backend trusted them.

    The exact sentence matters. Without the magic-byte check the PDF parser
    still refuses these, but it calls them *damaged* -- so a student who
    uploaded a Word document is told to re-export their PDF, which they do not
    have. (A mutation deleting the check passed a looser assertion here, which
    is how this was found.)
    """
    with pytest.raises(BadRequest) as exc:
        inspect_pdf(not_a_pdf)

    assert "not a PDF" in str(exc.value)


def test_an_empty_upload_is_refused():
    with pytest.raises(BadRequest):
        inspect_pdf(b"")


def test_a_password_protected_pdf_says_so_rather_than_failing_obscurely():
    with pytest.raises(BadRequest) as exc:
        inspect_pdf(make_pdf(pages=1, password="hunter2"))

    assert "password" in str(exc.value).lower()


def test_a_damaged_pdf_is_refused_with_advice():
    truncated = make_pdf(pages=2)[:120]

    with pytest.raises(BadRequest) as exc:
        inspect_pdf(truncated)

    assert "damaged" in str(exc.value).lower()


def test_a_pdf_with_no_pages_is_refused():
    empty = pikepdf.new()
    buffer = io.BytesIO()
    empty.save(buffer)

    with pytest.raises(BadRequest) as exc:
        inspect_pdf(buffer.getvalue())

    assert "no pages" in str(exc.value).lower()


# ── the caps ────────────────────────────────────────────────────────────────


def test_a_file_over_the_size_cap_is_refused_before_it_is_parsed():
    """Refused on length alone. Handing 400MB to a PDF parser to find out it is
    too big is the denial of service, not the defence against it."""
    with pytest.raises(BadRequest) as exc:
        inspect_pdf(b"%PDF-1.4" + b"0" * 64, max_bytes=32)

    assert "large" in str(exc.value).lower()


def test_the_size_cap_message_names_a_limit_a_person_can_act_on():
    with pytest.raises(BadRequest) as exc:
        inspect_pdf(b"%PDF-1.4" + b"0" * (3 * 1024 * 1024), max_bytes=2 * 1024 * 1024)

    assert "2 MB" in str(exc.value)


def test_a_document_over_the_page_cap_is_refused():
    with pytest.raises(BadRequest) as exc:
        inspect_pdf(make_pdf(pages=6), max_pages=5)

    assert "5" in str(exc.value)


def test_a_document_exactly_at_the_page_cap_is_accepted():
    assert inspect_pdf(make_pdf(pages=5), max_pages=5).page_count == 5


# ── the Ghostscript command ─────────────────────────────────────────────────


def test_the_command_runs_with_dsafer():
    """-dSAFER is what stops a crafted PDF reading or writing arbitrary files
    through PostScript operators. It is not optional."""
    command = ghostscript_command("gs", src="in.pdf", dst="out.pdf")

    assert "-dSAFER" in command


def test_the_command_never_prompts_or_waits_for_input():
    command = ghostscript_command("gs", src="in.pdf", dst="out.pdf")

    assert "-dNOPAUSE" in command
    assert "-dBATCH" in command


def test_the_command_is_a_list_so_no_shell_ever_sees_a_filename():
    command = ghostscript_command("gs", src="in.pdf", dst="out.pdf")

    assert isinstance(command, list)
    assert all(isinstance(part, str) for part in command)


def test_ghostscript_is_installed_on_this_machine():
    """The pipeline needs it. Failing loudly here beats every PDF silently
    skipping normalisation in production and nobody noticing."""
    assert ghostscript_executable() is not None


# ── normalising ─────────────────────────────────────────────────────────────


@pytest.fixture
def image_heavy_pdf(tmp_path):
    """A PDF big enough that normalising it is worth doing."""
    from PIL import Image

    noise = Image.effect_noise((1600, 2200), 96).convert("RGB")
    source = tmp_path / "big.pdf"
    noise.save(source, format="PDF", resolution=600.0)
    return source


def test_normalising_shrinks_an_image_heavy_pdf(image_heavy_pdf, tmp_path):
    destination = tmp_path / "small.pdf"

    assert normalise_pdf(image_heavy_pdf, destination) is True
    assert destination.stat().st_size < image_heavy_pdf.stat().st_size


def test_normalising_keeps_every_page(image_heavy_pdf, tmp_path):
    """The page range is applied at the printer. A normalised copy that lost or
    gained a page would make "pages 5-10" mean something different there."""
    destination = tmp_path / "small.pdf"
    normalise_pdf(image_heavy_pdf, destination)

    with pikepdf.open(image_heavy_pdf) as before, pikepdf.open(destination) as after:
        assert len(after.pages) == len(before.pages)


def test_a_normalised_copy_that_lost_pages_is_thrown_away(tmp_path, monkeypatch):
    """Belt and braces on the rule above: if Ghostscript ever returns a
    different page count, the result is discarded rather than printed."""
    source = tmp_path / "three.pdf"
    source.write_bytes(make_pdf(pages=3))

    def _truncating_gs(command, **kwargs):
        destination = next(a for a in command if a.startswith("-sOutputFile="))
        with open(destination.removeprefix("-sOutputFile="), "wb") as handle:
            handle.write(make_pdf(pages=1))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", _truncating_gs)

    destination = tmp_path / "out.pdf"
    assert normalise_pdf(source, destination) is False
    assert not destination.exists()


def test_a_normalised_copy_that_got_bigger_is_thrown_away(tmp_path, monkeypatch):
    source = tmp_path / "small.pdf"
    source.write_bytes(make_pdf(pages=1))

    def _inflating_gs(command, **kwargs):
        destination = next(a for a in command if a.startswith("-sOutputFile="))
        with open(destination.removeprefix("-sOutputFile="), "wb") as handle:
            handle.write(make_pdf(pages=1) + b"%" + b" " * 100_000)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", _inflating_gs)

    destination = tmp_path / "out.pdf"
    assert normalise_pdf(source, destination) is False
    assert not destination.exists()


def test_normalising_gives_up_rather_than_hanging(tmp_path, monkeypatch):
    """A crafted PDF can make Ghostscript run forever. The timeout is the only
    thing between that and a wedged worker."""
    source = tmp_path / "in.pdf"
    source.write_bytes(make_pdf(pages=1))

    def _hanging_gs(command, **kwargs):
        assert kwargs.get("timeout"), "Ghostscript must never run without a timeout"
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", _hanging_gs)

    assert normalise_pdf(source, tmp_path / "out.pdf") is False


def test_a_missing_ghostscript_is_not_a_failed_upload(tmp_path, monkeypatch):
    """Normalisation is an optimisation. The original prints perfectly well, so
    losing Ghostscript must degrade the service rather than stop it."""
    source = tmp_path / "in.pdf"
    source.write_bytes(make_pdf(pages=1))
    monkeypatch.setattr(pdfs, "ghostscript_executable", lambda: None)

    assert normalise_pdf(source, tmp_path / "out.pdf") is False


def test_a_ghostscript_that_crashes_is_not_a_failed_upload(tmp_path, monkeypatch):
    source = tmp_path / "in.pdf"
    source.write_bytes(make_pdf(pages=1))

    def _failing_gs(command, **kwargs):
        return subprocess.CompletedProcess(command, 1, "", "gs: unrecoverable error")

    monkeypatch.setattr(subprocess, "run", _failing_gs)

    assert normalise_pdf(source, tmp_path / "out.pdf") is False


def test_normalising_writes_nothing_next_to_the_source_until_it_has_succeeded(
    tmp_path, monkeypatch
):
    """Ghostscript writes to a scratch directory, and only a verified result is
    moved into place. A half-written file in the storage tree would otherwise be
    served to a printer."""
    source = tmp_path / "in.pdf"
    source.write_bytes(make_pdf(pages=1))
    destination = tmp_path / "nested" / "out.pdf"

    def _gs_writing_elsewhere(command, **kwargs):
        target = next(a for a in command if a.startswith("-sOutputFile="))
        written = target.removeprefix("-sOutputFile=")
        assert not str(written).startswith(str(destination.parent)), (
            "Ghostscript must not write directly into the storage tree"
        )
        return subprocess.CompletedProcess(command, 1, "", "")

    monkeypatch.setattr(subprocess, "run", _gs_writing_elsewhere)

    assert normalise_pdf(source, destination) is False
