# ruff: noqa: RUF001

from __future__ import annotations

import importlib
import inspect
import io
import urllib.request
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from PIL import Image, ImageChops, ImageDraw, ImageFont

from utils.shift_notice import (
    ShiftNoticeCaseKind,
    ShiftNoticeCutWindow,
    ShiftNoticeFrame,
    ShiftNoticeFrameState,
    civil_start,
)

if TYPE_CHECKING:
    from types import ModuleType

ASSET_DIR = Path(__file__).parents[1] / "resources/assets/shift_notice"
TWEMOJI_FILENAMES = (
    "1f503.png",
    "2198.png",
    "2199.png",
    "23ec.png",
    "23f9.png",
    "2b07.png",
)


@pytest.fixture(scope="module")
def renderer_module() -> ModuleType:
    return importlib.import_module("utils.shift_notice_renderer")


def test_vendored_assets_and_complete_licenses_exist() -> None:
    binary_assets = [
        ASSET_DIR / "NotoSansCJKjp-VF.otf",
        ASSET_DIR / "NotoSans-VF.ttf",
        ASSET_DIR / "NotoSansSymbols2-Regular.ttf",
        ASSET_DIR / "NotoColorEmoji.ttf",
        ASSET_DIR / "unifont.otf",
        *(ASSET_DIR / "twemoji" / name for name in TWEMOJI_FILENAMES),
    ]

    assert all(path.is_file() and path.stat().st_size > 0 for path in binary_assets)
    assert (ASSET_DIR / "OFL.txt").is_file()
    assert (ASSET_DIR / "OFL-NOTO-SANS.txt").is_file()
    assert (ASSET_DIR / "OFL-NOTO-SYMBOLS.txt").is_file()
    assert (ASSET_DIR / "OFL-NOTO-EMOJI.txt").is_file()
    assert (ASSET_DIR / "COPYING-UNIFONT.txt").is_file()
    assert (ASSET_DIR / "LICENSE-GRAPHICS").is_file()


def test_twemoji_assets_are_unmodified_rgba_compatible_72px_pngs() -> None:
    for filename in TWEMOJI_FILENAMES:
        with Image.open(ASSET_DIR / "twemoji" / filename) as image:
            image.load()
            assert image.format == "PNG"
            assert image.size == (72, 72)
            assert image.convert("RGBA").mode == "RGBA"


def test_variable_font_exposes_usable_regular_and_bold_instances() -> None:
    path = ASSET_DIR / "NotoSansCJKjp-VF.otf"
    probe = ImageFont.truetype(path, 32)
    variation_names = {name.decode() for name in probe.get_variation_names()}

    assert {"Regular", "Bold"} <= variation_names
    regular = ImageFont.truetype(path, 32)
    regular.set_variation_by_name("Regular")
    bold = ImageFont.truetype(path, 32)
    bold.set_variation_by_name("Bold")
    assert regular.getname()[1] == "Regular"
    assert bold.getname()[1] == "Bold"
    assert regular.getlength("日本語") > 0
    assert bold.getlength("日本語") > 0


def test_attribution_records_pins_paths_digests_and_unmodified_bytes() -> None:
    text = (ASSET_DIR / "ATTRIBUTION.md").read_text()

    assert "notofonts/noto-cjk@Sans2.004" in text
    assert "Sans/Variable/OTF/NotoSansCJKjp-VF.otf" in text
    assert "notofonts/latin-greek-cyrillic@" in text
    assert "notofonts/symbols@" in text
    assert "googlefonts/noto-emoji@" in text
    assert "unifoundry.com/unifont" in text
    assert "jdecked/twemoji@v17.0.3" in text
    assert all(filename in text for filename in TWEMOJI_FILENAMES)
    assert text.count("sha256:") == 17
    assert "unmodified" in text.lower()
    assert "render time" in text.lower()


def _frame(
    renderer: ModuleType,
    range_label: str,
    names: tuple[str | None, ...] = (None, None, None, None, None),
    hours: tuple[str | None, ...] = (None, None, None, None, None),
) -> object:
    return renderer.ShiftNoticeRenderFrame(range_label, names, hours)


