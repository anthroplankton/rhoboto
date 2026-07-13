from __future__ import annotations

# ruff: noqa: SLF001
import logging
from contextlib import asynccontextmanager
from types import MethodType, SimpleNamespace

import pytest
from tortoise.exceptions import DBConnectionError

from bot import config
from cogs.base.feature_channel_base import FeatureChannelBase
from cogs.shift_register import ShiftRegister
from cogs.team_register import TeamRegister
from models.feature_channel import FeatureChannel
from tests.fakes import FakeInteraction
from tests.test_manager_fakes import (
    FakeTeamGridSheet,
    FakeTeamGridWorksheet,
    configure_team_transaction_manager,
)
from utils import manager_base as manager_base_module, structs_base
from utils.google_sheets import DimensionMutation, GridValueUpdate
from utils.google_sheets_errors import GoogleSheetsError, GoogleSheetsErrorKind
from utils.shift_register_manager import ShiftRegisterManager
from utils.shift_register_structs import (
    DraftWorksheetMetadata,
    EntryWorksheetMetadata,
    FinalScheduleWorksheetMetadata,
    ShiftRegisterGoogleSheetsMetadata,
)
from utils.storage_errors import StorageError, StorageErrorKind
from utils.team_register_manager import TeamRegisterManager
from utils.team_register_structs import (
    SummaryWorksheetContent,
    TeamWorksheetContent,
)

PRIVATE_DATABASE_ERROR = "private database"


def test_required_unique_header_index_returns_zero_based_index() -> None:
    assert (
        structs_base.required_unique_header_index(
            ["other", "required", "third"],
            "required",
        )
        == 1
    )


def test_worksheet_contract_error_replaces_unknown_log_hint() -> None:
    error = structs_base.WorksheetContractError(log_hint="private-header")

    assert error.log_hint == "invalid_worksheet_contract"
    assert "private-header" not in str(error)


def test_required_unique_header_index_rejects_missing_header_safely(
    caplog: pytest.LogCaptureFixture,
) -> None:
    headers = ["private-alpha", "private-beta"]

    with pytest.raises(structs_base.WorksheetContractError) as exc_info:
        structs_base.required_unique_header_index(headers, "private-required")

    assert "private" not in str(exc_info.value)
    assert "private" not in caplog.text


def test_required_unique_header_index_rejects_duplicate_header_safely(
    caplog: pytest.LogCaptureFixture,
) -> None:
    headers = ["private-required", "private-other", "private-required"]

    with pytest.raises(structs_base.WorksheetContractError) as exc_info:
        structs_base.required_unique_header_index(headers, "private-required")

    assert exc_info.value.log_hint == "required_header_duplicate"
    assert "private" not in str(exc_info.value)
    assert "private" not in caplog.text


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
        self.reaction_events: list[tuple[object, ...]] = []

    async def add_reaction(self, emoji: str) -> None:
        self.added_reactions.append(emoji)
        self.reaction_events.append(("add", emoji))

    async def remove_reaction(self, emoji: str, user: object) -> None:
        self.removed_reactions.append((emoji, user))
        self.reaction_events.append(("remove", emoji, user))


async def fake_enabled_feature_channel_get_or_none(
    *, guild_id: int, channel_id: int, feature_name: str
) -> SimpleNamespace:
    return SimpleNamespace(
        guild_id=guild_id,
        channel_id=channel_id,
        feature_name=feature_name,
        is_enabled=True,
    )


async def noop_refresh_auto_guide(*_: object) -> bool:
    return True


class DummyManager:
    def __init__(self, feature_channel: object, service_account_path: str) -> None:
        self.feature_channel = feature_channel
        self.service_account_path = service_account_path


class ConfiguredDummyManager(DummyManager):
    async def get_sheet_config_or_none(self) -> SimpleNamespace:
        return SimpleNamespace(
            sheet_url=(
                "https://docs.google.com/spreadsheets/d/register-reactions/edit"
            ),
            recruitment_time_ranges=[{"start": 4, "end": 28}],
        )

    async def get_fresh_sheet_config(self) -> SimpleNamespace:
        return await self.get_sheet_config_or_none()


