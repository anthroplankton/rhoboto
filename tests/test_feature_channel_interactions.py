from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest
from discord import Embed

from bot import config
from cogs.base import feature_channel_base
from cogs.base.discord_context import require_guild_channel_source
from cogs.base.feature_channel_base import (
    FeatureChannelBase,
    FeatureChannelUserBase,
    FeatureNotEnabled,
)
from cogs.base.feature_context import (
    ConfiguredFeatureContext,
    FeatureContextMixin,
    MessageParseResult,
)
from cogs.shift import Shift
from cogs.shift_register import ShiftRegister
from cogs.team import Team
from cogs.team_register import TeamRegister
from components.ui_settings_flow import SettingsPanel, SettingsTimeoutView
from models.feature_channel import FeatureChannel
from tests.fakes import (
    ConfiguredManager,
    FakeContext,
    FakeInteraction,
    MissingConfigManager,
)
from utils.announcement_languages import RenderedAnnouncement
from utils.google_sheets_errors import GoogleSheetsError, GoogleSheetsErrorKind
from utils.structs_base import UserInfo


async def fake_feature_channel_get(
    *, guild_id: int, channel_id: int, feature_name: str
) -> SimpleNamespace:
    return SimpleNamespace(
        guild_id=guild_id,
        channel_id=channel_id,
        feature_name=feature_name,
    )


async def fake_feature_channel_get_or_none(
    *, guild_id: int, channel_id: int, feature_name: str
) -> SimpleNamespace:
    return SimpleNamespace(
        guild_id=guild_id,
        channel_id=channel_id,
        feature_name=feature_name,
        is_enabled=True,
    )


class FakeMessage:
    id = 123

    def __init__(self) -> None:
        self.added_reactions: list[str] = []
        self.removed_reactions: list[tuple[str, object]] = []

    async def add_reaction(self, emoji: str) -> None:
        self.added_reactions.append(emoji)

    async def remove_reaction(self, emoji: str, user: object) -> None:
        self.removed_reactions.append((emoji, user))


class FakeRegisterMessage(FakeMessage):
    def __init__(self, *, content: str = "hello", author_bot: bool = False) -> None:
        super().__init__()
        self.content = content
        self.author = SimpleNamespace(
            bot=author_bot,
            name="alice",
            display_name="Alice",
        )
        self.guild = SimpleNamespace(id=111)
        self.channel = SimpleNamespace(id=222)


class NullLogger:
    def warning(self, *_: object, **__: object) -> None:
        pass

    def debug(self, *_: object, **__: object) -> None:
        pass


class ConfiguredShiftInfoManager(ConfiguredManager):
    async def get_sheet_config_or_none(self) -> SimpleNamespace:
        return SimpleNamespace(
            sheet_url="https://sheet.example",
            day_number=2,
            event_date=dt.date(2026, 8, 12),
            submission_deadline_at=dt.datetime(2026, 8, 12, 12, tzinfo=dt.UTC),
            draft_shift_proposal_at=dt.datetime(2026, 8, 13, 11, tzinfo=dt.UTC),
            final_shift_notice_at=dt.datetime(2026, 8, 14, 9, tzinfo=dt.UTC),
            recruitment_time_ranges=[{"start": 4, "end": 28}],
        )


class ConfiguredMultiRangeShiftInfoManager(ConfiguredShiftInfoManager):
    async def get_sheet_config_or_none(self) -> SimpleNamespace:
        config = await super().get_sheet_config_or_none()
        config.recruitment_time_ranges = [
            {"start": 4, "end": 20},
            {"start": 24, "end": 28},
        ]
        return config


class RecordingLock:
    def __init__(self) -> None:
        self.keys: list[int] = []

    def __call__(self, key: int) -> RecordingLock:
        self.keys.append(key)
        return self

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_: object) -> bool:
        return False


class UnexpectedTeamRegisterManager:
    def __init__(self, *_: object, **__: object) -> None:
        msg = "summary should use self.ManagerType"
        raise AssertionError(msg)


class SummaryManager(ConfiguredManager):
    last_instance: SummaryManager | None = None
    summary_dataframe = object()

    def __init__(self, feature_channel: object, service_account_path: str) -> None:
        super().__init__(feature_channel, service_account_path)
        self.metadata = SimpleNamespace(name="metadata")
        self.ensured_metadata = SimpleNamespace(name="ensured_metadata")
        self.logged_metadata: object | None = None
        self.ensure_count: int | None = None
        self.refresh_metadata: object | None = None
        self.member_by_names: dict[str, object] | None = None
        SummaryManager.last_instance = self

    async def fetch_google_sheets_metadata(self) -> SimpleNamespace:
        return self.metadata

    def log_missing_worksheet_warnings(self, metadata: object) -> None:
        self.logged_metadata = metadata

    async def ensure_worksheets_and_upsert_sheet_config(
        self,
        metadata: object,
        *,
        count: int | None = None,
    ) -> SimpleNamespace:
        self.ensure_count = count
        assert metadata is self.metadata
        return self.ensured_metadata

    async def refresh_summary_worksheet(
        self,
        metadata: object,
        *,
        member_by_names: dict[str, object],
    ) -> object:
        self.refresh_metadata = metadata
        self.member_by_names = member_by_names
        return self.summary_dataframe


class DeleteManager(ConfiguredManager):
    last_instance: DeleteManager | None = None

    def __init__(self, feature_channel: object, service_account_path: str) -> None:
        super().__init__(feature_channel, service_account_path)
        self.metadata = SimpleNamespace(name="delete_metadata")
        DeleteManager.last_instance = self

    async def fetch_google_sheets_metadata(self) -> SimpleNamespace:
        return self.metadata


class UnexpectedSetupManager:
    def __init__(self, *_: object, **__: object) -> None:
        msg = "setup_after_enable should use self.ManagerType"
        raise AssertionError(msg)


class PanelManager(ConfiguredManager):
    last_instance: PanelManager | None = None

    def __init__(self, feature_channel: object, service_account_path: str) -> None:
        super().__init__(feature_channel, service_account_path)
        PanelManager.last_instance = self


