"""Render deterministic v12 Shift Notice PNG cards."""

# ruff: noqa: RUF001

from __future__ import annotations

import io
import math
import unicodedata
from dataclasses import dataclass, field
from enum import StrEnum
from functools import cache, lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

import emoji
from fontTools.ttLib import TTFont
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from utils.shift_notice import ShiftNoticeCaseKind, ShiftNoticeCutWindow

if TYPE_CHECKING:
    from collections.abc import Sequence

LANE_LABELS = ("アンコ", "本走", "本走", "本走", "待機")
_LANE_COUNT = len(LANE_LABELS)
_OUTPUT_SCALE = 2
_ANTIALIAS = 2
_WORK_SCALE = _OUTPUT_SCALE * _ANTIALIAS
_EMOJI_STRIKE_SIZE = 109
_VARIATION_SELECTOR_RANGES = ((0xFE00, 0xFE0F), (0xE0100, 0xE01EF))
_TEXT_VARIATION_SELECTOR = "\ufe0e"

ASSET_DIR = Path(__file__).parents[1] / "resources/assets/shift_notice"
FONT_PATH = ASSET_DIR / "NotoSansCJKjp-VF.otf"
NOTO_SANS_PATH = ASSET_DIR / "NotoSans-VF.ttf"
SYMBOLS_FONT_PATH = ASSET_DIR / "NotoSansSymbols2-Regular.ttf"
UNIFONT_PATH = ASSET_DIR / "unifont.otf"
EMOJI_FONT_PATH = ASSET_DIR / "NotoColorEmoji.ttf"
TEXT_FONT_PATHS = (FONT_PATH, NOTO_SANS_PATH, SYMBOLS_FONT_PATH, UNIFONT_PATH)
NAME_FONT_PATHS = (*TEXT_FONT_PATHS, EMOJI_FONT_PATH)
_TWEMOJI_DIR = ASSET_DIR / "twemoji"


class ShiftNoticeRenderError(RuntimeError):
    """Raised when a valid Shift Notice input cannot be rendered."""


@dataclass(frozen=True)
class _NameRun:
    text: str
    font_path: Path
    embedded_color: bool = False


@dataclass(frozen=True)
class ShiftNoticeRenderFrame:
    """One already-presented time slot with five display lanes."""

    range_label: str
    names: tuple[str | None, ...]
    hours: tuple[str | None, ...]

    def __post_init__(self) -> None:
        if len(self.names) != _LANE_COUNT or len(self.hours) != _LANE_COUNT:
            msg = "Shift Notice render frames require exactly five lanes."
            raise ValueError(msg)
        if not self.range_label:
            msg = "Shift Notice render frames require a range label."
            raise ValueError(msg)
        for name, hours in zip(self.names, self.hours, strict=True):
            if name is not None and not isinstance(name, str):
                msg = "Shift Notice display names must be text or None."
                raise TypeError(msg)
            if hours is not None and not isinstance(hours, str):
                msg = "Shift Notice display hours must be text or None."
                raise TypeError(msg)
            if name is None and hours is not None:
                msg = "Shift Notice display hours require a display name."
                raise ValueError(msg)


@dataclass(frozen=True)
class ShiftNoticeRenderInput:
    """Presentation-only input for one Shift Notice image."""

    case: ShiftNoticeCaseKind
    previous: ShiftNoticeRenderFrame | None
    next: ShiftNoticeRenderFrame | None
    cut_window: ShiftNoticeCutWindow | None

    def __post_init__(self) -> None:
        valid = {
            ShiftNoticeCaseKind.START: self.previous is None and self.next is not None,
            ShiftNoticeCaseKind.TRANSITION: (
                self.previous is not None and self.next is not None
            ),
            ShiftNoticeCaseKind.END: self.previous is not None and self.next is None,
            ShiftNoticeCaseKind.CUT: self.cut_window is not None,
        }
        if not valid[self.case]:
            msg = f"Invalid {self.case.value} Shift Notice render input."
            raise ValueError(msg)
        if self.case is not ShiftNoticeCaseKind.CUT and self.cut_window is not None:
            msg = "Only CUT render inputs may contain a cut window."
            raise ValueError(msg)


class Status(StrEnum):
    """Per-lane v12 handoff status."""

    NONE = "none"
    START = "start"
    END = "end"
    CONTINUE = "continue"
    SWAP = "swap"
    MOVE_RIGHT = "move_right"
    MOVE_LEFT = "move_left"


@dataclass(frozen=True)
class StatusPlacement:
    """One lane status with movement endpoint metadata."""

    status: Status
    movement_endpoint: str | None = None


@dataclass(frozen=True)
class StatusPresentation:
    """Japanese status copy and its pinned Twemoji asset."""

    png_filename: str
    left_text: str
    right_text: str


STATUS_PRESENTATION = {
    Status.START: StatusPresentation("2b07.png", "開", "始"),
    Status.END: StatusPresentation("23f9.png", "終", "了"),
    Status.CONTINUE: StatusPresentation("23ec.png", "継", "続"),
    Status.SWAP: StatusPresentation("1f503.png", "交", "代"),
    Status.MOVE_RIGHT: StatusPresentation("2198.png", "継続", ""),
    Status.MOVE_LEFT: StatusPresentation("2199.png", "継続", ""),
}


def _cut_banner_label(window: ShiftNoticeCutWindow) -> str:
    """Return the visible CUT range with edge truncation markers."""

    if not window.rows:
        msg = "CUT banner labels require at least one row."
        raise ShiftNoticeRenderError(msg)
    start = window.rows[0].event_hour
    end = window.rows[-1].event_hour + 1
    before = "…" if window.truncated_before else ""
    after = "…" if window.truncated_after else ""
    return f"{before}{start}–{end}{after}"


def derive_statuses(  # noqa: C901, PLR0912
    previous: ShiftNoticeRenderFrame | None,
    next_frame: ShiftNoticeRenderFrame | None,
) -> tuple[Status, ...]:
    """Derive the v12 status shown in each role lane."""

    if previous is None:
        if next_frame is None:
            return (Status.NONE,) * _LANE_COUNT
        return tuple(Status.START if name else Status.NONE for name in next_frame.names)
    if next_frame is None:
        return tuple(Status.END if name else Status.NONE for name in previous.names)

    statuses = [Status.NONE] * _LANE_COUNT
    previous_positions = {
        name: index for index, name in enumerate(previous.names) if name
    }
    next_positions = {
        name: index for index, name in enumerate(next_frame.names) if name
    }

    for index, (old, new) in enumerate(
        zip(previous.names, next_frame.names, strict=True)
    ):
        if old and new:
            statuses[index] = Status.CONTINUE if old == new else Status.SWAP

    for index, name in enumerate(next_frame.names):
        if name and name not in previous_positions and statuses[index] is Status.NONE:
            statuses[index] = Status.START
    for index, name in enumerate(previous.names):
        if name and name not in next_positions and statuses[index] is Status.NONE:
            statuses[index] = Status.END

    for name, source in previous_positions.items():
        destination = next_positions.get(name)
        if destination is None or destination == source:
            continue
        movement = Status.MOVE_RIGHT if destination > source else Status.MOVE_LEFT
        source_free = statuses[source] is Status.NONE
        destination_free = statuses[destination] is Status.NONE
        if destination_free:
            statuses[destination] = movement
        if source_free:
            statuses[source] = movement

    for index in range(_LANE_COUNT):
        if statuses[index] is not Status.NONE:
            continue
        if previous.names[index] and previous.names[index] not in next_positions:
            statuses[index] = Status.END
        elif next_frame.names[index]:
            statuses[index] = Status.START
    return tuple(statuses)