class FailingConfigManager(ConfiguredDummyManager):
    async def get_sheet_config_or_none(self) -> SimpleNamespace:
        raise DBConnectionError(PRIVATE_DATABASE_ERROR)


class MissingConfigDummyManager(DummyManager):
    async def get_sheet_config_or_none(self) -> None:
        return None


def make_subject(feature_name: str) -> SimpleNamespace:
    if feature_name == "team_register":
        parse_message_submission = TeamRegister._parse_message_submission
        process_configured = TeamRegister._process_configured_message_submission
        manager_type = TeamRegister.ManagerType
        parser_type = TeamRegister.ParserType
    else:
        parse_message_submission = ShiftRegister._parse_message_submission
        process_configured = ShiftRegister._process_configured_message_submission
        manager_type = ShiftRegister.ManagerType
        parser_type = ShiftRegister.ParserType

    subject = SimpleNamespace(
        feature_name=feature_name,
        logger=FakeLogger(),
        bot=SimpleNamespace(user=object()),
        ManagerType=manager_type,
        ParserType=parser_type,
        _refresh_auto_guide_if_enabled=noop_refresh_auto_guide,
    )
    subject._parse_message_submission = MethodType(
        parse_message_submission,
        subject,
    )
    subject._process_configured_message_submission = MethodType(
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
        "_process_feature_channel_message_with_outcome",
        "_add_invalid_registration_reactions",
    ):
        method = getattr(FeatureChannelBase, method_name)
        setattr(subject, method_name, MethodType(method, subject))
    subject._get_enabled_feature_channel_or_none = (
        FeatureChannelBase._get_enabled_feature_channel_or_none
    )
    return subject


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


@pytest.mark.asyncio
async def test_team_message_invalid_attempt_adds_confused_without_sheets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        FeatureChannel,
        "get_or_none",
        fake_enabled_feature_channel_get_or_none,
    )
    monkeypatch.setattr(TeamRegister, "ManagerType", ConfiguredDummyManager)
    subject = make_subject("team_register")
    message = FakeMessage("160//600/33")

    result = await message_upsert_result(subject, message)

    assert result is None
    assert message.added_reactions == [config.WARNING_EMOJI, config.CONFUSED_EMOJI]


@pytest.mark.asyncio
async def test_team_message_strict_mixed_rejects_with_confused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        FeatureChannel,
        "get_or_none",
        fake_enabled_feature_channel_get_or_none,
    )
    monkeypatch.setattr(TeamRegister, "ManagerType", ConfiguredDummyManager)
    subject = make_subject("team_register")
    message = FakeMessage("150/740/33.4\n160//600/33")

    result = await message_upsert_result(subject, message)

    assert result is None
    assert message.added_reactions == [config.WARNING_EMOJI, config.CONFUSED_EMOJI]


@pytest.mark.asyncio
async def test_team_message_invalid_missing_config_stays_silent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        FeatureChannel,
        "get_or_none",
        fake_enabled_feature_channel_get_or_none,
    )
    monkeypatch.setattr(TeamRegister, "ManagerType", MissingConfigDummyManager)
    subject = make_subject("team_register")
    message = FakeMessage("160//600/33")

    result = await message_upsert_result(subject, message)

    assert result is None
    assert message.added_reactions == []


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

    result = await message_upsert_result(subject, message)

    assert result is None
    assert message.added_reactions == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bot_user_present",
    [True, False],
    ids=["with-bot-user", "without-bot-user"],
)
async def test_team_listener_adds_success_before_removing_processing(
    monkeypatch: pytest.MonkeyPatch,
    *,
    bot_user_present: bool,
) -> None:
    class SuccessfulTeamManager(ConfiguredDummyManager):
        async def upsert_user_registration(
            self,
            *_args: object,
            **_kwargs: object,
        ) -> None:
            pass

    @asynccontextmanager
    async def unlocked(_channel_id: int) -> object:
        yield

    monkeypatch.setattr(
        FeatureChannel,
        "get_or_none",
        fake_enabled_feature_channel_get_or_none,
    )
    monkeypatch.setattr(TeamRegister, "ManagerType", SuccessfulTeamManager)
    bot_user = object() if bot_user_present else None
    subject = make_subject("team_register")
    subject.bot = SimpleNamespace(user=bot_user)
    subject.sheet_write_lock = unlocked
    message = FakeMessage("150/740/33")

    await FeatureChannelBase.on_message(subject, message)

    expected = [("add", "✅")]
    if bot_user is not None:
        expected = [
            ("add", config.PROCESSING_EMOJI),
            *expected,
            ("remove", config.PROCESSING_EMOJI, bot_user),
        ]
    assert message.reaction_events == expected