def _render_inputs(renderer: ModuleType) -> dict[ShiftNoticeCaseKind, object]:
    start_next = _frame(
        renderer,
        "14–15",
        (None, "支援者A", "支援者B", None, "支援者C"),
        (None, "3h", "1h", None, "2h"),
    )
    transition_previous = _frame(
        renderer,
        "14–15",
        ("支援者D", "支援者A", None, "支援者C", "支援者B"),
        ("1h", "2h", None, "2h", "3h"),
    )
    transition_next = _frame(
        renderer,
        "15–16",
        ("支援者E", "支援者A", "支援者B", None, None),
        ("1h", "1h", "2h", None, None),
    )
    end_previous = _frame(
        renderer,
        "21–22",
        (None, "支援者F", "支援者G", None, "支援者C"),
        (None, "3h", "2h", None, "1h"),
    )
    cut_rows = tuple(
        ShiftNoticeFrame(
            civil_start=civil_start(date(2026, 8, 1), hour),
            event_hour=hour,
            source_id=1,
            state=ShiftNoticeFrameState.CUT,
            lanes=(None, None, None, None, None),
        )
        for hour in range(12, 19)
    )
    cut_window = ShiftNoticeCutWindow(
        rows=cut_rows,
        truncated_before=True,
        truncated_after=True,
    )
    cut_current = _frame(renderer, "15–16")
    return {
        ShiftNoticeCaseKind.START: renderer.ShiftNoticeRenderInput(
            ShiftNoticeCaseKind.START,
            None,
            start_next,
            None,
        ),
        ShiftNoticeCaseKind.TRANSITION: renderer.ShiftNoticeRenderInput(
            ShiftNoticeCaseKind.TRANSITION,
            transition_previous,
            transition_next,
            None,
        ),
        ShiftNoticeCaseKind.END: renderer.ShiftNoticeRenderInput(
            ShiftNoticeCaseKind.END,
            end_previous,
            None,
            None,
        ),
        ShiftNoticeCaseKind.CUT: renderer.ShiftNoticeRenderInput(
            ShiftNoticeCaseKind.CUT,
            None,
            cut_current,
            cut_window,
        ),
    }


def _open_rendered(data: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(data))
    image.load()
    return image


def test_start_transition_end_and_cut_render_nonempty_png_bytes(
    renderer_module: ModuleType,
) -> None:
    for value in _render_inputs(renderer_module).values():
        data = renderer_module.render_shift_notice(value)
        assert data.startswith(b"\x89PNG\r\n\x1a\n")
        assert len(data) > 1_000


def test_normal_cards_use_true_2x_width_and_cut_uses_seven_row_geometry(
    renderer_module: ModuleType,
) -> None:
    inputs = _render_inputs(renderer_module)

    for case in (
        ShiftNoticeCaseKind.START,
        ShiftNoticeCaseKind.TRANSITION,
        ShiftNoticeCaseKind.END,
    ):
        assert (
            _open_rendered(renderer_module.render_shift_notice(inputs[case])).width
            == 1972
        )
    assert _open_rendered(
        renderer_module.render_shift_notice(inputs[ShiftNoticeCaseKind.CUT])
    ).size == (1972, 800)


def test_normal_blank_label_cell_does_not_add_a_bottom_rule(
    renderer_module: ModuleType,
) -> None:
    renderer = renderer_module._Renderer()  # noqa: SLF001
    layout = renderer.layout
    canvas = renderer_module.Canvas(
        Image.new(
            "RGBA",
            (
                renderer_module.Canvas.p(layout.label_width),
                renderer_module.Canvas.p(layout.row_height * 2),
            ),
        )
    )
    row_y = 4

    renderer.draw_label_blank(canvas, row_y)

    boundary_y = renderer_module.Canvas.p(row_y + layout.row_height)
    line_color = renderer_module._hex_rgb(  # noqa: SLF001
        renderer.theme.label_line
    )
    assert (
        canvas.image.getpixel(
            (renderer_module.Canvas.p(layout.time_anchor_width / 2), boundary_y)
        )[:3]
        != line_color
    )
    assert (
        canvas.image.getpixel(
            (renderer_module.Canvas.p(layout.time_anchor_width + 24), boundary_y)
        )[:3]
        != line_color
    )


def test_normal_time_pair_keeps_right_separator_without_full_width_bottom_rule(
    renderer_module: ModuleType,
) -> None:
    renderer = renderer_module._Renderer()  # noqa: SLF001
    layout = renderer.layout
    canvas = renderer_module.Canvas(
        Image.new(
            "RGBA",
            (
                renderer_module.Canvas.p(layout.label_width),
                renderer_module.Canvas.p(layout.row_height * 3),
            ),
        )
    )
    pair_y = 4

    renderer.draw_time_pair(canvas, pair_y, "14–15", "現在枠", "残り時間")

    line_color = renderer_module._hex_rgb(  # noqa: SLF001
        renderer.theme.label_line
    )
    inner_line_color = renderer_module._hex_rgb(  # noqa: SLF001
        renderer.theme.label_inner_line
    )
    bottom_y = renderer_module.Canvas.p(pair_y + layout.row_height * 2)
    middle_y = renderer_module.Canvas.p(pair_y + layout.row_height)
    assert (
        canvas.image.getpixel(
            (renderer_module.Canvas.p(layout.time_anchor_width / 2), bottom_y)
        )[:3]
        != line_color
    )
    assert (
        canvas.image.getpixel(
            (renderer_module.Canvas.p(layout.time_anchor_width + 24), middle_y)
        )[:3]
        == line_color
    )
    assert (
        canvas.image.getpixel(
            (
                renderer_module.Canvas.p(layout.time_anchor_width),
                renderer_module.Canvas.p(pair_y + 12),
            )
        )[:3]
        == inner_line_color
    )