def derive_status_placements(
    previous: ShiftNoticeRenderFrame | None,
    next_frame: ShiftNoticeRenderFrame | None,
) -> tuple[StatusPlacement, ...]:
    """Retain movement endpoints alongside the existing status classification."""

    statuses = derive_statuses(previous, next_frame)
    endpoints: list[str | None] = [None] * _LANE_COUNT
    if previous is not None and next_frame is not None:
        previous_positions = {
            name: index for index, name in enumerate(previous.names) if name
        }
        next_positions = {
            name: index for index, name in enumerate(next_frame.names) if name
        }
        for name, source in previous_positions.items():
            destination = next_positions.get(name)
            if destination is None or destination == source:
                continue
            movement = Status.MOVE_RIGHT if destination > source else Status.MOVE_LEFT
            if statuses[destination] is movement:
                endpoints[destination] = "destination"
            if statuses[source] is movement:
                endpoints[source] = "source"
    return tuple(
        StatusPlacement(status, endpoints[index])
        for index, status in enumerate(statuses)
    )


def status_copy(
    status: Status,
    movement_endpoint: str | None = None,
) -> tuple[str, str]:
    """Return status text placement around the pinned icon."""

    presentation = STATUS_PRESENTATION[status]
    if status is Status.MOVE_RIGHT:
        if movement_endpoint == "source":
            return "継続", ""
        if movement_endpoint == "destination":
            return "", "継続"
    if status is Status.MOVE_LEFT:
        if movement_endpoint == "source":
            return "", "継続"
        if movement_endpoint == "destination":
            return "継続", ""
    return presentation.left_text, presentation.right_text


def inactive_lanes(value: ShiftNoticeRenderInput) -> tuple[bool, ...]:
    """Return lanes dimmed by the v12 case-specific rule."""

    if value.case is ShiftNoticeCaseKind.CUT:
        return (False,) * _LANE_COUNT
    if value.case is ShiftNoticeCaseKind.START:
        next_frame = _required_frame(value.next, value.case)
        return tuple(name is None for name in next_frame.names)
    if value.case is ShiftNoticeCaseKind.END:
        previous = _required_frame(value.previous, value.case)
        return tuple(name is None for name in previous.names)
    previous = _required_frame(value.previous, value.case)
    next_frame = _required_frame(value.next, value.case)
    return tuple(
        old is None and new is None
        for old, new in zip(previous.names, next_frame.names, strict=True)
    )


def _required_frame(
    frame: ShiftNoticeRenderFrame | None,
    case: ShiftNoticeCaseKind,
) -> ShiftNoticeRenderFrame:
    if frame is None:
        msg = f"Missing frame for {case.value} Shift Notice rendering."
        raise ShiftNoticeRenderError(msg)
    return frame


@dataclass(frozen=True)
class Theme:
    """Approved v12 color tokens."""

    page_bg: str = "#eef1f5"
    surface: str = "#fffefe"
    text: str = "#111827"
    vertical_line: str = "#dce1e7"
    label_divider: str = "#b9c2cc"
    waiting_divider: str = "#aab4bf"
    label_line: str = "#d4dae1"
    label_inner_line: str = "#e0e4e9"
    outer_line: str = "#c4ccd5"
    row_label_bg: str = "#fafbfd"
    lane_header_top: str = "#ffffff"
    lane_header_bottom: str = "#fbfcfd"
    lane_header_underline: str = "#d9dee5"
    anko: str = "#e60012"
    anko_underline: str = "#f2a3aa"
    start_bg_a: str = "#edf5fc"
    start_bg_b: str = "#f7fafe"
    start_text: str = "#315d7e"
    end_bg_a: str = "#fff0f2"
    end_bg_b: str = "#fff7f8"
    end_text: str = "#a4424c"
    cut_bg_a: str = "#f1f3f5"
    cut_bg_b: str = "#f7f8fa"
    cut_text: str = "#5d6672"
    cut_sep_text: str = "#8a939d"
    cut_past_bg: str = "#e7eaee"
    cut_past_text: str = "#929aa4"
    cut_future_bg: str = "#f7f8fa"
    cut_future_text: str = "#5d6672"
    cut_current_bg: str = "#eaf3fb"
    cut_current_text: str = "#315d7e"
    cut_current_border: str = "#79a7c7"
    cut_track: str = "#c8d0d8"
    cut_track_current: str = "#79a7c7"
    name_bg: str = "#ffffff"
    name_border: str = "#cfd6de"
    name_shadow: str = "#1e293b"
    inactive_bg: str = "#eef1f4"
    inactive_text: str = "#929aa4"
    inactive_head_text: str = "#7f8791"
    inactive_underline: str = "#cbd1d7"
    hours_text: str = "#303740"
    pair_label_text: str = "#3f4955"
    time_anchor_text: str = "#25303b"


@dataclass(frozen=True)
class Layout:
    """Approved v12 logical geometry."""

    label_width: int = 208
    time_anchor_width: int = 96
    lane_width: int = 146
    row_height: int = 44
    lane_count: int = _LANE_COUNT
    card_radius: int = 13
    card_border_width: int = 1
    card_shadow_y: int = 8
    card_shadow_blur: int = 12
    banner_size: int = 18
    time_anchor_size: int = 18
    pair_label_size: int = 15
    lane_head_size: int = 17
    status_size: int = 17
    status_diagonal_size: int = 16
    name_size: int = 17
    name_mid_size: int = 15
    name_min_size: int = 13
    hours_size: int = 17
    name_chip_height: int = 31
    name_chip_min_width: int = 70
    name_chip_radius: int = 9
    name_chip_pad_x: int = 15
    name_chip_mid_pad_x: int = 6
    name_chip_min_pad_x: int = 4
    status_icon_size: int = 16
    status_icon_gap: int = 1
    single_margin: int = 24
    cut_track_x: int = 48
    cut_current_row_border: int = 2
    cut_focus_inset_x: int = 8
    cut_focus_inset_y: int = 4
    cut_focus_radius: int = 9

    @property
    def card_width(self) -> int:
        return self.label_width + self.lane_width * self.lane_count


