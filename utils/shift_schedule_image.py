from __future__ import annotations

import ctypes
import math
from functools import cache
from io import BytesIO
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c
from PIL import Image, ImageChops

if TYPE_CHECKING:
    from collections.abc import Sequence

SCHEDULE_IMAGE_DPI = 192
SCHEDULE_IMAGE_BORDER_PX = 24
MAX_SCHEDULE_IMAGE_PIXELS = 25_000_000
_PDF_DPI = 72
_WHITE = (255, 255, 255)
_FONT_DIR = Path(__file__).parents[1] / "resources/assets/shift_notice"
_FONT_SPECS = (
    (
        (b"Noto Sans CJK JP", b"NotoSansCJKjp-Thin"),
        "NotoSansCJKjp-VF.otf",
        (
            pdfium_c.FXFONT_SHIFTJIS_CHARSET,
            pdfium_c.FXFONT_HANGEUL_CHARSET,
            pdfium_c.FXFONT_GB2312_CHARSET,
            pdfium_c.FXFONT_CHINESEBIG5_CHARSET,
        ),
    ),
    (
        (b"Noto Sans", b"NotoSans-Regular"),
        "NotoSans-VF.ttf",
        (
            pdfium_c.FXFONT_ANSI_CHARSET,
            pdfium_c.FXFONT_GREEK_CHARSET,
            pdfium_c.FXFONT_VIETNAMESE_CHARSET,
            pdfium_c.FXFONT_CYRILLIC_CHARSET,
            pdfium_c.FXFONT_EASTERNEUROPEAN_CHARSET,
        ),
    ),
    (
        (b"Noto Sans Symbols 2", b"NotoSansSymbols2-Regular"),
        "NotoSansSymbols2-Regular.ttf",
        (pdfium_c.FXFONT_SYMBOL_CHARSET,),
    ),
    (
        (b"Unifont",),
        "unifont.otf",
        (
            pdfium_c.FXFONT_DEFAULT_CHARSET,
            pdfium_c.FXFONT_HEBREW_CHARSET,
            pdfium_c.FXFONT_ARABIC_CHARSET,
            pdfium_c.FXFONT_THAI_CHARSET,
        ),
    ),
    (
        (b"Noto Emoji", b"NotoEmoji-Regular", b"NotoEmoji"),
        "NotoEmoji-VF.ttf",
        (pdfium_c.FXFONT_DEFAULT_CHARSET,),
    ),
)
_FONT_INFO_LOCK = Lock()
_font_info: _BundledScheduleFontInfo | None = None


class ScheduleImageRenderError(Exception):
    """Raised when a schedule PDF cannot be rendered safely."""


class ScheduleImageTooLargeError(ScheduleImageRenderError):
    """Raised when a schedule image exceeds the pixel plan."""


@cache
def _font_bytes(path: Path) -> bytes:
    return path.read_bytes()


class _BundledScheduleFontInfo(pdfium.PdfSysfontBase):
    """Expose bundled schedule fonts through PDFium's system-font interface."""

    def __init__(self) -> None:
        super().__init__()
        self._fonts_by_face: dict[bytes, tuple[Path, int, bytes]] = {}
        self._fonts_by_handle: dict[int, tuple[Path, int, bytes]] = {}
        self._fallbacks: dict[int, tuple[Path, int, bytes]] = {}
        for names, filename, charsets in _FONT_SPECS:
            font = (_FONT_DIR / filename, charsets[0], names[0])
            self._fonts_by_handle[id(font)] = font
            for name in names:
                self._fonts_by_face[name] = font
            for charset in charsets:
                self._fallbacks.setdefault(charset, font)

    def EnumFonts(self, _: object, mapper: object) -> None:  # noqa: N802
        super().EnumFonts(_, mapper)
        for names, _filename, charsets in _FONT_SPECS:
            for name in names:
                for charset in charsets:
                    pdfium_c.FPDF_AddInstalledFont(mapper, name, charset)

    def MapFont(  # noqa: N802, PLR0913
        self,
        _: object,
        weight: int,
        italic: int,
        charset: int,
        pitch_family: int,
        face: object,
        exact: object,
    ) -> object:
        face_name = ctypes.cast(face, ctypes.c_char_p).value or b""
        face_name = face_name.rsplit(b"+", maxsplit=1)[-1]
        font = self._fonts_by_face.get(face_name) or self._fallbacks.get(charset)
        if font is not None:
            return id(font)
        return super().MapFont(
            _,
            weight,
            italic,
            charset,
            pitch_family,
            face,
            exact,
        )

    def GetFont(self, _: object, face: object) -> object:  # noqa: N802
        face_name = ctypes.cast(face, ctypes.c_char_p).value or b""
        face_name = face_name.rsplit(b"+", maxsplit=1)[-1]
        font = self._fonts_by_face.get(face_name)
        return id(font) if font is not None else super().GetFont(_, face)

    def GetFontData(  # noqa: N802
        self,
        _: object,
        handle: object,
        table: int,
        buffer: object,
        buffer_size: int,
    ) -> int:
        font = self._fonts_by_handle.get(getattr(handle, "value", handle))
        if font is None:
            return super().GetFontData(_, handle, table, buffer, buffer_size)
        if table:
            return 0
        data = _font_bytes(font[0])
        if not buffer or buffer_size < len(data):
            return len(data)
        ctypes.memmove(buffer, data, len(data))
        return len(data)

    def GetFaceName(  # noqa: N802
        self,
        _: object,
        handle: object,
        buffer: object,
        buffer_size: int,
    ) -> int:
        font = self._fonts_by_handle.get(getattr(handle, "value", handle))
        if font is None:
            return super().GetFaceName(_, handle, buffer, buffer_size)
        face = font[2] + b"\0"
        if buffer and buffer_size >= len(face):
            ctypes.memmove(buffer, face, len(face))
        return len(face)

    def GetFontCharset(  # noqa: N802
        self,
        _: object,
        handle: object,
    ) -> int:
        font = self._fonts_by_handle.get(getattr(handle, "value", handle))
        return font[1] if font is not None else super().GetFontCharset(_, handle)

    def DeleteFont(  # noqa: N802
        self,
        _: object,
        handle: object,
    ) -> None:
        if getattr(handle, "value", handle) not in self._fonts_by_handle:
            super().DeleteFont(_, handle)


def _ensure_bundled_fonts() -> None:
    global _font_info  # noqa: PLW0603
    if _font_info is not None:
        return
    with _FONT_INFO_LOCK:
        if _font_info is None:
            font_info = _BundledScheduleFontInfo()
            font_info.setup()
            _font_info = font_info


def render_schedule_pdf_to_png(pdf_bytes: bytes) -> bytes:
    """Render and combine a Google Sheets PDF entirely in memory."""
    if not isinstance(pdf_bytes, bytes) or not pdf_bytes:
        raise ScheduleImageRenderError

    rendered_pages: list[Image.Image] = []
    try:
        _ensure_bundled_fonts()
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
