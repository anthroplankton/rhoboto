from __future__ import annotations

import logging
from abc import ABC
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from models.base.sheet_config_base import SheetConfigBase
from utils.google_sheets import GoogleSheet, WorksheetCreationStatus
from utils.google_sheets_errors import GoogleSheetsError, GoogleSheetsErrorKind
from utils.google_sheets_urls import extract_google_sheet_id, normalize_google_sheet_url
from utils.key_async_lock import KeyAsyncLock
from utils.storage_errors import partial_success_storage_error
from utils.structs_base import GoogleSheetsMetadata, WorksheetMetadata

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Hashable, Mapping

    from models.feature_channel import FeatureChannel


SPREADSHEET_TRANSACTION_LOCK = KeyAsyncLock()


def spreadsheet_transaction_key(sheet_url: str) -> str:
    """Return a safe, classified spreadsheet transaction key."""
    try:
        return extract_google_sheet_id(sheet_url)
    except ValueError as exc:
        raise GoogleSheetsError(
            GoogleSheetsErrorKind.INVALID_URL,
            "Check the Google Sheet link and save the settings again.",
        ) from exc


@asynccontextmanager
async def spreadsheet_transaction(
    feature_channel_lock: KeyAsyncLock,
    *,
    channel_id: Hashable,
    sheet_url: str,
) -> AsyncIterator[None]:
    """Lock a Sheet transaction in channel-first, spreadsheet-second order."""
    spreadsheet_id = spreadsheet_transaction_key(sheet_url)
    async with (
        feature_channel_lock(channel_id),
        SPREADSHEET_TRANSACTION_LOCK(spreadsheet_id),
    ):
        yield


class SheetConfigNotFoundError(Exception):
    """Raised when SheetConfigBase is not found for the feature channel."""

    def __init__(self, feature_channel: FeatureChannel) -> None:
        msg = (
            f"Sheet configuration for "
            f"Feature: `{feature_channel.feature_name}` in "
            f"Guild: `{feature_channel.guild_id}` and "
            f"Channel: `{feature_channel.channel_id}` was not found."
        )
        super().__init__(msg)