def _hex_rgb(value: str) -> tuple[int, int, int]:
    raw = value.removeprefix("#")
    if len(raw) != 6:  # noqa: PLR2004
        msg = f"Expected #RRGGBB, got {value!r}."
        raise ValueError(msg)
    return tuple(int(raw[index : index + 2], 16) for index in (0, 2, 4))  # type: ignore[return-value]


def _rgba(value: str, alpha: int = 255) -> tuple[int, int, int, int]:
    return (*_hex_rgb(value), alpha)


def _lerp_color(
    first: str,
    second: str,
    amount: float,
) -> tuple[int, int, int, int]:
    start = _hex_rgb(first)
    end = _hex_rgb(second)
    return (
        round(start[0] + (end[0] - start[0]) * amount),
        round(start[1] + (end[1] - start[1]) * amount),
        round(start[2] + (end[2] - start[2]) * amount),
        255,
    )


@cache
def _font(logical_size: int, *, bold: bool) -> ImageFont.FreeTypeFont:
    font = ImageFont.truetype(FONT_PATH, size=logical_size * _WORK_SCALE)
    font.set_variation_by_name("Bold" if bold else "Regular")
    return font


@cache
def _font_cmap(font_path: Path) -> frozenset[int]:
    font = TTFont(font_path, lazy=True)
    try:
        return frozenset(font.getBestCmap() or {})
    finally:
        font.close()


@cache
def _name_font(font_path: Path, logical_size: int) -> ImageFont.FreeTypeFont:
    font = ImageFont.truetype(font_path, size=logical_size * _WORK_SCALE)
    if font_path in {FONT_PATH, NOTO_SANS_PATH}:
        font.set_variation_by_name("Bold")
    return font


@cache
def _emoji_font() -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(EMOJI_FONT_PATH, size=_EMOJI_STRIKE_SIZE)


def _is_variation_selector(character: str) -> bool:
    codepoint = ord(character)
    return any(start <= codepoint <= end for start, end in _VARIATION_SELECTOR_RANGES)


def _plain_name_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    for character in text:
        if tokens and (
            unicodedata.combining(character)
            or _is_variation_selector(character)
            or character == "\u200d"
        ):
            tokens[-1] += character
        else:
            tokens.append(character)
    return tokens


def _name_tokens(name: str) -> tuple[tuple[str, bool], ...]:
    tokens: list[tuple[str, bool]] = []
    cursor = 0
    for match in emoji.emoji_list(name):
        start = match["match_start"]
        end = match["match_end"]
        tokens.extend(
            (token, False) for token in _plain_name_tokens(name[cursor:start])
        )
        matched = name[start:end]
        if end < len(name) and name[end] == _TEXT_VARIATION_SELECTOR:
            matched += _TEXT_VARIATION_SELECTOR
            end += 1
            is_emoji = False
        else:
            is_emoji = True
        if is_emoji:
            tokens.append((matched, True))
        else:
            tokens.extend((token, False) for token in _plain_name_tokens(matched))
        cursor = end
    tokens.extend((token, False) for token in _plain_name_tokens(name[cursor:]))
    return tuple(tokens)


def _font_supports(font_path: Path, text: str) -> bool:
    required = {
        ord(character)
        for character in text
        if character != "\u200d" and not _is_variation_selector(character)
    }
    return bool(required) and required <= _font_cmap(font_path)


@lru_cache(maxsize=512)
def _name_runs(name: str) -> tuple[_NameRun, ...]:
    runs: list[_NameRun] = []
    for text, is_emoji in _name_tokens(name):
        if is_emoji and _font_supports(EMOJI_FONT_PATH, text):
            run = _NameRun(text, EMOJI_FONT_PATH, embedded_color=True)
        elif not is_emoji:
            font_path = next(
                (path for path in TEXT_FONT_PATHS if _font_supports(path, text)),
                None,
            )
            run = _NameRun(text, font_path) if font_path else _NameRun("□", FONT_PATH)
        else:
            run = _NameRun("□", FONT_PATH)
        if (
            runs
            and runs[-1].font_path == run.font_path
            and runs[-1].embedded_color == run.embedded_color
        ):
            previous = runs[-1]
            runs[-1] = _NameRun(
                previous.text + run.text,
                run.font_path,
                run.embedded_color,
            )
        else:
            runs.append(run)
    return tuple(runs)


def _name_run_bounds(
    draw: ImageDraw.ImageDraw,
    name: str,
    logical_size: int,
) -> tuple[float, float, float, float]:
    cursor = 0.0
    left = top = math.inf
    right = bottom = -math.inf
    for run in _name_runs(name):
        bounds, advance = _name_run_metrics(draw, run, logical_size)
        left = min(left, cursor + bounds[0])
        top = min(top, bounds[1])
        right = max(right, cursor + bounds[2])
        bottom = max(bottom, bounds[3])
        cursor += advance
    return (0, 0, 0, 0) if left == math.inf else (left, top, right, bottom)


def _name_run_metrics(
    draw: ImageDraw.ImageDraw,
    run: _NameRun,
    logical_size: int,
) -> tuple[tuple[float, float, float, float], float]:
    if run.embedded_color:
        font = _emoji_font()
        scale = logical_size * _WORK_SCALE / _EMOJI_STRIKE_SIZE
        bounds = draw.textbbox(
            (0, 0),
            run.text,
            font=font,
            anchor="ls",
            embedded_color=True,
        )
        return (
            tuple(value * scale for value in bounds),
            draw.textlength(run.text, font=font, embedded_color=True) * scale,
        )
    font = _name_font(run.font_path, logical_size)
    return (
        draw.textbbox((0, 0), run.text, font=font, anchor="ls"),
        draw.textlength(run.text, font=font),
    )


@lru_cache(maxsize=512)
def _emoji_run_image(text: str, logical_size: int) -> Image.Image:
    font = _emoji_font()
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    left, top, right, bottom = probe.textbbox(
        (0, 0),
        text,
        font=font,
        anchor="ls",
        embedded_color=True,
    )
    image = Image.new("RGBA", (max(1, right - left), max(1, bottom - top)))
    ImageDraw.Draw(image).text(
        (-left, -top),
        text,
        font=font,
        anchor="ls",
        embedded_color=True,
    )
    scale = logical_size * _WORK_SCALE / _EMOJI_STRIKE_SIZE
    return image.resize(
        (
            max(1, round(image.width * scale)),
            max(1, round(image.height * scale)),
        ),
        Image.Resampling.LANCZOS,
    )


def _name_width(
    draw: ImageDraw.ImageDraw,
    name: str,
    logical_size: int,
) -> float:
    left, _top, right, _bottom = _name_run_bounds(draw, name, logical_size)
    return (right - left) / _WORK_SCALE


