"""Photo printing: images arranged on an A4 canvas, rendered here.

The client sends the image files and a layout saying where each one goes; the
server composes A4 pages and produces a PDF, which then goes through **exactly
the same door as any other upload**. That is the point of doing it this way: a
photo job cannot drift away from a document job, because after this module they
are the same thing.

The behavioural change from the backend being replaced is that this refuses
rather than skips. That implementation `continue`d past anything it did not
understand -- a malformed element, an image index that named nothing, a page
with no elements -- and printed what was left. A student could pay for four
photos and be handed three, with no error anywhere. Here a layout is either
renderable in full or rejected with a sentence saying what is wrong.

Geometry is the exception, and deliberately so: a drag that finished slightly
off the canvas is a gesture, not a mistake, and the client's own preview clamps
it identically.
"""

import io
import json
import math
from dataclasses import dataclass

from PIL import Image, ImageOps, UnidentifiedImageError

from app.core.errors import BadRequest

# A4 at 300dpi. Below this, text in a photo of a page stops being readable and
# edges visibly stair-step.
A4_WIDTH_PX = 2480
A4_HEIGHT_PX = 3508

# Bounds on one request. Each element is a resize, a rotate and a composite at
# print resolution, so an unbounded layout pins a core for minutes.
MAX_IMAGES = 20
MAX_LAYOUT_PAGES = 20
MAX_ELEMENTS = 200

# A small file can decode to a gigapixel image. Pillow refuses past this rather
# than allocating, which is the difference between a rejected upload and a dead
# worker.
MAX_IMAGE_PIXELS = 25_000_000
Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS

# The smallest fraction of the page an element may occupy. Zero would be an
# invisible photo the student is still charged for.
MIN_EXTENT = 0.01


@dataclass(frozen=True)
class Crop:
    """A rectangle of the source image, in fractions of its own size."""

    x: float
    y: float
    width: float
    height: float


@dataclass(frozen=True)
class Placement:
    """One image on one page, positioned by its centre in page fractions."""

    image_index: int
    x: float
    y: float
    width: float
    height: float
    rotation: float
    crop: Crop | None


@dataclass(frozen=True)
class LayoutPage:
    elements: list[Placement]


@dataclass(frozen=True)
class PhotoLayout:
    pages: list[LayoutPage]


def _number(raw: object, *, field: str, default: float | None = None) -> float:
    """A float, or a refusal naming the field.

    Never a silent default. The old code fell back to the middle of the page for
    any value it could not read, so a broken client put photos somewhere nobody
    chose and the student found out at the printer.
    """
    if raw is None and default is not None:
        return default
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise BadRequest(
            f"The photo layout has an unreadable {field}. Please rearrange the "
            "photos and try again."
        ) from None
    if math.isnan(value) or math.isinf(value):
        raise BadRequest(f"The photo layout has an unreadable {field}.")
    return value


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _crop_from(raw: object) -> Crop | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise BadRequest("The photo layout has an unreadable crop.")

    width = _clamp(_number(raw.get("width"), field="crop width", default=1.0), 0.0, 1.0)
    height = _clamp(
        _number(raw.get("height"), field="crop height", default=1.0), 0.0, 1.0
    )
    if width <= 0 or height <= 0:
        # An empty selection produces a zero-pixel image, and Pillow then fails
        # somewhere a long way from the cause.
        raise BadRequest("One of the photos is cropped down to nothing.")

    return Crop(
        x=_clamp(_number(raw.get("x"), field="crop x", default=0.0), 0.0, 1.0),
        y=_clamp(_number(raw.get("y"), field="crop y", default=0.0), 0.0, 1.0),
        width=width,
        height=height,
    )


