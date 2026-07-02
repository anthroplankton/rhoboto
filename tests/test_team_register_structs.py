from __future__ import annotations

import pandas as pd
import pytest

from utils.structs_base import UserInfo
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


def test_team_parser_ignores_invalid_lines_and_raises_for_single_invalid() -> None:
    teams = TeamParser.parse_lines(
        make_user(),
        [
            "not a team",
            "150/740/33.4 first",
            "label 140 / 680 / .5 trailing",
        ],
    )

    assert [team.team_power for team in teams] == [33.4, 0.5]
    with pytest.raises(TeamFormatError):
        TeamParser.parse_line(make_user(), "missing separators")


def test_team_parser_accepts_full_width_slashes() -> None:
    team = TeamParser.parse_line(make_user(), "main: 150\uff0f740\uff0f33.4 note")

    assert team.leader_skill_value == 150
    assert team.internal_skill_value == 740
    assert team.team_power == 33.4


def test_team_parser_detects_invalid_attempt_by_numeric_tokens() -> None:
    assert TeamParser.looks_like_invalid_attempt(["160//600/33"])
    assert TeamParser.looks_like_invalid_attempt(["160,600,33"])
    assert TeamParser.looks_like_invalid_attempt(["160 600 33"])


def test_team_parser_does_not_flag_general_text_as_invalid_attempt() -> None:
    assert not TeamParser.looks_like_invalid_attempt(["公告"])
    assert not TeamParser.looks_like_invalid_attempt(["160/600"])


def test_team_classification_uses_effective_value_and_power_rules() -> None:
    teams = TeamParser.parse_lines(
        make_user(),
        [
            "150/740/33.4 main by effective value",
            "140/680/35.3 encore by highest power",
            "130/650/34.0 backup",
        ],
    )

    classified = TeamParser.classify_teams(teams)

    assert classified.main.original_message.endswith("main by effective value")
    assert classified.encore is not None
    assert classified.encore.original_message.endswith("encore by highest power")
    assert [team.original_message for team in classified.backup] == [
        "130/650/34.0 backup"
    ]
    assert classified.as_tuple() == (
        classified.main,
        classified.encore,
        *classified.backup,
    )


def test_team_classification_keeps_weaker_power_candidate_as_backup() -> None:
    teams = TeamParser.parse_lines(
        make_user(),
        [
            "150/740/40.0 main",
            "140/680/35.3 backup one",
            "130/650/34.0 backup two",
        ],
    )

    classified = TeamParser.classify_teams(teams)

    assert classified.encore is None
    assert [team.original_message for team in classified.backup] == [
        "140/680/35.3 backup one",
        "130/650/34.0 backup two",
    ]


def test_summary_generates_dynamic_team_columns_from_team_dataframes() -> None:
    user = make_user()
    team = TeamParser.parse_line(user, "150/740/33.4 main")
    content = TeamWorksheetContent()
    content.upsert(team)

    summary = SummaryWorksheetContent.generate_from_team_dataframes(
        {"Main Team": content.main}
    )

    assert list(summary.main.index) == ["alice"]
    assert summary.main.loc["alice", "display_name"] == "Alice"
    assert summary.main.loc["alice", "encore_roles"] == ""
    assert summary.main.loc["alice", Summary.isv_title("Main Team")] == 268
    assert summary.main.loc["alice", Summary.power_title("Main Team")] == 33.4


def test_summary_generation_handles_no_team_dataframes() -> None:
    summary = SummaryWorksheetContent.generate_from_team_dataframes({})

    assert isinstance(summary.main, pd.DataFrame)
    assert summary.main.empty
    assert list(summary.main.columns) == ["display_name", "encore_roles"]
