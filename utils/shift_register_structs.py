from __future__ import annotations

import dataclasses
import itertools as it
import re
from dataclasses import InitVar, dataclass, field
from typing import TYPE_CHECKING, ClassVar, Self, override

from utils.structs_base import (
    GoogleSheetsMetadata,
    OriginalMessage,
    UserInfo,
    WorksheetContentBase,
    WorksheetMetadata,
)

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator


class Period:

    def __init__(self, start: int, end: int) -> None:
        self.start = start % 24
        self.end = end % 24

    def __iter__(self) -> Generator[int, None, None]:
        start = self.start
        end = self.end
        if end < start:
            end += 24
        for t in range(start, end):
            yield ShiftParser.standardize(t)

    def __repr__(self) -> str:
        return f"Period({self.start}, {self.end})"


@dataclass
class Shift(OriginalMessage, UserInfo):

    shifts: InitVar[set[int]]

    def __post_init__(self, shifts: set[int]) -> None:
        """
        Post-initialization to set up shifts.

        Args:
            shifts (set[int]): Set of shift numbers.
        """
        self._shifts = set(map(ShiftParser.standardize, shifts))

    def __getattr__(self, name: str) -> int:  # compatible with google sheets
        try:
            num = int(name)
        except ValueError as e:
            if name in ShiftParser.HOUR_LABELS:
                num = ShiftParser.HOUR_SLOTS[ShiftParser.HOUR_LABELS.index(name)]
            else:
                raise AttributeError(name) from e
        return int(num in self._shifts)

    def __repr__(self) -> str:
        ranges = self._merge_ranges()
        shifts = ", ".join(f"{start}-{end+1}" for start, end in ranges)
        return f"Shift({self.user}, shifts={shifts})"

    def __bool__(self) -> bool:
        """
        Check if the Shift object has any shifts.

        Returns:
            bool: True if there are shifts, False otherwise.
        """
        return bool(self._shifts)

    def __contains__(self, shift: int) -> bool:
        """
        Check if a shift number is in the set of shifts.

        Args:
            shift (int): The shift number to check.

        Returns:
            bool: True if the shift number is in the set, False otherwise.
        """
        return shift in self._shifts

    def __iter__(self) -> Iterator[int]:
        """
        Iterate over the shift numbers.

        Yields:
            int: Each shift number in the set.
        """
        yield from self._shifts

    def _merge_ranges(self) -> list[tuple[int, int]]:
        if not self._shifts:
            return []
        sorted_nums = sorted(self._shifts)
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
        Get a list of tuples with shift numbers and their presence.

        Returns:
            list[tuple[int, bool]]: List of tuples with shift number and presence.
        """
        return [(n, n in self._shifts) for n in ShiftParser.HOUR_SLOTS]


class ShiftParser:
    """
    Parser for shift info lines.

    Attributes:
        pattern (Pattern): Regex pattern for parsing shift info lines.
    """

    SPLIT_HOUR: int = 4
    HOUR_SLOTS: ClassVar[list[int]] = list(range(SPLIT_HOUR, SPLIT_HOUR + 24))
    HOUR_LABELS: ClassVar[list[str]] = [f"{h}-{h + 1}" for h in HOUR_SLOTS]

    PATTERN = re.compile(r"(?P<start>\d+)\s*[-－~～]\s*(?P<end>\d+)")  # noqa: RUF001

    @classmethod
    def standardize(cls, hour: int) -> int:
        """
        Standardize the hour to fit within the split hour range.

        Args:
            hour (int): The hour to standardize.

        Returns:
            int: Standardized hour.
        """
        return (
            hour + 24
            if hour < cls.SPLIT_HOUR
            else hour if hour < cls.SPLIT_HOUR + 24 else hour - 24
        )

    @classmethod
    def parse_lines(cls, user_info: UserInfo, lines: list[str]) -> Shift:
        """
        Parse multiple lines into a Shift object.

        Args:
            user_info (UserInfo): The user information.
            lines (list[str]): List of shift info strings.

        Returns:
            Shift: Parsed Shift object.
        """
        matches = (cls.PATTERN.finditer(line) for line in lines)
        shifts = set().union(
            *(
                Period(int(m.group("start")), int(m.group("end")))
                for match in matches
                for m in match
            )
        )
        return Shift(
            username=user_info.username,
            display_name=user_info.display_name,
            original_message=" / ".join(lines),
            shifts=shifts,
        )


class EntryWorksheetMetadata(WorksheetMetadata):
    """
    Represents metadata for the entry worksheet in the team register.

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
    def default_title_generator(cls) -> Generator[str, None, None]:
        """
        Generate default titles for the summary worksheet.

        Yields:
            str: Default title for the entry worksheet.
        """
        yield "Shift Entry"
        yield from (f"Shift Entry {i}" for i in it.count(1))


class DraftWorksheetMetadata(WorksheetMetadata):
    """
    Represents metadata for the draft worksheet in the team register.

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
    def default_title_generator(cls) -> Generator[str, None, None]:
        """
        Generate default titles for the summary worksheet.

        Yields:
            str: Default title for the draft worksheet.
        """
        yield "Shift Draft"
        yield from (f"Shift Draft {i}" for i in it.count(1))


class FinalScheduleWorksheetMetadata(WorksheetMetadata):
    """
    Represents metadata for the final schedule worksheet in the team register.

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
    def default_title_generator(cls) -> Generator[str, None, None]:
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
            self.WORKSHEET_METADATA_TYPES.items(), self.worksheets
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

    COLUMNS: ClassVar[list[str]] = (
        [f.name for f in dataclasses.fields(UserInfo)]
        + [str(hour) for hour in ShiftParser.HOUR_LABELS]
        + [f.name for f in dataclasses.fields(OriginalMessage)]
    )
    DTYPES: ClassVar[dict[str, str]] = (
        {f.name: str(f.type) for f in dataclasses.fields(UserInfo)}
        | {str(hour): "int" for hour in ShiftParser.HOUR_LABELS}
        | {f.name: str(f.type) for f in dataclasses.fields(OriginalMessage)}
    )

    INDEX_NAME: ClassVar[str] = COLUMNS[0]