@pytest.mark.asyncio
async def test_team_duplicate_terminal_incident_batches_once_and_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    team_worksheets = [
        FakeTeamGridWorksheet(
            worksheet_id,
            title,
            [TeamWorksheetContent.COLUMNS],
        )
        for worksheet_id, title in zip(
            [101, 102, 103],
            ["Main Team", "Encore Team", "Backup Team"],
            strict=True,
        )
    ]
    summary_worksheet = FakeTeamGridWorksheet(
        201,
        "Team Summary",
        [
            [
                *SummaryWorksheetContent.COLUMNS,
                "Main Team ISV",
                "Main Team Power",
                "Encore Team ISV",
                "Encore Team Power",
                "Backup Team ISV",
                "Backup Team Power",
                "original_message",
                "Old Team ISV",
                "Old Team Power",
                "original_message",
            ]
        ],
    )
    sheet = FakeTeamGridSheet([*team_worksheets, summary_worksheet])

    class IncidentTeamManager(TeamRegisterManager):
        def __init__(self, feature_channel: object, service_account_path: str) -> None:
            super().__init__(feature_channel, service_account_path)
            configure_team_transaction_manager(
                self,
                sheet,
                team_worksheet_ids=[101, 102, 103],
                summary_worksheet_id=201,
            )
            self.fresh_config = self._sheet_config

        async def get_fresh_sheet_config(self) -> object:
            self._sheet_config = self.fresh_config
            self._google_sheet = None
            return self._sheet_config

    @asynccontextmanager
    async def unlocked(_channel_id: int) -> object:
        yield

    monkeypatch.setattr(
        FeatureChannel,
        "get_or_none",
        fake_enabled_feature_channel_get_or_none,
    )
    monkeypatch.setattr(TeamRegister, "ManagerType", IncidentTeamManager)
    monkeypatch.setattr(
        manager_base_module,
        "GoogleSheet",
        lambda _url, _path: sheet,
    )
    subject = make_subject("team_register")
    bot_user = object()
    subject.bot = SimpleNamespace(user=bot_user)
    subject.sheet_write_lock = unlocked
    message = FakeMessage("160/800/35.7\n160/800/35.7\n160/800/100")

    await FeatureChannelBase.on_message(subject, message)

    assert len(sheet.batch_updates) == 1
    mutations = sheet.batch_updates[0]
    assert mutations[0] == DimensionMutation.delete_columns(
        201,
        start_column=11,
        count=3,
    )
    assert [
        mutation.worksheet_id
        for mutation in mutations
        if isinstance(mutation, GridValueUpdate)
    ] == [101, 102, 103, 201]
    assert message.reaction_events == [
        ("add", config.PROCESSING_EMOJI),
        ("add", "✅"),
        ("remove", config.PROCESSING_EMOJI, bot_user),
    ]


@pytest.mark.asyncio
async def test_team_contract_failure_never_logs_private_pre_operation_data(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class ContractTeamManager(ConfiguredDummyManager):
        async def upsert_user_registration(
            self,
            *_args: object,
            **_kwargs: object,
        ) -> None:
            raise structs_base.WorksheetContractError(
                log_hint="required_header_missing"
            )

    @asynccontextmanager
    async def unlocked(_channel_id: int) -> object:
        yield

    monkeypatch.setattr(
        FeatureChannel,
        "get_or_none",
        fake_enabled_feature_channel_get_or_none,
    )
    monkeypatch.setattr(TeamRegister, "ManagerType", ContractTeamManager)
    log = logging.getLogger("tests.register_reactions.team_contract_privacy")
    caplog.set_level(logging.DEBUG, logger=log.name)
    subject = make_subject("team_register")
    subject.bot = SimpleNamespace(user=object())
    subject.logger = log
    subject.sheet_write_lock = unlocked
    message = FakeMessage("151/741/33.5")
    message.author.display_name = "private-team-display"

    await FeatureChannelBase.on_message(subject, message)

    assert message.content not in caplog.text
    assert message.author.display_name not in caplog.text
    assert "741" not in caplog.text
    assert "message=123" in caplog.text
    assert "teams=1" in caplog.text
    assert "operation=message_upsert" in caplog.text


@pytest.mark.asyncio
async def test_shift_message_invalid_attempt_adds_confused_without_sheets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        FeatureChannel,
        "get_or_none",
        fake_enabled_feature_channel_get_or_none,
    )
    monkeypatch.setattr(ShiftRegister, "ManagerType", ConfiguredDummyManager)
    subject = make_subject("shift_register")
    message = FakeMessage("18:00-20:00")

    result = await message_upsert_result(subject, message)

    assert result is None
    assert message.added_reactions == [config.WARNING_EMOJI, config.CONFUSED_EMOJI]