class MessageOrchestrationManager(ConfiguredManager):
    last_instance: MessageOrchestrationManager | None = None

    def __init__(self, feature_channel: object, service_account_path: str) -> None:
        super().__init__(feature_channel, service_account_path)
        MessageOrchestrationManager.last_instance = self


class MissingMessageConfigManager(MessageOrchestrationManager):
    async def get_sheet_config_or_none(self) -> None:
        return None


class RecordingMessageSubject(FeatureContextMixin[MessageOrchestrationManager]):
    feature_name = "team_register"
    ManagerType = MessageOrchestrationManager

    def __init__(self, parse_result: MessageParseResult[object]) -> None:
        self.parse_result = parse_result
        self.logger = NullLogger()
        self.bot = SimpleNamespace(user=object())
        self.configured_calls: list[
            tuple[
                object,
                ConfiguredFeatureContext[MessageOrchestrationManager],
                object,
                UserInfo,
            ]
        ] = []

    async def _get_message_feature_manager_context_or_none(
        self,
        message: object,
    ) -> object | None:
        return await FeatureChannelBase._get_message_feature_manager_context_or_none(  # noqa: SLF001
            self,
            message,
        )

    def _log_received_message(self, message: object) -> None:
        FeatureChannelBase._log_received_message(self, message)  # noqa: SLF001

    async def _parse_message_submission(
        self,
        _message: object,
    ) -> MessageParseResult[object]:
        return self.parse_result

    async def _process_configured_message_submission(
        self,
        message: object,
        context: ConfiguredFeatureContext[MessageOrchestrationManager],
        submission: object,
        user_info: UserInfo,
    ) -> str:
        self.configured_calls.append((message, context, submission, user_info))
        return "processed"


def fake_bot() -> SimpleNamespace:
    return SimpleNamespace(
        tree=SimpleNamespace(add_command=lambda _command: None),
        user=None,
    )


def feature_context_subject(**attributes: object) -> SimpleNamespace:
    subject = SimpleNamespace(**attributes)
    for method_name in (
        "_get_feature_manager_context",
        "_get_configured_feature_context",
        "_build_feature_manager_context",
        "_send_missing_config_followup",
    ):
        method = getattr(FeatureChannelBase, method_name)
        setattr(subject, method_name, method.__get__(subject, type(subject)))
    return subject


def test_old_configured_feature_helpers_are_removed() -> None:
    assert not hasattr(feature_channel_base, "_get_interaction_channel_context")
    assert not hasattr(FeatureChannelBase, "_get_interaction_channel_context")
    assert not hasattr(feature_channel_base, "_get_configured_feature_context")
    assert not hasattr(feature_channel_base, "get_configured_feature_context")
    assert not hasattr(feature_channel_base, "send_public_announcement_followups")


@pytest.mark.asyncio
async def test_configured_feature_context_exposes_feature_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(FeatureChannel, "get", fake_feature_channel_get)
    monkeypatch.setattr(TeamRegister, "ManagerType", ConfiguredManager)
    interaction = FakeInteraction()
    subject = TeamRegister(fake_bot())

    source = require_guild_channel_source(
        interaction,
        action="inspect feature context",
    )
    manager_context = await FeatureChannelBase._get_feature_manager_context(  # noqa: SLF001
        subject,
        source,
    )
    context = await FeatureChannelBase._get_configured_feature_context(  # noqa: SLF001
        subject,
        manager_context,
    )

    assert context is not None
    assert context.guild_id == 111
    assert context.channel_id == 222
    assert not hasattr(context, "guild")
    assert context.feature_channel.guild_id == 111
    assert context.feature_channel.channel_id == 222
    assert context.feature_channel.feature_name == "team_register"
    assert isinstance(context.manager, ConfiguredManager)
    assert context.manager.feature_channel is context.feature_channel
    assert context.feature_config.sheet_url == "https://sheet.example"
    assert not hasattr(context, "sheet_config")


@pytest.mark.asyncio
async def test_configured_feature_context_missing_config_is_pure_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(FeatureChannel, "get", fake_feature_channel_get)
    monkeypatch.setattr(TeamRegister, "ManagerType", MissingConfigManager)
    interaction = FakeInteraction()
    subject = TeamRegister(fake_bot())

    source = require_guild_channel_source(
        interaction,
        action="inspect feature context",
    )
    manager_context = await FeatureChannelBase._get_feature_manager_context(  # noqa: SLF001
        subject,
        source,
    )
    context = await FeatureChannelBase._get_configured_feature_context(  # noqa: SLF001
        subject,
        manager_context,
    )

    assert context is None
    assert interaction.followup.messages == []


@pytest.mark.asyncio
async def test_is_enabled_uses_enabled_feature_channel_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[int, int, str | None]] = []

    async def fake_enabled_lookup(
        _cls: type[FeatureChannelBase],
        guild_id: int,
        channel_id: int,
        feature_name: str | None = None,
    ) -> object | None:
        calls.append((guild_id, channel_id, feature_name))
        if feature_name == "team_register":
            return object()
        return None

    monkeypatch.setattr(
        FeatureChannelBase,
        "_get_enabled_feature_channel_or_none",
        classmethod(fake_enabled_lookup),
    )

    assert await FeatureChannelBase.is_enabled(111, 222, "team_register") is True
    assert await FeatureChannelBase.is_enabled(111, 222, "shift_register") is False
    assert calls == [
        (111, 222, "team_register"),
        (111, 222, "shift_register"),
    ]


