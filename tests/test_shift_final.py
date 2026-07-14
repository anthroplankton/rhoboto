from __future__ import annotations

import datetime as dt
from dataclasses import replace

import pytest

from utils.shift_final import (
    DEFAULT_EVENT_DAY_FORMAT,
    A1Cell,
    EventDayWriteStatus,
    FinalGenerationRequest,
    FinalRoleConflict,
    FinalScheduleConflictError,
    FinalScheduleInputError,
    FinalScheduleValidationError,
    FinalScheduleValidationKind,
    build_final_generation_request,
    build_final_schedule,
    format_event_day,
    parse_a1_cell,
)
from utils.shift_register_structs import DraftWorksheetContent, RecruitmentTimeRanges
from utils.shift_scheduler import hour_label


@pytest.mark.parametrize(
    ("raw", "canonical", "row", "column"),
    [
        (" a1 ", "A1", 1, 1),
        ("ｂ１２", "B12", 12, 2),  # noqa: RUF001
        ("ZZZ99999", "ZZZ99999", 99_999, 18_278),
    ],
)
def test_parse_a1_cell_is_strict_and_normalized(
    raw: str,
    canonical: str,
    row: int,
    column: int,
) -> None:
    assert parse_a1_cell(raw) == A1Cell(row=row, column=column, a1=canonical)


@pytest.mark.parametrize(
    "raw",
    ["", "A", "1", "A0", "$A$1", "A1:B2", "Sheet1!A1", "AAAA1", "A10000000"],
)
def test_parse_a1_cell_rejects_non_cell_or_out_of_contract_values(raw: str) -> None:
    with pytest.raises(FinalScheduleInputError):
        parse_a1_cell(raw)


def test_final_request_uses_db_axis_and_exact_rectangles() -> None:
    request = build_final_generation_request(
        recruitment_ranges=RecruitmentTimeRanges.from_json(
            [{"start": 4, "end": 12}, {"start": 20, "end": 28}]
        ),
        saved_anchor="B3",
        supplied_anchor=None,
        event_date=dt.date(2026, 12, 22),
        event_day_anchor="B1",
        event_day_format=None,
    )

    assert request.expected_hours == tuple(range(4, 28))
    assert request.recruitment_slots == frozenset((*range(4, 12), *range(20, 28)))
    assert request.source_range == "B2:G25"
    assert request.main_range.a1 == "B3:G26"
    assert request.anchor_to_persist is None
    assert request.event_day.status is EventDayWriteStatus.READY


def test_default_event_day_format_matches_final_header_example() -> None:
    assert format_event_day(dt.date(2026, 12, 22), DEFAULT_EVENT_DAY_FORMAT) == (
        "12月22日 火曜日 Tuesday, December 22"
    )


def test_event_day_format_preserves_literals_and_normalizes_only_tokens() -> None:
    assert (
        format_event_day(
            dt.date(2026, 12, 22),
            "１２月・{ＭＭ}",  # noqa: RUF001
        )
        == "１２月・12"
    )


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        ("{YYYY}/{YY}", "2026/26"),
        ("{M}/{MM}", "12/12"),
        ("{MMM_en}/{MMMM_en}", "Dec/December"),
        ("{D}/{DD}/{Do_en}", "22/22/22nd"),
        ("{ddd_ja}/{dddd_ja}", "火/火曜日"),
        ("{ddd_zh_tw}/{dddd_zh_tw}", "二/星期二"),
        ("{ddd_en}/{dddd_en}", "Tue/Tuesday"),
        ("{{{YYYY}}}", "{2026}"),
    ],
)
def test_event_day_format_allowlist(pattern: str, expected: str) -> None:
    assert format_event_day(dt.date(2026, 12, 22), pattern) == expected


@pytest.mark.parametrize(
    "pattern",
    ["", "{unknown}", "{YYYY", "YYYY}"],
)
def test_event_day_format_rejects_empty_unknown_or_unmatched_tokens(
    pattern: str,
) -> None:
    with pytest.raises(FinalScheduleInputError):
        format_event_day(dt.date(2026, 12, 22), pattern)


def request_for_hours(
    start: int,
    end: int,
    *,
    recruitment_slots: set[int] | None = None,
) -> FinalGenerationRequest:
    request = build_final_generation_request(
        recruitment_ranges=RecruitmentTimeRanges.from_json(
            [{"start": start, "end": end}]
        ),
        saved_anchor="B2",
        supplied_anchor=None,
        event_date=None,
        event_day_anchor=None,
        event_day_format=None,
    )
    if recruitment_slots is not None:
        request = replace(request, recruitment_slots=frozenset(recruitment_slots))
    return request


def grid_for_rows(
    *rows: tuple[int, str, str, tuple[str, str, str], str],
) -> list[list[str]]:
    grid = [list(DraftWorksheetContent.COLUMNS)]
    grid.extend(
        [hour_label(hour), runner, encore, *honso, standby]
        for hour, runner, encore, honso, standby in rows
    )
    return grid


def test_build_final_schedule_ignores_h_plus_and_preserves_formula_text_literal() -> (
    None
):
    grid = [
        [*DraftWorksheetContent.COLUMNS, "admin"],
        ["4-5", "Runner", "=MANUAL()", "A", "", "", "", "do not read"],
        ["5-6", "", "", "", "", "", "", object()],
    ]
    plan = build_final_schedule(grid, request_for_hours(4, 6))
    assert plan.values == [
        ["Runner", "=MANUAL()", "A", "", "", ""],
        ["", "", "", "", "", ""],
    ]