def test_cut_edge_markers_follow_truncation_flags(
    renderer_module: ModuleType,
) -> None:
    value = _render_inputs(renderer_module)[ShiftNoticeCaseKind.CUT]
    assert value.cut_window is not None

    def render_with_flags(*, before: bool, after: bool) -> Image.Image:
        window = renderer_module.ShiftNoticeCutWindow(
            value.cut_window.rows,
            before,
            after,
        )
        return _open_rendered(
            renderer_module.render_shift_notice(
                renderer_module.ShiftNoticeRenderInput(
                    ShiftNoticeCaseKind.CUT,
                    None,
                    value.next,
                    window,
                )
            )
        )

    no_markers = render_with_flags(before=False, after=False)
    leading = render_with_flags(before=True, after=False)
    trailing = render_with_flags(before=False, after=True)
    both = render_with_flags(before=True, after=True)

    # The marker belongs at the horizontal center of the right-side lane area
    # in the first/last existing CUT row; it must not consume another row or
    # alter the geometry.
    layout = renderer_module._Renderer().layout  # noqa: SLF001
    scale = 2
    margin = layout.single_margin * scale
    left = margin + layout.label_width * scale
    right = margin + layout.card_width * scale
    first_top = margin + layout.row_height * scale
    first_bottom = first_top + layout.row_height * scale
    last_top = first_top + layout.row_height * scale * 6
    last_bottom = last_top + layout.row_height * scale
    leading_box = (left, first_top, right, first_bottom)
    trailing_box = (left, last_top, right, last_bottom)

    def changed(box: tuple[int, int, int, int], image: Image.Image) -> bool:
        return (
            ImageChops.difference(no_markers.crop(box), image.crop(box)).getbbox()
            is not None
        )

    assert not changed(leading_box, no_markers)
    assert not changed(trailing_box, no_markers)
    assert changed(leading_box, leading)
    assert not changed(trailing_box, leading)
    assert not changed(leading_box, trailing)
    assert changed(trailing_box, trailing)
    assert changed(leading_box, both)
    assert changed(trailing_box, both)
    expected_center = (left + right) // 2
    for image, box in ((leading, leading_box), (trailing, trailing_box)):
        diff = ImageChops.difference(no_markers.crop(box), image.crop(box))
        marker_box = diff.getbbox()
        assert marker_box is not None
        marker_center = box[0] + (marker_box[0] + marker_box[2]) // 2
        assert abs(marker_center - expected_center) <= 8
    assert no_markers.size == leading.size == trailing.size == both.size == (1972, 800)


def test_cut_banner_label_marks_truncated_cross_day_window(
    renderer_module: ModuleType,
) -> None:
    rows = tuple(
        ShiftNoticeFrame(
            civil_start=civil_start(date(2026, 8, source_id), hour),
            event_hour=hour,
            source_id=source_id,
            state=ShiftNoticeFrameState.CUT,
            lanes=(None, None, None, None, None),
        )
        for source_id, hour in ((1, 26), (1, 27), (2, 4), (2, 5))
    )

    def label(*, before: bool, after: bool) -> str:
        return renderer_module._cut_banner_label(  # noqa: SLF001
            renderer_module.ShiftNoticeCutWindow(rows, before, after)
        )

    assert label(before=False, after=False) == "26–6"
    assert label(before=True, after=False) == "…26–6"
    assert label(before=False, after=True) == "26–6…"
    assert label(before=True, after=True) == "…26–6…"