@pytest.mark.asyncio
async def test_app_command_predicate_uses_lookup_key_and_display_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[int, int, str | None]] = []

    async def disabled_lookup(
        _cls: type[FeatureChannelBase],
        guild_id: int,
        channel_id: int,
        feature_name: str | None = None,
    ) -> object | None:
        calls.append((guild_id, channel_id, feature_name))
        return None

    monkeypatch.setattr(
        FeatureChannelBase,
        "_get_enabled_feature_channel_or_none",
        classmethod(disabled_lookup),
    )
    predicate = FeatureChannelBase.feature_enabled_app_command_predicate(
        "team_register",
        "Team Register",
    )

    with pytest.raises(FeatureNotEnabled, match="Team Register is not enabled"):
        await predicate(FakeInteraction())

    assert calls == [(111, 222, "team_register")]


@pytest.mark.asyncio
async def test_prefix_command_predicate_uses_lookup_key_and_display_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[int, int, str | None]] = []

    async def disabled_lookup(
        _cls: type[FeatureChannelBase],
        guild_id: int,
        channel_id: int,
        feature_name: str | None = None,
    ) -> object | None:
        calls.append((guild_id, channel_id, feature_name))
        return None

    monkeypatch.setattr(
        FeatureChannelBase,
        "_get_enabled_feature_channel_or_none",
        classmethod(disabled_lookup),
    )
    predicate = FeatureChannelBase.feature_enabled_prefix_command_predicate(
        "team_register",
        "Team Register",
    )
    ctx = FakeContext()

    with pytest.raises(FeatureNotEnabled, match="Team Register is not enabled"):
        await predicate(ctx)

    assert calls == [(111, 222, "team_register")]


@pytest.mark.asyncio
async def test_enable_response_uses_feature_display_name() -> None:
    setup_calls: list[object] = []

    async def fake_enable_channel(_guild_id: int, _channel_id: int) -> None:
        return None

    async def fake_setup_after_enable(interaction: object) -> None:
        setup_calls.append(interaction)

    subject = feature_context_subject(
        feature_name="team_register",
        feature_display_name="Team Register",
        _enable_channel=fake_enable_channel,
        setup_after_enable=fake_setup_after_enable,
    )
    interaction = FakeInteraction()

    await FeatureChannelBase.enable.callback(subject, interaction)

    assert interaction.response.messages == [
        ("Feature Team Register enabled in this channel.", {"ephemeral": True})
    ]
    assert setup_calls == [interaction]


@pytest.mark.asyncio
async def test_disable_response_uses_feature_display_name() -> None:
    async def fake_disable_channel(_guild_id: int, _channel_id: int) -> bool:
        return True

    subject = feature_context_subject(
        feature_name="team_register",
        feature_display_name="Team Register",
        _disable_channel=fake_disable_channel,
    )
    interaction = FakeInteraction()

    await FeatureChannelBase.disable.callback(subject, interaction)

    assert interaction.response.messages == [
        ("Feature Team Register disabled in this channel.", {"ephemeral": True})
    ]


@pytest.mark.asyncio
async def test_disable_response_uses_feature_display_name_when_not_enabled() -> None:
    async def fake_disable_channel(_guild_id: int, _channel_id: int) -> bool:
        return False

    subject = feature_context_subject(
        feature_name="team_register",
        feature_display_name="Team Register",
        _disable_channel=fake_disable_channel,
    )
    interaction = FakeInteraction()

    await FeatureChannelBase.disable.callback(subject, interaction)

    assert interaction.response.messages == [
        ("Feature Team Register is not enabled in this channel.", {"ephemeral": True})
    ]


@pytest.mark.asyncio
async def test_user_help_defers_before_followup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(FeatureChannel, "get", fake_feature_channel_get)
    interaction = FakeInteraction(locale="zh-TW")
    subject = feature_context_subject(
        feature_name="team_register",
        ManagerType=ConfiguredManager,
        bot=SimpleNamespace(user=SimpleNamespace(mention="@Rhoboto")),
    )

    await FeatureChannelUserBase.send_help_message(subject, interaction, "team.help")

    assert interaction.response.deferred == [True]
    message, kwargs = interaction.followup.messages[0]
    assert kwargs["ephemeral"] is True
    assert "@Rhoboto" in str(message)
    assert "https://sheet.example" in str(message)


