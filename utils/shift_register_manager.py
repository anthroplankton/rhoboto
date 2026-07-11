from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, overload, override

from models.team_register import TeamRegisterConfig
from utils.google_sheets import GoogleSheet
from utils.google_sheets_errors import GoogleSheetsError, GoogleSheetsErrorKind
from utils.structs_base import validate_anchor_cell
from utils.team_register_structs import Summary

if TYPE_CHECKING:
    from datetime import date, datetime

    from utils.google_sheets import AsyncioGspreadWorksheet
    from utils.structs_base import UserInfo

from models.shift_register import ShiftRegisterConfig
from utils.manager_base import ManagerBase
from utils.shift_register_structs import (
    EntryWorksheetContent,
    RecruitmentTimeRanges,
    Shift,
    ShiftRegisterGoogleSheetsMetadata,
    build_team_summary_formula,
    column_letter,
)
from utils.storage_errors import StorageError, StorageErrorKind

ENTRY_READ_RANGES = ["1:2", "A3:C"]


class TeamSummarySourceStatus(StrEnum):
    AVAILABLE = "available"
    MISSING = "missing"
    AMBIGUOUS = "ambiguous"
    INVALID = "invalid"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class TeamSummaryFormulaSource:
    channel_id: int
    sheet_url: str
    worksheet_id: int
    worksheet_title: str
    username_column: int
    roles_column: int
    main_isv_column: int
    encore_isv_column: int | None
    import_last_column: str


@dataclass(frozen=True)
class TeamSummarySourceResolution:
    status: TeamSummarySourceStatus
    source: TeamSummaryFormulaSource | None = None


class ShiftRegisterManager(
    ManagerBase[ShiftRegisterConfig, ShiftRegisterGoogleSheetsMetadata]
):
    SheetConfigType = ShiftRegisterConfig
    GoogleSheetsMetadataType = ShiftRegisterGoogleSheetsMetadata

    async def resolve_team_summary_source(self) -> TeamSummarySourceResolution:
        """Resolve the sole configured Team Summary source in this guild."""
        configs = await TeamRegisterConfig.filter(
            feature_channel__guild_id=self.feature_channel.guild_id
        ).select_related("feature_channel")
        if not configs:
            return TeamSummarySourceResolution(TeamSummarySourceStatus.MISSING)
        if len(configs) > 1:
            return TeamSummarySourceResolution(TeamSummarySourceStatus.AMBIGUOUS)

        config = configs[0]
        worksheet_ids = [
            *config.team_worksheet_ids,
            config.summary_worksheet_id,
        ]
        try:
            sheet = GoogleSheet(config.sheet_url, self.service_account_path)
            worksheets = await sheet.get_worksheets(worksheet_ids)
            return await self._resolve_team_summary_worksheets(config, worksheets)
        except GoogleSheetsError as exc:
            status = (
                TeamSummarySourceStatus.INVALID
                if exc.kind
                in {
                    GoogleSheetsErrorKind.INVALID_URL,
                    GoogleSheetsErrorKind.MISSING_WORKSHEET,
                }
                else TeamSummarySourceStatus.UNRESOLVED
            )
            self.logger.warning(
                "Could not resolve auxiliary Team Summary source: %s",
                exc.kind,
            )
            return TeamSummarySourceResolution(status)

    async def _resolve_team_summary_worksheets(
        self,
        config: TeamRegisterConfig,
        worksheets: dict[int, object | None],
    ) -> TeamSummarySourceResolution:
        team_worksheets = [
            worksheets.get(worksheet_id) for worksheet_id in config.team_worksheet_ids
        ]
        summary_worksheet = worksheets.get(config.summary_worksheet_id)
        if (
            not team_worksheets
            or any(worksheet is None for worksheet in team_worksheets)
            or summary_worksheet is None
        ):
            return TeamSummarySourceResolution(TeamSummarySourceStatus.INVALID)

        summary_values = await summary_worksheet.batch_get_values(["1:1"])
        header = summary_values[0][0] if summary_values and summary_values[0] else []
        if not isinstance(header, list):
            return TeamSummarySourceResolution(TeamSummarySourceStatus.INVALID)

        main_worksheet = team_worksheets[0]
        encore_worksheet = team_worksheets[1] if len(team_worksheets) > 1 else None
        try:
            username_column = _unique_header_column(header, "username")
            roles_column = _unique_header_column(header, "encore_roles")
            main_isv_column = _unique_header_column(
                header,
                Summary.isv_title(main_worksheet.title),
            )
            encore_isv_column = (
                _unique_header_column(
                    header,
                    Summary.isv_title(encore_worksheet.title),
                )
                if encore_worksheet is not None
                else None
            )
        except ValueError:
            return TeamSummarySourceResolution(TeamSummarySourceStatus.INVALID)

        source = TeamSummaryFormulaSource(
            channel_id=config.feature_channel.channel_id,
            sheet_url=config.sheet_url,
            worksheet_id=config.summary_worksheet_id,
            worksheet_title=summary_worksheet.title,
            username_column=username_column,
            roles_column=roles_column,
            main_isv_column=main_isv_column,
            encore_isv_column=encore_isv_column,
            import_last_column=column_letter(len(header)),
        )
        return TeamSummarySourceResolution(
            TeamSummarySourceStatus.AVAILABLE,
            source,
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
        resolution = await self.resolve_team_summary_source()

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
        await worksheet.batch_update_values(updates)

        self.logger.info(
            "Updated shift registration %r in worksheet `%s`",
            shift,
            worksheet.title,
        )


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
    resolution: TeamSummarySourceResolution,
) -> str | None:
    if resolution.status is TeamSummarySourceStatus.UNRESOLVED:
        return None
    source = resolution.source
    if resolution.status is not TeamSummarySourceStatus.AVAILABLE or source is None:
        return ""
    return build_team_summary_formula(
        row=row,
        sheet_url=source.sheet_url,
        worksheet_title=source.worksheet_title,
        username_column=source.username_column,
        roles_column=source.roles_column,
        main_isv_column=source.main_isv_column,
        encore_isv_column=source.encore_isv_column,
        import_last_column=source.import_last_column,
    )


def _padded_row(row: list[object], width: int) -> list[object]:
    return [*row[:width], *([""] * max(0, width - len(row)))]


def _is_blank(value: object) -> bool:
    return value in ("", None)


def _unique_header_column(header: list[object], name: str) -> int:
    matches = [index for index, value in enumerate(header, start=1) if value == name]
    if len(matches) != 1:
        msg = f"Expected one Summary header {name!r}, found {len(matches)}."
        raise ValueError(msg)
    return matches[0]
