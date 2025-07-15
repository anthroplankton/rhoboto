from __future__ import annotations

import asyncio
import itertools as it
from typing import TYPE_CHECKING, overload, override

import pandas as pd

if TYPE_CHECKING:

    from discord import Member, Role

    from utils.google_sheets import AsyncioGspreadWorksheet
    from utils.structs_base import UserInfo, WorksheetMetadata

from models.team_register import TeamRegisterConfig
from utils.manager_base import ManagerBase
from utils.team_register_structs import (
    Summary,
    SummaryWorksheetContent,
    Team,
    TeamRegisterGoogleSheetsMetadata,
    TeamWorksheetContent,
    TeamWorksheetMetadata,
    UserInfoWithEncoreRoles,
)


class TeamRegisterManager(
    ManagerBase[TeamRegisterConfig, TeamRegisterGoogleSheetsMetadata]
):

    SheetConfigType = TeamRegisterConfig
    GoogleSheetsMetadataType = TeamRegisterGoogleSheetsMetadata

    @overload
    async def upsert_sheet_config_and_worksheets(
        self, sheet_url: str, worksheet_titles: list[str]
    ) -> TeamRegisterGoogleSheetsMetadata: ...
    @overload
    async def upsert_sheet_config_and_worksheets(
        self,
        sheet_url: str,
        *,
        team_worksheet_titles: list[str],
        summary_worksheet_title: str,
    ) -> TeamRegisterGoogleSheetsMetadata: ...
    @override
    async def upsert_sheet_config_and_worksheets(
        self,
        sheet_url: str,
        worksheet_titles: list[str] | None = None,
        *,
        team_worksheet_titles: list[str] | None = None,
        summary_worksheet_title: str | None = None,
    ) -> TeamRegisterGoogleSheetsMetadata:
        worksheet_titles = worksheet_titles or []
        team_worksheet_titles = team_worksheet_titles or []
        if summary_worksheet_title is None:
            worksheet_titles = [*worksheet_titles, *team_worksheet_titles]
        else:
            worksheet_titles = [
                *worksheet_titles,
                *team_worksheet_titles,
                summary_worksheet_title,
            ]
        if not worksheet_titles:
            msg = "At least summary worksheet title must be provided."
            raise ValueError(msg)
        return await super().upsert_sheet_config_and_worksheets(
            sheet_url, worksheet_titles
        )

    @override
    async def ensure_worksheets_and_upsert_sheet_config(
        self,
        metadata: TeamRegisterGoogleSheetsMetadata,
        counts: dict[type[WorksheetMetadata], int] | int | None = None,
        count: int | None = None,
    ) -> TeamRegisterGoogleSheetsMetadata:
        if isinstance(counts, int):
            counts = {TeamWorksheetMetadata: counts}
        if count is not None:
            counts = counts or {}
            counts[TeamWorksheetMetadata] = max(
                counts.get(TeamWorksheetMetadata, count), count
            )
        return await super().ensure_worksheets_and_upsert_sheet_config(metadata, counts)

    async def update_encore_roles_record(self, roles: list[Role]) -> None:
        """
        Update the encore roles in the TeamRegister database record.

        Args:
            roles (list[Role]): List of encore roles.
        """
        team_register_config = await self.get_sheet_config()

        team_register_config.encore_role_ids = [role.id for role in roles]
        await team_register_config.save()

    async def upsert_or_delete_user_team(
        self,
        user: UserInfo,
        team: Team | None,
        worksheet: AsyncioGspreadWorksheet | None,
    ) -> None:
        if worksheet is None and team is None:
            return
        if worksheet is None:
            self.logger.warning(
                "No worksheet provided for team %r, skipping update.",
                team,
            )
            return

        df = await worksheet.to_frame()
        team_df, plain_df = TeamWorksheetContent.standardize_dataframe(df)
        content = TeamWorksheetContent(team_df, plain_df)

        if team is None:
            content.delete(user.username)
        else:
            content.upsert(team)

        updated_team_df = content.to_frame()

        await worksheet.update_from_dataframe(updated_team_df)

        if team is None:
            self.logger.info(
                "Deleted team for user %r from worksheet %s", user, worksheet.title
            )
        else:
            self.logger.info("Updated team %r in worksheet %s", team, worksheet.title)

        self.logger.debug(
            "Updated team %r in worksheet %s:\n%s",
            team,
            worksheet.title,
            updated_team_df,
        )

    async def upsert_user_teams(
        self,
        user: UserInfo,
        main_team: Team,
        encore_team: Team | None,
        *backup_teams: Team,
        metadata: TeamRegisterGoogleSheetsMetadata,
    ) -> None:
        await asyncio.gather(
            *(
                self.upsert_or_delete_user_team(user, team, ws.worksheet)
                for team, ws in it.zip_longest(
                    [main_team, encore_team, *backup_teams],
                    metadata.team_worksheets,
                )
            )
        )

    async def delete_user_teams(
        self, user: UserInfo, metadata: TeamRegisterGoogleSheetsMetadata
    ) -> None:
        await asyncio.gather(
            *(
                self.upsert_or_delete_user_team(user, None, ws.worksheet)
                for ws in metadata.team_worksheets
            )
        )

    async def upsert_user_summary(
        self,
        user: UserInfo,
        roles: list[Role],
        main_team: Team | None,
        encore_team: Team | None,
        *backup_teams: Team,
        metadata: TeamRegisterGoogleSheetsMetadata,
    ) -> None:
        """
        Summarize the teams for the user in the summary worksheet.

        Args:
            user (UserInfo): The user information.
            main_team (Team): The main team.
            encore_team (Team | None): The encore team, if any.
            backup_teams (Team): Any additional backup teams.
            metadata (TeamRegisterGoogleSheetsMetadata):
                Metadata containing worksheet info.
        """
        summary_ws = metadata.summary_worksheet.worksheet
        if summary_ws is None:
            self.logger.warning(
                "No summary worksheet found for Team Register, skipping refresh."
            )
            return

        titles = [ws.title for ws in metadata.team_worksheets if ws.title is not None]

        encore_role_ids = (await self.get_sheet_config()).encore_role_ids
        encore_roles = UserInfoWithEncoreRoles.roles_to_string(
            [role.name for role in roles if role.id in encore_role_ids]
        )

        summary = Summary(
            username=user.username,
            display_name=user.display_name,
            encore_roles=encore_roles,
            titles=titles,
            teams=[main_team, encore_team, *backup_teams],
        )

        extended_columns, extended_dtypes = (
            SummaryWorksheetContent.extended_columns_dtypes_from_titles(titles)
        )

        df = await summary_ws.to_frame()

        summary_df, plain_df = SummaryWorksheetContent.standardize_dataframe(
            df, extended_columns=extended_columns, extended_dtypes=extended_dtypes
        )

        content = SummaryWorksheetContent(
            summary_df,
            plain_df,
            extended_columns=extended_columns,
            extended_dtypes=extended_dtypes,
        )

        content.upsert(summary)

        await summary_ws.update_from_dataframe(content.to_frame())

        self.logger.info(
            "Updated summary for user %r in summary worksheet `%s`",
            user,
            summary_ws.title,
        )
        self.logger.debug(
            "Updated summary for user %r in summary worksheet `%s`:\n%s",
            user,
            summary_ws.title,
            content.to_frame(),
        )

    async def delete_user_summary(
        self, user: UserInfo, metadata: TeamRegisterGoogleSheetsMetadata
    ) -> None:
        """
        Delete the user's summary from the summary worksheet.

        Args:
            user (UserInfo): The user information.
            metadata (TeamRegisterGoogleSheetsMetadata):
                Metadata containing worksheet info.
        """
        summary_ws = metadata.summary_worksheet.worksheet
        if summary_ws is None:
            self.logger.warning(
                "No summary worksheet found for Team Register, skipping refresh."
            )
            return

        df = await summary_ws.to_frame()

        summary_df, plain_df = SummaryWorksheetContent.standardize_dataframe(
            df, extended_columns=list(df.columns), extended_dtypes=df.dtypes.to_dict()
        )

        content = SummaryWorksheetContent(
            summary_df,
            plain_df,
            extended_columns=list(df.columns),
            extended_dtypes=df.dtypes.to_dict(),
        )

        content.delete(user.username)

        await summary_ws.update_from_dataframe(content.to_frame())

        self.logger.info(
            "Deleted summary for user %r from summary worksheet `%s`",
            user,
            summary_ws.title,
        )
        self.logger.debug(
            "Deleted summary for user %r from summary worksheet `%s`:\n%s",
            user,
            summary_ws.title,
            content.to_frame(),
        )

    async def refresh_summary_worksheet(
        self,
        metadata: TeamRegisterGoogleSheetsMetadata,
        member_by_names: dict[str, Member],
    ) -> pd.DataFrame | None:
        summary_ws = metadata.summary_worksheet.worksheet
        if summary_ws is None:
            self.logger.warning(
                "No summary worksheet found for Team Register, skipping refresh."
            )
            return None

        team_dfs = await asyncio.gather(
            *(
                ws.worksheet.to_frame()
                for ws in metadata.team_worksheets
                if ws.worksheet is not None
            )
        )

        team_dfs = [
            TeamWorksheetContent.standardize_dataframe(df)[0] for df in team_dfs
        ]
        summary_df = await summary_ws.to_frame()
        content = SummaryWorksheetContent(summary_df)

        new = SummaryWorksheetContent.generate_from_team_dataframes(
            team_df_by_titles={
                ws.title: df
                for ws, df in zip(metadata.team_worksheets, team_dfs)
                if ws.title is not None
            }
        )

        display_names = pd.Series(
            (
                member_by_names[n].display_name if n in member_by_names else None
                for n in new.main.index
            ),
            index=new.main.index,
            name="display_name",
        )

        encore_role_ids = (await self.get_sheet_config()).encore_role_ids

        def get_encore_roles(name: str) -> str:
            if not encore_role_ids:
                return ""
            member = member_by_names.get(name)
            if not member:
                return ""
            return UserInfoWithEncoreRoles.roles_to_string(
                [role.name for role in member.roles if role.id in encore_role_ids]
            )

        encore_roles = pd.Series(
            (get_encore_roles(n) for n in new.main.index),
            index=new.main.index,
            name="encore_roles",
        )

        new.update_display_names(display_names)
        new.update_encore_roles(encore_roles)

        content.main = new.main

        await summary_ws.update_from_dataframe(content.to_frame())

        self.logger.info(
            "Refreshed summary worksheet `%s` with %d entries",
            summary_ws.title,
            len(content.main),
        )
        self.logger.debug(
            "Refreshed summary worksheet `%s`:\n%s",
            summary_ws.title,
            content.to_frame(),
        )

        return content.main
