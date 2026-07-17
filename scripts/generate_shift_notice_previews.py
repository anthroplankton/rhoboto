# ruff: noqa: INP001, RUF001
"""Generate deterministic local Shift Notice renderer previews."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from utils import shift_notice_renderer as renderer
from utils.shift_notice import ShiftNoticeFrame, ShiftNoticeFrameState, civil_start

OUTPUT_DIR = (
    Path(__file__).resolve().parents[1]
    / "docs"
    / "assets"
    / "shift_notice"
    / "examples"
)


def frame(
    label: str,
    names: tuple[str | None, ...] = (None,) * 5,
    hours: tuple[str | None, ...] = (None,) * 5,
) -> renderer.ShiftNoticeRenderFrame:
    return renderer.ShiftNoticeRenderFrame(label, names, hours)


def cut_window(
    first_hour: int,
    current_hour: int,
    *,
    row_count: int = 7,
    truncated_before: bool,
    truncated_after: bool,
) -> renderer.ShiftNoticeRenderInput:
    rows = tuple(
        ShiftNoticeFrame(
            civil_start(date(2026, 8, 1), event_hour),
            event_hour,
            1,
            ShiftNoticeFrameState.CUT,
            (None,) * 5,
        )
        for event_hour in range(first_hour, first_hour + row_count)
    )
    return renderer.ShiftNoticeRenderInput(
        renderer.ShiftNoticeCaseKind.CUT,
        None,
        frame(f"{current_hour}–{current_hour + 1}"),
        renderer.ShiftNoticeCutWindow(rows, truncated_before, truncated_after),
    )


def cross_frame_cut_window(
    *,
    truncated_before: bool,
    truncated_after: bool,
) -> renderer.ShiftNoticeRenderInput:
    row_specs = (
        (date(2026, 8, 1), 26, 1),
        (date(2026, 8, 1), 27, 1),
        *((date(2026, 8, 2), event_hour, 2) for event_hour in range(4, 9)),
    )
    rows = tuple(
        ShiftNoticeFrame(
            civil_start(event_day, event_hour),
            event_hour,
            source_id,
            ShiftNoticeFrameState.CUT,
            (None,) * 5,
        )
        for event_day, event_hour, source_id in row_specs
    )
    return renderer.ShiftNoticeRenderInput(
        renderer.ShiftNoticeCaseKind.CUT,
        None,
        frame("5–6"),
        renderer.ShiftNoticeCutWindow(rows, truncated_before, truncated_after),
    )


def previews() -> dict[str, renderer.ShiftNoticeRenderInput]:
    return {
        "01-simple-shift-start.png": renderer.ShiftNoticeRenderInput(
            renderer.ShiftNoticeCaseKind.START,
            None,
            frame(
                "13–14",
                (None, None, "支援者A", None, None),
                (None, None, "1h", None, None),
            ),
            None,
        ),
        "02-source-handoff-destination-continuation-"
        "left-right-start.png": renderer.ShiftNoticeRenderInput(
            renderer.ShiftNoticeCaseKind.TRANSITION,
            frame(
                "14–15",
                ("支援者A", None, None, None, "支援者B"),
                ("1h", None, None, None, "2h"),
            ),
            frame(
                "15–16",
                ("支援者D", "支援者B", "支援者A", "支援者F", "支援者E"),
                ("1h", "1h", "2h", "1h", "1h"),
            ),
            None,
        ),
        "03-source-continuation-destination-handoff-"
        "left-right-end.png": renderer.ShiftNoticeRenderInput(
            renderer.ShiftNoticeCaseKind.TRANSITION,
            frame(
                "14–15",
                ("支援者A", "支援者C", "支援者X", "支援者D", "支援者B"),
                ("2h", "1h", "2h", "3h", "1h"),
            ),
            frame(
                "15–16",
                (None, "支援者A", "支援者B", None, None),
                (None, "2h", "1h", None, None),
            ),
            None,
        ),
        "04-pure-left-right-movement.png": renderer.ShiftNoticeRenderInput(
            renderer.ShiftNoticeCaseKind.TRANSITION,
            frame(
                "16–17",
                ("支援者A", None, None, "支援者C", "支援者B"),
                ("1h", None, None, "2h", "3h"),
            ),
            frame(
                "17–18",
                (None, "支援者B", "支援者A", "支援者C", None),
                (None, "1h", "2h", "1h", None),
            ),
            None,
        ),
        "05-simple-handoff-encore-honso-standby.png": renderer.ShiftNoticeRenderInput(
            renderer.ShiftNoticeCaseKind.TRANSITION,
            frame(
                "18–19",
                ("支援者A", "支援者B", "支援者C", "支援者D", "支援者E"),
                ("1h", "2h", "1h", "3h", "2h"),
            ),
            frame(
                "19–20",
                ("支援者F", "支援者G", "支援者C", "支援者D", "支援者H"),
                ("1h", "1h", "2h", "1h", "3h"),
            ),
            None,
        ),
        "06-simple-continuation-encore-honso-"
        "standby.png": renderer.ShiftNoticeRenderInput(
            renderer.ShiftNoticeCaseKind.TRANSITION,
            frame(
                "20–21",
                ("支援者A", "支援者B", "支援者C", "支援者D", "支援者E"),
                ("1h", "2h", "1h", "3h", "2h"),
            ),
            frame(
                "21–22",
                ("支援者A", "支援者B", "支援者C", "支援者D", "支援者E"),
                ("2h", "3h", "2h", "1h", "3h"),
            ),
            None,
        ),
        "07-unoccupied-encore-honso-standby.png": renderer.ShiftNoticeRenderInput(
            renderer.ShiftNoticeCaseKind.TRANSITION,
            frame(
                "22–23",
                (None, None, "支援者A", "支援者B", None),
                (None, None, "1h", "2h", None),
            ),
            frame(
                "23–24",
                (None, None, "支援者A", "支援者B", None),
                (None, None, "2h", "1h", None),
            ),
            None,
        ),
        "08-simple-shift-end.png": renderer.ShiftNoticeRenderInput(
            renderer.ShiftNoticeCaseKind.END,
            frame(
                "24–25",
                (None, None, "支援者B", None, None),
                (None, None, "2h", None, None),
            ),
            None,
            None,
        ),
        "09-active-empty-shift-start.png": renderer.ShiftNoticeRenderInput(
            renderer.ShiftNoticeCaseKind.START,
            None,
            frame("25–26"),
            None,
        ),
        "10-active-empty-shift-middle.png": renderer.ShiftNoticeRenderInput(
            renderer.ShiftNoticeCaseKind.TRANSITION,
            frame("26–27"),
            frame("27–28"),
            None,
        ),
        "11-active-empty-shift-end.png": renderer.ShiftNoticeRenderInput(
            renderer.ShiftNoticeCaseKind.END,
            frame("28–29"),
            None,
            None,
        ),
        "12-long-name-fitting.png": renderer.ShiftNoticeRenderInput(
            renderer.ShiftNoticeCaseKind.START,
            None,
            frame(
                "29–30",
                (
                    None,
                    "支援者名が非常に長いサンプル表示ABCDEFGHIJKLMN",
                    None,
                    None,
                    None,
                ),
                (None, "1h", None, None, None),
            ),
            None,
        ),
        "13-cut-single-hour.png": cut_window(
            14,
            14,
            row_count=1,
            truncated_before=False,
            truncated_after=False,
        ),
        "14-cut-leading-ellipsis.png": cut_window(
            10, 13, truncated_before=True, truncated_after=False
        ),
        "15-cut-cross-frame-both-ellipses.png": cross_frame_cut_window(
            truncated_before=True,
            truncated_after=True,
        ),
        "16-cut-trailing-ellipsis.png": cut_window(
            14, 17, truncated_before=False, truncated_after=True
        ),
        "17-cut-cross-frame-seven-hours-no-ellipsis.png": cross_frame_cut_window(
            truncated_before=False,
            truncated_after=False,
        ),
    }


def main() -> None:
    for filename, value in previews().items():
        (OUTPUT_DIR / filename).write_bytes(renderer.render_shift_notice(value))


if __name__ == "__main__":
    main()
