from __future__ import annotations

import dataclasses
import itertools as it
import re
import unicodedata
from dataclasses import InitVar, dataclass, field
from typing import TYPE_CHECKING, ClassVar, Self, override

import pandas as pd

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
    from collections.abc import Generator


@dataclass
class UserInfoWithEncoreRoles(UserInfo):
    encore_roles: str

    """
    User information with encore roles.

    Attributes:
        encore_roles (str): Comma-separated list of encore roles.
            Defaults to an empty string if no roles are assigned.
    """

    @classmethod
    def roles_to_string(cls, roles: list[str]) -> str:
        """
        Convert a list of roles to a comma-separated string.

        Args:
            roles (list[str]): List of roles to convert.

        Returns:
            str: Comma-separated string of roles.
        """
        return ", ".join(roles) if roles else ""


@dataclass
class TeamInfo:
    leader_skill_value: int
    internal_skill_value: int
    team_power: float

    """
    Team skill values and related information.

    Attributes:
        leader_skill_value (int): Leader skill value.
        internal_skill_value (int): Internal skill value.
        team_power (float): Team power value.
    """

    def __repr__(self) -> str:
        return (
            f"TeamInfo("
            f"{self.leader_skill_value}/"
            f"{self.internal_skill_value}/"
            f"{self.team_power}"
            f")"
        )


@dataclass
class Team(OriginalMessage, TeamInfo, UserInfo):
    """
    Represents a team with user information and team skill values.

    Attributes:
        user (UserInfo): The user information associated with the team.
        team (TeamInfo): The team skill values and other related information.
    """

    @property
    def user(self) -> UserInfo:
        """
        Get the user information.

        Returns:
            UserInfo: The user information associated with the team.
        """
        return UserInfo(
            username=self.username,
            display_name=self.display_name,
        )

    @property
    def team(self) -> TeamInfo:
        """
        Get the team information.

        Returns:
            TeamInfo: The team skill values and related information.
        """
        return TeamInfo(
            leader_skill_value=self.leader_skill_value,
            internal_skill_value=self.internal_skill_value,
            team_power=self.team_power,
        )

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({self.user}, {self.team})"

    def __lt__(self, other: Team) -> bool:
        return self.effective_skill_value < other.effective_skill_value

    def __le__(self, other: Team) -> bool:
        return self.effective_skill_value <= other.effective_skill_value

    def __gt__(self, other: Team) -> bool:
        return self.effective_skill_value > other.effective_skill_value

    def __ge__(self, other: Team) -> bool:
        return self.effective_skill_value >= other.effective_skill_value

    @property
    def effective_skill_value(self) -> float:
        """
        Calculate the effective skill value of the team.

        Formula:
            leader_skill_value + (internal_skill_value - leader_skill_value) / 5

        Returns:
            float: The effective skill value.
        """
        return Team.compute_effective_skill_value(
            self.leader_skill_value,
            self.internal_skill_value,
        )

    @classmethod
    def compute_effective_skill_value(
        cls, leader_skill: float, internal_skill: float
    ) -> float:
        return leader_skill + (internal_skill - leader_skill) / 5


@dataclass
class ClassifiedTeams:
    main: Team
    encore: Team | None = None
    backup: list[Team] = field(default_factory=list)

    def __repr__(self) -> str:
        if not self.encore and not self.backup:
            return f"(Main: {self.main})"
        if not self.encore:
            return f"(Main: {self.main}, Backup: {self.backup})"
        if not self.backup:
            return f"(Main: {self.main}, Encore: {self.encore})"
        return f"(Main: {self.main}, Encore: {self.encore}, Backup: {self.backup})"

    def __len__(self) -> int:
        return 2 + len(self.backup)

    def as_tuple(self) -> tuple[Team, Team | None, *tuple[Team, ...]]:
        return self.main, self.encore, *self.backup


class TeamFormatError(Exception):
    def __init__(self, line: str) -> None:
        """
        Exception raised for invalid team format.

        Args:
            line (str): The line that failed to parse.
        """
        msg = f"Invalid team format: {line}"
        super().__init__(msg)


@dataclass(frozen=True)
class TeamParseResult(SubmissionParseResult[list[Team]]):
    @property
    def teams(self) -> list[Team]:
        return self.submission or []


