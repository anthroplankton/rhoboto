from __future__ import annotations

import pandas as pd
import pytest

from utils.structs_base import ORIGINAL_MESSAGE_LINE_SEPARATOR, UserInfo
from utils.team_register_structs import (
    Summary,
    SummaryWorksheetContent,
    TeamFormatError,
    TeamParser,
    TeamWorksheetContent,
)


def make_user(username: str = "alice", display_name: str = "Alice") -> UserInfo:
    return UserInfo(username=username, display_name=display_name)


def test_team_parser_extracts_embedded_team_values() -> None:
    team = TeamParser.parse_line(make_user(), "main: 150 / 740 / 33.4 note")

    assert team.username == "alice"
    assert team.display_name == "Alice"
    assert team.leader_skill_value == 150
    assert team.internal_skill_value == 740
    assert team.team_power == 33.4
    assert team.original_message == "main: 150 / 740 / 33.4 note"
    assert team.effective_skill_value == 268
    assert not hasattr(team.team, "original_message")


def test_team_parser_ignores_invalid_lines_and_raises_for_single_invalid() -> None:
    result = TeamParser.parse_submission(
        make_user(),
        [
            "not a team",
            "150/740/33.4 first",
            "label 140 / 680 / .5 trailing",
        ],
    )

    assert [team.team_power for team in result.teams] == [33.4, 0.5]
    with pytest.raises(TeamFormatError):
        TeamParser.parse_line(make_user(), "missing separators")


def test_team_parser_accepts_full_width_slashes() -> None:
    team = TeamParser.parse_line(
        make_user(),
        "main: 150／740／33.4 note",  # noqa: RUF001
    )

    assert team.leader_skill_value == 150
    assert team.internal_skill_value == 740
    assert team.team_power == 33.4


def test_team_parser_accepts_nfkc_compatible_submission_text() -> None:
    line = "main: １５０／７４０／３３．４ note"  # noqa: RUF001

    team = TeamParser.parse_line(make_user(), line)

    assert team.leader_skill_value == 150
    assert team.internal_skill_value == 740
    assert team.team_power == 33.4
    assert team.original_message == line


@pytest.mark.parametrize(
    "line",
    [
        "160//600/33",
        "160,600,33",
        "160 600 33",
        "１６０，６００，３３",  # noqa: RUF001
    ],
)
def test_team_parser_reports_invalid_attempt_by_numeric_tokens(line: str) -> None:
    result = TeamParser.parse_submission(make_user(), [line])

    assert result.teams == []
    assert result.invalid_attempts == [line]


@pytest.mark.parametrize("line", ["公告", "160/600"])
def test_team_parser_does_not_flag_general_text_as_invalid_attempt(line: str) -> None:
    result = TeamParser.parse_submission(make_user(), [line])

    assert result.teams == []
    assert result.invalid_attempts == []


def test_team_parser_parse_submission_accepts_valid_with_ordinary_text() -> None:
    result = TeamParser.parse_submission(
        make_user(),
        ["main team", "150/740/33.4", "よろしく"],
    )

    assert [team.team_power for team in result.teams] == [33.4]
    assert result.submission == result.teams
    assert result.teams[0].original_message == "main team ⏎  150/740/33.4 ⏎  よろしく"
    assert result.invalid_attempts == []


def test_team_parser_parse_submission_reports_strict_mixed_invalid_attempts() -> None:
    result = TeamParser.parse_submission(
        make_user(),
        ["150/740/33.4", "160//600/33"],
    )

    assert [team.team_power for team in result.teams] == [33.4]
    assert result.teams[0].original_message == "150/740/33.4 ⏎  160//600/33"
    assert result.invalid_attempts == ["160//600/33"]


def test_team_parser_parse_submission_treats_ordinary_text_as_noop() -> None:
    result = TeamParser.parse_submission(make_user(), ["公告"])

    assert result.teams == []
    assert result.invalid_attempts == []


def test_team_parser_parse_submission_assigns_text_to_team_blocks() -> None:
    result = TeamParser.parse_submission(
        make_user(),
        [
            "ordinary-0",
            "100/100/20.0 first valid line becomes main",
            "ordinary-1",
            "150/700/39.0 second valid line becomes encore",
            "ordinary-2",
            "140/680/35.3 third valid line becomes backup",
            "ordinary-3",
        ],
    )

    assert [team.original_message for team in result.teams] == [
        "ordinary-0 ⏎  100/100/20.0 first valid line becomes main ⏎  ordinary-1",
        "150/700/39.0 second valid line becomes encore ⏎  ordinary-2",
        "140/680/35.3 third valid line becomes backup ⏎  ordinary-3",
    ]
    assert result.invalid_attempts == []


