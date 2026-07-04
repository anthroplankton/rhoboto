from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest

from bot import config
from cogs.base import feature_channel_base
from cogs.base.feature_channel_base import FeatureChannelBase, FeatureChannelUserBase
from cogs.shift_register import ShiftRegister
from cogs.team_register import TeamRegister
from models.feature_channel import FeatureChannel
from tests.fakes import ConfiguredManager, FakeInteraction, MissingConfigManager
from utils.announcement_languages import RenderedAnnouncement
from utils.google_sheets_errors import GoogleSheetsError, GoogleSheetsErrorKind


async def fake_feature_channel_get(
    *, guild_id: int, channel_id: int, feature_name: str
) -> SimpleNamespace:
    return SimpleNamespace(
        guild_id=guild_id,
        channel_id=channel_id,
        feature_name=feature_name,
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


def fake_bot() -> SimpleNamespace:
    return SimpleNamespace(
        tree=SimpleNamespace(add_command=lambda _command: None),
        user=None,
    )


def test_configured_feature_helpers_are_internal() -> None:
    assert not hasattr(feature_channel_base, "get_configured_feature_context")
    assert not hasattr(feature_channel_base, "send_public_announcement_followups")


@pytest.mark.asyncio
async def test_configured_feature_context_exposes_interaction_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(FeatureChannel, "get", fake_feature_channel_get)
    interaction = FakeInteraction()
    get_context = feature_channel_base._get_configured_feature_context  # noqa: SLF001

    context = await get_context(
        interaction,
        feature_name="team_register",
        manager_type=ConfiguredManager,
    )

    assert context is not None
    assert context.guild is interaction.guild
    assert context.guild_id == 111
    assert context.channel_id == 222
    assert context.feature_channel.guild_id == 111
    assert context.feature_channel.channel_id == 222
    assert context.feature_channel.feature_name == "team_register"
    assert isinstance(context.manager, ConfiguredManager)
    assert context.manager.feature_channel is context.feature_channel
    assert context.sheet_config.sheet_url == "https://sheet.example"


@pytest.mark.asyncio
async def test_configured_feature_context_reports_missing_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(FeatureChannel, "get", fake_feature_channel_get)
    interaction = FakeInteraction()
    get_context = feature_channel_base._get_configured_feature_context  # noqa: SLF001

    context = await get_context(
        interaction,
        feature_name="team_register",
        manager_type=MissingConfigManager,
    )

    assert context is None
    assert interaction.followup.messages == [
        (
            "`team_register` is not configured for this channel.",
            {"ephemeral": True},
        )
    ]


@pytest.mark.asyncio
async def test_user_help_defers_before_followup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(FeatureChannel, "get", fake_feature_channel_get)
    interaction = FakeInteraction(locale="zh-TW")
    subject = SimpleNamespace(
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
async def test_message_processing_helpers_build_context_and_user_info() -> None:
    async def is_enabled(
        guild_id: int,
        channel_id: int,
        feature_name: str | None = None,
    ) -> bool:
        return (guild_id, channel_id, feature_name) == (111, 222, None)

    subject = SimpleNamespace(
        feature_name="team_register",
        logger=NullLogger(),
        is_enabled=is_enabled,
    )
    message = FakeRegisterMessage(content="150/740/33")
    should_process_message = FeatureChannelBase._should_process_message  # noqa: SLF001
    message_user_info = FeatureChannelBase._message_user_info  # noqa: SLF001
    log_received_message = FeatureChannelBase._log_received_message  # noqa: SLF001

    assert await should_process_message(subject, message)
    user_info = message_user_info(subject, message)
    log_received_message(subject, message)

    assert user_info.username == "alice"
    assert user_info.display_name == "Alice"


@pytest.mark.asyncio
async def test_message_processing_helper_ignores_bot_messages() -> None:
    async def fail_if_called(*_: object, **__: object) -> bool:
        raise AssertionError

    subject = SimpleNamespace(
        feature_name="team_register",
        logger=NullLogger(),
        is_enabled=fail_if_called,
    )
    message = FakeRegisterMessage(author_bot=True)
    should_process_message = FeatureChannelBase._should_process_message  # noqa: SLF001

    assert not await should_process_message(subject, message)


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
    subject = SimpleNamespace(
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
    subject = SimpleNamespace(
        feature_name="team_register",
        process_upsert_from_message=process_invalid_attempt,
        bot=SimpleNamespace(user=object()),
        logger=NullLogger(),
    )

    await FeatureChannelBase.upsert_from_content_menu(subject, interaction, message)

    assert message.added_reactions == [config.CONFUSED_EMOJI]
    assert interaction.followup.messages == [
        ("Failed to upsert for `team_register`.", {"ephemeral": False})
    ]


@pytest.mark.asyncio
async def test_context_menu_ordinary_text_failed_followup_without_reaction() -> None:
    message = FakeMessage()

    async def process_ordinary_text(_message: FakeMessage) -> None:
        return None

    interaction = FakeInteraction()
    subject = SimpleNamespace(
        feature_name="team_register",
        process_upsert_from_message=process_ordinary_text,
        bot=SimpleNamespace(user=object()),
        logger=NullLogger(),
    )

    await FeatureChannelBase.upsert_from_content_menu(subject, interaction, message)

    assert message.added_reactions == []
    assert interaction.followup.messages == [
        ("Failed to upsert for `team_register`.", {"ephemeral": False})
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
    subject = SimpleNamespace(
        feature_name="team_register",
        ManagerType=MissingConfigManager,
        bot=SimpleNamespace(user=None),
    )

    await FeatureChannelUserBase.send_help_message(subject, interaction, "team.help")

    assert interaction.response.deferred == [True]
    message, kwargs = interaction.followup.messages[0]
    assert kwargs["ephemeral"] is True
    assert message == "`team_register` is not configured for this channel."


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
    subject = SimpleNamespace(
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
    subject = SimpleNamespace(
        feature_name="team_register",
        ManagerType=MissingConfigManager,
        bot=SimpleNamespace(user=SimpleNamespace(mention="@Rhoboto")),
        help_template_key="team.help",
        logger=NullLogger(),
    )

    await FeatureChannelBase._help_callback(subject, interaction)  # noqa: SLF001

    assert interaction.response.deferred == [False]
    assert interaction.followup.messages == [
        (
            "`team_register` is not configured for this channel.",
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
    subject = SimpleNamespace(
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
    subject = SimpleNamespace(
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
    subject = SimpleNamespace(
        feature_name="team_register",
        ManagerType=MissingConfigManager,
        lock=lock,
    )

    await TeamRegister.summary.callback(subject, interaction)

    assert interaction.response.deferred == [True]
    assert interaction.followup.messages == [
        (
            "`team_register` is not configured for this channel.",
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
    subject = SimpleNamespace(
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
    subject = SimpleNamespace(
        feature_name="team_register",
        ManagerType=SummaryManager,
        lock=RecordingLock(),
    )

    with pytest.raises(
        ValueError,
        match="Cannot proceed without an interaction channel and guild.",
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

    subject = SimpleNamespace(
        feature_name="team_register",
        ManagerType=MissingConfigManager,
        FeatureChannelType=SimpleNamespace(lock=lock),
        _delete_user_data=fail_delete,
    )
    interaction = FakeInteraction()

    await FeatureChannelUserBase.delete_callback(subject, interaction)

    assert interaction.response.deferred == [True]
    assert interaction.followup.messages == [
        (
            "`team_register` is not configured for this channel.",
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

    subject = SimpleNamespace(
        feature_name="team_register",
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
            "✅ Your data for `team_register` has been deleted successfully.",
            {"ephemeral": True},
        )
    ]


@pytest.mark.asyncio
async def test_delete_callback_missing_channel_raises_before_defer() -> None:
    async def fail_delete(*_: object, **__: object) -> None:
        raise AssertionError

    interaction = FakeInteraction()
    interaction.channel = None
    subject = SimpleNamespace(
        feature_name="team_register",
        ManagerType=DeleteManager,
        FeatureChannelType=SimpleNamespace(lock=RecordingLock()),
        _delete_user_data=fail_delete,
    )

    with pytest.raises(
        ValueError,
        match="Cannot proceed without an interaction channel and guild.",
    ):
        await FeatureChannelUserBase.delete_callback(subject, interaction)

    assert interaction.response.deferred == []


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
    monkeypatch.setattr("cogs.team_register.TeamRegisterManager", MissingConfigManager)
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
        MissingConfigManager,
    )
    interaction = FakeInteraction()
    subject = ShiftRegister(fake_bot())

    await subject.setup_after_enable(interaction)

    content, kwargs = interaction.followup.messages[0]
    assert content == (
        "Shift Register is not yet configured for this channel. Click below to set up."
    )
    assert kwargs["wait"] is True
    assert kwargs["view"].message is interaction.followup.sent_message_objects[0]
