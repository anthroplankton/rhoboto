from __future__ import annotations

import pandas as pd
import pytest

from tests.fakes import FakeWorksheet
from utils.shift_register_structs import (
    DraftWorksheetMetadata,
    EntryWorksheetContent,
    EntryWorksheetMetadata,
    FinalScheduleWorksheetMetadata,
    ShiftRegisterGoogleSheetsMetadata,
)
from utils.structs_base import (
    GoogleSheetsMetadata,
    UserInfo,
    WorksheetMetadata,
    validate_anchor_cell,
)
from utils.team_register_structs import (
    SummaryWorksheetMetadata,
    TeamParser,
    TeamRegisterGoogleSheetsMetadata,
    TeamWorksheetContent,
    TeamWorksheetMetadata,
)


def test_validate_anchor_cell_accepts_a1_style_only() -> None:
    assert validate_anchor_cell("A1") == "A1"
    assert validate_anchor_cell("BC23") == "BC23"
    assert validate_anchor_cell("A0") == "A1"
    assert validate_anchor_cell("a1") == "A1"
    assert validate_anchor_cell("A") == "A1"


def test_worksheet_metadata_populates_id_and_title_from_worksheet() -> None:
    worksheet = FakeWorksheet(title="Existing", worksheet_id=42)
    metadata = WorksheetMetadata(id=None, title=None, worksheet=worksheet)

    assert metadata.id == 42
    assert metadata.title == "Existing"
    assert not metadata.is_missing()


def test_google_sheets_metadata_extends_missing_by_title() -> None:
    worksheet = FakeWorksheet(title="Main Team", worksheet_id=10)
    missing = GoogleSheetsMetadata(
        "https://sheet.example",
        [TeamWorksheetMetadata(id=None, title="Main Team", worksheet=None)],
    )
    found = GoogleSheetsMetadata(
        "https://sheet.example",
        [TeamWorksheetMetadata(id=None, title="Main Team", worksheet=worksheet)],
    )

    extended = missing.extended_by_title(found)

    assert extended.worksheets[0].id == 10
    assert extended.worksheets[0].title == "Main Team"
    assert extended.worksheets[0].worksheet is worksheet


def test_team_metadata_assigns_default_titles_and_expands_count() -> None:
    metadata = TeamRegisterGoogleSheetsMetadata(
        "https://sheet.example",
        [
            TeamWorksheetMetadata(None, None, None),
            SummaryWorksheetMetadata(None, None, None),
        ],
    )

    assigned = TeamRegisterGoogleSheetsMetadata.assign_missing_default_titles(
        metadata, {TeamWorksheetMetadata: 3}
    )

    assert [worksheet.title for worksheet in assigned.team_worksheets] == [
        "Main Team",
        "Encore Team",
        "Backup Team",
    ]
    assert assigned.summary_worksheet.title == "Team Summary"


def test_shift_metadata_assigns_default_titles() -> None:
    metadata = ShiftRegisterGoogleSheetsMetadata(
        "https://sheet.example",
        [
            EntryWorksheetMetadata(None, None, None),
            DraftWorksheetMetadata(None, None, None),
            FinalScheduleWorksheetMetadata(None, None, None),
        ],
    )

    assigned = ShiftRegisterGoogleSheetsMetadata.assign_missing_default_titles(metadata)

    assert [worksheet.title for worksheet in assigned.worksheets] == [
        "Shift Entry",
        "Shift Draft",
        "Shift Final Schedule",
    ]


def test_team_worksheet_content_standardizes_valid_invalid_and_duplicate_rows() -> None:
    rows = [
        ["alice", "Alice", "150", "740", "33.4", "150/740/33.4"],
        ["alice", "Alice Duplicate", "140", "680", "35.3", "140/680/35.3"],
        ["bad", "Bad", "not-int", "680", "35.3", "bad"],
    ]
    dataframe = pd.DataFrame(rows, columns=TeamWorksheetContent.COLUMNS)

    valid, invalid = TeamWorksheetContent.standardize_dataframe(dataframe)

    assert list(valid.index) == ["alice"]
    assert valid.loc["alice", "leader_skill_value"] == 150
    assert valid.loc["alice", "team_power"] == 33.4
    assert len(invalid) == 2


def test_team_worksheet_content_upsert_delete_and_padding() -> None:
    user = UserInfo(username="alice", display_name="Alice")
    team = TeamParser.parse_line(user, "150/740/33.4 main")
    content = TeamWorksheetContent()

    content.upsert(team)
    inserted = content.to_frame()

    assert inserted.loc[0, "username"] == "alice"
    assert inserted.loc[0, "leader_skill_value"] == 150

    content.delete("alice")
    deleted = content.to_frame()

    assert deleted.empty


def test_shift_entry_columns_use_0_30_hour_axis() -> None:
    expected_columns = [
        "username",
        "display_name",
        *[f"{hour}-{hour + 1}" for hour in range(30)],
        "original_message",
    ]

    assert expected_columns == EntryWorksheetContent.COLUMNS


def test_shift_entry_dtypes_use_0_30_hour_axis() -> None:
    expected_hour_columns = [f"{hour}-{hour + 1}" for hour in range(30)]

    assert {
        column: EntryWorksheetContent.DTYPES[column] for column in expected_hour_columns
    } == dict.fromkeys(expected_hour_columns, "int")


def test_shift_entry_header_guard_accepts_new_core_header_with_trailing_extra() -> None:
    columns = [*EntryWorksheetContent.COLUMNS, "manager_note"]
    dataframe = pd.DataFrame(columns=columns)

    EntryWorksheetContent.validate_core_header(dataframe)


def test_shift_entry_header_guard_accepts_empty_fresh_worksheet() -> None:
    EntryWorksheetContent.validate_core_header(pd.DataFrame())


def test_shift_entry_header_guard_rejects_old_4_28_header() -> None:
    old_columns = [
        "username",
        "display_name",
        *[f"{hour}-{hour + 1}" for hour in range(4, 28)],
        "original_message",
    ]
    dataframe = pd.DataFrame(columns=old_columns)

    with pytest.raises(ValueError, match="Shift Entry worksheet header"):
        EntryWorksheetContent.validate_core_header(dataframe)


def test_shift_entry_header_guard_rejects_missing_core_column() -> None:
    columns = EntryWorksheetContent.COLUMNS.copy()
    columns.remove("10-11")
    dataframe = pd.DataFrame(columns=columns)

    with pytest.raises(ValueError, match="Shift Entry worksheet header"):
        EntryWorksheetContent.validate_core_header(dataframe)


def test_shift_entry_header_guard_rejects_shuffled_core_column() -> None:
    columns = EntryWorksheetContent.COLUMNS.copy()
    columns[2], columns[3] = columns[3], columns[2]
    dataframe = pd.DataFrame(columns=columns)

    with pytest.raises(ValueError, match="Shift Entry worksheet header"):
        EntryWorksheetContent.validate_core_header(dataframe)


def test_shift_entry_header_guard_rejects_inserted_core_column() -> None:
    columns = EntryWorksheetContent.COLUMNS.copy()
    columns.insert(3, "manager_note")
    dataframe = pd.DataFrame(columns=columns)

    with pytest.raises(ValueError, match="Shift Entry worksheet header"):
        EntryWorksheetContent.validate_core_header(dataframe)