def test_team_parser_parse_submission_strips_lines() -> None:
    result = TeamParser.parse_submission(
        make_user(),
        [
            "",
            "  ",
            "  100/100/20.0 main  ",
            "\t",
            "  note  ",
        ],
    )

    assert [team.original_message for team in result.teams] == [
        "100/100/20.0 main ⏎  note",
    ]


def test_team_classification_uses_valid_submission_order() -> None:
    result = TeamParser.parse_submission(
        make_user(),
        [
            "announcement text",
            "100/100/20.0 first valid line becomes main",
            "not a team",
            "150/700/39.0 second valid line becomes encore",
            "140/680/35.3 third valid line becomes backup",
        ],
    )

    classified = TeamParser.classify_teams(result.teams)

    assert "first valid line becomes main" in classified.main.original_message
    assert classified.encore is not None
    assert "second valid line becomes encore" in classified.encore.original_message
    assert [team.original_message for team in classified.backup] == [
        "140/680/35.3 third valid line becomes backup"
    ]
    assert classified.as_tuple() == (
        classified.main,
        classified.encore,
        *classified.backup,
    )


def test_team_classification_handles_one_team_as_main_only() -> None:
    result = TeamParser.parse_submission(make_user(), ["100/100/20.0 only team"])

    classified = TeamParser.classify_teams(result.teams)

    assert classified.main.original_message == "100/100/20.0 only team"
    assert classified.encore is None
    assert classified.backup == []
    assert classified.as_tuple() == (classified.main, None)


def test_team_classification_handles_two_teams_without_backup() -> None:
    result = TeamParser.parse_submission(
        make_user(),
        [
            "100/100/20.0 first valid line becomes main",
            "150/700/39.0 second valid line becomes encore",
        ],
    )

    classified = TeamParser.classify_teams(result.teams)

    assert classified.main.original_message.endswith("first valid line becomes main")
    assert classified.encore is not None
    assert classified.encore.original_message.endswith(
        "second valid line becomes encore"
    )
    assert classified.backup == []
    assert classified.as_tuple() == (classified.main, classified.encore)


def test_summary_combines_original_messages_from_existing_teams() -> None:
    user = make_user()
    main = TeamParser.parse_line(user, "100/100/20.0 main")
    encore = TeamParser.parse_line(user, "150/700/39.0 encore")
    backup = TeamParser.parse_line(user, "140/680/35.3 backup")
    backup.original_message = ""

    summary = Summary(
        username=user.username,
        display_name=user.display_name,
        encore_roles="",
        titles=["Main Team", "Encore Team", "Backup Team", "Team 4"],
        teams=[main, None, encore, backup],
    )

    assert summary.original_message == ORIGINAL_MESSAGE_LINE_SEPARATOR.join(
        [
            "100/100/20.0 main",
            "150/700/39.0 encore",
        ]
    )


def test_summary_generates_dynamic_team_columns_from_team_dataframes() -> None:
    user = make_user()
    main = TeamParser.parse_line(user, "150/740/33.4 main")
    encore = TeamParser.parse_line(user, "140/680/35.3 encore")
    backup = TeamParser.parse_line(user, "130/600/30.0 backup")
    backup.original_message = ""

    main_content = TeamWorksheetContent()
    encore_content = TeamWorksheetContent()
    backup_content = TeamWorksheetContent()
    legacy_content = TeamWorksheetContent()
    main_content.upsert(main)
    encore_content.upsert(encore)
    backup_content.upsert(backup)
    legacy_content.upsert(TeamParser.parse_line(user, "120/500/25.0 legacy"))
    legacy_df = legacy_content.main.drop(columns=["original_message"])

    summary = SummaryWorksheetContent.generate_from_team_dataframes(
        {
            "Main Team": main_content.main,
            "Encore Team": encore_content.main,
            "Backup Team": backup_content.main,
            "Team 4": legacy_df,
        }
    )

    assert list(summary.main.index) == ["alice"]
    assert summary.main.loc["alice", "display_name"] == "Alice"
    assert summary.main.loc["alice", "encore_roles"] == ""
    assert summary.main.loc["alice", Summary.isv_title("Main Team")] == 268
    assert summary.main.loc["alice", Summary.power_title("Main Team")] == 33.4
    assert summary.main.loc["alice", "original_message"] == (
        f"150/740/33.4 main{ORIGINAL_MESSAGE_LINE_SEPARATOR}140/680/35.3 encore"
    )
    assert list(summary.to_frame().columns)[-1] == "original_message"


def test_summary_generation_handles_no_team_dataframes() -> None:
    summary = SummaryWorksheetContent.generate_from_team_dataframes({})

    assert isinstance(summary.main, pd.DataFrame)
    assert summary.main.empty
    assert list(summary.to_frame().columns) == [
        "username",
        "display_name",
        "encore_roles",
    ]