class ManagerBase[
    TSheetConfig: SheetConfigBase,
    TGoogleSheetsMetadata: GoogleSheetsMetadata,
](ABC):
    SheetConfigType: type[TSheetConfig]
    GoogleSheetsMetadataType: type[TGoogleSheetsMetadata]

    def __init__(
        self, feature_channel: FeatureChannel, service_account_path: str
    ) -> None:
        self.feature_channel = feature_channel
        self.service_account_path = service_account_path
        self._sheet_config: TSheetConfig | None = None
        self._google_sheet: GoogleSheet | None = None
        self.logger = logging.getLogger(self.__class__.__name__)

    async def get_sheet_config_or_none(self) -> TSheetConfig | None:
        if self._sheet_config is None:
            self._sheet_config = await self.SheetConfigType.get_or_none(
                feature_channel=self.feature_channel
            )
        return self._sheet_config

    async def get_sheet_config(self) -> TSheetConfig:
        if self._sheet_config is None:
            self._sheet_config = await self.get_sheet_config_or_none()
        if self._sheet_config is None:
            error = SheetConfigNotFoundError(self.feature_channel)
            raise error
        return self._sheet_config

    async def get_fresh_sheet_config(self) -> TSheetConfig | None:
        """Return current sheet config without using cached manager state."""
        self._sheet_config = None
        self._google_sheet = None
        return await self.get_sheet_config_or_none()

    async def get_google_sheet(self) -> GoogleSheet:
        """
        Get the GoogleSheet instance for the current TeamRegister.

        Returns:
            GoogleSheet: The GoogleSheet instance.
        """
        if self._google_sheet is None:
            sheet_config = await self.get_sheet_config()
            self._google_sheet = GoogleSheet(
                sheet_config.sheet_url, self.service_account_path
            )
        return self._google_sheet

    def log_missing_worksheet_warnings(self, metadata: TGoogleSheetsMetadata) -> None:
        """
        Log warnings for any missing worksheets in the metadata.

        Args:
            metadata (TGoogleSheetsMetadata):
                The metadata containing worksheet information.
        """
        guild_id = self.feature_channel.guild_id
        channel_id = self.feature_channel.channel_id
        feature_name = self.feature_channel.feature_name
        for ws in metadata:
            if not ws.is_missing():
                continue
            self.logger.warning(
                "Missing worksheet `%s` (ID:`%s`, Title: `%s`) for "
                "Feature: `%s` in Guild: `%s`, Channel: `%s`",
                ws.purpose,
                ws.id,
                ws.title,
                feature_name,
                guild_id,
                channel_id,
            )

    async def fetch_google_sheets_metadata(self) -> TGoogleSheetsMetadata:
        sheet = await self.get_google_sheet()
        sheet_config = await self.get_sheet_config()
        worksheet_ids = sheet_config.get_worksheet_ids()
        worksheets = await sheet.get_worksheets(worksheet_ids)
        return self.GoogleSheetsMetadataType.from_id_mapping(
            sheet.sheet_url, worksheets
        )

    async def create_or_get_worksheets(
        self,
        worksheet_titles: list[str],
        *,
        creation_status: WorksheetCreationStatus | None = None,
    ) -> TGoogleSheetsMetadata:
        sheet = await self.get_google_sheet()
        if creation_status is None:
            worksheets = await sheet.get_or_create_worksheets(worksheet_titles)
        else:
            worksheets = await sheet.get_or_create_worksheets(
                worksheet_titles,
                creation_status=creation_status,
            )
        return self.GoogleSheetsMetadataType.from_title_mapping(
            sheet.sheet_url, dict(worksheets)
        )

    async def upsert_sheet_config(
        self,
        metadata: TGoogleSheetsMetadata,
        *,
        extra_defaults: Mapping[str, object] | None = None,
    ) -> None:
        defaults: dict = {
            "sheet_url": metadata.sheet_url,
        }
        for ws in metadata:
            if ws.is_collection_field:
                defaults.setdefault(ws.db_field, []).append(ws.id)
            else:
                defaults[ws.db_field] = ws.id
        defaults.update(extra_defaults or {})

        self._sheet_config, _ = await self.SheetConfigType.update_or_create(
            feature_channel=self.feature_channel, defaults=defaults
        )

        if (
            self._google_sheet is not None
            and self._google_sheet.sheet_url != metadata.sheet_url
        ):
            self._google_sheet = None  # Will be recreated on next access

    async def upsert_sheet_config_and_worksheets(
        self,
        sheet_url: str,
        worksheet_titles: list[str],
    ) -> TGoogleSheetsMetadata:
        sheet_url = normalize_google_sheet_url(sheet_url)
        self._google_sheet = GoogleSheet(sheet_url, self.service_account_path)

        metadata = await self.create_or_get_worksheets(worksheet_titles)

        await self.upsert_sheet_config(metadata)

        return metadata

    async def ensure_worksheets(
        self,
        metadata: TGoogleSheetsMetadata,
        counts: dict[type[WorksheetMetadata], int] | None = None,
        *,
        creation_status: WorksheetCreationStatus | None = None,
    ) -> TGoogleSheetsMetadata:
        counts = counts or {}
        if all(ws.worksheet is not None for ws in metadata) and all(
            sum(type(ws) is worksheet_type for ws in metadata) >= count
            for worksheet_type, count in counts.items()
        ):
            return metadata
        ensured_metadata = self.GoogleSheetsMetadataType.assign_missing_default_titles(
            metadata, counts
        )

        updated_metadata = await self.create_or_get_worksheets(
            [ws.title for ws in ensured_metadata if ws.title is not None],
            creation_status=creation_status,
        )

        return ensured_metadata.extended_by_title(updated_metadata)

    async def ensure_worksheets_and_upsert_sheet_config(
        self,
        metadata: TGoogleSheetsMetadata,
        counts: dict[type[WorksheetMetadata], int] | None = None,
    ) -> TGoogleSheetsMetadata:
        creation_status = WorksheetCreationStatus()
        original_ids = [worksheet.id for worksheet in metadata]
        try:
            ensured_metadata = await self.ensure_worksheets(
                metadata,
                counts,
                creation_status=creation_status,
            )
            if [worksheet.id for worksheet in ensured_metadata] != original_ids:
                await self.upsert_sheet_config(ensured_metadata)
        except Exception as exc:
            if not creation_status.created:
                raise
            error = partial_success_storage_error(exc)
            if error is None:
                raise
            raise error from error.__cause__
        return ensured_metadata
