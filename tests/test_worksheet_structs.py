from __future__ import annotations

import pandas as pd

from tests.fakes import FakeWorksheet
from utils.shift_register_structs import (
    DraftWorksheetMetadata,
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
