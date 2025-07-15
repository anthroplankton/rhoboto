from __future__ import annotations

import itertools as it
import re
from abc import ABC
from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Generic, TypeVar

import pandas as pd

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator
    from typing import ClassVar, Self

    from utils.google_sheets import AsyncioGspreadWorksheet


CELL_PATTERN = re.compile(r"^[A-Z]+[1-9][0-9]*$")


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
    def default_title_generator(cls) -> Generator[str, None, None]:
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


TEntry = TypeVar("TEntry")


class WorksheetContentBase(ABC, Generic[TEntry]):

    INDEX_NAME: ClassVar[str]
    COLUMNS: ClassVar[list[str]]
    DTYPES: ClassVar[dict[str, str]]

    def __init__(
        self,
        main: pd.DataFrame | None = None,
        extra: pd.DataFrame | None = None,
        *,
        extended_columns: list[str] | None = None,
        extended_dtypes: dict[str, str] | None = None,
    ) -> None:
        columns, dtypes = self._merge_columns_dtypes(
            self.COLUMNS, self.DTYPES, extended_columns, extended_dtypes
        )
        self.main = (
            main.copy()
            if isinstance(main, pd.DataFrame)
            else pd.DataFrame(columns=columns).astype(dtypes).set_index(self.INDEX_NAME)
        )
        self.extra = (
            extra.copy()
            if isinstance(extra, pd.DataFrame)
            else pd.DataFrame(columns=columns)
        )
        self.original_row_size = len(self.main) + len(self.extra)

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}\n"
            f"main=\n{self.main!r}\n"
            f"extra=\n{self.extra!r}"
        )

    def upsert(self, entry: TEntry) -> None:
        index = getattr(entry, self.INDEX_NAME)
        # Remove the existing row
        if index in self.main.index:
            self.main = self.main.drop(index)
        # Add new row at the bottom
        self.main.loc[index] = [getattr(entry, col) for col in self.main.columns]

    def delete(self, index: str) -> None:
        if index not in self.main.index:
            return
        self.main = self.main.drop(index)

    def to_frame(self) -> pd.DataFrame:
        padding_row_size = max(
            0, self.original_row_size - len(self.main) - len(self.extra)
        )
        padding = pd.DataFrame(
            "", index=range(padding_row_size), columns=self.main.columns
        )
        return pd.concat(
            [self.main.reset_index(), padding, self.extra], ignore_index=True
        )

    @staticmethod
    def _merge_columns_dtypes(
        base_columns: list[str],
        base_dtypes: dict[str, str],
        extended_columns: list[str] | None = None,
        extended_dtypes: dict[str, str] | None = None,
    ) -> tuple[list[str], dict[str, str]]:
        """
        Merge base columns/dtypes with extensions, avoiding duplicates.
        """
        columns = [
            *base_columns,
            *filter(lambda c: c not in base_columns, extended_columns or []),
        ]
        dtypes = base_dtypes | (extended_dtypes or {})
        return columns, dtypes

    @classmethod
    def standardize_dataframe(
        cls,
        df: pd.DataFrame,
        *,
        extended_columns: list[str] | None = None,
        extended_dtypes: dict[str, str] | None = None,
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        """
        Standardize the input DataFrame by ensuring it has the correct columns
        and data types. Supports dynamic extension of columns/dtypes.

        Args:
            df (pd.DataFrame): The input DataFrame to standardize.
            extended_columns (list[str] | None): Extra columns to append.
            extended_dtypes (dict[str, str] | None): Extra dtypes to merge.

        Returns:
            tuple[pd.DataFrame, pd.DataFrame]: A tuple containing the valid and
            invalid DataFrames.
        """
        columns, dtypes = cls._merge_columns_dtypes(
            cls.COLUMNS, cls.DTYPES, extended_columns, extended_dtypes
        )
        temp = pd.DataFrame(columns=columns).astype(dtypes)

        if df.index.name == cls.INDEX_NAME:
            df = df.reset_index()

        df = df.rename(columns=dict(zip(df.columns, columns)))
        df = df[columns[: len(df.columns)]]

        temp = pd.concat([temp, df])

        def row_can_astype(row: pd.Series) -> bool:
            row_df = row.to_frame().T
            try:
                row_df.astype(dtypes)
            except (ValueError, TypeError):
                return False
            else:
                return True

        row_can_astype_mask = temp.apply(row_can_astype, axis=1)
        is_duplicate_mask = temp.duplicated(subset=cls.INDEX_NAME, keep="first")

        valid = (
            temp[row_can_astype_mask & ~is_duplicate_mask]
            .astype(dtypes)
            .set_index(cls.INDEX_NAME)
        )
        invalid = temp[~row_can_astype_mask | is_duplicate_mask]

        return valid, invalid
