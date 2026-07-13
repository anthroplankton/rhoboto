from __future__ import annotations

import itertools as it
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator, Sequence
    from typing import Self

    from utils.google_sheets import AsyncioGspreadWorksheet


CELL_PATTERN = re.compile(r"^[A-Z]+[1-9][0-9]*$")
_SAFE_WORKSHEET_CONTRACT_LOG_HINTS = {
    "invalid_worksheet_contract",
    "required_header_missing",
    "required_header_duplicate",
}


class WorksheetContractError(Exception):
    """Raised when a worksheet does not match the required bot-owned layout."""

    def __init__(self, *, log_hint: str = "invalid_worksheet_contract") -> None:
        super().__init__("Worksheet contract validation failed.")
        self.log_hint = (
            log_hint
            if log_hint in _SAFE_WORKSHEET_CONTRACT_LOG_HINTS
            else "invalid_worksheet_contract"
        )


def required_unique_header_index(
    headers: Sequence[object],
    required_header: object,
) -> int:
    """Return a required header's zero-based index without exposing header values."""
    matches = [index for index, value in enumerate(headers) if value == required_header]
    if not matches:
        raise WorksheetContractError(log_hint="required_header_missing")
    if len(matches) > 1:
        raise WorksheetContractError(log_hint="required_header_duplicate")
    return matches[0]


def validate_anchor_cell(cell: str) -> str:
    """Validate anchor cell format (e.g., 'A1'). Return 'A1' if invalid."""
    if CELL_PATTERN.fullmatch(cell):
        return cell
    return "A1"


@dataclass
class UserInfo:
    username: str
    display_name: str

    """
    User information for a team member.

    Attributes:
        username (str): The user's Discord username.
        display_name (str): The user's Discord display name.
    """


ORIGINAL_MESSAGE_LINE_SEPARATOR = " ⏎  "


@dataclass(frozen=True)
class SubmissionParseResult[TSubmission]:
    submission: TSubmission | None
    invalid_attempts: list[str]


@dataclass
class OriginalMessage:
    """
    Represents the original message content.

    Attributes:
        original_message (str): The original message content.
    """

    original_message: str


@dataclass
class WorksheetMetadata:
    """
    Represents metadata for a worksheet.
    """

    id: int | None
    title: str | None
    worksheet: AsyncioGspreadWorksheet | None

    @property
    def purpose(self) -> str:
        """
        Returns the purpose of the worksheet.
        This should be overridden in subclasses.
        """
        msg = "Subclasses must implement 'purpose' property."
        raise NotImplementedError(msg)

    @property
    def db_field(self) -> str:
        """
        Returns the database field name for the worksheet.
        This should be overridden in subclasses.
        """
        msg = "Subclasses must implement 'db_field' property."
        raise NotImplementedError(msg)

    @property
    def is_collection_field(self) -> bool:
        """
        Returns whether the worksheet is a collection field.
        This should be overridden in subclasses.
        """
        msg = "Subclasses must implement 'is_collection_field' property."
        raise NotImplementedError(msg)

    def __post_init__(self) -> None:
        """
        Post-initialization to ensure that id and title are set correctly.
        If worksheet is provided, it will override id and title if they are None.
        """
        if self.worksheet:
            if self.id is None:
                self.id = self.worksheet.id
            if self.title is None:
                self.title = self.worksheet.title

    def is_missing(self) -> bool:
        """
        Checks if the worksheet is missing.

        Returns:
            bool: True if the worksheet is missing, False otherwise.
        """
        return self.worksheet is None

    @classmethod
    def default_title_generator(cls) -> Generator[str]:
        yield from (f"Worksheet {i}" for i in it.count(1))


