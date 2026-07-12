from __future__ import annotations

import pytest

from utils.shift_register_structs import (
    HourRange,
    HourRangeFormatError,
    HourRanges,
    RecruitmentTimeRanges,
    Shift,
    ShiftParser,
)
from utils.structs_base import UserInfo


def make_user(username: str = "alice", display_name: str = "Alice") -> UserInfo:
    return UserInfo(username=username, display_name=display_name)


@pytest.mark.parametrize(
    ("text", "expected_ranges", "expected_slots"),
    [
        ("0-30", [(0, 30)], set(range(30))),
        ("0-24", [(0, 24)], set(range(24))),
        ("4-28", [(4, 28)], set(range(4, 28))),
        ("24-30", [(24, 30)], set(range(24, 30))),
        ("0-8,16-24", [(0, 8), (16, 24)], {*range(8), *range(16, 24)}),
        ("4-12,20-28", [(4, 12), (20, 28)], {*range(4, 12), *range(20, 28)}),
        ("4－12", [(4, 12)], set(range(4, 12))),  # noqa: RUF001
        ("4～12", [(4, 12)], set(range(4, 12))),  # noqa: RUF001
        ("４-１２", [(4, 12)], set(range(4, 12))),  # noqa: RUF001
        ("4️⃣-1️⃣2️⃣", [(4, 12)], set(range(4, 12))),
        ("🔟-1⃣2⃣", [(10, 12)], {10, 11}),
        ("4-12、20-28", [(4, 12), (20, 28)], {*range(4, 12), *range(20, 28)}),
        ("4-12・20-28", [(4, 12), (20, 28)], {*range(4, 12), *range(20, 28)}),
        ("４－１２，２０－２８", [(4, 12), (20, 28)], {*range(4, 12), *range(20, 28)}),  # noqa: RUF001
    ],
)
def test_hour_ranges_parse_valid_ranges(
    text: str,
    expected_ranges: list[tuple[int, int]],
    expected_slots: set[int],
) -> None:
    ranges = HourRanges.parse_strict(text)

    assert [(r.start, r.end) for r in ranges.ranges] == expected_ranges
    assert ranges.slots == expected_slots
    assert ranges.display() == ", ".join(f"{s}-{e}" for s, e in expected_ranges)


@pytest.mark.parametrize(
    "text",
    [
        "28-4",
        "4-4",
        "30-31",
        "31-32",
        "-20",
        "18-",
        "18:00-20:00",
        "18時-20時",
        "18點到20點",
    ],
)
def test_hour_ranges_parse_strict_rejects_invalid_ranges(text: str) -> None:
    with pytest.raises(HourRangeFormatError):
        HourRanges.parse_strict(text)


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (True, 4),
        (4.5, 8),
    ],
)
def test_hour_range_rejects_non_exact_int_boundaries(
    start: object,
    end: object,
) -> None:
    with pytest.raises(HourRangeFormatError):
        HourRange(start, end)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("4-8,6-10", "4-10"),
        ("4-8,8-12", "4-12"),
        ("8-12,4-8", "4-12"),
        ("4-8,10-12", "4-8, 10-12"),
    ],
)
def test_hour_ranges_normalizes_overlap_and_adjacency(
    text: str,
    expected: str,
) -> None:
    assert HourRanges.parse_strict(text).display() == expected


def test_recruitment_time_ranges_default_and_json_round_trip() -> None:
    default_ranges = RecruitmentTimeRanges.default()

    assert default_ranges.display() == "4-28"
    assert default_ranges.to_json() == [{"start": 4, "end": 28}]
    assert RecruitmentTimeRanges.from_json(None).display() == "4-28"
    assert RecruitmentTimeRanges.from_json([]).display() == "4-28"
    assert RecruitmentTimeRanges.from_modal_input("").display() == "4-28"
    assert RecruitmentTimeRanges.from_json(default_ranges.to_json()).display() == "4-28"


def test_recruitment_time_ranges_announcement_display_uses_middle_dot() -> None:
    ranges = RecruitmentTimeRanges.from_json(
        [
            {"start": 4, "end": 10},
            {"start": 14, "end": 20},
            {"start": 24, "end": 28},
        ]
    )

    assert ranges.display() == "4-10, 14-20, 24-28"
    assert ranges.announcement_display() == "4-10・14-20・24-28"


@pytest.mark.parametrize(
    "value",
    [
        [{"start": True, "end": 4}],
        [{"start": 4.9, "end": 8}],
    ],
)
def test_recruitment_time_ranges_from_json_rejects_non_int_values(
    value: list[dict[str, object]],
) -> None:
    with pytest.raises(HourRangeFormatError):
        RecruitmentTimeRanges.from_json(value)


@pytest.mark.parametrize(
    "value",
    [
        {"start": 4, "end": 8},
        ["4-8"],
        [{"end": 8}],
        [{"start": 4}],
    ],
)
def test_recruitment_time_ranges_from_json_rejects_malformed_shapes(
    value: object,
) -> None:
    with pytest.raises(HourRangeFormatError):
        RecruitmentTimeRanges.from_json(value)


