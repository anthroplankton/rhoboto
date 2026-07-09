from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from tests.fakes import FakeWorksheet
from utils.shift_register_manager import ShiftRegisterManager
from utils.shift_register_structs import (
    DraftWorksheetMetadata,
    EntryWorksheetContent,
    EntryWorksheetMetadata,
    FinalScheduleWorksheetMetadata,
    ShiftParser,
    ShiftRegisterGoogleSheetsMetadata,
)
from utils.storage_errors import StorageError, StorageErrorKind
from utils.structs_base import UserInfo
from utils.team_register_manager import TeamRegisterManager
from utils.team_register_structs import TeamParser


def make_feature_channel(feature_name: str) -> SimpleNamespace:
    return SimpleNamespace(guild_id=1, channel_id=2, feature_name=feature_name)


def make_user(username: str = "alice", display_name: str = "Alice") -> UserInfo:
    return UserInfo(username=username, display_name=display_name)


@pytest.mark.asyncio
async def test_team_manager_fresh_config_invalidates_cached_google_sheet() -> None:
    manager = TeamRegisterManager(make_feature_channel("team_register"), "service.json")
    old_config = SimpleNamespace(sheet_url="https://old.sheet.example")
    new_config = SimpleNamespace(sheet_url="https://new.sheet.example")
    cached_sheet = SimpleNamespace(sheet_url=old_config.sheet_url)

    class FakeSheetConfig:
        @classmethod
        async def get_or_none(cls, *, feature_channel: object) -> SimpleNamespace:
            assert feature_channel is manager.feature_channel
            return new_config

    manager.SheetConfigType = FakeSheetConfig
    manager._sheet_config = old_config  # noqa: SLF001
    manager._google_sheet = cached_sheet  # noqa: SLF001

    refreshed_config = await manager.get_fresh_sheet_config()

    assert refreshed_config is new_config
    assert manager._sheet_config is new_config  # noqa: SLF001
    assert manager._google_sheet is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_shift_manager_fresh_config_invalidates_cached_google_sheet() -> None:
    manager = ShiftRegisterManager(
        make_feature_channel("shift_register"), "service.json"
    )
    old_config = SimpleNamespace(sheet_url="https://old.sheet.example")
    new_config = SimpleNamespace(sheet_url="https://new.sheet.example")
    cached_sheet = SimpleNamespace(sheet_url=old_config.sheet_url)

    class FakeSheetConfig:
        @classmethod
        async def get_or_none(cls, *, feature_channel: object) -> SimpleNamespace:
            assert feature_channel is manager.feature_channel
            return new_config

    manager.SheetConfigType = FakeSheetConfig
    manager._sheet_config = old_config  # noqa: SLF001
    manager._google_sheet = cached_sheet  # noqa: SLF001

    refreshed_config = await manager.get_fresh_sheet_config()

    assert refreshed_config is new_config
    assert manager._sheet_config is new_config  # noqa: SLF001
    assert manager._google_sheet is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_team_manager_upserts_and_deletes_user_team_with_fake_worksheet() -> None:
    manager = TeamRegisterManager(make_feature_channel("team_register"), "service.json")
    worksheet = FakeWorksheet(title="Main Team")
    user = make_user()
    team = TeamParser.parse_line(user, "150/740/33.4 main")

    await manager.upsert_or_delete_user_team(user, team, worksheet)

    inserted = worksheet.updated_frames[-1]
    assert inserted.loc[0, "username"] == "alice"
    assert inserted.loc[0, "leader_skill_value"] == 150

    await manager.upsert_or_delete_user_team(user, None, worksheet)

    deleted = worksheet.updated_frames[-1]
    assert "alice" not in set(deleted["username"].astype(str))


