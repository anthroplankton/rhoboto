from __future__ import annotations

import itertools as it
import re
import unicodedata
from dataclasses import InitVar, dataclass, field
from typing import TYPE_CHECKING, ClassVar, Self, override

import pandas as pd

from utils.shift_scheduler import (
    ENCORE_SUPPORTER_SLOT,
    HONSO_SUPPORTER_SLOTS,
    STANDBY_SUPPORTER_SLOT,
)
from utils.structs_base import (
    ORIGINAL_MESSAGE_LINE_SEPARATOR,
    GoogleSheetsMetadata,
    OriginalMessage,
    SubmissionParseResult,
    UserInfo,
    WorksheetContentBase,
    WorksheetMetadata,
)

if TYPE_CHECKING:
    from collections.abc import Generator, Iterable, Iterator, Sequence

    from utils.shift_scheduler import DraftSchedule, DraftTeamProfile


class HourRangeFormatError(ValueError):
    def __init__(self, value: str) -> None:
        super().__init__(f"Invalid hour range: {value}")
        self.value = value


@dataclass(frozen=True)
class HourRange:
    start: int
    end: int

    MIN_BOUNDARY: ClassVar[int] = 0
    MAX_BOUNDARY: ClassVar[int] = 30

    def __post_init__(self) -> None:
        value = f"{self.start}-{self.end}"
        if self.start.__class__ is not int or self.end.__class__ is not int:
            raise HourRangeFormatError(value)
        if not (self.MIN_BOUNDARY <= self.start < self.end <= self.MAX_BOUNDARY):
            raise HourRangeFormatError(value)

    @property
    def slots(self) -> set[int]:
        return set(range(self.start, self.end))

    def display(self) -> str:
        return f"{self.start}-{self.end}"


class HourRanges:
    RANGE_PATTERN: ClassVar[re.Pattern[str]] = re.compile(
        r"(?<![\d:/\-~點点時时])"
        r"(?<!\d\.)"
        r"(?P<start>\d{1,2})\s*[-~]\s*"
        r"(?P<end>\d{1,2})"
        r"(?!\.\d)"
        r"(?![\d:/\-~點点時时])"
    )
    TIME_VALUE_PATTERN: ClassVar[str] = r"\d{1,2}(?:\s*[.:]\s*\d{2}|\s*[點点時时])?"
    INVALID_ATTEMPT_PATTERN: ClassVar[re.Pattern[str]] = re.compile(
        rf"(?<![\d:/\-~])(?:"
        rf"{TIME_VALUE_PATTERN}\s*(?:[-~]|到)\s*"
        rf"(?:{TIME_VALUE_PATTERN})?"
        rf"|(?:[-~]|到)\s*{TIME_VALUE_PATTERN}"
        rf")(?![\d:/\-~])"
    )
    MALFORMED_RANGE_PATTERN: ClassVar[re.Pattern[str]] = re.compile(
        r"(?<![\d:/\-~])(?:"
        r"\d{1,2}(?:\s*[-~]\s*){2,}\d{1,2}"
        r"|\d{1,2}(?:\s*[-~]\s*\d{1,2}){2,}"
        r"|(?:\d{3}\s*[-~]\s*\d{1,3}|\d{1,2}\s*[-~]\s*\d{3})"
        r")(?![\d:/\-~])"
    )

    def __init__(self, ranges: list[HourRange]) -> None:
        self.ranges = self._normalize(ranges)

    @classmethod
    def parse_strict(cls, text: str) -> Self:
        normalized = unicodedata.normalize("NFKC", text).strip()
        matches = list(cls.RANGE_PATTERN.finditer(normalized))
        if not matches:
            raise HourRangeFormatError(text)
        cls._raise_if_unparsed_content(text, normalized, matches)
        ranges = cls._ranges_from_matches(matches)
        return cls(ranges)

    @classmethod
    def parse_tolerant(cls, text: str) -> tuple[Self, list[str]]:
        normalized = unicodedata.normalize("NFKC", text)
        matches = list(cls.RANGE_PATTERN.finditer(normalized))
        ranges: list[HourRange] = []
        invalid_attempts: list[str] = []
        for match in matches:
            value = match.group(0)
            try:
                ranges.append(
                    HourRange(int(match.group("start")), int(match.group("end")))
                )
            except HourRangeFormatError:
                invalid_attempts.append(value)

        valid_spans = [match.span() for match in matches]
        invalid_matches = sorted(
            it.chain(
                cls.INVALID_ATTEMPT_PATTERN.finditer(normalized),
                cls.MALFORMED_RANGE_PATTERN.finditer(normalized),
            ),
            key=lambda match: match.start(),
        )
        for invalid_match in invalid_matches:
            if any(
                invalid_match.start() >= start and invalid_match.end() <= end
                for start, end in valid_spans
            ):
                continue
            invalid_attempts.append(invalid_match.group(0))

        return cls(ranges), invalid_attempts

    @classmethod
    def _ranges_from_matches(
        cls,
        matches: list[re.Match[str]],
    ) -> list[HourRange]:
        ranges: list[HourRange] = []
        invalid: list[str] = []
        for match in matches:
            try:
                ranges.append(
                    HourRange(int(match.group("start")), int(match.group("end")))
                )
            except HourRangeFormatError:
                invalid.append(match.group(0))
        if invalid:
            raise HourRangeFormatError(", ".join(invalid))
        return ranges

    @classmethod
    def _raise_if_unparsed_content(
        cls,
        original: str,
        normalized: str,
        matches: list[re.Match[str]],
    ) -> None:
        last_end = 0
        for match in matches:
            cls._raise_if_invalid_separator(
                original,
                normalized[last_end : match.start()],
            )
            last_end = match.end()
        cls._raise_if_invalid_separator(original, normalized[last_end:])

    @staticmethod
    def _raise_if_invalid_separator(original: str, value: str) -> None:
        if value.strip(" \t\r\n,、・"):
            raise HourRangeFormatError(original)

    @staticmethod
    def _normalize(ranges: list[HourRange]) -> list[HourRange]:
        if not ranges:
            return []
        sorted_ranges = sorted(ranges, key=lambda r: (r.start, r.end))
        merged: list[HourRange] = []
        current = sorted_ranges[0]
        for next_range in sorted_ranges[1:]:
            if next_range.start <= current.end:
                current = HourRange(current.start, max(current.end, next_range.end))
                continue
            merged.append(current)
            current = next_range
        merged.append(current)
        return merged

    @property
    def slots(self) -> set[int]:
        slots: set[int] = set()
        for hour_range in self.ranges:
            slots.update(hour_range.slots)
        return slots

    def display(self, separator: str = ", ") -> str:
        return separator.join(hour_range.display() for hour_range in self.ranges)

    def contains_all(self, slots: set[int]) -> bool:
        if any(slot.__class__ is not int for slot in slots):
            return False
        return slots <= self.slots


