from __future__ import annotations

import dataclasses
import itertools as it
import math
import re
import unicodedata
from dataclasses import InitVar, dataclass, field
from typing import TYPE_CHECKING, ClassVar, Self, cast, override

from utils.google_sheets import DimensionMutation, GridValueUpdate
from utils.structs_base import (
    ORIGINAL_MESSAGE_LINE_SEPARATOR,
    GoogleSheetsMetadata,
    OriginalMessage,
    SubmissionParseResult,
    UserInfo,
    WorksheetContractError,
    WorksheetMetadata,
    required_unique_header_index,
)

if TYPE_CHECKING:
    from collections.abc import Generator, Iterable, Mapping, Sequence


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
        self._summary[self.original_message_title()] = self.join_original_messages(
            team.original_message for team in teams if team is not None
        )

    def __getattr__(self, name: str) -> float | str:
        if name in self._summary:
            return self._summary[name]
        msg = f"{name} not found in summary"
        raise AttributeError(msg)

    @classmethod
    def original_message_title(cls) -> str:
        return "original_message"

    @classmethod
    def join_original_messages(cls, messages: Iterable[object]) -> str:
        return ORIGINAL_MESSAGE_LINE_SEPARATOR.join(
            str(message) for message in messages if message
        )

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


@dataclass(frozen=True, slots=True)
class WorksheetPhysicalIndex:
    """Exact worksheet columns and one-based rows keyed by username."""

    bot_headers: tuple[str, ...]
    column_by_header: dict[str, int]
    row_by_username: dict[object, int]
    reusable_rows: tuple[int, ...]

    @property
    def first_reusable_row(self) -> int | None:
        return self.reusable_rows[0] if self.reusable_rows else None


def _build_physical_index(
    bot_headers: tuple[str, ...],
    rows: Sequence[Sequence[object]],
    *,
    index_name: str,
) -> WorksheetPhysicalIndex:
    column_by_header = {
        header: column for column, header in enumerate(bot_headers, start=1)
    }
    username_index = column_by_header[index_name] - 1
    row_by_username: dict[object, int] = {}
    reusable_rows = []
    for row_number, row in enumerate(rows, start=2):
        if all(
            len(row) <= index or row[index] in (None, "")
            for index in range(len(bot_headers))
        ):
            reusable_rows.append(row_number)
        if len(row) <= username_index or row[username_index] in (None, ""):
            continue
        username = row[username_index]
        if username in row_by_username:
            raise WorksheetContractError
        row_by_username[username] = row_number
    return WorksheetPhysicalIndex(
        bot_headers=bot_headers,
        column_by_header=column_by_header,
        row_by_username=row_by_username,
        reusable_rows=tuple(reusable_rows),
    )


def _worksheet_nonnegative_integer(value: object) -> int:
    if isinstance(value, bool):
        raise TypeError
    integer = int(value)
    if integer < 0 or (
        isinstance(value, float)
        and (not math.isfinite(value) or not value.is_integer())
    ):
        raise ValueError
    return integer


def _worksheet_nonnegative_float(value: object) -> float:
    if isinstance(value, bool):
        raise TypeError
    number = float(value)
    if number < 0 or not math.isfinite(number):
        raise ValueError
    return number


def _summary_pair_titles(headers: Sequence[object]) -> list[str]:
    if len(headers) % 2:
        raise WorksheetContractError
    titles = []
    for index in range(0, len(headers), 2):
        isv_header, power_header = headers[index : index + 2]
        if not isinstance(isv_header, str) or not isv_header.endswith(" ISV"):
            raise WorksheetContractError
        title = isv_header.removesuffix(" ISV")
        if not title or power_header != Summary.power_title(title) or title in titles:
            raise WorksheetContractError
        titles.append(title)
    return titles


def _validate_summary_admin_headers(headers: Sequence[object]) -> None:
    reserved = {
        *SummaryWorksheetContent.COLUMNS,
        Summary.original_message_title(),
    }
    if any(
        header in reserved
        or (isinstance(header, str) and header.endswith((" ISV", " Power")))
        for header in headers
    ):
        raise WorksheetContractError