def test_cut_title_and_current_row_share_separator_axis(
    renderer_module: ModuleType,
) -> None:
    """The title and focused row keep one measured fullwidth separator axis."""

    value = _render_inputs(renderer_module)[ShiftNoticeCaseKind.CUT]
    assert value.cut_window is not None

    class CaptureRenderer(renderer_module._Renderer):  # noqa: SLF001
        def __init__(self) -> None:
            super().__init__()
            self.calls: list[tuple[str, str, object, tuple[float, float] | None]] = []

        def draw_left_centered_text(  # noqa: PLR0913
            self,
            canvas: object,
            x: float,
            y_bounds: tuple[float, float],
            text: str,
            font: object,
            fill: str,
        ) -> None:
            self.calls.append(("left", text, x, y_bounds))
            super().draw_left_centered_text(canvas, x, y_bounds, text, font, fill)

    def separator_x(
        renderer: CaptureRenderer,
        y_bounds: tuple[float, float],
    ) -> float:
        for kind, text, placement, bounds in renderer.calls:
            if kind == "left" and text == "｜" and bounds == y_bounds:
                return placement  # type: ignore[return-value]
        msg = "CUT separator was not drawn for the requested row."
        raise AssertionError(msg)

    for before, after in ((False, False), (True, True)):
        window = renderer_module.ShiftNoticeCutWindow(
            value.cut_window.rows,
            before,
            after,
        )
        renderer = CaptureRenderer()
        renderer.render_cut_card(
            renderer_module.ShiftNoticeRenderInput(
                ShiftNoticeCaseKind.CUT,
                None,
                value.next,
                window,
            )
        )
        title_x = separator_x(renderer, (0, renderer.layout.row_height))
        row_x = separator_x(renderer, (176, 220))
        assert title_x == pytest.approx(row_x, abs=0.75)


def test_cut_uses_next_time_band_label_without_changing_normal_current_frame(
    renderer_module: ModuleType,
) -> None:
    class CaptureRenderer(renderer_module._Renderer):  # noqa: SLF001
        def __init__(self) -> None:
            super().__init__()
            self.texts: list[str] = []

        def draw_left_centered_text(  # noqa: PLR0913
            self,
            canvas: object,
            x: float,
            y_bounds: tuple[float, float],
            text: str,
            font: object,
            fill: str,
        ) -> None:
            self.texts.append(text)
            super().draw_left_centered_text(canvas, x, y_bounds, text, font, fill)

    cut = CaptureRenderer()
    cut_input = _render_inputs(renderer_module)[ShiftNoticeCaseKind.CUT]
    cut.render_cut_card(cut_input)
    assert any(
        tuple(cut.texts[index : index + 3]) == ("この時間", "｜", "シフトカット")
        for index in range(len(cut.texts) - 2)
    )

    normal = CaptureRenderer()
    canvas = renderer_module.Canvas(
        Image.new(
            "RGBA",
            (
                renderer_module.Canvas.p(normal.layout.label_width),
                renderer_module.Canvas.p(normal.layout.row_height * 2),
            ),
        )
    )
    normal.draw_time_pair(canvas, 0, "14–15", "累計時間", "現在枠")
    assert "現在枠" in normal.texts


def test_cut_text_groups_are_optically_balanced_around_schedule_area(
    renderer_module: ModuleType,
) -> None:
    """The shared axis balances complete title/current groups, not the glyph."""

    renderer = renderer_module._Renderer()  # noqa: SLF001
    image = Image.new("RGBA", (renderer.layout.card_width * 2, 200))
    canvas = renderer_module.Canvas(image)
    rect = renderer.cut_main_rect(0)
    range_label = "…14–21…"
    separator_x = renderer.cut_separator_axis(canvas.draw, rect, range_label)
    font = renderer.font(renderer.layout.banner_size, bold=True)
    gap = 2
    separator_width = renderer.measure_text(canvas.draw, "｜", font)[0]
    right_width = renderer.measure_text(canvas.draw, "シフトカット", font)[0]
    title_width = renderer.measure_text(canvas.draw, range_label, font)[0]
    current_width = renderer.measure_text(canvas.draw, "この時間", font)[0]
    schedule_center = (rect[0] + rect[2]) / 2

    def group_center(left_width: float) -> float:
        left = separator_x - gap - left_width
        right = separator_x + separator_width + gap + right_width
        return (left + right) / 2

    title_center = group_center(title_width)
    current_center = group_center(current_width)
    balanced_error = abs((title_center + current_center) / 2 - schedule_center)
    glyph_only_axis = (rect[0] + rect[2] - separator_width) / 2

    def glyph_only_center(left_width: float) -> float:
        left = glyph_only_axis - gap - left_width
        right = glyph_only_axis + separator_width + gap + right_width
        return (left + right) / 2

    glyph_only_error = abs(
        (glyph_only_center(title_width) + glyph_only_center(current_width)) / 2
        - schedule_center
    )
    assert balanced_error < 1
    assert balanced_error < glyph_only_error


def test_cut_header_uses_cut_band_without_changing_normal_header(
    renderer_module: ModuleType,
) -> None:
    """The CUT banner joins its left label cell; normal cards stay unchanged."""

    inputs = _render_inputs(renderer_module)
    cut = _open_rendered(
        renderer_module.render_shift_notice(inputs[ShiftNoticeCaseKind.CUT])
    )
    normal = _open_rendered(
        renderer_module.render_shift_notice(inputs[ShiftNoticeCaseKind.TRANSITION])
    )
    layout = renderer_module._Renderer().layout  # noqa: SLF001
    scale = 2
    margin = layout.single_margin * scale
    sample = (margin + 40 * scale, margin + 20 * scale)

    assert cut.getpixel(sample)[:3] == renderer_module._hex_rgb(  # noqa: SLF001
        renderer_module.Theme().cut_bg_a
    )
    assert normal.getpixel(sample)[:3] == renderer_module._hex_rgb(  # noqa: SLF001
        renderer_module.Theme().row_label_bg
    )