class RecruitmentTimeRanges:
    DEFAULT_JSON: ClassVar[list[dict[str, int]]] = [{"start": 4, "end": 28}]

    def __init__(self, ranges: HourRanges) -> None:
        self.ranges = ranges

    @classmethod
    def default(cls) -> Self:
        return cls.from_json(cls.DEFAULT_JSON)

    @classmethod
    def from_json(cls, value: object) -> Self:
        if value in (None, []):
            return cls.default()
        if not isinstance(value, list):
            raise HourRangeFormatError(repr(value))
        ranges: list[HourRange] = []
        for item in value:
            if not isinstance(item, dict):
                raise HourRangeFormatError(repr(item))
            start = cls._json_int(item, "start")
            end = cls._json_int(item, "end")
            ranges.append(HourRange(start, end))
        return cls(HourRanges(ranges))

    @staticmethod
    def _json_int(item: dict[object, object], key: str) -> int:
        value = item.get(key)
        if value.__class__ is not int:
            raise HourRangeFormatError(repr(item))
        return value

    @classmethod
    def from_modal_input(cls, value: str) -> Self:
        if not value.strip():
            return cls.default()
        return cls(HourRanges.parse_strict(value))

    def to_json(self) -> list[dict[str, int]]:
        return [{"start": item.start, "end": item.end} for item in self.ranges.ranges]

    def display(self) -> str:
        return self.ranges.display()

    def announcement_display(self) -> str:
        return self.ranges.display(separator="・")

    def contains_slots(self, slots: set[int]) -> bool:
        return self.ranges.contains_all(slots)


@dataclass
class Shift(OriginalMessage, UserInfo):
    slots: InitVar[set[int]]

    def __post_init__(self, slots: set[int]) -> None:
        """
        Post-initialization to set up slots.

        Args:
            slots (set[int]): Set of slot numbers.
        """
        invalid = [
            slot
            for slot in slots
            if slot.__class__ is not int or slot not in ShiftParser.HOUR_SLOTS
        ]
        if invalid:
            msg = f"Invalid shift slots: {invalid!r}"
            raise ValueError(msg)
        self._slots = set(slots)

    def __getattr__(self, name: str) -> int:  # compatible with google sheets
        try:
            num = int(name)
        except ValueError as e:
            if name in ShiftParser.HOUR_LABELS:
                num = ShiftParser.HOUR_SLOTS[ShiftParser.HOUR_LABELS.index(name)]
            else:
                raise AttributeError(name) from e
        return int(num in self._slots)

    def __repr__(self) -> str:
        ranges = self._merge_ranges()
        ranges = ", ".join(f"{start}-{end + 1}" for start, end in ranges)
        return f"Shift({self.user}, ranges={ranges})"

    def __bool__(self) -> bool:
        """
        Check if the Shift object has any slots.

        Returns:
            bool: True if there are slots, False otherwise.
        """
        return bool(self._slots)

    def __contains__(self, slot: object) -> bool:
        """
        Check if a slot number is in the set of slots.

        Args:
            slot (object): The slot number to check.

        Returns:
            bool: True if the slot number is in the set, False otherwise.
        """
        return slot.__class__ is int and slot in self._slots

    def __iter__(self) -> Iterator[int]:
        """
        Iterate over the slot numbers.

        Yields:
            int: Each slot number in the set.
        """
        yield from sorted(self._slots)

    def _merge_ranges(self) -> list[tuple[int, int]]:
        if not self._slots:
            return []
        sorted_nums = sorted(self._slots)
        ranges = []
        start = end = sorted_nums[0]
        for n in sorted_nums[1:]:
            if n == end + 1:
                end = n
            else:
                ranges.append((start, end))
                start = end = n
        ranges.append((start, end))
        return ranges

    @property
    def user(self) -> UserInfo:
        """
        Get the user information.

        Returns:
            UserInfo: The user information.
        """
        return UserInfo(self.username, self.display_name)

    def items(self) -> list[tuple[int, bool]]:
        """
        Get a list of tuples with slot numbers and their presence.

        Returns:
            list[tuple[int, bool]]: List of tuples with slot number and presence.
        """
        return [(n, n in self._slots) for n in ShiftParser.HOUR_SLOTS]


@dataclass(frozen=True)
class ShiftParseResult(SubmissionParseResult[Shift]):
    periods: HourRanges

    @property
    def shift(self) -> Shift | None:
        return self.submission


class ShiftParser:
    """Parser for shift info lines."""

    HOUR_SLOTS: ClassVar[list[int]] = list(range(30))
    HOUR_LABELS: ClassVar[list[str]] = [f"{h}-{h + 1}" for h in HOUR_SLOTS]

    @classmethod
    def parse_submission(
        cls, user_info: UserInfo, lines: list[str]
    ) -> ShiftParseResult:
        """
        Parse a full message submission into a Shift object.

        Args:
            user_info (UserInfo): The user information.
            lines (list[str]): List of shift info strings.

        Returns:
            ShiftParseResult: Parsed shift, periods, and invalid attempts.
        """
        lines = [stripped for line in lines if (stripped := line.strip())]
        original_message = ORIGINAL_MESSAGE_LINE_SEPARATOR.join(lines)
        ranges, invalid_attempts = HourRanges.parse_tolerant("\n".join(lines))
        shift = None
        if ranges.ranges:
            shift = Shift(
                username=user_info.username,
                display_name=user_info.display_name,
                original_message=original_message,
                slots=ranges.slots,
            )
        return ShiftParseResult(
            submission=shift,
            invalid_attempts=invalid_attempts,
            periods=ranges,
        )


class EntryWorksheetMetadata(WorksheetMetadata):
    """
    Represents metadata for the entry worksheet in the shift register.

    Args:
        worksheet_id (int | None): The unique ID of the entry worksheet.
        title (str | None): The title of the entry worksheet.
        worksheet (AsyncioGspreadWorksheet | None):
            The entry worksheet object, or None if missing.

    Attributes:
        worksheet_id (int | None): The unique ID of the entry worksheet.
        title (str | None): The title of the entry worksheet.
        worksheet (AsyncioGspreadWorksheet | None):
            The entry worksheet object, or None if missing.
    """

    @property
    @override
    def purpose(self) -> str:
        return "entry"

    @property
    @override
    def db_field(self) -> str:
        return "entry_worksheet_id"

    @property
    @override
    def is_collection_field(self) -> bool:
        return False

    @classmethod
    @override
    def default_title_generator(cls) -> Generator[str]:
        """
        Generate default titles for the summary worksheet.

        Yields:
            str: Default title for the entry worksheet.
        """
        yield "Shift Entry"
        yield from (f"Shift Entry {i}" for i in it.count(1))


