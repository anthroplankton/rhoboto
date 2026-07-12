from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, overload, override

from models.feature_channel import FeatureChannel
from models.team_register import TeamRegisterConfig
from utils.google_sheets import BORDER_NAMES, GoogleSheet
from utils.google_sheets_errors import GoogleSheetsError, GoogleSheetsErrorKind
from utils.structs_base import validate_anchor_cell
from utils.team_register_structs import Summary, TeamRegisterGoogleSheetsMetadata

if TYPE_CHECKING:
    from datetime import date, datetime

    from utils.google_sheets import AsyncioGspreadWorksheet
    from utils.shift_scheduler import DraftSchedule
    from utils.structs_base import UserInfo

from models.shift_register import ShiftRegisterConfig
from utils.key_async_lock import KeyAsyncLock
from utils.manager_base import ManagerBase
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

ENTRY_READ_RANGES = ["1:2", "A3:C"]
SHIFT_REGISTER_SHEET_WRITE_LOCK = KeyAsyncLock()
OUTER_BORDER_SIDES = ("top", "bottom", "left", "right")


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


@dataclass(frozen=True)
class DraftGenerationResult:
    schedule: DraftSchedule
    team_source_status: TeamSourceStatus
    team_source_warning: str | None
    recruitment_ranges: RecruitmentTimeRanges
    notes_snapshot: str
    unregistered_usernames: tuple[str, ...] = ()


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

    async def get_saved_team_source_channel_id(self) -> int | None:
        """Return the Discord channel ID for the saved Team source."""
        config = await self.get_sheet_config()
        source_id = getattr(config, "team_source_feature_channel_id", None)
        if source_id is None:
            return None
        source = await FeatureChannel.get_or_none(id=source_id)
        return source.channel_id if source is not None else None

    async def resolve_team_source(
        self,
        *,
        team_channel_id: int | None = None,
    ) -> TeamSourceResolution:
        """Resolve an explicit or saved Team source."""
        if team_channel_id is not None:
            filters = {
                "feature_channel__guild_id": self.feature_channel.guild_id,
                "feature_channel__channel_id": team_channel_id,
                "feature_channel__feature_name": "team_register",
            }
            missing_status = TeamSourceStatus.INVALID
        else:
            shift_config = await self.get_sheet_config_or_none()
            selected_id = getattr(
                shift_config,
                "team_source_feature_channel_id",
                None,
            )
            if selected_id is None:
                return TeamSourceResolution(TeamSourceStatus.UNSET)
            filters = {"feature_channel_id": selected_id}
            missing_status = TeamSourceStatus.INVALID

        configs = await TeamRegisterConfig.filter(**filters).select_related(
            "feature_channel"
        )
        if not configs:
            return TeamSourceResolution(missing_status)
        if len(configs) > 1:
            return TeamSourceResolution(TeamSourceStatus.AMBIGUOUS)

        return await self._resolve_team_source_config(configs[0])

    async def resolve_draft_team_profiles(self) -> DraftTeamProfileResolution:
        """Read Draft scheduling values from the configured Team Summary."""
        resolution = await self.resolve_team_source()
        source = resolution.source
        if resolution.status is not TeamSourceStatus.AVAILABLE or source is None:
            return DraftTeamProfileResolution(resolution.status, {})

        columns = source.summary_columns
        if columns.main_power is None or (
            columns.encore_isv is not None and columns.encore_power is None
        ):
            return DraftTeamProfileResolution(TeamSourceStatus.INVALID, {})

        worksheet = source.metadata.summary_worksheet.worksheet
        if worksheet is None:
            return DraftTeamProfileResolution(TeamSourceStatus.INVALID, {})
        try:
            value_ranges = await worksheet.batch_get_values(
                [f"A:{columns.import_last_column}"]
            )
            profiles = _draft_profiles_from_summary(value_ranges[0], columns)
        except GoogleSheetsError as exc:
            self.logger.warning("Could not read Draft Team profiles: %s", exc.kind)
            return DraftTeamProfileResolution(TeamSourceStatus.UNRESOLVED, {})
        except (IndexError, ValueError):
            return DraftTeamProfileResolution(TeamSourceStatus.INVALID, {})
        summary_title = source.metadata.summary_worksheet.title
        main_title = source.metadata.team_worksheets[0].title
        if summary_title is None or main_title is None:
            msg = "Resolved Team Source is missing a worksheet title."
            raise ValueError(msg)
        encore_title = (
            source.metadata.team_worksheets[1].title
            if len(source.metadata.team_worksheets) > 1
            else None
        )
        notes_team_source = DraftNotesTeamSource(
            sheet_url=source.config.sheet_url,
            worksheet_title=summary_title,
            import_last_column=columns.import_last_column,
            username_header="username",
            roles_header="encore_roles",
            main_isv_header=Summary.isv_title(main_title),
            main_power_header=Summary.power_title(main_title),
            encore_isv_header=(
                Summary.isv_title(encore_title) if encore_title is not None else None
            ),
            encore_power_header=(
                Summary.power_title(encore_title) if encore_title is not None else None
            ),
        )
        return DraftTeamProfileResolution(
            TeamSourceStatus.AVAILABLE,
            profiles,
            notes_team_source,
        )

    async def get_team_source_candidate_channel_ids(self) -> tuple[int, ...]:
        """Return same-guild Team Register channels available for UI selection."""
        configs = await TeamRegisterConfig.filter(
            feature_channel__guild_id=self.feature_channel.guild_id,
            feature_channel__feature_name="team_register",
        ).select_related("feature_channel")
        return tuple(config.feature_channel.channel_id for config in configs)

    async def _resolve_team_source_config(
        self,
        config: TeamRegisterConfig,
    ) -> TeamSourceResolution:
        try:
            sheet = GoogleSheet(config.sheet_url, self.service_account_path)
            worksheets = await sheet.get_worksheets(config.get_worksheet_ids())
            metadata = TeamRegisterGoogleSheetsMetadata.from_id_mapping(
                config.sheet_url,
                worksheets,
            )
            return await self._build_team_source(config, metadata)
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
            return TeamSourceResolution(status)

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
        try:
            _layout, _values, participants = await _read_entry_state(worksheet)
        except ValueError as exc:
            error = StorageError(StorageErrorKind.MALFORMED_SHEET)
            error.__cause__ = exc
            raise error from exc

        updates: list[dict[str, object]] = []
        for row, username, current_formula in participants:
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
        resolution = await self.resolve_team_source(team_channel_id=team_channel_id)
        source = resolution.source
        if resolution.status is not TeamSourceStatus.AVAILABLE or source is None:
            return resolution

        config = await self.get_sheet_config()
        config.team_source_feature_channel_id = source.config.feature_channel.id
        await config.save(
            update_fields=["team_source_feature_channel_id", "updated_at"]
        )
        try:
            metadata = await self.fetch_google_sheets_metadata()
            await self.repair_team_references(metadata, resolution)
        except Exception as exc:
            partial = partial_success_storage_error(exc)
            if partial is None:
                raise
            raise partial from partial.__cause__
        return resolution

    async def _build_team_source(
        self,
        config: TeamRegisterConfig,
        metadata: TeamRegisterGoogleSheetsMetadata,
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

        summary_worksheet = metadata.summary_worksheet.worksheet
        if summary_worksheet is None:
            return TeamSourceResolution(TeamSourceStatus.INVALID)
        summary_values = await summary_worksheet.batch_get_values(["1:1"])
        header = summary_values[0][0] if summary_values and summary_values[0] else []
        if not isinstance(header, list):
            return TeamSourceResolution(TeamSourceStatus.INVALID)

        main_worksheet = metadata.team_worksheets[0]
        encore_worksheet = (
            metadata.team_worksheets[1] if len(metadata.team_worksheets) > 1 else None
        )
        try:
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
                import_last_column=column_letter(len(header)),
            )
        except ValueError:
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
    ) -> ShiftRegisterGoogleSheetsMetadata:
        worksheet_titles = worksheet_titles or []
        if (
            entry_worksheet_title
            and draft_worksheet_title
            and final_schedule_worksheet_title
        ):
            worksheet_titles = [
                entry_worksheet_title,
                draft_worksheet_title,
                final_schedule_worksheet_title,
            ]
        expected = self.GoogleSheetsMetadataType.WORKSHEET_METADATA_TYPES
        if len(worksheet_titles) != len(expected):
            msg = (
                f"Expected {len(expected)} worksheet titles "
                "(entry_worksheet_title, "
                "draft_worksheet_title, "
                "final_schedule_worksheet_title), "
                f"but got {len(worksheet_titles)}: {worksheet_titles!r}"
            )
            raise ValueError(msg)
        return await super().upsert_sheet_config_and_worksheets(
            sheet_url, worksheet_titles
        )

    async def update_final_schedule_anchor_cell(self, anchor_cell: str) -> None:
        """
        Update the anchor cell for the final schedule worksheet in the
        ShiftRegister database record.

        Args:
            anchor_cell (str): The anchor cell string (e.g., "A1").
        """
        anchor_cell = validate_anchor_cell(anchor_cell)
        shift_register_config = await self.get_sheet_config()
        shift_register_config.final_schedule_anchor_cell = anchor_cell
        await shift_register_config.save()

    async def update_timeline(
        self,
        *,
        day_number: int | None,
        event_date: date | None,
        submission_deadline_at: datetime | None,
        draft_shift_proposal_at: datetime | None,
        final_shift_notice_at: datetime | None,
    ) -> None:
        shift_register_config = await self.get_sheet_config()
        shift_register_config.day_number = day_number
        shift_register_config.event_date = event_date
        shift_register_config.submission_deadline_at = submission_deadline_at
        shift_register_config.draft_shift_proposal_at = draft_shift_proposal_at
        shift_register_config.final_shift_notice_at = final_shift_notice_at
        await shift_register_config.save(
            update_fields=[
                "day_number",
                "event_date",
                "submission_deadline_at",
                "draft_shift_proposal_at",
                "final_shift_notice_at",
                "updated_at",
            ]
        )

    async def update_recruitment_time_ranges(
        self,
        ranges: RecruitmentTimeRanges,
    ) -> None:
        shift_register_config = await self.get_sheet_config()
        shift_register_config.recruitment_time_ranges = ranges.to_json()
        await shift_register_config.save(
            update_fields=["recruitment_time_ranges", "updated_at"]
        )

    async def upsert_or_delete_user_shift(
        self,
        user: UserInfo,
        shift: Shift | None,
        metadata: ShiftRegisterGoogleSheetsMetadata,
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

        try:
            layout_updates, participant_values, participants = await _read_entry_state(
                worksheet
            )
        except ValueError as exc:
            error = StorageError(StorageErrorKind.MALFORMED_SHEET)
            error.__cause__ = exc
            raise error from exc

        if shift is None:
            matched = next(
                (
                    row
                    for row, username, _formula in participants
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
                for row, username, _formula in participants
                if username == user.username
            ),
            None,
        )
        target_row = matched or next(
            (row for row, username, _formula in participants if not username),
            EntryWorksheetContent.FIRST_DATA_ROW + len(participant_values),
        )
        current_formulas = {row: formula for row, _username, formula in participants}
        resolution = await self.resolve_team_source()

        updates = list(layout_updates)
        for row, username, current_formula in participants:
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

        await worksheet.ensure_size(
            min_rows=target_row,
            min_cols=EntryWorksheetContent.COLUMN_COUNT,
        )
        formula_ranges = {
            str(item["range"])
            for item in updates
            if item["range"] == "A1:AJ1" or str(item["range"]).startswith("C")
        }
        await worksheet.batch_update_typed_values(
            updates,
            formula_ranges=formula_ranges,
        )

        self.logger.info(
            "Updated shift registration %r in worksheet `%s`",
            shift,
            worksheet.title,
        )

    async def generate_draft(
        self,
        metadata: ShiftRegisterGoogleSheetsMetadata,
        *,
        encore_power_threshold: float,
        runner: str | None = None,
    ) -> DraftGenerationResult:
        """Build the draft schedule and overwrite the draft worksheet."""
        entry_worksheet = metadata.entry_worksheets.worksheet
        draft_worksheet = metadata.draft_worksheet.worksheet
        if entry_worksheet is None or draft_worksheet is None:
            raise StorageError(StorageErrorKind.GOOGLE_SHEETS_MISSING_WORKSHEET)

        try:
            (
                header_rows,
                identity_rows,
                availability_rows,
            ) = await entry_worksheet.batch_get_values(["2:2", "A3:B", "F3:AJ"])
            shifts = EntryWorksheetContent.shifts_from_ranges(
                header_rows,
                identity_rows,
                availability_rows,
            )
        except ValueError as exc:
            error = StorageError(StorageErrorKind.MALFORMED_SHEET)
            error.__cause__ = exc
            raise error from exc

        (
            old_axis_rows,
            old_threshold_labels,
            old_lookup_labels,
        ) = await draft_worksheet.batch_get_values(["A1:A33", "I1:I32", "J1:J37"])
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
        profile_resolution = await self.resolve_draft_team_profiles()
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
        draft_df = DraftWorksheetContent.from_schedule(schedule)
        team_source_warning = _draft_team_source_warning(profile_resolution.status)
        new_last_row = len(schedule.assignments) + 1
        threshold_row = new_last_row + 1
        threshold_cell = f"J{threshold_row}"
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
                {"range": f"I{old_threshold_row}:K{old_threshold_row}", "values": []}
            )
        candidate_control_updates.append(
            {
                "range": f"I{threshold_row}:K{threshold_row}",
                "values": [
                    [
                        DraftWorksheetContent.CANDIDATE_THRESHOLD_LABEL,
                        encore_power_threshold,
                        "万総合力",
                    ]
                ],
            }
        )
        await draft_worksheet.batch_update_typed_values(
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
            frozen_column_count=1,
        )
        self.logger.info(
            "Generated shift draft in worksheet `%s`: %d hours, %d seats short.",
            draft_worksheet.title,
            len(schedule.hours),
            schedule.total_shortage,
        )
        return DraftGenerationResult(
            schedule=schedule,
            team_source_status=profile_resolution.status,
            team_source_warning=team_source_warning,
            recruitment_ranges=recruitment_ranges,
            notes_snapshot=notes_snapshot,
            unregistered_usernames=unregistered_usernames,
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
            (f"I{old_threshold_row}:K{old_threshold_row}", "#FFFFFF")
        )
    background_updates.extend(
        [
            (f"I{threshold_row}", "#A4C2F4"),
            (f"J{threshold_row}", "#FFF2CC"),
            (f"K{threshold_row}", "#A4C2F4"),
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
            (f"J{old_threshold_row}:K{old_threshold_row}", None, "NONE", BORDER_NAMES)
        )
    border_updates.extend(
        [
            (f"I1:I{threshold_row}", "#000000", "SOLID", ("left",)),
            (
                f"I{threshold_row}:K{threshold_row}",
                "#000000",
                "SOLID",
                ("bottom",),
            ),
            (
                f"J{threshold_row}",
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


async def _read_entry_state(
    worksheet: AsyncioGspreadWorksheet,
) -> tuple[
    list[dict[str, object]],
    list[list[object]],
    list[tuple[int, str, str]],
]:
    range_values = await worksheet.batch_get_values(ENTRY_READ_RANGES)
    if len(range_values) != len(ENTRY_READ_RANGES):
        msg = "Shift Entry batch read did not return both requested ranges."
        raise ValueError(msg)
    header_rows, participant_values = range_values
    return (
        _entry_layout_updates(header_rows, participant_values),
        participant_values,
        _entry_participants(participant_values),
    )


def _entry_layout_updates(
    header_rows: list[list[object]],
    participant_rows: list[list[object]],
) -> list[dict[str, object]]:
    count_row = _padded_row(header_rows[0] if header_rows else [], 36)
    header_row = _padded_row(header_rows[1] if len(header_rows) > 1 else [], 36)
    expected_count = EntryWorksheetContent.count_row()
    expected_header = EntryWorksheetContent.COLUMNS
    migration_header = [
        "username",
        "display_name",
        "",
        "",
        "",
        *EntryWorksheetContent.HOUR_COLUMNS,
        "original_message",
    ]

    allowed_count_columns = {0, *range(5, 35)}
    for index, value in enumerate(count_row):
        if index not in allowed_count_columns and not _is_blank(value):
            msg = "Shift Entry count row contains data outside its owned columns."
            raise ValueError(msg)
    if count_row[0] not in ("", "count"):
        msg = "Shift Entry count row must start with `count`."
        raise ValueError(msg)

    header_is_blank = all(_is_blank(value) for value in header_row)
    count_is_blank = all(_is_blank(value) for value in count_row)
    has_participants = any(row and not _is_blank(row[0]) for row in participant_rows)
    if header_is_blank:
        if not count_is_blank or has_participants:
            msg = "Shift Entry worksheet header is missing."
            raise ValueError(msg)
    elif header_row not in (expected_header, migration_header):
        msg = (
            "Shift Entry worksheet header must match the canonical or "
            "migration-ready layout."
        )
        raise ValueError(msg)

    updates: list[dict[str, object]] = []
    if count_row != expected_count:
        updates.append({"range": "A1:AJ1", "values": [expected_count]})
    if header_row != expected_header:
        updates.append({"range": "A2:AJ2", "values": [expected_header]})
    return updates


def _entry_participants(
    rows: list[list[object]],
) -> list[tuple[int, str, str]]:
    participants: list[tuple[int, str, str]] = []
    seen_usernames: set[str] = set()
    for row_number, values in enumerate(
        rows,
        start=EntryWorksheetContent.FIRST_DATA_ROW,
    ):
        row = _padded_row(values, 3)
        username = "" if _is_blank(row[0]) else str(row[0])
        formula = "" if _is_blank(row[2]) else str(row[2])
        if username in seen_usernames:
            msg = f"Duplicate Shift Entry username: {username!r}."
            raise ValueError(msg)
        if username:
            seen_usernames.add(username)
        participants.append((row_number, username, formula))
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


def _summary_value(row: list[object], column: int | None) -> object:
    if column is None or column > len(row):
        return ""
    return row[column - 1]


def _draft_profiles_from_summary(
    rows: list[list[object]],
    columns: TeamSummaryColumns,
) -> dict[str, DraftTeamProfile]:
    if not rows:
        msg = "Team Summary returned no header row."
        raise ValueError(msg)

    profiles: dict[str, DraftTeamProfile] = {}
    for row in rows[1:]:
        username = str(_summary_value(row, columns.username)).strip()
        if not username:
            continue
        if username in profiles:
            msg = f"Duplicate Team Summary username: {username!r}."
            raise ValueError(msg)
        profiles[username] = DraftTeamProfile(
            main_isv=_optional_float(_summary_value(row, columns.main_isv)),
            main_power=_optional_float(_summary_value(row, columns.main_power)),
            encore_isv=_optional_float(_summary_value(row, columns.encore_isv)),
            encore_power=_optional_float(_summary_value(row, columns.encore_power)),
            has_encore_role=bool(str(_summary_value(row, columns.roles)).strip()),
        )
    return profiles


def _unique_header_column(header: list[object], name: str) -> int:
    matches = [index for index, value in enumerate(header, start=1) if value == name]
    if len(matches) != 1:
        msg = f"Expected one Summary header {name!r}, found {len(matches)}."
        raise ValueError(msg)
    return matches[0]


def _optional_unique_header_column(header: list[object], name: str) -> int | None:
    matches = [index for index, value in enumerate(header, start=1) if value == name]
    return matches[0] if len(matches) == 1 else None