def _placement_from(raw: object, *, image_count: int) -> Placement:
    if not isinstance(raw, dict):
        raise BadRequest("The photo layout contains something that is not a photo.")

    index_raw = raw.get("imageIndex")
    try:
        index = int(index_raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise BadRequest("The photo layout refers to a photo without saying which.") from None

    if index < 0 or index >= image_count:
        raise BadRequest(
            f"The photo layout refers to photo {index}, which was not uploaded. "
            "Please add the photos again."
        )

    return Placement(
        image_index=index,
        x=_clamp(_number(raw.get("x"), field="position", default=0.5), 0.0, 1.0),
        y=_clamp(_number(raw.get("y"), field="position", default=0.5), 0.0, 1.0),
        width=_clamp(_number(raw.get("width"), field="size", default=0.5), MIN_EXTENT, 1.0),
        height=_clamp(
            _number(raw.get("height"), field="size", default=0.5), MIN_EXTENT, 1.0
        ),
        rotation=_number(raw.get("rotation"), field="rotation", default=0.0),
        crop=_crop_from(raw.get("crop")),
    )


def parse_layout(raw: str | dict, *, image_count: int) -> PhotoLayout:
    """Validate a layout completely, or refuse it completely."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            raise BadRequest("The photo layout could not be read.") from None

    if not isinstance(raw, dict):
        raise BadRequest("The photo layout could not be read.")

    pages_raw = raw.get("pages")
    if not isinstance(pages_raw, list) or not pages_raw:
        raise BadRequest("There is nothing to print. Add at least one photo.")
    if len(pages_raw) > MAX_LAYOUT_PAGES:
        raise BadRequest(
            f"That is more than {MAX_LAYOUT_PAGES} pages of photos. "
            "Please split it into separate jobs."
        )

    pages: list[LayoutPage] = []
    total_elements = 0

    for number, page_raw in enumerate(pages_raw, start=1):
        if not isinstance(page_raw, dict):
            raise BadRequest(f"Page {number} of the photo layout could not be read.")

        elements_raw = page_raw.get("elements")
        if not isinstance(elements_raw, list) or not elements_raw:
            # Not skipped: a blank page still costs a sheet and still gets
            # charged for, so it is a mistake worth telling somebody about.
            raise BadRequest(f"Page {number} has no photos on it.")

        total_elements += len(elements_raw)
        if total_elements > MAX_ELEMENTS:
            raise BadRequest(
                f"That is more than {MAX_ELEMENTS} photos in one job. "
                "Please split it into separate jobs."
            )

        pages.append(
            LayoutPage(
                elements=[
                    _placement_from(element, image_count=image_count)
                    for element in elements_raw
                ]
            )
        )

    return PhotoLayout(pages=pages)


def _decode(data: bytes) -> Image.Image:
    if not data:
        raise BadRequest("One of the photos is empty.")
    try:
        image = Image.open(io.BytesIO(data))
        # Phone cameras record orientation in EXIF rather than in the pixels.
        # Without this, a portrait photo prints on its side.
        image = ImageOps.exif_transpose(image)
        image.load()
    except Image.DecompressionBombError:
        raise BadRequest("One of the photos is too large to process.") from None
    except (UnidentifiedImageError, OSError, ValueError):
        raise BadRequest("One of the photos could not be read.") from None

    # RGBA throughout so a transparent PNG composites rather than painting a
    # black rectangle over whatever is underneath it.
    return image.convert("RGBA")


def render_layout(layout: PhotoLayout, images: list[bytes]) -> bytes:
    """Compose the pages and return a PDF.

    The result goes through `create_document` like any other upload, so it is
    validated, page-counted, stored and normalised by the same code.
    """
    if not images:
        raise BadRequest("No photos were uploaded.")
    if len(images) > MAX_IMAGES:
        raise BadRequest(
            f"That is more than {MAX_IMAGES} photos in one job. "
            "Please split it into separate jobs."
        )

    decoded = [_decode(data) for data in images]

    pages: list[Image.Image] = []
    for page in layout.pages:
        canvas = Image.new("RGB", (A4_WIDTH_PX, A4_HEIGHT_PX), "white")

        for element in page.elements:
            source = decoded[element.image_index]
            work = _cropped(source, element.crop)

            box = (
                max(1, int(element.width * A4_WIDTH_PX)),
                max(1, int(element.height * A4_HEIGHT_PX)),
            )
            # Contain, not stretch: the client previews with object-fit:
            # contain, and resizing to the exact box distorted the photo --
            # obvious once it was also rotated.
            fitted = ImageOps.contain(work, box, method=Image.LANCZOS)

            if element.rotation:
                fitted = fitted.rotate(
                    -element.rotation, expand=True, resample=Image.BICUBIC
                )

            centre_x = int(element.x * A4_WIDTH_PX)
            centre_y = int(element.y * A4_HEIGHT_PX)
            corner = (centre_x - fitted.width // 2, centre_y - fitted.height // 2)

            canvas.paste(fitted, corner, fitted if fitted.mode == "RGBA" else None)

        pages.append(canvas)

    buffer = io.BytesIO()
    pages[0].save(
        buffer,
        format="PDF",
        save_all=len(pages) > 1,
        append_images=pages[1:],
        resolution=300.0,
    )
    return buffer.getvalue()


def _cropped(image: Image.Image, crop: Crop | None) -> Image.Image:
    if crop is None:
        return image

    left = int(crop.x * image.width)
    top = int(crop.y * image.height)
    right = min(image.width, int((crop.x + crop.width) * image.width))
    bottom = min(image.height, int((crop.y + crop.height) * image.height))

    # A crop that starts past the edge would otherwise produce an empty box.
    right = max(left + 1, right)
    bottom = max(top + 1, bottom)

    return image.crop((left, top, right, bottom))