class DraftWorksheetMetadata(WorksheetMetadata):
    """
    Represents metadata for the draft worksheet in the shift register.

    Args:
        worksheet_id (int | None): The unique ID of the draft worksheet.
        title (str | None): The title of the draft worksheet.
        worksheet (AsyncioGspreadWorksheet | None):
            The draft worksheet object, or None if missing.

    Attributes:
        worksheet_id (int | None): The unique ID of the draft worksheet.
        title (str | None): The title of the draft worksheet.
        worksheet (AsyncioGspreadWorksheet | None):
            The draft worksheet object, or None if missing.
    """

    @property
    @override
    def purpose(self) -> str:
        return "draft"

    @property
    @override
    def db_field(self) -> str:
        return "draft_worksheet_id"

    @property
    @override
    def is_collection_field(self) -> bool:
        return False

    @classmethod
    @override
    def default_title_generator(cls) -> Generator[str]:
        """
        Generate default titles for the summary worksheet.

        Yields:
            str: Default title for the draft worksheet.
        """
        yield "Shift Draft"
        yield from (f"Shift Draft {i}" for i in it.count(1))


class FinalScheduleWorksheetMetadata(WorksheetMetadata):
    """
    Represents metadata for the final schedule worksheet in the shift register.

    Args:
        worksheet_id (int | None): The unique ID of the final schedule worksheet.
        title (str | None): The title of the final schedule worksheet.
        worksheet (AsyncioGspreadWorksheet | None):
            The final schedule worksheet object, or None if missing.

    Attributes:
        worksheet_id (int | None): The unique ID of the final schedule worksheet.
        title (str | None): The title of the final schedule worksheet.
        worksheet (AsyncioGspreadWorksheet | None):
            The final schedule worksheet object, or None if missing.
    """

    @property
    @override
    def purpose(self) -> str:
        return "final_schedule"

    @property
    @override
    def db_field(self) -> str:
        return "final_schedule_worksheet_id"

    @property
    @override
    def is_collection_field(self) -> bool:
        return False

    @classmethod
    @override
    def default_title_generator(cls) -> Generator[str]:
        """
        Generate default titles for the summary worksheet.

        Yields:
            str: Default title for the final schedule worksheet.
        """
        yield "Shift Final Schedule"
        yield from (f"Shift Final Schedule {i}" for i in it.count(1))


@dataclass
class ShiftRegisterGoogleSheetsMetadata(GoogleSheetsMetadata):
    """
    Represents metadata for a Google Sheets document used in shift registration.

    Args:
        sheet_url (str): The URL of the Google Sheets document.
        worksheets (list[WorksheetMetadata]): List of worksheet metadata.

    Attributes:
        sheet_url (str): The URL of the Google Sheets document.
        worksheets (list[WorksheetMetadata]): List of worksheet metadata.
    """

    entry_worksheets: EntryWorksheetMetadata = field(init=False)
    draft_worksheet: DraftWorksheetMetadata = field(init=False)
    final_schedule_worksheet: FinalScheduleWorksheetMetadata = field(init=False)
    worksheets: list[WorksheetMetadata] = field(repr=False)

    WORKSHEET_METADATA_TYPES: ClassVar[dict[str, type[WorksheetMetadata]]] = {
        "entry_worksheets": EntryWorksheetMetadata,
        "draft_worksheet": DraftWorksheetMetadata,
        "final_schedule_worksheet": FinalScheduleWorksheetMetadata,
    }

    def __post_init__(self) -> None:
        """
        Post-initialization to set up entry, draft, and final schedule worksheets.
        """
        if len(self.worksheets) < len(self.WORKSHEET_METADATA_TYPES):
            msg = (
                "At least 3 worksheets must be provided: "
                "entry, draft, and final schedule."
            )
            raise ValueError(msg)
        for (ws_attr, ws_type), ws in zip(
            self.WORKSHEET_METADATA_TYPES.items(), self.worksheets, strict=False
        ):
            new = ws_type(ws.id, ws.title, ws.worksheet)
            setattr(self, ws_attr, new)
        # Rebuild worksheets as subclass instances so each provides correct purpose,
        # attributes, etc. This ensures all logic flows use the right worksheet type
        # and properties.
        self.worksheets = [
            self.entry_worksheets,
            self.draft_worksheet,
            self.final_schedule_worksheet,
        ]

    @classmethod
    def from_subtyped_worksheets(
        cls, sheet_url: str, worksheets: list[WorksheetMetadata]
    ) -> Self:
        """
        Construct ShiftRegisterGoogleSheetsMetadata from subtyped worksheet list.

        Args:
            sheet_url (str): The URL of the Google Sheets document.
            worksheets (list[WorksheetMetadata]):
                List of worksheet metadata (already subtyped).

        Returns:
            Self: The constructed ShiftRegisterGoogleSheetsMetadata instance.
        """
        found = []
        for ws_type in cls.WORKSHEET_METADATA_TYPES.values():
            ws = next((w for w in worksheets if isinstance(w, ws_type)), None)
            if ws is None:
                msg = f"Worksheet of purpose `{ws_type.purpose}` not found."
                raise ValueError(msg)
            found.append(ws)
        return cls(sheet_url, found)


class EntryWorksheetContent(WorksheetContentBase[Shift]):
    TEAM_COLUMNS: ClassVar[list[str]] = [
        "Main ISV",
        "Encore ISV",
        "Team Info",
    ]
    HOUR_COLUMNS: ClassVar[list[str]] = ShiftParser.HOUR_LABELS
    COLUMNS: ClassVar[list[str]] = [
        "username",
        "display_name",
        *TEAM_COLUMNS,
        *HOUR_COLUMNS,
        "original_message",
    ]
    DTYPES: ClassVar[dict[str, str]] = (
        dict.fromkeys(["username", "display_name", *TEAM_COLUMNS], "str")
        | dict.fromkeys(HOUR_COLUMNS, "int")
        | {"original_message": "str"}
    )

    INDEX_NAME: ClassVar[str] = COLUMNS[0]
    COUNT_ROW: ClassVar[int] = 1
    HEADER_ROW: ClassVar[int] = 2
    FIRST_DATA_ROW: ClassVar[int] = 3
    COLUMN_COUNT: ClassVar[int] = len(COLUMNS)

    @classmethod
    def count_row(cls) -> list[str]:
        """Build the bot-owned count row for all availability columns."""
        row = [""] * cls.COLUMN_COUNT
        row[0] = "count"
        for offset, _column in enumerate(cls.HOUR_COLUMNS, start=5):
            letter = column_letter(offset + 1)
            row[offset] = f"=COUNTIF({letter}${cls.FIRST_DATA_ROW}:{letter}, 1)"
        return row

    @classmethod
    def shift_value_ranges(
        cls,
        shift: Shift,
        *,
        row: int,
    ) -> list[dict[str, object]]:
        """Serialize one shift into the two disjoint bot-owned value ranges."""
        return [
            {
                "range": f"A{row}:B{row}",
                "values": [[shift.username, shift.display_name]],
            },
            {
                "range": f"F{row}:AJ{row}",
                "values": [
                    [
                        *(getattr(shift, column) for column in cls.HOUR_COLUMNS),
                        shift.original_message,
                    ]
                ],
            },
        ]

    @classmethod
    def validate_core_header(cls, df: object) -> None:
        columns = list(getattr(df, "columns", []))
        if not columns:
            return
        expected = cls.COLUMNS
        actual_core = columns[: len(expected)]
        if actual_core != expected:
            msg = (
                "Shift Entry worksheet header must start with "
                f"{expected!r}, got {actual_core!r}."
            )
            raise ValueError(msg)

    def to_shifts(self) -> list[Shift]:
        """Rebuild Shift entries from the standardized worksheet rows."""
        shifts: list[Shift] = []
        for username, row in self.main.iterrows():
            slots = {
                index
                for index, label in enumerate(ShiftParser.HOUR_LABELS)
                if int(row[label]) == 1
            }
            shifts.append(
                Shift(
                    username=str(username),
                    display_name=str(row["display_name"]),
                    original_message=str(row["original_message"]),
                    slots=slots,
                )
            )
        return shifts

    @classmethod
    def shifts_from_ranges(
        cls,
        header_rows: list[list[object]],
        identity_rows: list[list[object]],
        availability_rows: list[list[object]],
    ) -> list[Shift]:
        """Build shifts from the current bot-owned Entry ranges."""
        if header_rows != [cls.COLUMNS]:
            msg = "Shift Entry worksheet header does not match the current layout."
            raise ValueError(msg)
        if len(identity_rows) != len(availability_rows):
            msg = "Shift Entry participant ranges have different row counts."
            raise ValueError(msg)

        shifts = []
        availability_width = len(cls.HOUR_COLUMNS) + 1
        for identity, availability in zip(
            identity_rows,
            availability_rows,
            strict=True,
        ):
            username, display_name = [*identity[:2], "", ""][:2]
            if username in ("", None):
                continue
            values = [
                *availability[:availability_width],
                *([""] * max(0, availability_width - len(availability))),
            ]
            shifts.append(
                Shift(
                    username=str(username),
                    display_name=str(display_name),
                    original_message=str(values[-1]),
                    slots={
                        index
                        for index, value in enumerate(values[:-1])
                        if int(value) == 1
                    },
                )
            )
        return shifts