class TeamParser:
    """Parser for team info lines."""

    PATTERN: ClassVar[re.Pattern[str]] = re.compile(
        r"(?P<leader_skill>[0-9]+)\s*/\s*"
        r"(?P<total_skill>[0-9]+)\s*/\s*"
        r"(?P<team_power>([0-9]+(\.[0-9]*)?|\.[0-9]+))"
    )
    NUMBER_TOKEN_PATTERN: ClassVar[re.Pattern[str]] = re.compile(
        r"[0-9]+(?:\.[0-9]*)?|\.[0-9]+"
    )
    MIN_INVALID_ATTEMPT_NUMBER_TOKENS: ClassVar[int] = 3

    @classmethod
    def _from_match(
        cls,
        user_info: UserInfo,
        match: re.Match[str],
        original_message: str,
    ) -> Team:
        leader_skill_value = int(match.group("leader_skill"))
        total_skill_value = int(match.group("total_skill"))
        team_power = float(match.group("team_power"))
        return Team(
            username=user_info.username,
            display_name=user_info.display_name,
            leader_skill_value=leader_skill_value,
            internal_skill_value=total_skill_value,
            team_power=team_power,
            original_message=original_message,
        )

    @classmethod
    def parse_line(cls, user_info: UserInfo, line: str) -> Team:
        """
        Parse a single line into a Team object.

        Args:
            user_info (UserInfo): The user information.
            line (str): Team info string to parse.

        Returns:
            Team: Parsed Team object.

        Raises:
            TeamFormatError: If the line does not match the expected format.
        """
        match = cls.PATTERN.search(unicodedata.normalize("NFKC", line))
        if not match:
            raise TeamFormatError(line)
        return cls._from_match(user_info, match, line.strip())

    @classmethod
    def parse_submission(
        cls,
        user_info: UserInfo,
        lines: list[str],
    ) -> TeamParseResult:
        """
        Parse a full message submission into teams and invalid attempts.

        Args:
            user_info (UserInfo): The user information.
            lines (list[str]): List of team info strings.

        Returns:
            TeamParseResult: Parsed teams and invalid team-like lines.
        """
        invalid_attempts: list[str] = []
        pending_lines: list[str] = []
        team_matches: list[re.Match[str]] = []
        message_blocks: list[list[str]] = []
        for line in lines:
            stripped_line = line.strip()
            if not stripped_line:
                continue
            normalized_line = unicodedata.normalize("NFKC", line)
            match = cls.PATTERN.search(normalized_line)
            if match:
                team_matches.append(match)
                message_blocks.append([*pending_lines, stripped_line])
                pending_lines = []
                continue
            if (
                len(cls.NUMBER_TOKEN_PATTERN.findall(normalized_line))
                >= cls.MIN_INVALID_ATTEMPT_NUMBER_TOKENS
            ):
                invalid_attempts.append(stripped_line)
            target = message_blocks[-1] if message_blocks else pending_lines
            target.append(stripped_line)
        teams = [
            cls._from_match(
                user_info,
                match,
                ORIGINAL_MESSAGE_LINE_SEPARATOR.join(block),
            )
            for match, block in zip(team_matches, message_blocks, strict=True)
        ]
        return TeamParseResult(
            submission=teams or None,
            invalid_attempts=invalid_attempts,
        )

    @classmethod
    def classify_teams(cls, teams: list[Team]) -> ClassifiedTeams:
        if not teams:
            msg = "Cannot classify an empty team list."
            raise ValueError(msg)

        main_team = teams[0]
        encore_team = teams[1] if len(teams) > 1 else None
        backup_teams = teams[2:]

        return ClassifiedTeams(main_team, encore_team, backup_teams)