@dataclass
class GoogleSheetsMetadata:
    """
    Represents metadata for a Google Sheets document.
    """

    sheet_url: str
    worksheets: list[WorksheetMetadata]

    def __iter__(self) -> Iterator[WorksheetMetadata]:
        """
        Returns an iterator over the worksheets.
        """
        return iter(self.worksheets)

    def to_id_mapping(self) -> dict[int, AsyncioGspreadWorksheet | None]:
        """
        Converts the metadata to a mapping of worksheet IDs to worksheet objects.

        Args:
            metadata (GoogleSheetsMetadata): The metadata to convert.

        Returns:
            dict[int, AsyncioGspreadWorksheet | None]:
                Mapping of worksheet IDs to worksheet objects.
        """
        return {ws.id: ws.worksheet for ws in self if ws.id is not None}

    def to_title_mapping(self) -> dict[str, AsyncioGspreadWorksheet | None]:
        """
        Converts the metadata to a mapping of worksheet titles to worksheet objects.

        Returns:
            dict[str, AsyncioGspreadWorksheet | None]:
                Mapping of worksheet titles to worksheet objects.
        """
        return {ws.title: ws.worksheet for ws in self if ws.title is not None}

    def extended_by_id(self, other: Self) -> Self:
        """
        Returns a new metadata object with an extended list of WorksheetMetadata,
        combining this instance and another by worksheet ID.

        For each WorksheetMetadata in self.worksheets:
            - If its `worksheet` attribute is None and another WorksheetMetadata with
              the same id exists in `other`, use the `worksheet` from `other`
              (id from self, title will auto-populate from worksheet if available).
            - Otherwise, retain the original WorksheetMetadata from self.

        Then, for each WorksheetMetadata in other whose id is not present in self,
        append it to the result.

        Note:
            - WorksheetMetadata with id=None are always preserved from both self and
              other (no deduplication).
            - This method does not mutate either input; it returns a new
              GoogleSheetsMetadata instance.

        Args:
            other (Self): Another GoogleSheetsMetadata instance to extend from.

        Returns:
            Self: A new instance with the extended list of WorksheetMetadata.
        """
        # Build a mapping from id to WorksheetMetadata for other (excluding id=None)
        other_id_map = other.to_id_mapping()
        self_ids = self.to_id_mapping().keys()
        new_worksheets = []
        for ws in self:
            if ws.worksheet is None and ws.id in other_id_map:
                # Use other's worksheet if self's worksheet is None
                other_ws = other_id_map[ws.id]
                new_worksheets.append(WorksheetMetadata(ws.id, None, other_ws))
            else:
                new_worksheets.append(WorksheetMetadata(ws.id, None, ws.worksheet))
        # Add other's worksheets if id is not in self
        new_worksheets.extend(
            WorksheetMetadata(ws.id, None, ws.worksheet)
            for ws in other
            if ws.id not in self_ids
        )
        return type(self)(self.sheet_url, new_worksheets)

    def extended_by_title(self, other: Self) -> Self:
        """
        Returns a new metadata object with an extended list of WorksheetMetadata,
        combining this instance and another by worksheet title.

        For each WorksheetMetadata in self.worksheets:
            - If its `worksheet` attribute is None and another WorksheetMetadata with
              the same title exists in `other`, use the `worksheet` from `other`
              (title from self, id will auto-populate from worksheet if available).
            - Otherwise, retain the original WorksheetMetadata from self.

        Then, for each WorksheetMetadata in other whose title is not present in self,
        append it to the result.

        Note:
            - WorksheetMetadata with title=None are always preserved from both self and
              other (no deduplication).
            - This method does not mutate either input; it returns a new
              GoogleSheetsMetadata instance.

        Args:
            other (Self): Another GoogleSheetsMetadata instance to extend from.

        Returns:
            Self: A new instance with the extended list of WorksheetMetadata.
        """
        # Build a mapping from title to WorksheetMetadata for other
        # (excluding title=None)
        other_title_map = other.to_title_mapping()
        self_titles = self.to_title_mapping().keys()
        new_worksheets = []
        for ws in self:
            if ws.worksheet is None and ws.title in other_title_map:
                # Use other's worksheet if self's worksheet is None
                other_ws = other_title_map[ws.title]
                new_worksheets.append(type(ws)(None, ws.title, other_ws))
            else:
                new_worksheets.append(type(ws)(None, ws.title, ws.worksheet))
        # Add other's worksheets if title is not in self
        new_worksheets.extend(
            type(ws)(None, ws.title, ws.worksheet)
            for ws in other
            if ws.title not in self_titles
        )
        return type(self)(self.sheet_url, new_worksheets)

    @classmethod
    def from_id_mapping(
        cls, sheet_url: str, id_mapping: dict[int, AsyncioGspreadWorksheet | None]
    ) -> Self:
        """
        Creates a new GoogleSheetsMetadata instance from an ID mapping.

        Args:
            sheet_url (str): The URL of the Google Sheets document.
            id_mapping (dict[int, AsyncioGspreadWorksheet | None]):
                Mapping of worksheet IDs to worksheet objects.

        Returns:
            GoogleSheetsMetadata: New instance with updated worksheets.
        """
        return cls(
            sheet_url=sheet_url,
            worksheets=[
                WorksheetMetadata(wsid, None, id_mapping[wsid]) for wsid in id_mapping
            ],
        )

    @classmethod
    def from_title_mapping(
        cls, sheet_url: str, title_mapping: dict[str, AsyncioGspreadWorksheet | None]
    ) -> Self:
        """
        Creates a new GoogleSheetsMetadata instance from a title mapping.

        Args:
            sheet_url (str): The URL of the Google Sheets document.
            title_mapping (dict[str, AsyncioGspreadWorksheet | None]):
                Mapping of worksheet titles to worksheet objects.

        Returns:
            GoogleSheetsMetadata: New instance with updated worksheets.
        """
        return cls(
            sheet_url=sheet_url,
            worksheets=[
                WorksheetMetadata(None, title, title_mapping[title])
                for title in title_mapping
            ],
        )

    @classmethod
    def from_subtyped_worksheets(
        cls, sheet_url: str, worksheets: list[WorksheetMetadata]
    ) -> Self:
        """
        Creates a new GoogleSheetsMetadata instance from a list of subtyped worksheets.

        Args:
            sheet_url (str): The URL of the Google Sheets document.
            worksheets (list[WorksheetMetadata]): List of worksheet metadata.

        Returns:
            GoogleSheetsMetadata: New instance with updated worksheets.
        """
        return cls(sheet_url, worksheets)

    @classmethod
    def assign_missing_default_titles(
        cls, metadata: Self, counts: dict[type[WorksheetMetadata], int] | None = None
    ) -> Self:
        """
        Fills in missing titles for worksheets in the metadata.

        Args:
            metadata (GoogleSheetsMetadata): The metadata to fill titles for.

        Returns:
            GoogleSheetsMetadata: Updated metadata with filled titles.
        """
        counts = counts or {}
        curr_counts: defaultdict[type[WorksheetMetadata], int] = defaultdict(int)
        total_worksheets = len(metadata.worksheets)
        title_set = {ws.title for ws in metadata.worksheets if ws.title}
        new_worksheets = []
        default_title_gens = {}

        def next_title(ws_type: type[WorksheetMetadata]) -> str | None:
            if ws_type not in default_title_gens:
                default_title_gens[ws_type] = ws_type.default_title_generator()
            for title in default_title_gens[ws_type]:
                if title not in title_set:
                    return title
            return None

        for ws in metadata.worksheets:
            ws_type = type(ws)
            title = (
                next_title(ws_type)
                if ws.title is None
                and curr_counts[ws_type] < counts.get(ws_type, total_worksheets)
                else ws.title
            )
            new_worksheets.append(type(ws)(ws.id, title, ws.worksheet))
            curr_counts[ws_type] += 1

        for ws_type, count in counts.items():
            while curr_counts[ws_type] < count:
                title = next_title(ws_type)
                new_worksheets.append(ws_type(None, title, None))
                curr_counts[ws_type] += 1

        return cls.from_subtyped_worksheets(metadata.sheet_url, new_worksheets)
