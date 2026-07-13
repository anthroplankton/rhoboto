from __future__ import annotations

import pytest

from tests.fakes import FakeWorksheet
from utils import shift_register_structs
from utils.google_sheets import DimensionMutation, GridValueUpdate
from utils.shift_register_structs import (
    DraftWorksheetMetadata,
    EntryWorksheetContent,
    EntryWorksheetMetadata,
    FinalScheduleWorksheetMetadata,
    Shift,
    ShiftRegisterGoogleSheetsMetadata,
)
from utils.structs_base import (
    ORIGINAL_MESSAGE_LINE_SEPARATOR,
    GoogleSheetsMetadata,
    UserInfo,
    WorksheetContractError,
    WorksheetMetadata,
    validate_anchor_cell,
)
from utils.team_register_structs import (
    SummaryWorksheetContent,
    SummaryWorksheetMetadata,
    TeamParser,
    TeamRegisterGoogleSheetsMetadata,
    TeamWorksheetContent,
    TeamWorksheetMetadata,
    UserInfoWithEncoreRoles,
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


def test_team_worksheet_index_accepts_reordered_middle_headers() -> None:
    headers = [
        "username",
        "team_power",
        "display_name",
        "internal_skill_value",
        "leader_skill_value",
        "original_message",
        "manager_note",
    ]
    rows = [["alice", 33.4, "Alice", 740, 150, "150/740/33.4", "ready"]]

    index = TeamWorksheetContent.index_physical_rows(headers, rows)

    assert index.bot_headers == tuple(headers[:6])
    assert index.column_by_header == {
        header: column for column, header in enumerate(headers[:6], start=1)
    }
    assert index.row_by_username == {"alice": 2}


def test_team_worksheet_index_accepts_moved_username() -> None:
    headers = [
        "display_name",
        "team_power",
        "username",
        "internal_skill_value",
        "leader_skill_value",
        "original_message",
        "manager_note",
    ]
    rows = [["Alice", 33.4, "alice", 740, 150, "same", "private"]]

    index = TeamWorksheetContent.index_physical_rows(headers, rows)

    assert index.column_by_header["username"] == 3
    assert index.row_by_username == {"alice": 2}


def test_team_worksheet_index_rejects_duplicate_nonblank_usernames() -> None:
    rows = [
        ["alice", "Alice", 150, 740, 33.4, "main"],
        ["alice", "Alice Again", "not parsed", 0, 0, "other"],
    ]

    with pytest.raises(WorksheetContractError):
        TeamWorksheetContent.index_physical_rows(TeamWorksheetContent.COLUMNS, rows)


def test_team_worksheet_index_reuses_first_completely_blank_bot_band() -> None:
    headers = [*TeamWorksheetContent.COLUMNS, "manager_note"]
    rows = [
        ["", "occupied", "", "", "", "", "preserve"],
        ["", "", "", "", "", "", "prepared"],
        ["", "", "", "", "", "", "later"],
    ]

    index = TeamWorksheetContent.index_physical_rows(headers, rows)

    assert index.first_reusable_row == 3


def test_team_upsert_preserves_row_and_serializes_current_header_order() -> None:
    headers = [
        "username",
        "team_power",
        "display_name",
        "internal_skill_value",
        "leader_skill_value",
        "original_message",
        "manager_note",
    ]
    rows = [["alice", 1, "Old", 2, 3, "old", "preserve"]]
    team = TeamParser.parse_line(
        UserInfo(username="alice", display_name="Alice"),
        "150/740/33.4 main",
    )

    mutations = TeamWorksheetContent.plan_upsert(42, headers, rows, team)

    assert mutations == (
        GridValueUpdate.from_values(
            worksheet_id=42,
            start_row=2,
            start_column=1,
            values=[["alice", 33.4, "Alice", 740, 150, "150/740/33.4 main"]],
        ),
    )


def test_team_upsert_follows_moved_username_and_repeated_message_cells() -> None:
    headers = [
        "display_name",
        "team_power",
        "username",
        "internal_skill_value",
        "leader_skill_value",
        "original_message",
    ]
    rows = [
        ["Old Alice", 1, "alice", 2, 3, "same"],
        ["Bob", 2, "bob", 3, 4, "same"],
    ]
    team = TeamParser.parse_line(
        UserInfo(username="alice", display_name="Alice"),
        "150/740/33.4 same",
    )

    (update,) = TeamWorksheetContent.plan_upsert(42, headers, rows, team)

    assert update.start_row_index == 1
    assert update.rows[0] == (
        "Alice",
        33.4,
        "alice",
        740,
        150,
        "150/740/33.4 same",
    )


def test_team_upsert_skips_occupied_blank_key_row() -> None:
    headers = [*TeamWorksheetContent.COLUMNS, "manager_note"]
    rows = [
        ["", "orphan", "", "", "", "", "preserve"],
        ["", "", "", "", "", "", "prepared"],
    ]
    team = TeamParser.parse_line(
        UserInfo(username="alice", display_name="Alice"),
        "150/740/33.4 main",
    )

    (update,) = TeamWorksheetContent.plan_upsert(42, headers, rows, team)

    assert update.start_row_index == 2
    assert update.rows[0][0] == "alice"
    assert len(update.rows[0]) == len(TeamWorksheetContent.COLUMNS)


def test_team_upsert_appends_after_last_physical_row() -> None:
    rows = [["alice", "Alice", 150, 740, 33.4, "main"]]
    team = TeamParser.parse_line(
        UserInfo(username="bob", display_name="Bob"),
        "140/680/35.3 encore",
    )

    (update,) = TeamWorksheetContent.plan_upsert(
        42,
        TeamWorksheetContent.COLUMNS,
        rows,
        team,
    )

    assert update.start_row_index == 2


def test_team_delete_removes_complete_physical_row() -> None:
    headers = [*TeamWorksheetContent.COLUMNS, "manager_note"]
    rows = [["alice", "Alice", 150, 740, 33.4, "main", "remove too"]]

    mutations = TeamWorksheetContent.plan_delete(42, headers, rows, "alice")

    assert mutations == (DimensionMutation.delete_rows(42, start_row=2),)


def test_team_delete_follows_moved_username() -> None:
    headers = [
        "display_name",
        "username",
        "leader_skill_value",
        "internal_skill_value",
        "team_power",
        "original_message",
    ]
    rows = [["Alice", "alice", 150, 740, 33.4, "same"]]

    mutations = TeamWorksheetContent.plan_delete(42, headers, rows, "alice")

    assert mutations == (DimensionMutation.delete_rows(42, start_row=2),)


def test_team_full_consumption_rejects_malformed_keyed_numeric_row() -> None:
    rows = [["alice", "Alice", "not-int", 740, 33.4, "main"]]

    with pytest.raises(WorksheetContractError):
        TeamWorksheetContent.validated_teams(TeamWorksheetContent.COLUMNS, rows)


@pytest.mark.parametrize(
    ("leader_skill", "internal_skill", "team_power"),
    [
        (True, 740, 33.4),
        (150, False, 33.4),
        (150, 740, True),
        (150.5, 740, 33.4),
        (150, 740.5, 33.4),
        (150, 740, "NaN"),
        (150, 740, "inf"),
        (150, 740, float("nan")),
        (150, 740, float("inf")),
        (-1, 740, 33.4),
        (150, -1, 33.4),
        (150, 740, -0.1),
    ],
)
def test_team_full_consumption_rejects_noncanonical_numeric_values(
    leader_skill: object,
    internal_skill: object,
    team_power: object,
) -> None:
    rows = [
        [
            "alice",
            "Alice",
            leader_skill,
            internal_skill,
            team_power,
            "main",
        ]
    ]

    with pytest.raises(WorksheetContractError):
        TeamWorksheetContent.validated_teams(TeamWorksheetContent.COLUMNS, rows)


def test_team_header_migration_initializes_empty_sheet() -> None:
    mutations = TeamWorksheetContent.plan_header_migration(42, [], [])

    assert mutations == (
        GridValueUpdate.from_values(
            worksheet_id=42,
            start_row=1,
            start_column=1,
            values=[TeamWorksheetContent.COLUMNS],
        ),
    )


def test_team_header_migration_accepts_multiple_blank_bot_rows() -> None:
    mutations = TeamWorksheetContent.plan_header_migration(42, [], [[], ["", None]])

    assert len(mutations) == 1


def test_team_header_migration_rejects_populated_headerless_bot_band() -> None:
    with pytest.raises(WorksheetContractError):
        TeamWorksheetContent.plan_header_migration(
            42,
            [],
            [[], ["alice", "", "", "", "", ""]],
        )


def test_team_header_migration_adds_missing_canonical_terminal() -> None:
    headers = TeamWorksheetContent.COLUMNS[:-1]

    mutations = TeamWorksheetContent.plan_header_migration(
        42,
        headers,
        [],
        column_count=len(headers) + 1,
    )

    assert mutations == (
        DimensionMutation.insert_columns(42, start_column=6),
        GridValueUpdate.from_values(
            worksheet_id=42,
            start_row=1,
            start_column=6,
            values=[["original_message"]],
        ),
    )


def test_team_header_migration_appends_terminal_at_exact_grid_boundary() -> None:
    headers = TeamWorksheetContent.COLUMNS[:-1]

    mutations = TeamWorksheetContent.plan_header_migration(
        42,
        headers,
        [],
        column_count=len(headers),
    )

    assert mutations == (
        DimensionMutation.append_columns(42, count=1),
        GridValueUpdate.from_values(
            worksheet_id=42,
            start_row=1,
            start_column=6,
            values=[["original_message"]],
        ),
    )


def test_team_header_migration_adds_terminal_to_moved_exact_prefix() -> None:
    headers = [
        "display_name",
        "username",
        "leader_skill_value",
        "internal_skill_value",
        "team_power",
    ]

    mutations = TeamWorksheetContent.plan_header_migration(42, headers, [])

    assert mutations == (
        DimensionMutation.insert_columns(42, start_column=6),
        GridValueUpdate.from_values(
            worksheet_id=42,
            start_row=1,
            start_column=6,
            values=[["original_message"]],
        ),
    )


def test_team_header_migration_preserves_valid_reordered_header() -> None:
    headers = [
        "username",
        "team_power",
        "display_name",
        "internal_skill_value",
        "leader_skill_value",
        "original_message",
        "manager_note",
    ]

    assert TeamWorksheetContent.plan_header_migration(42, headers, []) == ()


def test_team_header_migration_preserves_moved_username() -> None:
    headers = [
        "display_name",
        "team_power",
        "username",
        "internal_skill_value",
        "leader_skill_value",
        "original_message",
        "manager_note",
    ]

    assert TeamWorksheetContent.plan_header_migration(42, headers, []) == ()


def test_summary_worksheet_index_uses_title_derived_headers() -> None:
    titles = ["Main Team", "Encore Team"]
    dynamic_headers, _ = SummaryWorksheetContent.extended_columns_dtypes_from_titles(
        titles
    )
    headers = [*SummaryWorksheetContent.COLUMNS, *dynamic_headers, "manager_note"]
    rows = [["alice", "Alice", "", 268, 33.4, 248, 35.3, "main", "ready"]]

    index = SummaryWorksheetContent.index_physical_rows(headers, rows, titles)

    assert index.bot_headers == tuple(headers[:-1])
    assert index.row_by_username == {"alice": 2}


def test_summary_worksheet_index_preserves_reordered_title_pairs() -> None:
    titles = ["Main Team", "Encore Team"]
    headers = [
        *SummaryWorksheetContent.COLUMNS,
        "Encore Team ISV",
        "Encore Team Power",
        "Main Team ISV",
        "Main Team Power",
        "original_message",
    ]

    index = SummaryWorksheetContent.index_physical_rows(headers, [], titles)

    assert index.bot_headers == tuple(headers)


def test_summary_worksheet_index_accepts_moved_base_headers() -> None:
    headers = [
        "encore_roles",
        "username",
        "display_name",
        "Main Team ISV",
        "Main Team Power",
        "original_message",
        "manager_note",
    ]
    rows = [["Encore", "alice", "Alice", 268, 33.4, "same", "private"]]

    index = SummaryWorksheetContent.index_physical_rows(
        headers,
        rows,
        ["Main Team"],
    )

    assert index.column_by_header["username"] == 2
    assert index.row_by_username == {"alice": 2}


def test_summary_worksheet_index_accepts_username_after_title_pair() -> None:
    headers = [
        "display_name",
        "Main Team ISV",
        "Main Team Power",
        "encore_roles",
        "username",
        "original_message",
        "manager_note",
    ]
    rows = [["Alice", 268, 33.4, "Encore", "alice", "same", "private"]]

    index = SummaryWorksheetContent.index_physical_rows(
        headers,
        rows,
        ["Main Team"],
    )

    assert index.bot_headers == tuple(headers[:-1])
    assert index.column_by_header["username"] == 5
    assert index.row_by_username == {"alice": 2}


def test_summary_upsert_serializes_explicit_title_values_in_header_order() -> None:
    headers = [
        *SummaryWorksheetContent.COLUMNS,
        "Encore Team ISV",
        "Encore Team Power",
        "Main Team ISV",
        "Main Team Power",
        "original_message",
        "manager_note",
    ]
    rows = [["alice", "Old", "", 0, 0, 0, 0, "old", "preserve"]]
    user = UserInfo(username="alice", display_name="Alice")
    main = TeamParser.parse_line(user, "150/740/33.4 main")
    encore = TeamParser.parse_line(user, "140/680/35.3 encore")

    mutations = SummaryWorksheetContent.plan_upsert(
        42,
        headers,
        rows,
        UserInfoWithEncoreRoles("alice", "Alice", "Encore"),
        {
            "Main Team": main,
            "Encore Team": encore,
        },
    )

    assert mutations == (
        GridValueUpdate.from_values(
            worksheet_id=42,
            start_row=2,
            start_column=1,
            values=[
                [
                    "alice",
                    "Alice",
                    "Encore",
                    248,
                    35.3,
                    268,
                    33.4,
                    f"150/740/33.4 main{ORIGINAL_MESSAGE_LINE_SEPARATOR}"
                    "140/680/35.3 encore",
                ]
            ],
        ),
    )


def test_summary_upsert_follows_moved_base_headers_and_repeated_messages() -> None:
    headers = [
        "encore_roles",
        "username",
        "display_name",
        "Main Team ISV",
        "Main Team Power",
        "original_message",
    ]
    rows = [
        ["Old", "alice", "Old Alice", 1, 1, "same"],
        ["", "bob", "Bob", 2, 2, "same"],
    ]
    user = UserInfo(username="alice", display_name="Alice")
    main = TeamParser.parse_line(user, "150/740/33.4 same")

    (update,) = SummaryWorksheetContent.plan_upsert(
        42,
        headers,
        rows,
        UserInfoWithEncoreRoles("alice", "Alice", "Encore"),
        {"Main Team": main},
    )

    assert update.start_row_index == 1
    assert update.rows[0] == (
        "Encore",
        "alice",
        "Alice",
        268,
        33.4,
        "150/740/33.4 same",
    )


def test_summary_upsert_follows_interleaved_required_headers() -> None:
    headers = [
        "Main Team ISV",
        "display_name",
        "Encore Team Power",
        "username",
        "Main Team Power",
        "encore_roles",
        "Encore Team ISV",
        "original_message",
    ]
    rows = [[1, "Old Alice", 1, "alice", 1, "Old", 1, "old"]]
    user = UserInfo(username="alice", display_name="Alice")
    main = TeamParser.parse_line(user, "150/740/33.4 main")
    encore = TeamParser.parse_line(user, "140/680/35.3 encore")

    (update,) = SummaryWorksheetContent.plan_upsert(
        42,
        headers,
        rows,
        UserInfoWithEncoreRoles("alice", "Alice", "Encore"),
        {"Main Team": main, "Encore Team": encore},
    )

    assert update.rows[0] == (
        268,
        "Alice",
        35.3,
        "alice",
        33.4,
        "Encore",
        248,
        f"150/740/33.4 main{ORIGINAL_MESSAGE_LINE_SEPARATOR}140/680/35.3 encore",
    )


def test_summary_delete_removes_complete_physical_row() -> None:
    titles = ["Main Team"]
    dynamic_headers, _ = SummaryWorksheetContent.extended_columns_dtypes_from_titles(
        titles
    )
    headers = [*SummaryWorksheetContent.COLUMNS, *dynamic_headers, "manager_note"]
    rows = [["alice", "Alice", "", 268, 33.4, "main", "remove too"]]

    mutations = SummaryWorksheetContent.plan_delete(
        42,
        headers,
        rows,
        titles,
        "alice",
    )

    assert mutations == (DimensionMutation.delete_rows(42, start_row=2),)


def test_summary_delete_follows_moved_username() -> None:
    headers = [
        "encore_roles",
        "username",
        "display_name",
        "Main Team ISV",
        "Main Team Power",
        "original_message",
    ]
    rows = [["Encore", "alice", "Alice", 268, 33.4, "same"]]

    mutations = SummaryWorksheetContent.plan_delete(
        42,
        headers,
        rows,
        ["Main Team"],
        "alice",
    )

    assert mutations == (DimensionMutation.delete_rows(42, start_row=2),)


def test_summary_delete_follows_username_after_title_pair() -> None:
    headers = [
        "display_name",
        "Main Team ISV",
        "Main Team Power",
        "encore_roles",
        "username",
        "original_message",
    ]
    rows = [["Alice", 268, 33.4, "Encore", "alice", "same"]]

    mutations = SummaryWorksheetContent.plan_delete(
        42,
        headers,
        rows,
        ["Main Team"],
        "alice",
    )

    assert mutations == (DimensionMutation.delete_rows(42, start_row=2),)


def test_summary_header_migration_initializes_canonical_empty_sheet() -> None:
    titles = ["Main Team", "Encore Team"]

    mutations = SummaryWorksheetContent.plan_header_migration(42, [], [], titles)

    assert mutations == (
        GridValueUpdate.from_values(
            worksheet_id=42,
            start_row=1,
            start_column=1,
            values=[
                [
                    "username",
                    "display_name",
                    "encore_roles",
                    "Main Team ISV",
                    "Main Team Power",
                    "Encore Team ISV",
                    "Encore Team Power",
                    "original_message",
                ]
            ],
        ),
    )


def test_summary_header_migration_accepts_multiple_blank_bot_rows() -> None:
    mutations = SummaryWorksheetContent.plan_header_migration(
        42,
        [],
        [[], ["", None]],
        ["Main Team"],
    )

    assert len(mutations) == 1


def test_summary_header_migration_rejects_populated_headerless_bot_band() -> None:
    with pytest.raises(WorksheetContractError):
        SummaryWorksheetContent.plan_header_migration(
            42,
            [],
            [[], ["alice", "", "", "", "", ""]],
            ["Main Team"],
        )


@pytest.mark.parametrize("titles", [["Main Team", "Main Team"], [""]])
def test_summary_header_migration_rejects_invalid_configured_titles(
    titles: list[str],
) -> None:
    with pytest.raises(WorksheetContractError):
        SummaryWorksheetContent.plan_header_migration(42, [], [], titles)


def test_summary_header_migration_adds_missing_canonical_terminal() -> None:
    titles = ["Main Team"]
    headers = [
        "username",
        "display_name",
        "encore_roles",
        "Main Team ISV",
        "Main Team Power",
    ]

    mutations = SummaryWorksheetContent.plan_header_migration(
        42,
        headers,
        [],
        titles,
        column_count=len(headers) + 1,
    )

    assert mutations == (
        DimensionMutation.insert_columns(42, start_column=6),
        GridValueUpdate.from_values(
            worksheet_id=42,
            start_row=1,
            start_column=6,
            values=[["original_message"]],
        ),
    )


def test_summary_header_migration_appends_terminal_at_exact_grid_boundary() -> None:
    titles = ["Main Team"]
    headers = [
        "username",
        "display_name",
        "encore_roles",
        "Main Team ISV",
        "Main Team Power",
    ]

    mutations = SummaryWorksheetContent.plan_header_migration(
        42,
        headers,
        [],
        titles,
        column_count=len(headers),
    )

    assert mutations == (
        DimensionMutation.append_columns(42, count=1),
        GridValueUpdate.from_values(
            worksheet_id=42,
            start_row=1,
            start_column=6,
            values=[["original_message"]],
        ),
    )


def test_summary_header_migration_preserves_moved_base_headers() -> None:
    headers = [
        "encore_roles",
        "username",
        "display_name",
        "Main Team ISV",
        "Main Team Power",
        "original_message",
        "manager_note",
    ]

    assert (
        SummaryWorksheetContent.plan_header_migration(
            42,
            headers,
            [],
            ["Main Team"],
        )
        == ()
    )


def test_summary_header_migration_preserves_interleaved_required_headers() -> None:
    headers = [
        "Main Team ISV",
        "display_name",
        "Encore Team Power",
        "username",
        "Main Team Power",
        "encore_roles",
        "Encore Team ISV",
        "original_message",
        "manager_note",
    ]

    assert (
        SummaryWorksheetContent.plan_header_migration(
            42,
            headers,
            [],
            ["Main Team", "Encore Team"],
        )
        == ()
    )


def test_summary_header_migration_inserts_new_pair_before_admin_band() -> None:
    headers = [
        "username",
        "display_name",
        "encore_roles",
        "Main Team ISV",
        "Main Team Power",
        "original_message",
        "manager_note",
    ]

    mutations = SummaryWorksheetContent.plan_header_migration(
        42,
        headers,
        [],
        ["Main Team", "Encore Team"],
    )

    assert mutations == (
        DimensionMutation.insert_columns(42, start_column=6, count=2),
        GridValueUpdate.from_values(
            worksheet_id=42,
            start_row=1,
            start_column=6,
            values=[["Encore Team ISV", "Encore Team Power"]],
        ),
    )


def test_summary_header_migration_deletes_obsolete_pair_structurally() -> None:
    headers = [
        "username",
        "display_name",
        "encore_roles",
        "Backup Team ISV",
        "Backup Team Power",
        "Main Team ISV",
        "Main Team Power",
        "Encore Team ISV",
        "Encore Team Power",
        "original_message",
        "manager_note",
    ]

    mutations = SummaryWorksheetContent.plan_header_migration(
        42,
        headers,
        [],
        ["Main Team", "Backup Team"],
    )

    assert mutations == (DimensionMutation.delete_columns(42, start_column=8, count=2),)


def test_summary_header_migration_replaces_changed_title_pair() -> None:
    headers = [
        "username",
        "display_name",
        "encore_roles",
        "Main Team ISV",
        "Main Team Power",
        "Old Encore ISV",
        "Old Encore Power",
        "original_message",
        "manager_note",
    ]

    mutations = SummaryWorksheetContent.plan_header_migration(
        42,
        headers,
        [],
        ["Main Team", "Encore Team"],
    )

    assert mutations == (
        DimensionMutation.delete_columns(42, start_column=6, count=2),
        DimensionMutation.insert_columns(42, start_column=6, count=2),
        GridValueUpdate.from_values(
            worksheet_id=42,
            start_row=1,
            start_column=6,
            values=[["Encore Team ISV", "Encore Team Power"]],
        ),
    )


def test_summary_header_migration_replaces_nonadjacent_obsolete_headers() -> None:
    headers = [
        "Old Team ISV",
        "Main Team Power",
        "username",
        "Old Team Power",
        "display_name",
        "Main Team ISV",
        "encore_roles",
        "original_message",
        "manager_note",
    ]

    mutations = SummaryWorksheetContent.plan_header_migration(
        42,
        headers,
        [],
        ["Main Team", "Encore Team"],
    )

    assert mutations == (
        DimensionMutation.delete_columns(42, start_column=4),
        DimensionMutation.delete_columns(42, start_column=1),
        DimensionMutation.insert_columns(42, start_column=6, count=2),
        GridValueUpdate.from_values(
            worksheet_id=42,
            start_row=1,
            start_column=6,
            values=[["Encore Team ISV", "Encore Team Power"]],
        ),
    )


def test_summary_header_migration_repairs_exact_duplicate_terminal_incident() -> None:
    headers = [
        "username",
        "display_name",
        "encore_roles",
        "Main Team ISV",
        "Main Team Power",
        "Encore Team ISV",
        "Encore Team Power",
        "original_message",
        "Backup Team ISV",
        "Backup Team Power",
        "original_message",
        "manager_note",
    ]

    mutations = SummaryWorksheetContent.plan_header_migration(
        42,
        headers,
        [],
        ["Main Team", "Encore Team"],
    )

    assert mutations == (DimensionMutation.delete_columns(42, start_column=9, count=3),)


def test_summary_header_migration_composes_duplicate_repair_with_pair_changes() -> None:
    headers = [
        "username",
        "display_name",
        "encore_roles",
        "Main Team ISV",
        "Main Team Power",
        "Old Encore ISV",
        "Old Encore Power",
        "original_message",
        "Retired Team ISV",
        "Retired Team Power",
        "original_message",
        "manager_note",
    ]
    before = headers.copy()

    mutations = SummaryWorksheetContent.plan_header_migration(
        42,
        headers,
        [],
        ["Main Team", "Encore Team", "Backup Team"],
    )

    assert mutations == (
        DimensionMutation.delete_columns(42, start_column=9, count=3),
        DimensionMutation.delete_columns(42, start_column=6, count=2),
        DimensionMutation.insert_columns(42, start_column=6, count=4),
        GridValueUpdate.from_values(
            worksheet_id=42,
            start_row=1,
            start_column=6,
            values=[
                [
                    "Encore Team ISV",
                    "Encore Team Power",
                    "Backup Team ISV",
                    "Backup Team Power",
                ]
            ],
        ),
    )
    assert headers == before


def test_summary_duplicate_terminal_composition_rejects_stale_desired_title() -> None:
    headers = [
        "username",
        "display_name",
        "encore_roles",
        "Main Team ISV",
        "Main Team Power",
        "Old Encore ISV",
        "Old Encore Power",
        "original_message",
        "Backup Team ISV",
        "Backup Team Power",
        "original_message",
    ]

    with pytest.raises(WorksheetContractError):
        SummaryWorksheetContent.plan_header_migration(
            42,
            headers,
            [],
            ["Main Team", "Encore Team", "Backup Team"],
        )


def test_summary_header_migration_rejects_reserved_admin_header() -> None:
    headers = [
        "username",
        "display_name",
        "encore_roles",
        "Main Team ISV",
        "Main Team Power",
        "original_message",
        "Main Team ISV",
    ]

    with pytest.raises(WorksheetContractError):
        SummaryWorksheetContent.plan_header_migration(42, headers, [], ["Main Team"])


def test_summary_worksheet_index_rejects_reserved_admin_header() -> None:
    headers = [
        "username",
        "display_name",
        "encore_roles",
        "Main Team ISV",
        "Main Team Power",
        "original_message",
        "Old Team Power",
    ]

    with pytest.raises(WorksheetContractError):
        SummaryWorksheetContent.index_physical_rows(headers, [], ["Main Team"])


@pytest.mark.parametrize(
    "headers",
    [
        [
            "username",
            "display_name",
            "encore_roles",
            "Encore Team ISV",
            "Encore Team Power",
            "Main Team ISV",
            "Main Team Power",
            "original_message",
            "Backup Team ISV",
            "Backup Team Power",
            "original_message",
        ],
        [
            "username",
            "display_name",
            "encore_roles",
            "Main Team ISV",
            "Main Team Power",
            "Encore Team ISV",
            "Encore Team Power",
            "original_message",
            "Backup Team ISV",
            "Backup Team Power",
            "original_message",
            "original_message",
        ],
        [
            "username",
            "display_name",
            "encore_roles",
            "Main Team ISV",
            "Main Team Power",
            "Encore Team ISV",
            "Encore Team Power",
            "original_message",
            "Backup Team ISV",
            "original_message",
        ],
        [
            "username",
            "display_name",
            "encore_roles",
            "Main Team ISV",
            "Main Team Power",
            "Encore Team ISV",
            "Encore Team Power",
            "original_message",
            "Backup Team ISV",
            "Backup Team Power",
            "original_message",
            "Main Team Power",
        ],
        [
            "username",
            "display_name",
            "encore_roles",
            "Main Team ISV",
            "Main Team Power",
            "Encore Team ISV",
            "Encore Team Power",
            "original_message",
            "legacy",
            "custom",
            "original_message",
        ],
    ],
    ids=[
        "reordered-prefix",
        "third-marker",
        "incomplete-pair",
        "reserved-admin-collision",
        "unrecognized-columns",
    ],
)
def test_summary_duplicate_terminal_near_misses_fail_closed(
    headers: list[str],
) -> None:
    before = headers.copy()

    with pytest.raises(WorksheetContractError):
        SummaryWorksheetContent.plan_header_migration(
            42,
            headers,
            [],
            ["Main Team", "Encore Team"],
        )

    assert headers == before


def test_summary_reconciliation_validates_every_consumed_team_row() -> None:
    summary_headers = [
        *SummaryWorksheetContent.COLUMNS,
        "Main Team ISV",
        "Main Team Power",
        "original_message",
    ]
    team_rows = [
        ["alice", "Alice", 150, 740, 33.4, "main"],
        ["bob", "Bob", "not-int", 680, 35.3, "bad"],
    ]

    with pytest.raises(WorksheetContractError):
        SummaryWorksheetContent.plan_reconciliation(
            worksheet_id=42,
            headers=summary_headers,
            rows=[],
            team_worksheets={"Main Team": (TeamWorksheetContent.COLUMNS, team_rows)},
            users={},
        )


def test_summary_reconciliation_updates_reuses_and_deletes_physical_rows() -> None:
    summary_headers = [
        *SummaryWorksheetContent.COLUMNS,
        "Encore Team ISV",
        "Encore Team Power",
        "Main Team ISV",
        "Main Team Power",
        "original_message",
        "manager_note",
    ]
    summary_rows = [
        ["carol", "Carol", "", 0, 0, 0, 0, "old", "delete too"],
        ["alice", "Old", "", 0, 0, 0, 0, "old", "preserve"],
        ["", "occupied", "", "", "", "", "", "", "preserve"],
        ["", "", "", "", "", "", "", "", "prepared"],
    ]
    main_rows = [
        ["alice", "Alice", 150, 740, 33.4, "same"],
        ["bob", "Bob", 130, 600, 30.0, "bob main"],
    ]
    encore_rows = [["alice", "Alice", 140, 680, 35.3, "same"]]

    mutations = SummaryWorksheetContent.plan_reconciliation(
        worksheet_id=42,
        headers=summary_headers,
        rows=summary_rows,
        team_worksheets={
            "Main Team": (TeamWorksheetContent.COLUMNS, main_rows),
            "Encore Team": (TeamWorksheetContent.COLUMNS, encore_rows),
        },
        users={
            "alice": UserInfoWithEncoreRoles("alice", "Alice New", "Lead"),
            "bob": UserInfoWithEncoreRoles("bob", "Bob", ""),
        },
    )

    assert mutations == (
        GridValueUpdate.from_values(
            worksheet_id=42,
            start_row=3,
            start_column=1,
            values=[
                [
                    "alice",
                    "Alice New",
                    "Lead",
                    248,
                    35.3,
                    268,
                    33.4,
                    f"same{ORIGINAL_MESSAGE_LINE_SEPARATOR}same",
                ]
            ],
        ),
        GridValueUpdate.from_values(
            worksheet_id=42,
            start_row=5,
            start_column=1,
            values=[["bob", "Bob", "", "", "", 224, 30.0, "bob main"]],
        ),
        DimensionMutation.delete_rows(42, start_row=2),
    )


def test_summary_reconciliation_follows_moved_team_and_summary_usernames() -> None:
    summary_headers = [
        "encore_roles",
        "username",
        "display_name",
        "Main Team ISV",
        "Main Team Power",
        "original_message",
    ]
    team_headers = [
        "display_name",
        "username",
        "leader_skill_value",
        "internal_skill_value",
        "team_power",
        "original_message",
    ]
    summary_rows = [["Old", "alice", "Old Alice", 1, 1, "same"]]
    team_rows = [
        ["Alice", "alice", 150, 740, 33.4, "same"],
        ["Bob", "bob", 140, 680, 35.3, "same"],
    ]

    mutations = SummaryWorksheetContent.plan_reconciliation(
        worksheet_id=42,
        headers=summary_headers,
        rows=summary_rows,
        team_worksheets={"Main Team": (team_headers, team_rows)},
        users={"alice": UserInfoWithEncoreRoles("alice", "Alice", "Encore")},
    )

    assert [mutation.rows[0] for mutation in mutations] == [
        ("Encore", "alice", "Alice", 268, 33.4, "same"),
        ("", "bob", "Bob", 248, 35.3, "same"),
    ]


def test_summary_reconciliation_follows_interleaved_required_headers() -> None:
    summary_headers = [
        "Main Team ISV",
        "display_name",
        "Encore Team Power",
        "username",
        "Main Team Power",
        "encore_roles",
        "Encore Team ISV",
        "original_message",
    ]
    summary_rows = [[1, "Old Alice", 1, "alice", 1, "Old", 1, "old"]]
    main_rows = [
        ["alice", "Alice", 150, 740, 33.4, "same"],
        ["bob", "Bob", 130, 600, 30.0, "bob main"],
    ]
    encore_rows = [["alice", "Alice", 140, 680, 35.3, "same"]]

    mutations = SummaryWorksheetContent.plan_reconciliation(
        worksheet_id=42,
        headers=summary_headers,
        rows=summary_rows,
        team_worksheets={
            "Main Team": (TeamWorksheetContent.COLUMNS, main_rows),
            "Encore Team": (TeamWorksheetContent.COLUMNS, encore_rows),
        },
        users={"alice": UserInfoWithEncoreRoles("alice", "Alice", "Encore")},
    )

    assert [mutation.rows[0] for mutation in mutations] == [
        (
            268,
            "Alice",
            35.3,
            "alice",
            33.4,
            "Encore",
            248,
            f"same{ORIGINAL_MESSAGE_LINE_SEPARATOR}same",
        ),
        (224, "Bob", "", "bob", 30.0, "", "", "bob main"),
    ]


@pytest.mark.parametrize(
    "headers",
    [
        [
            "username",
            "display_name",
            "leader_skill_value",
            "internal_skill_value",
            "original_message",
        ],
        [*TeamWorksheetContent.COLUMNS, "team_power"],
        [
            "username",
            "display_name",
            "manager_note",
            "leader_skill_value",
            "internal_skill_value",
            "team_power",
            "original_message",
        ],
    ],
    ids=[
        "missing-header",
        "reserved-admin-collision",
        "admin-inside-bot-band",
    ],
)
def test_team_worksheet_index_rejects_ambiguous_bot_ownership(
    headers: list[str],
) -> None:
    with pytest.raises(WorksheetContractError):
        TeamWorksheetContent.index_physical_rows(headers, [])


def test_team_header_migration_rejects_positional_legacy_names() -> None:
    headers = [
        "username",
        "display_name",
        "Old Leader",
        "Old Internal",
        "Old Power",
        "original_message",
    ]

    with pytest.raises(WorksheetContractError):
        TeamWorksheetContent.plan_header_migration(42, headers, [])


def test_team_upsert_does_not_parse_unrelated_keyed_rows() -> None:
    rows = [
        ["alice", "Old Alice", 0, 0, 0, "same"],
        ["bob", "Bob", "not-int", "bad", "bad", "same"],
    ]
    team = TeamParser.parse_line(
        UserInfo(username="alice", display_name="Alice"),
        "150/740/33.4 main",
    )

    (update,) = TeamWorksheetContent.plan_upsert(
        42,
        TeamWorksheetContent.COLUMNS,
        rows,
        team,
    )

    assert update.start_row_index == 1


def test_team_full_consumption_skips_occupied_blank_key_row() -> None:
    rows = [["", "orphan", "not-int", "bad", "bad", "preserve"]]

    assert (
        TeamWorksheetContent.validated_teams(
            TeamWorksheetContent.COLUMNS,
            rows,
        )
        == ()
    )


def test_repeated_original_message_cells_do_not_create_duplicate_records() -> None:
    rows = [
        ["alice", "Alice", 150, 740, 33.4, "same"],
        ["bob", "Bob", 140, 680, 35.3, "same"],
    ]

    index = TeamWorksheetContent.index_physical_rows(
        TeamWorksheetContent.COLUMNS,
        rows,
    )

    assert index.row_by_username == {"alice": 2, "bob": 3}


def test_shift_entry_layout_places_team_before_0_30_hour_axis() -> None:
    expected_columns = [
        "username",
        "display_name",
        "Main ISV",
        "Encore ISV",
        "Team Info",
        *[f"{hour}-{hour + 1}" for hour in range(30)],
        "original_message",
    ]

    assert expected_columns == EntryWorksheetContent.COLUMNS
    assert EntryWorksheetContent.COLUMN_COUNT == 36


def test_shift_entry_count_row_targets_f_through_ai() -> None:
    row = EntryWorksheetContent.count_row()

    assert row[0] == "count"
    assert row[1:5] == ["", "", "", ""]
    assert row[5] == "=COUNTIF(F$3:F, 1)"
    assert row[34] == "=COUNTIF(AI$3:AI, 1)"
    assert row[35] == ""


def test_shift_entry_serializes_only_owned_value_ranges() -> None:
    shift = Shift(
        username="alice",
        display_name="Alice",
        original_message="0-2",
        slots={0, 1},
    )

    updates = EntryWorksheetContent.shift_value_ranges(shift, row=7)

    assert updates == [
        {"range": "A7:B7", "values": [["alice", "Alice"]]},
        {
            "range": "F7:AJ7",
            "values": [[1, 1, *([0] * 28), "0-2"]],
        },
    ]


def test_team_formula_uses_pipe_and_row_reference() -> None:
    formula = shift_register_structs.build_team_summary_formula(
        row=7,
        sheet_url="https://docs.google.com/spreadsheets/d/source",
        worksheet_title="Team Summary",
        username_column=1,
        roles_column=3,
        main_isv_column=4,
        encore_isv_column=6,
        import_last_column="G",
    )

    assert "$A7" in formula
    assert "found, COUNTIF(username, $A7) > 0" in formula
    assert '"No team yet"' in formula
    assert '"｜Main fallback"' in formula  # noqa: RUF001
    assert '"No role"' in formula
    assert "Encore Team" not in formula
    assert formula.startswith("=LET(")


def test_team_formula_without_encore_column_uses_blank() -> None:
    formula = shift_register_structs.build_team_summary_formula(
        row=3,
        sheet_url="https://docs.google.com/spreadsheets/d/source",
        worksheet_title="Only Main",
        username_column=1,
        roles_column=3,
        main_isv_column=4,
        encore_isv_column=None,
        import_last_column="E",
    )

    assert 'encoreTeam, ""' in formula


def test_team_formula_escapes_formula_strings_and_sheet_title() -> None:
    formula = shift_register_structs.build_team_summary_formula(
        row=3,
        sheet_url='https://sheet.example/a"b',
        worksheet_title='Manager\'s "Summary"',
        username_column=1,
        roles_column=3,
        main_isv_column=4,
        encore_isv_column=6,
        import_last_column="G",
    )

    assert 'IMPORTRANGE("https://sheet.example/a""b"' in formula
    assert '"\'Manager\'\'s ""Summary""\'!A:G"' in formula


def test_shift_entry_dtypes_use_0_30_hour_axis() -> None:
    expected_hour_columns = [f"{hour}-{hour + 1}" for hour in range(30)]

    assert {
        column: EntryWorksheetContent.DTYPES[column] for column in expected_hour_columns
    } == dict.fromkeys(expected_hour_columns, "int")
