from __future__ import annotations

# ruff: noqa: SLF001
import asyncio
import datetime as dt
import logging
import re
from types import SimpleNamespace

import pytest
from discord import Embed, HTTPException, NotFound
from tortoise.exceptions import DBConnectionError

from bot import config
from cogs.base import feature_channel_base
from cogs.base.discord_context import require_guild_channel_source
from cogs.base.feature_channel_base import (
    FeatureChannelBase,
    FeatureChannelUserBase,
    FeatureNotEnabled,
    StorageCheckFailure,
)
from cogs.base.feature_channel_context import (
    ConfiguredFeatureChannelContext,
    FeatureChannelContextMixin,
    MessageParseResult,
)
from cogs.shift import Shift
from cogs.shift_register import ShiftRegister
from cogs.team import Team
from cogs.team_register import TeamRegister
from components.ui_auto_guide import LATEST_GUIDE_ENABLE_REFRESH_FAILED_WARNING
from components.ui_settings_flow import SettingsPanel, SettingsTimeoutView
from models.feature_channel import FeatureChannel
from models.feature_channel_message_state import (
    FeatureChannelMessageKind,
    FeatureChannelMessageState,
)
from tests.fakes import (
    ConfiguredManager,
    FakeContext,
    FakeDiscordFollowup,
    FakeInteraction,
    MissingConfigManager,
)
from utils.announcement_languages import RenderedAnnouncement
from utils.google_sheets_errors import GoogleSheetsError, GoogleSheetsErrorKind
from utils.shift_register_structs import (
    DraftWorksheetMetadata,
    Shift as RegisterShift,
    ShiftParser,
)
from utils.shift_scheduler import DraftSchedule, HourShiftAssignment
from utils.storage_errors import StorageError, StorageErrorKind
from utils.structs_base import UserInfo
from utils.team_register_structs import Team as RegisterTeam, TeamParser

PRIVATE_DATABASE_ERROR = "private database"


def test_format_shift_draft_report_lists_each_hour_with_code_numbers() -> None:
    schedule = DraftSchedule(
        runner=None,
        hours=[4, 5, 6, 7, 8, 9, 10, 11],
        assignments=[
            HourShiftAssignment(
                hour=4,
                lane_usernames={"encore": "alice"},
                unassigned_usernames=["carol", "dave"],
            ),
            HourShiftAssignment(
                hour=5,
                lane_usernames={"hashiri_1": "bob", "encore": "alice"},
            ),
            HourShiftAssignment(
                hour=6,
                lane_usernames={
                    "encore": "alice",
                    "hashiri_1": "bob",
                    "hashiri_2": "eve",
                    "hashiri_3": "frank",
                    "standby": "grace",
                },
            ),
            HourShiftAssignment(hour=7, lane_usernames={"hashiri_1": "bob"}),
            HourShiftAssignment(hour=8),
            HourShiftAssignment(
                hour=9,
                lane_usernames={"encore": "alice", "standby": "grace"},
            ),
            HourShiftAssignment(
                hour=10,
                lane_usernames={"standby": "grace", "hashiri_1": "bob"},
            ),
            HourShiftAssignment(hour=11, lane_usernames={"standby": "grace"}),
        ],
        display_names={
            "alice": "Alice",
            "bob": "Bob",
            "carol": "Carol",
            "dave": "Dave",
            "eve": "E`ve",
            "frank": "Frank",
            "grace": "Grace",
        },
    )

    assert ShiftRegister._format_draft_report(
        schedule,
        "https://docs.google.com/spreadsheets/d/abc/edit#gid=222",
        {"alice": "<@111>", "bob": "<@222>", "carol": "<@333>"},
    ) == (
        "### ✅ 班表草稿已產生\n"
        "- Runner（ランナー）：`Not set`\n"  # noqa: RUF001
        "- ‼️ 已將 `8` 個小時的班表寫入 "
        "[Shift Draft](https://docs.google.com/spreadsheets/d/abc/edit#gid=222)"
        "，並覆蓋原有內容。\n"  # noqa: RUF001
        "- 已排入：\n"  # noqa: RUF001
        "  - -# `4-5`（缺 `4`）：<@111>\n"  # noqa: RUF001
        "  - -# `5-6`（缺 `3`）：<@111> ｜ <@222>\n"  # noqa: RUF001
        "  - -# `6-7`：<@111> ｜ <@222>、E\\`ve、`Frank`；`Grace`\n"  # noqa: RUF001
        "  - -# `7-8`（缺 `4`）：`No encore` ｜ <@222>\n"  # noqa: RUF001
        "  - -# `8-9`（缺 `5`）\n"  # noqa: RUF001
        "  - -# `9-10`（缺 `3`）：<@111>；`Grace`\n"  # noqa: RUF001
        "  - -# `10-11`（缺 `3`）：`No encore` ｜ <@222>；`Grace`\n"  # noqa: RUF001
        "  - -# `11-12`（缺 `4`）：`No encore`；`Grace`\n"  # noqa: RUF001
        "- 未排入（位置已滿）：\n"  # noqa: RUF001
        "  - -# `4-5`：<@333>、`Dave`"  # noqa: RUF001
    )


@pytest.mark.asyncio
async def test_generate_shift_draft_links_to_draft_worksheet_id() -> None:
    schedule = DraftSchedule(
        None,
        [4],
        [HourShiftAssignment(4, unassigned_usernames=["carol"])],
        {"carol": "Carol"},
    )
    metadata = SimpleNamespace(
        sheet_url="https://docs.google.com/spreadsheets/d/abc/edit",
        draft_worksheet=DraftWorksheetMetadata(222, "Shift Draft", None),
    )

    class Manager:
        async def fetch_google_sheets_metadata(self) -> SimpleNamespace:
            return metadata

        def log_missing_worksheet_warnings(self, _metadata: object) -> None:
            pass

        async def ensure_worksheets_and_upsert_sheet_config(
            self,
            _metadata: object,
        ) -> SimpleNamespace:
            return metadata

        async def generate_draft(
            self,
            _metadata: object,
            *,
            runner: str | None,
        ) -> DraftSchedule:
            assert runner is None
            return schedule

    async def get_feature_channel_context(_source: object) -> object:
        return object()

    async def get_configured_context(_context: object) -> SimpleNamespace:
        return SimpleNamespace(manager=Manager())

    subject = ShiftRegister(fake_bot())
    subject._get_feature_channel_context = get_feature_channel_context
    subject._get_configured_feature_channel_context = get_configured_context
    interaction = FakeInteraction(
        guild=SimpleNamespace(
            id=111,
            members=[SimpleNamespace(name="carol", mention="<@333>")],
        )
    )

    await ShiftRegister.generate_draft.callback(subject, interaction)

    assert "[Shift Draft](https://docs.google.com/spreadsheets/d/abc/edit#gid=222)" in (
        interaction.followup.messages[0][0] or ""
    )
    assert "<@333>" in (interaction.followup.messages[0][0] or "")


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


class IdRecordingFollowup(FakeDiscordFollowup):
    def __init__(self, *, first_message_id: int = 501) -> None:
        super().__init__()
        self.first_message_id = first_message_id

    async def send(
        self,
        content: str | None = None,
        **kwargs: object,
    ) -> SimpleNamespace:
        self.messages.append((content, kwargs))
        message = SimpleNamespace(
            id=self.first_message_id + len(self.sent_message_objects)
        )
        self.sent_message_objects.append(message)
        return message


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
    def info(self, *_: object, **__: object) -> None:
        pass

    def warning(self, *_: object, **__: object) -> None:
        pass

    def debug(self, *_: object, **__: object) -> None:
        pass

    def exception(self, *_: object, **__: object) -> None:
        pass


class RecordingLogger(NullLogger):
    def __init__(self) -> None:
        self.warnings: list[tuple[tuple[object, ...], dict[str, object]]] = []
        self.exceptions: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def warning(self, *args: object, **kwargs: object) -> None:
        self.warnings.append((args, kwargs))

    def exception(self, *args: object, **kwargs: object) -> None:
        self.exceptions.append((args, kwargs))


def interaction_contents(interaction: FakeInteraction) -> list[str]:
    return [
        content
        for content, _kwargs in (
            interaction.response.messages + interaction.followup.messages
        )
        if content is not None
    ]


async def _noop_async(*_args: object, **_kwargs: object) -> None:
    return None


def assert_safe_storage_content(content: str) -> None:
    assert "could not complete this action" in content
    assert "Reference: `STG-" in content
    assert "private database" not in content


def private_database_error() -> DBConnectionError:
    return DBConnectionError(PRIVATE_DATABASE_ERROR)


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


class ConfiguredHelpUrlManager(ConfiguredManager):
    async def get_sheet_config_or_none(self) -> SimpleNamespace:
        return SimpleNamespace(
            sheet_url=(
                "https://docs.google.com/spreadsheets/d/abc/edit?usp=sharing#gid=999"
            ),
            summary_worksheet_id=333,
            entry_worksheet_id=444,
        )


class AutoGuideMessage:
    def __init__(
        self,
        message_id: int,
        *,
        delete_error: Exception | None = None,
    ) -> None:
        self.id = message_id
        self.delete_error = delete_error
        self.delete_count = 0

    async def delete(self) -> None:
        if self.delete_error is not None:
            raise self.delete_error
        self.delete_count += 1


class AutoGuideChannel:
    def __init__(
        self,
        *,
        old_messages: list[AutoGuideMessage] | None = None,
        send_errors: list[Exception] | None = None,
    ) -> None:
        self.old_messages = {message.id: message for message in old_messages or []}
        self.send_errors = send_errors or []
        self.send_attempts: list[dict[str, object]] = []
        self.sent_messages: list[AutoGuideMessage] = []
        self.fetched_message_ids: list[int] = []

    async def send(self, **kwargs: object) -> AutoGuideMessage:
        self.send_attempts.append(kwargs)
        if self.send_errors:
            raise self.send_errors.pop(0)
        message = AutoGuideMessage(9000 + len(self.sent_messages))
        self.sent_messages.append(message)
        return message

    async def fetch_message(self, message_id: int) -> AutoGuideMessage:
        self.fetched_message_ids.append(message_id)
        try:
            return self.old_messages[message_id]
        except KeyError as exc:
            raise fake_not_found() from exc


class SaveableAutoGuideState:
    def __init__(
        self,
        *,
        is_enabled: bool = True,
        message_id: int | None = None,
        save_error: Exception | None = None,
    ) -> None:
        self.is_enabled = is_enabled
        self.message_id = message_id
        self.save_error = save_error
        self.saved_message_ids: list[int | None] = []

    async def save(self) -> None:
        self.saved_message_ids.append(self.message_id)
        if self.save_error is not None:
            raise self.save_error


def auto_guide_context() -> SimpleNamespace:
    return SimpleNamespace(
        guild_id=111,
        channel_id=222,
        feature_channel=SimpleNamespace(
            guild_id=111,
            channel_id=222,
            feature_name="team_register",
        ),
        manager=object(),
    )


def auto_guide_subject(**attributes: object) -> SimpleNamespace:
    subject = feature_channel_context_subject(
        feature_name="team_register",
        feature_display_name="Team Register",
        bot=SimpleNamespace(user=SimpleNamespace(mention="@Rhoboto")),
        auto_guide_template_key="team.auto_guide",
        auto_guide_lock=RecordingLock(),
        logger=NullLogger(),
    )
    for name, value in attributes.items():
        setattr(subject, name, value)
    return subject


def shift_auto_guide_config() -> SimpleNamespace:
    return SimpleNamespace(
        sheet_url="https://docs.google.com/spreadsheets/d/abc/edit?usp=sharing#gid=999",
        entry_worksheet_id=444,
        day_number=2,
        event_date=dt.date(2026, 8, 12),
        submission_deadline_at=dt.datetime(2026, 8, 12, 12, tzinfo=dt.UTC),
        draft_shift_proposal_at=dt.datetime(2026, 8, 13, 11, tzinfo=dt.UTC),
        final_shift_notice_at=dt.datetime(2026, 8, 14, 9, tzinfo=dt.UTC),
        recruitment_time_ranges=[{"start": 4, "end": 28}],
    )


def shift_auto_guide_context() -> ConfiguredFeatureChannelContext:
    return ConfiguredFeatureChannelContext(
        guild_id=111,
        channel_id=222,
        feature_channel=SimpleNamespace(
            guild_id=111,
            channel_id=222,
            feature_name="shift_register",
        ),
        manager=object(),
        feature_config=shift_auto_guide_config(),
    )


def fake_http_exception() -> HTTPException:
    response = SimpleNamespace(status=404, reason="Not Found")
    return HTTPException(response, "missing")


def fake_not_found() -> NotFound:
    response = SimpleNamespace(status=404, reason="Not Found")
    return NotFound(response, "missing")


class RecordingLock:
    def __init__(self) -> None:
        self.keys: list[object] = []

    def __call__(self, key: object) -> RecordingLock:
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


class SummaryGoogleSheetsErrorManager(SummaryManager):
    async def fetch_google_sheets_metadata(self) -> SimpleNamespace:
        raise GoogleSheetsError(
            GoogleSheetsErrorKind.QUOTA,
            "private sheet quota detail",
        )


class SummaryRefreshErrorManager(SummaryManager):
    async def refresh_summary_worksheet(
        self,
        metadata: object,
        *,
        member_by_names: dict[str, object],
    ) -> object:
        await super().refresh_summary_worksheet(
            metadata,
            member_by_names=member_by_names,
        )
        raise private_database_error()


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


class OrderedTeamUpsertManager(ConfiguredManager):
    def __init__(
        self,
        feature_channel: object,
        service_account_path: str,
        *,
        ensure_error: Exception | None = None,
        team_error: Exception | None = None,
        summary_error: Exception | None = None,
    ) -> None:
        super().__init__(feature_channel, service_account_path)
        self.events: list[str] = []
        self.metadata = SimpleNamespace(name="metadata")
        self.ensured_metadata = SimpleNamespace(name="ensured_metadata")
        self.ensure_error = ensure_error
        self.team_error = team_error
        self.summary_error = summary_error

    async def get_sheet_config_or_none(self) -> SimpleNamespace:
        return SimpleNamespace(sheet_url="https://sheet.example")

    async def fetch_google_sheets_metadata(self) -> SimpleNamespace:
        self.events.append("fetch_metadata")
        return self.metadata

    def log_missing_worksheet_warnings(self, metadata: object) -> None:
        assert metadata is self.metadata
        self.events.append("log_missing")

    async def ensure_worksheets_and_upsert_sheet_config(
        self,
        metadata: object,
        *,
        count: int | None = None,
    ) -> SimpleNamespace:
        assert metadata is self.metadata
        assert count == 2
        self.events.append("ensure")
        if self.ensure_error is not None:
            raise self.ensure_error
        return self.ensured_metadata

    async def upsert_user_teams(
        self,
        user_info: UserInfo,
        main_team: RegisterTeam,
        encore_team: RegisterTeam | None,
        *backup_teams: RegisterTeam,
        metadata: object,
    ) -> None:
        assert user_info.username == "alice"
        assert main_team.username == "alice"
        assert encore_team is None
        assert backup_teams == ()
        assert metadata is self.ensured_metadata
        self.events.append("teams_start")
        await asyncio.sleep(0)
        if self.team_error is not None:
            self.events.append("teams_error")
            raise self.team_error
        self.events.append("teams_done")

    async def upsert_user_summary(
        self,
        user_info: UserInfo,
        roles: list[object],
        main_team: RegisterTeam,
        encore_team: RegisterTeam | None,
        *backup_teams: RegisterTeam,
        metadata: object,
    ) -> None:
        assert user_info.username == "alice"
        assert roles == []
        assert main_team.username == "alice"
        assert encore_team is None
        assert backup_teams == ()
        assert metadata is self.ensured_metadata
        self.events.append("summary")
        if self.summary_error is not None:
            raise self.summary_error