def test_cut_leading_marker_keeps_edge_current_title_visible(
    renderer_module: ModuleType,
) -> None:
    value = _render_inputs(renderer_module)[ShiftNoticeCaseKind.CUT]
    assert value.cut_window is not None
    edge_current = _frame(renderer_module, "12–13")
    no_marker_window = renderer_module.ShiftNoticeCutWindow(
        rows=value.cut_window.rows,
        truncated_before=False,
        truncated_after=False,
    )
    leading_window = renderer_module.ShiftNoticeCutWindow(
        rows=value.cut_window.rows,
        truncated_before=True,
        truncated_after=False,
    )

    def render(window: object) -> Image.Image:
        return _open_rendered(
            renderer_module.render_shift_notice(
                renderer_module.ShiftNoticeRenderInput(
                    ShiftNoticeCaseKind.CUT,
                    None,
                    edge_current,
                    window,
                )
            )
        )

    no_marker = render(no_marker_window)
    leading = render(leading_window)
    layout = renderer_module._Renderer().layout  # noqa: SLF001
    scale = 2
    margin = layout.single_margin * scale
    left = margin + layout.label_width * scale
    right = no_marker.width - margin
    marker_left = margin + layout.label_width * scale
    marker_right = margin + layout.card_width * scale
    first_top = margin + layout.row_height * scale
    marker_strip = (
        marker_left,
        first_top,
        marker_right,
        first_top + 12 * scale,
    )
    title_strip = (
        left,
        first_top + 12 * scale,
        right,
        first_top + layout.row_height * scale,
    )
    focus_border = (
        margin + (layout.label_width + layout.cut_focus_inset_x) * scale,
        first_top + layout.cut_focus_inset_y * scale,
        margin + (layout.card_width - layout.cut_focus_inset_x) * scale,
        first_top + (layout.cut_focus_inset_y + layout.cut_current_row_border) * scale,
    )

    assert (
        ImageChops.difference(
            no_marker.crop(marker_strip), leading.crop(marker_strip)
        ).getbbox()
        is not None
    )

    def current_text_pixels(image: Image.Image) -> int:
        color = renderer_module._hex_rgb(  # noqa: SLF001
            renderer_module.Theme().cut_current_text
        )
        return sum(
            pixel[:3] == color for pixel in image.crop(title_strip).get_flattened_data()
        )

    assert current_text_pixels(no_marker) > 0
    assert current_text_pixels(leading) > 0
    focus_diff = ImageChops.difference(
        no_marker.crop(focus_border), leading.crop(focus_border)
    )
    focus_area = focus_diff.width * focus_diff.height
    changed_focus_pixels = sum(
        pixel != (0, 0, 0) for pixel in focus_diff.get_flattened_data()
    )
    # The marker is painted before the focus frame. Downsampling can blend a
    # few pixels where the glyph crosses the frame, but the outline remains
    # present and unchanged across the surrounding central segment.
    assert changed_focus_pixels < focus_area * 0.05
    outline_sample = no_marker.getpixel((focus_border[0] + 100, focus_border[1]))
    background_sample = no_marker.getpixel((focus_border[0] + 100, focus_border[1] - 1))
    assert outline_sample != background_sample
    assert leading.getpixel((focus_border[0] + 100, focus_border[1])) == outline_sample


def test_png_metadata_reports_192_dpi(renderer_module: ModuleType) -> None:
    value = _render_inputs(renderer_module)[ShiftNoticeCaseKind.TRANSITION]
    image = _open_rendered(renderer_module.render_shift_notice(value))

    assert image.info["dpi"] == pytest.approx((192, 192), abs=0.1)


