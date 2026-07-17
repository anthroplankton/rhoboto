from __future__ import annotations

import math
from io import BytesIO
from typing import TYPE_CHECKING

import pypdfium2 as pdfium
from PIL import Image, ImageChops

if TYPE_CHECKING:
    from collections.abc import Sequence

SCHEDULE_IMAGE_DPI = 192
SCHEDULE_IMAGE_BORDER_PX = 24
MAX_SCHEDULE_IMAGE_PIXELS = 25_000_000
_PDF_DPI = 72
_WHITE = (255, 255, 255)


class ScheduleImageRenderError(Exception):
    """Raised when a schedule PDF cannot be rendered safely."""


class ScheduleImageTooLargeError(ScheduleImageRenderError):
    """Raised when a schedule image exceeds the pixel plan."""


def render_schedule_pdf_to_png(pdf_bytes: bytes) -> bytes:
    """Render and combine a Google Sheets PDF entirely in memory."""
    if not isinstance(pdf_bytes, bytes) or not pdf_bytes:
        raise ScheduleImageRenderError

    rendered_pages: list[Image.Image] = []
    try:
        _append_rendered_pdf_pages(pdf_bytes, rendered_pages)
        return _compose_schedule_pages(rendered_pages)
    except ScheduleImageRenderError:
        raise
    except Exception as exc:
        raise ScheduleImageRenderError from exc
    finally:
        for rendered_page in rendered_pages:
            rendered_page.close()


def _append_rendered_pdf_pages(
    pdf_bytes: bytes,
    rendered_pages: list[Image.Image],
) -> None:
    with pdfium.PdfDocument(pdf_bytes) as document:
        if not len(document):
            raise ScheduleImageRenderError

        scale = SCHEDULE_IMAGE_DPI / _PDF_DPI
        raster_pixels = 0
        for index in range(len(document)):
            page = document[index]
            try:
                width, height = page.get_size()
            finally:
                page.close()
            if (
                not math.isfinite(width)
                or not math.isfinite(height)
                or width <= 0
                or height <= 0
            ):
                raise ScheduleImageRenderError
            raster_pixels += math.ceil(width * scale) * math.ceil(height * scale)
            if raster_pixels > MAX_SCHEDULE_IMAGE_PIXELS:
                raise ScheduleImageTooLargeError

        for index in range(len(document)):
            page = document[index]
            bitmap = None
            try:
                bitmap = page.render(
                    scale=scale,
                    fill_color=(255, 255, 255, 255),
                    rev_byteorder=True,
                )
                rendered_pages.append(bitmap.to_pil().convert("RGB"))
            finally:
                if bitmap is not None:
                    bitmap.close()
                page.close()


def _compose_schedule_pages(pages: Sequence[Image.Image]) -> bytes:
    if not pages:
        raise ScheduleImageRenderError
    if any(page.mode != "RGB" or page.width <= 0 or page.height <= 0 for page in pages):
        raise ScheduleImageRenderError
    if any(page.width != pages[0].width for page in pages[1:]):
        raise ScheduleImageRenderError

    boundaries = [_content_boundary(page) for page in pages]
    common_left = min(boundary[0] for boundary in boundaries)
    common_right = max(boundary[2] for boundary in boundaries)

    cropped_pages: list[Image.Image] = []
    output = None
    try:
        final_index = len(pages) - 1
        for index, (page, boundary) in enumerate(zip(pages, boundaries, strict=True)):
            top = boundary[1] if index == 0 else 0
            bottom = boundary[3] if index == final_index else page.height
            cropped_pages.append(page.crop((common_left, top, common_right, bottom)))

        inner_width = common_right - common_left
        inner_height = sum(page.height for page in cropped_pages)
        output_width = inner_width + 2 * SCHEDULE_IMAGE_BORDER_PX
        output_height = inner_height + 2 * SCHEDULE_IMAGE_BORDER_PX
        if output_width * output_height > MAX_SCHEDULE_IMAGE_PIXELS:
            raise ScheduleImageTooLargeError

        output = Image.new("RGB", (output_width, output_height), _WHITE)
        y = SCHEDULE_IMAGE_BORDER_PX
        for page in cropped_pages:
            output.paste(page, (SCHEDULE_IMAGE_BORDER_PX, y))
            y += page.height

        encoded = BytesIO()
        output.save(encoded, format="PNG")
        return encoded.getvalue()
    finally:
        for cropped_page in cropped_pages:
            cropped_page.close()
        if output is not None:
            output.close()


def _content_boundary(page: Image.Image) -> tuple[int, int, int, int]:
    white = Image.new("RGB", page.size, _WHITE)
    try:
        difference = ImageChops.difference(page, white)
        try:
            boundary = difference.getbbox()
        finally:
            difference.close()
    finally:
        white.close()
    if boundary is None:
        return (0, 0, page.width, page.height)
    return boundary