@pytest.mark.asyncio
async def test_message_processing_helpers_build_context_and_user_info(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(FeatureChannel, "get_or_none", fake_feature_channel_get_or_none)
    subject = RecordingMessageSubject(MessageParseResult.ignored())
    message = FakeRegisterMessage(content="150/740/33")
    message_user_info = FeatureChannelBase._message_user_info  # noqa: SLF001
    log_received_message = FeatureChannelBase._log_received_message  # noqa: SLF001

    manager_context = (
        await FeatureChannelBase._get_message_feature_manager_context_or_none(  # noqa: SLF001
            subject,
            message,
        )
    )
    user_info = message_user_info(subject, message)
    log_received_message(subject, message)

    assert manager_context is not None
    assert manager_context.guild_id == 111
    assert manager_context.channel_id == 222
    assert user_info.username == "alice"
    assert user_info.display_name == "Alice"


@pytest.mark.asyncio
async def test_message_processing_helper_ignores_bot_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_if_called(*_: object, **__: object) -> object | None:
        raise AssertionError

    monkeypatch.setattr(FeatureChannel, "get_or_none", fail_if_called)
    subject = RecordingMessageSubject(MessageParseResult.ignored())
    message = FakeRegisterMessage(author_bot=True)

    manager_context = (
        await FeatureChannelBase._get_message_feature_manager_context_or_none(  # noqa: SLF001
            subject,
            message,
        )
    )

    assert manager_context is None


@pytest.mark.asyncio
async def test_base_message_orchestration_ignored_skips_config_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(FeatureChannel, "get_or_none", fake_feature_channel_get_or_none)
    subject = RecordingMessageSubject(MessageParseResult.ignored())
    message = FakeRegisterMessage(content="公告")

    result = await FeatureChannelBase.process_upsert_from_message(subject, message)

    assert result is None
    assert message.added_reactions == []
    assert subject.configured_calls == []


@pytest.mark.asyncio
async def test_base_message_orchestration_invalid_adds_confused_without_config_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(FeatureChannel, "get_or_none", fake_feature_channel_get_or_none)
    user_info = UserInfo(username="alice", display_name="Alice")
    subject = RecordingMessageSubject(MessageParseResult.invalid(user_info=user_info))
    message = FakeRegisterMessage(content="160//600/33")

    result = await FeatureChannelBase.process_upsert_from_message(subject, message)

    assert result is None
    assert message.added_reactions == [config.CONFUSED_EMOJI]
    assert subject.configured_calls == []


@pytest.mark.asyncio
async def test_base_message_orchestration_missing_config_debug_logs_and_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(FeatureChannel, "get_or_none", fake_feature_channel_get_or_none)
    user_info = UserInfo(username="alice", display_name="Alice")
    subject = RecordingMessageSubject(
        MessageParseResult.parsed("submission", user_info=user_info)
    )
    subject.ManagerType = MissingMessageConfigManager
    debug_messages: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def record_debug(*args: object, **kwargs: object) -> None:
        debug_messages.append((args, kwargs))

    subject.logger = SimpleNamespace(debug=record_debug)
    message = FakeRegisterMessage(content="150/740/33")

    result = await FeatureChannelBase.process_upsert_from_message(subject, message)

    assert result is None
    assert message.added_reactions == []
    assert subject.configured_calls == []
    assert any(
        args
        and "has no feature config" in str(args[0])
        and args[1:4] == ("team_register", 111, 222)
        for args, _kwargs in debug_messages
    )


@pytest.mark.asyncio
async def test_base_message_orchestration_configured_calls_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(FeatureChannel, "get_or_none", fake_feature_channel_get_or_none)
    MessageOrchestrationManager.last_instance = None
    user_info = UserInfo(username="alice", display_name="Alice")
    subject = RecordingMessageSubject(
        MessageParseResult.parsed("submission", user_info=user_info)
    )
    message = FakeRegisterMessage(content="150/740/33")

    result = await FeatureChannelBase.process_upsert_from_message(subject, message)

    assert result == "processed"
    assert len(subject.configured_calls) == 1
    call_message, context, submission, call_user_info = subject.configured_calls[0]
    assert call_message is message
    assert context.manager is MessageOrchestrationManager.last_instance
    assert context.feature_config.sheet_url == "https://sheet.example"
    assert submission == "submission"
    assert call_user_info is user_info


@pytest.mark.asyncio
async def test_team_inherited_message_upsert_ignores_bot_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_if_called(**_: object) -> object | None:
        msg = "bot-authored messages should not look up feature channels"
        raise AssertionError(msg)

    monkeypatch.setattr(FeatureChannel, "get_or_none", fail_if_called)
    subject = TeamRegister(fake_bot())
    message = FakeRegisterMessage(author_bot=True)

    result = await subject.process_upsert_from_message(message)

    assert result is None
    assert message.added_reactions == []


@pytest.mark.asyncio
async def test_shift_inherited_message_upsert_ignores_bot_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_if_called(**_: object) -> object | None:
        msg = "bot-authored messages should not look up feature channels"
        raise AssertionError(msg)

    monkeypatch.setattr(FeatureChannel, "get_or_none", fail_if_called)
    subject = ShiftRegister(fake_bot())
    message = FakeRegisterMessage(author_bot=True)

    result = await subject.process_upsert_from_message(message)

    assert result is None
    assert message.added_reactions == []


@pytest.mark.asyncio
async def test_context_menu_reports_google_sheets_error_safely() -> None:
    bot_user = object()
    message = FakeMessage()

    async def raise_google_sheets_error(message: FakeMessage) -> None:
        await message.add_reaction("<:haruka_math:1402204882492063825>")
        raise GoogleSheetsError(
            GoogleSheetsErrorKind.QUOTA,
            "Google Sheets is rate-limiting requests. Try again later.",
        )

    interaction = FakeInteraction()
    subject = feature_context_subject(
        feature_name="team_register",
        process_upsert_from_message=raise_google_sheets_error,
        bot=SimpleNamespace(user=bot_user),
        logger=NullLogger(),
    )

    await FeatureChannelBase.upsert_from_content_menu(
        subject,
        interaction,
        message,
    )

    assert interaction.response.deferred == [False]
    assert interaction.followup.messages == [
        (
            "Google Sheets could not complete this action. "
            "Google Sheets is rate-limiting requests. Try again later.",
            {"ephemeral": False},
        )
    ]
    assert message.removed_reactions == [
        ("<:haruka_math:1402204882492063825>", bot_user)
    ]
    assert message.added_reactions == [
        "<:haruka_math:1402204882492063825>",
        "⚠️",
        "🛠️",
    ]


@pytest.mark.asyncio
async def test_context_menu_invalid_attempt_keeps_processor_reaction() -> None:
    message = FakeMessage()

    async def process_invalid_attempt(message: FakeMessage) -> None:
        await message.add_reaction(config.CONFUSED_EMOJI)

    interaction = FakeInteraction()
    subject = feature_context_subject(
        feature_name="team_register",
        feature_display_name="Team Register",
        process_upsert_from_message=process_invalid_attempt,
        bot=SimpleNamespace(user=object()),
        logger=NullLogger(),
    )

    await FeatureChannelBase.upsert_from_content_menu(subject, interaction, message)

    assert message.added_reactions == [config.CONFUSED_EMOJI]
    assert interaction.followup.messages == [
        ("Failed to upsert for Team Register.", {"ephemeral": False})
    ]


@pytest.mark.asyncio
async def test_context_menu_ordinary_text_failed_followup_without_reaction() -> None:
    message = FakeMessage()

    async def process_ordinary_text(_message: FakeMessage) -> None:
        return None

    interaction = FakeInteraction()
    subject = feature_context_subject(
        feature_name="team_register",
        feature_display_name="Team Register",
        process_upsert_from_message=process_ordinary_text,
        bot=SimpleNamespace(user=object()),
        logger=NullLogger(),
    )

    await FeatureChannelBase.upsert_from_content_menu(subject, interaction, message)

    assert message.added_reactions == []
    assert interaction.followup.messages == [
        ("Failed to upsert for Team Register.", {"ephemeral": False})
    ]


@pytest.mark.asyncio
async def test_context_menu_success_followup_uses_feature_display_name() -> None:
    message = FakeMessage()

    async def process_valid_text(_message: FakeMessage) -> str:
        return "{'ok': true}"

    interaction = FakeInteraction()
    subject = feature_context_subject(
        feature_name="team_register",
        feature_display_name="Team Register",
        process_upsert_from_message=process_valid_text,
        bot=SimpleNamespace(user=object()),
        logger=NullLogger(),
    )

    await FeatureChannelBase.upsert_from_content_menu(subject, interaction, message)

    assert interaction.followup.messages == [
        (
            "Upsert for Team Register complete. Data: ```js\n{'ok': true}```",
            {"ephemeral": False},
        )
    ]


@pytest.mark.asyncio
async def test_message_listener_marks_google_sheets_error() -> None:
    bot_user = object()
    message = FakeMessage()

    async def raise_google_sheets_error(message: FakeMessage) -> None:
        await message.add_reaction("<:haruka_math:1402204882492063825>")
        raise GoogleSheetsError(
            GoogleSheetsErrorKind.TRANSIENT,
            "Google Sheets is temporarily unavailable. Try again later.",
        )

    subject = SimpleNamespace(
        process_upsert_from_message=raise_google_sheets_error,
        bot=SimpleNamespace(user=bot_user),
        logger=NullLogger(),
    )

    await FeatureChannelBase.on_message(subject, message)

    assert message.removed_reactions == [
        ("<:haruka_math:1402204882492063825>", bot_user)
    ]
    assert message.added_reactions == [
        "<:haruka_math:1402204882492063825>",
        "⚠️",
        "🛠️",
    ]


@pytest.mark.asyncio
async def test_user_help_uses_followup_for_missing_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(FeatureChannel, "get", fake_feature_channel_get)
    interaction = FakeInteraction()
    subject = feature_context_subject(
        feature_name="team_register",
        feature_display_name="Team Register",
        ManagerType=MissingConfigManager,
        bot=SimpleNamespace(user=None),
    )

    await FeatureChannelUserBase.send_help_message(subject, interaction, "team.help")

    assert interaction.response.deferred == [True]
    message, kwargs = interaction.followup.messages[0]
    assert kwargs["ephemeral"] is True
    assert message == "Team Register is not configured for this channel."


@pytest.mark.asyncio
async def test_user_help_missing_channel_raises_after_defer() -> None:
    interaction = FakeInteraction()
    interaction.channel = None
    subject = feature_context_subject(
        feature_name="team_register",
        ManagerType=ConfiguredManager,
        bot=SimpleNamespace(user=None),
    )

    with pytest.raises(
        ValueError,
        match=(
            "Interaction guild or channel is None. Cannot send feature help message."
        ),
    ):
        await FeatureChannelUserBase.send_help_message(
            subject,
            interaction,
            "team.help",
        )

    assert interaction.response.deferred == [True]


@pytest.mark.asyncio
async def test_public_register_help_sends_announcement_languages_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(FeatureChannel, "get", fake_feature_channel_get)

    async def fake_render_announcement_messages(
        template_key: str,
        guild_id: int,
        _logger: object = None,
        **values: object,
    ) -> list[RenderedAnnouncement]:
        assert template_key == "team.help"
        assert guild_id == 111
        assert values["bot"] == "@Rhoboto"
        assert values["sheet_url"] == "https://sheet.example"
        return [
            RenderedAnnouncement(language="ja", content="ja help"),
            RenderedAnnouncement(language="zh_tw", content="zh help"),
            RenderedAnnouncement(language="en", content="en help"),
        ]

    monkeypatch.setattr(
        "cogs.base.feature_channel_base.render_announcement_messages",
        fake_render_announcement_messages,
    )

    interaction = FakeInteraction(locale="en-US")
    subject = feature_context_subject(
        feature_name="team_register",
        ManagerType=ConfiguredManager,
        bot=SimpleNamespace(user=SimpleNamespace(mention="@Rhoboto")),
        help_template_key="team.help",
        logger=NullLogger(),
    )

    await FeatureChannelBase._help_callback(subject, interaction)  # noqa: SLF001

    assert interaction.response.deferred == [False]
    assert interaction.followup.messages == [
        ("ja help", {"ephemeral": False}),
        ("zh help", {"ephemeral": False}),
        ("en help", {"ephemeral": False}),
    ]


@pytest.mark.asyncio
async def test_public_register_help_reports_missing_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(FeatureChannel, "get", fake_feature_channel_get)
    interaction = FakeInteraction()
    subject = feature_context_subject(
        feature_name="team_register",
        feature_display_name="Team Register",
        ManagerType=MissingConfigManager,
        bot=SimpleNamespace(user=SimpleNamespace(mention="@Rhoboto")),
        help_template_key="team.help",
        logger=NullLogger(),
    )

    await FeatureChannelBase._help_callback(subject, interaction)  # noqa: SLF001

    assert interaction.response.deferred == [False]
    assert interaction.followup.messages == [
        (
            "Team Register is not configured for this channel.",
            {"ephemeral": True},
        )
    ]


@pytest.mark.asyncio
async def test_public_register_help_reports_render_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(FeatureChannel, "get", fake_feature_channel_get)

    async def fake_render_announcement_messages(
        *_args: object,
        **_kwargs: object,
    ) -> list[RenderedAnnouncement]:
        return []

    monkeypatch.setattr(
        "cogs.base.feature_channel_base.render_announcement_messages",
        fake_render_announcement_messages,
    )

    interaction = FakeInteraction()
    subject = feature_context_subject(
        feature_name="team_register",
        ManagerType=ConfiguredManager,
        bot=SimpleNamespace(user=SimpleNamespace(mention="@Rhoboto")),
        help_template_key="team.help",
        logger=NullLogger(),
    )

    await FeatureChannelBase._help_callback(subject, interaction)  # noqa: SLF001

    assert interaction.followup.messages == [
        (
            "No announcement templates could be rendered for this server.",
            {"ephemeral": True},
        )
    ]


@pytest.mark.asyncio
async def test_shift_info_defers_before_public_followup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(FeatureChannel, "get", fake_feature_channel_get)

    async def fake_render_shift_info_announcement_messages(
        template_key: str,
        guild_id: int,
        _logger: object = None,
        **values: object,
    ) -> list[RenderedAnnouncement]:
        assert template_key == "shift.info"
        assert guild_id == 111
        assert "bot" not in values
        assert values["day_number"] == 2
        assert values["event_date"] == dt.date(2026, 8, 12)
        assert values["recruitment_time_range"] == "4-20・24-28"
        assert values["submission_deadline_at"] == dt.datetime(
            2026,
            8,
            12,
            12,
            tzinfo=dt.UTC,
        )
        return [
            RenderedAnnouncement(language="ja", content="ja info"),
            RenderedAnnouncement(language="en", content="en info"),
        ]

    monkeypatch.setattr(
        "cogs.shift_register.render_shift_info_announcement_messages",
        fake_render_shift_info_announcement_messages,
    )

    interaction = FakeInteraction(locale="ja")
    subject = feature_context_subject(
        feature_name="shift_register",
        ManagerType=ConfiguredMultiRangeShiftInfoManager,
        bot=SimpleNamespace(user=SimpleNamespace(mention="@Rhoboto")),
        info_template_key="shift.info",
        logger=NullLogger(),
    )

    await ShiftRegister.info.callback(
        subject,
        interaction,
    )

    assert interaction.response.deferred == [False]
    assert interaction.followup.messages == [
        ("ja info", {"ephemeral": False}),
        ("en info", {"ephemeral": False}),
    ]


@pytest.mark.asyncio
async def test_team_summary_reports_default_missing_config_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(FeatureChannel, "get", fake_feature_channel_get)
    monkeypatch.setattr(
        "cogs.team_register.TeamRegisterManager",
        UnexpectedTeamRegisterManager,
    )
    lock = RecordingLock()
    interaction = FakeInteraction()
    subject = feature_context_subject(
        feature_name="team_register",
        feature_display_name="Team Register",
        ManagerType=MissingConfigManager,
        lock=lock,
    )

    await TeamRegister.summary.callback(subject, interaction)

    assert interaction.response.deferred == [True]
    assert interaction.followup.messages == [
        (
            "Team Register is not configured for this channel.",
            {"ephemeral": True},
        )
    ]
    assert lock.keys == []


@pytest.mark.asyncio
async def test_team_summary_refreshes_with_configured_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(FeatureChannel, "get", fake_feature_channel_get)
    monkeypatch.setattr(
        "cogs.team_register.TeamRegisterManager",
        UnexpectedTeamRegisterManager,
    )
    summary_embed = SimpleNamespace(title="summary")

    def fake_build_summary_embed(summary_dataframe: object) -> SimpleNamespace:
        assert summary_dataframe is SummaryManager.summary_dataframe
        return summary_embed

    monkeypatch.setattr(
        "cogs.team_register.build_summary_embed",
        fake_build_summary_embed,
    )

    members = [SimpleNamespace(name="alice"), SimpleNamespace(name="bob")]
    guild = SimpleNamespace(id=111, members=members)
    interaction = FakeInteraction(guild=guild)
    lock = RecordingLock()
    subject = feature_context_subject(
        feature_name="team_register",
        ManagerType=SummaryManager,
        lock=lock,
    )

    await TeamRegister.summary.callback(subject, interaction)

    manager = SummaryManager.last_instance
    assert manager is not None
    assert interaction.response.deferred == [True]
    assert lock.keys == [222]
    assert manager.feature_channel.guild_id == 111
    assert manager.feature_channel.channel_id == 222
    assert manager.feature_channel.feature_name == "team_register"
    assert manager.logged_metadata is manager.metadata
    assert manager.ensure_count == 0
    assert manager.refresh_metadata is manager.ensured_metadata
    assert manager.member_by_names == {
        "alice": members[0],
        "bob": members[1],
    }
    assert interaction.followup.messages == [(None, {"embed": summary_embed})]


@pytest.mark.asyncio
async def test_team_summary_missing_guild_raises_before_defer() -> None:
    interaction = FakeInteraction()
    interaction.guild = None
    subject = feature_context_subject(
        feature_name="team_register",
        ManagerType=SummaryManager,
        lock=RecordingLock(),
    )

    with pytest.raises(
        ValueError,
        match="Interaction guild or channel is None. Cannot refresh team summary.",
    ):
        await TeamRegister.summary.callback(subject, interaction)

    assert interaction.response.deferred == []


@pytest.mark.asyncio
async def test_delete_callback_reports_missing_config_without_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(FeatureChannel, "get", fake_feature_channel_get)
    lock = RecordingLock()

    async def fail_delete(*_: object, **__: object) -> None:
        raise AssertionError

    subject = feature_context_subject(
        feature_name="team_register",
        feature_display_name="Team Register",
        ManagerType=MissingConfigManager,
        FeatureChannelType=SimpleNamespace(lock=lock),
        _delete_user_data=fail_delete,
    )
    interaction = FakeInteraction()

    await FeatureChannelUserBase.delete_callback(subject, interaction)

    assert interaction.response.deferred == [True]
    assert interaction.followup.messages == [
        (
            "Team Register is not configured for this channel.",
            {"ephemeral": True},
        )
    ]
    assert lock.keys == []


@pytest.mark.asyncio
async def test_delete_callback_deletes_with_configured_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(FeatureChannel, "get", fake_feature_channel_get)
    lock = RecordingLock()
    deleted: list[tuple[object, object, object]] = []

    async def fake_delete_user_data(
        manager: object,
        user_info: object,
        metadata: object,
    ) -> None:
        deleted.append((manager, user_info, metadata))

    subject = feature_context_subject(
        feature_name="team_register",
        feature_display_name="Team Register",
        ManagerType=DeleteManager,
        FeatureChannelType=SimpleNamespace(lock=lock),
        _delete_user_data=fake_delete_user_data,
    )
    interaction = FakeInteraction(locale="en-US")

    await FeatureChannelUserBase.delete_callback(subject, interaction)

    manager = DeleteManager.last_instance
    assert manager is not None
    assert interaction.response.deferred == [True]
    assert manager.feature_channel.guild_id == 111
    assert manager.feature_channel.channel_id == 222
    assert manager.feature_channel.feature_name == "team_register"
    assert lock.keys == [222]
    assert len(deleted) == 1
    deleted_manager, user_info, metadata = deleted[0]
    assert deleted_manager is manager
    assert user_info.username == "alice"
    assert user_info.display_name == "Alice"
    assert metadata is manager.metadata
    assert interaction.followup.messages == [
        (
            "✅ Your data for Team Register has been deleted successfully.",
            {"ephemeral": True},
        )
    ]


@pytest.mark.asyncio
async def test_delete_callback_uses_feature_display_name_in_zh_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(FeatureChannel, "get", fake_feature_channel_get)
    lock = RecordingLock()

    async def fake_delete_user_data(*_: object) -> None:
        return None

    subject = feature_context_subject(
        feature_name="team_register",
        feature_display_name="Team Register",
        ManagerType=DeleteManager,
        FeatureChannelType=SimpleNamespace(lock=lock),
        _delete_user_data=fake_delete_user_data,
    )
    interaction = FakeInteraction(locale="zh-TW")

    await FeatureChannelUserBase.delete_callback(subject, interaction)

    assert interaction.followup.messages == [
        ("✅ 已成功刪除 Team Register 登記的資料。", {"ephemeral": True})
    ]


@pytest.mark.asyncio
async def test_delete_callback_uses_feature_display_name_in_ja_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(FeatureChannel, "get", fake_feature_channel_get)
    lock = RecordingLock()

    async def fake_delete_user_data(*_: object) -> None:
        return None

    subject = feature_context_subject(
        feature_name="team_register",
        feature_display_name="Team Register",
        ManagerType=DeleteManager,
        FeatureChannelType=SimpleNamespace(lock=lock),
        _delete_user_data=fake_delete_user_data,
    )
    interaction = FakeInteraction(locale="ja")

    await FeatureChannelUserBase.delete_callback(subject, interaction)

    assert interaction.followup.messages == [
        ("✅ Team Register の入力データを正常に削除しました。", {"ephemeral": True})
    ]


@pytest.mark.asyncio
async def test_delete_callback_missing_channel_raises_before_defer() -> None:
    async def fail_delete(*_: object, **__: object) -> None:
        raise AssertionError

    interaction = FakeInteraction()
    interaction.channel = None
    subject = feature_context_subject(
        feature_name="team_register",
        ManagerType=DeleteManager,
        FeatureChannelType=SimpleNamespace(lock=RecordingLock()),
        _delete_user_data=fail_delete,
    )

    with pytest.raises(
        ValueError,
        match=(
            "Interaction guild or channel is None. Cannot delete feature user data."
        ),
    ):
        await FeatureChannelUserBase.delete_callback(subject, interaction)

    assert interaction.response.deferred == []


def test_team_and_shift_use_inherited_setup_after_enable() -> None:
    assert TeamRegister.feature_display_name == "Team Register"
    assert ShiftRegister.feature_display_name == "Shift Register"
    assert Team.feature_display_name == "Team Register"
    assert Shift.feature_display_name == "Shift Register"
    assert "setup_after_enable" not in TeamRegister.__dict__
    assert "setup_after_enable" not in ShiftRegister.__dict__


def test_register_context_menu_names_use_feature_display_name() -> None:
    team_register = TeamRegister(fake_bot())
    shift_register = ShiftRegister(fake_bot())

    assert team_register.context_menu.name == "Team Register Upsert"
    assert shift_register.context_menu.name == "Shift Register Upsert"


def test_team_and_shift_use_inherited_message_upsert_orchestration() -> None:
    assert "process_upsert_from_message" not in TeamRegister.__dict__
    assert "process_upsert_from_message" not in ShiftRegister.__dict__
    assert "_parse_message_submission" in TeamRegister.__dict__
    assert "_parse_message_submission" in ShiftRegister.__dict__
    assert "_process_configured_message_submission" in TeamRegister.__dict__
    assert "_process_configured_message_submission" in ShiftRegister.__dict__


@pytest.mark.asyncio
async def test_team_settings_command_defers_and_reuses_setup_after_enable() -> None:
    called = 0

    async def fake_setup_after_enable(_interaction: object) -> None:
        nonlocal called
        called += 1

    subject = SimpleNamespace(setup_after_enable=fake_setup_after_enable)
    interaction = FakeInteraction()

    await TeamRegister.settings.callback(subject, interaction)

    assert interaction.response.deferred == [True]
    assert called == 1


@pytest.mark.asyncio
async def test_shift_settings_command_defers_and_reuses_setup_after_enable() -> None:
    called = 0

    async def fake_setup_after_enable(_interaction: object) -> None:
        nonlocal called
        called += 1

    subject = SimpleNamespace(setup_after_enable=fake_setup_after_enable)
    interaction = FakeInteraction()

    await ShiftRegister.settings.callback(subject, interaction)

    assert interaction.response.deferred == [True]
    assert called == 1


@pytest.mark.asyncio
async def test_team_setup_after_enable_attaches_initial_setup_view_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(FeatureChannel, "get", fake_feature_channel_get)
    monkeypatch.setattr(
        "cogs.team_register.TeamRegisterManager",
        UnexpectedSetupManager,
    )
    monkeypatch.setattr(TeamRegister, "ManagerType", MissingConfigManager)
    interaction = FakeInteraction()
    subject = TeamRegister(fake_bot())

    await subject.setup_after_enable(interaction)

    content, kwargs = interaction.followup.messages[0]
    assert content == (
        "Team Register is not yet configured for this channel. Click below to set up."
    )
    assert kwargs["wait"] is True
    assert kwargs["view"].message is interaction.followup.sent_message_objects[0]


@pytest.mark.asyncio
async def test_shift_setup_after_enable_attaches_initial_setup_view_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(FeatureChannel, "get", fake_feature_channel_get)
    monkeypatch.setattr(
        "cogs.shift_register.ShiftRegisterManager",
        UnexpectedSetupManager,
    )
    monkeypatch.setattr(ShiftRegister, "ManagerType", MissingConfigManager)
    interaction = FakeInteraction()
    subject = ShiftRegister(fake_bot())

    await subject.setup_after_enable(interaction)

    content, kwargs = interaction.followup.messages[0]
    assert content == (
        "Shift Register is not yet configured for this channel. Click below to set up."
    )
    assert kwargs["wait"] is True
    assert kwargs["view"].message is interaction.followup.sent_message_objects[0]


@pytest.mark.asyncio
async def test_team_setup_after_enable_sends_current_panel_from_base_flow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(FeatureChannel, "get", fake_feature_channel_get)
    monkeypatch.setattr(
        "cogs.team_register.TeamRegisterManager",
        UnexpectedSetupManager,
    )
    monkeypatch.setattr(TeamRegister, "ManagerType", PanelManager)
    PanelManager.last_instance = None
    panel_view = SettingsTimeoutView()
    panel = SettingsPanel(embed=Embed(title="Team Register Settings"), view=panel_view)
    calls: list[tuple[object, object, object]] = []

    async def fake_build_team_register_settings_panel(
        manager: object,
        interaction: object,
        sheet_config: object,
    ) -> SettingsPanel:
        calls.append((manager, interaction, sheet_config))
        return panel

    monkeypatch.setattr(
        "cogs.team_register.build_team_register_settings_panel",
        fake_build_team_register_settings_panel,
    )
    interaction = FakeInteraction()
    subject = TeamRegister(fake_bot())

    await subject.setup_after_enable(interaction)

    manager = PanelManager.last_instance
    assert manager is not None
    assert len(calls) == 1
    call_manager, call_interaction, sheet_config = calls[0]
    assert call_manager is manager
    assert call_interaction is interaction
    assert sheet_config.sheet_url == "https://sheet.example"
    assert interaction.followup.messages == [
        (
            None,
            {
                "embed": panel.embed,
                "view": panel.view,
                "ephemeral": True,
                "wait": True,
            },
        )
    ]
    assert panel_view.message is interaction.followup.sent_message_objects[0]


@pytest.mark.asyncio
async def test_shift_setup_after_enable_sends_current_panel_from_base_flow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(FeatureChannel, "get", fake_feature_channel_get)
    monkeypatch.setattr(
        "cogs.shift_register.ShiftRegisterManager",
        UnexpectedSetupManager,
    )
    monkeypatch.setattr(ShiftRegister, "ManagerType", PanelManager)
    PanelManager.last_instance = None
    panel_view = SettingsTimeoutView()
    panel = SettingsPanel(embed=Embed(title="Shift Register Settings"), view=panel_view)
    calls: list[tuple[object, object]] = []

    async def fake_build_shift_register_settings_panel(
        manager: object,
        sheet_config: object,
    ) -> SettingsPanel:
        calls.append((manager, sheet_config))
        return panel

    monkeypatch.setattr(
        "cogs.shift_register.build_shift_register_settings_panel",
        fake_build_shift_register_settings_panel,
    )
    interaction = FakeInteraction()
    subject = ShiftRegister(fake_bot())

    await subject.setup_after_enable(interaction)

    manager = PanelManager.last_instance
    assert manager is not None
    assert len(calls) == 1
    call_manager, sheet_config = calls[0]
    assert call_manager is manager
    assert sheet_config.sheet_url == "https://sheet.example"
    assert interaction.followup.messages == [
        (
            None,
            {
                "embed": panel.embed,
                "view": panel.view,
                "ephemeral": True,
                "wait": True,
            },
        )
    ]
    assert panel_view.message is interaction.followup.sent_message_objects[0]


@pytest.mark.asyncio
async def test_setup_after_enable_routes_panel_google_sheets_error_from_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(FeatureChannel, "get", fake_feature_channel_get)
    monkeypatch.setattr(
        "cogs.team_register.TeamRegisterManager",
        UnexpectedSetupManager,
    )
    monkeypatch.setattr(TeamRegister, "ManagerType", PanelManager)
    PanelManager.last_instance = None
    error = GoogleSheetsError(
        GoogleSheetsErrorKind.TRANSIENT,
        "Google Sheets is temporarily unavailable. Try again later.",
    )
    routed: list[tuple[object, GoogleSheetsError]] = []

    async def raise_google_sheets_error(*_: object, **__: object) -> SettingsPanel:
        raise error

    async def fake_send_google_sheets_error(
        interaction: object,
        exc: GoogleSheetsError,
    ) -> None:
        routed.append((interaction, exc))

    monkeypatch.setattr(
        "cogs.team_register.build_team_register_settings_panel",
        raise_google_sheets_error,
    )
    monkeypatch.setattr(
        "cogs.base.feature_channel_base.send_google_sheets_error",
        fake_send_google_sheets_error,
    )
    interaction = FakeInteraction()
    subject = TeamRegister(fake_bot())

    await subject.setup_after_enable(interaction)

    assert routed == [(interaction, error)]
    assert interaction.followup.messages == []


@pytest.mark.asyncio
async def test_setup_after_enable_missing_guild_raises_shared_interaction_error() -> (
    None
):
    interaction = FakeInteraction()
    interaction.guild = None
    subject = TeamRegister(fake_bot())

    with pytest.raises(
        ValueError,
        match=("Interaction guild or channel is None. Cannot set up feature settings."),
    ):
        await subject.setup_after_enable(interaction)

    assert interaction.followup.messages == []
