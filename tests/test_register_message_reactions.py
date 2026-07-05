from __future__ import annotations

from types import MethodType, SimpleNamespace

import pandas as pd
import pytest

from bot import config
from cogs.base.feature_channel_base import FeatureChannelBase
from cogs.shift_register import ShiftRegister
from cogs.team_register import TeamRegister
from models.feature_channel import FeatureChannel
from tests.fakes import FakeWorksheet
from utils.shift_register_manager import ShiftRegisterManager
from utils.shift_register_structs import (
    DraftWorksheetMetadata,
    EntryWorksheetMetadata,
    FinalScheduleWorksheetMetadata,
    ShiftRegisterGoogleSheetsMetadata,
)


class FakeLogger:
    def debug(self, *_: object, **__: object) -> None:
        pass

    def info(self, *_: object, **__: object) -> None:
        pass

    def warning(self, *_: object, **__: object) -> None:
        pass


class NoInfoLogger(FakeLogger):
    def info(self, *_: object, **__: object) -> None:
        raise AssertionError


class FakeAuthor:
    def __init__(self) -> None:
        self.bot = False
        self.name = "alice"
        self.display_name = "Alice"
        self.roles: list[object] = []


class FakeMessage:
    id = 123

    def __init__(self, content: str) -> None:
        self.content = content
        self.author = FakeAuthor()
        self.guild = SimpleNamespace(id=111)
        self.channel = SimpleNamespace(id=222)
        self.added_reactions: list[str] = []
        self.removed_reactions: list[tuple[str, object]] = []

    async def add_reaction(self, emoji: str) -> None:
        self.added_reactions.append(emoji)

    async def remove_reaction(self, emoji: str, user: object) -> None:
        self.removed_reactions.append((emoji, user))


async def fake_enabled_feature_channel_get_or_none(
    *, guild_id: int, channel_id: int, feature_name: str
) -> SimpleNamespace:
    return SimpleNamespace(
        guild_id=guild_id,
        channel_id=channel_id,
        feature_name=feature_name,
        is_enabled=True,
    )


class DummyManager:
    def __init__(self, feature_channel: object, service_account_path: str) -> None:
        self.feature_channel = feature_channel
        self.service_account_path = service_account_path


def make_subject(feature_name: str) -> SimpleNamespace:
    if feature_name == "team_register":
        parse_message_submission = getattr(TeamRegister, "_parse_message_submission")
        process_configured = (
            getattr(TeamRegister, "_process_configured_message_submission")
        )
        manager_type = TeamRegister.ManagerType
    else:
        parse_message_submission = (
            getattr(ShiftRegister, "_parse_message_submission")
        )
        process_configured = (
            getattr(ShiftRegister, "_process_configured_message_submission")
        )
        manager_type = ShiftRegister.ManagerType

    subject = SimpleNamespace(
        feature_name=feature_name,
        logger=FakeLogger(),
        bot=SimpleNamespace(user=object()),
        ManagerType=manager_type,
    )
    subject._parse_message_submission = MethodType(  # noqa: SLF001
        parse_message_submission,
        subject,
    )
    subject._process_configured_message_submission = MethodType(  # noqa: SLF001
        process_configured,
        subject,
    )
    for method_name in (
        "_message_user_info",
        "_log_received_message",
        "_build_feature_channel_context",
        "_get_feature_channel_context_or_none",
        "_get_configured_feature_channel_context",
        "_get_message_feature_channel_context_or_none",
    ):
        method = getattr(FeatureChannelBase, method_name)
        setattr(subject, method_name, MethodType(method, subject))
    subject._get_enabled_feature_channel_or_none = (  # noqa: SLF001
        FeatureChannelBase._get_enabled_feature_channel_or_none  # noqa: SLF001
    )
    subject.process_upsert_from_message = MethodType(
        FeatureChannelBase.process_upsert_from_message,
        subject,
    )
    return subject


@pytest.mark.asyncio
async def test_team_message_invalid_attempt_adds_confused_without_sheets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        FeatureChannel,
        "get_or_none",
        fake_enabled_feature_channel_get_or_none,
    )
    monkeypatch.setattr(TeamRegister, "ManagerType", DummyManager)
    subject = make_subject("team_register")
    message = FakeMessage("160//600/33")

    result = await subject.process_upsert_from_message(message)

    assert result is None
    assert message.added_reactions == [config.CONFUSED_EMOJI]


@pytest.mark.asyncio
async def test_team_message_strict_mixed_rejects_with_confused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        FeatureChannel,
        "get_or_none",
        fake_enabled_feature_channel_get_or_none,
    )
    monkeypatch.setattr(TeamRegister, "ManagerType", DummyManager)
    subject = make_subject("team_register")
    message = FakeMessage("150/740/33.4\n160//600/33")

    result = await subject.process_upsert_from_message(message)

    assert result is None
    assert message.added_reactions == [config.CONFUSED_EMOJI]


@pytest.mark.asyncio
async def test_team_message_ordinary_text_adds_no_reaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        FeatureChannel,
        "get_or_none",
        fake_enabled_feature_channel_get_or_none,
    )
    monkeypatch.setattr(TeamRegister, "ManagerType", DummyManager)
    subject = make_subject("team_register")
    message = FakeMessage("公告")

    result = await subject.process_upsert_from_message(message)

    assert result is None
    assert message.added_reactions == []