def column_letter(column: int) -> str:
    letters = ""
    while column:
        column, remainder = divmod(column - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def _formula_string(value: str) -> str:
    return f'"{value.replace(chr(34), chr(34) * 2)}"'


def _formula_column(values: Iterable[str]) -> str:
    items = list(values)
    if not items:
        return '{" "}'
    return "{" + ";".join(_formula_string(value) for value in items) + "}"


# ponytail: one IMPORTRANGE per participant; add one bot-managed helper import
# only if measured participant count, reload latency, or quota pressure requires it.
def build_team_summary_formula(  # noqa: PLR0913
    *,
    row: int,
    sheet_url: str,
    worksheet_title: str,
    username_column: int,
    roles_column: int,
    main_isv_column: int,
    encore_isv_column: int | None,
    import_last_column: str,
) -> str:
    """Build one C-cell formula that spills Team display values into D:E."""
    title = worksheet_title.replace("'", "''")
    source_range = _formula_string(f"'{title}'!A:{import_last_column}")
    username = f"CHOOSECOLS(team, {username_column})"
    roles = f"CHOOSECOLS(team, {roles_column})"
    main = f"CHOOSECOLS(team, {main_isv_column})"
    encore = (
        f'XLOOKUP($A{row}, username, CHOOSECOLS(team, {encore_isv_column}), "")'
        if encore_isv_column is not None
        else '""'
    )
    return (
        "=LET("
        f"team, IMPORTRANGE({_formula_string(sheet_url)}, {source_range}), "
        f"username, {username}, "
        f"found, COUNTIF(username, $A{row}) > 0, "
        f'roles, XLOOKUP($A{row}, username, {roles}, ""), '
        f'main, XLOOKUP($A{row}, username, {main}, ""), '
        f"encoreTeam, {encore}, "
        "HSTACK("
        "main, "
        'IF(encoreTeam <> "", encoreTeam, IF(roles <> "", main, "")), '
        'IF(found, IF(roles <> "", roles & IF(encoreTeam <> "", "", '
        '"｜Main fallback"), IF(encoreTeam <> "", "No role", "")), '  # noqa: RUF001
        '"No team yet")'
        ")"
        ")"
    )


def _longest_consecutive_hours(hours: list[int]) -> int:
    longest = current = 0
    previous: int | None = None
    for hour in hours:
        current = current + 1 if previous is not None and hour == previous + 1 else 1
        longest = max(longest, current)
        previous = hour
    return longest


@dataclass(frozen=True)
class DraftNotesTeamSource:
    """Team Summary formula metadata used by dynamic Draft Notes."""

    sheet_url: str
    worksheet_title: str
    import_last_column: str
    username_header: str
    roles_header: str
    main_isv_header: str
    main_power_header: str
    encore_isv_header: str | None
    encore_power_header: str | None


def _draft_team_value(isv: float | None, power: float | None) -> str:
    if isv is None and power is None:
        return ""
    return "/".join("—" if value is None else f"{value:g}" for value in (isv, power))


def _draft_identity_bindings(entry_worksheet_title: str) -> str:
    title = entry_worksheet_title.replace("'", "''")
    return (
        f"usernames, IFERROR(FILTER('{title}'!A3:A, "
        f'\'{title}\'!A3:A <> ""), ""), '
        f"names, IFERROR(FILTER('{title}'!B3:B, "
        f'\'{title}\'!A3:A <> ""), ""), '
        'pattern, "⟨@[a-z0-9._]{2,32}⟩$", '
        "keys, MAP(names, usernames, LAMBDA(name, username, "
        "IF(OR(SUMPRODUCT(N(names = name)) > 1, REGEXMATCH(name, pattern)), "
        'name & " ⟨@" & username & "⟩", name))), '
    )


class DraftWorksheetContent:
    """Builds the Shift Draft worksheet grid from a DraftSchedule.

    The draft worksheet is regenerated in full on each run, so it only renders
    values; unlike the entry worksheet it is never read back or header-validated.
    """

    JST_COLUMN: ClassVar[str] = "JST"
    RUNNER_COLUMN: ClassVar[str] = "ランナー"
    ENCORE_COLUMN: ClassVar[str] = "アンコ"
    HONSO_COLUMNS: ClassVar[tuple[str, str, str]] = ("本走①", "本走②", "本走③")
    STANDBY_COLUMN: ClassVar[str] = "待機"
    NOTES_HEADING: ClassVar[str] = "メモ"
    CANONICAL_NAME_LEGEND: ClassVar[str] = (
        "名前の表示ルール：通常は表示名を使用します。同じ表示名がある場合や、"  # noqa: RUF001
        "表示名が「⟨@username⟩」形式で終わる場合は、末尾に実際のユーザー名が"
        "付きます。シフトを調整するときは、名前全体をコピーしてください。"
    )
    TEAM_VALUE_LEGEND: ClassVar[str] = "編成欄の表示順：実効値/総合力"  # noqa: RUF001
    HONSO_CANDIDATE_HEADER: ClassVar[str] = "本走候補（実効値：高→低）"  # noqa: RUF001
    HONSO_FALLBACK_HEADER: ClassVar[str] = "本走候補（登録順）"  # noqa: RUF001
    ENCORE_CANDIDATE_HEADER: ClassVar[str] = "アンコ候補（実効値：高→低）"  # noqa: RUF001
    UNREGISTERED_HEADER: ClassVar[str] = "編成未登録"
    CANDIDATE_THRESHOLD_LABEL: ClassVar[str] = "アンコ候補閾値"
    NOTES_COLUMNS: ClassVar[tuple[str, ...]] = (
        "名前",
        "シフト合計（h）",  # noqa: RUF001
        "最長連続（h）",  # noqa: RUF001
        "アンコ（h）",  # noqa: RUF001
        "内部編成",
        "アンコ編成",
        "編成状態",
        "元メッセージ",
    )

    COLUMNS: ClassVar[list[str]] = [
        JST_COLUMN,
        RUNNER_COLUMN,
        ENCORE_COLUMN,
        *HONSO_COLUMNS,
        STANDBY_COLUMN,
    ]

    SUPPORTER_SLOT_COLUMNS: ClassVar[dict[str, str]] = {
        ENCORE_SUPPORTER_SLOT: ENCORE_COLUMN,
        HONSO_SUPPORTER_SLOTS[0]: HONSO_COLUMNS[0],
        HONSO_SUPPORTER_SLOTS[1]: HONSO_COLUMNS[1],
        HONSO_SUPPORTER_SLOTS[2]: HONSO_COLUMNS[2],
        STANDBY_SUPPORTER_SLOT: STANDBY_COLUMN,
    }

    @classmethod
    def from_schedule(cls, schedule: DraftSchedule) -> pd.DataFrame:
        """Render the draft schedule into a worksheet-shaped DataFrame.

        Args:
            schedule (DraftSchedule): The assignments to render.

        Returns:
            pd.DataFrame: Columns match ``COLUMNS``; one row per recruitment hour.
        """
        runner = schedule.runner or ""
        rows: list[dict[str, str]] = []
        for assignment in schedule.assignments:
            row = {
                cls.JST_COLUMN: ShiftParser.HOUR_LABELS[assignment.hour],
                cls.RUNNER_COLUMN: runner,
            }
            for supporter_slot, column in cls.SUPPORTER_SLOT_COLUMNS.items():
                row[column] = schedule.display_for(assignment, supporter_slot)
            rows.append(row)
        return pd.DataFrame(rows, columns=cls.COLUMNS)

    @classmethod
    def candidate_formula(
        cls,
        schedule: DraftSchedule,
        *,
        entry_worksheet_title: str,
        recruitment_slots: set[int],
        encore_power_threshold_cell: str,
        team_source: DraftNotesTeamSource | None,
    ) -> str:
        """Build the live per-hour candidate block spill formula."""
        title = entry_worksheet_title.replace("'", "''")
        hour_slots = "{" + ";".join(map(str, schedule.hours or [0])) + "}"
        active_slots = "{" + ";".join(map(str, sorted(recruitment_slots))) + "}"
        runner = _formula_string(schedule.runner or "")
        if team_source is None:
            team_bindings = (
                "mainIsvs, MAP(usernames, LAMBDA(username, 0)), "
                "effectiveIsvs, MAP(usernames, LAMBDA(username, 0)), "
                "honsoEligible, MAP(usernames, LAMBDA(username, TRUE)), "
                "encoreEligible, MAP(usernames, LAMBDA(username, FALSE)), "
                "unregistered, MAP(usernames, LAMBDA(username, FALSE)), "
            )
            honso_header = cls.HONSO_FALLBACK_HEADER
        else:
            source_title = team_source.worksheet_title.replace("'", "''")
            source_range = _formula_string(
                f"'{source_title}'!A:{team_source.import_last_column}"
            )
            encore_isv_binding = (
                (
                    "encoreIsvHeader, "
                    f"{_formula_string(team_source.encore_isv_header)}, "
                    "encoreIsvValues, CHOOSECOLS(team, XMATCH(encoreIsvHeader, "
                    "teamHeaders, 0)), "
                    "encoreIsvs, MAP(usernames, LAMBDA(username, "
                    'XLOOKUP(username, teamUsernames, encoreIsvValues, ""))), '
                )
                if team_source.encore_isv_header is not None
                else 'encoreIsvs, MAP(usernames, LAMBDA(username, "")), '
            )
            encore_power_binding = (
                (
                    "encorePowerHeader, "
                    f"{_formula_string(team_source.encore_power_header)}, "
                    "encorePowerValues, CHOOSECOLS(team, XMATCH(encorePowerHeader, "
                    "teamHeaders, 0)), "
                    "encorePowers, MAP(usernames, LAMBDA(username, "
                    'XLOOKUP(username, teamUsernames, encorePowerValues, ""))), '
                )
                if team_source.encore_power_header is not None
                else 'encorePowers, MAP(usernames, LAMBDA(username, "")), '
            )
            team_bindings = (
                f"teamSourceUrl, {_formula_string(team_source.sheet_url)}, "
                f"team, IMPORTRANGE(teamSourceUrl, {source_range}), "
                f"teamUsernameHeader, {_formula_string(team_source.username_header)}, "
                f"rolesHeader, {_formula_string(team_source.roles_header)}, "
                f"mainIsvHeader, {_formula_string(team_source.main_isv_header)}, "
                f"mainPowerHeader, {_formula_string(team_source.main_power_header)}, "
                "teamHeaders, CHOOSEROWS(team, 1), "
                "teamUsernames, CHOOSECOLS(team, XMATCH(teamUsernameHeader, "
                "teamHeaders, 0)), "
                "roleValues, CHOOSECOLS(team, XMATCH(rolesHeader, teamHeaders, 0)), "
                "mainIsvValues, CHOOSECOLS(team, XMATCH(mainIsvHeader, "
                "teamHeaders, 0)), "
                "mainPowerValues, CHOOSECOLS(team, XMATCH(mainPowerHeader, "
                "teamHeaders, 0)), "
                "roles, MAP(usernames, LAMBDA(username, "
                'XLOOKUP(username, teamUsernames, roleValues, ""))), '
                "mainIsvs, MAP(usernames, LAMBDA(username, "
                'XLOOKUP(username, teamUsernames, mainIsvValues, ""))), '
                "mainPowers, MAP(usernames, LAMBDA(username, "
                'XLOOKUP(username, teamUsernames, mainPowerValues, ""))), '
                f"{encore_isv_binding}{encore_power_binding}"
                "effectiveIsvs, MAP(mainIsvs, encoreIsvs, encorePowers, "
                "LAMBDA(mainIsv, encoreIsv, encorePower, "
                'IF(OR(encoreIsv <> "", encorePower <> ""), encoreIsv, mainIsv))), '
                "effectivePowers, MAP(mainPowers, encoreIsvs, encorePowers, "
                "LAMBDA(mainPower, encoreIsv, encorePower, "
                'IF(OR(encoreIsv <> "", encorePower <> ""), '
                "encorePower, mainPower))), "
                'honsoEligible, MAP(mainIsvs, LAMBDA(mainIsv, mainIsv <> "")), '
                "encoreEligible, MAP(roles, effectivePowers, effectiveIsvs, "
                "LAMBDA(role, effectivePower, effectiveIsv, "
                'AND(role <> "", effectivePower <> "", effectivePower > '
                'threshold, effectiveIsv <> ""))), '
                'unregistered, MAP(mainIsvs, LAMBDA(mainIsv, mainIsv = "")), '
            )
            honso_header = cls.HONSO_CANDIDATE_HEADER
        return (
            "=LET("
            f"threshold, IF(ISNUMBER({encore_power_threshold_cell}), "
            f"{encore_power_threshold_cell}, NA()), "
            f"{_draft_identity_bindings(entry_worksheet_title)}"
            f"availability, IFERROR(FILTER('{title}'!F3:AI, "
            f"'{title}'!A3:A <> \"\"), MAKEARRAY(1, 30, "
            "LAMBDA(row, column, 0))), "
            "entryOrder, SEQUENCE(ROWS(usernames)), "
            f"hourSlots, {hour_slots}, "
            f"recruitmentSlots, {active_slots}, "
            f'runnerEligible, N(usernames <> "") * N(names <> {runner}), '
            f"{team_bindings}"
            "activeHour, LAMBDA(hour, ISNUMBER(XMATCH(hour, recruitmentSlots, 0))), "
            "availableMask, LAMBDA(hour, N(activeHour(hour)) * runnerEligible * "
            "N(CHOOSECOLS(availability, hour + 1) = 1)), "
            "honsoMask, LAMBDA(hour, availableMask(hour) * N(honsoEligible)), "
            "encoreMask, LAMBDA(hour, availableMask(hour) * N(encoreEligible)), "
            "unregisteredMask, LAMBDA(hour, availableMask(hour) * N(unregistered)), "
            "honsoCounts, MAP(hourSlots, LAMBDA(hour, SUMPRODUCT(honsoMask(hour)))), "
            "encoreCounts, MAP(hourSlots, LAMBDA(hour, SUMPRODUCT(encoreMask(hour)))), "
            "unregisteredCounts, MAP(hourSlots, LAMBDA(hour, "
            "SUMPRODUCT(unregisteredMask(hour)))), "
            "honsoWidth, MAX(1, MAX(honsoCounts)), "
            "encoreWidth, MAX(1, MAX(encoreCounts)), "
            "unregisteredWidth, MAX(1, MAX(unregisteredCounts)), "
            "candidateNames, LAMBDA(mask, scores, IFERROR(CHOOSECOLS(SORT("
            "FILTER(HSTACK(keys, scores, entryOrder), mask), "
            '2, FALSE, 3, TRUE), 1), "")), '
            "honsoBlock, MAKEARRAY(ROWS(hourSlots) + 1, honsoWidth, "
            "LAMBDA(row, column, IF(row = 1, "
            f'IF(column = 1, {_formula_string(honso_header)}, ""), '
            'IF(column > INDEX(honsoCounts, row - 1), "", INDEX('
            "candidateNames(honsoMask(INDEX(hourSlots, row - 1)), mainIsvs), "
            "column))))), "
            "encoreBlock, MAKEARRAY(ROWS(hourSlots) + 1, encoreWidth, "
            "LAMBDA(row, column, IF(row = 1, "
            f'IF(column = 1, {_formula_string(cls.ENCORE_CANDIDATE_HEADER)}, ""), '
            'IF(column > INDEX(encoreCounts, row - 1), "", INDEX('
            "candidateNames(encoreMask(INDEX(hourSlots, row - 1)), "
            "effectiveIsvs), column))))), "
            "unregisteredBlock, MAKEARRAY(ROWS(hourSlots) + 1, unregisteredWidth, "
            "LAMBDA(row, column, IF(row = 1, "
            f'IF(column = 1, {_formula_string(cls.UNREGISTERED_HEADER)}, ""), '
            'IF(column > INDEX(unregisteredCounts, row - 1), "", INDEX('
            "candidateNames(unregisteredMask(INDEX(hourSlots, row - 1)), "
            "MAP(usernames, LAMBDA(username, 0))), column))))), "
            "blankColumn, MAKEARRAY(ROWS(hourSlots) + 1, 1, "
            'LAMBDA(row, column, "")), '
            "HSTACK(honsoBlock, blankColumn, encoreBlock, blankColumn, "
            "unregisteredBlock)"
            ")"
        )

    @classmethod
    def lookup_updates(
        cls,
        schedule: DraftSchedule,
        *,
        old_lookup_row: int | None,
        entry_worksheet_title: str,
        team_source: DraftNotesTeamSource | None,
    ) -> tuple[list[dict[str, object]], set[str]]:
        """Build exact reverse-lookup cleanup, labels, and formula updates."""
        lookup_row = len(schedule.assignments) + 4
        input_cell = f"K{lookup_row}"
        status_cell = f"L{lookup_row}"
        time_cell = f"K{lookup_row + 1}"
        message_cell = f"K{lookup_row + 2}"
        team_label_cell = f"J{lookup_row + 3}"
        team_cell = f"J{lookup_row + 4}"
        updates: list[dict[str, object]] = []
        if old_lookup_row is not None:
            updates.append(
                {
                    "range": f"J{old_lookup_row}:L{old_lookup_row + 4}",
                    "values": [],
                }
            )
        identity_bindings = _draft_identity_bindings(entry_worksheet_title)
        title = entry_worksheet_title.replace("'", "''")
        status_formula = (
            "=LET("
            f"{identity_bindings}"
            f"inputName, {input_cell}, "
            'IF(inputName = "", "", IF(ISNUMBER(XMATCH(inputName, keys, 0)), "", '
            '"⚠️ 参加者を特定できません"))'
            ")"
        )
        time_formula = (
            "=LET("
            f"{identity_bindings}"
            f"inputName, {input_cell}, "
            'matchedUsername, XLOOKUP(inputName, keys, usernames, ""), '
            "participantRow, IFERROR(XMATCH(matchedUsername, usernames, 0), 0), "
            f"availability, IFERROR(FILTER('{title}'!F3:AI, "
            f"'{title}'!A3:A <> \"\"), MAKEARRAY(1, 30, "
            "LAMBDA(row, column, 0))), "
            "selected, IF(participantRow = 0, MAKEARRAY(1, 30, "
            "LAMBDA(row, column, 0)), CHOOSEROWS(availability, participantRow)), "
            "slots, IFERROR(FILTER(SEQUENCE(30, 1, 0), "
            'TRANSPOSE(selected) = 1), ""), '
            'slotCount, SUMPRODUCT(N(slots <> "")), '
            'IF(OR(inputName = "", participantRow = 0, slotCount = 0), "", LET('
            "positions, SEQUENCE(ROWS(slots)), "
            "starts, MAP(positions, LAMBDA(i, IF(i = 1, 1, "
            "--(INDEX(slots, i) <> INDEX(slots, i - 1) + 1)))), "
            "groups, SCAN(0, starts, LAMBDA(total, start, total + start)), "
            "groupIds, UNIQUE(groups), "
            'TEXTJOIN("・", TRUE, MAP(groupIds, LAMBDA(group, '
            'MIN(FILTER(slots, groups = group)) & "-" & '
            "(MAX(FILTER(slots, groups = group)) + 1))))))"
            ")"
        )
        message_formula = (
            "=LET("
            f"{identity_bindings}"
            f"inputName, {input_cell}, "
            'matchedUsername, XLOOKUP(inputName, keys, usernames, ""), '
            f"messages, IFERROR(FILTER('{title}'!AJ3:AJ, "
            f'\'{title}\'!A3:A <> ""), ""), '
            'IF(inputName = "", "", XLOOKUP(matchedUsername, usernames, '
            'messages, ""))'
            ")"
        )
        updates.extend(
            [
                {
                    "range": f"J{lookup_row}:K{lookup_row}",
                    "values": [["名前を貼り付け", ""]],
                },
                {"range": status_cell, "values": [[status_formula]]},
                {"range": f"J{lookup_row + 1}", "values": [["シフト時間"]]},
                {"range": time_cell, "values": [[time_formula]]},
                {
                    "range": f"J{lookup_row + 2}",
                    "values": [["シフト元メッセージ"]],
                },
                {"range": message_cell, "values": [[message_formula]]},
            ]
        )
        formula_ranges = {status_cell, time_cell, message_cell}
        if team_source is not None:
            source_title = team_source.worksheet_title.replace("'", "''")
            source_range = _formula_string(
                f"'{source_title}'!A:{team_source.import_last_column}"
            )
            team_formula = (
                "=LET("
                f"{identity_bindings}"
                f"inputName, {input_cell}, "
                'matchedUsername, XLOOKUP(inputName, keys, usernames, ""), '
                f"team, IMPORTRANGE({_formula_string(team_source.sheet_url)}, "
                f"{source_range}), "
                "teamHeaders, CHOOSEROWS(team, 1), "
                "teamUsernames, CHOOSECOLS(team, XMATCH("
                f"{_formula_string(team_source.username_header)}, teamHeaders, 0)), "
                'matchCount, IF(matchedUsername = "", 0, '
                "SUMPRODUCT(N(teamUsernames = matchedUsername))), "
                "blankRow, MAKEARRAY(1, COLUMNS(teamHeaders), "
                'LAMBDA(row, column, "")), '
                "matchedRow, IF(matchCount = 0, blankRow, IF(matchCount = 1, "
                "XLOOKUP(matchedUsername, teamUsernames, team, blankRow), NA())), "
                'VSTACK(teamHeaders, IF(OR(inputName = "", '
                'matchedUsername = ""), blankRow, matchedRow))'
                ")"
            )
            updates.append({"range": team_label_cell, "values": [["編成一覧"]]})
            updates.append({"range": team_cell, "values": [[team_formula]]})
            formula_ranges.add(team_cell)
        return updates, formula_ranges

    @classmethod
    def notes_formula(
        cls,
        schedule: DraftSchedule,
        *,
        entry_worksheet_title: str,
        recruitment_time_range: str,
        team_source: DraftNotesTeamSource | None,
        team_source_warning: str | None,
    ) -> str:
        """Build the dynamic Japanese Notes spill formula below a Draft."""
        last_row = max(2, len(schedule.assignments) + 1)
        title = entry_worksheet_title.replace("'", "''")
        hour_slots = "{" + ";".join(map(str, schedule.hours or [0])) + "}"
        warning = _formula_string(team_source_warning or "")
        recruitment = _formula_string(f"募集時間【{recruitment_time_range}】")
        legend = _formula_string(cls.CANONICAL_NAME_LEGEND)
        team_legend = _formula_string(cls.TEAM_VALUE_LEGEND)
        headers = "{" + ",".join(map(_formula_string, cls.NOTES_COLUMNS)) + "}"
        if team_source is None:
            team_bindings = (
                'mainTeamIsvs, MAP(matchedUsernames, LAMBDA(username, "")), '
                'mainTeamPowers, MAP(matchedUsernames, LAMBDA(username, "")), '
                'encoreTeamIsvs, MAP(matchedUsernames, LAMBDA(username, "")), '
                'encoreTeamPowers, MAP(matchedUsernames, LAMBDA(username, "")), '
                "unregisteredFlags, MAP(matchedUsernames, "
                "LAMBDA(username, FALSE)), "
            )
        else:
            source_title = team_source.worksheet_title.replace("'", "''")
            source_range = _formula_string(
                f"'{source_title}'!A:{team_source.import_last_column}"
            )
            encore_bindings = (
                (
                    "encoreIsvHeader, "
                    f"{_formula_string(team_source.encore_isv_header)}, "
                    "encoreIsvValues, CHOOSECOLS(team, XMATCH(encoreIsvHeader, "
                    "teamHeaders, 0)), "
                    "encoreTeamIsvs, MAP(matchedUsernames, LAMBDA(username, "
                    'XLOOKUP(username, teamUsernames, encoreIsvValues, ""))), '
                )
                if team_source.encore_isv_header is not None
                else 'encoreTeamIsvs, MAP(matchedUsernames, LAMBDA(username, "")), '
            )
            encore_power_bindings = (
                (
                    "encorePowerHeader, "
                    f"{_formula_string(team_source.encore_power_header)}, "
                    "encorePowerValues, CHOOSECOLS(team, XMATCH(encorePowerHeader, "
                    "teamHeaders, 0)), "
                    "encoreTeamPowers, MAP(matchedUsernames, LAMBDA(username, "
                    'XLOOKUP(username, teamUsernames, encorePowerValues, ""))), '
                )
                if team_source.encore_power_header is not None
                else 'encoreTeamPowers, MAP(matchedUsernames, LAMBDA(username, "")), '
            )
            team_bindings = (
                f"teamSourceUrl, {_formula_string(team_source.sheet_url)}, "
                f"team, IMPORTRANGE(teamSourceUrl, {source_range}), "
                f"teamUsernameHeader, {_formula_string(team_source.username_header)}, "
                f"mainIsvHeader, {_formula_string(team_source.main_isv_header)}, "
                f"mainPowerHeader, {_formula_string(team_source.main_power_header)}, "
                "teamHeaders, CHOOSEROWS(team, 1), "
                "teamUsernames, CHOOSECOLS(team, XMATCH(teamUsernameHeader, "
                "teamHeaders, 0)), "
                "mainIsvValues, CHOOSECOLS(team, XMATCH(mainIsvHeader, "
                "teamHeaders, 0)), "
                "mainPowerValues, CHOOSECOLS(team, XMATCH(mainPowerHeader, "
                "teamHeaders, 0)), "
                "mainTeamIsvs, MAP(matchedUsernames, LAMBDA(username, "
                'XLOOKUP(username, teamUsernames, mainIsvValues, ""))), '
                "mainTeamPowers, MAP(matchedUsernames, LAMBDA(username, "
                'XLOOKUP(username, teamUsernames, mainPowerValues, ""))), '
                f"{encore_bindings}{encore_power_bindings}"
                'unregisteredFlags, MAP(mainTeamIsvs, LAMBDA(value, value = "")), '
            )
        return (
            "=LET("
            f"shifts, C2:G{last_row}, "
            f"encore, C2:C{last_row}, "
            f"hourSlots, {hour_slots}, "
            f"{_draft_identity_bindings(entry_worksheet_title)}"
            f"messages, IFERROR(FILTER('{title}'!AJ3:AJ, "
            f'\'{title}\'!A3:A <> ""), ""), '
            'people, IFERROR(UNIQUE(TOCOL(shifts, 1)), ""), '
            "knownMask, MAP(people, LAMBDA(person, "
            "ISNUMBER(XMATCH(person, keys, 0)))), "
            'knownPeople, IFERROR(FILTER(people, knownMask), ""), '
            'unknownPeople, IFERROR(FILTER(people, knownMask = FALSE), ""), '
            "totals, MAP(knownPeople, LAMBDA(person, "
            "SUMPRODUCT(N(shifts = person)))), "
            "runs, MAP(knownPeople, LAMBDA(person, LET("
            "assignedRows, BYROW(shifts, LAMBDA(row, "
            "--(SUMPRODUCT(N(row = person)) > 0))), "
            "MAX(SCAN(0, SEQUENCE(ROWS(shifts)), LAMBDA(streak, i, "
            "IF(INDEX(assignedRows, i), IF(i = 1, 1, "
            "IF(INDEX(hourSlots, i) = INDEX(hourSlots, MAX(1, i - 1)) + 1, "
            "streak + 1, 1)), 0))))))), "
            "encoreHours, MAP(knownPeople, LAMBDA(person, "
            "SUMPRODUCT(N(encore = person)))), "
            "matchedUsernames, MAP(knownPeople, LAMBDA(person, "
            'XLOOKUP(person, keys, usernames, ""))), '
            "matchedMessages, MAP(matchedUsernames, LAMBDA(username, "
            'XLOOKUP(username, usernames, messages, ""))), '
            f"{team_bindings}"
            "internalTeams, MAP(mainTeamIsvs, mainTeamPowers, LAMBDA(isv, power, "
            'IF(AND(isv = "", power = ""), "", IF(isv = "", "—", isv) & "/" & '
            'IF(power = "", "—", power)))), '
            "encoreTeams, MAP(encoreTeamIsvs, encoreTeamPowers, "
            "LAMBDA(isv, power, "
            'IF(AND(isv = "", power = ""), "", IF(isv = "", "—", isv) & "/" & '
            'IF(power = "", "—", power)))), '
            "stats, SORT(HSTACK(knownPeople, totals, runs, encoreHours, "
            "internalTeams, encoreTeams, matchedUsernames, matchedMessages, "
            "unregisteredFlags), "
            "2, FALSE, 3, FALSE, 4, FALSE, 1, TRUE), "
            "unknownLines, MAP(unknownPeople, LAMBDA(person, "
            'IF(person = "", "", "⚠️ 参加者を特定できません：" & person))), '  # noqa: RUF001
            f'metaCandidates, VSTACK("{cls.NOTES_HEADING}", {recruitment}, '
            f"{warning}, unknownLines), "
            'metaLines, FILTER(metaCandidates, metaCandidates <> ""), '
            "meta, HSTACK(metaLines, MAKEARRAY(ROWS(metaLines), 7, "
            'LAMBDA(row, column, ""))), '
            'blankRow, MAKEARRAY(1, 8, LAMBDA(row, column, "")), '
            f"headers, {headers}, "
            "rawStatRows, HSTACK(CHOOSECOLS(stats, 1), CHOOSECOLS(stats, 2), "
            "CHOOSECOLS(stats, 3), CHOOSECOLS(stats, 4), CHOOSECOLS(stats, 5), "
            "CHOOSECOLS(stats, 6), MAP(CHOOSECOLS(stats, 9), "
            'LAMBDA(flag, IF(flag, "未登録", ""))), CHOOSECOLS(stats, 8)), '
            "statRows, IFERROR(FILTER(rawStatRows, CHOOSECOLS(rawStatRows, 1) "
            '<> ""), MAKEARRAY(1, 8, LAMBDA(row, column, ""))), '
            f"legendLines, VSTACK({legend}, {team_legend}), "
            "legendRows, HSTACK(legendLines, MAKEARRAY(ROWS(legendLines), 7, "
            'LAMBDA(row, column, ""))), '
            "VSTACK(meta, blankRow, headers, statRows, blankRow, legendRows)"
            ")"
        )

    @classmethod
    def notes_snapshot(
        cls,
        schedule: DraftSchedule,
        *,
        shifts: Sequence[Shift],
        recruitment_time_range: str,
        team_profiles: dict[str, DraftTeamProfile] | None,
        team_source_warning: str | None,
    ) -> str:
        """Render the complete initial Japanese Notes as plain text."""
        shifts_by_username = {shift.username: shift for shift in shifts}
        assigned_hours: dict[str, list[int]] = {}
        encore_hours: dict[str, int] = {}
        for assignment in schedule.assignments:
            for username in set(assignment.supporter_usernames_by_slot.values()):
                assigned_hours.setdefault(username, []).append(assignment.hour)
            encore_username = assignment.supporter_usernames_by_slot.get(
                ENCORE_SUPPORTER_SLOT
            )
            if encore_username is not None:
                encore_hours[encore_username] = encore_hours.get(encore_username, 0) + 1

        lines = [
            cls.NOTES_HEADING,
            f"募集時間【{recruitment_time_range}】",
        ]
        if team_source_warning is not None:
            lines.append(team_source_warning)
        lines.append("")
        for username in sorted(
            assigned_hours,
            key=lambda item: (
                -len(assigned_hours[item]),
                -_longest_consecutive_hours(assigned_hours[item]),
                -encore_hours.get(item, 0),
                schedule.display_names.get(item, item),
            ),
        ):
            hours = assigned_hours[username]
            profile = None if team_profiles is None else team_profiles.get(username)
            parts = [
                f"{schedule.display_names.get(username, username)}："  # noqa: RUF001
                f"シフト合計 {len(hours)}h",
                f"最長連続 {_longest_consecutive_hours(hours)}h",
                f"アンコ {encore_hours.get(username, 0)}h",
            ]
            internal_team = _draft_team_value(
                None if profile is None else profile.main_isv,
                None if profile is None else profile.main_power,
            )
            if internal_team:
                parts.append(f"内部編成 {internal_team}")
            encore_team = _draft_team_value(
                None if profile is None else profile.encore_isv,
                None if profile is None else profile.encore_power,
            )
            if encore_team:
                parts.append(f"アンコ編成 {encore_team}")
            if team_profiles is not None and (
                profile is None or profile.main_isv is None
            ):
                parts.append("内部編成 未登録")
            if message := shifts_by_username[username].original_message:
                parts.append(f"元メッセージ：{message}")  # noqa: RUF001
            lines.append("｜".join(parts))  # noqa: RUF001
        lines.extend(("", cls.CANONICAL_NAME_LEGEND, cls.TEAM_VALUE_LEGEND))
        return "\n".join(lines)