def _summary_title_header_positions(
    headers: Sequence[object],
) -> dict[str, tuple[int, int]]:
    positions: dict[str, dict[str, int]] = {}
    for index, header in enumerate(headers):
        if header in SummaryWorksheetContent.COLUMNS:
            continue
        if not isinstance(header, str):
            raise WorksheetContractError
        if header.endswith(" ISV"):
            title = header.removesuffix(" ISV")
            kind = "isv"
        elif header.endswith(" Power"):
            title = header.removesuffix(" Power")
            kind = "power"
        else:
            raise WorksheetContractError
        if not title or kind in positions.setdefault(title, {}):
            raise WorksheetContractError
        positions[title][kind] = index
    if any(set(pair) != {"isv", "power"} for pair in positions.values()):
        raise WorksheetContractError
    return {title: (pair["isv"], pair["power"]) for title, pair in positions.items()}


def _summary_values_by_header(
    user: UserInfoWithEncoreRoles,
    team_by_title: Mapping[str, Team | None],
) -> dict[str, object]:
    values: dict[str, object] = {
        "username": user.username,
        "display_name": user.display_name,
        "encore_roles": user.encore_roles,
        Summary.original_message_title(): Summary.join_original_messages(
            team.original_message for team in team_by_title.values() if team is not None
        ),
    }
    for title, team in team_by_title.items():
        values[Summary.isv_title(title)] = team.effective_skill_value if team else ""
        values[Summary.power_title(title)] = team.team_power if team else ""
    return values


def _plan_duplicate_terminal_repair(
    worksheet_id: int,
    headers: Sequence[object],
    titles: Sequence[str],
    marker_positions: Sequence[int],
) -> tuple[DimensionMutation, ...]:
    try:
        first_terminal, former_terminal = marker_positions
    except ValueError:
        raise WorksheetContractError from None
    base_count = len(SummaryWorksheetContent.COLUMNS)
    if list(headers[:base_count]) != SummaryWorksheetContent.COLUMNS:
        raise WorksheetContractError
    current_titles = _summary_pair_titles(headers[base_count:first_terminal])
    current_shared = [title for title in current_titles if title in titles]
    desired_shared = [title for title in titles if title in current_titles]
    if current_shared != desired_shared:
        raise WorksheetContractError
    stale_titles = _summary_pair_titles(headers[first_terminal + 1 : former_terminal])
    if not stale_titles or set(stale_titles) & {*current_titles, *titles}:
        raise WorksheetContractError
    _validate_summary_admin_headers(headers[former_terminal + 1 :])
    return (
        DimensionMutation.delete_columns(
            worksheet_id,
            start_column=first_terminal + 2,
            count=former_terminal - first_terminal,
        ),
    )