@pytest.mark.parametrize(
    ("grid", "kind"),
    [
        ([], FinalScheduleValidationKind.EMPTY),
        ([["wrong"]], FinalScheduleValidationKind.HEADER),
        ([list(DraftWorksheetContent.COLUMNS)], FinalScheduleValidationKind.AXIS),
    ],
)
def test_build_final_schedule_rejects_invalid_draft_contract(
    grid: list[list[object]],
    kind: FinalScheduleValidationKind,
) -> None:
    with pytest.raises(FinalScheduleValidationError) as caught:
        build_final_schedule(grid, request_for_hours(4, 5))
    assert caught.value.kind is kind
    assert caught.value.expected is not None


def test_invalid_axis_reports_expected_and_detected_values() -> None:
    grid = [list(DraftWorksheetContent.COLUMNS), ["5-6"]]

    with pytest.raises(FinalScheduleValidationError) as caught:
        build_final_schedule(grid, request_for_hours(4, 5))

    assert caught.value.expected == "4-5"
    assert caught.value.detected == "5-6"


def test_build_final_schedule_rejects_recognized_extra_axis_label() -> None:
    grid = grid_for_rows((4, "", "", ("", "", ""), ""))
    grid.append(["5-6", "", "", "", "", "", ""])
    with pytest.raises(FinalScheduleValidationError) as caught:
        build_final_schedule(grid, request_for_hours(4, 5))
    assert caught.value.kind is FinalScheduleValidationKind.EXTRA_AXIS
    assert caught.value.expected == ""
    assert caught.value.detected == "5-6"


def test_build_final_schedule_rejects_non_string_role_values() -> None:
    grid = grid_for_rows((4, "", "", ("", "", ""), ""))
    grid[1][2] = 1
    with pytest.raises(FinalScheduleValidationError) as caught:
        build_final_schedule(grid, request_for_hours(4, 5))
    assert caught.value.kind is FinalScheduleValidationKind.ROLE_VALUE
    assert caught.value.expected is str
    assert caught.value.detected == 1


def test_duplicate_roles_report_every_hour_name_and_role() -> None:
    grid = grid_for_rows(
        (4, "Runner", "", ("", "Alice", ""), "Alice"),
        (5, "", "", ("", "", ""), ""),
        (6, "", "", ("Bob", "", ""), "Bob"),
    )
    with pytest.raises(FinalScheduleConflictError) as caught:
        build_final_schedule(grid, request_for_hours(4, 7))
    assert caught.value.conflicts == (
        FinalRoleConflict(hour=4, name="Alice", roles=("本走 2", "待機")),
        FinalRoleConflict(hour=6, name="Bob", roles=("本走 1", "待機")),
    )


def test_valid_empty_schedule_is_renderable() -> None:
    plan = build_final_schedule(
        grid_for_rows(
            (4, "", "", ("", "", ""), ""),
            (5, "", "", ("", "", ""), ""),
        ),
        request_for_hours(4, 6),
    )
    assert plan.values == [["", "", "", "", "", ""]] * 2


def test_honso_dp_bridges_non_recruitment_gap_for_visual_continuity() -> None:
    plan = build_final_schedule(
        grid_for_rows(
            (4, "", "", ("A", "B", "C"), ""),
            (5, "", "", ("C", "A", "B"), ""),
            (6, "", "", ("B", "C", "A"), ""),
        ),
        request_for_hours(4, 7, recruitment_slots={4, 6}),
    )
    assert plan.rows[1].honso == ("C", "A", "B")
    assert plan.rows[0].honso == plan.rows[2].honso == ("A", "B", "C")


def test_honso_dp_prefers_original_columns_when_costs_tie() -> None:
    plan = build_final_schedule(
        grid_for_rows(
            (4, "", "", ("A", "B", "C"), ""),
            (5, "", "", ("B", "C", "A"), ""),
        ),
        request_for_hours(4, 6),
    )
    assert plan.rows[0].honso == ("A", "B", "C")
    assert plan.rows[1].honso == ("A", "B", "C")


def test_split_detection_uses_roles_gaps_and_excludes_runner() -> None:
    plan = build_final_schedule(
        grid_for_rows(
            (4, "RunnerOnly", "RoleChange", ("HonsoLaneOnly", "", ""), ""),
            (5, "", "", ("", "", ""), ""),
            (6, "", "", ("RoleChange", "HonsoLaneOnly", ""), ""),
            (7, "", "MissingRow", ("", "", ""), ""),
            (8, "", "", ("", "", ""), "MissingRow"),
        ),
        request_for_hours(4, 9),
    )
    assert tuple(plan.split_colors) == (
        "RoleChange",
        "HonsoLaneOnly",
        "MissingRow",
    )
    assert "RunnerOnly" not in plan.split_colors


def test_split_palette_is_equal_hue_and_farthest_first() -> None:
    plan = build_final_schedule(
        grid_for_rows(
            (4, "", "A", ("B", "C", ""), "D"),
            (5, "", "", ("", "", ""), ""),
            (6, "", "A", ("B", "C", ""), "D"),
        ),
        request_for_hours(4, 7),
    )
    assert plan.split_colors == {
        "A": "#EBDECB",
        "B": "#CBD9EB",
        "C": "#CBEBCE",
        "D": "#EBCBE9",
    }
