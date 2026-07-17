from __future__ import annotations

from io import BytesIO

import pypdfium2 as pdfium
import pytest
from PIL import Image, ImageDraw

from utils import shift_schedule_image as image_module
from utils.shift_schedule_image import (
    ScheduleImageRenderError,
    ScheduleImageTooLargeError,
    _compose_schedule_pages,
    render_schedule_pdf_to_png,
)


def make_pdf_bytes(*page_sizes: tuple[float, float]) -> bytes:
    document = pdfium.PdfDocument.new()
    try:
        for width, height in page_sizes:
            page = document.new_page(width, height)
            page.close()
        output = BytesIO()
        document.save(output)
        return output.getvalue()
    finally:
        document.close()


def make_page(
    size: tuple[int, int] = (100, 80),
    rectangle: tuple[int, int, int, int] | None = None,
) -> Image.Image:
    page = Image.new("RGB", size, "white")
    if rectangle is not None:
        ImageDraw.Draw(page).rectangle(rectangle, fill="black")
    return page


def test_render_schedule_pdf_to_png_renders_blank_page_at_192_dpi() -> None:
    png_bytes = render_schedule_pdf_to_png(make_pdf_bytes((72, 36)))

    with Image.open(BytesIO(png_bytes)) as image:
        assert image.format == "PNG"
        assert image.mode == "RGB"
        assert image.size == (240, 144)


def test_render_schedule_pdf_to_png_rejects_invalid_and_zero_page_pdf() -> None:
    with pytest.raises(ScheduleImageRenderError):
        render_schedule_pdf_to_png(b"not a PDF")

    with pytest.raises(ScheduleImageRenderError):
        render_schedule_pdf_to_png(make_pdf_bytes())


def test_compose_schedule_pages_uses_common_horizontal_and_outer_vertical_crop() -> (
    None
):
    pages = [
        make_page(rectangle=(10, 20, 89, 79)),
        make_page(rectangle=(15, 0, 84, 59)),
    ]
    try:
        png_bytes = _compose_schedule_pages(pages)
    finally:
        for page in pages:
            page.close()

    with Image.open(BytesIO(png_bytes)) as image:
        assert image.mode == "RGB"
        assert image.size == (128, 168)
        assert image.getpixel((44, 83)) == (0, 0, 0)
        assert image.getpixel((44, 84)) == (0, 0, 0)
        assert image.getpixel((23, 24)) == (255, 255, 255)
        assert image.getpixel((24, 24)) == (0, 0, 0)


def test_compose_schedule_pages_keeps_complete_middle_page() -> None:
    pages = [
        make_page(rectangle=(10, 20, 89, 79)),
        make_page(rectangle=(40, 30, 49, 39)),
        make_page(rectangle=(15, 0, 84, 59)),
    ]
    try:
        png_bytes = _compose_schedule_pages(pages)
    finally:
        for page in pages:
            page.close()

    with Image.open(BytesIO(png_bytes)) as image:
        assert image.size == (128, 248)
        assert image.getpixel((64, 103)) == (255, 255, 255)
        assert image.getpixel((60, 114)) == (0, 0, 0)


def test_compose_schedule_pages_retains_all_white_page_full_bounds() -> None:
    pages = [
        make_page(rectangle=(10, 20, 89, 79)),
        make_page(),
        make_page(rectangle=(15, 0, 84, 59)),
    ]
    try:
        png_bytes = _compose_schedule_pages(pages)
    finally:
        for page in pages:
            page.close()

    with Image.open(BytesIO(png_bytes)) as image:
        assert image.size == (148, 248)
        assert image.getpixel((0, 0)) == (255, 255, 255)
        assert image.getpixel((24, 24)) == (255, 255, 255)
        assert image.getpixel((34, 24)) == (0, 0, 0)


def test_compose_schedule_pages_checks_output_cap_before_canvas_allocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = make_page(rectangle=(10, 20, 89, 79))
    original_new = Image.new

    def guarded_new(
        mode: str,
        size: tuple[int, int],
        color: str | tuple[int, int, int] = 0,
    ) -> Image.Image:
        if size == (128, 108):
            pytest.fail("final canvas allocated before pixel-cap check")
        return original_new(mode, size, color)

    monkeypatch.setattr(image_module, "MAX_SCHEDULE_IMAGE_PIXELS", 128 * 108 - 1)
    monkeypatch.setattr(Image, "new", guarded_new)
    try:
        with pytest.raises(ScheduleImageTooLargeError):
            _compose_schedule_pages([page])
    finally:
        page.close()


def test_render_schedule_pdf_to_png_rejects_aggregate_raster_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(image_module, "MAX_SCHEDULE_IMAGE_PIXELS", 192 * 96 - 1)

    with pytest.raises(ScheduleImageTooLargeError):
        render_schedule_pdf_to_png(make_pdf_bytes((72, 36)))