class TeamWorksheetContent:
    COLUMNS: ClassVar[list[str]] = [f.name for f in dataclasses.fields(Team)]

    INDEX_NAME: ClassVar[str] = COLUMNS[0]

    @classmethod
    def plan_header_migration(
        cls,
        worksheet_id: int,
        headers: Sequence[object],
        proposed_bot_rows: Sequence[Sequence[object]],
        *,
        column_count: int | None = None,
    ) -> tuple[GridValueUpdate | DimensionMutation, ...]:
        """Plan only recognized Team header initialization or repair."""
        if not headers:
            if any(
                value not in (None, "") for row in proposed_bot_rows for value in row
            ):
                raise WorksheetContractError
            return (
                GridValueUpdate.from_values(
                    worksheet_id=worksheet_id,
                    start_row=1,
                    start_column=1,
                    values=[cls.COLUMNS],
                ),
            )
        if len(headers) == len(cls.COLUMNS) - 1 and all(
            header in headers for header in cls.COLUMNS[:-1]
        ):
            column = len(headers) + 1
            dimension_mutation = (
                DimensionMutation.append_columns(worksheet_id, count=1)
                if column_count is not None and column > column_count
                else DimensionMutation.insert_columns(
                    worksheet_id,
                    start_column=column,
                )
            )
            return (
                dimension_mutation,
                GridValueUpdate.from_values(
                    worksheet_id=worksheet_id,
                    start_row=1,
                    start_column=column,
                    values=[[Summary.original_message_title()]],
                ),
            )
        cls.index_physical_rows(headers, [])
        return ()

    @classmethod
    def index_physical_rows(
        cls,
        headers: Sequence[object],
        rows: Sequence[Sequence[object]],
    ) -> WorksheetPhysicalIndex:
        """Index a structurally valid Team worksheet without parsing row values."""
        for required_header in cls.COLUMNS:
            required_unique_header_index(headers, required_header)
        terminal = required_unique_header_index(
            headers, Summary.original_message_title()
        )
        bot_headers = tuple(headers[: terminal + 1])
        if (
            not bot_headers
            or len(bot_headers) != len(cls.COLUMNS)
            or any(header not in cls.COLUMNS for header in bot_headers)
        ):
            raise WorksheetContractError
        bot_headers = cast("tuple[str, ...]", bot_headers)
        return _build_physical_index(
            bot_headers,
            rows,
            index_name=cls.INDEX_NAME,
        )

    @classmethod
    def plan_upsert(
        cls,
        worksheet_id: int,
        headers: Sequence[object],
        rows: Sequence[Sequence[object]],
        team: Team,
    ) -> tuple[GridValueUpdate, ...]:
        """Plan one Team bot-band update in the worksheet's current header order."""
        index = cls.index_physical_rows(headers, rows)
        row = index.row_by_username.get(team.username)
        if row is None:
            row = index.first_reusable_row
        if row is None:
            row = len(rows) + 2
        values_by_header = {
            "username": team.username,
            "display_name": team.display_name,
            "leader_skill_value": team.leader_skill_value,
            "internal_skill_value": team.internal_skill_value,
            "team_power": team.team_power,
            "original_message": team.original_message,
        }
        return (
            GridValueUpdate.from_values(
                worksheet_id=worksheet_id,
                start_row=row,
                start_column=1,
                values=[[values_by_header[header] for header in index.bot_headers]],
            ),
        )

    @classmethod
    def plan_delete(
        cls,
        worksheet_id: int,
        headers: Sequence[object],
        rows: Sequence[Sequence[object]],
        username: str,
    ) -> tuple[DimensionMutation, ...]:
        """Plan deletion of the complete physical row for one Team username."""
        row = cls.index_physical_rows(headers, rows).row_by_username.get(username)
        if row is None:
            return ()
        return (DimensionMutation.delete_rows(worksheet_id, start_row=row),)

    @classmethod
    def validated_teams(
        cls,
        headers: Sequence[object],
        rows: Sequence[Sequence[object]],
    ) -> tuple[Team, ...]:
        """Parse every keyed Team row after structural indexing succeeds."""
        index = cls.index_physical_rows(headers, rows)
        teams = []
        for row in rows:
            values = {
                header: row[column - 1] if len(row) >= column else ""
                for header, column in index.column_by_header.items()
            }
            if values[cls.INDEX_NAME] in (None, ""):
                continue
            try:
                teams.append(
                    Team(
                        username=cast("str", values["username"]),
                        display_name=cast("str", values["display_name"]),
                        leader_skill_value=_worksheet_nonnegative_integer(
                            values["leader_skill_value"]
                        ),
                        internal_skill_value=_worksheet_nonnegative_integer(
                            values["internal_skill_value"]
                        ),
                        team_power=_worksheet_nonnegative_float(values["team_power"]),
                        original_message=cast("str", values["original_message"]),
                    )
                )
            except (OverflowError, TypeError, ValueError):
                raise WorksheetContractError from None
        return tuple(teams)