class OrderedShiftUpsertManager(ConfiguredManager):
    def __init__(
        self,
        feature_channel: object,
        service_account_path: str,
        *,
        ensure_error: Exception | None = None,
        upsert_error: Exception | None = None,
    ) -> None:
        super().__init__(feature_channel, service_account_path)
        self.events: list[str] = []
        self.metadata = SimpleNamespace(name="metadata")
        self.ensured_metadata = SimpleNamespace(name="ensured_metadata")
        self.ensure_error = ensure_error
        self.upsert_error = upsert_error

    async def fetch_google_sheets_metadata(self) -> SimpleNamespace:
        self.events.append("fetch_metadata")
        return self.metadata

    def log_missing_worksheet_warnings(self, metadata: object) -> None:
        assert metadata is self.metadata
        self.events.append("log_missing")

    async def ensure_worksheets_and_upsert_sheet_config(
        self,
        metadata: object,
    ) -> SimpleNamespace:
        assert metadata is self.metadata
        self.events.append("ensure")
        if self.ensure_error is not None:
            raise self.ensure_error
        return self.ensured_metadata

    async def upsert_or_delete_user_shift(
        self,
        user_info: UserInfo,
        shift: RegisterShift | None,
        *,
        metadata: object,
    ) -> None:
        assert user_info.username == "alice"
        assert shift is not None
        assert metadata is self.ensured_metadata
        self.events.append("upsert")
        if self.upsert_error is not None:
            raise self.upsert_error


class MissingMessageConfigManager(MessageOrchestrationManager):
    async def get_sheet_config_or_none(self) -> None:
        return None


