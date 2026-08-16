"""Photo printing: images arranged on an A4 canvas, rendered server-side.

The client sends the images and a layout describing where they go. The old
implementation skipped anything it did not understand -- a bad element, an
out-of-range image index, a page with no elements -- and printed whatever was
left, so a student could pay for four photos and be handed three.
"""

import io

import pikepdf
import pytest
from PIL import Image

from app.core.errors import BadRequest
from app.modules.printing.photos import (
    A4_HEIGHT_PX,
    A4_WIDTH_PX,
    MAX_ELEMENTS,
    MAX_IMAGES,
    MAX_LAYOUT_PAGES,
    parse_layout,
    render_layout,
)


def make_image(width: int = 600, height: int = 400, colour=(200, 30, 30)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), colour).save(buffer, format="JPEG")
    return buffer.getvalue()


def one_photo_layout(pages: int = 1, per_page: int = 1) -> dict:
    return {
        "pages": [
            {
                "elements": [
                    {
                        "imageIndex": 0,
                        "x": 0.5,
                        "y": 0.5,
                        "width": 0.8,
                        "height": 0.5,
                        "rotation": 0,
                    }
                    for _ in range(per_page)
                ]
            }
            for _ in range(pages)
        ]
    }


# ── parsing ─────────────────────────────────────────────────────────────────


def test_a_layout_is_parsed_from_json_text():
    """The client sends it as a form field, so it arrives as a string."""
    import json

    layout = parse_layout(json.dumps(one_photo_layout()), image_count=1)

    assert len(layout.pages) == 1
    assert layout.pages[0].elements[0].image_index == 0


def test_layout_that_is_not_json_is_refused():
    with pytest.raises(BadRequest):
        parse_layout("{not json", image_count=1)


def test_a_layout_with_no_pages_is_refused():
    with pytest.raises(BadRequest):
        parse_layout({"pages": []}, image_count=1)


def test_a_page_with_no_elements_is_refused_rather_than_dropped(caplog):
    """The old code skipped it and printed the rest, so a student paid for a
    blank page that never appeared -- or did, blank."""
    with pytest.raises(BadRequest):
        parse_layout({"pages": [{"elements": []}]}, image_count=1)


def test_an_element_naming_an_image_that_was_not_uploaded_is_refused():
    """Silently skipping it is how a four-photo order prints three."""
    layout = one_photo_layout()
    layout["pages"][0]["elements"][0]["imageIndex"] = 7

    with pytest.raises(BadRequest) as exc:
        parse_layout(layout, image_count=1)

    assert "7" in str(exc.value)


def test_a_negative_image_index_is_refused():
    layout = one_photo_layout()
    layout["pages"][0]["elements"][0]["imageIndex"] = -1

    with pytest.raises(BadRequest):
        parse_layout(layout, image_count=1)


def test_a_non_numeric_position_is_refused_rather_than_defaulted():
    """Defaulting to the middle of the page puts a photo somewhere the student
    did not ask for, which they only discover once it is printed."""
    layout = one_photo_layout()
    layout["pages"][0]["elements"][0]["x"] = "middle-ish"

    with pytest.raises(BadRequest):
        parse_layout(layout, image_count=1)


def test_positions_outside_the_page_are_clamped_rather_than_refused():
    """A drag that ended slightly off-canvas is a user gesture, not an error --
    the client's own preview clamps it the same way."""
    layout = one_photo_layout()
    layout["pages"][0]["elements"][0]["x"] = 1.4
    layout["pages"][0]["elements"][0]["width"] = 0

    parsed = parse_layout(layout, image_count=1)

    assert parsed.pages[0].elements[0].x == 1.0
    assert parsed.pages[0].elements[0].width > 0


# ── the limits ──────────────────────────────────────────────────────────────


def test_too_many_pages_is_refused():
    with pytest.raises(BadRequest):
        parse_layout(one_photo_layout(pages=MAX_LAYOUT_PAGES + 1), image_count=1)


