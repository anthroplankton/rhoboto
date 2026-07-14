from __future__ import annotations

import math
import secrets
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from itertools import pairwise
from typing import TYPE_CHECKING, overload, override

from tortoise.transactions import in_transaction

from models.feature_channel import FeatureChannel
from models.shift_register import ShiftRegisterConfig
from models.shift_timeline_event_state import (
    ShiftTimelineEventKind,
    ShiftTimelineEventState,
    ShiftTimelineEventStatus,
)
from models.team_register import TeamRegisterConfig
from utils.google_sheets import BORDER_NAMES, GoogleSheet, WorksheetCreationStatus
from utils.google_sheets_errors import GoogleSheetsError, GoogleSheetsErrorKind
from utils.structs_base import WorksheetContractError, validate_anchor_cell
from utils.team_register_manager import SummaryReconciliationPlan, TeamRegisterManager
from utils.team_register_structs import (
    Summary,
    SummaryWorksheetContent,
    TeamRegisterGoogleSheetsMetadata,
    TeamWorksheetContent,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Hashable, Sequence
    from datetime import date

    from discord import Member

    from utils.google_sheets import AsyncioGspreadWorksheet
    from utils.shift_scheduler import DraftSchedule
    from utils.structs_base import UserInfo

from utils.google_sheets_urls import (
    google_sheet_url_with_gid,
    normalize_google_sheet_url,
)
from utils.key_async_lock import KeyAsyncLock
from utils.manager_base import (
    ManagerBase,
    SheetConfigNotFoundError,
    spreadsheet_structure_transaction,
    worksheet_transaction_key,
    worksheet_transactions,
)
from utils.shift_final import (
    EventDayWriteStatus,
    FinalGenerationRequest,
    FinalScheduleInputError,
    FinalSchedulePlan,
    build_final_schedule,
)
from utils.shift_register_structs import (
    DraftNotesTeamSource,
    DraftWorksheetContent,
    EntryWorksheetContent,
    RecruitmentTimeRanges,
    Shift,
    ShiftParser,
    ShiftRegisterGoogleSheetsMetadata,
    build_team_summary_formula,
    column_letter,
)
from utils.shift_scheduler import DraftTeamProfile, ShiftScheduler
from utils.storage_errors import (
    StorageError,
    StorageErrorKind,
    partial_success_storage_error,
)

SHIFT_REGISTER_SHEET_WRITE_LOCK = KeyAsyncLock()
MAX_DISCORD_NONCE = (1 << 63) - 1
OUTER_BORDER_SIDES = ("top", "bottom", "left", "right")
ENTRY_RULE_MARKER = "rhoboto:shift-entry:"
DRAFT_CANDIDATE_RULE_MARKER = "rhoboto:shift-draft:candidate:"
ENTRY_IDENTITY_LAST_COLUMN = 3
ENTRY_AVAILABILITY_FIRST_COLUMN = 6


def _new_delivery_nonce() -> int:
    return secrets.randbelow(MAX_DISCORD_NONCE) + 1


def _is_future_deadline(deadline: datetime | None, now: datetime) -> bool:
    return deadline is not None and deadline > now


def _deadline_schedule_change(
    shift_register_id: int,
    *,
    scheduled_at: datetime | None = None,
    delivery_nonce: int | None = None,
) -> ShiftTimelineScheduleChange:
    return ShiftTimelineScheduleChange(
        shift_register_id=shift_register_id,
        event_kind=ShiftTimelineEventKind.SUBMISSION_DEADLINE,
        scheduled_at=scheduled_at,
        delivery_nonce=delivery_nonce,
    )


@asynccontextmanager
async def fresh_shift_channel_transaction(
    manager: ShiftRegisterManager,
    feature_channel_lock: KeyAsyncLock,
    *,
    channel_id: Hashable,
) -> AsyncIterator[ShiftRegisterConfig]:
    """Lock the Shift channel and refresh its current Sheet configuration."""
    async with feature_channel_lock(channel_id):
        config = await manager.get_fresh_sheet_config()
        if config is None:
            raise SheetConfigNotFoundError(manager.feature_channel)
        yield config


class TeamSourceStatus(StrEnum):
    AVAILABLE = "available"
    UNSET = "unset"
    MISSING = "missing"
    AMBIGUOUS = "ambiguous"
    INVALID = "invalid"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class TeamSummaryColumns:
    username: int
    roles: int
    main_isv: int
    main_power: int | None
    encore_isv: int | None
    encore_power: int | None
    import_last_column: str


@dataclass(frozen=True)
class TeamSource:
    config: TeamRegisterConfig
    metadata: TeamRegisterGoogleSheetsMetadata
    summary_columns: TeamSummaryColumns


@dataclass(frozen=True)
class TeamSourceResolution:
    status: TeamSourceStatus
    source: TeamSource | None = None


@dataclass(frozen=True)
class DraftTeamProfileResolution:
    status: TeamSourceStatus
    profiles: dict[str, DraftTeamProfile]
    notes_team_source: DraftNotesTeamSource | None = None


class AutoCloseDeadlineNotFutureError(ValueError):
    """Raised when Auto Close is enabled without a future deadline."""


@dataclass(frozen=True)
class ShiftTimelineScheduleChange:
    shift_register_id: int
    event_kind: ShiftTimelineEventKind
    scheduled_at: datetime | None
    delivery_nonce: int | None


@dataclass(frozen=True)
class ShiftTimelineUpdateResult:
    schedule_change: ShiftTimelineScheduleChange | None = None
    auto_close_disabled: bool = False


@dataclass(frozen=True)
class ShiftDeadlineExecution:
    event_state_id: int
    shift_register_id: int
    guild_id: int
    channel_id: int
    delivery_nonce: int
    status: ShiftTimelineEventStatus
    message_id: int | None


@dataclass(frozen=True)
class DraftGenerationResult:
    schedule: DraftSchedule
    team_source_status: TeamSourceStatus
    team_source_warning: str | None
    recruitment_ranges: RecruitmentTimeRanges
    notes_snapshot: str
    unregistered_usernames: tuple[str, ...] = ()
    team_summary_url: str | None = None


class FinalScheduleReconfirmationRequired(Exception):  # noqa: N818
    """Raised after missing Final inputs are repaired and need reconfirmation."""


@dataclass(frozen=True)
class FinalGenerationResult:
    request: FinalGenerationRequest
    schedule: FinalSchedulePlan


@dataclass(frozen=True)
class EntryPresentationPlan:
    format_updates: tuple[tuple[str, dict[str, object], str], ...]
    border_updates: tuple[tuple[str, str, str, tuple[str, ...]], ...]
    column_width_updates: tuple[tuple[str, int], ...]
    hidden_column_updates: tuple[tuple[str, bool], ...]
    conditional_format_rules: tuple[dict[str, object], ...]
    frozen_column_count: int = 5


TEAM_SOURCE_UNSET_DRAFT_WARNING = (
    "⚠️ Team Sourceが未設定のため、今回はISVを使用せず、アンコを空欄にしています。"
)
TEAM_SOURCE_UNAVAILABLE_DRAFT_WARNING = (
    "⚠️🛠️ Team Sourceを読み取れなかったため、今回はISVを使用せず、"
    "アンコを空欄にしています。"
)


class ShiftRegisterManager(
    ManagerBase[ShiftRegisterConfig, ShiftRegisterGoogleSheetsMetadata]
):
    SheetConfigType = ShiftRegisterConfig
    GoogleSheetsMetadataType = ShiftRegisterGoogleSheetsMetadata

    async def _ensure_current_worksheets(
        self,
        metadata: ShiftRegisterGoogleSheetsMetadata,
        *,
        required_worksheets: tuple[AsyncioGspreadWorksheet | None, ...],
    ) -> tuple[ShiftRegisterGoogleSheetsMetadata, bool]:
        if all(worksheet is not None for worksheet in required_worksheets):
            return metadata, False
        async with spreadsheet_structure_transaction(metadata.sheet_url):
            current = await self.fetch_google_sheets_metadata()
            previous_ids = tuple(worksheet.id for worksheet in current)
            ensured = await self.ensure_worksheets_and_upsert_sheet_config(current)
        changed = tuple(worksheet.id for worksheet in ensured) != previous_ids
        return ensured, changed

    async def get_saved_team_source_channel_id(self) -> int | None:
        """Return the Discord channel ID for the saved Team source."""
        config = await self.get_sheet_config()
        source_id = config.team_source_feature_channel_id
        if source_id is None:
            return None
        source = await FeatureChannel.get_or_none(id=source_id)
        return source.channel_id if source is not None else None

    async def get_saved_team_summary_destination(
        self,
    ) -> tuple[TeamSourceStatus, str | None]:
        """Return the DB-configured Summary destination without Sheets access."""
        config = await self.get_sheet_config()
        source_id = config.team_source_feature_channel_id
        if source_id is None:
            return TeamSourceStatus.UNSET, None
        configs = await TeamRegisterConfig.filter(feature_channel_id=source_id)
        if len(configs) != 1:
            return TeamSourceStatus.INVALID, None
        source = configs[0]
        return (
            TeamSourceStatus.AVAILABLE,
            google_sheet_url_with_gid(
                source.sheet_url,
                source.summary_worksheet_id,
            ),
        )

    async def resolve_team_source(
        self,
        *,
        team_channel_id: int | None = None,
    ) -> TeamSourceResolution:
        """Resolve an explicit or saved Team source."""
        status, config, metadata = await self._resolve_team_source_metadata(
            team_channel_id=team_channel_id
        )
        if config is None or metadata is None:
            return TeamSourceResolution(status)
        resource = worksheet_transaction_key(
            config.sheet_url,
            metadata.summary_worksheet.id,
        )
        async with worksheet_transactions([resource]):
            resolution, _summary_grid = await self._read_team_source_locked(
                config,
                metadata,
            )
            return resolution

    async def _resolve_team_source_metadata(
        self,
        *,
        team_channel_id: int | None = None,
    ) -> tuple[
        TeamSourceStatus,
        TeamRegisterConfig | None,
        TeamRegisterGoogleSheetsMetadata | None,
    ]:
        """Resolve Team config and worksheet objects without reading Summary."""
        if team_channel_id is not None:
            filters = {
                "feature_channel__guild_id": self.feature_channel.guild_id,
                "feature_channel__channel_id": team_channel_id,
                "feature_channel__feature_name": "team_register",
            }
            missing_status = TeamSourceStatus.INVALID
        else:
            shift_config = await self.get_sheet_config_or_none()
            selected_id = (
                shift_config.team_source_feature_channel_id
                if shift_config is not None
                else None
            )
            if selected_id is None:
                return TeamSourceStatus.UNSET, None, None
            filters = {"feature_channel_id": selected_id}
            missing_status = TeamSourceStatus.INVALID

        configs = await TeamRegisterConfig.filter(**filters).select_related(
            "feature_channel"
        )
        if not configs:
            return missing_status, None, None
        if len(configs) > 1:
            return TeamSourceStatus.AMBIGUOUS, None, None

        config = configs[0]
        try:
            sheet = GoogleSheet(config.sheet_url, self.service_account_path)
            worksheets = await sheet.get_worksheets(config.get_worksheet_ids())
            metadata = TeamRegisterGoogleSheetsMetadata.from_id_mapping(
                config.sheet_url,
                worksheets,
            )
        except GoogleSheetsError as exc:
            status = (
                TeamSourceStatus.INVALID
                if exc.kind
                in {
                    GoogleSheetsErrorKind.INVALID_URL,
                    GoogleSheetsErrorKind.MISSING_WORKSHEET,
                }
                else TeamSourceStatus.UNRESOLVED
            )
            self.logger.warning(
                "Could not resolve auxiliary Team source: %s",
                exc.kind,
            )
            return status, None, None
        return TeamSourceStatus.AVAILABLE, config, metadata

    def _draft_profiles_from_summary(
        self,
        source: TeamSource,
        summaries: Sequence[Summary],
    ) -> DraftTeamProfileResolution:
        """Project Draft-only values from the shared active Summary snapshot."""
        main_title = source.metadata.team_worksheets[0].title
        summary_title = source.metadata.summary_worksheet.title
        if main_title is None or summary_title is None:
            msg = "Resolved Team Source is missing a worksheet title."
            raise ValueError(msg)
        encore_title = (
            source.metadata.team_worksheets[1].title
            if len(source.metadata.team_worksheets) > 1
            else None
        )
        if encore_title is None and len(source.metadata.team_worksheets) > 1:
            msg = "Resolved Team Source is missing a worksheet title."
            raise ValueError(msg)

        profiles = {
            summary.username: DraftTeamProfile(
                main_isv=_optional_float(
                    getattr(summary, Summary.isv_title(main_title))
                ),
                main_power=_optional_float(
                    getattr(summary, Summary.power_title(main_title))
                ),
                encore_isv=(
                    _optional_float(getattr(summary, Summary.isv_title(encore_title)))
                    if encore_title is not None
                    else None
                ),
                encore_power=(
                    _optional_float(getattr(summary, Summary.power_title(encore_title)))
                    if encore_title is not None
                    else None
                ),
                has_encore_role=bool(summary.encore_roles.strip()),
            )
            for summary in summaries
        }
        columns = source.summary_columns
        return DraftTeamProfileResolution(
            TeamSourceStatus.AVAILABLE,
            profiles,
            DraftNotesTeamSource(
                sheet_url=source.config.sheet_url,
                worksheet_title=summary_title,
                import_last_column=columns.import_last_column,
                username_header="username",
                roles_header="encore_roles",
                main_isv_header=Summary.isv_title(main_title),
                main_power_header=Summary.power_title(main_title),
                encore_isv_header=(
                    Summary.isv_title(encore_title)
                    if encore_title is not None
                    else None
                ),
                encore_power_header=(
                    Summary.power_title(encore_title)
                    if encore_title is not None
                    else None
                ),
            ),
        )

    async def _read_team_source_locked(
        self,
        config: TeamRegisterConfig,
        metadata: TeamRegisterGoogleSheetsMetadata,
    ) -> tuple[TeamSourceResolution, list[list[object]] | None]:
        summary_worksheet = metadata.summary_worksheet.worksheet
        if summary_worksheet is None:
            return TeamSourceResolution(TeamSourceStatus.INVALID), None
        try:
            sheet = GoogleSheet(config.sheet_url, self.service_account_path)
            grids = await sheet.batch_get_worksheet_values([summary_worksheet])
        except GoogleSheetsError as exc:
            self.logger.warning("Could not read auxiliary Team source: %s", exc.kind)
            return TeamSourceResolution(TeamSourceStatus.UNRESOLVED), None
        summary_grid = grids[summary_worksheet.id]
        return self._build_team_source(config, metadata, summary_grid), summary_grid

    async def _read_shift_and_summary_locked(
        self,
        shift_sheet_url: str,
        shift_worksheets: list[AsyncioGspreadWorksheet],
        source_status: TeamSourceStatus,
        source_config: TeamRegisterConfig | None,
        source_metadata: TeamRegisterGoogleSheetsMetadata | None,
    ) -> tuple[
        dict[int, list[list[object]]],
        TeamSourceResolution,
        list[list[object]] | None,
    ]:
        shift_sheet = await self.get_google_sheet()
        summary_worksheet = (
            source_metadata.summary_worksheet.worksheet
            if source_metadata is not None
            else None
        )
        if source_config is None or source_metadata is None:
            grids = await shift_sheet.batch_get_worksheet_values(shift_worksheets)
            return grids, TeamSourceResolution(source_status), None
        if summary_worksheet is None:
            grids = await shift_sheet.batch_get_worksheet_values(shift_worksheets)
            return grids, TeamSourceResolution(TeamSourceStatus.INVALID), None

        source_sheet_url = normalize_google_sheet_url(source_config.sheet_url)
        if source_sheet_url == normalize_google_sheet_url(shift_sheet_url):
            grids = await shift_sheet.batch_get_worksheet_values(
                [*shift_worksheets, summary_worksheet]
            )
            summary_grid = grids[summary_worksheet.id]
            resolution = self._build_team_source(
                source_config,
                source_metadata,
                summary_grid,
            )
            return grids, resolution, summary_grid

        grids = await shift_sheet.batch_get_worksheet_values(shift_worksheets)
        resolution, summary_grid = await self._read_team_source_locked(
            source_config,
            source_metadata,
        )
        return grids, resolution, summary_grid

    async def _read_shift_and_team_source_locked(
        self,
        shift_sheet_url: str,
        shift_worksheets: list[AsyncioGspreadWorksheet],
        source_status: TeamSourceStatus,
        source_config: TeamRegisterConfig | None,
        source_metadata: TeamRegisterGoogleSheetsMetadata | None,
    ) -> tuple[
        dict[int, list[list[object]]],
        TeamSourceResolution,
        dict[int, list[list[object]]] | None,
    ]:
        """Read all confirmed Draft inputs with one batch per spreadsheet."""
        shift_sheet = await self.get_google_sheet()
        if source_config is None or source_metadata is None:
            grids = await shift_sheet.batch_get_worksheet_values(shift_worksheets)
            return grids, TeamSourceResolution(source_status), None

        summary_worksheet = source_metadata.summary_worksheet.worksheet
        team_worksheets = [
            metadata.worksheet
            for metadata in source_metadata.team_worksheets
            if metadata.worksheet is not None
        ]
        if summary_worksheet is None or len(team_worksheets) != len(
            source_metadata.team_worksheets
        ):
            grids = await shift_sheet.batch_get_worksheet_values(shift_worksheets)
            return grids, TeamSourceResolution(TeamSourceStatus.INVALID), None

        source_worksheets = [*team_worksheets, summary_worksheet]
        source_sheet_url = normalize_google_sheet_url(source_config.sheet_url)
        if source_sheet_url == normalize_google_sheet_url(shift_sheet_url):
            grids = await shift_sheet.batch_get_worksheet_values(
                [*shift_worksheets, *source_worksheets]
            )
            source_grids = {
                worksheet.id: grids[worksheet.id] for worksheet in source_worksheets
            }
            resolution = self._build_team_source(
                source_config,
                source_metadata,
                source_grids[summary_worksheet.id],
            )
            return grids, resolution, source_grids

        shift_grids = await shift_sheet.batch_get_worksheet_values(shift_worksheets)
        try:
            source_sheet = GoogleSheet(
                source_config.sheet_url,
                self.service_account_path,
            )
            source_grids = await source_sheet.batch_get_worksheet_values(
                source_worksheets
            )
        except GoogleSheetsError as exc:
            self.logger.warning("Could not read auxiliary Team source: %s", exc.kind)
            return shift_grids, TeamSourceResolution(TeamSourceStatus.UNRESOLVED), None
        resolution = self._build_team_source(
            source_config,
            source_metadata,
            source_grids[summary_worksheet.id],
        )
        return shift_grids, resolution, source_grids

    async def get_team_source_candidate_channel_ids(self) -> tuple[int, ...]:
        """Return same-guild Team Register channels available for UI selection."""
        configs = await TeamRegisterConfig.filter(
            feature_channel__guild_id=self.feature_channel.guild_id,
            feature_channel__feature_name="team_register",
        ).select_related("feature_channel")
        return tuple(config.feature_channel.channel_id for config in configs)

    async def repair_team_references(
        self,
        metadata: ShiftRegisterGoogleSheetsMetadata,
        resolution: TeamSourceResolution,
    ) -> int:
        """Repair changed Team formula anchors for populated Shift Entry rows."""
        if resolution.status is not TeamSourceStatus.AVAILABLE:
            msg = "Team source must be available before repair."
            raise ValueError(msg)
        worksheet = metadata.entry_worksheets.worksheet
        if worksheet is None:
            raise GoogleSheetsError(
                GoogleSheetsErrorKind.MISSING_WORKSHEET,
                "Repair the Shift Register worksheet settings.",
            )
        source = resolution.source
        if source is None:
            msg = "Team source must be available before repair."
            raise ValueError(msg)
        resources = [
            worksheet_transaction_key(metadata.sheet_url, worksheet.id),
            worksheet_transaction_key(
                source.config.sheet_url,
                source.metadata.summary_worksheet.id,
            ),
        ]
        async with worksheet_transactions(resources):
            (
                grids,
                resolution,
                _summary_grid,
            ) = await self._read_shift_and_summary_locked(
                metadata.sheet_url,
                [worksheet],
                resolution.status,
                source.config,
                source.metadata,
            )
            return await self._repair_team_references_locked(
                metadata,
                resolution,
                entry_grid=grids[worksheet.id],
            )

    async def _repair_team_references_locked(
        self,
        metadata: ShiftRegisterGoogleSheetsMetadata,
        resolution: TeamSourceResolution,
        *,
        entry_grid: list[list[object]],
    ) -> int:
        worksheet = metadata.entry_worksheets.worksheet
        if worksheet is None:
            raise GoogleSheetsError(
                GoogleSheetsErrorKind.MISSING_WORKSHEET,
                "Repair the Shift Register worksheet settings.",
            )
        _layout, _values, participants = _entry_state_from_grid(entry_grid)

        updates: list[dict[str, object]] = []
        for row, username, current_formula, _reusable in participants:
            if not username:
                continue
            expected_formula = _entry_team_formula(row, resolution)
            if expected_formula is not None and expected_formula != current_formula:
                updates.append({"range": f"C{row}", "values": [[expected_formula]]})
        if updates:
            await worksheet.batch_update_typed_values(
                updates,
                formula_ranges={str(item["range"]) for item in updates},
            )
        return len(updates)

    async def select_team_source_and_repair(
        self,
        team_channel_id: int,
    ) -> TeamSourceResolution:
        """Persist a valid Team source, then repair Shift Entry references."""
        (
            status,
            source_config,
            source_metadata,
        ) = await self._resolve_team_source_metadata(team_channel_id=team_channel_id)
        if source_config is None or source_metadata is None:
            return TeamSourceResolution(status)

        metadata = await self.fetch_google_sheets_metadata()
        worksheet = metadata.entry_worksheets.worksheet
        if worksheet is None:
            raise GoogleSheetsError(
                GoogleSheetsErrorKind.MISSING_WORKSHEET,
                "Repair the Shift Register worksheet settings.",
            )
        resources = [
            worksheet_transaction_key(metadata.sheet_url, worksheet.id),
            worksheet_transaction_key(
                source_config.sheet_url,
                source_metadata.summary_worksheet.id,
            ),
        ]
        async with worksheet_transactions(resources):
            (
                grids,
                resolution,
                _summary_grid,
            ) = await self._read_shift_and_summary_locked(
                metadata.sheet_url,
                [worksheet],
                status,
                source_config,
                source_metadata,
            )
            source = resolution.source
            if resolution.status is not TeamSourceStatus.AVAILABLE or source is None:
                return resolution
            entry_grid = grids[worksheet.id]
            _entry_state_from_grid(entry_grid)
            config = await self.get_sheet_config()
            config.team_source_feature_channel_id = source.config.feature_channel.id
            await config.save(
                update_fields=["team_source_feature_channel_id", "updated_at"]
            )
            try:
                await self._repair_team_references_locked(
                    metadata,
                    resolution,
                    entry_grid=entry_grid,
                )
            except Exception as exc:
                partial = partial_success_storage_error(exc)
                if partial is None:
                    raise
                raise partial from partial.__cause__
            return resolution

    def _build_team_source(
        self,
        config: TeamRegisterConfig,
        metadata: TeamRegisterGoogleSheetsMetadata,
        summary_grid: list[list[object]],
    ) -> TeamSourceResolution:
        landing_worksheet = next(
            (
                worksheet
                for worksheet in metadata
                if worksheet.id == config.landing_worksheet_id
            ),
            None,
        )
        if (
            not metadata.team_worksheets
            or any(worksheet.is_missing() for worksheet in metadata)
            or landing_worksheet is None
        ):
            return TeamSourceResolution(TeamSourceStatus.INVALID)

        header = summary_grid[0] if summary_grid else []
        if not isinstance(header, list):
            return TeamSourceResolution(TeamSourceStatus.INVALID)

        main_worksheet = metadata.team_worksheets[0]
        encore_worksheet = (
            metadata.team_worksheets[1] if len(metadata.team_worksheets) > 1 else None
        )
        try:
            terminal_column = _unique_header_column(
                header,
                Summary.original_message_title(),
            )
            columns = TeamSummaryColumns(
                username=_unique_header_column(header, "username"),
                roles=_unique_header_column(header, "encore_roles"),
                main_isv=_unique_header_column(
                    header,
                    Summary.isv_title(main_worksheet.title),
                ),
                main_power=_optional_unique_header_column(
                    header,
                    Summary.power_title(main_worksheet.title),
                ),
                encore_isv=(
                    _unique_header_column(
                        header,
                        Summary.isv_title(encore_worksheet.title),
                    )
                    if encore_worksheet is not None
                    else None
                ),
                encore_power=(
                    _optional_unique_header_column(
                        header,
                        Summary.power_title(encore_worksheet.title),
                    )
                    if encore_worksheet is not None
                    else None
                ),
                import_last_column=column_letter(terminal_column),
            )
        except ValueError:
            return TeamSourceResolution(TeamSourceStatus.INVALID)
        if any(
            column is not None and column > terminal_column
            for column in (
                columns.username,
                columns.roles,
                columns.main_isv,
                columns.main_power,
                columns.encore_isv,
                columns.encore_power,
            )
        ):
            return TeamSourceResolution(TeamSourceStatus.INVALID)

        return TeamSourceResolution(
            TeamSourceStatus.AVAILABLE,
            TeamSource(
                config=config,
                metadata=metadata,
                summary_columns=columns,
            ),
        )

    @overload
    async def upsert_sheet_config_and_worksheets(
        self,
        sheet_url: str,
        worksheet_titles: list[str],
    ) -> ShiftRegisterGoogleSheetsMetadata: ...
    @overload
    async def upsert_sheet_config_and_worksheets(
        self,
        sheet_url: str,
        *,
        entry_worksheet_title: str,
        draft_worksheet_title: str,
        final_schedule_worksheet_title: str,
        final_schedule_anchor_cell: str | None = None,
    ) -> ShiftRegisterGoogleSheetsMetadata: ...
    @override
    async def upsert_sheet_config_and_worksheets(
        self,
        sheet_url: str,
        worksheet_titles: list[str] | None = None,
        *,
        entry_worksheet_title: str | None = None,
        draft_worksheet_title: str | None = None,
        final_schedule_worksheet_title: str | None = None,
        final_schedule_anchor_cell: str | None = None,
    ) -> ShiftRegisterGoogleSheetsMetadata:
        worksheet_titles = worksheet_titles or []
        if any(
            title is not None
            for title in (
                entry_worksheet_title,
                draft_worksheet_title,
                final_schedule_worksheet_title,
            )
        ):
            worksheet_titles = [
                entry_worksheet_title or "",
                draft_worksheet_title or "",
                final_schedule_worksheet_title or "",
            ]
        expected = self.GoogleSheetsMetadataType.WORKSHEET_METADATA_TYPES
        if (
            len(worksheet_titles) != len(expected)
            or any(not title.strip() for title in worksheet_titles)
            or len(set(worksheet_titles)) != len(worksheet_titles)
        ):
            raise WorksheetContractError(log_hint="invalid_worksheet_titles")
        sheet_url = normalize_google_sheet_url(sheet_url)
        self._google_sheet = GoogleSheet(sheet_url, self.service_account_path)
        creation_status = WorksheetCreationStatus()
        config_saved = False
        try:
            entry_title, draft_title, final_title = worksheet_titles
            async with spreadsheet_structure_transaction(sheet_url):
                entry_mapping = await self._google_sheet.get_or_create_worksheets(
                    [entry_title],
                    creation_status=creation_status,
                )
            entry_worksheet = entry_mapping[entry_title]
            entry_resource = worksheet_transaction_key(
                sheet_url,
                entry_worksheet.id,
            )
            async with worksheet_transactions([entry_resource]):
                entry_grids = await self._google_sheet.batch_get_worksheet_values(
                    [entry_worksheet]
                )
                _entry_state_from_grid(entry_grids[entry_worksheet.id])
            async with spreadsheet_structure_transaction(sheet_url):
                remaining_mapping = await self._google_sheet.get_or_create_worksheets(
                    [draft_title, final_title],
                    creation_status=creation_status,
                )
                metadata = self.GoogleSheetsMetadataType.from_title_mapping(
                    sheet_url,
                    {**entry_mapping, **remaining_mapping},
                )
                if final_schedule_anchor_cell is None:
                    await self.upsert_sheet_config(metadata)
                else:
                    await self.upsert_sheet_config(
                        metadata,
                        extra_defaults={
                            "final_schedule_anchor_cell": validate_anchor_cell(
                                final_schedule_anchor_cell
                            )
                        },
                    )
                config_saved = True
            async with worksheet_transactions([entry_resource]):
                config = await self.get_sheet_config()
                ranges = RecruitmentTimeRanges.from_json(config.recruitment_time_ranges)
                entry_grids = await self._google_sheet.batch_get_worksheet_values(
                    [entry_worksheet]
                )
                await self._sync_entry_presentation_locked(
                    metadata,
                    ranges,
                    entry_grid=entry_grids[entry_worksheet.id],
                    force=True,
                )
        except Exception as exc:
            if creation_status.created or config_saved:
                partial = partial_success_storage_error(exc)
                if partial is not None:
                    raise partial from partial.__cause__
            raise
        return metadata

    async def update_timeline(  # noqa: PLR0913
        self,
        *,
        day_number: int | None,
        event_date: date | None,
        submission_deadline_at: datetime | None,
        draft_shift_proposal_at: datetime | None,
        final_shift_notice_at: datetime | None,
        now: datetime | None = None,
    ) -> ShiftTimelineUpdateResult:
        effective_now = now or datetime.now(UTC)
        async with in_transaction() as connection:
            shift_register_config = await self._get_locked_shift_config(connection)
            deadline_changed = (
                shift_register_config.submission_deadline_at != submission_deadline_at
            )
            shift_register_config.day_number = day_number
            shift_register_config.event_date = event_date
            shift_register_config.submission_deadline_at = submission_deadline_at
            shift_register_config.draft_shift_proposal_at = draft_shift_proposal_at
            shift_register_config.final_shift_notice_at = final_shift_notice_at
            timeline_fields = [
                "day_number",
                "event_date",
                "submission_deadline_at",
                "draft_shift_proposal_at",
                "final_shift_notice_at",
                "updated_at",
            ]

            if shift_register_config.deadline_automation_enabled and not (
                _is_future_deadline(submission_deadline_at, effective_now)
            ):
                shift_register_config.deadline_automation_enabled = False
                timeline_fields.append("deadline_automation_enabled")
                await shift_register_config.save(
                    using_db=connection,
                    update_fields=timeline_fields,
                )
                await self._delete_deadline_event(
                    shift_register_config.id,
                    connection,
                )
                self._sheet_config = shift_register_config
                return ShiftTimelineUpdateResult(
                    schedule_change=_deadline_schedule_change(shift_register_config.id),
                    auto_close_disabled=True,
                )

            await shift_register_config.save(
                using_db=connection,
                update_fields=timeline_fields,
            )
            self._sheet_config = shift_register_config

            if (
                not shift_register_config.deadline_automation_enabled
                or not deadline_changed
            ):
                return ShiftTimelineUpdateResult()

            state = await self._reset_deadline_event(
                shift_register_config,
                connection,
            )
            return ShiftTimelineUpdateResult(
                schedule_change=_deadline_schedule_change(
                    shift_register_config.id,
                    scheduled_at=state.scheduled_at,
                    delivery_nonce=state.delivery_nonce,
                )
            )

    async def set_deadline_automation_enabled(
        self,
        *,
        enabled: bool,
        now: datetime,
    ) -> ShiftTimelineScheduleChange:
        async with in_transaction() as connection:
            shift_register_config = await self._get_locked_shift_config(connection)
            if enabled and not _is_future_deadline(
                shift_register_config.submission_deadline_at,
                now,
            ):
                raise AutoCloseDeadlineNotFutureError

            shift_register_config.deadline_automation_enabled = enabled
            await shift_register_config.save(
                using_db=connection,
                update_fields=["deadline_automation_enabled", "updated_at"],
            )
            self._sheet_config = shift_register_config
            if not enabled:
                await self._delete_deadline_event(
                    shift_register_config.id,
                    connection,
                )
                return _deadline_schedule_change(shift_register_config.id)

            state = await self._reset_deadline_event(
                shift_register_config,
                connection,
            )
            return _deadline_schedule_change(
                shift_register_config.id,
                scheduled_at=state.scheduled_at,
                delivery_nonce=state.delivery_nonce,
            )

    async def reconcile_deadline_automation(
        self,
        *,
        now: datetime,
    ) -> ShiftTimelineUpdateResult:
        async with in_transaction() as connection:
            shift_register_config = await self._get_locked_shift_config(connection)
            state = await self._get_locked_deadline_event(
                shift_register_config.id,
                connection,
            )
            deadline = shift_register_config.submission_deadline_at
            if shift_register_config.deadline_automation_enabled:
                matching_active_state = (
                    state is not None
                    and state.status
                    in (
                        ShiftTimelineEventStatus.SCHEDULED,
                        ShiftTimelineEventStatus.SENT,
                    )
                    and state.scheduled_at == deadline
                )
                if not matching_active_state and not _is_future_deadline(deadline, now):
                    shift_register_config.deadline_automation_enabled = False
                    await shift_register_config.save(
                        using_db=connection,
                        update_fields=["deadline_automation_enabled", "updated_at"],
                    )
                    if state is not None:
                        await state.delete(using_db=connection)
                    self._sheet_config = shift_register_config
                    return ShiftTimelineUpdateResult(
                        schedule_change=_deadline_schedule_change(
                            shift_register_config.id
                        ),
                        auto_close_disabled=True,
                    )

                if state is None:
                    state = await self._reset_deadline_event(
                        shift_register_config,
                        connection,
                    )
                    return ShiftTimelineUpdateResult(
                        schedule_change=_deadline_schedule_change(
                            shift_register_config.id,
                            scheduled_at=state.scheduled_at,
                            delivery_nonce=state.delivery_nonce,
                        )
                    )
                if state.scheduled_at != deadline:
                    state = await self._reset_deadline_event(
                        shift_register_config,
                        connection,
                        state=state,
                    )
                    return ShiftTimelineUpdateResult(
                        schedule_change=_deadline_schedule_change(
                            shift_register_config.id,
                            scheduled_at=state.scheduled_at,
                            delivery_nonce=state.delivery_nonce,
                        )
                    )
                return ShiftTimelineUpdateResult()

            if (
                state is not None
                and state.status is not ShiftTimelineEventStatus.COMPLETED
            ):
                await state.delete(using_db=connection)
                return ShiftTimelineUpdateResult(
                    schedule_change=_deadline_schedule_change(shift_register_config.id)
                )
            return ShiftTimelineUpdateResult()

    async def begin_submission_deadline_close(  # noqa: PLR0911
        self,
        *,
        expected_scheduled_at: datetime,
        expected_delivery_nonce: int,
        now: datetime,
    ) -> ShiftDeadlineExecution | None:
        async with in_transaction() as connection:
            shift_register_config = await self._get_locked_shift_config(connection)
            state = await self._get_locked_deadline_event(
                shift_register_config.id,
                connection,
            )
            if state is None or state.status is ShiftTimelineEventStatus.COMPLETED:
                return None
            if not shift_register_config.deadline_automation_enabled:
                return None
            if shift_register_config.submission_deadline_at != state.scheduled_at:
                return None
            if state.scheduled_at != expected_scheduled_at:
                return None
            if state.delivery_nonce != expected_delivery_nonce:
                return None
            if state.scheduled_at > now:
                return None

            if state.status is ShiftTimelineEventStatus.SCHEDULED:
                feature_channel = await self._get_locked_feature_channel(
                    shift_register_config.feature_channel_id,
                    connection,
                )
                feature_channel.is_enabled = False
                await feature_channel.save(
                    using_db=connection,
                    update_fields=["is_enabled", "updated_at"],
                )

            feature_channel = await self._get_locked_feature_channel(
                shift_register_config.feature_channel_id,
                connection,
            )
            return ShiftDeadlineExecution(
                event_state_id=state.id,
                shift_register_id=shift_register_config.id,
                guild_id=feature_channel.guild_id,
                channel_id=feature_channel.channel_id,
                delivery_nonce=state.delivery_nonce,
                status=state.status,
                message_id=state.message_id,
            )

    async def mark_submission_deadline_sent(
        self,
        *,
        event_state_id: int,
        delivery_nonce: int,
        message_id: int,
    ) -> bool:
        async with in_transaction() as connection:
            state = await self._get_locked_deadline_event_by_id(
                event_state_id,
                connection,
            )
            if state is None or state.delivery_nonce != delivery_nonce:
                return False
            if state.status is ShiftTimelineEventStatus.SENT:
                return state.message_id == message_id
            if state.status is not ShiftTimelineEventStatus.SCHEDULED:
                return False
            state.status = ShiftTimelineEventStatus.SENT
            state.message_id = message_id
            await state.save(
                using_db=connection,
                update_fields=["status", "message_id", "updated_at"],
            )
            return True

    async def complete_submission_deadline(
        self,
        *,
        event_state_id: int,
        delivery_nonce: int,
    ) -> bool:
        async with in_transaction() as connection:
            state = await self._get_locked_deadline_event_by_id(
                event_state_id,
                connection,
            )
            if (
                state is None
                or state.delivery_nonce != delivery_nonce
                or state.status is not ShiftTimelineEventStatus.SENT
            ):
                return False
            shift_register_config = await self._get_locked_shift_config_by_id(
                state.shift_register_id,
                connection,
            )
            shift_register_config.deadline_automation_enabled = False
            await shift_register_config.save(
                using_db=connection,
                update_fields=["deadline_automation_enabled", "updated_at"],
            )
            state.status = ShiftTimelineEventStatus.COMPLETED
            await state.save(
                using_db=connection,
                update_fields=["status", "updated_at"],
            )
            self._sheet_config = shift_register_config
            return True

    async def set_manual_feature_enabled(self, *, enabled: bool) -> int | None:
        async with in_transaction() as connection:
            feature_channel = await self._get_locked_feature_channel(
                self.feature_channel.id,
                connection,
                required=False,
            )
            if feature_channel is None:
                return None
            shift_register_config = await self._get_locked_shift_config(
                connection,
                required=False,
            )
            feature_channel.is_enabled = enabled
            await feature_channel.save(
                using_db=connection,
                update_fields=["is_enabled", "updated_at"],
            )
            if shift_register_config is None:
                self._sheet_config = None
                return None
            shift_register_config.deadline_automation_enabled = False
            await shift_register_config.save(
                using_db=connection,
                update_fields=["deadline_automation_enabled", "updated_at"],
            )
            await self._delete_deadline_event(
                shift_register_config.id,
                connection,
            )
            self._sheet_config = shift_register_config
            return shift_register_config.id

    async def clear_feature_settings(self) -> int | None:
        async with in_transaction() as connection:
            shift_register_config = await self._get_locked_shift_config(
                connection,
                required=False,
            )
            config_id = shift_register_config.id if shift_register_config else None
            feature_channel = await self._get_locked_feature_channel(
                self.feature_channel.id,
                connection,
                required=False,
            )
            if feature_channel is not None:
                await feature_channel.delete(using_db=connection)
            self._sheet_config = None
            return config_id

    async def _get_locked_shift_config(
        self,
        connection: object,
        *,
        required: bool = True,
    ) -> ShiftRegisterConfig | None:
        config = await self._get_locked_shift_config_by_feature_channel(
            self.feature_channel.id,
            connection,
        )
        if config is None and required:
            raise SheetConfigNotFoundError(self.feature_channel)
        return config

    async def _get_locked_shift_config_by_feature_channel(
        self,
        feature_channel_id: int,
        connection: object,
    ) -> ShiftRegisterConfig | None:
        return await (
            ShiftRegisterConfig.filter(feature_channel_id=feature_channel_id)
            .using_db(connection)
            .select_for_update()
            .first()
        )

    async def _get_locked_shift_config_by_id(
        self,
        config_id: int,
        connection: object,
    ) -> ShiftRegisterConfig:
        config = await (
            ShiftRegisterConfig.filter(id=config_id)
            .using_db(connection)
            .select_for_update()
            .first()
        )
        if config is None:
            raise SheetConfigNotFoundError(self.feature_channel)
        return config

    async def _get_locked_feature_channel(
        self,
        feature_channel_id: int,
        connection: object,
        *,
        required: bool = True,
    ) -> FeatureChannel | None:
        feature_channel = await (
            FeatureChannel.filter(id=feature_channel_id)
            .using_db(connection)
            .select_for_update()
            .first()
        )
        if feature_channel is None and required:
            raise SheetConfigNotFoundError(self.feature_channel)
        return feature_channel

    async def _get_locked_deadline_event(
        self,
        shift_register_id: int,
        connection: object,
    ) -> ShiftTimelineEventState | None:
        return await (
            ShiftTimelineEventState.filter(
                shift_register_id=shift_register_id,
                event_kind=ShiftTimelineEventKind.SUBMISSION_DEADLINE,
            )
            .using_db(connection)
            .select_for_update()
            .first()
        )

    async def _get_locked_deadline_event_by_id(
        self,
        event_state_id: int,
        connection: object,
    ) -> ShiftTimelineEventState | None:
        return await (
            ShiftTimelineEventState.filter(
                id=event_state_id,
                event_kind=ShiftTimelineEventKind.SUBMISSION_DEADLINE,
                shift_register__feature_channel_id=self.feature_channel.id,
            )
            .using_db(connection)
            .select_for_update()
            .first()
        )

    async def _reset_deadline_event(
        self,
        shift_register_config: ShiftRegisterConfig,
        connection: object,
        *,
        state: ShiftTimelineEventState | None = None,
    ) -> ShiftTimelineEventState:
        deadline = shift_register_config.submission_deadline_at
        if deadline is None:
            raise AutoCloseDeadlineNotFutureError
        state = state or await self._get_locked_deadline_event(
            shift_register_config.id,
            connection,
        )
        if state is None:
            return await ShiftTimelineEventState.create(
                using_db=connection,
                shift_register=shift_register_config,
                event_kind=ShiftTimelineEventKind.SUBMISSION_DEADLINE,
                scheduled_at=deadline,
                delivery_nonce=_new_delivery_nonce(),
                status=ShiftTimelineEventStatus.SCHEDULED,
                message_id=None,
            )
        state.scheduled_at = deadline
        state.delivery_nonce = _new_delivery_nonce()
        state.status = ShiftTimelineEventStatus.SCHEDULED
        state.message_id = None
        await state.save(
            using_db=connection,
            update_fields=[
                "scheduled_at",
                "delivery_nonce",
                "status",
                "message_id",
                "updated_at",
            ],
        )
        return state

    async def _delete_deadline_event(
        self,
        shift_register_id: int,
        connection: object,
    ) -> None:
        await (
            ShiftTimelineEventState.filter(
                shift_register_id=shift_register_id,
                event_kind=ShiftTimelineEventKind.SUBMISSION_DEADLINE,
            )
            .using_db(connection)
            .delete()
        )

    async def update_recruitment_time_ranges(
        self,
        ranges: RecruitmentTimeRanges,
    ) -> None:
        metadata = await self.fetch_google_sheets_metadata()
        worksheet = metadata.entry_worksheets.worksheet
        if worksheet is None:
            raise GoogleSheetsError(
                GoogleSheetsErrorKind.MISSING_WORKSHEET,
                "Repair the Shift Register worksheet settings.",
            )
        resource = worksheet_transaction_key(metadata.sheet_url, worksheet.id)
        async with worksheet_transactions([resource]):
            await self._update_recruitment_time_ranges_locked(metadata, ranges)

    async def _update_recruitment_time_ranges_locked(
        self,
        metadata: ShiftRegisterGoogleSheetsMetadata,
        ranges: RecruitmentTimeRanges,
    ) -> None:
        worksheet = metadata.entry_worksheets.worksheet
        if worksheet is None:
            raise GoogleSheetsError(
                GoogleSheetsErrorKind.MISSING_WORKSHEET,
                "Repair the Shift Register worksheet settings.",
            )
        sheet = await self.get_google_sheet()
        entry_grids = await sheet.batch_get_worksheet_values([worksheet])
        entry_grid = entry_grids[worksheet.id]
        _entry_state_from_grid(entry_grid)
        shift_register_config = await self.get_sheet_config()
        shift_register_config.recruitment_time_ranges = ranges.to_json()
        await shift_register_config.save(
            update_fields=["recruitment_time_ranges", "updated_at"]
        )
        try:
            await self._sync_entry_presentation_locked(
                metadata,
                ranges,
                entry_grid=entry_grid,
                force=True,
            )
        except Exception as exc:
            partial = partial_success_storage_error(exc)
            if partial is None:
                raise
            raise partial from partial.__cause__

    async def sync_entry_presentation(
        self,
        metadata: ShiftRegisterGoogleSheetsMetadata,
        ranges: RecruitmentTimeRanges,
        *,
        force: bool,
    ) -> None:
        """Initialize or repair Shift Entry presentation."""
        worksheet = metadata.entry_worksheets.worksheet
        if worksheet is None:
            raise GoogleSheetsError(
                GoogleSheetsErrorKind.MISSING_WORKSHEET,
                "Repair the Shift Register worksheet settings.",
            )
        resource = worksheet_transaction_key(metadata.sheet_url, worksheet.id)
        async with worksheet_transactions([resource]):
            sheet = await self.get_google_sheet()
            entry_grids = await sheet.batch_get_worksheet_values([worksheet])
            await self._sync_entry_presentation_locked(
                metadata,
                ranges,
                entry_grid=entry_grids[worksheet.id],
                force=force,
            )

    async def _sync_entry_presentation_locked(
        self,
        metadata: ShiftRegisterGoogleSheetsMetadata,
        ranges: RecruitmentTimeRanges,
        *,
        entry_grid: list[list[object]],
        force: bool,
    ) -> None:
        worksheet = metadata.entry_worksheets.worksheet
        if worksheet is None:
            raise GoogleSheetsError(
                GoogleSheetsErrorKind.MISSING_WORKSHEET,
                "Repair the Shift Register worksheet settings.",
            )
        layout_updates, _identity_rows, _participants = _entry_state_from_grid(
            entry_grid
        )

        presentation = _entry_presentation_plan(ranges, worksheet_id=worksheet.id)
        current_rules = await worksheet.get_conditional_format_rules()
        rule_deletes, rule_adds, presentation_is_current = _entry_rule_updates(
            current_rules,
            presentation.conditional_format_rules,
        )
        if presentation_is_current and not force and not layout_updates:
            return
        await worksheet.batch_update_typed_values(
            layout_updates,
            formula_ranges={
                str(item["range"])
                for item in layout_updates
                if item["range"] == "F1:AI1"
            },
            border_updates=presentation.border_updates,
            format_updates=presentation.format_updates,
            column_width_updates=presentation.column_width_updates,
            hidden_column_updates=presentation.hidden_column_updates,
            conditional_format_rule_deletes=rule_deletes,
            conditional_format_rule_adds=rule_adds,
            frozen_column_count=presentation.frozen_column_count,
            min_rows=EntryWorksheetContent.FIRST_DATA_ROW,
            min_cols=EntryWorksheetContent.COLUMN_COUNT,
        )

    async def upsert_or_delete_user_shift(
        self,
        user: UserInfo,
        shift: Shift | None,
        metadata: ShiftRegisterGoogleSheetsMetadata,
        *,
        recruitment_ranges: RecruitmentTimeRanges | None = None,
    ) -> None:
        structure_changed = False
        if shift is not None:
            metadata, structure_changed = await self._ensure_current_worksheets(
                metadata,
                required_worksheets=(metadata.entry_worksheets.worksheet,),
            )
        worksheet = metadata.entry_worksheets.worksheet
        if worksheet is None and shift is None:
            return
        if worksheet is None:
            self.logger.warning(
                "Skipped shift registration for %r: worksheet is not available.",
                shift,
            )
            return

        entry_resource = worksheet_transaction_key(metadata.sheet_url, worksheet.id)
        if shift is None:
            source_status = TeamSourceStatus.UNSET
            source_config = None
            source_metadata = None
        else:
            (
                source_status,
                source_config,
                source_metadata,
            ) = await self._resolve_team_source_metadata()
        resources = [entry_resource]
        if source_config is not None and source_metadata is not None:
            resources.append(
                worksheet_transaction_key(
                    source_config.sheet_url,
                    source_metadata.summary_worksheet.id,
                )
            )
        try:
            async with worksheet_transactions(resources):
                (
                    grids,
                    resolution,
                    _summary_grid,
                ) = await self._read_shift_and_summary_locked(
                    metadata.sheet_url,
                    [worksheet],
                    source_status,
                    source_config,
                    source_metadata,
                )
                await self._upsert_or_delete_user_shift_locked(
                    user,
                    shift,
                    metadata,
                    resolution,
                    entry_grid=grids[worksheet.id],
                    recruitment_ranges=recruitment_ranges,
                )
        except Exception as exc:
            if structure_changed and (partial := partial_success_storage_error(exc)):
                raise partial from partial.__cause__
            raise

    async def _upsert_or_delete_user_shift_locked(  # noqa: PLR0913
        self,
        user: UserInfo,
        shift: Shift | None,
        metadata: ShiftRegisterGoogleSheetsMetadata,
        resolution: TeamSourceResolution,
        *,
        entry_grid: list[list[object]],
        recruitment_ranges: RecruitmentTimeRanges | None = None,
    ) -> None:
        worksheet = metadata.entry_worksheets.worksheet
        if worksheet is None and shift is None:
            return
        if worksheet is None:
            self.logger.warning(
                "Skipped shift registration for %r: worksheet is not available.",
                shift,
            )
            return

        layout_updates, participant_values, participants = _entry_state_from_grid(
            entry_grid
        )

        if shift is None:
            matched = next(
                (
                    row
                    for row, username, _formula, _reusable in participants
                    if username == user.username
                ),
                None,
            )
            if matched is None:
                return
            await worksheet.delete_row(matched)
            self.logger.info(
                "Deleted shift registration for user %r from worksheet `%s`",
                user,
                worksheet.title,
            )
            return

        matched = next(
            (
                row
                for row, username, _formula, _reusable in participants
                if username == user.username
            ),
            None,
        )
        target_row = matched or next(
            (row for row, _username, _formula, reusable in participants if reusable),
            EntryWorksheetContent.FIRST_DATA_ROW + len(participant_values),
        )
        current_formulas = {
            row: formula for row, _username, formula, _reusable in participants
        }
        updates = list(layout_updates)
        for row, username, current_formula, _reusable in participants:
            if not username or row == target_row:
                continue
            expected_formula = _entry_team_formula(row, resolution)
            if expected_formula is not None and current_formula != expected_formula:
                updates.append({"range": f"C{row}", "values": [[expected_formula]]})

        updates.append(
            {
                "range": f"A{target_row}:B{target_row}",
                "values": [[shift.username, shift.display_name]],
            }
        )
        expected_target_formula = _entry_team_formula(target_row, resolution)
        if (
            expected_target_formula is not None
            and current_formulas.get(target_row, "") != expected_target_formula
        ):
            updates.append(
                {"range": f"C{target_row}", "values": [[expected_target_formula]]}
            )
        updates.extend(
            EntryWorksheetContent.shift_value_ranges(shift, row=target_row)[1:]
        )

        presentation = _entry_presentation_plan(
            recruitment_ranges or RecruitmentTimeRanges.default(),
            worksheet_id=worksheet.id,
        )
        current_rules = await worksheet.get_conditional_format_rules()
        rule_deletes, rule_adds, presentation_is_current = _entry_rule_updates(
            current_rules,
            presentation.conditional_format_rules,
        )

        formula_ranges = {
            str(item["range"])
            for item in updates
            if item["range"] == "F1:AI1" or str(item["range"]).startswith("C")
        }
        await worksheet.batch_update_typed_values(
            updates,
            formula_ranges=formula_ranges,
            border_updates=(
                () if presentation_is_current else presentation.border_updates
            ),
            format_updates=(
                () if presentation_is_current else presentation.format_updates
            ),
            column_width_updates=(
                () if presentation_is_current else presentation.column_width_updates
            ),
            hidden_column_updates=(
                () if presentation_is_current else presentation.hidden_column_updates
            ),
            conditional_format_rule_deletes=rule_deletes,
            conditional_format_rule_adds=rule_adds,
            frozen_column_count=(
                None if presentation_is_current else presentation.frozen_column_count
            ),
            min_rows=max(target_row, EntryWorksheetContent.FIRST_DATA_ROW),
            min_cols=EntryWorksheetContent.COLUMN_COUNT,
        )

        self.logger.info(
            "Updated shift registration %r in worksheet `%s`",
            shift,
            worksheet.title,
        )

    async def generate_final(
        self,
        metadata: ShiftRegisterGoogleSheetsMetadata,
        *,
        request: FinalGenerationRequest,
    ) -> FinalGenerationResult:
        metadata, structure_changed = await self._ensure_current_worksheets(
            metadata,
            required_worksheets=(
                metadata.draft_worksheet.worksheet,
                metadata.final_schedule_worksheet.worksheet,
            ),
        )
        if structure_changed:
            raise FinalScheduleReconfirmationRequired

        draft_worksheet = metadata.draft_worksheet.worksheet
        final_worksheet = metadata.final_schedule_worksheet.worksheet
        if draft_worksheet is None or final_worksheet is None:
            raise StorageError(StorageErrorKind.GOOGLE_SHEETS_MISSING_WORKSHEET)

        resources = [
            worksheet_transaction_key(metadata.sheet_url, draft_worksheet.id),
            worksheet_transaction_key(metadata.sheet_url, final_worksheet.id),
        ]
        async with worksheet_transactions(resources):
            sheet = await self.get_google_sheet()
            grids = await sheet.batch_get_worksheet_values([draft_worksheet])
            schedule = build_final_schedule(grids[draft_worksheet.id], request)
            requests = _final_typed_requests(final_worksheet, request, schedule)
            await sheet.batch_update_grid((), worksheet_requests=requests)
            await self._persist_generated_final_anchor(request.anchor_to_persist)
        return FinalGenerationResult(request=request, schedule=schedule)

    async def _persist_generated_final_anchor(self, anchor: str | None) -> None:
        if anchor is None:
            return
        config = await self.get_sheet_config()
        config.final_schedule_anchor_cell = anchor
        try:
            await config.save(
                update_fields=["final_schedule_anchor_cell", "updated_at"]
            )
        except Exception as exc:
            partial = partial_success_storage_error(exc)
            if partial is None:
                raise
            partial.log_hint = "final_schedule_written_anchor_not_persisted"
            raise partial from partial.__cause__

    async def generate_draft(  # noqa: C901, PLR0912
        self,
        metadata: ShiftRegisterGoogleSheetsMetadata,
        *,
        member_by_names: dict[str, Member],
        encore_power_threshold: float,
        runner: UserInfo | None = None,
    ) -> DraftGenerationResult:
        """Build the draft schedule and overwrite the draft worksheet."""
        metadata, structure_changed = await self._ensure_current_worksheets(
            metadata,
            required_worksheets=(
                metadata.entry_worksheets.worksheet,
                metadata.draft_worksheet.worksheet,
            ),
        )
        entry_worksheet = metadata.entry_worksheets.worksheet
        draft_worksheet = metadata.draft_worksheet.worksheet
        if entry_worksheet is None or draft_worksheet is None:
            raise StorageError(StorageErrorKind.GOOGLE_SHEETS_MISSING_WORKSHEET)
        (
            source_status,
            source_config,
            source_metadata,
        ) = await self._resolve_team_source_metadata()
        resources = [
            worksheet_transaction_key(metadata.sheet_url, entry_worksheet.id),
            worksheet_transaction_key(metadata.sheet_url, draft_worksheet.id),
        ]
        if source_config is not None and source_metadata is not None:
            resources.extend(
                worksheet_transaction_key(source_config.sheet_url, worksheet.id)
                for worksheet in source_metadata
                if worksheet.id is not None
            )
        try:
            async with worksheet_transactions(resources):
                (
                    shift_grids,
                    resolution,
                    source_grids,
                ) = await self._read_shift_and_team_source_locked(
                    metadata.sheet_url,
                    [entry_worksheet, draft_worksheet],
                    source_status,
                    source_config,
                    source_metadata,
                )
                summary_mutations = ()
                source = resolution.source
                if (
                    source_config is not None
                    and source_metadata is not None
                    and source_grids is not None
                ):
                    source_grid_plan = self._summary_grid_plan(
                        source_config,
                        source_metadata,
                        source_grids,
                        member_by_names,
                    )
                    resolution = self._build_team_source(
                        source_config,
                        source_metadata,
                        [list(source_grid_plan.summary_headers)],
                    )
                    source = resolution.source
                    if source is None:
                        profile_resolution = DraftTeamProfileResolution(
                            TeamSourceStatus.INVALID,
                            {},
                        )
                    else:
                        profile_resolution = self._draft_profiles_from_summary(
                            source,
                            source_grid_plan.summaries,
                        )
                        summary_mutations = source_grid_plan.mutations
                else:
                    profile_resolution = DraftTeamProfileResolution(
                        resolution.status,
                        {},
                    )
                result, draft_requests = await self._plan_draft_locked(
                    metadata,
                    profile_resolution,
                    team_summary_url=(
                        google_sheet_url_with_gid(
                            source.config.sheet_url,
                            source.metadata.summary_worksheet.id,
                        )
                        if source is not None
                        else None
                    ),
                    entry_grid=shift_grids[entry_worksheet.id],
                    draft_grid=shift_grids[draft_worksheet.id],
                    encore_power_threshold=encore_power_threshold,
                    runner=runner,
                )
                shift_sheet = await self.get_google_sheet()
                if source is None:
                    await shift_sheet.batch_update_grid(
                        (),
                        worksheet_requests=draft_requests,
                    )
                elif normalize_google_sheet_url(source.config.sheet_url) == (
                    normalize_google_sheet_url(metadata.sheet_url)
                ):
                    await shift_sheet.batch_update_grid(
                        summary_mutations,
                        worksheet_requests=draft_requests,
                    )
                else:
                    source_sheet = GoogleSheet(
                        source.config.sheet_url,
                        self.service_account_path,
                    )
                    await source_sheet.batch_update_grid(summary_mutations)
                    try:
                        await shift_sheet.batch_update_grid(
                            (),
                            worksheet_requests=draft_requests,
                        )
                    except Exception as exc:
                        partial = partial_success_storage_error(exc)
                        if partial is None:
                            raise
                        partial.log_hint = "team_summary_refreshed_draft_incomplete"
                        raise partial from partial.__cause__
                return result
        except Exception as exc:
            if (
                isinstance(exc, StorageError)
                and exc.kind is StorageErrorKind.PARTIAL_SUCCESS
            ):
                raise
            if structure_changed and (partial := partial_success_storage_error(exc)):
                raise partial from partial.__cause__
            raise

    def _summary_grid_plan(
        self,
        source_config: TeamRegisterConfig,
        source_metadata: TeamRegisterGoogleSheetsMetadata,
        source_grids: dict[int, list[list[object]]],
        member_by_names: dict[str, Member],
    ) -> SummaryReconciliationPlan:
        """Project full source grids into the Team manager's shared plan input."""
        titles = [worksheet.title for worksheet in source_metadata.team_worksheets]
        if any(title is None for title in titles):
            raise WorksheetContractError
        dynamic_headers, _ = (
            SummaryWorksheetContent.extended_columns_dtypes_from_titles(
                [title for title in titles if title is not None]
            )
        )
        expected_summary_headers = [
            *SummaryWorksheetContent.COLUMNS,
            *dynamic_headers,
        ]
        grids = {
            worksheet.id: TeamRegisterManager._project_contract_grid(  # noqa: SLF001
                source_grids[worksheet.id],
                TeamWorksheetContent.COLUMNS,
            )
            for worksheet in source_metadata.team_worksheets
            if worksheet.id is not None
        }
        summary_worksheet = source_metadata.summary_worksheet
        if summary_worksheet.id is None:
            raise WorksheetContractError
        grids[summary_worksheet.id] = TeamRegisterManager._project_contract_grid(  # noqa: SLF001
            source_grids[summary_worksheet.id],
            expected_summary_headers,
        )
        return TeamRegisterManager.plan_summary_reconciliation(
            source_metadata,
            grids,
            member_by_names,
            source_config.encore_role_ids,
        )

    async def _plan_draft_locked(  # noqa: PLR0913
        self,
        metadata: ShiftRegisterGoogleSheetsMetadata,
        profile_resolution: DraftTeamProfileResolution,
        *,
        team_summary_url: str | None,
        entry_grid: list[list[object]],
        draft_grid: list[list[object]],
        encore_power_threshold: float,
        runner: UserInfo | None,
    ) -> tuple[DraftGenerationResult, list[dict[str, object]]]:
        entry_worksheet = metadata.entry_worksheets.worksheet
        draft_worksheet = metadata.draft_worksheet.worksheet
        if entry_worksheet is None or draft_worksheet is None:
            raise StorageError(StorageErrorKind.GOOGLE_SHEETS_MISSING_WORKSHEET)

        (
            _layout_updates,
            header_rows,
            identity_rows,
            availability_rows,
        ) = _entry_layout_from_grid(entry_grid)
        header_row = _padded_row(
            header_rows[1] if len(header_rows) > 1 else [],
            EntryWorksheetContent.COLUMN_COUNT,
        )
        shifts = EntryWorksheetContent.shifts_from_ranges(
            [header_row] if any(not _is_blank(value) for value in header_row) else [],
            identity_rows,
            availability_rows,
        )

        (
            old_axis_rows,
            old_threshold_labels,
            old_lookup_labels,
        ) = _draft_control_state_from_grid(draft_grid)
        old_last_row = _old_draft_last_row(old_axis_rows)
        old_notes_row = _old_notes_row(
            old_last_row=old_last_row,
            rows=old_axis_rows,
        )
        old_threshold_row = _old_candidate_threshold_row(
            old_last_row=old_last_row,
            threshold_labels=old_threshold_labels,
        )
        old_lookup_row = _old_lookup_row(
            old_last_row=old_last_row,
            lookup_labels=old_lookup_labels,
        )

        shift_register_config = await self.get_sheet_config()
        recruitment_ranges = RecruitmentTimeRanges.from_json(
            shift_register_config.recruitment_time_ranges
        )
        recruitment_time_range = recruitment_ranges.announcement_display()
        normalized_ranges = recruitment_ranges.ranges.ranges
        draft_hours = range(normalized_ranges[0].start, normalized_ranges[-1].end)
        active_slots = recruitment_ranges.ranges.slots
        shifts = [
            Shift(
                username=shift.username,
                display_name=shift.display_name,
                original_message=shift.original_message,
                slots=set(shift) & active_slots,
            )
            for shift in shifts
        ]
        profiles = profile_resolution.profiles
        schedule = ShiftScheduler.assign(
            shifts,
            draft_hours,
            team_profiles=profiles,
            encore_power_threshold=encore_power_threshold,
            runner=runner,
        )
        unregistered_usernames = (
            tuple(
                username
                for username in schedule.display_names
                if (profile := profiles.get(username)) is None
                or profile.main_isv is None
            )
            if profile_resolution.status is TeamSourceStatus.AVAILABLE
            else ()
        )
        draft_df = DraftWorksheetContent.from_schedule(
            schedule,
            recruitment_slots=active_slots,
        )
        team_source_warning = _draft_team_source_warning(profile_resolution.status)
        new_last_row = len(schedule.assignments) + 1
        threshold_row = new_last_row + 1
        threshold_cell = f"L{threshold_row}"
        notes_formula = DraftWorksheetContent.notes_formula(
            schedule,
            entry_worksheet_title=entry_worksheet.title,
            recruitment_time_range=recruitment_time_range,
            team_source=profile_resolution.notes_team_source,
            team_source_warning=team_source_warning,
        )
        candidate_formula = DraftWorksheetContent.candidate_formula(
            schedule,
            entry_worksheet_title=entry_worksheet.title,
            recruitment_slots=active_slots,
            encore_power_threshold_cell=threshold_cell,
            team_source=profile_resolution.notes_team_source,
        )
        lookup_updates, lookup_formula_ranges = DraftWorksheetContent.lookup_updates(
            schedule,
            old_lookup_row=old_lookup_row,
            entry_worksheet_title=entry_worksheet.title,
            team_source=profile_resolution.notes_team_source,
        )
        notes_snapshot = DraftWorksheetContent.notes_snapshot(
            schedule,
            shifts=shifts,
            recruitment_time_range=recruitment_time_range,
            team_profiles=(
                profiles
                if profile_resolution.status is TeamSourceStatus.AVAILABLE
                else None
            ),
            team_source_warning=team_source_warning,
        )
        notes_row = len(schedule.assignments) + 3
        notes_cell = f"A{notes_row}"
        notes_cleanup_updates = (
            [{"range": f"A{old_notes_row}", "values": []}]
            if old_notes_row is not None
            and old_notes_row > DraftWorksheetContent.DRAFT_VALUE_LAST_ROW
            and old_notes_row != notes_row
            else []
        )
        background_updates, border_updates = _draft_format_updates(
            schedule=schedule,
            active_slots=active_slots,
            old_last_row=old_last_row,
            new_last_row=new_last_row,
            old_threshold_row=old_threshold_row,
            threshold_row=threshold_row,
            old_lookup_row=old_lookup_row,
            lookup_row=notes_row + 1,
            has_team_source=profile_resolution.notes_team_source is not None,
        )
        candidate_control_updates = []
        if old_threshold_row is not None:
            candidate_control_updates.append(
                {"range": f"I{old_threshold_row}:M{old_threshold_row}", "values": []}
            )
        candidate_control_updates.append(
            {
                "range": f"I{threshold_row}:M{threshold_row}",
                "values": [
                    [
                        "仮配置済：緑背景",  # noqa: RUF001
                        "アンコ配置済：緑背景＋赤字",  # noqa: RUF001
                        DraftWorksheetContent.CANDIDATE_THRESHOLD_LABEL,
                        encore_power_threshold,
                        "万総合力",
                    ]
                ],
            }
        )
        legend_format_updates = _draft_legend_format_updates(
            old_threshold_row=old_threshold_row,
            threshold_row=threshold_row,
        )
        current_rules = await draft_worksheet.get_conditional_format_rules()
        candidate_rules = _draft_candidate_conditional_rules(
            worksheet_id=draft_worksheet.id,
            last_row=new_last_row,
        )
        candidate_rule_deletes = tuple(
            index
            for index, rule in reversed(list(enumerate(current_rules)))
            if DRAFT_CANDIDATE_RULE_MARKER in _entry_conditional_formula(rule)
        )
        draft_requests = draft_worksheet.typed_update_requests(
            [
                {
                    "range": f"A1:G{DraftWorksheetContent.DRAFT_VALUE_LAST_ROW}",
                    "values": [
                        DraftWorksheetContent.COLUMNS,
                        *draft_df.fillna("").to_numpy().tolist(),
                    ],
                },
                *notes_cleanup_updates,
                *candidate_control_updates,
                {"range": notes_cell, "values": [[notes_formula]]},
                {"range": "I1", "values": [[candidate_formula]]},
                *lookup_updates,
            ],
            formula_ranges={notes_cell, "I1", *lookup_formula_ranges},
            background_updates=background_updates,
            border_updates=border_updates,
            format_updates=legend_format_updates,
            conditional_format_rule_deletes=candidate_rule_deletes,
            conditional_format_rule_adds=tuple(reversed(candidate_rules)),
            frozen_column_count=1,
            min_rows=DraftWorksheetContent.EXPLICIT_FOOTPRINT_LAST_ROW,
            min_cols=DraftWorksheetContent.EXPLICIT_FOOTPRINT_COLUMN_COUNT,
        )
        self.logger.info(
            "Generated shift draft in worksheet `%s`: %d hours, %d seats short.",
            draft_worksheet.title,
            len(schedule.hours),
            schedule.total_shortage,
        )
        return (
            DraftGenerationResult(
                schedule=schedule,
                team_source_status=profile_resolution.status,
                team_source_warning=team_source_warning,
                recruitment_ranges=recruitment_ranges,
                notes_snapshot=notes_snapshot,
                unregistered_usernames=unregistered_usernames,
                team_summary_url=team_summary_url,
            ),
            draft_requests,
        )


def _old_draft_last_row(rows: list[list[object]]) -> int:
    if not rows or not rows[0] or rows[0][0] != DraftWorksheetContent.JST_COLUMN:
        return 1
    valid_labels = set(ShiftParser.HOUR_LABELS)
    last_row = 1
    for row_number, row in enumerate(rows[1:], start=2):
        if not row or row[0] not in valid_labels:
            break
        last_row = row_number
    return last_row


def _old_candidate_threshold_row(
    *,
    old_last_row: int,
    threshold_labels: list[list[object]],
) -> int | None:
    row = old_last_row + 1
    value = (
        threshold_labels[row - 1][0]
        if row <= len(threshold_labels) and threshold_labels[row - 1]
        else ""
    )
    return row if value == DraftWorksheetContent.CANDIDATE_THRESHOLD_LABEL else None


def _old_lookup_row(
    *,
    old_last_row: int,
    lookup_labels: list[list[object]],
) -> int | None:
    expected = ("名前を貼り付け", "シフト時間", "シフト元メッセージ")
    for lookup_row in (old_last_row + 3, old_last_row + 2):
        actual = tuple(
            lookup_labels[row - 1][0]
            if row <= len(lookup_labels) and lookup_labels[row - 1]
            else ""
            for row in range(lookup_row, lookup_row + len(expected))
        )
        if actual == expected:
            return lookup_row
    return None


def _old_notes_row(
    *,
    old_last_row: int,
    rows: list[list[object]],
) -> int | None:
    row = old_last_row + 2
    value = rows[row - 1][0] if row <= len(rows) and rows[row - 1] else ""
    if not isinstance(value, str):
        return None
    signature = DraftWorksheetContent.NOTES_FORMULA_SIGNATURE
    legacy_prefix = f"=LET(shifts, C2:G{old_last_row}, encore, C2:C{old_last_row}, "
    return row if signature in value or value.startswith(legacy_prefix) else None


def _draft_format_updates(  # noqa: PLR0913
    *,
    schedule: DraftSchedule,
    active_slots: set[int],
    old_last_row: int,
    new_last_row: int,
    old_threshold_row: int | None,
    threshold_row: int,
    old_lookup_row: int | None,
    lookup_row: int,
    has_team_source: bool,
) -> tuple[
    list[tuple[str, str]],
    list[tuple[str, str | None, str, tuple[str, ...]]],
]:
    format_last_row = max(old_last_row, new_last_row)
    background_updates = [(f"A1:G{format_last_row}", "#FFFFFF")]
    background_updates.extend(
        (f"B{row}:G{row}", "#CCCCCC")
        for row, assignment in enumerate(schedule.assignments, start=2)
        if assignment.hour not in active_slots
    )
    if old_threshold_row is not None:
        background_updates.append(
            (f"I{old_threshold_row}:M{old_threshold_row}", "#FFFFFF")
        )
    background_updates.extend(
        [
            (f"I{threshold_row}:J{threshold_row}", "#D9EAD3"),
            (f"K{threshold_row}", "#A4C2F4"),
            (f"L{threshold_row}", "#FFF2CC"),
            (f"M{threshold_row}", "#A4C2F4"),
        ]
    )
    if old_lookup_row is not None:
        background_updates.append(
            (f"J{old_lookup_row}:L{old_lookup_row + 4}", "#FFFFFF")
        )
    background_updates.extend(
        [
            (f"J{lookup_row}:L{lookup_row + 2}", "#FFFFFF"),
            (f"J{lookup_row}:J{lookup_row + 2}", "#A4C2F4"),
            (f"K{lookup_row}", "#FFF2CC"),
        ]
    )
    if has_team_source:
        background_updates.append((f"J{lookup_row + 3}:L{lookup_row + 3}", "#A4C2F4"))

    border_updates = [
        (f"A1:G{format_last_row}", None, "NONE", BORDER_NAMES),
        (
            f"A1:G{new_last_row}",
            "#000000",
            "SOLID",
            OUTER_BORDER_SIDES,
        ),
        ("A1:G1", "#000000", "SOLID", ("bottom",)),
        (
            f"I1:I{max(old_last_row + 1, threshold_row)}",
            None,
            "NONE",
            BORDER_NAMES,
        ),
    ]
    if old_threshold_row is not None:
        border_updates.append(
            (f"J{old_threshold_row}:M{old_threshold_row}", None, "NONE", BORDER_NAMES)
        )
    border_updates.extend(
        [
            (f"I1:I{threshold_row}", "#000000", "SOLID", ("left",)),
            (
                f"I{threshold_row}:M{threshold_row}",
                "#000000",
                "SOLID",
                OUTER_BORDER_SIDES,
            ),
            (
                f"L{threshold_row}",
                "#FF0000",
                "SOLID_MEDIUM",
                OUTER_BORDER_SIDES,
            ),
            (
                f"B2:G{new_last_row}",
                "#FF0000",
                "SOLID_MEDIUM",
                OUTER_BORDER_SIDES,
            ),
        ]
    )
    if old_lookup_row is not None:
        border_updates.append(
            (
                f"J{old_lookup_row}:L{old_lookup_row + 2}",
                None,
                "NONE",
                BORDER_NAMES,
            )
        )
    border_updates.extend(
        [
            (f"J{lookup_row}:L{lookup_row + 2}", None, "NONE", BORDER_NAMES),
            (f"J{lookup_row}:L{lookup_row}", "#000000", "SOLID", ("top",)),
            (f"J{lookup_row}:J{lookup_row + 2}", "#000000", "SOLID", ("left",)),
            (
                f"K{lookup_row}",
                "#FF0000",
                "SOLID_MEDIUM",
                OUTER_BORDER_SIDES,
            ),
        ]
    )
    return background_updates, border_updates


def _draft_candidate_conditional_rules(
    *, worksheet_id: int, last_row: int
) -> tuple[dict[str, object], dict[str, object]]:
    grid_range = {
        "sheetId": worksheet_id,
        "startRowIndex": 1,
        "endRowIndex": last_row,
        "startColumnIndex": 8,
    }
    marker = f'N("{DRAFT_CANDIDATE_RULE_MARKER}v1")=0'
    return (
        _entry_conditional_rule(
            [grid_range],
            f'=AND(I2<>"",$C2=I2,{marker})',
            background="#D9EAD3",
            foreground="#FF0000",
        ),
        _entry_conditional_rule(
            [grid_range],
            f'=AND(I2<>"",SUMPRODUCT(N($C2:$G2=I2))>0,{marker})',
            background="#D9EAD3",
        ),
    )


def _draft_legend_format_updates(
    *, old_threshold_row: int | None, threshold_row: int
) -> tuple[tuple[str, dict[str, object], str], ...]:
    field = "userEnteredFormat.textFormat.foregroundColorStyle"

    def update(column: str, row: int, color: str) -> tuple[str, dict[str, object], str]:
        return (
            f"{column}{row}",
            {"textFormat": {"foregroundColorStyle": {"rgbColor": _entry_rgb(color)}}},
            field,
        )

    return (
        *(
            (update(column, old_threshold_row, "#000000") for column in ("J", "M"))
            if old_threshold_row
            else ()
        ),
        update("J", threshold_row, "#FF0000"),
        update("M", threshold_row, "#000000"),
    )


def _entry_presentation_plan(
    ranges: RecruitmentTimeRanges,
    *,
    worksheet_id: int,
) -> EntryPresentationPlan:
    configured = ranges.ranges.ranges
    first_hour = configured[0].start
    last_hour = configured[-1].end
    hidden = [("F:AI", False)]
    if first_hour:
        hidden.append((_entry_hour_columns(0, first_hour), True))
    if last_hour < len(EntryWorksheetContent.HOUR_COLUMNS):
        hidden.append(
            (
                _entry_hour_columns(
                    last_hour,
                    len(EntryWorksheetContent.HOUR_COLUMNS),
                ),
                True,
            )
        )

    identity_ranges = [
        _entry_rule_range(worksheet_id, 0, 5),
        _entry_rule_range(worksheet_id, 35, 36),
    ]
    active_ranges = [
        _entry_rule_range(worksheet_id, 5 + item.start, 5 + item.end)
        for item in configured
    ]
    gap_ranges = [
        _entry_rule_range(worksheet_id, 5 + left.end, 5 + right.start)
        for left, right in pairwise(configured)
        if left.end < right.start
    ]
    visible_index = "SUBTOTAL(103,$A$3:$A3)"
    rules = []
    if gap_ranges:
        count_gap_ranges = [
            _entry_rule_range(
                worksheet_id,
                item["startColumnIndex"],
                item["endColumnIndex"],
                start_row=0,
                end_row=1,
            )
            for item in gap_ranges
        ]
        header_gap_ranges = [
            _entry_rule_range(
                worksheet_id,
                item["startColumnIndex"],
                item["endColumnIndex"],
                start_row=1,
                end_row=2,
            )
            for item in gap_ranges
        ]
        rules.extend(
            (
                _entry_conditional_rule(
                    count_gap_ranges,
                    '=N("rhoboto:shift-entry:gap-count:v1")=0',
                    background="#CCCCCC",
                    foreground="#B7B7B7",
                ),
                _entry_conditional_rule(
                    header_gap_ranges,
                    '=N("rhoboto:shift-entry:gap-header:v1")=0',
                    background="#B7B7B7",
                    foreground="#999999",
                ),
                _entry_conditional_rule(
                    gap_ranges,
                    '=N("rhoboto:shift-entry:gap:v1")=0',
                    background="#CCCCCC",
                    foreground="#B7B7B7",
                ),
            )
        )
    rules.extend(
        [
            _entry_conditional_rule(
                identity_ranges,
                (
                    '=AND(N("rhoboto:shift-entry:row-orange:v1")=0,'
                    f'$A3<>"",ISODD({visible_index}))'
                ),
                background="#F8CBAD",
            ),
            _entry_conditional_rule(
                identity_ranges,
                (
                    '=AND(N("rhoboto:shift-entry:row-pink:v1")=0,'
                    f'$A3<>"",ISEVEN({visible_index}))'
                ),
                background="#F4B6D2",
            ),
            _entry_conditional_rule(
                active_ranges,
                (
                    '=AND(N("rhoboto:shift-entry:one-orange:v1")=0,'
                    'INDIRECT("RC",FALSE)=1,'
                    f"ISODD({visible_index}))"
                ),
                background="#F8CBAD",
                foreground="#E6B89C",
            ),
            _entry_conditional_rule(
                active_ranges,
                (
                    '=AND(N("rhoboto:shift-entry:one-pink:v1")=0,'
                    'INDIRECT("RC",FALSE)=1,'
                    f"ISEVEN({visible_index}))"
                ),
                background="#F4B6D2",
                foreground="#DDA3BD",
            ),
            _entry_conditional_rule(
                active_ranges,
                ('=AND(N("rhoboto:shift-entry:zero:v1")=0,INDIRECT("RC",FALSE)=0)'),
                background="#FFFFFF",
                foreground="#E6E6E6",
            ),
        ]
    )
    header_format = {
        "backgroundColorStyle": {"rgbColor": _entry_rgb("#3C78D8")},
        "textFormat": {
            "bold": True,
            "foregroundColorStyle": {"rgbColor": _entry_rgb("#FFFFFF")},
        },
    }
    return EntryPresentationPlan(
        format_updates=(
            (
                "A1",
                header_format,
                "userEnteredFormat(backgroundColorStyle,textFormat)",
            ),
            (
                "A2:AJ2",
                header_format,
                "userEnteredFormat(backgroundColorStyle,textFormat)",
            ),
        ),
        border_updates=(
            ("A2:AJ2", "#000000", "SOLID", ("top", "bottom")),
            ("B:B", "#000000", "SOLID", ("right",)),
            ("E:E", "#000000", "SOLID", ("right",)),
            ("AI:AI", "#000000", "SOLID", ("right",)),
        ),
        column_width_updates=(
            ("A:B", 100),
            ("C:D", 60),
            ("E:E", 60),
            ("F:AI", 40),
        ),
        hidden_column_updates=tuple(hidden),
        conditional_format_rules=tuple(rules),
    )


def _entry_rule_updates(
    current: list[dict[str, object]],
    desired: tuple[dict[str, object], ...],
) -> tuple[tuple[int, ...], tuple[dict[str, object], ...], bool]:
    marked = [
        (index, rule)
        for index, rule in enumerate(current)
        if ENTRY_RULE_MARKER in _entry_conditional_formula(rule)
    ]
    if [rule for _index, rule in marked] == list(desired):
        return (), (), True
    return (
        tuple(sorted((index for index, _rule in marked), reverse=True)),
        tuple(reversed(desired)),
        False,
    )


def _entry_conditional_formula(rule: dict[str, object]) -> str:
    try:
        boolean_rule = rule["booleanRule"]
        if not isinstance(boolean_rule, dict):
            return ""
        condition = boolean_rule["condition"]
        if not isinstance(condition, dict):
            return ""
        values = condition["values"]
        if not isinstance(values, list) or not values:
            return ""
        value = values[0]
        return str(value.get("userEnteredValue", "")) if isinstance(value, dict) else ""
    except KeyError:
        return ""


def _entry_conditional_rule(
    ranges: list[dict[str, int]],
    formula: str,
    *,
    background: str | None = None,
    foreground: str | None = None,
) -> dict[str, object]:
    cell_format: dict[str, object] = {}
    if background is not None:
        cell_format["backgroundColorStyle"] = {"rgbColor": _entry_rgb(background)}
    if foreground is not None:
        cell_format["textFormat"] = {
            "foregroundColorStyle": {"rgbColor": _entry_rgb(foreground)}
        }
    return {
        "ranges": ranges,
        "booleanRule": {
            "condition": {
                "type": "CUSTOM_FORMULA",
                "values": [{"userEnteredValue": formula}],
            },
            "format": cell_format,
        },
    }


def _entry_rule_range(
    worksheet_id: int,
    start_column: int,
    end_column: int,
    *,
    start_row: int = EntryWorksheetContent.FIRST_DATA_ROW - 1,
    end_row: int | None = None,
) -> dict[str, int]:
    grid_range = {
        "sheetId": worksheet_id,
        "startRowIndex": start_row,
        "startColumnIndex": start_column,
        "endColumnIndex": end_column,
    }
    if end_row is not None:
        grid_range["endRowIndex"] = end_row
    return grid_range


def _entry_hour_columns(start: int, end: int) -> str:
    return f"{column_letter(6 + start)}:{column_letter(5 + end)}"


def _entry_rgb(color: str) -> dict[str, float]:
    return {
        name: int(color[start : start + 2], 16) / 255
        for name, start in (("red", 1), ("green", 3), ("blue", 5))
    }


def _final_typed_requests(
    worksheet: AsyncioGspreadWorksheet,
    request: FinalGenerationRequest,
    schedule: FinalSchedulePlan,
) -> list[dict[str, object]]:
    data: list[dict[str, object]] = [
        {"range": request.main_range.a1, "values": schedule.values}
    ]
    if request.event_day.status is EventDayWriteStatus.READY:
        anchor = request.event_day.anchor
        value = request.event_day.value
        if anchor is None or value is None:
            raise FinalScheduleInputError
        data.append(
            {
                "range": anchor.a1,
                "values": [[value]],
            }
        )
    return worksheet.typed_update_requests(
        data,
        formula_ranges=set(),
        background_updates=_final_background_updates(request, schedule),
        format_updates=_final_foreground_updates(request, schedule),
        min_rows=_final_required_rows(request),
        min_cols=_final_required_columns(request),
    )


def _final_background_updates(
    request: FinalGenerationRequest,
    schedule: FinalSchedulePlan,
) -> list[tuple[str, str]]:
    role_range = _final_role_range(request)
    split_cells: list[tuple[int, int, str]] = []
    for row_offset, row in enumerate(schedule.rows):
        role_values = (row.encore, *row.honso, row.standby)
        for role_offset, name in enumerate(role_values):
            if name in schedule.split_colors:
                split_cells.append(
                    (
                        request.main_anchor.row + row_offset,
                        request.main_anchor.column + 1 + role_offset,
                        schedule.split_colors[name],
                    )
                )
    gap_rows = [
        request.main_anchor.row + row_offset
        for row_offset, row in enumerate(schedule.rows)
        if not row.is_recruitment
    ]
    return [
        (role_range, "#FFFFFF"),
        *_coalesce_final_cells(split_cells),
        *_coalesce_final_rows(
            gap_rows,
            start_column=request.main_anchor.column + 1,
            end_column=request.main_range.end.column,
        ),
    ]


def _final_foreground_updates(
    request: FinalGenerationRequest,
    schedule: FinalSchedulePlan,
) -> list[tuple[str, dict[str, object], str]]:
    role_range = _final_role_range(request)
    encore_cells = [
        (
            request.main_anchor.row + row_offset,
            request.main_anchor.column + 1,
        )
        for row_offset, row in enumerate(schedule.rows)
        if row.encore
    ]
    return [
        _final_text_color_update(role_range, "#000000"),
        *(
            _final_text_color_update(range_name, "#FF0000")
            for range_name, _color in _coalesce_final_cells(
                encore_cells, include_color=False
            )
        ),
    ]


def _final_text_color_update(
    range_name: str,
    color: str,
) -> tuple[str, dict[str, object], str]:
    return (
        range_name,
        {"textFormat": {"foregroundColorStyle": {"rgbColor": _entry_rgb(color)}}},
        "userEnteredFormat.textFormat.foregroundColorStyle",
    )


def _final_role_range(request: FinalGenerationRequest) -> str:
    return (
        f"{column_letter(request.main_anchor.column + 1)}{request.main_anchor.row}:"
        f"{column_letter(request.main_range.end.column)}{request.main_range.end.row}"
    )


def _final_required_rows(request: FinalGenerationRequest) -> int:
    rows = request.main_range.end.row
    if request.event_day.status is EventDayWriteStatus.READY:
        anchor = request.event_day.anchor
        if anchor is not None:
            rows = max(rows, anchor.row)
    return rows


def _final_required_columns(request: FinalGenerationRequest) -> int:
    columns = request.main_range.end.column
    if request.event_day.status is EventDayWriteStatus.READY:
        anchor = request.event_day.anchor
        if anchor is not None:
            columns = max(columns, anchor.column)
    return columns


def _coalesce_final_cells(
    cells: list[tuple[int, int, str] | tuple[int, int]],
    *,
    include_color: bool = True,
) -> list[tuple[str, str]]:
    grouped: dict[tuple[int, str], list[int]] = {}
    for cell in cells:
        row, column = cell[:2]
        color = cell[2] if include_color else ""
        grouped.setdefault((column, color), []).append(row)
    ranges: list[tuple[str, str]] = []
    for (column, _color), rows in grouped.items():
        rows.sort()
        start = previous = rows[0]
        for row in rows[1:]:
            if row == previous + 1:
                previous = row
                continue
            ranges.append((_final_range(column, start, column, previous), _color))
            start = previous = row
        ranges.append((_final_range(column, start, column, previous), _color))
    return ranges


def _coalesce_final_rows(
    rows: list[int],
    *,
    start_column: int,
    end_column: int,
) -> list[tuple[str, str]]:
    if not rows:
        return []
    rows.sort()
    ranges: list[tuple[str, str]] = []
    start = previous = rows[0]
    for row in rows[1:]:
        if row == previous + 1:
            previous = row
            continue
        ranges.append(
            (_final_range(start_column, start, end_column, previous), "#CCCCCC")
        )
        start = previous = row
    ranges.append((_final_range(start_column, start, end_column, previous), "#CCCCCC"))
    return ranges


def _final_range(
    start_column: int,
    start_row: int,
    end_column: int,
    end_row: int,
) -> str:
    start = f"{column_letter(start_column)}{start_row}"
    end = f"{column_letter(end_column)}{end_row}"
    return start if start == end else f"{start}:{end}"


def _trim_blank_rows(rows: list[list[object]]) -> list[list[object]]:
    normalized = []
    for source_row in rows:
        row = list(source_row)
        while row and _is_blank(row[-1]):
            row.pop()
        normalized.append(row)
    while normalized and not normalized[-1]:
        normalized.pop()
    return normalized


def _entry_layout_from_grid(
    grid: list[list[object]],
) -> tuple[
    list[dict[str, object]],
    list[list[object]],
    list[list[object]],
    list[list[object]],
]:
    header_rows = _trim_blank_rows(
        [row[: EntryWorksheetContent.COLUMN_COUNT] for row in grid[:2]]
    )
    identity_rows = _trim_blank_rows(
        [row[:ENTRY_IDENTITY_LAST_COLUMN] for row in grid[2:]]
    )
    availability_rows = _trim_blank_rows(
        [
            row[
                ENTRY_AVAILABILITY_FIRST_COLUMN - 1 : EntryWorksheetContent.COLUMN_COUNT
            ]
            for row in grid[2:]
        ]
    )
    row_count = max(len(identity_rows), len(availability_rows))
    identity_rows.extend([[]] * (row_count - len(identity_rows)))
    availability_rows.extend([[]] * (row_count - len(availability_rows)))
    return (
        _entry_layout_updates(header_rows, identity_rows, availability_rows),
        header_rows,
        identity_rows,
        availability_rows,
    )


def _entry_state_from_grid(
    grid: list[list[object]],
) -> tuple[
    list[dict[str, object]],
    list[list[object]],
    list[tuple[int, str, str, bool]],
]:
    layout, _headers, identities, availability = _entry_layout_from_grid(grid)
    return layout, identities, _entry_participants(identities, availability)


def _draft_control_state_from_grid(
    grid: list[list[object]],
) -> tuple[list[list[object]], list[list[object]], list[list[object]]]:
    return (
        _trim_blank_rows([row[:1] for row in grid[:33]]),
        _trim_blank_rows([row[8:9] for row in grid[:32]]),
        _trim_blank_rows([row[9:10] for row in grid[:37]]),
    )


def _entry_layout_updates(
    header_rows: list[list[object]],
    identity_rows: list[list[object]],
    availability_rows: list[list[object]],
) -> list[dict[str, object]]:
    count_row = _padded_row(header_rows[0] if header_rows else [], 36)
    header_row = _padded_row(header_rows[1] if len(header_rows) > 1 else [], 36)
    expected_count = EntryWorksheetContent.count_row()
    expected_header = EntryWorksheetContent.COLUMNS

    header_is_blank = all(_is_blank(value) for value in header_row)
    has_participant_data = any(
        not _is_blank(value)
        for identity, availability in zip(
            identity_rows,
            availability_rows,
            strict=True,
        )
        for value in [*identity, *availability]
    )
    if header_is_blank:
        if has_participant_data:
            raise WorksheetContractError(log_hint="required_header_missing")
    elif header_row != expected_header:
        raise WorksheetContractError

    updates: list[dict[str, object]] = []
    if count_row[0] != expected_count[0]:
        updates.append({"range": "A1", "values": [[expected_count[0]]]})
    if count_row[5:35] != expected_count[5:35]:
        updates.append({"range": "F1:AI1", "values": [expected_count[5:35]]})
    if header_row != expected_header:
        updates.append({"range": "A2:AJ2", "values": [expected_header]})
    return updates


def _entry_participants(
    identity_rows: list[list[object]],
    availability_rows: list[list[object]],
) -> list[tuple[int, str, str, bool]]:
    participants: list[tuple[int, str, str, bool]] = []
    seen_usernames: set[str] = set()
    for row_number, (identity, availability) in enumerate(
        zip(identity_rows, availability_rows, strict=True),
        start=EntryWorksheetContent.FIRST_DATA_ROW,
    ):
        row = _padded_row(identity, 3)
        availability_row = _padded_row(
            availability,
            len(EntryWorksheetContent.HOUR_COLUMNS) + 1,
        )
        username = "" if _is_blank(row[0]) else str(row[0])
        formula = "" if _is_blank(row[2]) else str(row[2])
        if username in seen_usernames:
            raise WorksheetContractError
        if username:
            seen_usernames.add(username)
        reusable = not username and all(
            _is_blank(value) for value in [*row, *availability_row]
        )
        participants.append((row_number, username, formula, reusable))
    return participants


def _entry_team_formula(
    row: int,
    resolution: TeamSourceResolution,
) -> str | None:
    if resolution.status is TeamSourceStatus.UNRESOLVED:
        return None
    source = resolution.source
    if resolution.status is not TeamSourceStatus.AVAILABLE or source is None:
        return ""
    summary = source.metadata.summary_worksheet
    if summary.title is None:
        return ""
    columns = source.summary_columns
    return build_team_summary_formula(
        row=row,
        sheet_url=source.config.sheet_url,
        worksheet_title=summary.title,
        username_column=columns.username,
        roles_column=columns.roles,
        main_isv_column=columns.main_isv,
        encore_isv_column=columns.encore_isv,
        import_last_column=columns.import_last_column,
    )


def _padded_row(row: list[object], width: int) -> list[object]:
    return [*row[:width], *([""] * max(0, width - len(row)))]


def _is_blank(value: object) -> bool:
    return value in ("", None)


def _draft_team_source_warning(status: TeamSourceStatus) -> str | None:
    if status is TeamSourceStatus.AVAILABLE:
        return None
    if status is TeamSourceStatus.UNSET:
        return TEAM_SOURCE_UNSET_DRAFT_WARNING
    return TEAM_SOURCE_UNAVAILABLE_DRAFT_WARNING


def _optional_float(value: object) -> float | None:
    if value in ("", None) or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _unique_header_column(header: list[object], name: str) -> int:
    matches = [index for index, value in enumerate(header, start=1) if value == name]
    if len(matches) != 1:
        msg = f"Expected one Summary header {name!r}, found {len(matches)}."
        raise ValueError(msg)
    return matches[0]


def _optional_unique_header_column(header: list[object], name: str) -> int | None:
    matches = [index for index, value in enumerate(header, start=1) if value == name]
    return matches[0] if len(matches) == 1 else None