@pytest.mark.asyncio
async def test_shift_message_invalid_attempt_adds_confused_without_sheets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        FeatureChannel,
        "get_or_none",
        fake_enabled_feature_channel_get_or_none,
    )
    monkeypatch.setattr(ShiftRegister, "ManagerType", DummyManager)
    subject = make_subject("shift_register")
    message = FakeMessage("18:00-20:00")

    result = await subject.process_upsert_from_message(message)

    assert result is None
    assert message.added_reactions == [config.CONFUSED_EMOJI]


@pytest.mark.asyncio
async def test_shift_message_strict_mixed_rejects_with_confused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        FeatureChannel,
        "get_or_none",
        fake_enabled_feature_channel_get_or_none,
    )
    monkeypatch.setattr(ShiftRegister, "ManagerType", DummyManager)
    subject = make_subject("shift_register")
    message = FakeMessage("4-12\n18:00-20:00")

    result = await subject.process_upsert_from_message(message)

    assert result is None
    assert message.added_reactions == [config.CONFUSED_EMOJI]


@pytest.mark.asyncio
async def test_shift_message_out_of_recruitment_range_rejects_before_sheets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeShiftRegisterManager:
        def __init__(self, *_: object) -> None:
            pass

        async def get_sheet_config_or_none(self) -> SimpleNamespace:
            return SimpleNamespace(
                recruitment_time_ranges=[{"start": 4, "end": 28}],
            )

        async def fetch_google_sheets_metadata(self) -> None:
            raise AssertionError

        async def ensure_worksheets_and_upsert_sheet_config(
            self,
            *_: object,
        ) -> None:
            raise AssertionError

        async def upsert_or_delete_user_shift(self, *_: object) -> None:
            raise AssertionError

    monkeypatch.setattr(
        FeatureChannel,
        "get_or_none",
        fake_enabled_feature_channel_get_or_none,
    )
    monkeypatch.setattr(ShiftRegister, "ManagerType", FakeShiftRegisterManager)
    subject = make_subject("shift_register")
    subject.logger = NoInfoLogger()
    message = FakeMessage("0-30")

    result = await subject.process_upsert_from_message(message)

    assert result is None
    assert message.added_reactions == [config.CONFUSED_EMOJI]


@pytest.mark.asyncio
async def test_shift_listener_marks_old_entry_header_google_sheets_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_frame = pd.DataFrame(
        columns=[
            "username",
            "display_name",
            *[f"{hour}-{hour + 1}" for hour in range(4, 28)],
            "original_message",
        ],
    )
    metadata = ShiftRegisterGoogleSheetsMetadata(
        "https://sheet.example",
        [
            EntryWorksheetMetadata(
                1,
                "Shift Entry",
                FakeWorksheet(title="Shift Entry", frame=old_frame),
            ),
            DraftWorksheetMetadata(2, "Shift Draft", None),
            FinalScheduleWorksheetMetadata(3, "Shift Final Schedule", None),
        ],
    )

    class OldHeaderShiftRegisterManager:
        upsert_or_delete_user_shift = ShiftRegisterManager.upsert_or_delete_user_shift

        def __init__(self, *_: object) -> None:
            pass

        async def get_sheet_config_or_none(self) -> SimpleNamespace:
            return SimpleNamespace(
                recruitment_time_ranges=[{"start": 4, "end": 28}],
            )

        async def fetch_google_sheets_metadata(
            self,
        ) -> ShiftRegisterGoogleSheetsMetadata:
            return metadata

        def log_missing_worksheet_warnings(
            self,
            _metadata: ShiftRegisterGoogleSheetsMetadata,
        ) -> None:
            pass

        async def ensure_worksheets_and_upsert_sheet_config(
            self,
            active_metadata: ShiftRegisterGoogleSheetsMetadata,
        ) -> ShiftRegisterGoogleSheetsMetadata:
            return active_metadata

    monkeypatch.setattr(
        FeatureChannel,
        "get_or_none",
        fake_enabled_feature_channel_get_or_none,
    )
    monkeypatch.setattr(ShiftRegister, "ManagerType", OldHeaderShiftRegisterManager)
    bot_user = object()
    subject = make_subject("shift_register")
    subject.bot = SimpleNamespace(user=bot_user)
    subject.lock = ShiftRegister.lock
    write_shift_registration = ShiftRegister._write_shift_registration  # noqa: SLF001
    subject._write_shift_registration = MethodType(  # noqa: SLF001
        write_shift_registration,
        subject,
    )
    message = FakeMessage("4-8")

    await FeatureChannelBase.on_message(subject, message)

    assert message.removed_reactions == [(config.PROCESSING_EMOJI, bot_user)]
    assert message.added_reactions == [
        config.PROCESSING_EMOJI,
        "⚠️",
        "🛠️",
    ]


@pytest.mark.asyncio
async def test_shift_message_ordinary_text_adds_no_reaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        FeatureChannel,
        "get_or_none",
        fake_enabled_feature_channel_get_or_none,
    )
    monkeypatch.setattr(ShiftRegister, "ManagerType", DummyManager)
    subject = make_subject("shift_register")
    message = FakeMessage("20:00")

    result = await subject.process_upsert_from_message(message)

    assert result is None
    assert message.added_reactions == []