@pytest.mark.asyncio
async def test_shift_manager_upserts_deletes_user_shift_with_fake_worksheet() -> None:
    manager = ShiftRegisterManager(
        make_feature_channel("shift_register"), "service.json"
    )
    worksheet = FakeWorksheet(
        title="Shift Entry",
        frame=pd.DataFrame(columns=EntryWorksheetContent.COLUMNS),
    )
    metadata = ShiftRegisterGoogleSheetsMetadata(
        "https://sheet.example",
        [
            EntryWorksheetMetadata(1, "Shift Entry", worksheet),
            DraftWorksheetMetadata(2, "Shift Draft", None),
            FinalScheduleWorksheetMetadata(3, "Shift Final Schedule", None),
        ],
    )
    user = make_user()
    result = ShiftParser.parse_submission(user, ["15-17"])
    shift = result.shift
    assert shift is not None

    await manager.upsert_or_delete_user_shift(user, shift, metadata)

    inserted = worksheet.updated_frames[-1]
    assert inserted.loc[0, "username"] == "alice"
    assert inserted.loc[0, "15-16"] == 1
    assert inserted.loc[0, "17-18"] == 0
    assert "0-1" in inserted.columns
    assert "29-30" in inserted.columns

    await manager.upsert_or_delete_user_shift(user, None, metadata)

    deleted = worksheet.updated_frames[-1]
    assert "alice" not in set(deleted["username"].astype(str))


@pytest.mark.asyncio
async def test_shift_manager_initializes_empty_entry_worksheet() -> None:
    manager = ShiftRegisterManager(
        make_feature_channel("shift_register"), "service.json"
    )
    worksheet = FakeWorksheet(title="Shift Entry")
    metadata = ShiftRegisterGoogleSheetsMetadata(
        "https://sheet.example",
        [
            EntryWorksheetMetadata(1, "Shift Entry", worksheet),
            DraftWorksheetMetadata(2, "Shift Draft", None),
            FinalScheduleWorksheetMetadata(3, "Shift Final Schedule", None),
        ],
    )
    user = make_user()
    shift = ShiftParser.parse_submission(user, ["4-8"]).shift
    assert shift is not None

    await manager.upsert_or_delete_user_shift(user, shift, metadata)

    inserted = worksheet.updated_frames[-1]
    assert list(inserted.columns) == EntryWorksheetContent.COLUMNS
    assert inserted.loc[0, "username"] == "alice"
    assert inserted.loc[0, "4-5"] == 1
    assert inserted.loc[0, "8-9"] == 0


@pytest.mark.asyncio
async def test_shift_manager_rejects_old_entry_header_before_update() -> None:
    manager = ShiftRegisterManager(
        make_feature_channel("shift_register"), "service.json"
    )
    old_frame = pd.DataFrame(
        columns=[
            "username",
            "display_name",
            *[f"{hour}-{hour + 1}" for hour in range(4, 28)],
            "original_message",
        ],
    )
    worksheet = FakeWorksheet(title="Shift Entry", frame=old_frame)
    metadata = ShiftRegisterGoogleSheetsMetadata(
        "https://sheet.example",
        [
            EntryWorksheetMetadata(1, "Shift Entry", worksheet),
            DraftWorksheetMetadata(2, "Shift Draft", None),
            FinalScheduleWorksheetMetadata(3, "Shift Final Schedule", None),
        ],
    )
    user = make_user()
    shift = ShiftParser.parse_submission(user, ["4-8"]).shift
    assert shift is not None

    with pytest.raises(StorageError) as exc_info:
        await manager.upsert_or_delete_user_shift(user, shift, metadata)

    assert exc_info.value.kind is StorageErrorKind.MALFORMED_SHEET
    assert isinstance(exc_info.value.__cause__, ValueError)
    assert "Shift Entry worksheet header" in str(exc_info.value.__cause__)
    assert worksheet.updated_frames == []


@pytest.mark.asyncio
async def test_manager_skips_missing_worksheets_without_updates() -> None:
    team_manager = TeamRegisterManager(
        make_feature_channel("team_register"), "service.json"
    )
    shift_manager = ShiftRegisterManager(
        make_feature_channel("shift_register"), "service.json"
    )
    metadata = ShiftRegisterGoogleSheetsMetadata(
        "https://sheet.example",
        [
            EntryWorksheetMetadata(1, "Shift Entry", None),
            DraftWorksheetMetadata(2, "Shift Draft", None),
            FinalScheduleWorksheetMetadata(3, "Shift Final Schedule", None),
        ],
    )

    await team_manager.upsert_or_delete_user_team(make_user(), None, None)
    await shift_manager.upsert_or_delete_user_shift(make_user(), None, metadata)

    assert isinstance(metadata.sheet_url, str)


def test_fake_worksheet_returns_copies() -> None:
    original = pd.DataFrame({"username": ["alice"]})
    worksheet = FakeWorksheet(frame=original)

    original.loc[0, "username"] = "changed"

    assert worksheet.frame.loc[0, "username"] == "alice"