@dataclass
class Summary(UserInfoWithEncoreRoles):
    """
    Represents a summary of user information with encore roles.

    Attributes:
        username (str): The username of the user.
        display_name (str): The display name of the user.
        encore_roles (str): Comma-separated list of encore roles.
    """

    titles: InitVar[list[str]]
    teams: InitVar[list[Team | None]]

    def __post_init__(self, titles: list[str], teams: list[Team | None]) -> None:
        self._summary: dict[str, float | str] = {}
        for (isv_title, power_title), team in it.zip_longest(
            self.isv_power_title_pairs(titles), teams
        ):
            self._summary[isv_title] = team.effective_skill_value if team else ""
            self._summary[power_title] = team.team_power if team else ""

    def __getattr__(self, name: str) -> float | str:
        if name in self._summary:
            return self._summary[name]
        msg = f"{name} not found in summary"
        raise AttributeError(msg)

    @classmethod
    def isv_title(cls, title: str) -> str:
        """
        Get the ISV title for a given team title.

        Args:
            title (str): The team title.

        Returns:
            str: The ISV title for the team.
        """
        return f"{title} ISV"

    @classmethod
    def power_title(cls, title: str) -> str:
        """
        Get the power title for a given team title.

        Args:
            title (str): The team title.

        Returns:
            str: The power title for the team.
        """
        return f"{title} Power"

    @classmethod
    def isv_power_title_pairs(cls, titles: list[str]) -> list[tuple[str, str]]:
        """
        Get the ISV and Power title pairs for a list of team titles.

        Args:
            titles (list[str]): The list of team titles.

        Returns:
            list[tuple[str, str]]:
                The list of (ISV title, Power title) pairs for the teams.
        """
        return [(cls.isv_title(title), cls.power_title(title)) for title in titles]


@dataclass
class TeamWorksheetMetadata(WorksheetMetadata):
    """
    Represents metadata for a team worksheet.

    Args:
        worksheet_id (int | None): The unique ID of the worksheet.
        title (str | None): The title of the worksheet.
        worksheet (AsyncioGspreadWorksheet | None):
            The worksheet object, or None if missing.

    Attributes:
        worksheet_id (int | None): The unique ID of the worksheet.
        title (str | None): The title of the worksheet.
        worksheet (AsyncioGspreadWorksheet | None):
            The worksheet object, or None if missing.
    """

    @property
    @override
    def purpose(self) -> str:
        return "team"

    @property
    @override
    def db_field(self) -> str:
        return "team_worksheet_ids"

    @property
    @override
    def is_collection_field(self) -> bool:
        return True

    @classmethod
    @override
    def default_title_generator(cls) -> Generator[str]:
        """
        Generate default titles for team worksheets.

        Yields:
            str: Default titles for team worksheets.
        """
        yield "Main Team"
        yield "Encore Team"
        yield "Backup Team"
        yield from (f"Team {i}" for i in it.count(4))