class SummaryWorksheetContent:
    COLUMNS: ClassVar[list[str]] = [
        f.name for f in dataclasses.fields(UserInfoWithEncoreRoles)
    ]
    INDEX_NAME: ClassVar[str] = COLUMNS[0]

    @classmethod
    def plan_header_migration(
        cls,
        worksheet_id: int,
        headers: Sequence[object],
        proposed_bot_rows: Sequence[Sequence[object]],
        titles: Sequence[str],
        *,
        column_count: int | None = None,
    ) -> tuple[GridValueUpdate | DimensionMutation, ...]:
        """Plan only deterministic Summary header initialization or migration."""
        if any(not title for title in titles) or len(set(titles)) != len(titles):
            raise WorksheetContractError
        dynamic_headers, _ = cls.extended_columns_dtypes_from_titles(list(titles))
        canonical_headers = [*cls.COLUMNS, *dynamic_headers]
        if not headers:
            if any(
                value not in (None, "") for row in proposed_bot_rows for value in row
            ):
                raise WorksheetContractError
            return (
                GridValueUpdate.from_values(
                    worksheet_id=worksheet_id,
                    start_row=1,
                    start_column=1,
                    values=[canonical_headers],
                ),
            )
        if list(headers) == canonical_headers[:-1]:
            column = len(headers) + 1
            dimension_mutation = (
                DimensionMutation.append_columns(worksheet_id, count=1)
                if column_count is not None and column > column_count
                else DimensionMutation.insert_columns(
                    worksheet_id,
                    start_column=column,
                )
            )
            return (
                dimension_mutation,
                GridValueUpdate.from_values(
                    worksheet_id=worksheet_id,
                    start_row=1,
                    start_column=column,
                    values=[[Summary.original_message_title()]],
                ),
            )
        for required_header in cls.COLUMNS:
            required_unique_header_index(headers, required_header)
        marker_positions = [
            index
            for index, header in enumerate(headers)
            if header == Summary.original_message_title()
        ]
        if len(marker_positions) != 1:
            repair = _plan_duplicate_terminal_repair(
                worksheet_id,
                headers,
                titles,
                marker_positions,
            )
            first_terminal, former_terminal = marker_positions
            repaired_headers = list(headers)
            del repaired_headers[first_terminal + 1 : former_terminal + 1]
            return (
                *repair,
                *cls.plan_header_migration(
                    worksheet_id,
                    repaired_headers,
                    proposed_bot_rows,
                    titles,
                    column_count=(
                        None
                        if column_count is None
                        else column_count - (former_terminal - first_terminal)
                    ),
                ),
            )
        terminal = required_unique_header_index(
            headers, Summary.original_message_title()
        )
        _validate_summary_admin_headers(headers[terminal + 1 :])
        title_positions = _summary_title_header_positions(headers[:terminal])
        obsolete_titles = [title for title in title_positions if title not in titles]
        missing_titles = [title for title in titles if title not in title_positions]
        deletion_ranges = []
        for title in obsolete_titles:
            indexes = sorted(title_positions[title])
            if indexes[1] == indexes[0] + 1:
                deletion_ranges.append((indexes[0], 2))
            else:
                deletion_ranges.extend((index, 1) for index in indexes)
        deletion_ranges.sort(reverse=True)
        deletions = tuple(
            DimensionMutation.delete_columns(
                worksheet_id,
                start_column=index + 1,
                count=count,
            )
            for index, count in deletion_ranges
        )
        if not missing_titles:
            return deletions
        new_headers = [
            header
            for title in missing_titles
            for header in (Summary.isv_title(title), Summary.power_title(title))
        ]
        column = terminal - sum(count for _, count in deletion_ranges) + 1
        return (
            *deletions,
            DimensionMutation.insert_columns(
                worksheet_id,
                start_column=column,
                count=len(new_headers),
            ),
            GridValueUpdate.from_values(
                worksheet_id=worksheet_id,
                start_row=1,
                start_column=column,
                values=[new_headers],
            ),
        )

    @classmethod
    def index_physical_rows(
        cls,
        headers: Sequence[object],
        rows: Sequence[Sequence[object]],
        titles: Sequence[str],
    ) -> WorksheetPhysicalIndex:
        """Index a Summary worksheet using exact title-derived bot headers."""
        dynamic_headers, _ = cls.extended_columns_dtypes_from_titles(list(titles))
        expected_headers = [*cls.COLUMNS, *dynamic_headers]
        for required_header in expected_headers:
            required_unique_header_index(headers, required_header)
        terminal = required_unique_header_index(
            headers, Summary.original_message_title()
        )
        _validate_summary_admin_headers(headers[terminal + 1 :])
        bot_headers = tuple(headers[: terminal + 1])
        if len(bot_headers) != len(expected_headers) or any(
            header not in expected_headers for header in bot_headers
        ):
            raise WorksheetContractError
        bot_headers = cast("tuple[str, ...]", bot_headers)
        return _build_physical_index(
            bot_headers,
            rows,
            index_name=cls.INDEX_NAME,
        )

    @classmethod
    def plan_upsert(
        cls,
        worksheet_id: int,
        headers: Sequence[object],
        rows: Sequence[Sequence[object]],
        user: UserInfoWithEncoreRoles,
        team_by_title: Mapping[str, Team | None],
    ) -> tuple[GridValueUpdate, ...]:
        """Plan one Summary update from an explicit title-to-Team mapping."""
        titles = list(team_by_title)
        index = cls.index_physical_rows(headers, rows, titles)
        row = (
            index.row_by_username.get(user.username)
            or index.first_reusable_row
            or len(rows) + 2
        )
        values_by_header = _summary_values_by_header(user, team_by_title)
        return (
            GridValueUpdate.from_values(
                worksheet_id=worksheet_id,
                start_row=row,
                start_column=1,
                values=[[values_by_header[header] for header in index.bot_headers]],
            ),
        )

    @classmethod
    def plan_delete(
        cls,
        worksheet_id: int,
        headers: Sequence[object],
        rows: Sequence[Sequence[object]],
        titles: Sequence[str],
        username: str,
    ) -> tuple[DimensionMutation, ...]:
        """Plan deletion of one complete physical Summary row."""
        row = cls.index_physical_rows(headers, rows, titles).row_by_username.get(
            username
        )
        if row is None:
            return ()
        return (DimensionMutation.delete_rows(worksheet_id, start_row=row),)

    @classmethod
    def plan_reconciliation(
        cls,
        *,
        worksheet_id: int,
        headers: Sequence[object],
        rows: Sequence[Sequence[object]],
        team_worksheets: Mapping[
            str,
            tuple[Sequence[object], Sequence[Sequence[object]]],
        ],
        users: Mapping[str, UserInfoWithEncoreRoles],
    ) -> tuple[GridValueUpdate | DimensionMutation, ...]:
        """Plan a full Summary reconciliation from validated Team rows."""
        titles = list(team_worksheets)
        index = cls.index_physical_rows(headers, rows, titles)
        teams_by_title: dict[str, dict[str, Team]] = {}
        desired_usernames: list[str] = []
        first_team_by_username: dict[str, Team] = {}
        for title, (team_headers, team_rows) in team_worksheets.items():
            teams = TeamWorksheetContent.validated_teams(team_headers, team_rows)
            teams_by_title[title] = {team.username: team for team in teams}
            for team in teams:
                first_team_by_username.setdefault(team.username, team)
                if team.username not in desired_usernames:
                    desired_usernames.append(team.username)

        updates: list[GridValueUpdate | DimensionMutation] = []
        reusable_rows = iter(index.reusable_rows)
        appended_rows = 0
        for username in desired_usernames:
            row = index.row_by_username.get(username)
            if row is None:
                row = next(reusable_rows, None)
            if row is None:
                row = len(rows) + 2 + appended_rows
                appended_rows += 1
            fallback_team = first_team_by_username[username]
            user = users.get(
                username,
                UserInfoWithEncoreRoles(username, fallback_team.display_name, ""),
            )
            user = UserInfoWithEncoreRoles(
                username,
                user.display_name,
                user.encore_roles,
            )
            values_by_header = _summary_values_by_header(
                user,
                {title: teams_by_title[title].get(username) for title in titles},
            )
            updates.append(
                GridValueUpdate.from_values(
                    worksheet_id=worksheet_id,
                    start_row=row,
                    start_column=1,
                    values=[[values_by_header[header] for header in index.bot_headers]],
                )
            )

        desired = set(desired_usernames)
        updates.extend(
            DimensionMutation.delete_rows(worksheet_id, start_row=row)
            for username, row in sorted(
                index.row_by_username.items(),
                key=lambda item: item[1],
                reverse=True,
            )
            if username not in desired
        )
        return tuple(updates)

    @classmethod
    def extended_columns_dtypes_from_titles(
        cls, titles: list[str]
    ) -> tuple[list[str], dict[str, str]]:
        """
        Get the extended columns for the summary worksheet content.

        Returns:
            list[str]: List of extended column names.
        """
        columns = [
            *[c for pair in Summary.isv_power_title_pairs(titles) for c in pair],
            Summary.original_message_title(),
        ]
        return columns, dict.fromkeys(columns, "object")
