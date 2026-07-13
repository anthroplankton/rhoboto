from __future__ import annotations

import itertools as it
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, overload, override

import pandas as pd
from gspread.utils import rowcol_to_a1

from models.team_register import TeamRegisterConfig
from utils.google_sheets import (
    AsyncioGspreadWorksheet,
    DimensionMutation,
    GoogleSheet,
    GridValueUpdate,
)
from utils.google_sheets_errors import (
    GOOGLE_SHEETS_EXTERNAL_EXCEPTIONS,
    GoogleSheetsError,
    classify_google_sheets_exception,
)
from utils.google_sheets_urls import normalize_google_sheet_url
from utils.key_async_lock import KeyAsyncLock
from utils.manager_base import (
    ManagerBase,
    SheetConfigNotFoundError,
    spreadsheet_structure_transaction,
    worksheet_transaction_key,
    worksheet_transactions,
)
from utils.storage_errors import partial_success_storage_error
from utils.structs_base import WorksheetContractError
from utils.team_register_structs import (
    Summary,
    SummaryWorksheetContent,
    SummaryWorksheetMetadata,
    Team,
    TeamRegisterGoogleSheetsMetadata,
    TeamWorksheetContent,
    TeamWorksheetMetadata,
    UserInfoWithEncoreRoles,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Hashable

    from discord import Member, Role

    from utils.structs_base import UserInfo, WorksheetMetadata

TEAM_REGISTER_SHEET_WRITE_LOCK = KeyAsyncLock()
SUMMARY_TITLE_PAIR_COLUMN_COUNT = 2
TEAM_FIRST_DATA_ROW = 2


def _raise_partial_after_side_effect(
    exc: Exception,
    *,
    side_effect: bool,
) -> None:
    if side_effect and (error := partial_success_storage_error(exc)) is not None:
        raise error from error.__cause__


@asynccontextmanager
async def fresh_team_channel_transaction(
    manager: TeamRegisterManager,
    feature_channel_lock: KeyAsyncLock,
    *,
    channel_id: Hashable,
) -> AsyncIterator[TeamRegisterConfig]:
    """Lock the Team channel and refresh its current Sheet configuration."""
    async with feature_channel_lock(channel_id):
        config = await manager.get_fresh_sheet_config()
        if config is None:
            raise SheetConfigNotFoundError(manager.feature_channel)
        yield config


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
        member_by_names: dict[str, Member] | None = None,
    ) -> TeamRegisterGoogleSheetsMetadata:
        worksheet_titles = worksheet_titles or []
        team_worksheet_titles = team_worksheet_titles or []
        if summary_worksheet_title is None:
            worksheet_titles = [*worksheet_titles, *team_worksheet_titles]
            if worksheet_titles:
                *team_worksheet_titles, summary_worksheet_title = worksheet_titles
        if summary_worksheet_title is None:
            msg = "At least summary worksheet title must be provided."
            raise ValueError(msg)
        if (
            not summary_worksheet_title.strip()
            or any(not title.strip() for title in team_worksheet_titles)
            or len({*team_worksheet_titles, summary_worksheet_title})
            != len(team_worksheet_titles) + 1
        ):
            raise WorksheetContractError(log_hint="invalid_worksheet_titles")

        sheet_url = normalize_google_sheet_url(sheet_url)
        current_config = await self.get_fresh_sheet_config()
        encore_role_ids = current_config.encore_role_ids if current_config else []
        self._google_sheet = GoogleSheet(sheet_url, self.service_account_path)
        requested = self.GoogleSheetsMetadataType.from_subtyped_worksheets(
            sheet_url,
            [
                *(
                    TeamWorksheetMetadata(None, title, None)
                    for title in team_worksheet_titles
                ),
                SummaryWorksheetMetadata(
                    None,
                    summary_worksheet_title,
                    None,
                ),
            ],
        )
        async with self._prepared_metadata_transaction(
            requested,
            team_count=len(team_worksheet_titles),
            validate_summary_sources=True,
            force_structure=True,
        ) as (metadata, grids, side_effect):
            try:
                mutations, _ = self._plan_summary_reconciliation(
                    metadata,
                    grids,
                    member_by_names or {},
                    encore_role_ids,
                )
            except Exception as exc:
                _raise_partial_after_side_effect(exc, side_effect=side_effect)
                raise
            try:
                await self._google_sheet.batch_update_grid(mutations)
            except Exception as exc:
                error = partial_success_storage_error(exc)
                if error is None:
                    raise
                raise error from error.__cause__
            return metadata

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
        await self.update_encore_role_ids_record([role.id for role in roles])

    async def update_encore_role_ids_record(self, role_ids: list[int]) -> None:
        """
        Update the encore role IDs in the TeamRegister database record.

        Args:
            role_ids (list[int]): List of encore role IDs.
        """
        team_register_config = await self.get_sheet_config()

        team_register_config.encore_role_ids = role_ids
        await team_register_config.save()

    @staticmethod
    async def _read_grid(
        worksheet: AsyncioGspreadWorksheet,
        expected_bot_headers: list[str],
    ) -> tuple[list[object], list[list[object]]]:
        (header_range,) = await worksheet.batch_get_values(["1:1"])
        headers = list(header_range[0]) if header_range else []
        marker_positions = [
            index
            for index, header in enumerate(headers)
            if header == Summary.original_message_title()
        ]
        if marker_positions:
            last_column = marker_positions[0] + 1
        elif not headers or (
            len(headers) == len(expected_bot_headers) - 1
            and all(header in headers for header in expected_bot_headers[:-1])
        ):
            last_column = len(expected_bot_headers)
        else:
            raise WorksheetContractError
        if worksheet.row_count < TEAM_FIRST_DATA_ROW or worksheet.col_count < 1:
            return headers, []
        readable_last_column = min(last_column, worksheet.col_count)
        last_column_letter = rowcol_to_a1(1, readable_last_column).removesuffix("1")
        (rows,) = await worksheet.batch_get_values(
            [f"A{TEAM_FIRST_DATA_ROW}:{last_column_letter}{worksheet.row_count}"]
        )
        return headers, list(rows)

    @staticmethod
    def _grid_after_mutations(
        headers: list[object],
        rows: list[list[object]],
        mutations: tuple[GridValueUpdate | DimensionMutation, ...],
    ) -> tuple[list[object], list[list[object]]]:
        grid = [list(headers), *(list(row) for row in rows)]
        for mutation in mutations:
            if isinstance(mutation, GridValueUpdate):
                last_row = mutation.start_row_index + len(mutation.rows)
                last_column = mutation.start_column_index + len(mutation.rows[0])
                while len(grid) < last_row:
                    grid.append([])
                for row_index, values in enumerate(
                    mutation.rows,
                    start=mutation.start_row_index,
                ):
                    row = grid[row_index]
                    row.extend([""] * max(0, last_column - len(row)))
                    row[mutation.start_column_index : last_column] = list(values)
                continue
            if mutation.dimension != "COLUMNS":
                if mutation.dimension == "ROWS" and mutation.operation == "delete":
                    start = mutation.start_index or 0
                    del grid[start : mutation.end_index]
                continue
            start = mutation.start_index or 0
            end = mutation.end_index or start
            for row in grid:
                if mutation.operation == "insert":
                    row[start:start] = [""] * (end - start)
                elif mutation.operation == "delete":
                    del row[start:end]
        return grid[0], grid[1:]

    @staticmethod
    def _plan_grid_growth(
        dimensions: dict[int, tuple[int, int]],
        column_mutations: list[DimensionMutation],
        updates: list[GridValueUpdate],
    ) -> list[DimensionMutation]:
        resulting_dimensions = dict(dimensions)
        for mutation in column_mutations:
            if mutation.dimension != "COLUMNS":
                continue
            rows, columns = resulting_dimensions[mutation.worksheet_id]
            if mutation.operation == "append":
                columns += mutation.length or 0
            else:
                count = (mutation.end_index or 0) - (mutation.start_index or 0)
                columns += count if mutation.operation == "insert" else -count
            resulting_dimensions[mutation.worksheet_id] = rows, columns
        required = {worksheet_id: [0, 0] for worksheet_id in dimensions}
        for update in updates:
            required_rows, required_columns = required[update.worksheet_id]
            required[update.worksheet_id] = [
                max(required_rows, update.start_row_index + len(update.rows)),
                max(
                    required_columns,
                    update.start_column_index + len(update.rows[0]),
                ),
            ]
        growth = []
        for worksheet_id, (rows, columns) in resulting_dimensions.items():
            required_rows, required_columns = required[worksheet_id]
            if required_rows > rows:
                growth.append(
                    DimensionMutation.append_rows(
                        worksheet_id,
                        required_rows - rows,
                    )
                )
            if required_columns > columns:
                growth.append(
                    DimensionMutation.append_columns(
                        worksheet_id,
                        required_columns - columns,
                    )
                )
        return growth

    async def _ensure_metadata_structure(
        self,
        metadata: TeamRegisterGoogleSheetsMetadata,
        *,
        team_count: int,
        create_missing: bool = True,
    ) -> tuple[
        TeamRegisterGoogleSheetsMetadata,
        bool,
        bool,
    ]:
        """Resolve or create Team worksheets and report persisted side effects."""
        desired = self.GoogleSheetsMetadataType.assign_missing_default_titles(
            metadata,
            {TeamWorksheetMetadata: team_count},
        )
        sheet = await self.get_google_sheet()
        try:
            spreadsheet = await sheet.sheet
            raw_worksheets = await spreadsheet.worksheets()
        except GoogleSheetsError:
            raise
        except GOOGLE_SHEETS_EXTERNAL_EXCEPTIONS as exc:
            raise classify_google_sheets_exception(
                exc,
                operation="read_worksheet",
            ) from exc
        worksheet_by_title = {}
        for raw_worksheet in raw_worksheets:
            worksheet = (
                raw_worksheet
                if hasattr(raw_worksheet, "batch_get_values")
                else AsyncioGspreadWorksheet(raw_worksheet)
            )
            worksheet_by_title[worksheet.title] = worksheet
        resolved_worksheets = []
        for worksheet_metadata in desired:
            worksheet = worksheet_metadata.worksheet
            if worksheet is None and worksheet_metadata.title is not None:
                worksheet = worksheet_by_title.get(worksheet_metadata.title)
            resolved_worksheets.append(
                type(worksheet_metadata)(
                    None if worksheet is not None else worksheet_metadata.id,
                    worksheet_metadata.title,
                    worksheet,
                )
            )
        desired = self.GoogleSheetsMetadataType.from_subtyped_worksheets(
            desired.sheet_url,
            resolved_worksheets,
        )
        missing_titles = [
            worksheet.title
            for worksheet in desired
            if worksheet.worksheet is None and worksheet.title is not None
        ]
        created_side_effect = False
        if missing_titles and create_missing:
            created = {}
            for title in missing_titles:
                try:
                    worksheet = await sheet.get_or_create_worksheet(title)
                except Exception as exc:
                    _raise_partial_after_side_effect(
                        exc,
                        side_effect=bool(created),
                    )
                    raise
                created[worksheet.title] = worksheet
            created_side_effect = bool(created)
            desired = self.GoogleSheetsMetadataType.from_subtyped_worksheets(
                desired.sheet_url,
                [
                    type(worksheet_metadata)(
                        None,
                        worksheet_metadata.title,
                        created.get(
                            worksheet_metadata.title,
                            worksheet_metadata.worksheet,
                        ),
                    )
                    for worksheet_metadata in desired
                ],
            )

        config_changed = [worksheet.id for worksheet in desired] != [
            worksheet.id for worksheet in metadata
        ]
        return desired, config_changed, created_side_effect

    async def _read_metadata_grids(
        self,
        metadata: TeamRegisterGoogleSheetsMetadata,
        *,
        validate_summary_sources: bool,
    ) -> dict[int, tuple[list[object], list[list[object]]]]:
        """Read and validate Team and Summary grids while their locks are held."""
        titles = [
            worksheet.title
            for worksheet in metadata.team_worksheets
            if worksheet.title is not None
        ]
        summary_headers, _ = (
            SummaryWorksheetContent.extended_columns_dtypes_from_titles(titles)
        )
        expected_summary_headers = [
            *SummaryWorksheetContent.COLUMNS,
            *summary_headers,
        ]
        grids: dict[int, tuple[list[object], list[list[object]]]] = {}
        for worksheet_metadata in metadata:
            worksheet = worksheet_metadata.worksheet
            if worksheet is not None:
                expected_headers = (
                    TeamWorksheetContent.COLUMNS
                    if isinstance(worksheet_metadata, TeamWorksheetMetadata)
                    else expected_summary_headers
                )
                grids[worksheet.id] = await self._read_grid(
                    worksheet,
                    expected_headers,
                )
        migrated_team_grids = []
        for worksheet_metadata in metadata.team_worksheets:
            worksheet = worksheet_metadata.worksheet
            if worksheet is None:
                continue
            headers, rows = grids[worksheet.id]
            migration = TeamWorksheetContent.plan_header_migration(
                worksheet.id,
                headers,
                rows,
                column_count=worksheet.col_count,
            )
            migrated_headers, migrated_rows = self._grid_after_mutations(
                headers,
                rows,
                migration,
            )
            TeamWorksheetContent.index_physical_rows(
                migrated_headers,
                migrated_rows,
            )
            migrated_team_grids.append((migrated_headers, migrated_rows))
        summary_worksheet = metadata.summary_worksheet.worksheet
        summary_inserts_title_pair = False
        if summary_worksheet is not None:
            headers, rows = grids[summary_worksheet.id]
            migration = SummaryWorksheetContent.plan_header_migration(
                summary_worksheet.id,
                headers,
                rows,
                titles,
                column_count=summary_worksheet.col_count,
            )
            summary_inserts_title_pair = any(
                isinstance(mutation, DimensionMutation)
                and mutation.operation == "insert"
                and mutation.dimension == "COLUMNS"
                and (mutation.end_index or 0) - (mutation.start_index or 0)
                >= SUMMARY_TITLE_PAIR_COLUMN_COUNT
                for mutation in migration
            )
            migrated_headers, migrated_rows = self._grid_after_mutations(
                headers,
                rows,
                migration,
            )
            SummaryWorksheetContent.index_physical_rows(
                migrated_headers,
                migrated_rows,
                titles,
            )
        if validate_summary_sources or summary_inserts_title_pair:
            for migrated_headers, migrated_rows in migrated_team_grids:
                TeamWorksheetContent.validated_teams(
                    migrated_headers,
                    migrated_rows,
                )
        return grids

    @asynccontextmanager
    async def _prepared_metadata_transaction(
        self,
        metadata: TeamRegisterGoogleSheetsMetadata,
        *,
        team_count: int,
        validate_summary_sources: bool = False,
        force_structure: bool = False,
    ) -> AsyncIterator[
        tuple[
            TeamRegisterGoogleSheetsMetadata,
            dict[int, tuple[list[object], list[list[object]]]],
            bool,
        ]
    ]:
        needs_structure = force_structure or (
            len(metadata.team_worksheets) < team_count
            or any(worksheet.worksheet is None for worksheet in metadata)
        )
        side_effect = False
        if needs_structure:
            async with spreadsheet_structure_transaction(metadata.sheet_url):
                (
                    metadata,
                    resolved_config_changed,
                    _,
                ) = await self._ensure_metadata_structure(
                    metadata,
                    team_count=team_count,
                    create_missing=False,
                )
            existing_resources = [
                worksheet_transaction_key(metadata.sheet_url, worksheet.id)
                for worksheet in metadata
                if worksheet.worksheet is not None
            ]
            async with worksheet_transactions(existing_resources):
                await self._read_metadata_grids(
                    metadata,
                    validate_summary_sources=validate_summary_sources,
                )
            async with spreadsheet_structure_transaction(metadata.sheet_url):
                (
                    metadata,
                    config_changed,
                    created,
                ) = await self._ensure_metadata_structure(
                    metadata,
                    team_count=team_count,
                )
                config_changed = resolved_config_changed or config_changed
                if config_changed:
                    try:
                        await self.upsert_sheet_config(metadata)
                    except Exception as exc:
                        _raise_partial_after_side_effect(exc, side_effect=created)
                        raise
                side_effect = created or config_changed

        resources = [
            worksheet_transaction_key(metadata.sheet_url, worksheet.id)
            for worksheet in metadata
            if worksheet.worksheet is not None
        ]
        async with worksheet_transactions(resources):
            grids = await self._read_metadata_grids(
                metadata,
                validate_summary_sources=validate_summary_sources,
            )
            yield metadata, grids, side_effect

    async def upsert_user_registration(
        self,
        user: UserInfo,
        roles: list[Role],
        main_team: Team,
        encore_team: Team | None,
        *backup_teams: Team,
    ) -> None:
        """Plan all Team tabs and the row-local Summary update as one batch."""
        teams = [main_team, encore_team, *backup_teams]
        async with self._prepared_metadata_transaction(
            await self.fetch_google_sheets_metadata(),
            team_count=len(teams),
        ) as (metadata, grids, side_effect):
            await self._upsert_user_registration_locked(
                metadata,
                grids,
                user,
                roles,
                teams,
                side_effect=side_effect,
            )

    async def _upsert_user_registration_locked(  # noqa: C901, PLR0912, PLR0913, PLR0915
        self,
        metadata: TeamRegisterGoogleSheetsMetadata,
        grids: dict[int, tuple[list[object], list[list[object]]]],
        user: UserInfo,
        roles: list[Role],
        teams: list[Team | None],
        *,
        side_effect: bool,
    ) -> None:
        config_changed = False
        created_side_effect = side_effect
        simulated_grids = dict(grids)
        column_mutations: list[DimensionMutation] = []
        header_updates: list[GridValueUpdate] = []
        data_mutations: list[GridValueUpdate | DimensionMutation] = []
        dimensions: dict[int, tuple[int, int]] = {}
        team_by_title: dict[str, Team | None] = {}
        for team, worksheet_metadata in it.zip_longest(
            teams,
            metadata.team_worksheets,
        ):
            if worksheet_metadata is None or worksheet_metadata.worksheet is None:
                continue
            worksheet = worksheet_metadata.worksheet
            dimensions[worksheet.id] = worksheet.row_count, worksheet.col_count
            headers, rows = grids[worksheet.id]
            header_migration = TeamWorksheetContent.plan_header_migration(
                worksheet_metadata.worksheet.id,
                headers,
                rows,
                column_count=worksheet.col_count,
            )
            headers, rows = self._grid_after_mutations(
                headers,
                rows,
                header_migration,
            )
            column_mutations.extend(
                mutation
                for mutation in header_migration
                if isinstance(mutation, DimensionMutation)
            )
            header_updates.extend(
                mutation
                for mutation in header_migration
                if isinstance(mutation, GridValueUpdate)
            )
            if team is None:
                action = TeamWorksheetContent.plan_delete(
                    worksheet_metadata.worksheet.id,
                    headers,
                    rows,
                    user.username,
                )
            else:
                action = TeamWorksheetContent.plan_upsert(
                    worksheet_metadata.worksheet.id,
                    headers,
                    rows,
                    team,
                )
            data_mutations.extend(action)
            simulated_grids[worksheet.id] = self._grid_after_mutations(
                headers,
                rows,
                action,
            )
            if worksheet_metadata.title is not None:
                team_by_title[worksheet_metadata.title] = team

        summary_worksheet = metadata.summary_worksheet.worksheet
        if summary_worksheet is None:
            return
        dimensions[summary_worksheet.id] = (
            summary_worksheet.row_count,
            summary_worksheet.col_count,
        )
        summary_headers, summary_rows = grids[summary_worksheet.id]
        summary_migration = SummaryWorksheetContent.plan_header_migration(
            summary_worksheet.id,
            summary_headers,
            summary_rows,
            list(team_by_title),
            column_count=summary_worksheet.col_count,
        )
        summary_headers, summary_rows = self._grid_after_mutations(
            summary_headers,
            summary_rows,
            summary_migration,
        )
        try:
            encore_role_ids = (await self.get_sheet_config()).encore_role_ids
            sheet = await self.get_google_sheet()
        except Exception as exc:
            _raise_partial_after_side_effect(
                exc,
                side_effect=created_side_effect,
            )
            raise
        encore_roles = UserInfoWithEncoreRoles.roles_to_string(
            [role.name for role in roles if role.id in encore_role_ids]
        )
        summary_user = UserInfoWithEncoreRoles(
            user.username,
            user.display_name,
            encore_roles,
        )
        inserts_title_pair = any(
            isinstance(mutation, DimensionMutation)
            and mutation.operation == "insert"
            and mutation.dimension == "COLUMNS"
            and (mutation.end_index or 0) - (mutation.start_index or 0)
            >= SUMMARY_TITLE_PAIR_COLUMN_COUNT
            for mutation in summary_migration
        )
        if inserts_title_pair:
            reconciliation, _ = self._plan_summary_reconciliation(
                metadata,
                simulated_grids,
                {},
                encore_role_ids,
                {user.username: summary_user},
            )
            row_updates = [
                mutation
                for mutation in data_mutations
                if isinstance(mutation, GridValueUpdate)
            ]
            row_deletions = [
                mutation
                for mutation in data_mutations
                if isinstance(mutation, DimensionMutation)
            ]
            growth = self._plan_grid_growth(
                dimensions,
                column_mutations,
                [*header_updates, *row_updates],
            )
            reconciliation_columns = [
                mutation
                for mutation in reconciliation
                if isinstance(mutation, DimensionMutation)
                and mutation.dimension == "COLUMNS"
                and mutation.operation != "append"
            ]
            reconciliation_growth = [
                mutation
                for mutation in reconciliation
                if isinstance(mutation, DimensionMutation)
                and mutation.operation == "append"
            ]
            reconciliation_headers = [
                mutation
                for mutation in reconciliation
                if isinstance(mutation, GridValueUpdate)
                and mutation.start_row_index == 0
            ]
            reconciliation_rows = [
                mutation
                for mutation in reconciliation
                if isinstance(mutation, GridValueUpdate)
                and mutation.start_row_index != 0
            ]
            row_deletions.extend(
                mutation
                for mutation in reconciliation
                if isinstance(mutation, DimensionMutation)
                and mutation.dimension == "ROWS"
                and mutation.operation == "delete"
            )
            if config_changed:
                try:
                    await self.upsert_sheet_config(metadata)
                except Exception as exc:
                    _raise_partial_after_side_effect(
                        exc,
                        side_effect=created_side_effect,
                    )
                    raise
            try:
                await sheet.batch_update_grid(
                    [
                        *column_mutations,
                        *reconciliation_columns,
                        *growth,
                        *reconciliation_growth,
                        *header_updates,
                        *reconciliation_headers,
                        *row_updates,
                        *reconciliation_rows,
                        *sorted(
                            row_deletions,
                            key=lambda mutation: mutation.start_index or 0,
                            reverse=True,
                        ),
                    ]
                )
            except Exception as exc:
                _raise_partial_after_side_effect(
                    exc,
                    side_effect=created_side_effect or config_changed,
                )
                raise
            return

        column_mutations.extend(
            mutation
            for mutation in summary_migration
            if isinstance(mutation, DimensionMutation)
        )
        header_updates.extend(
            mutation
            for mutation in summary_migration
            if isinstance(mutation, GridValueUpdate)
        )
        data_mutations.extend(
            SummaryWorksheetContent.plan_upsert(
                summary_worksheet.id,
                summary_headers,
                summary_rows,
                summary_user,
                team_by_title,
            )
        )
        row_updates = [
            mutation
            for mutation in data_mutations
            if isinstance(mutation, GridValueUpdate)
        ]
        row_deletions = sorted(
            (
                mutation
                for mutation in data_mutations
                if isinstance(mutation, DimensionMutation)
            ),
            key=lambda mutation: mutation.start_index or 0,
            reverse=True,
        )
        growth = self._plan_grid_growth(
            dimensions,
            column_mutations,
            [*header_updates, *row_updates],
        )
        if config_changed:
            try:
                await self.upsert_sheet_config(metadata)
            except Exception as exc:
                _raise_partial_after_side_effect(
                    exc,
                    side_effect=created_side_effect,
                )
                raise
        try:
            await sheet.batch_update_grid(
                [
                    *column_mutations,
                    *growth,
                    *header_updates,
                    *row_updates,
                    *row_deletions,
                ]
            )
        except Exception as exc:
            _raise_partial_after_side_effect(
                exc,
                side_effect=created_side_effect or config_changed,
            )
            raise

    async def delete_user_registration(self, user: UserInfo) -> None:
        """Plan complete Team and Summary row deletions as one batch."""
        source_metadata = await self.fetch_google_sheets_metadata()
        async with self._prepared_metadata_transaction(
            source_metadata,
            team_count=len(source_metadata.team_worksheets),
        ) as (metadata, grids, side_effect):
            await self._delete_user_registration_locked(
                metadata,
                grids,
                user,
                side_effect=side_effect,
            )

    async def _delete_user_registration_locked(  # noqa: PLR0915
        self,
        metadata: TeamRegisterGoogleSheetsMetadata,
        grids: dict[int, tuple[list[object], list[list[object]]]],
        user: UserInfo,
        *,
        side_effect: bool,
    ) -> None:
        config_changed = False
        created_side_effect = side_effect
        simulated_grids: dict[
            int,
            tuple[list[object], list[list[object]]],
        ] = dict(grids)
        column_mutations: list[DimensionMutation] = []
        header_updates: list[GridValueUpdate] = []
        row_deletions: list[DimensionMutation] = []
        dimensions: dict[int, tuple[int, int]] = {}
        titles = [
            worksheet.title
            for worksheet in metadata.team_worksheets
            if worksheet.title is not None
        ]
        for worksheet_metadata in metadata.team_worksheets:
            worksheet = worksheet_metadata.worksheet
            if worksheet is None:
                continue
            dimensions[worksheet.id] = worksheet.row_count, worksheet.col_count
            headers, rows = grids[worksheet.id]
            header_migration = TeamWorksheetContent.plan_header_migration(
                worksheet.id,
                headers,
                rows,
                column_count=worksheet.col_count,
            )
            headers, rows = self._grid_after_mutations(
                headers,
                rows,
                header_migration,
            )
            column_mutations.extend(
                mutation
                for mutation in header_migration
                if isinstance(mutation, DimensionMutation)
            )
            header_updates.extend(
                mutation
                for mutation in header_migration
                if isinstance(mutation, GridValueUpdate)
            )
            action = TeamWorksheetContent.plan_delete(
                worksheet.id,
                headers,
                rows,
                user.username,
            )
            row_deletions.extend(action)
            simulated_grids[worksheet.id] = self._grid_after_mutations(
                headers,
                rows,
                action,
            )

        summary_worksheet = metadata.summary_worksheet.worksheet
        if summary_worksheet is not None:
            dimensions[summary_worksheet.id] = (
                summary_worksheet.row_count,
                summary_worksheet.col_count,
            )
            summary_headers, summary_rows = grids[summary_worksheet.id]
            simulated_grids[summary_worksheet.id] = (
                summary_headers,
                summary_rows,
            )
            summary_migration = SummaryWorksheetContent.plan_header_migration(
                summary_worksheet.id,
                summary_headers,
                summary_rows,
                titles,
                column_count=summary_worksheet.col_count,
            )
            summary_headers, summary_rows = self._grid_after_mutations(
                summary_headers,
                summary_rows,
                summary_migration,
            )
            inserts_title_pair = any(
                isinstance(mutation, DimensionMutation)
                and mutation.operation == "insert"
                and mutation.dimension == "COLUMNS"
                and (mutation.end_index or 0) - (mutation.start_index or 0)
                >= SUMMARY_TITLE_PAIR_COLUMN_COUNT
                for mutation in summary_migration
            )
            if inserts_title_pair:
                reconciliation, _ = self._plan_summary_reconciliation(
                    metadata,
                    simulated_grids,
                    {},
                    [],
                )
                reconciliation_columns = [
                    mutation
                    for mutation in reconciliation
                    if isinstance(mutation, DimensionMutation)
                    and mutation.dimension == "COLUMNS"
                    and mutation.operation != "append"
                ]
                reconciliation_growth = [
                    mutation
                    for mutation in reconciliation
                    if isinstance(mutation, DimensionMutation)
                    and mutation.operation == "append"
                ]
                reconciliation_headers = [
                    mutation
                    for mutation in reconciliation
                    if isinstance(mutation, GridValueUpdate)
                    and mutation.start_row_index == 0
                ]
                reconciliation_rows = [
                    mutation
                    for mutation in reconciliation
                    if isinstance(mutation, GridValueUpdate)
                    and mutation.start_row_index != 0
                ]
                row_deletions.extend(
                    mutation
                    for mutation in reconciliation
                    if isinstance(mutation, DimensionMutation)
                    and mutation.dimension == "ROWS"
                    and mutation.operation == "delete"
                )
                growth = self._plan_grid_growth(
                    dimensions,
                    column_mutations,
                    header_updates,
                )
                mutations: list[GridValueUpdate | DimensionMutation] = [
                    *column_mutations,
                    *reconciliation_columns,
                    *growth,
                    *reconciliation_growth,
                    *header_updates,
                    *reconciliation_headers,
                    *reconciliation_rows,
                    *sorted(
                        row_deletions,
                        key=lambda mutation: mutation.start_index or 0,
                        reverse=True,
                    ),
                ]
            else:
                column_mutations.extend(
                    mutation
                    for mutation in summary_migration
                    if isinstance(mutation, DimensionMutation)
                )
                header_updates.extend(
                    mutation
                    for mutation in summary_migration
                    if isinstance(mutation, GridValueUpdate)
                )
                row_deletions.extend(
                    SummaryWorksheetContent.plan_delete(
                        summary_worksheet.id,
                        summary_headers,
                        summary_rows,
                        titles,
                        user.username,
                    )
                )
                growth = self._plan_grid_growth(
                    dimensions,
                    column_mutations,
                    header_updates,
                )
                mutations = [
                    *column_mutations,
                    *growth,
                    *header_updates,
                    *sorted(
                        row_deletions,
                        key=lambda mutation: mutation.start_index or 0,
                        reverse=True,
                    ),
                ]
        else:
            growth = self._plan_grid_growth(
                dimensions,
                column_mutations,
                header_updates,
            )
            mutations = [
                *column_mutations,
                *growth,
                *header_updates,
                *sorted(
                    row_deletions,
                    key=lambda mutation: mutation.start_index or 0,
                    reverse=True,
                ),
            ]

        if config_changed:
            try:
                await self.upsert_sheet_config(metadata)
            except Exception as exc:
                _raise_partial_after_side_effect(
                    exc,
                    side_effect=created_side_effect,
                )
                raise
        try:
            sheet = await self.get_google_sheet()
            await sheet.batch_update_grid(mutations)
        except Exception as exc:
            _raise_partial_after_side_effect(
                exc,
                side_effect=created_side_effect or config_changed,
            )
            raise

    def _plan_summary_reconciliation(
        self,
        metadata: TeamRegisterGoogleSheetsMetadata,
        grids: dict[int, tuple[list[object], list[list[object]]]],
        member_by_names: dict[str, Member],
        encore_role_ids: list[int],
        user_overrides: dict[str, UserInfoWithEncoreRoles] | None = None,
    ) -> tuple[list[GridValueUpdate | DimensionMutation], pd.DataFrame]:
        column_mutations: list[DimensionMutation] = []
        header_updates: list[GridValueUpdate] = []
        dimensions: dict[int, tuple[int, int]] = {}
        team_grids: dict[
            str,
            tuple[list[object], list[list[object]]],
        ] = {}
        for worksheet_metadata in metadata.team_worksheets:
            worksheet = worksheet_metadata.worksheet
            if worksheet is None or worksheet_metadata.title is None:
                continue
            dimensions[worksheet.id] = worksheet.row_count, worksheet.col_count
            headers, rows = grids[worksheet.id]
            migration = TeamWorksheetContent.plan_header_migration(
                worksheet.id,
                headers,
                rows,
                column_count=worksheet.col_count,
            )
            headers, rows = self._grid_after_mutations(headers, rows, migration)
            column_mutations.extend(
                mutation
                for mutation in migration
                if isinstance(mutation, DimensionMutation)
            )
            header_updates.extend(
                mutation
                for mutation in migration
                if isinstance(mutation, GridValueUpdate)
            )
            team_grids[worksheet_metadata.title] = (headers, rows)

        summary_worksheet = metadata.summary_worksheet.worksheet
        if summary_worksheet is None:
            return [], pd.DataFrame()
        dimensions[summary_worksheet.id] = (
            summary_worksheet.row_count,
            summary_worksheet.col_count,
        )
        original_headers, original_rows = grids[summary_worksheet.id]
        summary_migration = SummaryWorksheetContent.plan_header_migration(
            summary_worksheet.id,
            original_headers,
            original_rows,
            list(team_grids),
            column_count=summary_worksheet.col_count,
        )
        summary_headers, summary_rows = self._grid_after_mutations(
            original_headers,
            original_rows,
            summary_migration,
        )
        column_mutations.extend(
            mutation
            for mutation in summary_migration
            if isinstance(mutation, DimensionMutation)
        )
        header_updates.extend(
            mutation
            for mutation in summary_migration
            if isinstance(mutation, GridValueUpdate)
        )

        summary_index = SummaryWorksheetContent.index_physical_rows(
            summary_headers,
            summary_rows,
            list(team_grids),
        )
        users = {}
        for username, row_number in summary_index.row_by_username.items():
            row = summary_rows[row_number - 2]
            display_name_index = summary_index.column_by_header["display_name"] - 1
            encore_roles_index = summary_index.column_by_header["encore_roles"] - 1
            users[str(username)] = UserInfoWithEncoreRoles(
                str(username),
                str(
                    row[display_name_index]
                    if len(row) > display_name_index
                    and row[display_name_index] is not None
                    else ""
                ),
                str(
                    row[encore_roles_index]
                    if len(row) > encore_roles_index
                    and row[encore_roles_index] is not None
                    else ""
                ),
            )
        for username, member in member_by_names.items():
            users[username] = UserInfoWithEncoreRoles(
                username,
                member.display_name,
                UserInfoWithEncoreRoles.roles_to_string(
                    [role.name for role in member.roles if role.id in encore_role_ids]
                ),
            )
        users.update(user_overrides or {})

        reconciliation = SummaryWorksheetContent.plan_reconciliation(
            worksheet_id=summary_worksheet.id,
            headers=summary_headers,
            rows=summary_rows,
            team_worksheets=team_grids,
            users=users,
        )
        row_updates = [
            mutation
            for mutation in reconciliation
            if isinstance(mutation, GridValueUpdate)
        ]
        row_deletions = [
            mutation
            for mutation in reconciliation
            if isinstance(mutation, DimensionMutation)
        ]
        growth = self._plan_grid_growth(
            dimensions,
            column_mutations,
            [*header_updates, *row_updates],
        )
        mutations: list[GridValueUpdate | DimensionMutation] = [
            *column_mutations,
            *growth,
            *header_updates,
            *row_updates,
            *row_deletions,
        ]

        final_headers, final_rows = self._grid_after_mutations(
            original_headers,
            original_rows,
            (*summary_migration, *reconciliation),
        )
        final_index = SummaryWorksheetContent.index_physical_rows(
            final_headers,
            final_rows,
            list(team_grids),
        )
        records = []
        for _, row_number in sorted(
            final_index.row_by_username.items(),
            key=lambda item: item[1],
        ):
            row = final_rows[row_number - 2]
            records.append(
                {
                    header: row[column - 1] if len(row) >= column else ""
                    for header, column in final_index.column_by_header.items()
                }
            )
        final = pd.DataFrame(records, columns=final_index.bot_headers).set_index(
            SummaryWorksheetContent.INDEX_NAME
        )
        final = final.drop(columns=[Summary.original_message_title()])
        team_columns = final.columns.difference(["display_name", "encore_roles"])
        final[team_columns] = final[team_columns].replace("", pd.NA)
        return mutations, final

    async def refresh_summary_registration(
        self,
        member_by_names: dict[str, Member],
    ) -> pd.DataFrame:
        """Reconcile the complete derived Summary in one batch and return it."""
        async with self._prepared_metadata_transaction(
            await self.fetch_google_sheets_metadata(),
            team_count=0,
            validate_summary_sources=True,
        ) as (metadata, grids, side_effect):
            try:
                encore_role_ids = (await self.get_sheet_config()).encore_role_ids
                mutations, final = self._plan_summary_reconciliation(
                    metadata,
                    grids,
                    member_by_names,
                    encore_role_ids,
                )
                sheet = await self.get_google_sheet()
                await sheet.batch_update_grid(mutations)
            except Exception as exc:
                _raise_partial_after_side_effect(exc, side_effect=side_effect)
                raise
            return final

    async def update_encore_role_ids_and_summary(
        self,
        role_ids: list[int],
        member_by_names: dict[str, Member],
    ) -> TeamRegisterGoogleSheetsMetadata:
        """Save proposed Encore role IDs and reconcile Summary in one action."""
        async with self._prepared_metadata_transaction(
            await self.fetch_google_sheets_metadata(),
            team_count=0,
            validate_summary_sources=True,
        ) as (metadata, grids, side_effect):
            try:
                mutations, _ = self._plan_summary_reconciliation(
                    metadata,
                    grids,
                    member_by_names,
                    role_ids,
                )
                sheet = await self.get_google_sheet()
                config = await self.get_sheet_config()
                config.sheet_url = metadata.sheet_url
                config.team_worksheet_ids = [
                    worksheet.id for worksheet in metadata.team_worksheets
                ]
                config.summary_worksheet_id = metadata.summary_worksheet.id
                config.encore_role_ids = role_ids
                await config.save()
            except Exception as exc:
                _raise_partial_after_side_effect(exc, side_effect=side_effect)
                raise
            try:
                await sheet.batch_update_grid(mutations)
            except Exception as exc:
                error = partial_success_storage_error(exc)
                if error is None:
                    raise
                raise error from error.__cause__
            return metadata