@cache
def _icon(filename: str, pixel_size: int) -> Image.Image:
    with Image.open(_TWEMOJI_DIR / filename) as source:
        icon = source.convert("RGBA")
    if icon.size != (pixel_size, pixel_size):
        icon = icon.resize((pixel_size, pixel_size), Image.Resampling.LANCZOS)
    return icon


class Canvas:
    """Draw in logical v12 coordinates on the antialiased image."""

    def __init__(self, image: Image.Image) -> None:
        self.image = image
        self.draw = ImageDraw.Draw(image, "RGBA")

    @staticmethod
    def p(value: float) -> int:
        return round(value * _WORK_SCALE)

    @classmethod
    def box(cls, rect: tuple[float, float, float, float]) -> tuple[int, ...]:
        return tuple(cls.p(value) for value in rect)

    def line(
        self,
        xy: Sequence[tuple[float, float]],
        fill: str,
        width: float = 1,
    ) -> None:
        self.draw.line(
            [(self.p(x), self.p(y)) for x, y in xy],
            fill=fill,
            width=max(1, self.p(width)),
        )

    def rectangle(
        self,
        rect: tuple[float, float, float, float],
        *,
        fill: str,
    ) -> None:
        self.draw.rectangle(self.box(rect), fill=fill)

    def rounded_rectangle(
        self,
        rect: tuple[float, float, float, float],
        *,
        radius: float,
        fill: str | None = None,
        outline: str | None = None,
        width: float = 1,
    ) -> None:
        self.draw.rounded_rectangle(
            self.box(rect),
            radius=self.p(radius),
            fill=fill,
            outline=outline,
            width=max(1, self.p(width)),
        )