@pytest.mark.asyncio
async def test_shift_message_strict_mixed_rejects_with_confused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        FeatureChannel,
        "get_or_none",
        fake_enabled_feature_channel_get_or_none,
    )
    monkeypatch.setattr(ShiftRegister, "ManagerType", ConfiguredDummyManager)
    subject = make_subject("shift_register")
    message = FakeMessage("4-12\n18:00-20:00")

    result = await message_upsert_result(subject, message)

    assert result is None
    assert message.added_reactions == [config.WARNING_EMOJI, config.CONFUSED_EMOJI]


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["4-12-20", "4-12 4-12-20"])
async def test_shift_message_malformed_range_rejects_before_processing(
    monkeypatch: pytest.MonkeyPatch,
    content: str,
) -> None:
    monkeypatch.setattr(
        FeatureChannel,
        "get_or_none",
        fake_enabled_feature_channel_get_or_none,
    )
    monkeypatch.setattr(ShiftRegister, "ManagerType", ConfiguredDummyManager)
    subject = make_subject("shift_register")

    async def fail_if_processed(*_: object) -> None:
        msg = "malformed Shift message reached processing"
        raise AssertionError(msg)

    subject._process_configured_message_submission = fail_if_processed
    message = FakeMessage(content)

    result = await message_upsert_result(subject, message)

    assert result is None
    assert message.added_reactions == [config.WARNING_EMOJI, config.CONFUSED_EMOJI]


@pytest.mark.asyncio
async def test_shift_message_date_only_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        FeatureChannel,
        "get_or_none",
        fake_enabled_feature_channel_get_or_none,
    )
    monkeypatch.setattr(ShiftRegister, "ManagerType", DummyManager)
    subject = make_subject("shift_register")
    message = FakeMessage("2026-8-12")

    result = await message_upsert_result(subject, message)

    assert result is None
    assert message.added_reactions == []


@pytest.mark.asyncio
async def test_shift_message_valid_range_with_date_reaches_processing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        FeatureChannel,
        "get_or_none",
        fake_enabled_feature_channel_get_or_none,
    )
    monkeypatch.setattr(ShiftRegister, "ManagerType", ConfiguredDummyManager)
    subject = make_subject("shift_register")
    processed: list[object] = []

    async def record_submission(
        _message: object,
        _context: object,
        submission: object,
        _user_info: object,
    ) -> object:
        processed.append(submission)
        return submission

    subject._process_configured_message_submission = record_submission
    message = FakeMessage("4-12 2026-8-12")

    result = await message_upsert_result(subject, message)

    assert result is processed[0]
    assert set(result) == set(range(4, 12))
    assert message.added_reactions == []


