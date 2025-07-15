from __future__ import annotations

from typing import TYPE_CHECKING, overload, override

from utils.structs_base import validate_anchor_cell

if TYPE_CHECKING:

    from utils.structs_base import UserInfo

from models.shift_register import ShiftRegisterConfig
from utils.manager_base import ManagerBase
from utils.shift_register_structs import (
    EntryWorksheetContent,
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