def test_lane_labels_are_fixed_japanese_and_locale_independent(
    renderer_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = _render_inputs(renderer_module)[ShiftNoticeCaseKind.TRANSITION]
    rendered = []
    for locale in ("en_US.UTF-8", "ja_JP.UTF-8", "zh_TW.UTF-8"):
        monkeypatch.setenv("LANG", locale)
        rendered.append(renderer_module.render_shift_notice(value))

    assert renderer_module.LANE_LABELS == (
        "アンコ",
        "本走",
        "本走",
        "本走",
        "待機",
    )
    assert len(set(rendered)) == 1
    assert tuple(inspect.signature(renderer_module.render_shift_notice).parameters) == (
        "value",
    )


def test_status_icons_and_inactive_lanes_match_v12(renderer_module: ModuleType) -> None:
    status = renderer_module.Status
    inputs = _render_inputs(renderer_module)
    transition = inputs[ShiftNoticeCaseKind.TRANSITION]

    assert renderer_module.derive_statuses(
        transition.previous,
        transition.next,
    ) == (
        status.SWAP,
        status.CONTINUE,
        status.MOVE_LEFT,
        status.END,
        status.MOVE_LEFT,
    )
    assert renderer_module.inactive_lanes(inputs[ShiftNoticeCaseKind.START]) == (
        True,
        False,
        False,
        True,
        False,
    )
    assert renderer_module.inactive_lanes(transition) == (
        False,
        False,
        False,
        False,
        False,
    )
    assert renderer_module.inactive_lanes(inputs[ShiftNoticeCaseKind.END]) == (
        True,
        False,
        False,
        True,
        False,
    )
    assert {
        kind: presentation.png_filename
        for kind, presentation in renderer_module.STATUS_PRESENTATION.items()
    } == {
        status.START: "2b07.png",
        status.END: "23f9.png",
        status.CONTINUE: "23ec.png",
        status.SWAP: "1f503.png",
        status.MOVE_RIGHT: "2198.png",
        status.MOVE_LEFT: "2199.png",
    }
    assert {
        kind: (presentation.left_text, presentation.right_text)
        for kind, presentation in renderer_module.STATUS_PRESENTATION.items()
    } == {
        status.START: ("開", "始"),
        status.END: ("終", "了"),
        status.CONTINUE: ("継", "続"),
        status.SWAP: ("交", "代"),
        status.MOVE_RIGHT: ("継続", ""),
        status.MOVE_LEFT: ("継続", ""),
    }


def test_cross_role_movement_arrow_is_painted(renderer_module: ModuleType) -> None:
    value = _render_inputs(renderer_module)[ShiftNoticeCaseKind.TRANSITION]
    image = _open_rendered(renderer_module.render_shift_notice(value)).convert("RGB")
    # Transition status row, third lane: the blue Twemoji arrow must survive
    # 4x drawing and the final downsample to the true 2x result.
    crop = image.crop((1048, 312, 1340, 400))
    assert any(b > r + 20 and b > g for r, g, b in crop.get_flattened_data())


def test_cross_role_continuation_does_not_mark_source_as_end(
    renderer_module: ModuleType,
) -> None:
    previous = _frame(
        renderer_module,
        "14–15",
        (None, None, None, None, "支援者A"),
    )
    next_frame = _frame(
        renderer_module,
        "15–16",
        (None, None, "支援者A", None, None),
    )

    assert renderer_module.derive_statuses(previous, next_frame) == (
        renderer_module.Status.NONE,
        renderer_module.Status.NONE,
        renderer_module.Status.MOVE_LEFT,
        renderer_module.Status.NONE,
        renderer_module.Status.MOVE_LEFT,
    )
    placements = renderer_module.derive_status_placements(previous, next_frame)
    assert placements[2].movement_endpoint == "destination"
    assert placements[4].movement_endpoint == "source"


def test_cross_role_move_keeps_occupied_different_lanes_as_handoffs(
    renderer_module: ModuleType,
) -> None:
    previous = _frame(
        renderer_module,
        "16–17",
        ("支援者A", "支援者B", None, "支援者C", "支援者D"),
    )
    next_frame = _frame(
        renderer_module,
        "17–18",
        ("支援者E", "支援者F", "支援者D", "支援者G", "支援者H"),
    )

    assert renderer_module.derive_statuses(previous, next_frame) == (
        renderer_module.Status.SWAP,
        renderer_module.Status.SWAP,
        renderer_module.Status.MOVE_LEFT,
        renderer_module.Status.SWAP,
        renderer_module.Status.SWAP,
    )


def test_cross_role_right_move_keeps_occupied_different_lanes_as_handoffs(
    renderer_module: ModuleType,
) -> None:
    previous = _frame(
        renderer_module,
        "16–17",
        ("支援者A", "支援者B", None, "支援者C", "支援者D"),
    )
    next_frame = _frame(
        renderer_module,
        "17–18",
        ("支援者E", "支援者F", "支援者A", "支援者G", "支援者H"),
    )

    assert renderer_module.derive_statuses(previous, next_frame) == (
        renderer_module.Status.SWAP,
        renderer_module.Status.SWAP,
        renderer_module.Status.MOVE_RIGHT,
        renderer_module.Status.SWAP,
        renderer_module.Status.SWAP,
    )


def test_one_honso_transition_lane_is_dimmed_while_other_lanes_have_status(
    renderer_module: ModuleType,
) -> None:
    previous = _frame(
        renderer_module,
        "20–21",
        ("支援者B", None, None, "支援者C", "支援者A"),
    )
    next_frame = _frame(
        renderer_module,
        "21–22",
        ("支援者F", None, "支援者A", "支援者G", "支援者E"),
    )

    assert renderer_module.derive_statuses(previous, next_frame) == (
        renderer_module.Status.SWAP,
        renderer_module.Status.NONE,
        renderer_module.Status.MOVE_LEFT,
        renderer_module.Status.SWAP,
        renderer_module.Status.SWAP,
    )
    value = renderer_module.ShiftNoticeRenderInput(
        renderer_module.ShiftNoticeCaseKind.TRANSITION,
        previous,
        next_frame,
        None,
    )
    assert renderer_module.inactive_lanes(value) == (
        False,
        True,
        False,
        False,
        False,
    )


def test_encore_transition_lane_can_be_the_only_inactive_lane(
    renderer_module: ModuleType,
) -> None:
    previous = _frame(
        renderer_module,
        "20–21",
        (None, "支援者A", "支援者B", "支援者C", "支援者D"),
    )
    next_frame = _frame(
        renderer_module,
        "21–22",
        (None, "支援者A", "支援者E", "支援者F", "支援者G"),
    )

    value = renderer_module.ShiftNoticeRenderInput(
        renderer_module.ShiftNoticeCaseKind.TRANSITION,
        previous,
        next_frame,
        None,
    )
    assert renderer_module.inactive_lanes(value) == (
        True,
        False,
        False,
        False,
        False,
    )


@pytest.mark.parametrize(
    ("source", "destination", "destination_previous_name", "expected"),
    [
        (4, 2, None, (2, "destination", ("継続", ""))),
        (4, 2, "先任", (4, "source", ("", "継続"))),
        (0, 2, None, (2, "destination", ("", "継続"))),
        (0, 2, "先任", (0, "source", ("継続", ""))),
    ],
)
def test_movement_status_retains_endpoint_and_copy_layout(
    renderer_module: ModuleType,
    source: int,
    destination: int,
    destination_previous_name: str | None,
    expected: tuple[int, str, tuple[str, str]],
) -> None:
    previous_names = [None] * 5
    next_names = [None] * 5
    previous_names[source] = "移動者"
    previous_names[destination] = destination_previous_name
    next_names[destination] = "移動者"
    previous = _frame(renderer_module, "14–15", tuple(previous_names))
    next_frame = _frame(renderer_module, "15–16", tuple(next_names))

    placements = renderer_module.derive_status_placements(previous, next_frame)
    index, endpoint, copy = expected
    placement = placements[index]
    assert placement.status is (
        renderer_module.Status.MOVE_LEFT
        if destination < source
        else renderer_module.Status.MOVE_RIGHT
    )
    assert placement.movement_endpoint == endpoint
    assert (
        renderer_module.status_copy(
            placement.status,
            placement.movement_endpoint,
        )
        == copy
    )


def test_movement_status_copy_places_arrow_at_endpoint_side(
    renderer_module: ModuleType,
) -> None:
    status = renderer_module.Status
    assert renderer_module.status_copy(status.MOVE_LEFT) == ("継続", "")
    assert renderer_module.status_copy(status.MOVE_RIGHT) == ("継続", "")
    assert renderer_module.status_copy(status.MOVE_LEFT, "source") == ("", "継続")
    assert renderer_module.status_copy(status.MOVE_LEFT, "destination") == ("継続", "")
    assert renderer_module.status_copy(status.MOVE_RIGHT, "source") == ("継続", "")
    assert renderer_module.status_copy(status.MOVE_RIGHT, "destination") == (
        "",
        "継続",
    )


def test_name_fitting_shrinks_before_using_middle_ellipsis(
    renderer_module: ModuleType,
) -> None:
    renderer = renderer_module._Renderer()  # noqa: SLF001
    draw = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    shrink_only = "長い名前テスト長い名前"

    size, _pad, _width, display = renderer.name_style(draw, shrink_only, 128)
    assert display == shrink_only
    assert size < renderer.layout.name_size

    very_long = "支援者名が非常に長いサンプル表示ABCDEFGHIJKLMN"
    _size, _pad, width, display = renderer.name_style(draw, very_long, 128)
    assert width <= 128
    assert display.startswith(very_long[0])
    assert display.endswith(very_long[-1])
    assert "…" in display


@pytest.mark.parametrize(
    ("compound", "max_width"),
    [
        ("👨‍👩‍👧‍👦", 65),
        ("👍🏽", 65),
        ("🇹🇼", 65),
        ("1️⃣", 65),
        ("e\u0301", 58),
    ],
)
def test_middle_ellipsis_never_splits_compound_name_tokens(
    renderer_module: ModuleType,
    compound: str,
    max_width: float,
) -> None:
    renderer = renderer_module._Renderer()  # noqa: SLF001
    draw = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    display = renderer._truncate_name_to_width(  # noqa: SLF001
        draw,
        f"AAAAA{compound}BBBBB",
        9,
        max_width,
    )

    assert "…" in display
    if compound not in display:
        assert not {
            character for character in compound if character not in {"\u200d", "\ufe0f"}
        } & set(display)


def test_mixed_name_uses_only_bundled_fallbacks_and_color_emoji(
    renderer_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_download(*_args: object, **_kwargs: object) -> None:
        pytest.fail("renderer attempted a runtime download")

    monkeypatch.setattr(urllib.request, "urlopen", fail_download)
    monkeypatch.setattr(urllib.request, "urlretrieve", fail_download)
    loaded_font_paths: list[Path] = []
    truetype = ImageFont.truetype

    def track_font(
        font: str | Path,
        *args: object,
        **kwargs: object,
    ) -> ImageFont.FreeTypeFont:
        loaded_font_paths.append(Path(font))
        return truetype(font, *args, **kwargs)

    renderer_module._font.cache_clear()  # noqa: SLF001
    renderer_module._name_font.cache_clear()  # noqa: SLF001
    renderer_module._emoji_font.cache_clear()  # noqa: SLF001
    monkeypatch.setattr(ImageFont, "truetype", track_font)
    name = "🎨🌙 Demo ૮( •ᴗ• )ა"
    runs = renderer_module._name_runs(name)  # noqa: SLF001
    assert [(run.text, run.font_path, run.embedded_color) for run in runs] == [
        ("🎨🌙", renderer_module.EMOJI_FONT_PATH, True),
        (" Demo ", renderer_module.FONT_PATH, False),
        ("૮", renderer_module.UNIFONT_PATH, False),
        ("( •", renderer_module.FONT_PATH, False),
        ("ᴗ", renderer_module.NOTO_SANS_PATH, False),
        ("• )", renderer_module.FONT_PATH, False),
        ("ა", renderer_module.UNIFONT_PATH, False),
    ]
    assert renderer_module._name_runs("⏻")[0].font_path == (  # noqa: SLF001
        renderer_module.SYMBOLS_FONT_PATH
    )
    unsupported = renderer_module._name_runs("\U0010ffff")  # noqa: SLF001
    assert [(run.text, run.font_path) for run in unsupported] == [
        ("□", renderer_module.FONT_PATH)
    ]
    value = renderer_module.ShiftNoticeRenderInput(
        ShiftNoticeCaseKind.START,
        None,
        _frame(
            renderer_module,
            "14–15",
            (None, name, None, None, None),
            (None, "1h", None, None, None),
        ),
        None,
    )

    data = renderer_module.render_shift_notice(value)
    assert _open_rendered(data).width == 1972
    assert renderer_module.FONT_PATH == ASSET_DIR / "NotoSansCJKjp-VF.otf"
    assert loaded_font_paths
    assert set(loaded_font_paths) <= set(renderer_module.NAME_FONT_PATHS)
    assert {
        renderer_module.FONT_PATH,
        renderer_module.NOTO_SANS_PATH,
        renderer_module.EMOJI_FONT_PATH,
        renderer_module.UNIFONT_PATH,
    } <= set(loaded_font_paths)


def test_name_chip_draws_embedded_emoji_color(renderer_module: ModuleType) -> None:
    renderer = renderer_module._Renderer()  # noqa: SLF001
    canvas = renderer_module.Canvas(Image.new("RGBA", (640, 240), "white"))

    renderer.draw_name_chip(canvas, (0, 0, 160, 60), "🌙")

    assert any(
        red > 180 and green > 120 and blue < 100 and alpha > 0
        for red, green, blue, alpha in canvas.image.get_flattened_data()
    )


def test_name_runs_respect_unicode_text_and_emoji_presentation(
    renderer_module: ModuleType,
) -> None:
    for text in ("©︎", "☀︎", "❤︎"):
        runs = renderer_module._name_runs(text)  # noqa: SLF001
        assert "".join(run.text for run in runs) == text
        assert not any(run.embedded_color for run in runs)

    for emoji_text in ("©", "☀", "❤", "©️", "☀️", "❤️"):
        runs = renderer_module._name_runs(emoji_text)  # noqa: SLF001
        assert "".join(run.text for run in runs) == emoji_text
        assert all(run.embedded_color for run in runs)


def test_renderer_rejects_invalid_presentation_shapes(
    renderer_module: ModuleType,
) -> None:
    with pytest.raises(ValueError, match="five lanes"):
        renderer_module.ShiftNoticeRenderFrame("14–15", ("name",), ("1h",))
    assert issubclass(renderer_module.ShiftNoticeRenderError, RuntimeError)