class RecordingMessageSubject(FeatureChannelContextMixin[MessageOrchestrationManager]):
    feature_name = "team_register"
    ManagerType = MessageOrchestrationManager

    def __init__(self, parse_result: MessageParseResult[object]) -> None:
        self.parse_result = parse_result
        self.logger = NullLogger()
        self.bot = SimpleNamespace(user=object())
        self.configured_calls: list[
            tuple[
                object,
                ConfiguredFeatureChannelContext[MessageOrchestrationManager],
                object,
                UserInfo,
            ]
        ] = []

    async def _get_message_feature_channel_context_or_none(
        self,
        message: object,
    ) -> object | None:
        get_context = FeatureChannelBase._get_message_feature_channel_context_or_none
        return await get_context(self, message)

    async def _process_feature_channel_message_with_outcome(
        self,
        message: object,
        feature_channel_context: object,
    ) -> object:
        process = FeatureChannelBase._process_feature_channel_message_with_outcome
        return await process(self, message, feature_channel_context)

    def _log_received_message(self, message: object) -> None:
        FeatureChannelBase._log_received_message(self, message)

    async def _add_invalid_registration_reactions(self, message: object) -> None:
        add_reactions = FeatureChannelBase._add_invalid_registration_reactions
        await add_reactions(self, message)

    async def _parse_message_submission(
        self,
        _message: object,
    ) -> MessageParseResult[object]:
        return self.parse_result

    async def _process_configured_message_submission(
        self,
        message: object,
        context: ConfiguredFeatureChannelContext[MessageOrchestrationManager],
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


async def message_upsert_result(subject: object, message: object) -> object | None:
    feature_channel_context = (
        await subject._get_message_feature_channel_context_or_none(message)
    )
    if feature_channel_context is None:
        return None
    outcome = await subject._process_feature_channel_message_with_outcome(
        message,
        feature_channel_context,
    )
    return outcome.result


async def fake_context_menu_feature_channel_context(_message: object) -> object:
    return object()


def feature_channel_context_subject(**attributes: object) -> SimpleNamespace:
    subject = SimpleNamespace(**attributes)
    for method_name in (
        "_get_feature_channel_context",
        "_get_enabled_feature_channel_or_none",
        "_get_feature_channel_context_or_none",
        "_get_configured_feature_channel_context",
        "_build_feature_channel_context",
        "_send_missing_config_followup",
        "_interaction_storage_context",
        "_send_interaction_storage_error_or_raise",
        "_guide_worksheet_id",
        "_guide_sheet_url",
        "_auto_guide_is_enabled",
        "_auto_guide_template_values",
        "_render_auto_guide_embeds",
        "_auto_guide_delete_callback",
        "_build_auto_guide_buttons_view",
        "_refresh_auto_guide_if_enabled",
        "_send_and_record_auto_guide",
        "_send_auto_guide_message",
        "_delete_auto_guide_message",
        "_disable_auto_guide_and_delete_message",
        "_delete_auto_guide_message_for_hard_clear",
        "toggle_auto_guide_from_settings",
    ):
        method = getattr(FeatureChannelBase, method_name)
        setattr(subject, method_name, method.__get__(subject, type(subject)))
    return subject


def ordered_team_upsert_context(
    manager: OrderedTeamUpsertManager,
) -> ConfiguredFeatureChannelContext[OrderedTeamUpsertManager]:
    return ConfiguredFeatureChannelContext(
        guild_id=111,
        channel_id=222,
        feature_channel=SimpleNamespace(
            guild_id=111,
            channel_id=222,
            feature_name="team_register",
        ),
        manager=manager,
        feature_config=SimpleNamespace(sheet_url="https://sheet.example"),
    )


def ordered_shift_upsert_context(
    manager: OrderedShiftUpsertManager,
) -> ConfiguredFeatureChannelContext[OrderedShiftUpsertManager]:
    return ConfiguredFeatureChannelContext(
        guild_id=111,
        channel_id=222,
        feature_channel=SimpleNamespace(
            guild_id=111,
            channel_id=222,
            feature_name="shift_register",
        ),
        manager=manager,
        feature_config=SimpleNamespace(
            sheet_url="https://sheet.example",
            recruitment_time_ranges=[{"start": 4, "end": 28}],
        ),
    )


def team_register_submission() -> tuple[list[RegisterTeam], UserInfo]:
    user_info = UserInfo(username="alice", display_name="Alice")
    team = RegisterTeam(
        username=user_info.username,
        display_name=user_info.display_name,
        leader_skill_value=150,
        internal_skill_value=740,
        team_power=33.0,
        original_message="150/740/33",
    )
    return [team], user_info


def shift_register_submission() -> tuple[RegisterShift, UserInfo]:
    user_info = UserInfo(username="alice", display_name="Alice")
    shift = RegisterShift(
        username=user_info.username,
        display_name=user_info.display_name,
        original_message="4-8",
        slots=set(range(4, 8)),
    )
    return shift, user_info


def test_feature_channel_context_helpers_are_not_module_level() -> None:
    assert not hasattr(feature_channel_base, "_snowflake_id")
    assert not hasattr(feature_channel_base, "_interaction_storage_context")
    assert not hasattr(feature_channel_base, "_message_storage_context")
    assert not hasattr(feature_channel_base, "_send_interaction_storage_error_or_raise")
    assert hasattr(FeatureChannelBase, "_send_interaction_storage_error_or_raise")
    assert not hasattr(feature_channel_base, "_get_interaction_channel_context")
    assert not hasattr(FeatureChannelBase, "_get_interaction_channel_context")
    assert not hasattr(feature_channel_base, "_get_configured_feature_channel_context")
    assert not hasattr(feature_channel_base, "get_configured_feature_channel_context")
    assert not hasattr(feature_channel_base, "send_public_announcement_followups")


@pytest.mark.asyncio
async def test_configured_feature_channel_context_exposes_feature_config(
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
    get_context = FeatureChannelBase._get_feature_channel_context
    feature_channel_context = await get_context(subject, source)
    get_configured_context = FeatureChannelBase._get_configured_feature_channel_context
    context = await get_configured_context(
        subject,
        feature_channel_context,
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
async def test_configured_feature_channel_context_missing_config_is_pure_lookup(
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
    get_context = FeatureChannelBase._get_feature_channel_context
    feature_channel_context = await get_context(subject, source)
    get_configured_context = FeatureChannelBase._get_configured_feature_channel_context
    context = await get_configured_context(
        subject,
        feature_channel_context,
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
async def test_app_command_predicate_disabled_uses_interaction_locale(
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

    with pytest.raises(FeatureNotEnabled) as exc_info:
        await predicate(FakeInteraction(locale="ja"))

    assert str(exc_info.value) == "⚠️ このチャンネルでは編成登録が有効になっていません。"
    assert calls == [(111, 222, "team_register")]


@pytest.mark.asyncio
async def test_app_command_predicate_db_failure_sends_safe_check_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def failing_lookup(
        _cls: type[FeatureChannelBase],
        _guild_id: int,
        _channel_id: int,
        _feature_name: str | None = None,
    ) -> object | None:
        raise private_database_error()

    monkeypatch.setattr(
        FeatureChannelBase,
        "_get_enabled_feature_channel_or_none",
        classmethod(failing_lookup),
    )
    predicate = FeatureChannelBase.feature_enabled_app_command_predicate(
        "team_register",
        "Team Register",
    )
    interaction = FakeInteraction()

    with pytest.raises(StorageCheckFailure) as exc_info:
        await predicate(interaction)

    subject = feature_channel_context_subject(
        feature_name="team_register",
        feature_display_name="Team Register",
        logger=NullLogger(),
    )
    await FeatureChannelBase.cog_app_command_error(
        subject,
        interaction,
        exc_info.value,
    )

    contents = interaction_contents(interaction)
    assert len(contents) == 1
    assert_safe_storage_content(contents[0])
    assert interaction.response.messages[0][1] == {"ephemeral": True}


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
async def test_prefix_command_predicate_db_failure_replies_safe_check_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def failing_lookup(
        _cls: type[FeatureChannelBase],
        _guild_id: int,
        _channel_id: int,
        _feature_name: str | None = None,
    ) -> object | None:
        raise private_database_error()

    monkeypatch.setattr(
        FeatureChannelBase,
        "_get_enabled_feature_channel_or_none",
        classmethod(failing_lookup),
    )
    predicate = FeatureChannelBase.feature_enabled_prefix_command_predicate(
        "team_register",
        "Team Register",
    )
    ctx = FakeContext()
    replies: list[str] = []

    async def reply(content: str) -> None:
        replies.append(content)

    ctx.reply = reply

    with pytest.raises(StorageCheckFailure) as exc_info:
        await predicate(ctx)

    subject = feature_channel_context_subject(
        feature_name="team_register",
        feature_display_name="Team Register",
        logger=NullLogger(),
    )
    await FeatureChannelBase.cog_command_error(subject, ctx, exc_info.value)

    assert len(replies) == 1
    assert_safe_storage_content(replies[0])


@pytest.mark.asyncio
async def test_prefix_command_storage_failure_logs_safe_fields(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def failing_lookup(
        _cls: type[FeatureChannelBase],
        _guild_id: int,
        _channel_id: int,
        _feature_name: str | None = None,
    ) -> object | None:
        raise private_database_error()

    monkeypatch.setattr(
        FeatureChannelBase,
        "_get_enabled_feature_channel_or_none",
        classmethod(failing_lookup),
    )
    predicate = FeatureChannelBase.feature_enabled_prefix_command_predicate(
        "team_register",
        "Team Register",
    )
    ctx = FakeContext()
    replies: list[str] = []

    async def reply(content: str) -> None:
        replies.append(content)

    ctx.reply = reply
    log = logging.getLogger("tests.feature_channel.storage")
    caplog.set_level(logging.WARNING, logger=log.name)

    with pytest.raises(StorageCheckFailure) as exc_info:
        await predicate(ctx)

    subject = feature_channel_context_subject(
        feature_name="team_register",
        feature_display_name="Team Register",
        logger=log,
    )
    await FeatureChannelBase.cog_command_error(subject, ctx, exc_info.value)

    assert len(replies) == 1
    assert "STG-" in caplog.text
    assert "database_unavailable" in caplog.text
    assert "private database" not in caplog.text


@pytest.mark.asyncio
async def test_enable_response_uses_feature_display_name() -> None:
    setup_calls: list[object] = []

    async def fake_enable_channel(_guild_id: int, _channel_id: int) -> None:
        return None

    async def fake_setup_after_enable(interaction: object) -> None:
        setup_calls.append(interaction)

    subject = feature_channel_context_subject(
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
    context = auto_guide_context()

    async def fake_get_feature_channel_context_or_none(
        *,
        guild_id: int,
        channel_id: int,
        require_enabled: bool = False,
    ) -> object:
        assert (guild_id, channel_id, require_enabled) == (111, 222, True)
        return context

    async def fake_disable_channel(_guild_id: int, _channel_id: int) -> bool:
        return True

    async def fake_disable_auto_guide_and_delete_message(_context: object) -> bool:
        return True

    subject = feature_channel_context_subject(
        feature_name="team_register",
        feature_display_name="Team Register",
        _disable_channel=fake_disable_channel,
    )
    subject._get_feature_channel_context_or_none = (
        fake_get_feature_channel_context_or_none
    )
    subject._disable_auto_guide_and_delete_message = (
        fake_disable_auto_guide_and_delete_message
    )
    interaction = FakeInteraction()

    await FeatureChannelBase.disable.callback(subject, interaction)

    assert interaction.response.messages == [
        ("Feature Team Register disabled in this channel.", {"ephemeral": True})
    ]
    assert interaction.followup.messages == []


@pytest.mark.asyncio
async def test_disable_sends_auto_guide_warning_when_cleanup_fails() -> None:
    context = auto_guide_context()
    calls: list[str] = []

    async def fake_get_feature_channel_context_or_none(
        *,
        guild_id: int,
        channel_id: int,
        require_enabled: bool = False,
    ) -> object:
        assert (guild_id, channel_id, require_enabled) == (111, 222, True)
        calls.append("get_context")
        return context

    async def fake_disable_channel(_guild_id: int, _channel_id: int) -> bool:
        calls.append("disable")
        return True

    async def fake_disable_auto_guide_and_delete_message(context_arg: object) -> bool:
        assert context_arg is context
        calls.append("auto_guide")
        return False

    subject = feature_channel_context_subject(
        feature_name="team_register",
        feature_display_name="Team Register",
        _disable_channel=fake_disable_channel,
    )
    subject._get_feature_channel_context_or_none = (
        fake_get_feature_channel_context_or_none
    )
    subject._disable_auto_guide_and_delete_message = (
        fake_disable_auto_guide_and_delete_message
    )
    interaction = FakeInteraction()

    await FeatureChannelBase.disable.callback(subject, interaction)

    assert interaction.response.messages == [
        ("Feature Team Register disabled in this channel.", {"ephemeral": True})
    ]
    assert interaction.followup.messages == [
        (
            feature_channel_base.LATEST_GUIDE_DELETE_FAILED_WARNING,
            {"ephemeral": True},
        )
    ]
    assert calls == ["get_context", "disable", "auto_guide"]


@pytest.mark.asyncio
async def test_disable_response_uses_feature_display_name_when_not_enabled() -> None:
    async def fake_get_feature_channel_context_or_none(
        *,
        guild_id: int,
        channel_id: int,
        require_enabled: bool = False,
    ) -> None:
        assert (guild_id, channel_id, require_enabled) == (111, 222, True)

    async def fail_if_called(*_args: object, **_kwargs: object) -> bool:
        msg = "not-enabled disable must not mutate or warn"
        raise AssertionError(msg)

    subject = feature_channel_context_subject(
        feature_name="team_register",
        feature_display_name="Team Register",
        _disable_channel=fail_if_called,
        logger=NullLogger(),
    )
    subject._get_feature_channel_context_or_none = (
        fake_get_feature_channel_context_or_none
    )
    subject._disable_auto_guide_and_delete_message = fail_if_called
    interaction = FakeInteraction()

    await FeatureChannelBase.disable.callback(subject, interaction)

    assert interaction.response.messages == [
        ("Feature Team Register is not enabled in this channel.", {"ephemeral": True})
    ]
    assert interaction.followup.messages == []


@pytest.mark.asyncio
async def test_disable_and_clear_confirmed_deletes_auto_guide_before_clear_and_warns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = auto_guide_context()
    calls: list[str] = []

    class ConfirmView:
        value = True

        async def wait(self) -> None:
            calls.append("wait")

    async def fake_get_feature_channel_context_or_none(
        *,
        guild_id: int,
        channel_id: int,
        require_enabled: bool = False,
    ) -> object:
        assert (guild_id, channel_id, require_enabled) == (111, 222, False)
        calls.append("get_context")
        return context

    async def fake_delete_auto_guide_message_for_hard_clear(
        context_arg: object,
    ) -> bool:
        assert context_arg is context
        calls.append("auto_guide")
        return False

    async def fake_clear_feature_settings(guild_id: int, channel_id: int) -> None:
        assert (guild_id, channel_id) == (111, 222)
        calls.append("clear")

    monkeypatch.setattr(
        feature_channel_base,
        "DisableAndClearConfirmView",
        ConfirmView,
    )
    subject = feature_channel_context_subject(
        feature_name="team_register",
        feature_display_name="Team Register",
        _clear_feature_settings=fake_clear_feature_settings,
    )
    subject._get_feature_channel_context_or_none = (
        fake_get_feature_channel_context_or_none
    )
    subject._delete_auto_guide_message_for_hard_clear = (
        fake_delete_auto_guide_message_for_hard_clear
    )
    interaction = FakeInteraction()

    await FeatureChannelBase.disable_and_clear.callback(subject, interaction)

    assert calls == ["wait", "get_context", "auto_guide", "clear"]
    assert interaction.followup.messages == [
        (
            "Feature Team Register has been disabled and all bot settings for this "
            "feature in this channel have been permanently cleared.",
            {"ephemeral": True},
        ),
        (
            feature_channel_base.HARD_CLEAR_LATEST_GUIDE_DELETE_FAILED_WARNING,
            {"ephemeral": True},
        ),
    ]


@pytest.mark.asyncio
async def test_disable_and_clear_hard_clear_does_not_save_auto_guide_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = auto_guide_context()
    old_message = AutoGuideMessage(1234)
    channel = AutoGuideChannel(old_messages=[old_message])
    state = SaveableAutoGuideState(
        message_id=1234,
        save_error=private_database_error(),
    )
    calls: list[str] = []

    class ConfirmView:
        value = True

        async def wait(self) -> None:
            calls.append("wait")

    async def fake_get_feature_channel_context_or_none(
        *,
        guild_id: int,
        channel_id: int,
        require_enabled: bool = False,
    ) -> object:
        assert (guild_id, channel_id, require_enabled) == (111, 222, False)
        calls.append("get_context")
        return context

    async def fake_get_auto_guide_state(feature_channel: object) -> object:
        assert feature_channel is context.feature_channel
        calls.append("auto_guide_state")
        return state

    async def fake_clear_feature_settings(guild_id: int, channel_id: int) -> None:
        assert (guild_id, channel_id) == (111, 222)
        calls.append("clear")

    monkeypatch.setattr(
        feature_channel_base,
        "DisableAndClearConfirmView",
        ConfirmView,
    )
    monkeypatch.setattr(
        feature_channel_base,
        "get_auto_guide_state",
        fake_get_auto_guide_state,
        raising=False,
    )
    subject = feature_channel_context_subject(
        feature_name="team_register",
        feature_display_name="Team Register",
        _clear_feature_settings=fake_clear_feature_settings,
        auto_guide_lock=RecordingLock(),
        bot=SimpleNamespace(
            user=SimpleNamespace(mention="@Rhoboto"),
            get_channel=lambda channel_id: channel if channel_id == 222 else None,
        ),
        logger=NullLogger(),
    )
    subject._get_feature_channel_context_or_none = (
        fake_get_feature_channel_context_or_none
    )
    interaction = FakeInteraction()

    await FeatureChannelBase.disable_and_clear.callback(subject, interaction)

    assert calls == ["wait", "get_context", "auto_guide_state", "clear"]
    assert state.saved_message_ids == []
    assert channel.fetched_message_ids == [1234]
    assert old_message.delete_count == 1
    assert interaction.followup.messages == [
        (
            "Feature Team Register has been disabled and all bot settings for this "
            "feature in this channel have been permanently cleared.",
            {"ephemeral": True},
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("view_value", [False, None])
async def test_disable_and_clear_cancel_or_timeout_skips_auto_guide_delete(
    monkeypatch: pytest.MonkeyPatch,
    view_value: object,
) -> None:
    class ConfirmView:
        value = view_value

        async def wait(self) -> None:
            return None

    async def fail_if_called(*_args: object, **_kwargs: object) -> object:
        msg = "cancel and timeout must not touch auto guide or clear settings"
        raise AssertionError(msg)

    monkeypatch.setattr(
        feature_channel_base,
        "DisableAndClearConfirmView",
        ConfirmView,
    )
    subject = feature_channel_context_subject(
        feature_name="team_register",
        feature_display_name="Team Register",
        _clear_feature_settings=fail_if_called,
    )
    subject._get_feature_channel_context_or_none = fail_if_called
    subject._disable_auto_guide_and_delete_message = fail_if_called
    subject._delete_auto_guide_message_for_hard_clear = fail_if_called
    interaction = FakeInteraction()

    await FeatureChannelBase.disable_and_clear.callback(subject, interaction)

    if view_value is None:
        assert interaction.followup.messages == [
            ("No response received. Operation timed out.", {"ephemeral": True})
        ]
    else:
        assert interaction.followup.messages == []


@pytest.mark.asyncio
async def test_user_guide_defers_before_followup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(FeatureChannel, "get", fake_feature_channel_get)
    interaction = FakeInteraction(locale="zh-TW")
    subject = feature_channel_context_subject(
        feature_name="team_register",
        feature_display_name="Team Register",
        ManagerType=ConfiguredManager,
        bot=SimpleNamespace(user=SimpleNamespace(mention="@Rhoboto")),
    )

    await FeatureChannelUserBase.send_guide_message(subject, interaction, "team.guide")

    assert interaction.response.deferred == [True]
    message, kwargs = interaction.followup.messages[0]
    assert kwargs["ephemeral"] is True
    assert "@Rhoboto" in str(message)
    assert "https://sheet.example" in str(message)


@pytest.mark.asyncio
async def test_setup_after_enable_db_failure_sends_safe_storage_message() -> None:
    async def failing_context(_source: object) -> object:
        raise private_database_error()

    interaction = FakeInteraction()
    subject = feature_channel_context_subject(
        feature_name="team_register",
        feature_display_name="Team Register",
        logger=NullLogger(),
    )
    subject._get_feature_channel_context = failing_context

    await FeatureChannelBase.setup_after_enable(subject, interaction)

    contents = interaction_contents(interaction)
    assert len(contents) == 1
    assert_safe_storage_content(contents[0])
    assert interaction.response.messages[0][1] == {"ephemeral": True}


@pytest.mark.asyncio
async def test_message_processing_helpers_build_context_and_user_info(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(FeatureChannel, "get_or_none", fake_feature_channel_get_or_none)
    subject = RecordingMessageSubject(MessageParseResult.ignored())
    message = FakeRegisterMessage(content="150/740/33")
    message_user_info = FeatureChannelBase._message_user_info
    log_received_message = FeatureChannelBase._log_received_message

    get_message_context = (
        FeatureChannelBase._get_message_feature_channel_context_or_none
    )
    feature_channel_context = await get_message_context(subject, message)
    user_info = message_user_info(subject, message)
    log_received_message(subject, message)

    assert feature_channel_context is not None
    assert feature_channel_context.guild_id == 111
    assert feature_channel_context.channel_id == 222
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

    get_message_context = (
        FeatureChannelBase._get_message_feature_channel_context_or_none
    )
    feature_channel_context = await get_message_context(subject, message)

    assert feature_channel_context is None


@pytest.mark.asyncio
async def test_base_message_orchestration_ignored_skips_config_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(FeatureChannel, "get_or_none", fake_feature_channel_get_or_none)
    subject = RecordingMessageSubject(MessageParseResult.ignored())
    message = FakeRegisterMessage(content="公告")

    result = await message_upsert_result(subject, message)

    assert result is None
    assert message.added_reactions == []
    assert subject.configured_calls == []


@pytest.mark.asyncio
async def test_base_message_orchestration_invalid_configured_adds_warning_then_confused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(FeatureChannel, "get_or_none", fake_feature_channel_get_or_none)
    user_info = UserInfo(username="alice", display_name="Alice")
    subject = RecordingMessageSubject(MessageParseResult.invalid(user_info=user_info))
    message = FakeRegisterMessage(content="160//600/33")

    result = await message_upsert_result(subject, message)

    assert result is None
    assert message.added_reactions == [config.WARNING_EMOJI, config.CONFUSED_EMOJI]
    assert subject.configured_calls == []


@pytest.mark.asyncio
async def test_base_message_orchestration_invalid_missing_config_stays_silent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(FeatureChannel, "get_or_none", fake_feature_channel_get_or_none)
    user_info = UserInfo(username="alice", display_name="Alice")
    subject = RecordingMessageSubject(MessageParseResult.invalid(user_info=user_info))
    subject.ManagerType = MissingMessageConfigManager
    message = FakeRegisterMessage(content="160//600/33")

    result = await message_upsert_result(subject, message)

    assert result is None
    assert message.added_reactions == []
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

    result = await message_upsert_result(subject, message)

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

    result = await message_upsert_result(subject, message)

    assert result == "processed"
    assert len(subject.configured_calls) == 1
    call_message, context, submission, call_user_info = subject.configured_calls[0]
    assert call_message is message
    assert context.manager is MessageOrchestrationManager.last_instance
    assert context.feature_config.sheet_url == "https://sheet.example"
    assert submission == "submission"
    assert call_user_info is user_info


@pytest.mark.asyncio
async def test_listener_refreshes_auto_guide_after_ordinary_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(FeatureChannel, "get_or_none", fake_feature_channel_get_or_none)
    subject = RecordingMessageSubject(MessageParseResult.ignored())
    message = FakeRegisterMessage(content="ordinary chat")
    refresh_calls: list[tuple[object, object]] = []

    async def refresh_auto_guide(context: object, channel: object) -> bool:
        refresh_calls.append((context, channel))
        return True

    subject._refresh_auto_guide_if_enabled = refresh_auto_guide

    await FeatureChannelBase.on_message(subject, message)

    assert len(refresh_calls) == 1
    context, channel = refresh_calls[0]
    assert context.guild_id == 111
    assert context.channel_id == 222
    assert channel is message.channel
    assert message.added_reactions == []


@pytest.mark.asyncio
async def test_listener_refreshes_auto_guide_after_upsert_storage_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(FeatureChannel, "get_or_none", fake_feature_channel_get_or_none)
    user_info = UserInfo(username="alice", display_name="Alice")
    subject = RecordingMessageSubject(
        MessageParseResult.parsed("submission", user_info=user_info)
    )
    message = FakeRegisterMessage(content="150/740/33")
    refresh_calls: list[tuple[object, object]] = []

    async def fail_upsert(
        _message: object,
        _context: object,
        _submission: object,
        _user_info: object,
    ) -> None:
        raise private_database_error()

    async def refresh_auto_guide(context: object, channel: object) -> bool:
        refresh_calls.append((context, channel))
        return True

    subject._process_configured_message_submission = fail_upsert
    subject._refresh_auto_guide_if_enabled = refresh_auto_guide

    await FeatureChannelBase.on_message(subject, message)

    assert message.added_reactions == [config.WARNING_EMOJI, "🛠️"]
    assert len(refresh_calls) == 1
    context, channel = refresh_calls[0]
    assert context.guild_id == 111
    assert context.channel_id == 222
    assert channel is message.channel


@pytest.mark.asyncio
async def test_listener_refresh_failure_does_not_add_reactions_or_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(FeatureChannel, "get_or_none", fake_feature_channel_get_or_none)
    user_info = UserInfo(username="alice", display_name="Alice")
    subject = RecordingMessageSubject(
        MessageParseResult.parsed("submission", user_info=user_info)
    )
    message = FakeRegisterMessage(content="150/740/33")
    refresh_calls: list[tuple[object, object]] = []

    async def refresh_auto_guide(context: object, channel: object) -> bool:
        refresh_calls.append((context, channel))
        return False

    subject._refresh_auto_guide_if_enabled = refresh_auto_guide

    await FeatureChannelBase.on_message(subject, message)

    assert len(subject.configured_calls) == 1
    assert len(refresh_calls) == 1
    assert message.added_reactions == []


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

    result = await message_upsert_result(subject, message)

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

    result = await message_upsert_result(subject, message)

    assert result is None
    assert message.added_reactions == []


@pytest.mark.asyncio
async def test_context_menu_reports_google_sheets_error_safely() -> None:
    bot_user = object()
    message = FakeMessage()

    async def raise_google_sheets_error(
        message: FakeMessage,
        _feature_channel_context: object,
    ) -> None:
        await message.add_reaction(config.PROCESSING_EMOJI)
        raise GoogleSheetsError(
            GoogleSheetsErrorKind.QUOTA,
            "Google Sheets is rate-limiting requests. Try again later.",
        )

    interaction = FakeInteraction()
    subject = feature_channel_context_subject(
        feature_name="team_register",
        _get_message_feature_channel_context_or_none=(
            fake_context_menu_feature_channel_context
        ),
        _process_feature_channel_message_with_outcome=raise_google_sheets_error,
        bot=SimpleNamespace(user=bot_user),
        logger=NullLogger(),
    )

    await FeatureChannelBase.upsert_from_content_menu(
        subject,
        interaction,
        message,
    )

    assert interaction.response.deferred == [True]
    assert len(interaction.followup.messages) == 1
    content, kwargs = interaction.followup.messages[0]
    assert content is not None
    assert "Google Sheets is rate-limiting requests. Try again later." in content
    assert "Reference: `STG-" in content
    assert kwargs == {"ephemeral": True}
    assert message.removed_reactions == [(config.PROCESSING_EMOJI, bot_user)]
    assert message.added_reactions == [
        config.PROCESSING_EMOJI,
        config.WARNING_EMOJI,
        "🛠️",
    ]


@pytest.mark.asyncio
async def test_context_menu_db_failure_marks_message_and_sends_safe_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bot_user = object()
    message = FakeMessage()

    async def raise_db_error(
        message: FakeMessage,
        _feature_channel_context: object,
    ) -> None:
        await message.add_reaction(config.PROCESSING_EMOJI)
        raise private_database_error()

    interaction = FakeInteraction()
    log = logging.getLogger("tests.feature_channel.context_menu_storage")
    caplog.set_level(logging.WARNING, logger=log.name)
    subject = feature_channel_context_subject(
        feature_name="team_register",
        feature_display_name="Team Register",
        _get_message_feature_channel_context_or_none=(
            fake_context_menu_feature_channel_context
        ),
        _process_feature_channel_message_with_outcome=raise_db_error,
        bot=SimpleNamespace(user=bot_user),
        logger=log,
    )

    await FeatureChannelBase.upsert_from_content_menu(
        subject,
        interaction,
        message,
    )

    contents = interaction_contents(interaction)
    assert len(contents) == 1
    assert_safe_storage_content(contents[0])
    message_reference = re.search(r"Reference: `(STG-[0-9a-f]{8})`", contents[0])
    assert message_reference is not None
    logged_references = re.findall(r"reference=(STG-[0-9a-f]{8})", caplog.text)
    assert logged_references == [message_reference.group(1), message_reference.group(1)]
    assert interaction.followup.messages[0][1] == {"ephemeral": True}
    assert message.removed_reactions == [(config.PROCESSING_EMOJI, bot_user)]
    assert message.added_reactions == [
        config.PROCESSING_EMOJI,
        config.WARNING_EMOJI,
        "🛠️",
    ]


@pytest.mark.asyncio
async def test_context_menu_invalid_attempt_keeps_processor_reaction() -> None:
    message = FakeMessage()

    async def process_invalid_attempt(
        message: FakeMessage,
        _feature_channel_context: object,
    ) -> object:
        await message.add_reaction(config.WARNING_EMOJI)
        await message.add_reaction(config.CONFUSED_EMOJI)
        return feature_channel_base._MessageUpsertOutcome.invalid()

    interaction = FakeInteraction()
    subject = feature_channel_context_subject(
        feature_name="team_register",
        feature_display_name="Team Register",
        _get_message_feature_channel_context_or_none=(
            fake_context_menu_feature_channel_context
        ),
        _process_feature_channel_message_with_outcome=process_invalid_attempt,
        bot=SimpleNamespace(user=object()),
        logger=NullLogger(),
    )

    await FeatureChannelBase.upsert_from_content_menu(subject, interaction, message)

    assert message.added_reactions == [config.WARNING_EMOJI, config.CONFUSED_EMOJI]
    assert interaction.response.deferred == [True]
    assert interaction.followup.messages == [
        ("Failed to upsert for Team Register.", {"ephemeral": True})
    ]


@pytest.mark.asyncio
async def test_context_menu_ordinary_text_failed_followup_without_reaction() -> None:
    message = FakeMessage()

    async def process_ordinary_text(
        _message: FakeMessage,
        _feature_channel_context: object,
    ) -> object:
        return feature_channel_base._MessageUpsertOutcome.ignored()

    interaction = FakeInteraction()
    subject = feature_channel_context_subject(
        feature_name="team_register",
        feature_display_name="Team Register",
        _get_message_feature_channel_context_or_none=(
            fake_context_menu_feature_channel_context
        ),
        _process_feature_channel_message_with_outcome=process_ordinary_text,
        bot=SimpleNamespace(user=object()),
        logger=NullLogger(),
    )

    await FeatureChannelBase.upsert_from_content_menu(subject, interaction, message)

    assert interaction.response.deferred == [True]
    assert message.added_reactions == []
    assert interaction.followup.messages == [
        ("Failed to upsert for Team Register.", {"ephemeral": True})
    ]


@pytest.mark.asyncio
async def test_context_menu_success_followup_uses_feature_display_name() -> None:
    message = FakeMessage()

    async def process_valid_text(
        _message: FakeMessage,
        _feature_channel_context: object,
    ) -> object:
        return feature_channel_base._MessageUpsertOutcome.processed("{'ok': true}")

    interaction = FakeInteraction()
    subject = feature_channel_context_subject(
        feature_name="team_register",
        feature_display_name="Team Register",
        _get_message_feature_channel_context_or_none=(
            fake_context_menu_feature_channel_context
        ),
        _process_feature_channel_message_with_outcome=process_valid_text,
        bot=SimpleNamespace(user=object()),
        logger=NullLogger(),
    )

    await FeatureChannelBase.upsert_from_content_menu(subject, interaction, message)

    assert interaction.response.deferred == [True]
    assert interaction.followup.messages == [
        (
            "Upsert for Team Register complete. Data: ```js\n{'ok': true}```",
            {"ephemeral": True},
        )
    ]


@pytest.mark.asyncio
async def test_context_menu_missing_config_reports_private_clear_message() -> None:
    message = FakeMessage()

    async def process_missing_config(
        _message: FakeMessage,
        _feature_channel_context: object,
    ) -> object:
        return feature_channel_base._MessageUpsertOutcome.missing_config()

    interaction = FakeInteraction()
    subject = feature_channel_context_subject(
        feature_name="team_register",
        feature_display_name="Team Register",
        _get_message_feature_channel_context_or_none=(
            fake_context_menu_feature_channel_context
        ),
        _process_feature_channel_message_with_outcome=process_missing_config,
        bot=SimpleNamespace(user=object()),
        logger=NullLogger(),
    )

    await FeatureChannelBase.upsert_from_content_menu(subject, interaction, message)

    assert interaction.response.deferred == [True]
    assert interaction.followup.messages == [
        ("⚠️ Team Register is not configured for this channel.", {"ephemeral": True})
    ]


@pytest.mark.asyncio
async def test_message_listener_marks_google_sheets_error() -> None:
    bot_user = object()
    message = FakeRegisterMessage()

    async def get_context(
        *,
        guild_id: int,
        channel_id: int,
        require_enabled: bool,
    ) -> object:
        assert (guild_id, channel_id, require_enabled) == (111, 222, True)
        return object()

    async def get_message_context(message_arg: FakeMessage) -> object:
        assert message_arg is message
        return await get_context(
            guild_id=message_arg.guild.id,
            channel_id=message_arg.channel.id,
            require_enabled=True,
        )

    async def raise_google_sheets_error(
        message: FakeMessage,
        _feature_channel_context: object,
    ) -> None:
        await message.add_reaction(config.PROCESSING_EMOJI)
        raise GoogleSheetsError(
            GoogleSheetsErrorKind.TRANSIENT,
            "Google Sheets is temporarily unavailable. Try again later.",
        )

    subject = SimpleNamespace(
        _get_message_feature_channel_context_or_none=get_message_context,
        _process_feature_channel_message_with_outcome=raise_google_sheets_error,
        _refresh_auto_guide_if_enabled=_noop_async,
        feature_name="team_register",
        bot=SimpleNamespace(user=bot_user),
        logger=NullLogger(),
    )

    await FeatureChannelBase.on_message(subject, message)

    assert message.removed_reactions == [(config.PROCESSING_EMOJI, bot_user)]
    assert message.added_reactions == [
        config.PROCESSING_EMOJI,
        config.WARNING_EMOJI,
        "🛠️",
    ]


@pytest.mark.asyncio
async def test_delete_after_confirmation_db_failure_sends_safe_storage_error() -> None:
    async def failing_context(**_: object) -> object:
        raise private_database_error()

    interaction = FakeInteraction()
    await interaction.response.send_message("delete prompt", ephemeral=True)
    source = require_guild_channel_source(
        interaction,
        action="delete feature user data",
    )
    subject = feature_channel_context_subject(
        feature_name="team_register",
        feature_display_name="Team Register",
        logger=NullLogger(),
    )
    subject._get_feature_channel_context_or_none = failing_context

    await FeatureChannelUserBase._delete_user_data_after_confirmation(
        subject,
        interaction,
        source,
    )

    contents = interaction_contents(interaction)
    assert len(contents) == 2
    assert_safe_storage_content(contents[1])
    assert interaction.response.deferred == []
    assert interaction.followup.messages[0][1] == {"ephemeral": True}


@pytest.mark.asyncio
async def test_user_guide_uses_followup_for_missing_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(FeatureChannel, "get", fake_feature_channel_get)
    interaction = FakeInteraction()
    subject = feature_channel_context_subject(
        feature_name="team_register",
        feature_display_name="Team Register",
        ManagerType=MissingConfigManager,
        bot=SimpleNamespace(user=None),
    )

    await FeatureChannelUserBase.send_guide_message(subject, interaction, "team.guide")

    assert interaction.response.deferred == [True]
    message, kwargs = interaction.followup.messages[0]
    assert kwargs["ephemeral"] is True
    assert message == "⚠️ Team Register is not configured for this channel."


@pytest.mark.asyncio
async def test_user_guide_missing_config_uses_interaction_locale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(FeatureChannel, "get", fake_feature_channel_get)
    interaction = FakeInteraction(locale="zh-TW")
    subject = feature_channel_context_subject(
        feature_name="team_register",
        feature_display_name="Team Register",
        ManagerType=MissingConfigManager,
        bot=SimpleNamespace(user=None),
    )

    await FeatureChannelUserBase.send_guide_message(subject, interaction, "team.guide")

    assert interaction.response.deferred == [True]
    message, kwargs = interaction.followup.messages[0]
    assert kwargs["ephemeral"] is True
    assert message == "⚠️ 此頻道尚未設定隊伍編成登記。"


@pytest.mark.asyncio
async def test_user_guide_missing_channel_raises_after_defer() -> None:
    interaction = FakeInteraction()
    interaction.channel = None
    subject = feature_channel_context_subject(
        feature_name="team_register",
        feature_display_name="Team Register",
        ManagerType=ConfiguredManager,
        bot=SimpleNamespace(user=None),
    )

    with pytest.raises(
        ValueError,
        match=re.escape(
            "Interaction guild or channel is None. Cannot send Team Register guide "
            "message."
        ),
    ):
        await FeatureChannelUserBase.send_guide_message(
            subject,
            interaction,
            "team.guide",
        )

    assert interaction.response.deferred == [True]


@pytest.mark.asyncio
async def test_disable_auto_guide_and_delete_message_clears_message_id_after_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = auto_guide_context()
    old_message = AutoGuideMessage(1234)
    channel = AutoGuideChannel(old_messages=[old_message])
    state = SaveableAutoGuideState(message_id=1234)
    bot = SimpleNamespace(
        user=SimpleNamespace(mention="@Rhoboto"),
        get_channel=lambda channel_id: channel if channel_id == 222 else None,
    )
    subject = auto_guide_subject(bot=bot)

    async def fake_get_auto_guide_state(feature_channel: object) -> object:
        assert feature_channel is context.feature_channel
        return state

    monkeypatch.setattr(
        feature_channel_base,
        "get_auto_guide_state",
        fake_get_auto_guide_state,
        raising=False,
    )

    result = await FeatureChannelBase._disable_auto_guide_and_delete_message(
        subject,
        context,
    )

    assert result is True
    assert state.is_enabled is False
    assert state.message_id is None
    assert state.saved_message_ids == [1234, None]
    assert channel.fetched_message_ids == [1234]
    assert old_message.delete_count == 1


@pytest.mark.asyncio
async def test_disable_auto_guide_and_delete_message_clears_message_id_after_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = auto_guide_context()
    channel = AutoGuideChannel()
    state = SaveableAutoGuideState(message_id=1234)
    bot = SimpleNamespace(
        user=SimpleNamespace(mention="@Rhoboto"),
        get_channel=lambda channel_id: channel if channel_id == 222 else None,
    )
    subject = auto_guide_subject(bot=bot)

    async def fake_get_auto_guide_state(_feature_channel: object) -> object:
        return state

    monkeypatch.setattr(
        feature_channel_base,
        "get_auto_guide_state",
        fake_get_auto_guide_state,
        raising=False,
    )

    result = await FeatureChannelBase._disable_auto_guide_and_delete_message(
        subject,
        context,
    )

    assert result is True
    assert state.is_enabled is False
    assert state.message_id is None
    assert state.saved_message_ids == [1234, None]
    assert channel.fetched_message_ids == [1234]


@pytest.mark.asyncio
async def test_disable_auto_guide_and_delete_message_keeps_message_id_after_http_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = auto_guide_context()
    old_message = AutoGuideMessage(1234, delete_error=fake_http_exception())
    channel = AutoGuideChannel(old_messages=[old_message])
    state = SaveableAutoGuideState(message_id=1234)
    logger = RecordingLogger()
    bot = SimpleNamespace(
        user=SimpleNamespace(mention="@Rhoboto"),
        get_channel=lambda channel_id: channel if channel_id == 222 else None,
    )
    subject = auto_guide_subject(bot=bot, logger=logger)

    async def fake_get_auto_guide_state(_feature_channel: object) -> object:
        return state

    monkeypatch.setattr(
        feature_channel_base,
        "get_auto_guide_state",
        fake_get_auto_guide_state,
        raising=False,
    )

    result = await FeatureChannelBase._disable_auto_guide_and_delete_message(
        subject,
        context,
    )

    assert result is False
    assert state.is_enabled is False
    assert state.message_id == 1234
    assert state.saved_message_ids == [1234]
    assert channel.fetched_message_ids == [1234]
    assert old_message.delete_count == 0
    assert len(logger.warnings) == 1


@pytest.mark.asyncio
async def test_disable_auto_guide_and_delete_message_uses_auto_guide_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = auto_guide_context()
    lock = RecordingLock()
    subject = auto_guide_subject(auto_guide_lock=lock)

    async def fake_get_auto_guide_state(_feature_channel: object) -> None:
        return None

    monkeypatch.setattr(
        feature_channel_base,
        "get_auto_guide_state",
        fake_get_auto_guide_state,
        raising=False,
    )

    result = await FeatureChannelBase._disable_auto_guide_and_delete_message(
        subject,
        context,
    )

    assert result is True
    assert lock.keys == [222]


@pytest.mark.asyncio
async def test_latest_guide_toggle_enable_saves_before_refresh_and_warns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = auto_guide_context()
    fresh_config = SimpleNamespace(
        sheet_url="https://sheet.example",
        summary_worksheet_id=333,
    )
    events: list[str] = []

    class ToggleState:
        is_enabled = False

        async def save(self) -> None:
            events.append(f"save:{self.is_enabled}")

    async def fake_get_feature_channel_context(source: object) -> object:
        assert source is interaction
        events.append("get_context")
        return context

    async def fake_get_or_create_auto_guide_state(feature_channel: object) -> object:
        assert feature_channel is context.feature_channel
        events.append("state")
        return state

    async def fake_build_settings_panel(
        interaction_arg: object,
        manager: object,
        sheet_config: object,
    ) -> SettingsPanel:
        assert interaction_arg is interaction
        assert manager is context.manager
        assert sheet_config is fresh_config
        events.append("build_panel")
        return panel

    async def fake_refresh_auto_guide(
        context_arg: object,
        channel: object,
        *,
        feature_config: object | None = None,
    ) -> bool:
        assert context_arg is context
        assert channel is interaction.channel
        assert feature_config is fresh_config
        assert state.is_enabled is True
        events.append("refresh")
        return False

    async def fail_disable(*_: object, **__: object) -> bool:
        msg = "enable path must not delete latest guide"
        raise AssertionError(msg)

    state = ToggleState()
    panel_view = SettingsTimeoutView()
    panel = SettingsPanel(embed=Embed(title="Team Register Settings"), view=panel_view)
    current_view = SettingsTimeoutView()
    interaction = FakeInteraction()
    subject = feature_channel_context_subject(
        feature_name="team_register",
        feature_display_name="Team Register",
        logger=NullLogger(),
    )
    subject._get_feature_channel_context = fake_get_feature_channel_context
    subject._build_settings_panel = fake_build_settings_panel
    subject._refresh_auto_guide_if_enabled = fake_refresh_auto_guide
    subject._disable_auto_guide_and_delete_message = fail_disable
    monkeypatch.setattr(
        feature_channel_base,
        "get_or_create_auto_guide_state",
        fake_get_or_create_auto_guide_state,
        raising=False,
    )

    await FeatureChannelBase.toggle_auto_guide_from_settings(
        subject,
        interaction,
        enabled=True,
        current_view=current_view,
        feature_config=fresh_config,
    )

    assert events == ["get_context", "state", "save:True", "build_panel", "refresh"]
    assert current_view.is_finished()
    assert len(interaction.response.edits) == 1
    edit_kwargs = interaction.response.edits[0][1]
    assert edit_kwargs["embed"] is panel.embed
    assert edit_kwargs["view"] is panel.view
    assert interaction.followup.messages == [
        (LATEST_GUIDE_ENABLE_REFRESH_FAILED_WARNING, {"ephemeral": True})
    ]


@pytest.mark.asyncio
async def test_latest_guide_toggle_disable_calls_delete_helper_and_warns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = auto_guide_context()
    fresh_config = SimpleNamespace(sheet_url="https://sheet.example")
    events: list[str] = []

    class ToggleState:
        is_enabled = True

        async def save(self) -> None:
            events.append(f"save:{self.is_enabled}")

    async def fake_get_feature_channel_context(_source: object) -> object:
        events.append("get_context")
        return context

    async def fake_get_or_create_auto_guide_state(feature_channel: object) -> object:
        assert feature_channel is context.feature_channel
        events.append("state")
        return state

    async def fake_build_settings_panel(*_: object) -> SettingsPanel:
        events.append("build_panel")
        return SettingsPanel(
            embed=Embed(title="Team Register Settings"),
            view=SettingsTimeoutView(),
        )

    async def fake_refresh(*_: object, **__: object) -> bool:
        msg = "disable path must not send latest guide"
        raise AssertionError(msg)

    async def fake_disable_auto_guide(context_arg: object) -> bool:
        assert context_arg is context
        assert state.is_enabled is False
        events.append("disable")
        return False

    state = ToggleState()
    interaction = FakeInteraction()
    subject = feature_channel_context_subject(
        feature_name="team_register",
        feature_display_name="Team Register",
        logger=NullLogger(),
    )
    subject._get_feature_channel_context = fake_get_feature_channel_context
    subject._build_settings_panel = fake_build_settings_panel
    subject._refresh_auto_guide_if_enabled = fake_refresh
    subject._disable_auto_guide_and_delete_message = fake_disable_auto_guide
    monkeypatch.setattr(
        feature_channel_base,
        "get_or_create_auto_guide_state",
        fake_get_or_create_auto_guide_state,
        raising=False,
    )

    await FeatureChannelBase.toggle_auto_guide_from_settings(
        subject,
        interaction,
        enabled=False,
        current_view=SettingsTimeoutView(),
        feature_config=fresh_config,
    )

    assert events == ["get_context", "state", "save:False", "build_panel", "disable"]
    assert interaction.followup.messages == [
        (feature_channel_base.LATEST_GUIDE_DELETE_FAILED_WARNING, {"ephemeral": True})
    ]


@pytest.mark.asyncio
async def test_latest_guide_toggle_disable_deletes_when_panel_refresh_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = auto_guide_context()
    fresh_config = SimpleNamespace(sheet_url="https://sheet.example")
    events: list[str] = []

    class ToggleState:
        is_enabled = True

        async def save(self) -> None:
            events.append(f"save:{self.is_enabled}")

    async def fake_get_feature_channel_context(_source: object) -> object:
        events.append("get_context")
        return context

    async def fake_get_or_create_auto_guide_state(feature_channel: object) -> object:
        assert feature_channel is context.feature_channel
        events.append("state")
        return state

    async def fake_build_settings_panel(*_: object) -> SettingsPanel:
        events.append("build_panel")
        raise private_database_error()

    async def fake_disable_auto_guide(context_arg: object) -> bool:
        assert context_arg is context
        assert state.is_enabled is False
        events.append("disable")
        return False

    state = ToggleState()
    current_view = SettingsTimeoutView()
    interaction = FakeInteraction()
    subject = feature_channel_context_subject(
        feature_name="team_register",
        feature_display_name="Team Register",
        logger=NullLogger(),
    )
    subject._get_feature_channel_context = fake_get_feature_channel_context
    subject._build_settings_panel = fake_build_settings_panel
    subject._disable_auto_guide_and_delete_message = fake_disable_auto_guide
    monkeypatch.setattr(
        feature_channel_base,
        "get_or_create_auto_guide_state",
        fake_get_or_create_auto_guide_state,
        raising=False,
    )

    await FeatureChannelBase.toggle_auto_guide_from_settings(
        subject,
        interaction,
        enabled=False,
        current_view=current_view,
        feature_config=fresh_config,
    )

    assert events == ["get_context", "state", "save:False", "build_panel", "disable"]
    assert current_view.is_finished()
    assert len(interaction.response.edits) == 1
    edit_content, edit_kwargs = interaction.response.edits[0]
    assert "settings view could not be refreshed" in edit_content
    assert edit_kwargs == {"embed": None, "view": None}
    assert interaction.followup.messages == [
        (feature_channel_base.LATEST_GUIDE_DELETE_FAILED_WARNING, {"ephemeral": True})
    ]


@pytest.mark.asyncio
async def test_team_latest_guide_toggle_uses_fresh_missing_settings_guard() -> None:
    class MissingFreshTeamRegisterManager:
        async def get_fresh_sheet_config(self) -> None:
            return None

    async def fail_toggle(*_: object, **__: object) -> None:
        msg = "missing settings must not toggle latest guide"
        raise AssertionError(msg)

    subject = SimpleNamespace(toggle_auto_guide_from_settings=fail_toggle)
    interaction = FakeInteraction()
    current_view = SimpleNamespace(
        team_register_manager=MissingFreshTeamRegisterManager()
    )

    await TeamRegister._toggle_team_latest_guide(
        subject,
        interaction,
        enabled=True,
        current_view=current_view,
    )

    assert interaction.response.messages == [
        (
            "Team Register settings are no longer configured for this channel.",
            {"ephemeral": True},
        )
    ]


@pytest.mark.asyncio
async def test_auto_guide_disabled_state_skips_config_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = auto_guide_context()
    channel = AutoGuideChannel()
    subject = auto_guide_subject()

    async def fake_get_auto_guide_state(feature_channel: object) -> object:
        assert feature_channel is context.feature_channel
        return SimpleNamespace(is_enabled=False)

    async def unexpected_config_lookup(_context: object) -> None:
        msg = "disabled auto guide must not fetch feature config"
        raise AssertionError(msg)

    monkeypatch.setattr(
        feature_channel_base,
        "get_auto_guide_state",
        fake_get_auto_guide_state,
        raising=False,
    )
    subject._get_configured_feature_channel_context = unexpected_config_lookup

    result = await FeatureChannelBase._refresh_auto_guide_if_enabled(
        subject,
        context,
        channel,
    )

    assert result is True
    assert channel.send_attempts == []


@pytest.mark.asyncio
async def test_auto_guide_enabled_missing_config_is_silent_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = auto_guide_context()
    channel = AutoGuideChannel()
    subject = auto_guide_subject()
    config_lookups: list[object] = []

    async def fake_get_auto_guide_state(_feature_channel: object) -> object:
        return SimpleNamespace(is_enabled=True)

    async def missing_config_lookup(feature_channel_context: object) -> None:
        config_lookups.append(feature_channel_context)

    monkeypatch.setattr(
        feature_channel_base,
        "get_auto_guide_state",
        fake_get_auto_guide_state,
        raising=False,
    )
    subject._get_configured_feature_channel_context = missing_config_lookup

    result = await FeatureChannelBase._refresh_auto_guide_if_enabled(
        subject,
        context,
        channel,
    )

    assert result is True
    assert config_lookups == [context]
    assert channel.send_attempts == []


@pytest.mark.asyncio
async def test_auto_guide_replies_to_manual_anchor_and_records_latest_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = auto_guide_context()
    configured_context = ConfiguredFeatureChannelContext(
        guild_id=111,
        channel_id=222,
        feature_channel=context.feature_channel,
        manager=context.manager,
        feature_config=SimpleNamespace(sheet_url="https://sheet.example"),
    )
    old_message = AutoGuideMessage(1234)
    channel = AutoGuideChannel(old_messages=[old_message])
    auto_state = SaveableAutoGuideState(message_id=1234)
    subject = auto_guide_subject()
    render_calls: list[tuple[str, str, dict[str, object]]] = []

    async def fake_get_auto_guide_state(_feature_channel: object) -> object:
        return SimpleNamespace(is_enabled=True, message_id=1234)

    async def fake_get_or_create_auto_guide_state(_feature_channel: object) -> object:
        return auto_state

    async def fake_get_announcement_languages(
        guild_id: int,
        _logger: object,
    ) -> list[str]:
        assert guild_id == 111
        return ["en"]

    def fake_render_message_template(
        template_key: str,
        locale: str,
        **values: object,
    ) -> str:
        render_calls.append((template_key, locale, values))
        return f"{locale}:{template_key}"

    async def fake_message_state_get_or_none(**kwargs: object) -> object:
        assert kwargs == {
            "feature_channel": context.feature_channel,
            "message_kind": FeatureChannelMessageKind.MANUAL_GUIDE,
            "message_id__not_isnull": True,
        }
        return SimpleNamespace(message_id=5555)

    async def configured_lookup(_context: object) -> object:
        return configured_context

    monkeypatch.setattr(
        feature_channel_base,
        "get_auto_guide_state",
        fake_get_auto_guide_state,
        raising=False,
    )
    monkeypatch.setattr(
        feature_channel_base,
        "get_or_create_auto_guide_state",
        fake_get_or_create_auto_guide_state,
        raising=False,
    )
    monkeypatch.setattr(
        feature_channel_base,
        "get_announcement_languages",
        fake_get_announcement_languages,
        raising=False,
    )
    monkeypatch.setattr(
        feature_channel_base,
        "render_message_template",
        fake_render_message_template,
    )
    monkeypatch.setattr(
        FeatureChannelMessageState,
        "get_or_none",
        fake_message_state_get_or_none,
    )
    subject._get_configured_feature_channel_context = configured_lookup

    result = await FeatureChannelBase._refresh_auto_guide_if_enabled(
        subject,
        context,
        channel,
    )

    assert result is True
    assert len(channel.send_attempts) == 1
    send_kwargs = channel.send_attempts[0]
    assert send_kwargs["mention_author"] is False
    assert send_kwargs["reference"].message_id == 5555
    view = send_kwargs["view"]
    assert [child.label for child in view.children] == [
        "Delete Your Teams",
        "Full Guide",
        "Google Sheets",
    ]
    assert [str(child.emoji) for child in view.children] == ["🗑️", "⤴️", "👀"]
    assert view.children[0].custom_id == ("rhoboto:auto_guide:delete:team_register")
    assert view.children[1].url == "https://discord.com/channels/111/222/5555"
    assert view.children[2].url == "https://sheet.example"
    embed = send_kwargs["embeds"][0]
    assert embed.title == "en:team.auto_guide.title"
    assert embed.description == "en:team.auto_guide.description"
    assert embed.footer.text == "en:team.auto_guide.footer"
    assert embed.color.value == config.DEFAULT_EMBED_COLOR
    assert old_message.delete_count == 1
    assert channel.fetched_message_ids == [1234]
    assert auto_state.saved_message_ids == [channel.sent_messages[0].id]
    assert all(
        values == {"bot": "@Rhoboto", "sheet_url": "https://sheet.example"}
        for _template_key, _locale, values in render_calls
    )


@pytest.mark.asyncio
async def test_auto_guide_delete_failure_logs_and_keeps_new_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = auto_guide_context()
    configured_context = ConfiguredFeatureChannelContext(
        guild_id=111,
        channel_id=222,
        feature_channel=context.feature_channel,
        manager=context.manager,
        feature_config=SimpleNamespace(sheet_url="https://sheet.example"),
    )
    old_message = AutoGuideMessage(1234, delete_error=fake_http_exception())
    channel = AutoGuideChannel(old_messages=[old_message])
    auto_state = SaveableAutoGuideState(message_id=1234)
    logger = RecordingLogger()
    subject = auto_guide_subject(logger=logger)

    async def fake_get_auto_guide_state(_feature_channel: object) -> object:
        return SimpleNamespace(is_enabled=True, message_id=1234)

    async def fake_get_or_create_auto_guide_state(_feature_channel: object) -> object:
        return auto_state

    async def fake_get_announcement_languages(
        _guild_id: int,
        _logger: object,
    ) -> list[str]:
        return ["en"]

    def fake_render_message_template(
        template_key: str,
        locale: str,
        **_values: object,
    ) -> str:
        return f"{locale}:{template_key}"

    async def fake_message_state_get_or_none(**_kwargs: object) -> None:
        return None

    async def configured_lookup(_context: object) -> object:
        return configured_context

    monkeypatch.setattr(
        feature_channel_base,
        "get_auto_guide_state",
        fake_get_auto_guide_state,
        raising=False,
    )
    monkeypatch.setattr(
        feature_channel_base,
        "get_or_create_auto_guide_state",
        fake_get_or_create_auto_guide_state,
        raising=False,
    )
    monkeypatch.setattr(
        feature_channel_base,
        "get_announcement_languages",
        fake_get_announcement_languages,
        raising=False,
    )
    monkeypatch.setattr(
        feature_channel_base,
        "render_message_template",
        fake_render_message_template,
    )
    monkeypatch.setattr(
        FeatureChannelMessageState,
        "get_or_none",
        fake_message_state_get_or_none,
    )
    subject._get_configured_feature_channel_context = configured_lookup

    result = await FeatureChannelBase._refresh_auto_guide_if_enabled(
        subject,
        context,
        channel,
    )

    assert result is True
    assert len(channel.sent_messages) == 1
    assert channel.sent_messages[0].delete_count == 0
    assert channel.fetched_message_ids == [1234]
    assert auto_state.saved_message_ids == [channel.sent_messages[0].id]
    assert logger.exceptions == []
    assert len(logger.warnings) == 1
    warning_args, warning_kwargs = logger.warnings[0]
    assert warning_args == ("Failed to delete previous auto guide message `%s`.", 1234)
    assert warning_kwargs == {"exc_info": True}


@pytest.mark.asyncio
async def test_auto_guide_reply_failure_falls_back_without_footer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = auto_guide_context()
    configured_context = ConfiguredFeatureChannelContext(
        guild_id=111,
        channel_id=222,
        feature_channel=context.feature_channel,
        manager=context.manager,
        feature_config=SimpleNamespace(sheet_url="https://sheet.example"),
    )
    channel = AutoGuideChannel(send_errors=[fake_http_exception()])
    auto_state = SaveableAutoGuideState()
    subject = auto_guide_subject()

    async def fake_get_auto_guide_state(_feature_channel: object) -> object:
        return SimpleNamespace(is_enabled=True, message_id=None)

    async def fake_get_or_create_auto_guide_state(_feature_channel: object) -> object:
        return auto_state

    async def fake_get_announcement_languages(
        _guild_id: int,
        _logger: object,
    ) -> list[str]:
        return ["en"]

    def fake_render_message_template(
        template_key: str,
        locale: str,
        **_values: object,
    ) -> str:
        return f"{locale}:{template_key}"

    async def fake_message_state_get_or_none(**_kwargs: object) -> object:
        return SimpleNamespace(message_id=5555)

    async def configured_lookup(_context: object) -> object:
        return configured_context

    monkeypatch.setattr(
        feature_channel_base,
        "get_auto_guide_state",
        fake_get_auto_guide_state,
        raising=False,
    )
    monkeypatch.setattr(
        feature_channel_base,
        "get_or_create_auto_guide_state",
        fake_get_or_create_auto_guide_state,
        raising=False,
    )
    monkeypatch.setattr(
        feature_channel_base,
        "get_announcement_languages",
        fake_get_announcement_languages,
        raising=False,
    )
    monkeypatch.setattr(
        feature_channel_base,
        "render_message_template",
        fake_render_message_template,
    )
    monkeypatch.setattr(
        FeatureChannelMessageState,
        "get_or_none",
        fake_message_state_get_or_none,
    )
    subject._get_configured_feature_channel_context = configured_lookup

    result = await FeatureChannelBase._refresh_auto_guide_if_enabled(
        subject,
        context,
        channel,
    )

    assert result is True
    assert len(channel.send_attempts) == 2
    reply_kwargs = channel.send_attempts[0]
    assert reply_kwargs["mention_author"] is False
    assert reply_kwargs["reference"].message_id == 5555
    normal_kwargs = channel.send_attempts[1]
    assert "mention_author" not in normal_kwargs
    assert "reference" not in normal_kwargs
    fallback_view = normal_kwargs["view"]
    assert [child.label for child in fallback_view.children] == [
        "Delete Your Teams",
        "Google Sheets",
    ]
    assert [str(child.emoji) for child in fallback_view.children] == ["🗑️", "👀"]
    assert normal_kwargs["embeds"][0].footer.text is None
    assert auto_state.saved_message_ids == [channel.sent_messages[0].id]


@pytest.mark.asyncio
async def test_auto_guide_buttons_use_first_announcement_language(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = auto_guide_context()
    configured_context = ConfiguredFeatureChannelContext(
        guild_id=111,
        channel_id=222,
        feature_channel=context.feature_channel,
        manager=context.manager,
        feature_config=SimpleNamespace(sheet_url="https://sheet.example"),
    )
    channel = AutoGuideChannel()
    auto_state = SaveableAutoGuideState()
    subject = auto_guide_subject()

    async def fake_get_auto_guide_state(_feature_channel: object) -> object:
        return SimpleNamespace(is_enabled=True, message_id=None)

    async def fake_get_or_create_auto_guide_state(_feature_channel: object) -> object:
        return auto_state

    async def fake_get_announcement_languages(
        _guild_id: int,
        _logger: object,
    ) -> list[str]:
        return ["zh_tw", "ja", "en"]

    def fake_render_message_template(
        template_key: str,
        locale: str,
        **_values: object,
    ) -> str:
        return f"{locale}:{template_key}"

    async def fake_message_state_get_or_none(**_kwargs: object) -> None:
        return None

    async def configured_lookup(_context: object) -> object:
        return configured_context

    monkeypatch.setattr(
        feature_channel_base,
        "get_auto_guide_state",
        fake_get_auto_guide_state,
    )
    monkeypatch.setattr(
        feature_channel_base,
        "get_or_create_auto_guide_state",
        fake_get_or_create_auto_guide_state,
    )
    monkeypatch.setattr(
        feature_channel_base,
        "get_announcement_languages",
        fake_get_announcement_languages,
    )
    monkeypatch.setattr(
        feature_channel_base,
        "render_message_template",
        fake_render_message_template,
    )
    monkeypatch.setattr(
        FeatureChannelMessageState,
        "get_or_none",
        fake_message_state_get_or_none,
    )
    subject._get_configured_feature_channel_context = configured_lookup

    result = await FeatureChannelBase._refresh_auto_guide_if_enabled(
        subject,
        context,
        channel,
    )

    assert result is True
    view = channel.send_attempts[0]["view"]
    assert [child.label for child in view.children] == [
        "刪除我的編成",
        "Google Sheets",
    ]


@pytest.mark.asyncio
async def test_auto_guide_save_id_failure_keeps_new_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = auto_guide_context()
    configured_context = ConfiguredFeatureChannelContext(
        guild_id=111,
        channel_id=222,
        feature_channel=context.feature_channel,
        manager=context.manager,
        feature_config=SimpleNamespace(sheet_url="https://sheet.example"),
    )
    channel = AutoGuideChannel()
    auto_state = SaveableAutoGuideState(save_error=RuntimeError("save failed"))
    subject = auto_guide_subject()

    async def fake_get_auto_guide_state(_feature_channel: object) -> object:
        return SimpleNamespace(is_enabled=True, message_id=None)

    async def fake_get_or_create_auto_guide_state(_feature_channel: object) -> object:
        return auto_state

    async def fake_get_announcement_languages(
        _guild_id: int,
        _logger: object,
    ) -> list[str]:
        return ["en"]

    def fake_render_message_template(
        template_key: str,
        locale: str,
        **_values: object,
    ) -> str:
        return f"{locale}:{template_key}"

    async def fake_message_state_get_or_none(**_kwargs: object) -> None:
        return None

    async def configured_lookup(_context: object) -> object:
        return configured_context

    monkeypatch.setattr(
        feature_channel_base,
        "get_auto_guide_state",
        fake_get_auto_guide_state,
        raising=False,
    )
    monkeypatch.setattr(
        feature_channel_base,
        "get_or_create_auto_guide_state",
        fake_get_or_create_auto_guide_state,
        raising=False,
    )
    monkeypatch.setattr(
        feature_channel_base,
        "get_announcement_languages",
        fake_get_announcement_languages,
        raising=False,
    )
    monkeypatch.setattr(
        feature_channel_base,
        "render_message_template",
        fake_render_message_template,
    )
    monkeypatch.setattr(
        FeatureChannelMessageState,
        "get_or_none",
        fake_message_state_get_or_none,
    )
    subject._get_configured_feature_channel_context = configured_lookup

    result = await FeatureChannelBase._refresh_auto_guide_if_enabled(
        subject,
        context,
        channel,
    )

    assert result is False
    assert len(channel.sent_messages) == 1
    assert channel.sent_messages[0].delete_count == 0
    assert auto_state.saved_message_ids == [channel.sent_messages[0].id]


def test_shift_auto_guide_template_values_include_timeline_values() -> None:
    subject = ShiftRegister(fake_bot())

    values = subject._auto_guide_template_values(shift_auto_guide_context(), "en")

    assert values["sheet_url"].endswith("#gid=444")
    assert values["recruitment_time_range"] == "4-28"
    assert values["event_date"].weekday == "Wed"
    assert values["submission_deadline"].hour == "21"
    assert values["draft_shift_proposal"].hour == "20"
    assert values["final_shift_notice"].hour == "18"


@pytest.mark.asyncio
async def test_shift_auto_guide_render_smoke_with_footer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_get_announcement_languages(
        _guild_id: int,
        _logger: object,
    ) -> list[str]:
        return ["ja"]

    monkeypatch.setattr(
        feature_channel_base,
        "get_announcement_languages",
        fake_get_announcement_languages,
        raising=False,
    )
    subject = ShiftRegister(fake_bot())

    embeds = await subject._render_auto_guide_embeds(
        shift_auto_guide_context(),
        include_footer=True,
    )

    assert len(embeds) == 1
    assert embeds[0].title
    assert "2日目" in embeds[0].title
    assert embeds[0].description
    assert embeds[0].footer.text


@pytest.mark.asyncio
async def test_team_public_guide_uses_summary_worksheet_gid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(FeatureChannel, "get", fake_feature_channel_get)
    monkeypatch.setattr(TeamRegister, "ManagerType", ConfiguredHelpUrlManager)
    captured_sheet_urls: list[object] = []

    async def fake_render_announcement_messages(
        template_key: str,
        guild_id: int,
        _logger: object = None,
        **values: object,
    ) -> list[RenderedAnnouncement]:
        assert template_key == "team.guide"
        assert guild_id == 111
        captured_sheet_urls.append(values["sheet_url"])
        return [RenderedAnnouncement(language="en", content="en guide")]

    monkeypatch.setattr(
        "cogs.base.feature_channel_base.render_announcement_messages",
        fake_render_announcement_messages,
    )
    monkeypatch.setattr(
        "cogs.base.feature_channel_base.save_manual_guide_anchor",
        _noop_async,
    )

    subject = TeamRegister(fake_bot())
    subject.bot.user = SimpleNamespace(mention="@Rhoboto")
    interaction = FakeInteraction(locale="en-US")
    interaction.followup = IdRecordingFollowup()

    await subject.send_guide_message(interaction)

    assert captured_sheet_urls == [
        "https://docs.google.com/spreadsheets/d/abc/edit#gid=333"
    ]
    assert interaction.followup.messages == [
        ("en guide", {"ephemeral": False, "wait": True})
    ]


@pytest.mark.asyncio
async def test_shift_public_guide_uses_entry_worksheet_gid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(FeatureChannel, "get", fake_feature_channel_get)
    monkeypatch.setattr(ShiftRegister, "ManagerType", ConfiguredHelpUrlManager)
    captured_sheet_urls: list[object] = []

    async def fake_render_announcement_messages(
        template_key: str,
        guild_id: int,
        _logger: object = None,
        **values: object,
    ) -> list[RenderedAnnouncement]:
        assert template_key == "shift.guide"
        assert guild_id == 111
        captured_sheet_urls.append(values["sheet_url"])
        return [RenderedAnnouncement(language="en", content="en guide")]

    monkeypatch.setattr(
        "cogs.base.feature_channel_base.render_announcement_messages",
        fake_render_announcement_messages,
    )
    monkeypatch.setattr(
        "cogs.base.feature_channel_base.save_manual_guide_anchor",
        _noop_async,
    )

    subject = ShiftRegister(fake_bot())
    subject.bot.user = SimpleNamespace(mention="@Rhoboto")
    interaction = FakeInteraction(locale="en-US")
    interaction.followup = IdRecordingFollowup()

    await subject.send_guide_message(interaction)

    assert captured_sheet_urls == [
        "https://docs.google.com/spreadsheets/d/abc/edit#gid=444"
    ]
    assert interaction.followup.messages == [
        ("en guide", {"ephemeral": False, "wait": True})
    ]


@pytest.mark.asyncio
async def test_team_user_guide_uses_summary_worksheet_gid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(FeatureChannel, "get", fake_feature_channel_get)
    monkeypatch.setattr(Team, "ManagerType", ConfiguredHelpUrlManager)
    captured_values: dict[str, object] = {}

    def fake_render_message_template(
        template_key: str,
        locale: str,
        **values: object,
    ) -> str:
        assert template_key == "team.guide"
        assert locale == "en"
        captured_values.update(values)
        return "rendered team guide"

    monkeypatch.setattr(
        "cogs.base.feature_channel_base.render_message_template",
        fake_render_message_template,
    )

    subject = Team(fake_bot())
    subject.bot.user = SimpleNamespace(mention="@Rhoboto")
    interaction = FakeInteraction(locale="en-US")

    await subject.send_guide_message(interaction, TeamRegister.guide_template_key)

    assert captured_values["sheet_url"] == (
        "https://docs.google.com/spreadsheets/d/abc/edit#gid=333"
    )
    assert interaction.followup.messages == [
        ("rendered team guide", {"ephemeral": True})
    ]


@pytest.mark.asyncio
async def test_shift_user_guide_uses_entry_worksheet_gid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(FeatureChannel, "get", fake_feature_channel_get)
    monkeypatch.setattr(Shift, "ManagerType", ConfiguredHelpUrlManager)
    captured_values: dict[str, object] = {}

    def fake_render_message_template(
        template_key: str,
        locale: str,
        **values: object,
    ) -> str:
        assert template_key == "shift.guide"
        assert locale == "en"
        captured_values.update(values)
        return "rendered shift guide"

    monkeypatch.setattr(
        "cogs.base.feature_channel_base.render_message_template",
        fake_render_message_template,
    )

    subject = Shift(fake_bot())
    subject.bot.user = SimpleNamespace(mention="@Rhoboto")
    interaction = FakeInteraction(locale="en-US")

    await subject.send_guide_message(interaction, ShiftRegister.guide_template_key)

    assert captured_values["sheet_url"] == (
        "https://docs.google.com/spreadsheets/d/abc/edit#gid=444"
    )
    assert interaction.followup.messages == [
        ("rendered shift guide", {"ephemeral": True})
    ]


@pytest.mark.asyncio
async def test_public_register_guide_sends_announcement_languages_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(FeatureChannel, "get", fake_feature_channel_get)

    async def fake_render_announcement_messages(
        template_key: str,
        guild_id: int,
        _logger: object = None,
        **values: object,
    ) -> list[RenderedAnnouncement]:
        assert template_key == "team.guide"
        assert guild_id == 111
        assert values["bot"] == "@Rhoboto"
        assert values["sheet_url"] == "https://sheet.example"
        return [
            RenderedAnnouncement(language="ja", content="ja guide"),
            RenderedAnnouncement(language="zh_tw", content="zh guide"),
            RenderedAnnouncement(language="en", content="en guide"),
        ]

    monkeypatch.setattr(
        "cogs.base.feature_channel_base.render_announcement_messages",
        fake_render_announcement_messages,
    )
    monkeypatch.setattr(
        "cogs.base.feature_channel_base.save_manual_guide_anchor",
        _noop_async,
    )

    interaction = FakeInteraction(locale="en-US")
    interaction.followup = IdRecordingFollowup()
    subject = feature_channel_context_subject(
        feature_name="team_register",
        feature_display_name="Team Register",
        ManagerType=ConfiguredManager,
        bot=SimpleNamespace(user=SimpleNamespace(mention="@Rhoboto")),
        guide_template_key="team.guide",
        logger=NullLogger(),
    )

    await FeatureChannelBase.send_guide_message(subject, interaction)

    assert interaction.response.deferred == [False]
    assert [content for content, _kwargs in interaction.followup.messages] == [
        "ja guide",
        "zh guide",
        "en guide",
    ]


@pytest.mark.asyncio
async def test_public_register_guide_saves_first_manual_anchor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(FeatureChannel, "get", fake_feature_channel_get)
    saved_anchors: list[tuple[object, int]] = []

    async def fake_render_announcement_messages(
        template_key: str,
        guild_id: int,
        _logger: object = None,
        **values: object,
    ) -> list[RenderedAnnouncement]:
        assert template_key == "team.guide"
        assert guild_id == 111
        assert values["bot"] == "@Rhoboto"
        assert values["sheet_url"] == "https://sheet.example"
        return [
            RenderedAnnouncement(language="ja", content="ja guide"),
            RenderedAnnouncement(language="en", content="en guide"),
        ]

    async def fake_save_manual_guide_anchor(
        feature_channel: object,
        message_id: int,
    ) -> None:
        saved_anchors.append((feature_channel, message_id))

    monkeypatch.setattr(
        "cogs.base.feature_channel_base.render_announcement_messages",
        fake_render_announcement_messages,
    )
    monkeypatch.setattr(
        "cogs.base.feature_channel_base.save_manual_guide_anchor",
        fake_save_manual_guide_anchor,
        raising=False,
    )

    interaction = FakeInteraction(locale="en-US")
    interaction.followup = IdRecordingFollowup()
    subject = feature_channel_context_subject(
        feature_name="team_register",
        feature_display_name="Team Register",
        ManagerType=ConfiguredManager,
        bot=SimpleNamespace(user=SimpleNamespace(mention="@Rhoboto")),
        guide_template_key="team.guide",
        logger=NullLogger(),
    )

    await FeatureChannelBase.send_guide_message(subject, interaction)

    assert [
        (anchor.feature_name, message_id) for anchor, message_id in saved_anchors
    ] == [("team_register", 501)]
    assert [content for content, _kwargs in interaction.followup.messages] == [
        "ja guide",
        "en guide",
    ]


@pytest.mark.asyncio
async def test_public_register_guide_reports_missing_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(FeatureChannel, "get", fake_feature_channel_get)
    interaction = FakeInteraction()
    subject = feature_channel_context_subject(
        feature_name="team_register",
        feature_display_name="Team Register",
        ManagerType=MissingConfigManager,
        bot=SimpleNamespace(user=SimpleNamespace(mention="@Rhoboto")),
        guide_template_key="team.guide",
        logger=NullLogger(),
    )

    await FeatureChannelBase.send_guide_message(subject, interaction)

    assert interaction.response.deferred == [False]
    assert interaction.followup.messages == [
        (
            "⚠️ Team Register is not configured for this channel.",
            {"ephemeral": True},
        )
    ]


@pytest.mark.asyncio
async def test_public_register_guide_reports_render_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(FeatureChannel, "get", fake_feature_channel_get)
    saved_anchors: list[tuple[object, int]] = []

    async def fake_render_announcement_messages(
        *_args: object,
        **_kwargs: object,
    ) -> list[RenderedAnnouncement]:
        return []

    monkeypatch.setattr(
        "cogs.base.feature_channel_base.render_announcement_messages",
        fake_render_announcement_messages,
    )

    async def fake_save_manual_guide_anchor(
        feature_channel: object,
        message_id: int,
    ) -> None:
        saved_anchors.append((feature_channel, message_id))

    monkeypatch.setattr(
        "cogs.base.feature_channel_base.save_manual_guide_anchor",
        fake_save_manual_guide_anchor,
        raising=False,
    )

    interaction = FakeInteraction()
    subject = feature_channel_context_subject(
        feature_name="team_register",
        feature_display_name="Team Register",
        ManagerType=ConfiguredManager,
        bot=SimpleNamespace(user=SimpleNamespace(mention="@Rhoboto")),
        guide_template_key="team.guide",
        logger=NullLogger(),
    )

    await FeatureChannelBase.send_guide_message(subject, interaction)

    assert saved_anchors == []
    assert interaction.followup.messages == [
        (
            "No announcement templates could be rendered for this server.",
            {"ephemeral": True},
        )
    ]


@pytest.mark.asyncio
async def test_public_register_guide_manual_anchor_save_failure_logs_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(FeatureChannel, "get", fake_feature_channel_get)

    async def fake_render_announcement_messages(
        *_args: object,
        **_kwargs: object,
    ) -> list[RenderedAnnouncement]:
        return [RenderedAnnouncement(language="en", content="en guide")]

    async def fake_save_manual_guide_anchor(
        _feature_channel: object,
        _message_id: int,
    ) -> None:
        msg = "anchor save failed"
        raise RuntimeError(msg)

    monkeypatch.setattr(
        "cogs.base.feature_channel_base.render_announcement_messages",
        fake_render_announcement_messages,
    )
    monkeypatch.setattr(
        "cogs.base.feature_channel_base.save_manual_guide_anchor",
        fake_save_manual_guide_anchor,
        raising=False,
    )

    logger = RecordingLogger()
    interaction = FakeInteraction()
    interaction.followup = IdRecordingFollowup()
    subject = feature_channel_context_subject(
        feature_name="team_register",
        feature_display_name="Team Register",
        ManagerType=ConfiguredManager,
        bot=SimpleNamespace(user=SimpleNamespace(mention="@Rhoboto")),
        guide_template_key="team.guide",
        logger=logger,
    )

    await FeatureChannelBase.send_guide_message(subject, interaction)

    assert interaction.followup.messages == [
        ("en guide", {"ephemeral": False, "wait": True})
    ]
    assert len(logger.warnings) == 1
    warning_args, warning_kwargs = logger.warnings[0]
    assert "Failed to save manual guide anchor" in str(warning_args[0])
    assert warning_kwargs == {"exc_info": True}


@pytest.mark.asyncio
async def test_public_register_guide_saves_manual_anchor_before_later_send_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(FeatureChannel, "get", fake_feature_channel_get)
    events: list[tuple[str, object]] = []

    async def fake_render_announcement_messages(
        *_args: object,
        **_kwargs: object,
    ) -> list[RenderedAnnouncement]:
        return [
            RenderedAnnouncement(language="ja", content="ja guide"),
            RenderedAnnouncement(language="en", content="en guide"),
        ]

    async def fake_save_manual_guide_anchor(
        _feature_channel: object,
        message_id: int,
    ) -> None:
        events.append(("save", message_id))

    class SecondSendFailsFollowup(IdRecordingFollowup):
        async def send(
            self,
            content: str | None = None,
            **kwargs: object,
        ) -> SimpleNamespace:
            events.append(("send", content))
            if len(self.sent_message_objects) == 1:
                msg = "second send failed"
                raise RuntimeError(msg)
            return await super().send(content, **kwargs)

    monkeypatch.setattr(
        "cogs.base.feature_channel_base.render_announcement_messages",
        fake_render_announcement_messages,
    )
    monkeypatch.setattr(
        "cogs.base.feature_channel_base.save_manual_guide_anchor",
        fake_save_manual_guide_anchor,
        raising=False,
    )

    interaction = FakeInteraction()
    interaction.followup = SecondSendFailsFollowup()
    subject = feature_channel_context_subject(
        feature_name="team_register",
        feature_display_name="Team Register",
        ManagerType=ConfiguredManager,
        bot=SimpleNamespace(user=SimpleNamespace(mention="@Rhoboto")),
        guide_template_key="team.guide",
        logger=NullLogger(),
    )

    with pytest.raises(RuntimeError, match="second send failed"):
        await FeatureChannelBase.send_guide_message(subject, interaction)

    assert events == [
        ("send", "ja guide"),
        ("save", 501),
        ("send", "en guide"),
    ]


@pytest.mark.asyncio
async def test_shift_timeline_defers_before_public_followup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(FeatureChannel, "get", fake_feature_channel_get)

    async def fake_render_shift_timeline_announcement_messages(
        template_key: str,
        guild_id: int,
        _logger: object = None,
        **values: object,
    ) -> list[RenderedAnnouncement]:
        assert template_key == "shift.timeline"
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
            RenderedAnnouncement(language="ja", content="ja timeline"),
            RenderedAnnouncement(language="en", content="en timeline"),
        ]

    monkeypatch.setattr(
        "cogs.shift_register.render_shift_timeline_announcement_messages",
        fake_render_shift_timeline_announcement_messages,
    )

    interaction = FakeInteraction(locale="ja")
    subject = feature_channel_context_subject(
        feature_name="shift_register",
        ManagerType=ConfiguredMultiRangeShiftInfoManager,
        bot=SimpleNamespace(user=SimpleNamespace(mention="@Rhoboto")),
        timeline_template_key="shift.timeline",
        logger=NullLogger(),
    )

    await ShiftRegister.announce_timeline.callback(
        subject,
        interaction,
    )

    assert interaction.response.deferred == [False]
    assert interaction.followup.messages == [
        ("ja timeline", {"ephemeral": False}),
        ("en timeline", {"ephemeral": False}),
    ]


@pytest.mark.asyncio
async def test_shift_timeline_db_failure_sends_storage_followup() -> None:
    async def failing_context(_source: object) -> object:
        raise private_database_error()

    interaction = FakeInteraction(locale="ja")
    subject = feature_channel_context_subject(
        feature_name="shift_register",
        feature_display_name="Shift Register",
        logger=NullLogger(),
    )
    subject._get_feature_channel_context = failing_context

    await ShiftRegister.announce_timeline.callback(subject, interaction)

    assert interaction.response.deferred == [False]
    contents = interaction_contents(interaction)
    assert len(contents) == 1
    assert_safe_storage_content(contents[0])
    assert interaction.followup.messages[0][1] == {"ephemeral": True}


@pytest.mark.asyncio
async def test_shift_timeline_delivery_timeout_is_not_classified_as_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(FeatureChannel, "get", fake_feature_channel_get)

    async def fake_render_shift_timeline_announcement_messages(
        *_: object,
        **__: object,
    ) -> list[RenderedAnnouncement]:
        return [RenderedAnnouncement(language="en", content="en timeline")]

    async def fail_delivery(*_: object, **__: object) -> bool:
        message = "discord delivery timeout"
        raise TimeoutError(message)

    monkeypatch.setattr(
        "cogs.shift_register.render_shift_timeline_announcement_messages",
        fake_render_shift_timeline_announcement_messages,
    )
    monkeypatch.setattr(
        "cogs.shift_register._send_public_announcement_followups",
        fail_delivery,
    )
    interaction = FakeInteraction(locale="ja")
    subject = feature_channel_context_subject(
        feature_name="shift_register",
        ManagerType=ConfiguredShiftInfoManager,
        timeline_template_key="shift.timeline",
        logger=NullLogger(),
    )

    with pytest.raises(TimeoutError, match="discord delivery timeout"):
        await ShiftRegister.announce_timeline.callback(subject, interaction)

    assert interaction.response.deferred == [False]
    assert interaction.followup.messages == []


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
    subject = feature_channel_context_subject(
        feature_name="team_register",
        feature_display_name="Team Register",
        ManagerType=MissingConfigManager,
        sheet_write_lock=lock,
    )

    await TeamRegister.summary.callback(subject, interaction)

    assert interaction.response.deferred == [True]
    assert interaction.followup.messages == [
        (
            "⚠️ Team Register is not configured for this channel.",
            {"ephemeral": True},
        )
    ]
    assert lock.keys == []


@pytest.mark.asyncio
async def test_team_summary_config_lookup_db_failure_sends_safe_storage_followup() -> (
    None
):
    async def failing_context(_source: object) -> object:
        raise private_database_error()

    interaction = FakeInteraction()
    subject = feature_channel_context_subject(
        feature_name="team_register",
        feature_display_name="Team Register",
        sheet_write_lock=RecordingLock(),
        logger=NullLogger(),
    )
    subject._get_feature_channel_context = failing_context

    await TeamRegister.summary.callback(subject, interaction)

    assert interaction.response.deferred == [True]
    contents = interaction_contents(interaction)
    assert len(contents) == 1
    assert_safe_storage_content(contents[0])
    assert interaction.followup.messages[0][1] == {"ephemeral": True}


@pytest.mark.asyncio
async def test_team_summary_google_sheets_failure_sends_safe_storage_followup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(FeatureChannel, "get", fake_feature_channel_get)
    interaction = FakeInteraction()
    subject = feature_channel_context_subject(
        feature_name="team_register",
        ManagerType=SummaryGoogleSheetsErrorManager,
        sheet_write_lock=RecordingLock(),
        logger=NullLogger(),
    )

    await TeamRegister.summary.callback(subject, interaction)

    assert interaction.response.deferred == [True]
    contents = interaction_contents(interaction)
    assert len(contents) == 1
    assert "Reference: `STG-" in contents[0]
    assert "private sheet quota detail" not in contents[0]
    assert interaction.followup.messages[0][1] == {"ephemeral": True}


@pytest.mark.asyncio
async def test_team_summary_refresh_failure_reports_partial_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(FeatureChannel, "get", fake_feature_channel_get)
    interaction = FakeInteraction()
    lock = RecordingLock()
    subject = feature_channel_context_subject(
        feature_name="team_register",
        ManagerType=SummaryRefreshErrorManager,
        sheet_write_lock=lock,
        logger=NullLogger(),
    )

    await TeamRegister.summary.callback(subject, interaction)

    manager = SummaryRefreshErrorManager.last_instance
    assert manager is not None
    assert manager.ensure_count == 0
    assert manager.refresh_metadata is manager.ensured_metadata
    contents = interaction_contents(interaction)
    assert len(contents) == 1
    assert "Some changes may have been saved" in contents[0]
    assert "Reference: `STG-" in contents[0]
    assert "private database" not in contents[0]
    assert interaction.followup.messages[0][1] == {"ephemeral": True}


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
    subject = feature_channel_context_subject(
        feature_name="team_register",
        ManagerType=SummaryManager,
        sheet_write_lock=lock,
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
    subject = feature_channel_context_subject(
        feature_name="team_register",
        ManagerType=SummaryManager,
        sheet_write_lock=RecordingLock(),
    )

    with pytest.raises(
        ValueError,
        match=re.escape(
            "Interaction guild or channel is None. Cannot refresh team summary."
        ),
    ):
        await TeamRegister.summary.callback(subject, interaction)

    assert interaction.response.deferred == []


@pytest.mark.asyncio
async def test_delete_callback_sends_confirmation_without_touching_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock = RecordingLock()
    created_views: list[object] = []

    class ConfirmView:
        value = False

        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs
            created_views.append(self)

        async def wait(self) -> None:
            return None

    async def fail_delete(*_: object, **__: object) -> None:
        raise AssertionError

    monkeypatch.setattr(
        feature_channel_base,
        "ConfirmDeleteUserDataView",
        ConfirmView,
    )
    subject = feature_channel_context_subject(
        feature_name="team_register",
        feature_display_name="Team Register",
        ManagerType=MissingConfigManager,
        FeatureChannelType=SimpleNamespace(sheet_write_lock=lock),
        _delete_user_data=fail_delete,
    )
    interaction = FakeInteraction(locale="en-US", user_id=333)

    await FeatureChannelUserBase.delete_callback(subject, interaction)

    assert interaction.response.deferred == []
    assert len(interaction.response.messages) == 1
    content, kwargs = interaction.response.messages[0]
    assert content == (
        "‼️ Are you sure you want to delete your data for Team Register in this "
        "channel? This will only delete the data from Google Sheets."
    )
    assert kwargs["ephemeral"] is True
    assert kwargs["view"] is created_views[0]
    assert created_views[0].kwargs["requesting_user_id"] == 333
    assert created_views[0].kwargs["confirm_label"] == "Confirm"
    assert created_views[0].kwargs["cancel_label"] == "Cancel"
    assert created_views[0].kwargs["in_progress_message"] == (
        f"{config.PROCESSING_EMOJI} Deleting your data..."
    )
    assert created_views[0].kwargs["cancelled_message"] == "✖️ Delete cancelled."
    assert created_views[0].kwargs["unauthorized_message"] == (
        "⚠️ Only the user who started this delete request can use these buttons."
    )
    assert lock.keys == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("view_value", "expected_followups"),
    [
        (False, []),
        (None, [("✖️ No response received. Delete cancelled.", {"ephemeral": True})]),
    ],
)
async def test_delete_callback_cancel_or_timeout_skips_delete_and_lock(
    monkeypatch: pytest.MonkeyPatch,
    view_value: object,
    expected_followups: list[tuple[str, dict[str, object]]],
) -> None:
    lock = RecordingLock()
    deleted: list[object] = []

    class ConfirmView:
        value = view_value

        def __init__(self, **_kwargs: object) -> None:
            pass

        async def wait(self) -> None:
            return None

    async def fake_delete_user_data(*args: object) -> None:
        deleted.append(args)

    monkeypatch.setattr(
        feature_channel_base,
        "ConfirmDeleteUserDataView",
        ConfirmView,
    )
    subject = feature_channel_context_subject(
        feature_name="team_register",
        feature_display_name="Team Register",
        ManagerType=DeleteManager,
        FeatureChannelType=SimpleNamespace(sheet_write_lock=lock),
        _delete_user_data=fake_delete_user_data,
    )
    interaction = FakeInteraction(locale="en-US", user_id=333)

    await FeatureChannelUserBase.delete_callback(subject, interaction)

    assert interaction.followup.messages == expected_followups
    assert deleted == []
    assert lock.keys == []


@pytest.mark.asyncio
async def test_delete_after_confirmation_deletes_with_configured_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(FeatureChannel, "get_or_none", fake_feature_channel_get_or_none)
    lock = RecordingLock()
    deleted: list[tuple[object, object, object]] = []

    async def fake_delete_user_data(
        manager: object,
        user_info: object,
        metadata: object,
    ) -> None:
        deleted.append((manager, user_info, metadata))

    subject = feature_channel_context_subject(
        feature_name="team_register",
        feature_display_name="Team Register",
        ManagerType=DeleteManager,
        FeatureChannelType=SimpleNamespace(sheet_write_lock=lock),
        _delete_user_data=fake_delete_user_data,
    )
    interaction = FakeInteraction(locale="en-US", user_id=333)
    source = require_guild_channel_source(
        interaction,
        action="delete feature user data",
    )

    result = await FeatureChannelUserBase._delete_user_data_after_confirmation(
        subject,
        interaction,
        source,
    )

    manager = DeleteManager.last_instance
    assert manager is not None
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
    assert result == (
        "✅ Your data for Team Register has been deleted from Google Sheets. If "
        "you also want to remove your original registration message from Discord, "
        "you'll need to delete it yourself."
    )
    assert interaction.followup.messages == []


@pytest.mark.asyncio
async def test_delete_callback_confirm_edits_prompt_to_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ConfirmView:
        value = True

        def __init__(self, **_kwargs: object) -> None:
            pass

        async def wait(self) -> None:
            return None

    async def fake_delete_after_confirmation(
        interaction_arg: object,
        source_arg: object,
    ) -> str:
        assert interaction_arg is interaction
        assert source_arg is interaction
        return "success"

    monkeypatch.setattr(
        feature_channel_base,
        "ConfirmDeleteUserDataView",
        ConfirmView,
    )
    subject = feature_channel_context_subject(
        feature_name="team_register",
        feature_display_name="Team Register",
    )
    subject._delete_user_data_after_confirmation = fake_delete_after_confirmation
    interaction = FakeInteraction(locale="en-US", user_id=333)

    await FeatureChannelUserBase.delete_callback(subject, interaction)

    assert interaction.original_response_edits == [("success", {"view": None})]
    assert interaction.followup.messages == []


@pytest.mark.asyncio
async def test_delete_after_confirmation_reports_missing_config_without_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(FeatureChannel, "get_or_none", fake_feature_channel_get_or_none)
    lock = RecordingLock()

    async def fail_delete(*_: object, **__: object) -> None:
        raise AssertionError

    subject = feature_channel_context_subject(
        feature_name="team_register",
        feature_display_name="Team Register",
        ManagerType=MissingConfigManager,
        FeatureChannelType=SimpleNamespace(sheet_write_lock=lock),
        _delete_user_data=fail_delete,
    )
    interaction = FakeInteraction(locale="en-US")
    source = require_guild_channel_source(
        interaction,
        action="delete feature user data",
    )

    result = await FeatureChannelUserBase._delete_user_data_after_confirmation(
        subject,
        interaction,
        source,
    )

    assert interaction.followup.messages == [
        (
            "⚠️ Team Register is not configured for this channel.",
            {"ephemeral": True},
        )
    ]
    assert result is None
    assert lock.keys == []


@pytest.mark.asyncio
async def test_delete_after_confirmation_disabled_feature_skips_delete_and_lock() -> (
    None
):
    lock = RecordingLock()
    deleted: list[tuple[object, object, object]] = []

    async def fake_get_enabled_context_or_none(
        *,
        guild_id: int,
        channel_id: int,
        require_enabled: bool = False,
    ) -> object | None:
        assert (guild_id, channel_id, require_enabled) == (111, 222, True)
        return None

    async def fake_get_feature_channel_context(_source: object) -> object:
        return object()

    async def fake_get_configured_feature_channel_context(
        _context: object,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            channel_id=222,
            manager=SimpleNamespace(metadata=SimpleNamespace(name="metadata")),
        )

    async def fake_delete_user_data(
        manager: object,
        user_info: object,
        metadata: object,
    ) -> None:
        deleted.append((manager, user_info, metadata))

    subject = feature_channel_context_subject(
        feature_name="team_register",
        feature_display_name="Team Register",
        FeatureChannelType=SimpleNamespace(sheet_write_lock=lock),
        _delete_user_data=fake_delete_user_data,
    )
    subject._get_feature_channel_context_or_none = fake_get_enabled_context_or_none
    subject._get_feature_channel_context = fake_get_feature_channel_context
    subject._get_configured_feature_channel_context = (
        fake_get_configured_feature_channel_context
    )
    interaction = FakeInteraction(locale="en-US")
    source = require_guild_channel_source(
        interaction,
        action="delete feature user data",
    )

    result = await FeatureChannelUserBase._delete_user_data_after_confirmation(
        subject,
        interaction,
        source,
    )

    assert interaction.followup.messages == [
        (
            "⚠️ Team Register is not enabled in this channel.",
            {"ephemeral": True},
        )
    ]
    assert result is None
    assert deleted == []
    assert lock.keys == []


@pytest.mark.asyncio
async def test_delete_after_confirmation_missing_feature_row_reports_not_enabled() -> (
    None
):
    async def fake_get_enabled_context_or_none(
        *,
        guild_id: int,
        channel_id: int,
        require_enabled: bool = False,
    ) -> object | None:
        assert (guild_id, channel_id, require_enabled) == (111, 222, True)
        return None

    async def fail_stale_row_lookup(_source: object) -> object:
        msg = "stale hard-cleared row lookup must not run"
        raise AssertionError(msg)

    subject = feature_channel_context_subject(
        feature_name="team_register",
        feature_display_name="Team Register",
        logger=NullLogger(),
    )
    subject._get_feature_channel_context_or_none = fake_get_enabled_context_or_none
    subject._get_feature_channel_context = fail_stale_row_lookup
    interaction = FakeInteraction(locale="en-US")
    source = require_guild_channel_source(
        interaction,
        action="delete feature user data",
    )

    result = await FeatureChannelUserBase._delete_user_data_after_confirmation(
        subject,
        interaction,
        source,
    )

    assert interaction.followup.messages == [
        (
            "⚠️ Team Register is not enabled in this channel.",
            {"ephemeral": True},
        )
    ]
    assert result is None


@pytest.mark.asyncio
async def test_delete_callback_uses_feature_catalog_in_zh_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(FeatureChannel, "get_or_none", fake_feature_channel_get_or_none)
    lock = RecordingLock()

    async def fake_delete_user_data(*_: object) -> None:
        return None

    subject = feature_channel_context_subject(
        feature_name="team_register",
        feature_display_name="Team Register",
        ManagerType=DeleteManager,
        FeatureChannelType=SimpleNamespace(sheet_write_lock=lock),
        _delete_user_data=fake_delete_user_data,
    )
    interaction = FakeInteraction(locale="zh-TW")
    source = require_guild_channel_source(
        interaction,
        action="delete feature user data",
    )

    result = await FeatureChannelUserBase._delete_user_data_after_confirmation(
        subject,
        interaction,
        source,
    )

    assert result == (
        "✅ 已成功刪除您在 Google Sheets 中的隊伍編成登記資料。"
        "若也想移除 Discord 上的原始登記訊息，"  # noqa: RUF001
        "請記得自行刪除。"
    )
    assert interaction.followup.messages == []


@pytest.mark.asyncio
async def test_delete_callback_uses_feature_catalog_in_ja_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(FeatureChannel, "get_or_none", fake_feature_channel_get_or_none)
    lock = RecordingLock()

    async def fake_delete_user_data(*_: object) -> None:
        return None

    subject = feature_channel_context_subject(
        feature_name="team_register",
        feature_display_name="Team Register",
        ManagerType=DeleteManager,
        FeatureChannelType=SimpleNamespace(sheet_write_lock=lock),
        _delete_user_data=fake_delete_user_data,
    )
    interaction = FakeInteraction(locale="ja")
    source = require_guild_channel_source(
        interaction,
        action="delete feature user data",
    )

    result = await FeatureChannelUserBase._delete_user_data_after_confirmation(
        subject,
        interaction,
        source,
    )

    assert result == (
        "✅ Google Sheets 上の編成登録の入力データを正常に削除しました。"
        "Discord 上の元の登録メッセージも削除したい場合は、"
        "ご自身で削除してください。"
    )
    assert interaction.followup.messages == []


@pytest.mark.asyncio
async def test_delete_callback_missing_channel_raises_before_defer() -> None:
    async def fail_delete(*_: object, **__: object) -> None:
        raise AssertionError

    interaction = FakeInteraction()
    interaction.channel = None
    subject = feature_channel_context_subject(
        feature_name="team_register",
        ManagerType=DeleteManager,
        FeatureChannelType=SimpleNamespace(sheet_write_lock=RecordingLock()),
        _delete_user_data=fail_delete,
    )

    with pytest.raises(
        ValueError,
        match=re.escape(
            "Interaction guild or channel is None. Cannot delete feature user data."
        ),
    ):
        await FeatureChannelUserBase.delete_callback(subject, interaction)

    assert interaction.response.deferred == []
    assert interaction.response.messages == []


@pytest.mark.asyncio
async def test_auto_guide_persistent_delete_button_reuses_delete_callback() -> None:
    calls: list[object] = []
    subject = feature_channel_context_subject(
        feature_name="team_register",
        feature_display_name="Team Register",
    )

    async def fake_delete_callback(interaction: object) -> None:
        calls.append(interaction)

    subject.delete_callback = fake_delete_callback
    view = FeatureChannelUserBase.build_auto_guide_delete_view(subject)
    interaction = FakeInteraction()

    await view.children[0].callback(interaction)

    assert view.is_persistent()
    assert view.children[0].custom_id == ("rhoboto:auto_guide:delete:team_register")
    assert calls == [interaction]


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
    assert not hasattr(FeatureChannelBase, "process_upsert_from_message")
    assert not hasattr(FeatureChannelBase, "_process_upsert_from_message_with_outcome")
    assert hasattr(FeatureChannelBase, "_process_feature_channel_message_with_outcome")
    assert "process_upsert_from_message" not in TeamRegister.__dict__
    assert "process_upsert_from_message" not in ShiftRegister.__dict__
    assert "_process_upsert_from_message_with_outcome" not in TeamRegister.__dict__
    assert "_process_upsert_from_message_with_outcome" not in ShiftRegister.__dict__
    assert "_parse_message_submission" not in TeamRegister.__dict__
    assert "_parse_message_submission" not in ShiftRegister.__dict__
    assert TeamRegister.ParserType is TeamParser
    assert ShiftRegister.ParserType is ShiftParser
    assert "_process_configured_message_submission" in TeamRegister.__dict__
    assert "_process_configured_message_submission" in ShiftRegister.__dict__


@pytest.mark.asyncio
async def test_team_register_message_upsert_writes_summary_after_team_source() -> None:
    subject = TeamRegister(fake_bot())
    manager = OrderedTeamUpsertManager(object(), "service.json")
    context = ordered_team_upsert_context(manager)
    submission, user_info = team_register_submission()
    message = FakeRegisterMessage(content="150/740/33")

    await subject._process_configured_message_submission(
        message,
        context,
        submission,
        user_info,
    )

    assert manager.events.index("teams_done") < manager.events.index("summary")


@pytest.mark.asyncio
async def test_team_register_message_upsert_marks_ensure_failure_partial_success() -> (
    None
):
    subject = TeamRegister(fake_bot())
    manager = OrderedTeamUpsertManager(
        object(),
        "service.json",
        ensure_error=private_database_error(),
    )
    context = ordered_team_upsert_context(manager)
    submission, user_info = team_register_submission()
    message = FakeRegisterMessage(content="150/740/33")

    with pytest.raises(StorageError) as exc_info:
        await subject._process_configured_message_submission(
            message,
            context,
            submission,
            user_info,
        )

    error = exc_info.value
    assert manager.events == ["fetch_metadata", "log_missing", "ensure"]
    assert error.kind is StorageErrorKind.PARTIAL_SUCCESS
    assert isinstance(error.__cause__, StorageError)
    assert error.__cause__.kind is StorageErrorKind.DATABASE_UNAVAILABLE


@pytest.mark.asyncio
async def test_team_register_message_upsert_marks_team_failure_partial_success() -> (
    None
):
    error = StorageError(
        StorageErrorKind.GOOGLE_SHEETS_TRANSIENT,
    )
    subject = TeamRegister(fake_bot())
    manager = OrderedTeamUpsertManager(object(), "service.json", team_error=error)
    context = ordered_team_upsert_context(manager)
    submission, user_info = team_register_submission()
    message = FakeRegisterMessage(content="150/740/33")

    with pytest.raises(StorageError) as exc_info:
        await subject._process_configured_message_submission(
            message,
            context,
            submission,
            user_info,
        )

    raised_error = exc_info.value
    assert "summary" not in manager.events
    assert raised_error.kind is StorageErrorKind.PARTIAL_SUCCESS
    assert isinstance(raised_error.__cause__, StorageError)
    assert raised_error.__cause__.kind is StorageErrorKind.GOOGLE_SHEETS_TRANSIENT


@pytest.mark.asyncio
async def test_team_register_message_upsert_marks_summary_failure_partial_success() -> (
    None
):
    subject = TeamRegister(fake_bot())
    manager = OrderedTeamUpsertManager(
        object(),
        "service.json",
        summary_error=private_database_error(),
    )
    context = ordered_team_upsert_context(manager)
    submission, user_info = team_register_submission()
    message = FakeRegisterMessage(content="150/740/33")

    with pytest.raises(StorageError) as exc_info:
        await subject._process_configured_message_submission(
            message,
            context,
            submission,
            user_info,
        )

    error = exc_info.value
    assert manager.events.index("teams_done") < manager.events.index("summary")
    assert error.kind is StorageErrorKind.PARTIAL_SUCCESS
    assert isinstance(error.__cause__, StorageError)
    assert error.__cause__.kind is StorageErrorKind.DATABASE_UNAVAILABLE


@pytest.mark.asyncio
async def test_shift_register_message_upsert_marks_ensure_failure_partial_success() -> (
    None
):
    subject = ShiftRegister(fake_bot())
    manager = OrderedShiftUpsertManager(
        object(),
        "service.json",
        ensure_error=private_database_error(),
    )
    context = ordered_shift_upsert_context(manager)
    submission, user_info = shift_register_submission()
    message = FakeRegisterMessage(content="4-8")

    with pytest.raises(StorageError) as exc_info:
        await subject._process_configured_message_submission(
            message,
            context,
            submission,
            user_info,
        )

    error = exc_info.value
    assert manager.events == ["fetch_metadata", "log_missing", "ensure"]
    assert error.kind is StorageErrorKind.PARTIAL_SUCCESS
    assert isinstance(error.__cause__, StorageError)
    assert error.__cause__.kind is StorageErrorKind.DATABASE_UNAVAILABLE


@pytest.mark.asyncio
async def test_shift_register_message_upsert_marks_entry_failure_partial_success() -> (
    None
):
    raw_error = StorageError(StorageErrorKind.GOOGLE_SHEETS_TRANSIENT)
    subject = ShiftRegister(fake_bot())
    manager = OrderedShiftUpsertManager(
        object(),
        "service.json",
        upsert_error=raw_error,
    )
    context = ordered_shift_upsert_context(manager)
    submission, user_info = shift_register_submission()
    message = FakeRegisterMessage(content="4-8")

    with pytest.raises(StorageError) as exc_info:
        await subject._process_configured_message_submission(
            message,
            context,
            submission,
            user_info,
        )

    error = exc_info.value
    assert manager.events == ["fetch_metadata", "log_missing", "ensure", "upsert"]
    assert error.kind is StorageErrorKind.PARTIAL_SUCCESS
    assert isinstance(error.__cause__, StorageError)
    assert error.__cause__.kind is StorageErrorKind.GOOGLE_SHEETS_TRANSIENT


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
async def test_team_settings_panel_passes_latest_guide_state_from_base_flow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(FeatureChannel, "get", fake_feature_channel_get)
    monkeypatch.setattr(
        "cogs.team_register.TeamRegisterManager",
        UnexpectedSetupManager,
    )
    monkeypatch.setattr(TeamRegister, "ManagerType", PanelManager)

    async def enabled_auto_guide_state(_feature_channel: object) -> SimpleNamespace:
        return SimpleNamespace(is_enabled=True)

    monkeypatch.setattr(
        feature_channel_base,
        "get_auto_guide_state",
        enabled_auto_guide_state,
    )
    PanelManager.last_instance = None
    panel_view = SettingsTimeoutView()
    panel = SettingsPanel(embed=Embed(title="Team Register Settings"), view=panel_view)
    calls: list[tuple[object, object, object, bool, object, bool]] = []

    async def fake_build_team_register_settings_panel(
        manager: object,
        interaction: object,
        sheet_config: object,
        **kwargs: object,
    ) -> SettingsPanel:
        latest_guide_enabled = kwargs["latest_guide_enabled"]
        latest_guide_toggle_callback = kwargs["latest_guide_toggle_callback"]
        latest_guide_state_resolver = kwargs["latest_guide_state_resolver"]
        latest_guide_current_state = await latest_guide_state_resolver()
        calls.append(
            (
                manager,
                interaction,
                sheet_config,
                latest_guide_enabled,
                latest_guide_toggle_callback,
                latest_guide_current_state,
            )
        )
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
    (
        call_manager,
        call_interaction,
        sheet_config,
        latest_guide_enabled,
        latest_guide_toggle_callback,
        latest_guide_current_state,
    ) = calls[0]
    assert call_manager is manager
    assert call_interaction is interaction
    assert sheet_config.sheet_url == "https://sheet.example"
    assert latest_guide_enabled is False
    assert latest_guide_toggle_callback is not None
    assert latest_guide_current_state is True
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
async def test_shift_settings_panel_passes_latest_guide_state_from_base_flow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(FeatureChannel, "get", fake_feature_channel_get)
    monkeypatch.setattr(
        "cogs.shift_register.ShiftRegisterManager",
        UnexpectedSetupManager,
    )
    monkeypatch.setattr(ShiftRegister, "ManagerType", PanelManager)

    async def missing_auto_guide_state(_feature_channel: object) -> None:
        return None

    monkeypatch.setattr(
        feature_channel_base,
        "get_auto_guide_state",
        missing_auto_guide_state,
    )
    PanelManager.last_instance = None
    panel_view = SettingsTimeoutView()
    panel = SettingsPanel(embed=Embed(title="Shift Register Settings"), view=panel_view)
    calls: list[tuple[object, object, bool, object, bool]] = []

    async def fake_build_shift_register_settings_panel(
        manager: object,
        sheet_config: object,
        **kwargs: object,
    ) -> SettingsPanel:
        latest_guide_enabled = kwargs["latest_guide_enabled"]
        latest_guide_toggle_callback = kwargs["latest_guide_toggle_callback"]
        latest_guide_state_resolver = kwargs["latest_guide_state_resolver"]
        latest_guide_current_state = await latest_guide_state_resolver()
        calls.append(
            (
                manager,
                sheet_config,
                latest_guide_enabled,
                latest_guide_toggle_callback,
                latest_guide_current_state,
            )
        )
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
    (
        call_manager,
        sheet_config,
        latest_guide_enabled,
        latest_guide_toggle_callback,
        latest_guide_current_state,
    ) = calls[0]
    assert call_manager is manager
    assert sheet_config.sheet_url == "https://sheet.example"
    assert latest_guide_enabled is False
    assert latest_guide_toggle_callback is not None
    assert latest_guide_current_state is False
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

    async def missing_auto_guide_state(_feature_channel: object) -> None:
        return None

    monkeypatch.setattr(
        feature_channel_base,
        "get_auto_guide_state",
        missing_auto_guide_state,
    )
    PanelManager.last_instance = None
    error = GoogleSheetsError(
        GoogleSheetsErrorKind.TRANSIENT,
        "Google Sheets is temporarily unavailable. Try again later.",
    )

    async def raise_google_sheets_error(*_: object, **__: object) -> SettingsPanel:
        raise error

    monkeypatch.setattr(
        "cogs.team_register.build_team_register_settings_panel",
        raise_google_sheets_error,
    )
    interaction = FakeInteraction()
    subject = TeamRegister(fake_bot())

    await subject.setup_after_enable(interaction)

    contents = interaction_contents(interaction)
    assert len(contents) == 1
    assert "Google Sheets is temporarily unavailable. Try again later." in contents[0]
    assert "Reference: `STG-" in contents[0]


@pytest.mark.asyncio
async def test_setup_after_enable_missing_guild_raises_shared_interaction_error() -> (
    None
):
    interaction = FakeInteraction()
    interaction.guild = None
    subject = TeamRegister(fake_bot())

    with pytest.raises(
        ValueError,
        match=re.escape(
            "Interaction guild or channel is None. Cannot set up feature settings."
        ),
    ):
        await subject.setup_after_enable(interaction)

    assert interaction.followup.messages == []