@dataclass
class _Renderer:
    theme: Theme = field(default_factory=Theme)
    layout: Layout = field(default_factory=Layout)

    @staticmethod
    def font(logical_size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont:
        return _font(logical_size, bold=bold)

    @staticmethod
    def _text_bbox(
        draw: ImageDraw.ImageDraw,
        text: str,
        font: ImageFont.FreeTypeFont,
    ) -> tuple[int, int, int, int]:
        return draw.textbbox((0, 0), text, font=font, stroke_width=0)

    def measure_text(
        self,
        draw: ImageDraw.ImageDraw,
        text: str,
        font: ImageFont.FreeTypeFont,
    ) -> tuple[float, float]:
        bounds = self._text_bbox(draw, text, font)
        return (
            (bounds[2] - bounds[0]) / _WORK_SCALE,
            (bounds[3] - bounds[1]) / _WORK_SCALE,
        )

    def draw_centered_text(
        self,
        canvas: Canvas,
        rect: tuple[float, float, float, float],
        text: str,
        font: ImageFont.FreeTypeFont,
        fill: str,
    ) -> None:
        x0, y0, x1, y1 = rect
        bounds = canvas.draw.textbbox((0, 0), text, font=font)
        width = bounds[2] - bounds[0]
        height = bounds[3] - bounds[1]
        x = Canvas.p((x0 + x1) / 2) - width / 2 - bounds[0]
        y = Canvas.p((y0 + y1) / 2) - height / 2 - bounds[1]
        canvas.draw.text((round(x), round(y)), text, font=font, fill=fill)

    def draw_left_centered_text(  # noqa: PLR0913
        self,
        canvas: Canvas,
        x: float,
        y_bounds: tuple[float, float],
        text: str,
        font: ImageFont.FreeTypeFont,
        fill: str,
    ) -> None:
        y0, y1 = y_bounds
        bounds = canvas.draw.textbbox((0, 0), text, font=font)
        height = bounds[3] - bounds[1]
        y = Canvas.p((y0 + y1) / 2) - height / 2 - bounds[1]
        canvas.draw.text((Canvas.p(x), round(y)), text, font=font, fill=fill)

    @staticmethod
    def gradient_image(
        width: int,
        height: int,
        first: str,
        second: str,
        *,
        horizontal: bool,
    ) -> Image.Image:
        work_width = max(1, width * _WORK_SCALE)
        work_height = max(1, height * _WORK_SCALE)
        if horizontal:
            strip = Image.new("RGBA", (work_width, 1))
            pixels = strip.load()
            denominator = max(1, work_width - 1)
            for x in range(work_width):
                pixels[x, 0] = _lerp_color(first, second, x / denominator)
            return strip.resize((work_width, work_height), Image.Resampling.BILINEAR)
        strip = Image.new("RGBA", (1, work_height))
        pixels = strip.load()
        denominator = max(1, work_height - 1)
        for y in range(work_height):
            pixels[0, y] = _lerp_color(first, second, y / denominator)
        return strip.resize((work_width, work_height), Image.Resampling.BILINEAR)

    def paste_gradient(
        self,
        target: Image.Image,
        rect: tuple[float, float, float, float],
        first: str,
        second: str,
        *,
        horizontal: bool,
    ) -> None:
        x0, y0, x1, y1 = rect
        gradient = self.gradient_image(
            round(x1 - x0),
            round(y1 - y0),
            first,
            second,
            horizontal=horizontal,
        )
        target.alpha_composite(
            gradient,
            dest=(Canvas.p(x0), Canvas.p(y0)),
        )

    def draw_icon(
        self,
        canvas: Canvas,
        status: Status,
        x: float,
        y: float,
        size: float,
    ) -> None:
        presentation = STATUS_PRESENTATION[status]
        pixel_size = max(1, Canvas.p(size))
        canvas.image.alpha_composite(
            _icon(presentation.png_filename, pixel_size),
            dest=(Canvas.p(x), Canvas.p(y)),
        )

    def draw_status(
        self,
        canvas: Canvas,
        rect: tuple[float, float, float, float],
        status: Status,
        color: str,
        movement_endpoint: str | None = None,
    ) -> None:
        if status is Status.NONE:
            return
        size = (
            self.layout.status_diagonal_size
            if status in {Status.MOVE_LEFT, Status.MOVE_RIGHT}
            else self.layout.status_size
        )
        font = self.font(size, bold=True)
        icon_size = self.layout.status_icon_size
        gap = self.layout.status_icon_gap
        left, right = status_copy(status, movement_endpoint)
        left_width = self.measure_text(canvas.draw, left, font)[0] if left else 0
        right_width = self.measure_text(canvas.draw, right, font)[0] if right else 0
        total = left_width + right_width + icon_size
        if left:
            total += gap
        if right:
            total += gap
        x0, y0, x1, y1 = rect
        x = (x0 + x1 - total) / 2
        if left:
            self.draw_left_centered_text(canvas, x, (y0, y1), left, font, color)
            x += left_width + gap
        self.draw_icon(
            canvas,
            status,
            x,
            (y0 + y1 - icon_size) / 2,
            icon_size,
        )
        if right:
            x += icon_size + gap
            self.draw_left_centered_text(canvas, x, (y0, y1), right, font, color)

    def draw_banner_text(
        self,
        canvas: Canvas,
        rect: tuple[float, float, float, float],
        hour: str,
        *,
        start: bool,
    ) -> None:
        font = self.font(self.layout.banner_size, bold=True)
        status = Status.START if start else Status.END
        color = self.theme.start_text if start else self.theme.end_text
        separator_color = "#7891a4" if start else "#bd737c"
        icon_size = self.layout.status_icon_size
        gap = 2
        chunks = (
            hour,
            "｜",
            "シフト開" if start else "シフト終",
            "始" if start else "了",
        )
        widths = [self.measure_text(canvas.draw, chunk, font)[0] for chunk in chunks]
        total = sum(widths) + icon_size + gap * 4
        x0, y0, x1, y1 = rect
        x = (x0 + x1 - total) / 2
        self.draw_left_centered_text(canvas, x, (y0, y1), chunks[0], font, color)
        x += widths[0] + gap
        self.draw_left_centered_text(
            canvas,
            x,
            (y0, y1),
            chunks[1],
            font,
            separator_color,
        )
        x += widths[1] + gap
        self.draw_left_centered_text(canvas, x, (y0, y1), chunks[2], font, color)
        x += widths[2] + gap
        self.draw_icon(canvas, status, x, (y0 + y1 - icon_size) / 2, icon_size)
        x += icon_size + gap
        self.draw_left_centered_text(canvas, x, (y0, y1), chunks[3], font, color)

    def draw_cut_banner_text(
        self,
        canvas: Canvas,
        rect: tuple[float, float, float, float],
        range_label: str,
    ) -> float:
        """Draw the CUT title and return its measured separator axis."""

        font = self.font(self.layout.banner_size, bold=True)
        chunks = (range_label, "｜", "シフトカット")
        widths = [self.measure_text(canvas.draw, chunk, font)[0] for chunk in chunks]
        gap = 2
        _x0, y0, _x1, y1 = rect
        separator_x = self.cut_separator_axis(canvas.draw, rect, range_label)
        x = separator_x - gap - widths[0]
        self.draw_left_centered_text(
            canvas,
            x,
            (y0, y1),
            chunks[0],
            font,
            self.theme.cut_text,
        )
        self.draw_left_centered_text(
            canvas,
            separator_x,
            (y0, y1),
            chunks[1],
            font,
            self.theme.cut_sep_text,
        )
        self.draw_left_centered_text(
            canvas,
            separator_x + widths[1] + gap,
            (y0, y1),
            chunks[2],
            font,
            self.theme.cut_text,
        )
        return separator_x

    def cut_separator_axis(
        self,
        draw: ImageDraw.ImageDraw,
        rect: tuple[float, float, float, float],
        range_label: str,
    ) -> float:
        """Return one separator axis that balances both visible CUT groups."""

        font = self.font(self.layout.banner_size, bold=True)
        separator_width = self.measure_text(draw, "｜", font)[0]
        right_width = self.measure_text(draw, "シフトカット", font)[0]
        title_width = self.measure_text(draw, range_label, font)[0]
        current_width = self.measure_text(draw, "この時間", font)[0]
        title_offset = (separator_width + right_width - title_width) / 2
        current_offset = (separator_width + right_width - current_width) / 2
        schedule_center = (rect[0] + rect[2]) / 2
        return schedule_center - (title_offset + current_offset) / 2

    def draw_cut_current_text(
        self,
        canvas: Canvas,
        rect: tuple[float, float, float, float],
        separator_x: float,
    ) -> None:
        """Draw the focused CUT row using the title's separator axis."""

        font = self.font(self.layout.banner_size, bold=True)
        left, separator, right = "この時間", "｜", "シフトカット"
        left_width = self.measure_text(canvas.draw, left, font)[0]
        separator_width = self.measure_text(canvas.draw, separator, font)[0]
        gap = 2
        y0, y1 = rect[1], rect[3]
        self.draw_left_centered_text(
            canvas,
            separator_x - gap - left_width,
            (y0, y1),
            left,
            font,
            self.theme.cut_current_text,
        )
        self.draw_left_centered_text(
            canvas,
            separator_x,
            (y0, y1),
            separator,
            font,
            self.theme.cut_current_text,
        )
        self.draw_left_centered_text(
            canvas,
            separator_x + separator_width + gap,
            (y0, y1),
            right,
            font,
            self.theme.cut_current_text,
        )

    def _truncate_name_to_width(
        self,
        draw: ImageDraw.ImageDraw,
        name: str,
        logical_size: int,
        max_text_width: float,
    ) -> str:
        if _name_width(draw, name, logical_size) <= max_text_width:
            return name
        ellipsis = "…"
        if _name_width(draw, ellipsis, logical_size) > max_text_width:
            return ellipsis
        tokens = [text for text, _is_emoji in _name_tokens(name)]
        low, high = 2, len(tokens)
        best = ellipsis
        while low <= high:
            keep = (low + high) // 2
            prefix = max(1, math.ceil(keep * 0.6))
            suffix = max(1, keep - prefix)
            candidate = (
                f"{''.join(tokens[:prefix])}{ellipsis}{''.join(tokens[-suffix:])}"
            )
            if _name_width(draw, candidate, logical_size) <= max_text_width:
                best = candidate
                low = keep + 1
            else:
                high = keep - 1
        return best

    def name_style(
        self,
        draw: ImageDraw.ImageDraw,
        name: str,
        max_width: float,
    ) -> tuple[int, float, float, str]:
        """Choose the first v12 size that fits, then middle-ellipsize."""

        candidates = (
            (self.layout.name_size, self.layout.name_chip_pad_x),
            (self.layout.name_mid_size, self.layout.name_chip_mid_pad_x),
            (self.layout.name_min_size, self.layout.name_chip_min_pad_x),
            (12, 3),
            (11, 2),
            (10, 2),
            (9, 1),
        )
        for size, padding in candidates:
            text_width = _name_width(draw, name, size)
            chip_width = max(
                self.layout.name_chip_min_width,
                text_width + padding * 2,
            )
            if chip_width <= max_width:
                return size, padding, chip_width, name

        size = 9
        padding = 1
        display = self._truncate_name_to_width(
            draw,
            name,
            size,
            max_width - padding * 2,
        )
        text_width = _name_width(draw, display, size)
        chip_width = min(
            max_width,
            max(self.layout.name_chip_min_width, text_width + padding * 2),
        )
        return size, padding, chip_width, display

    def draw_centered_name(
        self,
        canvas: Canvas,
        rect: tuple[float, float, float, float],
        name: str,
        logical_size: int,
        fill: str,
    ) -> None:
        left, top, right, bottom = _name_run_bounds(
            canvas.draw,
            name,
            logical_size,
        )
        x0, y0, x1, y1 = rect
        x = Canvas.p((x0 + x1) / 2) - (right - left) / 2 - left
        baseline = Canvas.p((y0 + y1) / 2) - (bottom - top) / 2 - top
        for run in _name_runs(name):
            bounds, advance = _name_run_metrics(
                canvas.draw,
                run,
                logical_size,
            )
            if run.embedded_color:
                canvas.image.alpha_composite(
                    _emoji_run_image(run.text, logical_size),
                    dest=(round(x + bounds[0]), round(baseline + bounds[1])),
                )
            else:
                canvas.draw.text(
                    (round(x), round(baseline)),
                    run.text,
                    font=_name_font(run.font_path, logical_size),
                    fill=fill,
                    anchor="ls",
                )
            x += advance

    def draw_name_chip(
        self,
        canvas: Canvas,
        rect: tuple[float, float, float, float],
        name: str | None,
    ) -> None:
        if not name:
            return
        x0, y0, x1, y1 = rect
        logical_size, _padding, chip_width, display = self.name_style(
            canvas.draw,
            name,
            x1 - x0 - 18,
        )
        chip_height = self.layout.name_chip_height
        chip_x = (x0 + x1 - chip_width) / 2
        chip_y = (y0 + y1 - chip_height) / 2
        margin = 6
        shadow = Image.new(
            "RGBA",
            (
                Canvas.p(chip_width + margin * 2),
                Canvas.p(chip_height + margin * 2),
            ),
            (0, 0, 0, 0),
        )
        shadow_draw = ImageDraw.Draw(shadow, "RGBA")
        shadow_draw.rounded_rectangle(
            (
                Canvas.p(margin),
                Canvas.p(margin + 2),
                Canvas.p(margin + chip_width),
                Canvas.p(margin + 2 + chip_height),
            ),
            radius=Canvas.p(self.layout.name_chip_radius),
            fill=_rgba(self.theme.name_shadow, 28),
        )
        shadow = shadow.filter(ImageFilter.GaussianBlur(2.3 * _WORK_SCALE))
        canvas.image.alpha_composite(
            shadow,
            dest=(Canvas.p(chip_x - margin), Canvas.p(chip_y - margin)),
        )
        chip_rect = (
            chip_x,
            chip_y,
            chip_x + chip_width,
            chip_y + chip_height,
        )
        canvas.rounded_rectangle(
            chip_rect,
            radius=self.layout.name_chip_radius,
            fill=self.theme.name_bg,
            outline=self.theme.name_border,
        )
        self.draw_centered_name(
            canvas,
            chip_rect,
            display,
            logical_size,
            self.theme.text,
        )

    def draw_label_blank(self, canvas: Canvas, row_y: float) -> None:
        layout = self.layout
        canvas.rectangle(
            (0, row_y, layout.label_width, row_y + layout.row_height),
            fill=self.theme.row_label_bg,
        )

    def draw_time_pair(
        self,
        canvas: Canvas,
        y: float,
        range_label: str,
        upper_label: str,
        lower_label: str,
    ) -> None:
        layout = self.layout
        height = layout.row_height * 2
        canvas.rectangle(
            (0, y, layout.label_width, y + height),
            fill=self.theme.row_label_bg,
        )
        canvas.line(
            [
                (layout.time_anchor_width, y),
                (layout.time_anchor_width, y + height),
            ],
            fill=self.theme.label_inner_line,
        )
        canvas.line(
            [
                (layout.time_anchor_width, y + layout.row_height),
                (layout.label_width, y + layout.row_height),
            ],
            fill=self.theme.label_line,
        )
        self.draw_centered_text(
            canvas,
            (0, y, layout.time_anchor_width, y + height),
            range_label,
            self.font(layout.time_anchor_size, bold=True),
            self.theme.time_anchor_text,
        )
        label_x = layout.time_anchor_width + 13
        label_font = self.font(layout.pair_label_size, bold=True)
        self.draw_left_centered_text(
            canvas,
            label_x,
            (y, y + layout.row_height),
            upper_label,
            label_font,
            self.theme.pair_label_text,
        )
        self.draw_left_centered_text(
            canvas,
            label_x,
            (y + layout.row_height, y + height),
            lower_label,
            label_font,
            self.theme.pair_label_text,
        )

    def lane_rect(
        self,
        lane: int,
        y: float,
        height: float,
    ) -> tuple[float, float, float, float]:
        x0 = self.layout.label_width + lane * self.layout.lane_width
        return (x0, y, x0 + self.layout.lane_width, y + height)

    def draw_lane_header(
        self,
        card: Image.Image,
        canvas: Canvas,
        lane: int,
        y: float,
        *,
        inactive: bool,
    ) -> None:
        layout = self.layout
        rect = self.lane_rect(lane, y, layout.row_height)
        if not inactive:
            self.paste_gradient(
                card,
                rect,
                self.theme.lane_header_top,
                self.theme.lane_header_bottom,
                horizontal=False,
            )
        color = (
            self.theme.inactive_head_text
            if inactive
            else self.theme.anko
            if lane == 0
            else self.theme.text
        )
        self.draw_centered_text(
            canvas,
            rect,
            LANE_LABELS[lane],
            self.font(layout.lane_head_size, bold=True),
            color,
        )
        x0, _y0, x1, y1 = rect
        line_color = (
            self.theme.inactive_underline
            if inactive
            else self.theme.anko_underline
            if lane == 0
            else self.theme.lane_header_underline
        )
        canvas.rounded_rectangle(
            (
                x0 + layout.lane_width * 0.34,
                y1 - 8,
                x1 - layout.lane_width * 0.34,
                y1 - 6,
            ),
            radius=1,
            fill=line_color,
        )

    def draw_lane_dividers(self, canvas: Canvas, y0: float, y1: float) -> None:
        layout = self.layout
        for boundary in range(layout.lane_count):
            x = layout.label_width + boundary * layout.lane_width
            if boundary == 0:
                color, width = self.theme.label_divider, 2
            elif boundary == layout.lane_count - 1:
                color, width = self.theme.waiting_divider, 2
            else:
                color, width = self.theme.vertical_line, 1
            canvas.line([(x, y0), (x, y1)], fill=color, width=width)

    def draw_banner(
        self,
        card: Image.Image,
        canvas: Canvas,
        y: float,
        hour: str,
        *,
        start: bool,
    ) -> None:
        first, second = (
            (self.theme.start_bg_a, self.theme.start_bg_b)
            if start
            else (self.theme.end_bg_a, self.theme.end_bg_b)
        )
        rect = (0, y, self.layout.card_width, y + self.layout.row_height)
        self.paste_gradient(card, rect, first, second, horizontal=True)
        self.draw_banner_text(canvas, rect, hour, start=start)

    def draw_frame_rows(
        self,
        canvas: Canvas,
        frame: ShiftNoticeRenderFrame,
        upper_y: float,
        *,
        names_on_lower_row: bool,
        inactive: tuple[bool, ...],
    ) -> None:
        for lane in range(self.layout.lane_count):
            upper = self.lane_rect(lane, upper_y, self.layout.row_height)
            lower = self.lane_rect(
                lane,
                upper_y + self.layout.row_height,
                self.layout.row_height,
            )
            name_rect = lower if names_on_lower_row else upper
            hours_rect = upper if names_on_lower_row else lower
            if frame.hours[lane]:
                self.draw_centered_text(
                    canvas,
                    hours_rect,
                    frame.hours[lane] or "",
                    self.font(self.layout.hours_size),
                    (
                        self.theme.inactive_text
                        if inactive[lane]
                        else self.theme.hours_text
                    ),
                )
            self.draw_name_chip(canvas, name_rect, frame.names[lane])

    def cut_main_rect(self, row_y: float) -> tuple[float, float, float, float]:
        return (
            self.layout.label_width,
            row_y,
            self.layout.card_width,
            row_y + self.layout.row_height,
        )

    def cut_focus_rect(self, row_y: float) -> tuple[float, float, float, float]:
        main = self.cut_main_rect(row_y)
        return (
            main[0] + self.layout.cut_focus_inset_x,
            main[1] + self.layout.cut_focus_inset_y,
            main[2] - self.layout.cut_focus_inset_x,
            main[3] - self.layout.cut_focus_inset_y,
        )

    def draw_cut_edge_markers(
        self,
        canvas: Canvas,
        rows_top: float,
        row_count: int,
        window: ShiftNoticeCutWindow,
    ) -> None:
        marker_font = self.font(self.layout.banner_size, bold=True)
        marker_height = 12
        if window.truncated_before:
            row_y = rows_top
            self.draw_centered_text(
                canvas,
                (
                    self.layout.label_width,
                    row_y,
                    self.layout.card_width,
                    row_y + marker_height,
                ),
                "…",
                marker_font,
                self.theme.cut_sep_text,
            )
        if window.truncated_after:
            row_y = rows_top + (row_count - 1) * self.layout.row_height
            self.draw_centered_text(
                canvas,
                (
                    self.layout.label_width,
                    row_y + self.layout.row_height - marker_height,
                    self.layout.card_width,
                    row_y + self.layout.row_height,
                ),
                "…",
                marker_font,
                self.theme.cut_sep_text,
            )

    def render_cut_card(self, value: ShiftNoticeRenderInput) -> Image.Image:
        layout = self.layout
        theme = self.theme
        window = value.cut_window
        if window is None:
            msg = "CUT render inputs require a cut window."
            raise ShiftNoticeRenderError(msg)
        slots = tuple(f"{row.event_hour}–{row.event_hour + 1}" for row in window.rows)
        if not slots:
            msg = "CUT render inputs require at least one row."
            raise ShiftNoticeRenderError(msg)
        current_label = (
            value.next.range_label
            if value.next is not None
            else value.previous.range_label
            if value.previous is not None
            else slots[len(slots) // 2]
        )
        if current_label not in slots:
            msg = "CUT current range is absent from its render window."
            raise ShiftNoticeRenderError(msg)
        current_index = slots.index(current_label)
        card_height = (1 + len(slots)) * layout.row_height
        card = Image.new(
            "RGBA",
            (Canvas.p(layout.card_width), Canvas.p(card_height)),
            _rgba(theme.surface),
        )
        canvas = Canvas(card)
        canvas.rectangle(
            (0, 0, layout.label_width, layout.row_height),
            fill=theme.cut_bg_a,
        )
        self.paste_gradient(
            card,
            self.cut_main_rect(0),
            theme.cut_bg_a,
            theme.cut_bg_b,
            horizontal=True,
        )
        banner_label = _cut_banner_label(window)
        separator_x = self.draw_cut_banner_text(
            canvas,
            self.cut_main_rect(0),
            banner_label,
        )

        rows_top = layout.row_height
        for index in range(len(slots)):
            y = rows_top + index * layout.row_height
            background = (
                theme.cut_current_bg
                if index == current_index
                else theme.cut_past_bg
                if index < current_index
                else theme.cut_future_bg
            )
            canvas.rectangle(
                (0, y, layout.card_width, y + layout.row_height),
                fill=background,
            )

        canvas.line(
            [(layout.label_width, 0), (layout.label_width, card_height)],
            fill=theme.label_divider,
            width=2,
        )
        canvas.line(
            [
                (layout.time_anchor_width, rows_top),
                (layout.time_anchor_width, card_height),
            ],
            fill=theme.label_inner_line,
        )
        canvas.line(
            [(0, rows_top), (layout.card_width, rows_top)],
            fill=theme.label_line,
        )
        for index in range(len(slots) - 1):
            y = rows_top + (index + 1) * layout.row_height
            canvas.line(
                [(0, y), (layout.card_width, y)],
                fill=theme.label_line,
            )

        current_y = rows_top + current_index * layout.row_height
        track_x = layout.label_width + layout.cut_track_x
        if len(slots) == 1:
            canvas.line(
                [(track_x, current_y + 10), (track_x, current_y + 34)],
                fill=theme.cut_track_current,
                width=3,
            )
        else:
            canvas.line(
                [
                    (track_x, rows_top + layout.row_height * 0.5),
                    (
                        track_x,
                        rows_top + layout.row_height * (len(slots) - 0.5),
                    ),
                ],
                fill=theme.cut_track,
            )
            canvas.line(
                [(track_x, current_y + 8), (track_x, current_y + 36)],
                fill=theme.cut_track_current,
                width=3,
            )
        self.draw_cut_edge_markers(canvas, rows_top, len(slots), window)
        canvas.rounded_rectangle(
            self.cut_focus_rect(current_y),
            radius=layout.cut_focus_radius,
            outline=theme.cut_current_border,
            width=layout.cut_current_row_border,
        )

        time_font = self.font(layout.time_anchor_size, bold=True)
        for index, range_label in enumerate(slots):
            y = rows_top + index * layout.row_height
            color = (
                theme.cut_current_text
                if index == current_index
                else theme.cut_past_text
                if index < current_index
                else theme.cut_future_text
            )
            self.draw_centered_text(
                canvas,
                (0, y, layout.time_anchor_width, y + layout.row_height),
                range_label,
                time_font,
                color,
            )
            if index == current_index:
                self.draw_cut_current_text(
                    canvas,
                    self.cut_main_rect(y),
                    separator_x,
                )
        return self._round_card(card)

    def render_card(  # noqa: C901, PLR0912, PLR0915
        self,
        value: ShiftNoticeRenderInput,
    ) -> Image.Image:
        if value.case is ShiftNoticeCaseKind.CUT:
            return self.render_cut_card(value)
        layout = self.layout
        theme = self.theme
        row_count = 6 if value.case is ShiftNoticeCaseKind.TRANSITION else 5
        card_height = row_count * layout.row_height
        card = Image.new(
            "RGBA",
            (Canvas.p(layout.card_width), Canvas.p(card_height)),
            _rgba(theme.surface),
        )
        canvas = Canvas(card)
        inactive = inactive_lanes(value)
        statuses = derive_status_placements(value.previous, value.next)
        previous = value.previous
        next_frame = value.next

        if value.case is ShiftNoticeCaseKind.START:
            next_frame = _required_frame(next_frame, value.case)
            header_y = layout.row_height
            status_y = layout.row_height * 2
            next_y = layout.row_height * 3
            content_y0 = header_y
            content_y1 = card_height
            self.draw_banner(
                card,
                canvas,
                0,
                f"{_range_start(next_frame.range_label)}時",
                start=True,
            )
        elif value.case is ShiftNoticeCaseKind.END:
            previous = _required_frame(previous, value.case)
            header_y = 0
            current_y = layout.row_height
            status_y = layout.row_height * 3
            banner_y = layout.row_height * 4
            content_y0 = 0
            content_y1 = banner_y
            self.draw_banner(
                card,
                canvas,
                banner_y,
                f"{_range_end(previous.range_label)}時",
                start=False,
            )
        else:
            previous = _required_frame(previous, value.case)
            next_frame = _required_frame(next_frame, value.case)
            header_y = 0
            current_y = layout.row_height
            status_y = layout.row_height * 3
            next_y = layout.row_height * 4
            content_y0 = 0
            content_y1 = card_height

        self.draw_label_blank(canvas, header_y)
        if value.case is ShiftNoticeCaseKind.START:
            self.draw_label_blank(canvas, status_y)
            self.draw_time_pair(
                canvas,
                next_y,
                next_frame.range_label,
                "次枠",
                "残り時間",
            )
        elif value.case is ShiftNoticeCaseKind.END:
            self.draw_time_pair(
                canvas,
                current_y,
                previous.range_label,
                "累計時間",
                "現在枠",
            )
            self.draw_label_blank(canvas, status_y)
        else:
            self.draw_time_pair(
                canvas,
                current_y,
                previous.range_label,
                "累計時間",
                "現在枠",
            )
            self.draw_label_blank(canvas, status_y)
            self.draw_time_pair(
                canvas,
                next_y,
                next_frame.range_label,
                "次枠",
                "残り時間",
            )

        for lane, is_inactive in enumerate(inactive):
            if is_inactive:
                canvas.rectangle(
                    self.lane_rect(lane, content_y0, content_y1 - content_y0),
                    fill=theme.inactive_bg,
                )
        for lane in range(layout.lane_count):
            self.draw_lane_header(
                card,
                canvas,
                lane,
                header_y,
                inactive=inactive[lane],
            )
        self.draw_lane_dividers(canvas, content_y0, content_y1)
        for lane, placement in enumerate(statuses):
            self.draw_status(
                canvas,
                self.lane_rect(lane, status_y, layout.row_height),
                placement.status,
                theme.inactive_text if inactive[lane] else theme.text,
                placement.movement_endpoint,
            )
        if previous is not None:
            self.draw_frame_rows(
                canvas,
                previous,
                current_y,
                names_on_lower_row=True,
                inactive=inactive,
            )
        if next_frame is not None:
            self.draw_frame_rows(
                canvas,
                next_frame,
                next_y,
                names_on_lower_row=False,
                inactive=inactive,
            )
        return self._round_card(card)

    def _round_card(self, card: Image.Image) -> Image.Image:
        mask = Image.new("L", card.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            (0, 0, card.width - 1, card.height - 1),
            radius=Canvas.p(self.layout.card_radius),
            fill=255,
        )
        card.putalpha(mask)
        return card

    def composite_card(self, page: Image.Image, card: Image.Image) -> None:
        layout = self.layout
        margin = layout.single_margin
        alpha = card.getchannel("A")
        blur = Canvas.p(layout.card_shadow_blur)
        offset_y = Canvas.p(layout.card_shadow_y)
        padding = max(1, blur * 2 + abs(offset_y) + 2)
        shadow_size = (card.width + padding * 2, card.height + padding * 2)
        shadow_alpha = Image.new("L", shadow_size, 0)
        shadow_alpha.paste(alpha, (padding, padding))
        shadow_alpha = shadow_alpha.filter(ImageFilter.GaussianBlur(blur))
        shadow = Image.new("RGBA", shadow_size, _rgba("#1f2937", 0))
        shadow.putalpha(shadow_alpha.point(lambda amount: round(amount * 0.12)))
        page.alpha_composite(
            shadow,
            dest=(Canvas.p(margin) - padding, Canvas.p(margin) - padding + offset_y),
        )
        page.alpha_composite(card, dest=(Canvas.p(margin), Canvas.p(margin)))
        Canvas(page).rounded_rectangle(
            (
                margin,
                margin,
                margin + layout.card_width,
                margin + card.height / _WORK_SCALE,
            ),
            radius=layout.card_radius,
            outline=self.theme.outer_line,
            width=layout.card_border_width,
        )

    def render(self, value: ShiftNoticeRenderInput) -> Image.Image:
        card = self.render_card(value)
        margin = self.layout.single_margin
        page = Image.new(
            "RGBA",
            (
                Canvas.p(self.layout.card_width + margin * 2),
                card.height + Canvas.p(margin * 2),
            ),
            _rgba(self.theme.page_bg),
        )
        self.composite_card(page, card)
        page = page.resize(
            (page.width // _ANTIALIAS, page.height // _ANTIALIAS),
            Image.Resampling.LANCZOS,
        )
        return page.convert("RGB")


def _range_start(range_label: str) -> str:
    start, separator, _end = range_label.partition("–")
    if not separator or not start:
        msg = f"Invalid Shift Notice range label: {range_label!r}."
        raise ShiftNoticeRenderError(msg)
    return start


def _range_end(range_label: str) -> str:
    _start, separator, end = range_label.partition("–")
    if not separator or not end:
        msg = f"Invalid Shift Notice range label: {range_label!r}."
        raise ShiftNoticeRenderError(msg)
    return end


def render_shift_notice(value: ShiftNoticeRenderInput) -> bytes:
    """Render one Shift Notice as an in-memory true-2x, 192-DPI PNG."""

    try:
        image = _Renderer().render(value)
        output = io.BytesIO()
        image.save(output, format="PNG", dpi=(192, 192))
    except ShiftNoticeRenderError:
        raise
    except (OSError, ValueError) as error:
        msg = "Failed to render Shift Notice PNG."
        raise ShiftNoticeRenderError(msg) from error
    return output.getvalue()
