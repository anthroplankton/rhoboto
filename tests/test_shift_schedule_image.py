from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c
import pytest
from fontTools.ttLib import TTFont
from PIL import Image, ImageChops, ImageDraw

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


def make_unifont_pdf_pair(text: str) -> tuple[bytes, bytes]:
    font_bytes = (
        Path(__file__).parents[1] / "resources/assets/shift_notice/unifont.otf"
    ).read_bytes()
    font_buffer = (pdfium_c.uint8_t * len(font_bytes)).from_buffer_copy(font_bytes)
    document = pdfium.PdfDocument.new()
    font = None
    page = None
    try:
        raw_font = pdfium_c.FPDFText_LoadFont(
            document,
            font_buffer,
            len(font_bytes),
            pdfium_c.FPDF_FONT_TRUETYPE,
            1,
        )
        assert raw_font
        font = pdfium.PdfFont(raw_font, needs_free=True)
        raw_text = pdfium_c.FPDFPageObj_CreateTextObj(document, font, 24)
        assert raw_text
        text_object = pdfium.PdfTextObj(raw_text, pdf=document)
        encoded = text.encode("utf-16-le") + b"\0\0"
        wide_text = (pdfium_c.FPDF_WCHAR * (len(encoded) // 2)).from_buffer_copy(
            encoded
        )
        assert pdfium_c.FPDFText_SetText(text_object, wide_text)
        text_object.set_matrix(pdfium.PdfMatrix().translate(10, 50))

        page = document.new_page(200, 100)
        page.insert_obj(text_object)
        page.gen_content()
        output = BytesIO()
        document.save(output)
    finally:
        if page is not None:
            page.close()
        if font is not None:
            font.close()
        document.close()

    embedded = output.getvalue()
    assert embedded.count(b"/FontFile2") == 1
    nonembedded = embedded.replace(b"/FontFile2", b"/XontFile2")
    assert len(nonembedded) == len(embedded)
    return embedded, nonembedded


def make_emoji_pdf(text: str, *, embedded: bool) -> bytes:
    font_path = (
        Path(__file__).parents[1] / "resources/assets/shift_notice/NotoEmoji-VF.ttf"
    )
    font = TTFont(font_path)
    try:
        glyph_name = font.getBestCmap()[ord(text)]
        glyph_id = font.getGlyphID(glyph_name)
        advance = round(font["hmtx"][glyph_name][0] * 1000 / font["head"].unitsPerEm)
    finally:
        font.close()

    def pdf_stream(data: bytes, extra: bytes = b"") -> bytes:
        return (
            f"<< /Length {len(data)} ".encode()
            + extra
            + b">>\nstream\n"
            + data
            + b"\nendstream"
        )

    text_utf16 = text.encode("utf-16-be").hex().upper()
    to_unicode = f"""/CIDInit /ProcSet findresource begin
12 dict begin
begincmap
/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def
/CMapName /Adobe-Identity-UCS def
/CMapType 2 def
1 begincodespacerange
<0001> <0001>
endcodespacerange
1 beginbfchar
<0001> <{text_utf16}>
endbfchar
endcmap
CMapName currentdict /CMap defineresource pop
end
end""".encode()
    font_file = b" /FontFile2 10 0 R" if embedded else b""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 100] "
            b"/Resources << /Font << /F1 4 0 R >> >> /Contents 9 0 R >>"
        ),
        (
            b"<< /Type /Font /Subtype /Type0 /BaseFont /NotoEmoji "
            b"/Encoding /Identity-H /DescendantFonts [5 0 R] /ToUnicode 7 0 R >>"
        ),
        (
            b"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /NotoEmoji "
            b"/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) "
            b"/Supplement 0 >> /FontDescriptor 6 0 R "
            + f"/W [1 [{advance}]] ".encode()
            + b"/CIDToGIDMap 8 0 R >>"
        ),
        (
            b"<< /Type /FontDescriptor /FontName /NotoEmoji /Flags 4 "
            b"/FontBBox [-1000 -1000 2000 2000] /ItalicAngle 0 "
            b"/Ascent 1000 /Descent -300 /CapHeight 1000 /StemV 80" + font_file + b" >>"
        ),
        pdf_stream(to_unicode),
        pdf_stream(b"\0\0" + glyph_id.to_bytes(2, "big")),
        pdf_stream(b"BT /F1 24 Tf 10 50 Td <0001> Tj ET"),
    ]
    if embedded:
        font_data = font_path.read_bytes()
        objects.append(pdf_stream(font_data, f"/Length1 {len(font_data)} ".encode()))

    output = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, object_data in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(f"{number} 0 obj\n".encode() + object_data + b"\nendobj\n")
    xref_offset = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        output.extend(f"{offset:010d} 00000 n \n".encode())
    output.extend(
        f"trailer << /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n".encode()
    )
    return bytes(output)


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


def test_render_schedule_pdf_to_png_uses_bundled_font_for_nonembedded_unicode() -> None:
    embedded_pdf, nonembedded_pdf = make_unifont_pdf_pair("૮ა中")
    expected_png = render_schedule_pdf_to_png(embedded_pdf)
    actual_png = render_schedule_pdf_to_png(nonembedded_pdf)

    with (
        Image.open(BytesIO(expected_png)) as expected,
        Image.open(BytesIO(actual_png)) as actual,
    ):
        assert actual.size == expected.size
        assert ImageChops.difference(actual, expected).getbbox() is None


def test_render_schedule_pdf_to_png_uses_bundled_font_for_nonembedded_emoji() -> None:
    expected_png = render_schedule_pdf_to_png(make_emoji_pdf("🌙", embedded=True))
    actual_png = render_schedule_pdf_to_png(make_emoji_pdf("🌙", embedded=False))

    with (
        Image.open(BytesIO(expected_png)) as expected,
        Image.open(BytesIO(actual_png)) as actual,
    ):
        assert actual.size == expected.size
        assert ImageChops.difference(actual, expected).getbbox() is None


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
