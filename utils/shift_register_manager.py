from __future__ import annotations

from typing import TYPE_CHECKING, overload, override

from utils.structs_base import validate_anchor_cell

if TYPE_CHECKING:
    from datetime import date, datetime

    from utils.structs_base import UserInfo

from models.shift_register import ShiftRegisterConfig
from utils.google_sheets_errors import classify_google_sheets_exception
from utils.manager_base import ManagerBase
from utils.shift_register_structs import (
    EntryWorksheetContent,
    RecruitmentTimeRanges,
    Shift,
    ShiftRegisterGoogleSheetsMetadata,
)


class ShiftRegisterManager(
    ManagerBase[ShiftRegisterConfig, ShiftRegisterGoogleSheetsMetadata]
):
    SheetConfigType = ShiftRegisterConfig
    GoogleSheetsMetadataType = ShiftRegisterGoogleSheetsMetadata

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

        df = await worksheet.to_frame()
        try:
            EntryWorksheetContent.validate_core_header(df)
        except ValueError as exc:
            raise classify_google_sheets_exception(
                exc,
                operation="validate_shift_entry_header",
            ) from exc
        shift_df, plain_df = EntryWorksheetContent.standardize_dataframe(df)
        content = EntryWorksheetContent(shift_df, plain_df)

        if shift is None:
            content.delete(user.username)
        else:
            content.upsert(shift)

        updated_shift_df = content.to_frame()

        await worksheet.update_from_dataframe(updated_shift_df)

        if shift is None:
            self.logger.info(
                "Deleted shift registration for user %r from worksheet `%s`",
                user,
                worksheet.title,
            )
        else:
            self.logger.info(
                "Updated shift registration %r in worksheet `%s`",
                shift,
                worksheet.title,
            )

        self.logger.debug(
            "Updated shift registration %r in worksheet `%s`:\n%s",
            shift,
            worksheet.title,
            updated_shift_df,
        )