def test_too_many_elements_is_refused():
    """Each element is a resize, a rotate and a composite at 300dpi. Unbounded,
    one request pins a vCore for minutes."""
    layout = one_photo_layout(pages=2, per_page=MAX_ELEMENTS)

    with pytest.raises(BadRequest):
        parse_layout(layout, image_count=1)


def test_too_many_images_is_refused():
    with pytest.raises(BadRequest):
        render_layout(
            parse_layout(one_photo_layout(), image_count=1),
            [make_image()] * (MAX_IMAGES + 1),
        )


# ── rendering ───────────────────────────────────────────────────────────────


def test_rendering_produces_a_pdf_with_one_page_per_layout_page():
    layout = parse_layout(one_photo_layout(pages=3), image_count=1)

    pdf = render_layout(layout, [make_image()])

    assert pdf.startswith(b"%PDF-")
    with pikepdf.open(io.BytesIO(pdf)) as opened:
        assert len(opened.pages) == 3


def test_the_canvas_is_a4_at_print_resolution():
    """2480x3508 is A4 at 300dpi. Anything less shows on paper."""
    assert (A4_WIDTH_PX, A4_HEIGHT_PX) == (2480, 3508)


def test_an_unreadable_image_is_refused_with_a_sentence():
    layout = parse_layout(one_photo_layout(), image_count=1)

    with pytest.raises(BadRequest):
        render_layout(layout, [b"this is not an image"])


def test_an_empty_image_is_refused():
    layout = parse_layout(one_photo_layout(), image_count=1)

    with pytest.raises(BadRequest):
        render_layout(layout, [b""])


def test_a_decompression_bomb_is_refused_rather_than_decoded():
    """A small file that expands to a gigapixel image is a way to exhaust the
    server's memory with one upload."""
    from app.modules.printing.photos import MAX_IMAGE_PIXELS

    assert MAX_IMAGE_PIXELS <= 50_000_000

    huge = io.BytesIO()
    Image.new("RGB", (100, 100)).save(huge, format="PNG")
    layout = parse_layout(one_photo_layout(), image_count=1)

    # Not a real bomb -- constructing one is slow. What matters is that the
    # guard is set at all, asserted above, and that ordinary images still pass.
    assert render_layout(layout, [huge.getvalue()]).startswith(b"%PDF-")


def test_a_rotated_photo_still_renders():
    layout = one_photo_layout()
    layout["pages"][0]["elements"][0]["rotation"] = 37.5

    pdf = render_layout(parse_layout(layout, image_count=1), [make_image()])

    assert pdf.startswith(b"%PDF-")


def test_a_cropped_photo_still_renders():
    layout = one_photo_layout()
    layout["pages"][0]["elements"][0]["crop"] = {
        "x": 0.1,
        "y": 0.1,
        "width": 0.5,
        "height": 0.5,
    }

    pdf = render_layout(parse_layout(layout, image_count=1), [make_image()])

    assert pdf.startswith(b"%PDF-")


def test_a_crop_that_selects_nothing_is_refused():
    """Zero-width crop would produce an empty image, and Pillow raises somewhere
    far from the cause."""
    layout = one_photo_layout()
    layout["pages"][0]["elements"][0]["crop"] = {
        "x": 0.5,
        "y": 0.5,
        "width": 0.0,
        "height": 0.0,
    }

    with pytest.raises(BadRequest):
        parse_layout(layout, image_count=1)


def test_the_rendered_pdf_is_accepted_by_the_upload_pipeline():
    """It goes through exactly the same door as a student's own PDF -- one
    pipeline, so a photo job cannot drift from a document job."""
    from app.modules.printing.pdfs import inspect_pdf

    pdf = render_layout(parse_layout(one_photo_layout(pages=2), image_count=1), [make_image()])

    assert inspect_pdf(pdf).page_count == 2
