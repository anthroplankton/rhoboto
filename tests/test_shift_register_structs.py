from __future__ import annotations

import pytest

from utils.shift_register_structs import Period, Shift, ShiftParser
from utils.structs_base import UserInfo


def make_user(username: str = "alice", display_name: str = "Alice") -> UserInfo:
    return UserInfo(username=username, display_name=display_name)


def test_shift_standardize_uses_split_hour_window() -> None:
    assert ShiftParser.standardize(2) == 26
    assert ShiftParser.standardize(4) == 4
    assert ShiftParser.standardize(27) == 27
    assert ShiftParser.standardize(28) == 4


def test_period_iterates_across_midnight() -> None:
    assert list(Period(22, 2)) == [22, 23, 24, 25]
    assert list(Period(4, 6)) == [4, 5]


def test_shift_parser_handles_multiple_lines_and_full_width_separator() -> None:
    full_width_range = "23－2"  # noqa: RUF001 - intentional parser coverage
    shift, periods = ShiftParser.parse_lines(
        make_user(),
        [
            "15-18 18-20 consecutive not allowed",
            full_width_range,
        ],
    )

    assert len(periods) == 3
    assert (
        shift.original_message
        == f"15-18 18-20 consecutive not allowed / {full_width_range}"
    )
    assert all(hour in shift for hour in [15, 16, 17, 18, 19, 23, 24, 25])
    assert bool(shift)


def test_shift_parser_returns_empty_shift_without_ranges() -> None:
    shift, periods = ShiftParser.parse_lines(make_user(), ["no ranges here"])

    assert periods == []
    assert not shift
    assert list(shift) == []


@pytest.mark.parametrize("line", ["18:00-20:00", "18時-20時"])
def test_shift_parser_does_not_parse_time_notation_as_valid(line: str) -> None:
    shift, periods = ShiftParser.parse_lines(make_user(), [line])

    assert periods == []
    assert not shift


@pytest.mark.parametrize(
    "line",
    [
        "18:00-20:00",
        "18點到20點",
        "18點到",
        "到20點",
        "18時-20時",
        "18-",
        "-20",
    ],
)
def test_shift_parser_detects_invalid_attempts(line: str) -> None:
    assert ShiftParser.looks_like_invalid_attempt([line])


@pytest.mark.parametrize("line", ["20:00", "20點前", "公告"])
def test_shift_parser_does_not_flag_general_text_as_invalid_attempt(
    line: str,
) -> None:
    assert not ShiftParser.looks_like_invalid_attempt([line])


def test_shift_google_sheet_compatible_attributes_and_items() -> None:
    shift = Shift(
        username="alice",
        display_name="Alice",
        original_message="2-5",
        shifts={2, 4},
    )

    assert getattr(shift, "4-5") == 1
    assert getattr(shift, "5-6") == 0
    assert getattr(shift, "2") == 0
    assert getattr(shift, "26") == 1
    assert (4, True) in shift.items()
    assert (5, False) in shift.items()