@pytest.mark.asyncio
async def test_shift_message_invalid_missing_config_stays_silent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        FeatureChannel,
        "get_or_none",
        fake_enabled_feature_channel_get_or_none,
    )
    monkeypatch.setattr(ShiftRegister, "ManagerType", MissingConfigDummyManager)
    subject = make_subject("shift_register")
    message = FakeMessage("18:00-20:00")

    result = await message_upsert_result(subject, message)

    assert result is None
    assert message.added_reactions == []


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

        async def get_fresh_sheet_config(self) -> SimpleNamespace:
            return SimpleNamespace(
                sheet_url=(
                    "https://docs.google.com/spreadsheets/d/range-rejection/edit"
                ),
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
    subject.sheet_write_lock = ShiftRegister.sheet_write_lock
    subject._write_shift_registration = MethodType(
        ShiftRegister._write_shift_registration,
        subject,
    )
    message = FakeMessage("0-30")

    result = await message_upsert_result(subject, message)

    assert result is None
    assert message.added_reactions == [config.WARNING_EMOJI, config.CONFUSED_EMOJI]


@pytest.mark.asyncio
async def test_shift_listener_marks_old_entry_header_contract_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class OldHeaderWorksheet:
        id = 1
        title = "Shift Entry"
        row_count = 100
        col_count = 36

        async def batch_get_values(self, ranges: list[str]) -> list[list[list[object]]]:
            assert ranges == ["A1:AJ2", "A3:C", "F3:AJ"]
            return [
                [
                    ["count"],
                    [
                        "username",
                        "display_name",
                        *[f"{hour}-{hour + 1}" for hour in range(4, 28)],
                        "original_message",
                    ],
                ],
                [],
                [],
            ]

    metadata = ShiftRegisterGoogleSheetsMetadata(
        "https://sheet.example",
        [
            EntryWorksheetMetadata(1, "Shift Entry", OldHeaderWorksheet()),
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

        async def get_fresh_sheet_config(self) -> SimpleNamespace:
            return SimpleNamespace(
                sheet_url=(
                    "https://docs.google.com/spreadsheets/d/old-shift-header/edit"
                ),
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
    subject.sheet_write_lock = ShiftRegister.sheet_write_lock
    write_shift_registration = ShiftRegister._write_shift_registration
    subject._write_shift_registration = MethodType(
        write_shift_registration,
        subject,
    )
    message = FakeMessage("4-8")

    await FeatureChannelBase.on_message(subject, message)

    assert message.removed_reactions == [(config.PROCESSING_EMOJI, bot_user)]
    assert message.added_reactions == [
        config.PROCESSING_EMOJI,
        "⚠️",
        "📏",
    ]


@pytest.mark.asyncio
async def test_shift_listener_marks_config_storage_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        FeatureChannel,
        "get_or_none",
        fake_enabled_feature_channel_get_or_none,
    )
    monkeypatch.setattr(ShiftRegister, "ManagerType", FailingConfigManager)
    bot_user = object()
    subject = make_subject("shift_register")
    subject.bot = SimpleNamespace(user=bot_user)
    message = FakeMessage("4-8")

    await FeatureChannelBase.on_message(subject, message)

    assert message.added_reactions == [config.WARNING_EMOJI, "🛠️"]


@pytest.mark.asyncio
async def test_shift_listener_lookup_storage_failure_stays_silent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def failing_feature_channel_get_or_none(
        *, guild_id: int, channel_id: int, feature_name: str
    ) -> SimpleNamespace:
        _ = (guild_id, channel_id, feature_name)
        raise DBConnectionError(PRIVATE_DATABASE_ERROR)

    monkeypatch.setattr(
        FeatureChannel,
        "get_or_none",
        failing_feature_channel_get_or_none,
    )
    subject = make_subject("shift_register")
    message = FakeMessage("ordinary chat")

    await FeatureChannelBase.on_message(subject, message)

    assert message.added_reactions == []
    assert message.removed_reactions == []


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

    result = await message_upsert_result(subject, message)

    assert result is None
    assert message.added_reactions == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bot_user_present",
    [True, False],
    ids=["with-bot-user", "without-bot-user"],
)
async def test_shift_listener_adds_success_before_removing_processing(
    monkeypatch: pytest.MonkeyPatch,
    *,
    bot_user_present: bool,
) -> None:
    class SuccessfulShiftManager(ConfiguredDummyManager):
        async def fetch_google_sheets_metadata(self) -> object:
            return SimpleNamespace(worksheets=[SimpleNamespace(id=1)])

        def log_missing_worksheet_warnings(self, _metadata: object) -> None:
            pass

        async def ensure_worksheets_and_upsert_sheet_config(
            self,
            metadata: object,
        ) -> object:
            return metadata

        async def upsert_or_delete_user_shift(
            self,
            *_args: object,
            **_kwargs: object,
        ) -> None:
            pass

    @asynccontextmanager
    async def unlocked(_channel_id: int) -> object:
        yield

    monkeypatch.setattr(
        FeatureChannel,
        "get_or_none",
        fake_enabled_feature_channel_get_or_none,
    )
    monkeypatch.setattr(ShiftRegister, "ManagerType", SuccessfulShiftManager)
    bot_user = object() if bot_user_present else None
    subject = make_subject("shift_register")
    subject.bot = SimpleNamespace(user=bot_user)
    subject.sheet_write_lock = unlocked
    subject._write_shift_registration = MethodType(
        ShiftRegister._write_shift_registration,
        subject,
    )
    message = FakeMessage("4-8")

    await FeatureChannelBase.on_message(subject, message)

    expected = [("add", "✅")]
    if bot_user is not None:
        expected = [
            ("add", config.PROCESSING_EMOJI),
            *expected,
            ("remove", config.PROCESSING_EMOJI, bot_user),
        ]
    assert message.reaction_events == expected


@pytest.mark.asyncio
async def test_shift_contract_failure_never_logs_private_pre_operation_data(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class ContractShiftManager(ConfiguredDummyManager):
        async def fetch_google_sheets_metadata(self) -> None:
            raise structs_base.WorksheetContractError(
                log_hint="required_header_duplicate"
            )

    @asynccontextmanager
    async def unlocked(_channel_id: int) -> object:
        yield

    monkeypatch.setattr(
        FeatureChannel,
        "get_or_none",
        fake_enabled_feature_channel_get_or_none,
    )
    monkeypatch.setattr(ShiftRegister, "ManagerType", ContractShiftManager)
    log = logging.getLogger("tests.register_reactions.shift_contract_privacy")
    caplog.set_level(logging.DEBUG, logger=log.name)
    subject = make_subject("shift_register")
    subject.bot = SimpleNamespace(user=object())
    subject.logger = log
    subject.sheet_write_lock = unlocked
    subject._write_shift_registration = MethodType(
        ShiftRegister._write_shift_registration,
        subject,
    )
    message = FakeMessage("4-8")
    message.author.display_name = "private-shift-display"

    await FeatureChannelBase.on_message(subject, message)

    assert message.content not in caplog.text
    assert message.author.display_name not in caplog.text
    assert "ranges=4-8" not in caplog.text
    assert "message=123" in caplog.text
    assert "slots=4" in caplog.text
    assert "operation=message_upsert" in caplog.text


@asynccontextmanager
async def unlocked_sheet_write(_channel_id: int) -> object:
    yield


async def fake_feature_channel_get(
    *, guild_id: int, channel_id: int, feature_name: str
) -> SimpleNamespace:
    return SimpleNamespace(
        guild_id=guild_id,
        channel_id=channel_id,
        feature_name=feature_name,
        is_enabled=True,
    )


def make_team_summary_subject(manager_type: type[object]) -> SimpleNamespace:
    subject = SimpleNamespace(
        feature_name="team_register",
        feature_display_name="Team Register",
        logger=FakeLogger(),
        ManagerType=manager_type,
        sheet_write_lock=unlocked_sheet_write,
    )
    for method_name in (
        "_get_feature_channel_context",
        "_build_feature_channel_context",
        "_get_configured_feature_channel_context",
        "_send_missing_config_followup",
        "_interaction_storage_context",
        "_send_interaction_storage_error_or_raise",
    ):
        method = getattr(FeatureChannelBase, method_name)
        setattr(subject, method_name, MethodType(method, subject))
    return subject


@pytest.mark.asyncio
async def test_team_message_prewrite_permission_failure_keeps_access_kind(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class PermissionFailureTeamManager(ConfiguredDummyManager):
        async def upsert_user_registration(
            self,
            *_args: object,
            **_kwargs: object,
        ) -> None:
            raise GoogleSheetsError(
                GoogleSheetsErrorKind.PERMISSION,
                "private permission detail",
            )

    monkeypatch.setattr(
        FeatureChannel,
        "get_or_none",
        fake_enabled_feature_channel_get_or_none,
    )
    monkeypatch.setattr(TeamRegister, "ManagerType", PermissionFailureTeamManager)
    log = logging.getLogger("tests.register_reactions.team_permission_stage")
    caplog.set_level(logging.WARNING, logger=log.name)
    subject = make_subject("team_register")
    subject.logger = log
    subject.sheet_write_lock = unlocked_sheet_write
    message = FakeMessage("150/740/33")

    await FeatureChannelBase.on_message(subject, message)

    assert "kind=google_sheets_access" in caplog.text
    assert "kind=partial_success" not in caplog.text
    assert message.added_reactions == [
        config.PROCESSING_EMOJI,
        config.WARNING_EMOJI,
        "🛠️",
    ]


@pytest.mark.asyncio
async def test_team_message_preserves_explicit_partial_success_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager_error = StorageError(StorageErrorKind.PARTIAL_SUCCESS)

    class PartialFailureTeamManager(ConfiguredDummyManager):
        async def upsert_user_registration(
            self,
            *_args: object,
            **_kwargs: object,
        ) -> None:
            raise manager_error

    monkeypatch.setattr(
        FeatureChannel,
        "get_or_none",
        fake_enabled_feature_channel_get_or_none,
    )
    monkeypatch.setattr(TeamRegister, "ManagerType", PartialFailureTeamManager)
    subject = make_subject("team_register")
    subject.sheet_write_lock = unlocked_sheet_write
    message = FakeMessage("150/740/33")

    with pytest.raises(StorageError) as exc_info:
        await message_upsert_result(subject, message)

    assert exc_info.value is manager_error


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("manager_error", "expected_copy"),
    [
        pytest.param(
            GoogleSheetsError(
                GoogleSheetsErrorKind.TRANSIENT,
                "private transient detail",
            ),
            "Google Sheets is temporarily unavailable. Try again later.",
            id="transient-before-write",
        ),
        pytest.param(
            StorageError(StorageErrorKind.PARTIAL_SUCCESS),
            "Some changes may have been saved, but this action could not be completed.",
            id="explicit-partial-success",
        ),
    ],
)
async def test_team_summary_preserves_manager_storage_copy(
    monkeypatch: pytest.MonkeyPatch,
    manager_error: Exception,
    expected_copy: str,
) -> None:
    class SummaryFailureTeamManager(ConfiguredDummyManager):
        async def refresh_summary_registration(
            self,
            *,
            member_by_names: dict[str, object],
        ) -> object:
            del member_by_names
            raise manager_error

    monkeypatch.setattr(FeatureChannel, "get", fake_feature_channel_get)
    interaction = FakeInteraction()
    subject = make_team_summary_subject(SummaryFailureTeamManager)

    await TeamRegister.summary.callback(subject, interaction)

    assert interaction.response.deferred == [True]
    assert len(interaction.followup.messages) == 1
    content, kwargs = interaction.followup.messages[0]
    assert content is not None
    assert content.startswith(expected_copy)
    assert "Reference: `STG-" in content
    assert kwargs == {"ephemeral": True}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        pytest.param(
            (
                "fetch",
                GoogleSheetsError(
                    GoogleSheetsErrorKind.PERMISSION,
                    "private fetch permission detail",
                ),
                StorageErrorKind.GOOGLE_SHEETS_ACCESS,
                ["fetch"],
            ),
            id="metadata-fetch-permission",
        ),
        pytest.param(
            (
                "ensure",
                GoogleSheetsError(
                    GoogleSheetsErrorKind.TRANSIENT,
                    "private ensure transient detail",
                ),
                StorageErrorKind.GOOGLE_SHEETS_TRANSIENT,
                ["fetch", "log", "ensure"],
            ),
            id="worksheet-ensure-transient",
        ),
        pytest.param(
            (
                "ensure",
                StorageError(StorageErrorKind.PARTIAL_SUCCESS),
                StorageErrorKind.PARTIAL_SUCCESS,
                ["fetch", "log", "ensure"],
            ),
            id="worksheet-ensure-proven-partial",
        ),
    ],
)
async def test_shift_ensure_failure_preserves_manager_storage_kind(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    case: tuple[str, Exception, StorageErrorKind, list[str]],
) -> None:
    failure_stage, manager_error, expected_kind, expected_events = case
    events: list[str] = []

    class StagedFailureShiftManager(ConfiguredDummyManager):
        async def fetch_google_sheets_metadata(self) -> object:
            events.append("fetch")
            if failure_stage == "fetch":
                raise manager_error
            return SimpleNamespace(worksheets=[SimpleNamespace(id=1)])

        def log_missing_worksheet_warnings(self, _metadata: object) -> None:
            events.append("log")

        async def ensure_worksheets_and_upsert_sheet_config(
            self,
            metadata: object,
        ) -> object:
            events.append("ensure")
            if failure_stage == "ensure":
                raise manager_error
            return metadata

        async def upsert_or_delete_user_shift(
            self,
            *_args: object,
            **_kwargs: object,
        ) -> None:
            events.append("upsert")

    monkeypatch.setattr(
        FeatureChannel,
        "get_or_none",
        fake_enabled_feature_channel_get_or_none,
    )
    monkeypatch.setattr(ShiftRegister, "ManagerType", StagedFailureShiftManager)
    log = logging.getLogger(f"tests.register_reactions.shift_{failure_stage}_stage")
    caplog.set_level(logging.WARNING, logger=log.name)
    subject = make_subject("shift_register")
    subject.logger = log
    subject.sheet_write_lock = unlocked_sheet_write
    subject._write_shift_registration = MethodType(
        ShiftRegister._write_shift_registration,
        subject,
    )
    message = FakeMessage("4-8")

    await FeatureChannelBase.on_message(subject, message)

    assert events == expected_events
    assert f"kind={expected_kind.value}" in caplog.text
    if expected_kind is not StorageErrorKind.PARTIAL_SUCCESS:
        assert "kind=partial_success" not in caplog.text
    assert message.added_reactions == [
        config.PROCESSING_EMOJI,
        config.WARNING_EMOJI,
        "🛠️",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("ids_changed", [False, True])
async def test_shift_post_ensure_upsert_failure_uses_actual_id_change(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    *,
    ids_changed: bool,
) -> None:
    events: list[str] = []
    original_metadata = SimpleNamespace(worksheets=[SimpleNamespace(id=1)])
    ensured_metadata = SimpleNamespace(
        worksheets=[SimpleNamespace(id=2 if ids_changed else 1)]
    )

    class PostEnsureFailureShiftManager(ConfiguredDummyManager):
        async def fetch_google_sheets_metadata(self) -> object:
            events.append("fetch")
            return original_metadata

        def log_missing_worksheet_warnings(self, _metadata: object) -> None:
            events.append("log")

        async def ensure_worksheets_and_upsert_sheet_config(
            self,
            metadata: object,
        ) -> object:
            events.append("ensure")
            assert metadata is original_metadata
            return ensured_metadata

        async def upsert_or_delete_user_shift(
            self,
            *_args: object,
            **_kwargs: object,
        ) -> None:
            events.append("upsert")
            raise GoogleSheetsError(
                GoogleSheetsErrorKind.PERMISSION,
                "private post-ensure permission detail",
            )

    monkeypatch.setattr(
        FeatureChannel,
        "get_or_none",
        fake_enabled_feature_channel_get_or_none,
    )
    monkeypatch.setattr(ShiftRegister, "ManagerType", PostEnsureFailureShiftManager)
    log = logging.getLogger("tests.register_reactions.shift_post_ensure_stage")
    caplog.set_level(logging.WARNING, logger=log.name)
    subject = make_subject("shift_register")
    subject.logger = log
    subject.sheet_write_lock = unlocked_sheet_write
    subject._write_shift_registration = MethodType(
        ShiftRegister._write_shift_registration,
        subject,
    )
    message = FakeMessage("4-8")

    await FeatureChannelBase.on_message(subject, message)

    assert events == [
        "fetch",
        "log",
        "ensure",
        "upsert",
    ]
    expected_kind = (
        StorageErrorKind.PARTIAL_SUCCESS
        if ids_changed
        else StorageErrorKind.GOOGLE_SHEETS_ACCESS
    )
    assert f"kind={expected_kind.value}" in caplog.text
    if not ids_changed:
        assert "kind=partial_success" not in caplog.text
    assert message.added_reactions == [
        config.PROCESSING_EMOJI,
        config.WARNING_EMOJI,
        "🛠️",
    ]