class SummaryWorksheetMetadata(WorksheetMetadata):
    """
    Represents metadata for the summary worksheet in the team register.

    Args:
        worksheet_id (int | None): The unique ID of the summary worksheet.
        title (str | None): The title of the summary worksheet.
        worksheet (AsyncioGspreadWorksheet | None):
            The summary worksheet object, or None if missing.

    Attributes:
        worksheet_id (int | None): The unique ID of the summary worksheet.
        title (str | None): The title of the summary worksheet.
        worksheet (AsyncioGspreadWorksheet | None):
            The summary worksheet object, or None if missing.
    """

    @property
    @override
    def purpose(self) -> str:
        return "summary"

    @property
    @override
    def db_field(self) -> str:
        return "summary_worksheet_id"

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
            str: Default title for the summary worksheet.
        """
        yield "Team Summary"
        yield from (f"Team Summary {i}" for i in it.count(1))


@dataclass
class TeamRegisterGoogleSheetsMetadata(GoogleSheetsMetadata):
    """
    Represents metadata for a Google Sheets document used in team registration.

    Args:
        sheet_url (str): The URL of the Google Sheets document.
        worksheets (list[WorksheetMetadata]): List of worksheet metadata.

    Attributes:
        sheet_url (str): The URL of the Google Sheets document.
        worksheets (list[WorksheetMetadata]): List of worksheet metadata.
    """

    team_worksheets: list[TeamWorksheetMetadata] = field(init=False)
    summary_worksheet: SummaryWorksheetMetadata = field(init=False)
    worksheets: list[WorksheetMetadata] = field(repr=False)

    def __post_init__(self) -> None:
        """
        Post-initialization to set up teams and summary worksheets.
        """
        self.team_worksheets = [
            TeamWorksheetMetadata(
                id=ws.id,
                title=ws.title,
                worksheet=ws.worksheet,
            )
            for ws in self.worksheets[:-1]
        ]
        self.summary_worksheet = SummaryWorksheetMetadata(
            id=self.worksheets[-1].id,
            title=self.worksheets[-1].title,
            worksheet=self.worksheets[-1].worksheet,
        )
        # Rebuild worksheets as subclass instances so each provides correct purpose,
        # attributes, etc. This ensures all logic flows use the right worksheet type
        # and properties.
        self.worksheets = [*self.team_worksheets, self.summary_worksheet]

    @classmethod
    def from_subtyped_worksheets(
        cls, sheet_url: str, worksheets: list[WorksheetMetadata]
    ) -> Self:
        team_worksheets = [
            ws for ws in worksheets if isinstance(ws, TeamWorksheetMetadata)
        ]
        summary_worksheet = next(
            (ws for ws in worksheets if isinstance(ws, SummaryWorksheetMetadata)),
            None,
        )
        if summary_worksheet is None:
            msg = "Summary worksheet must be provided."
            raise ValueError(msg)
        return cls(sheet_url, [*team_worksheets, summary_worksheet])


class TeamWorksheetContent(WorksheetContentBase[Team]):
    COLUMNS: ClassVar[list[str]] = [f.name for f in dataclasses.fields(Team)]
    DTYPES: ClassVar[dict[str, str]] = {
        f.name: str(f.type) for f in dataclasses.fields(Team)
    }

    INDEX_NAME: ClassVar[str] = COLUMNS[0]


class SummaryWorksheetContent(WorksheetContentBase[UserInfoWithEncoreRoles]):
    COLUMNS: ClassVar[list[str]] = [
        f.name for f in dataclasses.fields(UserInfoWithEncoreRoles)
    ]
    DTYPES: ClassVar[dict[str, str]] = {
        f.name: str(f.type) for f in dataclasses.fields(UserInfoWithEncoreRoles)
    }
    INDEX_NAME: ClassVar[str] = COLUMNS[0]

    def update_display_names(self, display_names: pd.Series[str]) -> None:
        self.main.update(display_names)

    def update_encore_roles(self, encore_roles: pd.Series[str]) -> None:
        self.main.update(encore_roles)

    @classmethod
    def extended_columns_dtypes_from_titles(
        cls, titles: list[str]
    ) -> tuple[list[str], dict[str, str]]:
        """
        Get the extended columns for the summary worksheet content.

        Returns:
            list[str]: List of extended column names.
        """
        columns = [c for pair in Summary.isv_power_title_pairs(titles) for c in pair]
        return columns, dict.fromkeys(columns, "object")

    @classmethod
    def generate_from_team_dataframes(
        cls, team_df_by_titles: dict[str, pd.DataFrame]
    ) -> Self:
        """
        Generate a summary worksheet content from team DataFrames.

        Args:
            team_df_by_titles (dict[str, pd.DataFrame]):
                Dictionary mapping team titles to their DataFrames.
            roles (pd.Series): Series containing encore roles for each user.

        Returns:
            Self: The generated summary worksheet content.
        """
        if not team_df_by_titles:
            return cls(
                pd.DataFrame(columns=cls.COLUMNS)
                .astype(cls.DTYPES)
                .set_index(cls.INDEX_NAME)
            )

        all_users = pd.concat(
            [
                df.reset_index()[[f.name for f in dataclasses.fields(UserInfo)]]
                for df in team_df_by_titles.values()
            ]
        ).drop_duplicates(subset=cls.INDEX_NAME)

        summary_df = all_users.set_index(cls.INDEX_NAME)

        summary_df["encore_roles"] = ""

        extra_columns = []
        extra_dtypes = {}

        for title, df in team_df_by_titles.items():
            isv_col = Summary.isv_title(title)
            power_col = Summary.power_title(title)
            effective_skill_value = Team.compute_effective_skill_value(
                df["leader_skill_value"], df["internal_skill_value"]
            )
            summary_df[isv_col] = effective_skill_value
            summary_df[power_col] = df["team_power"]
            extra_columns.extend([isv_col, power_col])
            extra_dtypes[isv_col] = "object"
            extra_dtypes[power_col] = "object"

        return cls(
            summary_df, extended_columns=extra_columns, extended_dtypes=extra_dtypes
        )