def test_recruitment_time_ranges_contains_canonical_slots() -> None:
    ranges = RecruitmentTimeRanges.default()

    assert ranges.contains_slots(set(range(4, 28)))
    assert not ranges.contains_slots({3})
    assert not ranges.contains_slots({4.0})  # type: ignore[arg-type]


def test_shift_parser_accepts_linear_0_30_ranges_and_notes() -> None:
    result = ShiftParser.parse_submission(
        make_user(),
        [
            "  希望 4-12 可以  ",
            "  20-28 備註文字  ",
        ],
    )

    assert result.invalid_attempts == []
    assert result.shift is not None
    assert result.submission is result.shift
    assert set(result.shift) == {*range(4, 12), *range(20, 28)}
    assert result.shift.original_message == "希望 4-12 可以 ⏎  20-28 備註文字"
    assert result.periods.display() == "4-12, 20-28"


@pytest.mark.parametrize(
    "connector",
    "-‐‑‒–—―−⁓〜～〰ーｰ⸺⸻﹘﹣－➖",  # noqa: RUF001
)
def test_shift_parser_accepts_common_range_connectors(connector: str) -> None:
    line = f"20{connector}21"

    result = ShiftParser.parse_submission(make_user(), [line])

    assert result.invalid_attempts == []
    assert result.shift is not None
    assert set(result.shift) == {20}
    assert result.shift.original_message == line


@pytest.mark.parametrize(
    ("line", "expected_slots"),
    [
        ("2️⃣0️⃣-2️⃣1️⃣", {20}),
        ("2⃣0⃣-2⃣1⃣", {20}),
        ("🔟-1️⃣2️⃣", {10, 11}),
    ],
)
def test_shift_parser_accepts_emoji_digits(
    line: str,
    expected_slots: set[int],
) -> None:
    result = ShiftParser.parse_submission(make_user(), [line])

    assert result.invalid_attempts == []
    assert result.shift is not None
    assert set(result.shift) == expected_slots
    assert result.shift.original_message == line


@pytest.mark.parametrize(
    ("line", "expected_invalid"),
    [
        ("4-12 18:00-20:00", "18:00-20:00"),
        ("4-12 18.00-20.00", "18.00-20.00"),
        ("4-12,30-31", "30-31"),
        ("4-12,28-4", "28-4"),
        ("4-12\n18-", "18-"),
        ("4-12 4--12", "4--12"),
        ("4-12 4-12-20", "4-12-20"),
        ("4-12 99-100", "99-100"),
    ],
)
def test_shift_parser_reports_invalid_attempts_for_strict_mixed(
    line: str,
    expected_invalid: str,
) -> None:
    result = ShiftParser.parse_submission(make_user(), line.splitlines())

    assert expected_invalid in result.invalid_attempts
    assert result.shift is not None
    assert set(result.shift) == set(range(4, 12))


@pytest.mark.parametrize(
    "line",
    ["公告", "20:00", "20點前", "2026-8-12", "18至20", "18 to 20", "18時から20時"],
)
def test_shift_parser_treats_ordinary_text_as_noop(line: str) -> None:
    result = ShiftParser.parse_submission(make_user(), [line])

    assert result.shift is None
    assert result.invalid_attempts == []
    assert result.periods.ranges == []


@pytest.mark.parametrize(
    "line",
    [
        "18:00-20:00",
        "18點到20點",
        "18點到",
        "到20點",
        "18時-20時",
        "18.00-20.00",
        "18-",
        "-20",
        "4--12",
        "4-12-20",
        "26-8-12",
        "99-100",
    ],
)
def test_shift_parser_reports_invalid_attempts_without_valid_range(line: str) -> None:
    result = ShiftParser.parse_submission(make_user(), [line])

    assert result.shift is None
    assert result.invalid_attempts == [line]


def test_shift_parser_reports_invalid_attempts_in_source_order() -> None:
    result = ShiftParser.parse_submission(make_user(), ["4--12 18:00-20:00"])

    assert result.invalid_attempts == ["4--12", "18:00-20:00"]


@pytest.mark.parametrize("line", ["文字.4-12", "4-12.備註"])
def test_shift_parser_accepts_ranges_next_to_sentence_punctuation(line: str) -> None:
    result = ShiftParser.parse_submission(make_user(), [line])

    assert result.invalid_attempts == []
    assert result.shift is not None
    assert set(result.shift) == set(range(4, 12))


def test_shift_google_sheet_compatible_attributes_and_items() -> None:
    shift = Shift(
        username="alice",
        display_name="Alice",
        original_message="0-2, 29-30",
        slots={0, 1, 29},
    )

    assert getattr(shift, "0-1") == 1
    assert getattr(shift, "1-2") == 1
    assert getattr(shift, "2-3") == 0
    assert getattr(shift, "29-30") == 1
    assert getattr(shift, "30") == 0
    assert (0, True) in shift.items()
    assert (29, True) in shift.items()


def test_shift_contains_requires_exact_int_slots() -> None:
    shift = Shift(
        username="alice",
        display_name="Alice",
        original_message="1-2",
        slots={1},
    )

    assert 1 in shift
    assert True not in shift
    assert 1.0 not in shift
