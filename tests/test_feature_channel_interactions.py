from __future__ import annotations

# ruff: noqa: RUF001, SLF001
import asyncio
import datetime as dt
import logging
import re
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import ClassVar, override

import pytest
from discord import (
    ButtonStyle,
    Embed,
    File,
    HTTPException,
    Interaction,
    Message,
    NotFound,
    Role,
    app_commands,
)
from discord.ext import commands
from tortoise.exceptions import DBConnectionError

from bot import config
from cogs.base import (
    feature_channel_base,
    register_feature_channel_base,
    register_feature_channel_user_base,
)
from cogs.base.discord_context import (
    GuildChannelSource,
    require_guild_channel_source,
)
from cogs.base.feature_channel_base import (
    FeatureChannelBase,
    FeatureNotEnabled,
    StorageCheckFailure,
)
from cogs.base.message_upsert_feature_channel_base import (
    MessageParseResult,
    MessageUpsertFeatureChannelBase,
    MessageUpsertOutcome,
)
from cogs.base.register_feature_channel_base import RegisterFeatureChannelBase
from cogs.base.register_feature_channel_context import (
    ConfiguredRegisterFeatureChannelContext,
    RegisterFeatureChannelContext,
    RegisterFeatureChannelContextMixin,
)
from cogs.base.register_feature_channel_user_base import (
    RegisterFeatureChannelUserBase,
)
from cogs.shift import Shift
from cogs.shift_register import (
    _SHIFT_REPORT_SECTION_PREFIXES,
    ShiftRegister,
    ShiftReportAssignment,
    _format_generate_draft_confirmation,
    _format_shift_assignment_section,
    _split_shift_report,
)
from cogs.team import Team
from cogs.team_register import TeamRegister
from components import ui_shift_register, ui_team_register
from components.ui_auto_guide import LATEST_GUIDE_ENABLE_REFRESH_FAILED_WARNING
from components.ui_settings_flow import SettingsPanel, SettingsTimeoutView
from components.ui_shift_register import ShiftDeadlineCloseView, ShiftRegisterSheetModal
from components.ui_team_register import TeamRegisterSheetModal
from models.feature_channel import FeatureChannel
from models.feature_channel_message_state import (
    FeatureChannelMessageKind,
    FeatureChannelMessageState,
)
from models.shift_register import ShiftRegisterConfig
from models.shift_timeline_event_state import (
    ShiftTimelineEventKind,
    ShiftTimelineEventStatus,
)
from models.team_register import TeamRegisterConfig
from tests.fakes import (
    ConfiguredManager,
    FakeContext,
    FakeDiscordFollowup,
    FakeInteraction,
    MissingConfigManager,
)
from utils.announcement_languages import RenderedAnnouncement
from utils.google_sheets_errors import GoogleSheetsError, GoogleSheetsErrorKind
from utils.manager_base import ManagerBase
from utils.shift_register_manager import (
    SHIFT_REGISTER_SHEET_WRITE_LOCK,
    TEAM_SOURCE_UNAVAILABLE_DRAFT_WARNING,
    TEAM_SOURCE_UNSET_DRAFT_WARNING,
    DraftGenerationResult,
    ShiftDeadlineExecution,
    ShiftRegisterManager,
    ShiftTimelineScheduleChange,
    TeamSourceStatus,
)
from utils.shift_register_structs import (
    DraftWorksheetMetadata,
    EntryWorksheetMetadata,
    FinalScheduleWorksheetMetadata,
    RecruitmentTimeRanges,
    Shift as RegisterShift,
    ShiftParser,
    ShiftRegisterGoogleSheetsMetadata,
)
from utils.shift_scheduler import DraftSchedule, HourShiftAssignment
from utils.storage_errors import StorageError, StorageErrorKind
from utils.structs_base import (
    UserInfo,
    WorksheetContractError,
    WorksheetMetadata,
    required_unique_header_index,
)
from utils.team_register_manager import TeamRegisterManager
from utils.team_register_structs import (
    Team as RegisterTeam,
    TeamParser,
    TeamRegisterGoogleSheetsMetadata,
)

PRIVATE_DATABASE_ERROR = "private database"


def test_generate_draft_requires_non_negative_power_threshold() -> None:
    parameters = {
        parameter.name: parameter
        for parameter in ShiftRegister.generate_draft.parameters
    }

    threshold = parameters["encore_power_threshold"]
    assert threshold.required is True
    assert threshold.min_value == 0
    assert parameters["runner"].required is False
    assert parameters["runner"].type.value == 6


def test_split_shift_report_uses_semantic_boundaries_with_unicode_limit() -> None:
    report = "\n".join(
        [
            "draft generated",
            _SHIFT_REPORT_SECTION_PREFIXES[0]
            + "、".join(f"😀user{index}" for index in range(8)),
            _SHIFT_REPORT_SECTION_PREFIXES[1] + "assigned",
            "hour 4: 😀alice, 😀bob, 😀carol",
            _SHIFT_REPORT_SECTION_PREFIXES[2] + "unassigned",
            "hour 4: 😀dave, 😀eve",
            "notes attached",
        ]
    )

    messages = _split_shift_report(report, limit=80)

    assert len(messages) > 1
    assert all(len(message.encode("utf-16-le")) // 2 <= 80 for message in messages)
    assert any(
        message.startswith(_SHIFT_REPORT_SECTION_PREFIXES[0]) for message in messages
    )
    assert any(
        message.startswith(_SHIFT_REPORT_SECTION_PREFIXES[1]) for message in messages
    )
    assert any(
        message.startswith(_SHIFT_REPORT_SECTION_PREFIXES[2]) for message in messages
    )
    assert "".join(messages).replace("\n", "") == report.replace("\n", "")


def test_split_shift_report_keeps_fitting_preamble_before_assignments() -> None:
    report = "\n".join(
        [
            "### ✅ 班表草稿已產生",
            "⚠️ 編成未登録：`Alice`",
            "- 募集時間【4-12】",
            "- 已排入（安可｜本走；待機）：",
            *[f"  - row {index} " + "x" * 40 for index in range(30)],
        ]
    )

    messages = _split_shift_report(report, limit=200)

    assert messages[0] == "\n".join(report.splitlines()[:3])
    assert messages[1].startswith("- 已排入（安可｜本走；待機）：")
    assert all(len(message.encode("utf-16-le")) // 2 <= 200 for message in messages)


def test_format_shift_assignment_section_uses_shared_draft_grammar() -> None:
    assert _format_shift_assignment_section(
        [ShiftReportAssignment(15, "`A`", ("`B`", "`C`"), None)],
        empty=False,
    ) == [
        "- 已排入（安可｜本走；待機）：",
        "  - -# `15-16`：`A`｜`B`、`C`、缺 `1`；缺",
    ]
    assert _format_shift_assignment_section([], empty=True) == [
        "- 已排入（安可｜本走；待機）：なし"
    ]


def test_generate_draft_confirmation_formats_new_destinations() -> None:
    ranges = RecruitmentTimeRanges.from_json(
        [{"start": 4, "end": 12}, {"start": 20, "end": 28}]
    )

    content = _format_generate_draft_confirmation(
        ranges,
        "https://docs.google.com/spreadsheets/d/abc/edit#gid=222",
        TeamSourceStatus.AVAILABLE,
        "https://docs.google.com/spreadsheets/d/team/edit#gid=333",
    )

    assert (
        "[Shift Draft](https://docs.google.com/spreadsheets/d/abc/edit#gid=222)"
        in content
    )
    assert content.startswith(
        "### ‼️ 確認產生班表草稿\n"
        "請先備份需要保留的內容。確認後將覆蓋 "
        "[Shift Draft](https://docs.google.com/spreadsheets/d/abc/edit#gid=222)"
        " 的以下位置："
    )
    assert (
        content.count(
            "[Shift Draft](https://docs.google.com/spreadsheets/d/abc/edit#gid=222)"
        )
        == 1
    )
    assert "`A1:G31`" in content
    assert "`A27`" in content
    assert "`I1`" in content
    assert "候補：`I1`、閾值・圖例 `I26:M26`" in content
    assert "`J28:L30`" in content
    assert "`J31`" in content
    assert (
        "Team Source 同步：\n"
        "- 確認後會以目前 Discord 成員與 Team 資料更新 "
        "[Team Summary](https://docs.google.com/spreadsheets/d/team/edit#gid=333)"
        in content
    )
    assert "#REF!" in content


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (
            TeamSourceStatus.UNSET,
            "Team Source 同步：\n⚠️ 未設定，本次不會同步",
        ),
        (
            TeamSourceStatus.INVALID,
            "Team Source 同步：\n⚠️ 設定無效，本次不會同步",
        ),
    ],
)
def test_generate_draft_confirmation_formats_missing_team_source(
    status: TeamSourceStatus,
    expected: str,
) -> None:
    content = _format_generate_draft_confirmation(
        RecruitmentTimeRanges.default(),
        "https://docs.google.com/spreadsheets/d/abc/edit#gid=222",
        status,
        None,
    )

    assert expected in content
    assert "[Team Summary]" not in content


def test_format_shift_draft_report_lists_each_hour_with_code_numbers() -> None:
    schedule = DraftSchedule(
        runner=None,
        hours=[4, 5, 6, 7, 8, 9, 10, 11],
        assignments=[
            HourShiftAssignment(
                hour=4,
                supporter_usernames_by_slot={"encore": "alice"},
                unassigned_usernames=["carol", "dave"],
            ),
            HourShiftAssignment(
                hour=5,
                supporter_usernames_by_slot={"honso_1": "bob", "encore": "alice"},
            ),
            HourShiftAssignment(
                hour=6,
                supporter_usernames_by_slot={
                    "encore": "alice",
                    "honso_1": "bob",
                    "honso_2": "eve",
                    "honso_3": "frank",
                    "standby": "grace",
                },
            ),
            HourShiftAssignment(
                hour=7,
                supporter_usernames_by_slot={"honso_1": "bob"},
            ),
            HourShiftAssignment(hour=8),
            HourShiftAssignment(
                hour=9,
                supporter_usernames_by_slot={"encore": "alice", "standby": "grace"},
            ),
            HourShiftAssignment(
                hour=10,
                supporter_usernames_by_slot={"standby": "grace", "honso_1": "bob"},
            ),
            HourShiftAssignment(
                hour=11,
                supporter_usernames_by_slot={"standby": "grace"},
            ),
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

    report = ShiftRegister._format_draft_report(
        schedule,
        "https://docs.google.com/spreadsheets/d/abc/edit#gid=222",
        {"alice": "<@111>", "bob": "<@222>", "carol": "<@333>"},
        encore_power_threshold=35,
        recruitment_ranges=RecruitmentTimeRanges.from_json(
            [{"start": 4, "end": 8}, {"start": 9, "end": 12}]
        ),
        team_summary_url="https://docs.google.com/spreadsheets/d/team/edit#gid=333",
        team_source_warning=None,
        unregistered_usernames=("carol", "eve"),
    )

    assert report == (
        "### ✅ 班表草稿已產生\n"
        "- Runner（ランナー）：`Not set`\n"
        "- 安可綜合力閾值：35\n"
        "🔄 已同步 "
        "[Team Summary](https://docs.google.com/spreadsheets/d/team/edit#gid=333)\n"
        "‼️ 已將班表寫入 "
        "[Shift Draft](https://docs.google.com/spreadsheets/d/abc/edit#gid=222)"
        "，並覆蓋原有內容。\n"
        "⚠️ 編成未登録：<@333>、E\\`ve\n"
        "- 募集時間【4-8・9-12】\n"
        "- 已排入（安可｜本走；待機）：\n"
        "  - -# `4-5`：<@111>｜缺 `3`；缺\n"
        "  - -# `5-6`：<@111>｜<@222>、缺 `2`；缺\n"
        "  - -# `6-7`：<@111>｜<@222>、E\\`ve、`Frank`；`Grace`\n"
        "  - -# `7-8`：缺｜<@222>、缺 `2`；缺\n"
        "  - -# `9-10`：<@111>｜缺 `3`；`Grace`\n"
        "  - -# `10-11`：缺｜<@222>、缺 `2`；`Grace`\n"
        "  - -# `11-12`：缺｜缺 `3`；`Grace`\n"
        "- 未排入（位置已滿）：\n"
        "  - -# `4-5`：<@333>、`Dave`\n"
        "附件是生成時資料的 Notes 快照，不會隨 Sheet 調整更新。"
    )
    assert "`8-9`" not in report
    assert "`7-8`" in report
    assert "`9-10`" in report
    assert "`8` 個小時" not in report


def test_format_shift_draft_report_compacts_zero_entry_initialization() -> None:
    report = ShiftRegister._format_draft_report(
        DraftSchedule(
            None,
            [4, 5],
            [HourShiftAssignment(4), HourShiftAssignment(5)],
            {},
        ),
        "https://docs.google.com/spreadsheets/d/abc/edit#gid=222",
        {},
        encore_power_threshold=35,
        recruitment_ranges=RecruitmentTimeRanges.from_json([{"start": 4, "end": 6}]),
        team_summary_url=None,
        team_source_warning=None,
    )

    assert "- 已排入（安可｜本走；待機）：なし" in report
    assert "`4-5`" not in report
    assert "`5-6`" not in report
    assert "募集時間【4-6】" in report
    assert "附件是生成時資料的 Notes 快照" in report


@pytest.mark.parametrize(
    "warning",
    [TEAM_SOURCE_UNSET_DRAFT_WARNING, TEAM_SOURCE_UNAVAILABLE_DRAFT_WARNING],
)
def test_format_shift_draft_report_places_team_warning_before_assignments(
    warning: str,
) -> None:
    report = ShiftRegister._format_draft_report(
        DraftSchedule(None, [], [], {}),
        "https://docs.google.com/spreadsheets/d/abc/edit#gid=222",
        {},
        encore_power_threshold=35,
        recruitment_ranges=RecruitmentTimeRanges.default(),
        team_summary_url=None,
        team_source_warning=warning,
    )

    assert report.index(warning) < report.index("募集時間") < report.index("已排入")
    assert report.index("已排入") == report.index("募集時間") + len(
        f"募集時間【{RecruitmentTimeRanges.default().announcement_display()}】\n- "
    )
    assert report.index("已排入") < report.index("附件是生成時資料")
    assert "[Team Summary]" not in report


@pytest.mark.asyncio
async def test_generate_shift_draft_links_to_draft_worksheet_id(  # noqa: C901
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    schedule = DraftSchedule(
        None,
        [4],
        [HourShiftAssignment(4, unassigned_usernames=["carol"])],
        {"carol": "Carol"},
    )
    metadata = SimpleNamespace(
        sheet_url="https://docs.google.com/spreadsheets/d/abc/edit",
        draft_worksheet=DraftWorksheetMetadata(222, "Shift Draft", None),
        worksheets=[
            SimpleNamespace(id=1),
            SimpleNamespace(id=222),
            SimpleNamespace(id=3),
        ],
    )
    ranges = RecruitmentTimeRanges.from_json([{"start": 4, "end": 5}])
    notes_snapshot = "メモ\n募集時間【4-5】\nAlice：シフト合計 1h／original message"

    class Manager:
        async def get_saved_team_summary_destination(
            self,
        ) -> tuple[TeamSourceStatus, str]:
            events.append("destination")
            return (
                TeamSourceStatus.AVAILABLE,
                "https://docs.google.com/spreadsheets/d/team/edit?gid=333#gid=333",
            )

        async def get_fresh_sheet_config(self) -> SimpleNamespace:
            events.append("fresh")
            return config

        async def fetch_google_sheets_metadata(self) -> SimpleNamespace:
            events.append("sheet")
            return metadata

        async def get_sheet_config(self) -> SimpleNamespace:
            return config

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
            member_by_names: dict[str, object],
            encore_power_threshold: float,
            runner: UserInfo | None,
        ) -> DraftGenerationResult:
            assert list(member_by_names) == ["carol"]
            assert encore_power_threshold == 35
            assert runner is None
            return DraftGenerationResult(
                schedule=schedule,
                team_source_status=TeamSourceStatus.AVAILABLE,
                team_source_warning=None,
                recruitment_ranges=ranges,
                notes_snapshot=notes_snapshot,
                unregistered_usernames=(
                    "carol",
                    *(f"user{index}" for index in range(300)),
                ),
                team_summary_url=(
                    "https://docs.google.com/spreadsheets/d/team/edit?gid=333#gid=333"
                ),
            )

    async def get_feature_channel_context(_source: object) -> object:
        return object()

    config = SimpleNamespace(
        recruitment_time_ranges=[{"start": 4, "end": 5}],
        sheet_url=metadata.sheet_url,
        draft_worksheet_id=222,
    )

    async def get_configured_context(_context: object) -> SimpleNamespace:
        return SimpleNamespace(manager=Manager(), feature_config=config)

    class ConfirmView:
        value = True

        def __init__(
            self,
            *,
            requesting_user_id: int,
            destination_label: str,
            destination_url: str,
        ) -> None:
            assert requesting_user_id == 333
            assert destination_label == "Shift Draft"
            assert destination_url.endswith("#gid=222")

        async def wait(self) -> None:
            events.append("wait")

    @asynccontextmanager
    async def recording_lock(_channel_id: int) -> object:
        events.append("channel")
        yield

    subject = ShiftRegister(fake_bot())
    subject._get_register_feature_channel_context = get_feature_channel_context
    subject._get_configured_register_feature_channel_context = get_configured_context
    subject.sheet_write_lock = recording_lock
    monkeypatch.setattr(
        "cogs.shift_register.GenerateShiftScheduleConfirmView", ConfirmView
    )
    interaction = FakeInteraction(
        guild=SimpleNamespace(
            id=111,
            members=[SimpleNamespace(name="carol", mention="<@333>")],
        )
    )

    await ShiftRegister.generate_draft.callback(subject, interaction, 35)

    assert events[:6] == [
        "destination",
        "wait",
        "channel",
        "fresh",
        "destination",
        "sheet",
    ]
    prompt, prompt_kwargs = interaction.original_response_edits[0]
    assert "`A1:G31`" in prompt
    assert isinstance(prompt_kwargs["view"], ConfirmView)
    assert len(interaction.followup.messages) > 1
    assert all(
        len((content or "").encode("utf-16-le")) // 2 <= 2000
        for content, _kwargs in interaction.followup.messages
    )
    content = "\n".join(
        content or "" for content, _kwargs in interaction.followup.messages
    )
    assert (
        "[Shift Draft](https://docs.google.com/spreadsheets/d/abc/edit?gid=222#gid=222)"
        in (content or "")
    )
    assert "<@333>" in (content or "")
    assert "募集時間【4-5】" in (content or "")
    assert (
        "[Team Summary](https://docs.google.com/spreadsheets/d/team/edit?gid=333#gid=333)"
        in (content or "")
    )
    assert "⚠️ 編成未登録：<@333>" in (content or "")
    kwargs = interaction.followup.messages[0][1]
    attachment = kwargs["file"]
    assert isinstance(attachment, File)
    assert attachment.filename == "shift-draft-notes.txt"
    attachment.fp.seek(0)
    assert attachment.fp.read().decode("utf-8") == notes_snapshot
    assert (
        sum("file" in kwargs for _content, kwargs in interaction.followup.messages) == 1
    )
    assert all(
        kwargs["ephemeral"] is True
        for _content, kwargs in interaction.followup.messages
    )


@pytest.mark.asyncio
async def test_generate_shift_draft_reports_contract_error_without_storage_alias(  # noqa: C901
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata = SimpleNamespace(
        sheet_url="https://docs.google.com/spreadsheets/d/abc/edit",
        draft_worksheet=DraftWorksheetMetadata(222, "Shift Draft", None),
        worksheets=[
            SimpleNamespace(id=1),
            SimpleNamespace(id=222),
            SimpleNamespace(id=3),
        ],
    )

    class Manager:
        async def get_saved_team_summary_destination(
            self,
        ) -> tuple[TeamSourceStatus, None]:
            return TeamSourceStatus.UNSET, None

        async def get_fresh_sheet_config(self) -> SimpleNamespace:
            return config

        async def fetch_google_sheets_metadata(self) -> SimpleNamespace:
            return metadata

        def log_missing_worksheet_warnings(self, _metadata: object) -> None:
            pass

        async def ensure_worksheets_and_upsert_sheet_config(
            self,
            _metadata: object,
        ) -> SimpleNamespace:
            return metadata

        async def generate_draft(self, *_args: object, **_kwargs: object) -> None:
            raise WorksheetContractError(log_hint="required_header_missing")

    config = SimpleNamespace(
        recruitment_time_ranges=[{"start": 4, "end": 5}],
        sheet_url=metadata.sheet_url,
        draft_worksheet_id=222,
    )

    async def get_feature_channel_context(_source: object) -> object:
        return object()

    async def get_configured_context(_context: object) -> SimpleNamespace:
        return SimpleNamespace(manager=Manager(), feature_config=config)

    class ConfirmView:
        value = True

        def __init__(
            self,
            *,
            requesting_user_id: int,
            destination_label: str,
            destination_url: str,
        ) -> None:
            assert requesting_user_id == 333
            assert destination_label == "Shift Draft"
            assert destination_url.endswith("#gid=222")

        async def wait(self) -> None:
            pass

    @asynccontextmanager
    async def unlocked(_channel_id: int) -> object:
        yield

    subject = ShiftRegister(fake_bot())
    subject._get_register_feature_channel_context = get_feature_channel_context
    subject._get_configured_register_feature_channel_context = get_configured_context
    subject.sheet_write_lock = unlocked
    monkeypatch.setattr(
        "cogs.shift_register.GenerateShiftScheduleConfirmView", ConfirmView
    )
    interaction = FakeInteraction()

    await ShiftRegister.generate_draft.callback(subject, interaction, 35)

    assert len(interaction.followup.messages) == 1
    content, kwargs = interaction.followup.messages[0]
    assert content is not None
    assert_worksheet_contract_content(content)
    assert "STG-" not in content
    assert "Some changes may have been saved" not in content
    assert kwargs == {"ephemeral": True}


@pytest.mark.asyncio
@pytest.mark.parametrize("ids_changed", [False, True])
async def test_generate_shift_draft_failure_uses_actual_id_change(  # noqa: C901
    monkeypatch: pytest.MonkeyPatch,
    *,
    ids_changed: bool,
) -> None:
    metadata = SimpleNamespace(
        sheet_url="https://docs.google.com/spreadsheets/d/abc/edit",
        draft_worksheet=DraftWorksheetMetadata(222, "Shift Draft", None),
        worksheets=[
            SimpleNamespace(id=1),
            SimpleNamespace(id=222),
            SimpleNamespace(id=3),
        ],
    )
    ensured_metadata = SimpleNamespace(
        sheet_url=metadata.sheet_url,
        draft_worksheet=DraftWorksheetMetadata(
            333 if ids_changed else 222,
            "Shift Draft",
            None,
        ),
        worksheets=[
            SimpleNamespace(id=1),
            SimpleNamespace(id=333 if ids_changed else 222),
            SimpleNamespace(id=3),
        ],
    )

    class Manager:
        async def get_saved_team_summary_destination(
            self,
        ) -> tuple[TeamSourceStatus, None]:
            return TeamSourceStatus.UNSET, None

        async def get_fresh_sheet_config(self) -> SimpleNamespace:
            return config

        async def fetch_google_sheets_metadata(self) -> SimpleNamespace:
            return metadata

        def log_missing_worksheet_warnings(self, _metadata: object) -> None:
            pass

        async def ensure_worksheets_and_upsert_sheet_config(
            self,
            _metadata: object,
        ) -> SimpleNamespace:
            return ensured_metadata

        async def generate_draft(self, *_args: object, **_kwargs: object) -> None:
            if ids_changed:
                raise StorageError(StorageErrorKind.PARTIAL_SUCCESS)
            raise GoogleSheetsError(
                GoogleSheetsErrorKind.TRANSIENT,
                "private draft failure",
            )

    config = SimpleNamespace(
        recruitment_time_ranges=[{"start": 4, "end": 5}],
        sheet_url=metadata.sheet_url,
        draft_worksheet_id=222,
    )

    async def get_feature_channel_context(_source: object) -> object:
        return object()

    async def get_configured_context(_context: object) -> SimpleNamespace:
        return SimpleNamespace(manager=Manager(), feature_config=config)

    class ConfirmView:
        value = True

        def __init__(
            self,
            *,
            requesting_user_id: int,
            destination_label: str,
            destination_url: str,
        ) -> None:
            assert requesting_user_id == 333
            assert destination_label == "Shift Draft"
            assert destination_url.endswith("#gid=222")

        async def wait(self) -> None:
            pass

    @asynccontextmanager
    async def unlocked(_channel_id: int) -> object:
        yield

    subject = ShiftRegister(fake_bot())
    subject._get_register_feature_channel_context = get_feature_channel_context
    subject._get_configured_register_feature_channel_context = get_configured_context
    subject.sheet_write_lock = unlocked
    monkeypatch.setattr(
        "cogs.shift_register.GenerateShiftScheduleConfirmView", ConfirmView
    )
    interaction = FakeInteraction()

    await ShiftRegister.generate_draft.callback(subject, interaction, 35)

    assert len(interaction.followup.messages) == 1
    content, kwargs = interaction.followup.messages[0]
    assert content is not None
    if ids_changed:
        assert "Some changes may have been saved" in content
    else:
        assert "Some changes may have been saved" not in content
        assert "Google Sheets is temporarily unavailable" in content
    assert kwargs == {"ephemeral": True}


@pytest.mark.asyncio
@pytest.mark.parametrize("confirmation", [False, None])
async def test_generate_draft_cancel_or_timeout_skips_google_sheets(
    monkeypatch: pytest.MonkeyPatch,
    *,
    confirmation: bool | None,
) -> None:
    class Manager:
        async def get_saved_team_summary_destination(
            self,
        ) -> tuple[TeamSourceStatus, None]:
            return TeamSourceStatus.UNSET, None

        async def fetch_google_sheets_metadata(self) -> None:
            msg = "Google Sheets must not be accessed before confirmation"
            raise AssertionError(msg)

    async def get_feature_channel_context(_source: object) -> object:
        return object()

    async def get_configured_context(_context: object) -> SimpleNamespace:
        return SimpleNamespace(
            manager=Manager(),
            feature_config=SimpleNamespace(
                recruitment_time_ranges=[{"start": 4, "end": 5}],
                sheet_url="https://docs.google.com/spreadsheets/d/abc/edit",
                draft_worksheet_id=222,
            ),
        )

    class ConfirmView:
        value = confirmation

        def __init__(
            self,
            *,
            requesting_user_id: int,
            destination_label: str,
            destination_url: str,
        ) -> None:
            assert requesting_user_id == 333
            assert destination_label == "Shift Draft"
            assert destination_url.endswith("#gid=222")

        async def wait(self) -> None:
            pass

    subject = ShiftRegister(fake_bot())
    subject._get_register_feature_channel_context = get_feature_channel_context
    subject._get_configured_register_feature_channel_context = get_configured_context
    monkeypatch.setattr(
        "cogs.shift_register.GenerateShiftScheduleConfirmView", ConfirmView
    )
    interaction = FakeInteraction()

    await ShiftRegister.generate_draft.callback(subject, interaction, 35)

    assert interaction.followup.messages == []
    assert "`A1:G31`" in interaction.original_response_edits[0][0]
    if confirmation is False:
        assert interaction.original_response_edits[-1][1] == {"view": None}
    else:
        assert interaction.original_response_edits[-1] == (
            "✖️ 確認逾時，未變更 Shift Draft。",
            {"view": None},
        )


@pytest.mark.asyncio
async def test_generate_draft_changed_destinations_skip_google_sheets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    feature_context_calls = 0

    class Manager:
        def __init__(self, current_config: SimpleNamespace) -> None:
            self.current_config = current_config

        async def get_fresh_sheet_config(self) -> SimpleNamespace:
            return self.current_config

        async def get_saved_team_summary_destination(
            self,
        ) -> tuple[TeamSourceStatus, None]:
            return TeamSourceStatus.UNSET, None

        async def fetch_google_sheets_metadata(self) -> None:
            msg = "changed destinations must abort before Google Sheets"
            raise AssertionError(msg)

    async def get_feature_channel_context(_source: object) -> object:
        nonlocal feature_context_calls
        feature_context_calls += 1
        return object()

    configs = iter(
        [
            SimpleNamespace(
                recruitment_time_ranges=[{"start": 4, "end": 5}],
                sheet_url="https://docs.google.com/spreadsheets/d/abc/edit",
                draft_worksheet_id=222,
            ),
            SimpleNamespace(
                recruitment_time_ranges=[{"start": 4, "end": 6}],
                sheet_url="https://docs.google.com/spreadsheets/d/abc/edit",
                draft_worksheet_id=222,
            ),
        ]
    )

    async def get_configured_context(_context: object) -> SimpleNamespace:
        current_config = next(configs)
        return SimpleNamespace(
            manager=Manager(current_config),
            feature_config=current_config,
        )

    class ConfirmView:
        value = True

        def __init__(
            self,
            *,
            requesting_user_id: int,
            destination_label: str,
            destination_url: str,
        ) -> None:
            assert requesting_user_id == 333
            assert destination_label == "Shift Draft"
            assert destination_url.endswith("#gid=222")

        async def wait(self) -> None:
            pass

    subject = ShiftRegister(fake_bot())
    subject._get_register_feature_channel_context = get_feature_channel_context
    subject._get_configured_register_feature_channel_context = get_configured_context
    monkeypatch.setattr(
        "cogs.shift_register.GenerateShiftScheduleConfirmView", ConfirmView
    )
    interaction = FakeInteraction()

    await ShiftRegister.generate_draft.callback(subject, interaction, 35)

    assert feature_context_calls == 2
    assert interaction.followup.messages == []
    assert interaction.original_response_edits[-1] == (
        "⚠️ 募集時段設定已變更，未變更 Shift Draft；請重新執行 command。",
        {"view": None},
    )


async def fake_feature_channel_get(
    *, guild_id: int, channel_id: int, feature_name: str
) -> SimpleNamespace:
    return SimpleNamespace(
        guild_id=guild_id,
        channel_id=channel_id,
        feature_name=feature_name,
    )


def feature_channel_row(
    feature_name: str,
    *,
    feature_channel_id: int = 77,
    guild_id: int = 111,
    channel_id: int = 222,
) -> FeatureChannel:
    return FeatureChannel(
        id=feature_channel_id,
        guild_id=guild_id,
        channel_id=channel_id,
        feature_name=feature_name,
        is_enabled=True,
    )


async def fake_feature_channel_get_or_none(
    *, guild_id: int, channel_id: int, feature_name: str
) -> FeatureChannel:
    return feature_channel_row(
        feature_name,
        guild_id=guild_id,
        channel_id=channel_id,
    )


async def fake_enabled_feature_channel_by_id(**query: int) -> FeatureChannel:
    return feature_channel_row(
        "shift_register",
        feature_channel_id=query["id"],
    )


class FakeMessage:
    id = 123

    def __init__(self) -> None:
        self.added_reactions: list[str] = []
        self.removed_reactions: list[tuple[str, object]] = []
        self.reaction_events: list[tuple[object, ...]] = []

    async def add_reaction(self, emoji: str) -> None:
        self.added_reactions.append(emoji)
        self.reaction_events.append(("add", emoji))

    async def remove_reaction(self, emoji: str, user: object) -> None:
        self.removed_reactions.append((emoji, user))
        self.reaction_events.append(("remove", emoji, user))


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


class FakeRegisterChannel:
    id = 222

    async def send(self, *_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(id=456)

    async def fetch_message(self, message_id: int) -> SimpleNamespace:
        return SimpleNamespace(id=message_id)


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
        self.channel = FakeRegisterChannel()


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


def assert_worksheet_contract_content(content: str) -> None:
    assert re.fullmatch(
        r"⚠️📏 The configured Google Sheet layout needs correction\. Reopen "
        r"settings, verify the worksheets, and try again\. Reference: "
        r"`WSC-[0-9a-f]{8}`",
        content,
    )


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
            landing_worksheet_id=333,
        )


class ConfiguredShiftHelpUrlManager(ConfiguredHelpUrlManager):
    async def get_sheet_config_or_none(self) -> SimpleNamespace:
        config = await super().get_sheet_config_or_none()
        config.landing_worksheet_id = config.entry_worksheet_id
        return config

    async def get_saved_team_source_channel_id(self) -> int:
        return 987


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
    id = 222

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
        bot=SimpleNamespace(user=SimpleNamespace(mention="@Rhoboto"), cogs={}),
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
        landing_worksheet_id=444,
        day_number=2,
        event_date=dt.date(2026, 8, 12),
        submission_deadline_at=dt.datetime(2026, 8, 12, 12, tzinfo=dt.UTC),
        draft_shift_proposal_at=dt.datetime(2026, 8, 13, 11, tzinfo=dt.UTC),
        final_shift_notice_at=dt.datetime(2026, 8, 14, 9, tzinfo=dt.UTC),
        recruitment_time_ranges=[{"start": 4, "end": 28}],
    )


def shift_auto_guide_context() -> ConfiguredRegisterFeatureChannelContext:
    return ConfiguredRegisterFeatureChannelContext(
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


class GatedRecordingLock:
    def __init__(self) -> None:
        self.keys: list[object] = []
        self.attempted = asyncio.Event()
        self.release = asyncio.Event()

    @asynccontextmanager
    async def __call__(self, key: object) -> object:
        self.keys.append(key)
        self.attempted.set()
        await self.release.wait()
        yield


class _ManualLifecycleManager:
    def __init__(
        self,
        feature_channel: object,
        _service_account_path: str,
        *,
        events: list[str],
        config_id: int | None = 91,
        error: Exception | None = None,
    ) -> None:
        self.feature_channel = feature_channel
        self.events = events
        self.config_id = config_id
        self.error = error

    async def set_manual_feature_enabled(self, *, enabled: bool) -> int | None:
        self.events.append(f"enabled:{enabled}")
        if self.error is not None:
            raise self.error
        return self.config_id

    async def clear_feature_settings(self) -> int | None:
        self.events.append("clear")
        if self.error is not None:
            raise self.error
        return self.config_id


@pytest.mark.asyncio
async def test_shift_auto_close_manual_hard_clear_auto_close_cancels_after_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    feature_channel = SimpleNamespace(id=77, is_enabled=True)
    manager = _ManualLifecycleManager(feature_channel, "service.json", events=events)
    scheduler = SimpleNamespace(cancel=lambda *_args: events.append("cancel"))
    subject = ShiftRegister(fake_bot())
    subject.ManagerType = lambda *_args: manager  # type: ignore[method-assign]
    subject.sheet_write_lock = RecordingLock()
    subject._timeline_scheduler = scheduler  # type: ignore[assignment]
    key = (91, ShiftTimelineEventKind.SUBMISSION_DEADLINE)
    subject._pending_message_ids[key] = (123, 456)

    async def get_or_none(**_kwargs: object) -> SimpleNamespace:
        return feature_channel

    async def get_or_create(**_kwargs: object) -> tuple[SimpleNamespace, bool]:
        events.append("get_or_create")
        return feature_channel, False

    monkeypatch.setattr(FeatureChannel, "get_or_none", get_or_none)
    monkeypatch.setattr(FeatureChannel, "get_or_create", get_or_create)

    await subject._enable_channel(111, 222)
    assert events == ["get_or_create", "enabled:True", "cancel"]
    assert key not in subject._pending_message_ids
    assert subject.sheet_write_lock.keys == [222]

    events.clear()
    subject._pending_message_ids[key] = (123, 456)
    assert await subject._disable_channel(111, 222) is True
    assert events == ["enabled:False", "cancel"]
    assert key not in subject._pending_message_ids

    events.clear()
    subject._pending_message_ids[key] = (123, 456)
    await subject._clear_feature_settings(111, 222)
    assert events == ["clear", "cancel"]
    assert key not in subject._pending_message_ids


@pytest.mark.asyncio
async def test_shift_auto_close_manual_lifecycle_failure_keeps_task_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    feature_channel = SimpleNamespace(id=77, is_enabled=True)
    error = RuntimeError("transaction failed")
    manager = _ManualLifecycleManager(
        feature_channel,
        "service.json",
        events=events,
        error=error,
    )
    scheduler = SimpleNamespace(cancel=lambda *_args: events.append("cancel"))
    subject = ShiftRegister(fake_bot())
    subject.ManagerType = lambda *_args: manager  # type: ignore[method-assign]
    subject._timeline_scheduler = scheduler  # type: ignore[assignment]
    key = (91, ShiftTimelineEventKind.SUBMISSION_DEADLINE)
    subject._pending_message_ids[key] = (123, 456)

    async def get_or_none(**_kwargs: object) -> SimpleNamespace:
        return feature_channel

    monkeypatch.setattr(FeatureChannel, "get_or_none", get_or_none)

    with pytest.raises(RuntimeError, match="transaction failed"):
        await subject._disable_channel(111, 222)

    assert events == ["enabled:False"]
    assert subject._pending_message_ids[key] == (123, 456)


@pytest.mark.asyncio
async def test_shift_auto_close_manual_lifecycle_missing_rows_are_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_feature_channel(**_kwargs: object) -> None:
        return None

    monkeypatch.setattr(FeatureChannel, "get_or_none", no_feature_channel)
    subject = ShiftRegister(fake_bot())

    assert await subject._disable_channel(111, 222) is False
    await subject._clear_feature_settings(111, 222)


class _RegistrationRaceManager:
    def __init__(
        self,
        feature_channel: object,
        started: asyncio.Event | None = None,
    ) -> None:
        self.feature_channel = feature_channel
        self.started = started
        self.release = asyncio.Event()
        self.events: list[str] = []

    async def get_fresh_sheet_config(self) -> SimpleNamespace:
        self.events.append("fresh")
        if self.started is not None:
            self.started.set()
            await self.release.wait()
        return SimpleNamespace(
            sheet_url="https://sheet.example",
            recruitment_time_ranges=[{"start": 4, "end": 28}],
        )

    async def fetch_google_sheets_metadata(self) -> SimpleNamespace:
        self.events.append("metadata")
        return SimpleNamespace()

    def log_missing_worksheet_warnings(self, _metadata: object) -> None:
        self.events.append("warnings")

    async def upsert_or_delete_user_shift(
        self,
        _user_info: UserInfo,
        _shift: RegisterShift | None,
        *,
        metadata: object,
        recruitment_ranges: RecruitmentTimeRanges,
    ) -> None:
        del metadata, recruitment_ranges
        self.events.append("write")


def _registration_race_context(
    manager: _RegistrationRaceManager,
) -> ConfiguredRegisterFeatureChannelContext:
    return ConfiguredRegisterFeatureChannelContext(
        guild_id=111,
        channel_id=222,
        feature_channel=manager.feature_channel,
        manager=manager,
        feature_config=SimpleNamespace(
            recruitment_time_ranges=[{"start": 4, "end": 28}],
        ),
    )


@pytest.mark.asyncio
async def test_shift_registration_close_race_rechecks_inside_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    feature_channel = SimpleNamespace(id=77, is_enabled=True)
    manager = _RegistrationRaceManager(feature_channel)
    context = _registration_race_context(manager)
    subject = ShiftRegister(fake_bot())
    early_lookup = asyncio.Event()
    before_lock = asyncio.Event()
    release_to_lock = asyncio.Event()

    async def get_context_or_none(_message: object) -> object:
        early_lookup.set()
        return context

    async def get_configured_context(_context: object) -> object:
        before_lock.set()
        await release_to_lock.wait()
        return context

    def parse(_message: object) -> object:
        shift, user_info = shift_register_submission()
        return MessageParseResult.parsed(shift, user_info=user_info)

    async def get_or_none(**_kwargs: object) -> SimpleNamespace:
        return feature_channel

    auto_guide_calls: list[object] = []

    async def base_refresh(*args: object, **_kwargs: object) -> bool:
        auto_guide_calls.append(args)
        return True

    monkeypatch.setattr(FeatureChannel, "get_or_none", get_or_none)
    monkeypatch.setattr(
        RegisterFeatureChannelBase,
        "_refresh_auto_guide_if_enabled",
        base_refresh,
    )
    subject._get_message_feature_channel_context_or_none = (  # type: ignore[method-assign]
        get_context_or_none
    )
    subject._get_configured_message_context = (  # type: ignore[method-assign]
        get_configured_context
    )
    subject._parse_message_submission = parse  # type: ignore[method-assign]
    subject.sheet_write_lock = SHIFT_REGISTER_SHEET_WRITE_LOCK
    message = FakeRegisterMessage(content="4-8")

    task = asyncio.create_task(subject.on_message(message))
    await early_lookup.wait()
    await before_lock.wait()
    async with SHIFT_REGISTER_SHEET_WRITE_LOCK(message.channel.id):
        feature_channel.is_enabled = False
    release_to_lock.set()
    await task

    assert manager.events == ["fresh"]
    assert message.reaction_events == []
    assert auto_guide_calls == []


@pytest.mark.asyncio
async def test_shift_registration_close_race_wins_then_deadline_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    feature_channel = SimpleNamespace(id=77, is_enabled=True)
    manager = _RegistrationRaceManager(feature_channel, asyncio.Event())
    context = _registration_race_context(manager)
    subject = ShiftRegister(fake_bot())
    early_lookup = asyncio.Event()

    async def get_context_or_none(_message: object) -> object:
        early_lookup.set()
        return context

    async def get_configured_context(_context: object) -> object:
        return context

    def parse(_message: object) -> object:
        shift, user_info = shift_register_submission()
        return MessageParseResult.parsed(shift, user_info=user_info)

    async def close_after_registration() -> None:
        async with SHIFT_REGISTER_SHEET_WRITE_LOCK(222):
            feature_channel.is_enabled = False
            manager.events.append("closed")

    async def get_or_none(**_kwargs: object) -> SimpleNamespace:
        return feature_channel

    monkeypatch.setattr(FeatureChannel, "get_or_none", get_or_none)
    subject._get_message_feature_channel_context_or_none = (  # type: ignore[method-assign]
        get_context_or_none
    )
    subject._get_configured_message_context = (  # type: ignore[method-assign]
        get_configured_context
    )
    subject._parse_message_submission = parse  # type: ignore[method-assign]
    subject._refresh_auto_guide_if_enabled = _noop_async  # type: ignore[method-assign]
    subject.sheet_write_lock = SHIFT_REGISTER_SHEET_WRITE_LOCK
    message = FakeRegisterMessage(content="4-8")

    registration = asyncio.create_task(subject.on_message(message))
    await early_lookup.wait()
    await manager.started.wait()
    closing = asyncio.create_task(close_after_registration())
    await asyncio.sleep(0)
    manager.release.set()
    await registration
    await closing

    assert manager.events == ["fresh", "metadata", "warnings", "write", "closed"]
    assert feature_channel.is_enabled is False


@pytest.mark.asyncio
async def test_shift_auto_guide_refresh_lookup_failure_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    feature_channel = SimpleNamespace(id=77, is_enabled=True)
    context = _registration_race_context(_RegistrationRaceManager(feature_channel))
    subject = ShiftRegister(fake_bot())
    subject.logger = RecordingLogger()

    async def fail_lookup(**_kwargs: object) -> None:
        raise private_database_error()

    monkeypatch.setattr(FeatureChannel, "get_or_none", fail_lookup)

    assert await subject._refresh_auto_guide_if_enabled(context, object()) is False
    assert len(subject.logger.exceptions) == 1  # type: ignore[union-attr]


class UnexpectedTeamRegisterManager:
    def __init__(self, *_: object, **__: object) -> None:
        msg = "summary should use self.ManagerType"
        raise AssertionError(msg)


class SummaryManager(ConfiguredManager):
    last_instance: SummaryManager | None = None
    summary_dataframe = object()

    def __init__(self, feature_channel: object, service_account_path: str) -> None:
        super().__init__(feature_channel, service_account_path)
        self.member_by_names: dict[str, object] | None = None
        self.current_sheet_url = (
            "https://docs.google.com/spreadsheets/d/team-summary/edit"
        )
        self.fresh_sheet_urls: list[str] = []
        SummaryManager.last_instance = self

    async def get_sheet_config_or_none(self) -> SimpleNamespace:
        return SimpleNamespace(
            sheet_url=self.current_sheet_url,
            landing_worksheet_id=None,
        )

    async def get_fresh_sheet_config(self) -> SimpleNamespace:
        config = await self.get_sheet_config_or_none()
        self.fresh_sheet_urls.append(config.sheet_url)
        return config

    async def refresh_summary_registration(
        self,
        member_by_names: dict[str, object],
    ) -> object:
        self.member_by_names = member_by_names
        return self.summary_dataframe


class SummaryGoogleSheetsErrorManager(SummaryManager):
    async def refresh_summary_registration(
        self,
        member_by_names: dict[str, object],
    ) -> object:
        del member_by_names
        raise GoogleSheetsError(
            GoogleSheetsErrorKind.QUOTA,
            "private sheet quota detail",
        )


class SummaryRefreshErrorManager(SummaryManager):
    async def refresh_summary_registration(
        self,
        member_by_names: dict[str, object],
    ) -> object:
        await super().refresh_summary_registration(
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


class IntegratedTeamDeleteManager:
    def __init__(self) -> None:
        self.feature_channel = SimpleNamespace(channel_id=222)
        self.users: list[UserInfo] = []
        self.current_sheet_url = (
            "https://docs.google.com/spreadsheets/d/current-team-delete/edit"
        )
        self.fresh_sheet_urls: list[str] = []
        self.metadata = SimpleNamespace(name="fresh_team_delete_metadata")
        self.events: list[str] = []

    async def get_fresh_sheet_config(self) -> SimpleNamespace:
        self.events.append("fresh_config")
        self.fresh_sheet_urls.append(self.current_sheet_url)
        return SimpleNamespace(sheet_url=self.current_sheet_url)

    async def fetch_google_sheets_metadata(self) -> SimpleNamespace:
        self.events.append("fetch_metadata")
        return self.metadata

    async def delete_user_registration(self, user: UserInfo) -> None:
        await self.fetch_google_sheets_metadata()
        self.events.append("delete")
        self.users.append(user)


class InvalidInitialSettingsManager:
    def __init__(self) -> None:
        self.feature_channel = SimpleNamespace(channel_id=222)
        self.upsert_calls = 0

    async def upsert_sheet_config_and_worksheets(self, **_kwargs: object) -> None:
        self.upsert_calls += 1


class IntegratedShiftDeleteManager:
    def __init__(self) -> None:
        self.feature_channel = SimpleNamespace(channel_id=222)
        self.users: list[UserInfo] = []
        self.current_sheet_url = (
            "https://docs.google.com/spreadsheets/d/current-shift-delete/edit"
        )
        self.fresh_sheet_urls: list[str] = []
        self.metadata = SimpleNamespace(name="fresh_shift_delete_metadata")
        self.events: list[str] = []

    async def get_fresh_sheet_config(self) -> SimpleNamespace:
        self.events.append("fresh_config")
        self.fresh_sheet_urls.append(self.current_sheet_url)
        return SimpleNamespace(sheet_url=self.current_sheet_url)

    async def fetch_google_sheets_metadata(self) -> SimpleNamespace:
        self.events.append("fetch_metadata")
        return self.metadata

    async def upsert_or_delete_user_shift(
        self,
        user: UserInfo,
        shift: None,
        metadata: object,
    ) -> None:
        assert shift is None
        assert metadata is self.metadata
        self.events.append("delete")
        self.users.append(user)


class UnexpectedSetupManager:
    def __init__(self, *_: object, **__: object) -> None:
        msg = "setup_after_enable should use self.ManagerType"
        raise AssertionError(msg)


class PanelManager(ConfiguredManager):
    last_instance: PanelManager | None = None

    def __init__(self, feature_channel: object, service_account_path: str) -> None:
        super().__init__(feature_channel, service_account_path)
        PanelManager.last_instance = self


def team_test_config(sheet_url: str) -> TeamRegisterConfig:
    return TeamRegisterConfig(
        sheet_url=sheet_url,
        team_worksheet_ids=[],
        summary_worksheet_id=1,
        encore_role_ids=[],
    )


def shift_test_config(
    sheet_url: str,
    *,
    recruitment_time_ranges: list[dict[str, int]] | None = None,
) -> ShiftRegisterConfig:
    return ShiftRegisterConfig(
        sheet_url=sheet_url,
        entry_worksheet_id=1,
        draft_worksheet_id=2,
        final_schedule_worksheet_id=3,
        recruitment_time_ranges=(
            recruitment_time_ranges
            if recruitment_time_ranges is not None
            else [{"start": 4, "end": 28}]
        ),
    )


class MessageOrchestrationManager(
    ManagerBase[TeamRegisterConfig, TeamRegisterGoogleSheetsMetadata]
):
    SheetConfigType = TeamRegisterConfig
    GoogleSheetsMetadataType = TeamRegisterGoogleSheetsMetadata
    last_instance: MessageOrchestrationManager | None = None

    def __init__(
        self,
        feature_channel: FeatureChannel,
        service_account_path: str,
    ) -> None:
        super().__init__(feature_channel, service_account_path)
        MessageOrchestrationManager.last_instance = self

    @override
    async def get_sheet_config_or_none(self) -> TeamRegisterConfig | None:
        return team_test_config("https://sheet.example")


class OrderedTeamUpsertManager(TeamRegisterManager):
    def __init__(
        self,
        feature_channel: FeatureChannel,
        service_account_path: str,
        *,
        ensure_error: Exception | None = None,
        team_error: Exception | None = None,
        summary_error: Exception | None = None,
    ) -> None:
        super().__init__(feature_channel, service_account_path)
        self.events: list[str] = []
        self.ensure_error = ensure_error
        self.team_error = team_error
        self.summary_error = summary_error
        self.current_sheet_url = (
            "https://docs.google.com/spreadsheets/d/team-message/edit"
        )
        self.fresh_sheet_urls: list[str] = []

    @override
    async def get_fresh_sheet_config(self) -> TeamRegisterConfig:
        self.fresh_sheet_urls.append(self.current_sheet_url)
        return team_test_config(self.current_sheet_url)

    @override
    async def upsert_user_registration(
        self,
        user: UserInfo,
        roles: list[Role],
        main_team: RegisterTeam,
        encore_team: RegisterTeam | None,
        *backup_teams: RegisterTeam,
    ) -> None:
        assert user.username == "alice"
        assert roles == []
        assert main_team.username == "alice"
        assert encore_team is None
        assert backup_teams == ()
        self.events.append("upsert")
        if self.ensure_error is not None:
            raise self.ensure_error
        if self.team_error is not None:
            raise self.team_error
        if self.summary_error is not None:
            raise self.summary_error


class OrderedShiftUpsertManager(ShiftRegisterManager):
    def __init__(
        self,
        feature_channel: FeatureChannel,
        service_account_path: str,
        *,
        ensure_error: Exception | None = None,
        upsert_error: Exception | None = None,
        ensure_changes_ids: bool = False,
    ) -> None:
        super().__init__(feature_channel, service_account_path)
        self.events: list[str] = []
        self.current_sheet_url = (
            "https://docs.google.com/spreadsheets/d/shift-message/edit"
        )
        self.metadata = ShiftRegisterGoogleSheetsMetadata(
            sheet_url=self.current_sheet_url,
            worksheets=[
                EntryWorksheetMetadata(id=1, title="Entry", worksheet=None),
                DraftWorksheetMetadata(id=2, title="Draft", worksheet=None),
                FinalScheduleWorksheetMetadata(
                    id=3,
                    title="Final",
                    worksheet=None,
                ),
            ],
        )
        self.ensured_metadata = ShiftRegisterGoogleSheetsMetadata(
            sheet_url=self.current_sheet_url,
            worksheets=[
                EntryWorksheetMetadata(
                    id=4 if ensure_changes_ids else 1,
                    title="Entry",
                    worksheet=None,
                ),
                DraftWorksheetMetadata(id=2, title="Draft", worksheet=None),
                FinalScheduleWorksheetMetadata(
                    id=3,
                    title="Final",
                    worksheet=None,
                ),
            ],
        )
        self.ensure_error = ensure_error
        self.upsert_error = upsert_error
        self.ensure_changes_ids = ensure_changes_ids
        self.fresh_sheet_urls: list[str] = []

    @override
    async def get_fresh_sheet_config(self) -> ShiftRegisterConfig:
        self.fresh_sheet_urls.append(self.current_sheet_url)
        return shift_test_config(
            self.current_sheet_url,
            recruitment_time_ranges=[{"start": 4, "end": 28}],
        )

    @override
    async def fetch_google_sheets_metadata(
        self,
    ) -> ShiftRegisterGoogleSheetsMetadata:
        self.events.append("fetch_metadata")
        return self.metadata

    @override
    def log_missing_worksheet_warnings(
        self,
        metadata: ShiftRegisterGoogleSheetsMetadata,
    ) -> None:
        assert metadata is self.metadata
        self.events.append("log_missing")

    @override
    async def ensure_worksheets_and_upsert_sheet_config(
        self,
        metadata: ShiftRegisterGoogleSheetsMetadata,
        counts: dict[type[WorksheetMetadata], int] | None = None,
    ) -> ShiftRegisterGoogleSheetsMetadata:
        assert metadata is self.metadata
        assert counts is None
        self.events.append("ensure")
        if self.ensure_error is not None:
            raise self.ensure_error
        return self.ensured_metadata

    @override
    async def upsert_or_delete_user_shift(
        self,
        user: UserInfo,
        shift: RegisterShift | None,
        metadata: ShiftRegisterGoogleSheetsMetadata,
        *,
        recruitment_ranges: RecruitmentTimeRanges | None = None,
    ) -> None:
        assert user.username == "alice"
        assert shift is not None
        metadata = await self.ensure_worksheets_and_upsert_sheet_config(metadata)
        assert metadata is self.ensured_metadata
        assert recruitment_ranges is not None
        assert recruitment_ranges.to_json() == [{"start": 4, "end": 28}]
        self.events.append("upsert")
        if self.upsert_error is not None:
            if self.ensure_changes_ids:
                error = StorageError(StorageErrorKind.PARTIAL_SUCCESS)
                error.__cause__ = self.upsert_error
                raise error
            raise self.upsert_error


class MissingMessageConfigManager(MessageOrchestrationManager):
    @override
    async def get_sheet_config_or_none(self) -> TeamRegisterConfig | None:
        return None


class RecordingMessageSubject(
    RegisterFeatureChannelContextMixin[
        TeamRegisterConfig,
        TeamRegisterGoogleSheetsMetadata,
        MessageOrchestrationManager,
    ],
    MessageUpsertFeatureChannelBase[
        RegisterFeatureChannelContext[MessageOrchestrationManager],
        ConfiguredRegisterFeatureChannelContext[
            TeamRegisterConfig,
            MessageOrchestrationManager,
        ],
        object,
        str,
    ],
    group_name="recording_message_test",
):
    feature_name = "team_register"
    feature_display_name = "Team Register Test"
    ManagerType = MessageOrchestrationManager

    def __init__(self, parse_result: MessageParseResult[object]) -> None:
        super().__init__(
            SimpleNamespace(
                tree=SimpleNamespace(add_command=lambda _command: None),
                user=object(),
                cogs={},
            )
        )
        self.parse_result = parse_result
        self.logger = NullLogger()
        self.configured_calls: list[
            tuple[
                Message,
                ConfiguredRegisterFeatureChannelContext[
                    TeamRegisterConfig,
                    MessageOrchestrationManager,
                ],
                object,
                UserInfo,
            ]
        ] = []

    @override
    async def setup_after_enable(self, interaction: Interaction) -> None:
        del interaction

    @override
    def _build_message_context(
        self,
        membership: FeatureChannel,
    ) -> RegisterFeatureChannelContext[MessageOrchestrationManager]:
        return self._build_register_feature_channel_context(membership)

    @override
    async def _get_configured_message_context(
        self,
        context: RegisterFeatureChannelContext[MessageOrchestrationManager],
    ) -> (
        ConfiguredRegisterFeatureChannelContext[
            TeamRegisterConfig,
            MessageOrchestrationManager,
        ]
        | None
    ):
        return await self._get_configured_register_feature_channel_context(context)

    @override
    async def _process_enabled_message(
        self,
        message: Message,
        context: RegisterFeatureChannelContext[MessageOrchestrationManager],
    ) -> None:
        await RegisterFeatureChannelBase._process_enabled_message(
            self,
            message,
            context,
        )

    @override
    def _parse_message_submission(
        self,
        message: Message,
    ) -> MessageParseResult[object]:
        del message
        return self.parse_result

    @override
    async def _process_configured_message_submission(
        self,
        message: Message,
        context: ConfiguredRegisterFeatureChannelContext[
            TeamRegisterConfig,
            MessageOrchestrationManager,
        ],
        submission: object,
        user_info: UserInfo,
    ) -> str:
        self.configured_calls.append((message, context, submission, user_info))
        return "processed"

    @override
    async def _process_context_menu_message(
        self,
        interaction: Interaction,
        message: Message,
        source: GuildChannelSource,
    ) -> None:
        del interaction, message, source


def fake_bot() -> SimpleNamespace:
    return SimpleNamespace(
        tree=SimpleNamespace(add_command=lambda _command: None),
        user=None,
        cogs={},
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
    context_method_names = (
        "_get_register_feature_channel_context",
        "_get_register_feature_channel_context_or_none",
        "_get_configured_register_feature_channel_context",
        "_build_register_feature_channel_context",
        "_send_missing_register_config_followup",
    )
    core_method_names = (
        "_interaction_storage_context",
        "_send_interaction_storage_error_or_raise",
        "_validate_lifecycle_owner",
    )
    register_method_names = (
        "_guide_sheet_url",
        "_guide_template_values",
        "_auto_guide_is_enabled",
        "_auto_guide_template_values",
        "_render_auto_guide_embeds",
        "_render_localized_embeds",
        "_auto_guide_delete_callback",
        "_build_auto_guide_buttons_view",
        "_refresh_auto_guide_if_enabled",
        "_send_and_record_auto_guide",
        "_send_auto_guide_message",
        "_delete_auto_guide_message",
        "_disable_auto_guide_and_delete_message",
        "_delete_auto_guide_message_for_hard_clear",
        "toggle_auto_guide_from_settings",
        "_process_context_menu_message",
        "_process_enabled_message",
        "_cleanup_after_disable",
        "_cleanup_before_clear",
    )
    for owner, method_names in (
        (RegisterFeatureChannelContextMixin, context_method_names),
        (FeatureChannelBase, core_method_names),
        (RegisterFeatureChannelBase, register_method_names),
    ):
        for method_name in method_names:
            method = getattr(owner, method_name)
            setattr(subject, method_name, method.__get__(subject, type(subject)))

    if not hasattr(subject, "_get_feature_channel_or_none"):

        async def get_feature_channel_or_none(
            guild_id: int,
            channel_id: int,
            feature_name: str | None = None,
            *,
            require_enabled: bool = False,
        ) -> object | None:
            return await FeatureChannelBase._get_feature_channel_or_none(
                guild_id,
                channel_id,
                feature_name or subject.feature_name,
                require_enabled=require_enabled,
            )

        subject._get_feature_channel_or_none = get_feature_channel_or_none
    if not hasattr(subject, "_get_enabled_feature_channel_or_none"):

        async def get_enabled_feature_channel_or_none(
            guild_id: int,
            channel_id: int,
            feature_name: str | None = None,
        ) -> object | None:
            return await FeatureChannelBase._get_enabled_feature_channel_or_none(
                guild_id,
                channel_id,
                feature_name or subject.feature_name,
            )

        subject._get_enabled_feature_channel_or_none = (
            get_enabled_feature_channel_or_none
        )
    if not hasattr(subject, "_delete_user_data_transaction"):
        method = RegisterFeatureChannelUserBase._delete_user_data_transaction
        subject._delete_user_data_transaction = method.__get__(subject, type(subject))
    return subject


def register_message_listener_subject(**attributes: object) -> SimpleNamespace:
    subject = SimpleNamespace(**attributes)
    method = RegisterFeatureChannelBase._process_enabled_message
    subject._process_enabled_message = method.__get__(subject, type(subject))
    return subject


def ordered_team_upsert_context(
    manager: OrderedTeamUpsertManager,
) -> ConfiguredRegisterFeatureChannelContext[
    TeamRegisterConfig,
    OrderedTeamUpsertManager,
]:
    return ConfiguredRegisterFeatureChannelContext(
        guild_id=111,
        channel_id=222,
        feature_channel=feature_channel_row("team_register"),
        manager=manager,
        feature_config=team_test_config(
            "https://docs.google.com/spreadsheets/d/team-message/edit"
        ),
    )


def ordered_shift_upsert_context(
    manager: OrderedShiftUpsertManager,
) -> ConfiguredRegisterFeatureChannelContext[
    ShiftRegisterConfig,
    OrderedShiftUpsertManager,
]:
    return ConfiguredRegisterFeatureChannelContext(
        guild_id=111,
        channel_id=222,
        feature_channel=feature_channel_row("shift_register"),
        manager=manager,
        feature_config=shift_test_config(
            "https://sheet.example",
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
    assert not hasattr(
        feature_channel_base, "_get_configured_register_feature_channel_context"
    )
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
    get_context = (
        RegisterFeatureChannelContextMixin._get_register_feature_channel_context
    )
    feature_channel_context = await get_context(subject, source)
    mixin = RegisterFeatureChannelContextMixin
    get_configured_context = mixin._get_configured_register_feature_channel_context
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
    get_context = (
        RegisterFeatureChannelContextMixin._get_register_feature_channel_context
    )
    feature_channel_context = await get_context(subject, source)
    mixin = RegisterFeatureChannelContextMixin
    get_configured_context = mixin._get_configured_register_feature_channel_context
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
async def test_app_command_wrapped_db_failure_sends_safe_check_error(
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
        app_commands.CommandInvokeError(
            SimpleNamespace(name="wrapped-storage-check"),
            exc_info.value,
        ),
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
async def test_prefix_command_wrapped_db_failure_replies_safe_check_error(
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
    await FeatureChannelBase.cog_command_error(
        subject,
        ctx,
        commands.CommandInvokeError(exc_info.value),
    )

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
    membership = object()

    async def fake_get_feature_channel_or_none(
        guild_id: int,
        channel_id: int,
        _feature_name: str | None = None,
        *,
        require_enabled: bool = False,
    ) -> object:
        assert (guild_id, channel_id, require_enabled) == (111, 222, True)
        return membership

    async def fake_disable_channel(_guild_id: int, _channel_id: int) -> bool:
        return True

    async def fake_cleanup_after_disable(membership_arg: object) -> None:
        assert membership_arg is membership

    subject = feature_channel_context_subject(
        feature_name="team_register",
        feature_display_name="Team Register",
        _disable_channel=fake_disable_channel,
    )
    subject._get_feature_channel_or_none = fake_get_feature_channel_or_none
    subject._cleanup_after_disable = fake_cleanup_after_disable
    interaction = FakeInteraction()

    await FeatureChannelBase.disable.callback(subject, interaction)

    assert interaction.response.messages == [
        ("Feature Team Register disabled in this channel.", {"ephemeral": True})
    ]
    assert interaction.followup.messages == []


@pytest.mark.asyncio
async def test_disable_sends_auto_guide_warning_when_cleanup_fails() -> None:
    membership = object()
    calls: list[str] = []

    async def fake_get_feature_channel_or_none(
        guild_id: int,
        channel_id: int,
        _feature_name: str | None = None,
        *,
        require_enabled: bool = False,
    ) -> object:
        assert (guild_id, channel_id, require_enabled) == (111, 222, True)
        calls.append("get_context")
        return membership

    async def fake_disable_channel(_guild_id: int, _channel_id: int) -> bool:
        calls.append("disable")
        return True

    async def fake_cleanup_after_disable(membership_arg: object) -> str:
        assert membership_arg is membership
        calls.append("auto_guide")
        return register_feature_channel_base.LATEST_GUIDE_DELETE_FAILED_WARNING

    subject = feature_channel_context_subject(
        feature_name="team_register",
        feature_display_name="Team Register",
        _disable_channel=fake_disable_channel,
    )
    subject._get_feature_channel_or_none = fake_get_feature_channel_or_none
    subject._cleanup_after_disable = fake_cleanup_after_disable
    interaction = FakeInteraction()

    await FeatureChannelBase.disable.callback(subject, interaction)

    assert interaction.response.messages == [
        ("Feature Team Register disabled in this channel.", {"ephemeral": True})
    ]
    assert interaction.followup.messages == [
        (
            register_feature_channel_base.LATEST_GUIDE_DELETE_FAILED_WARNING,
            {"ephemeral": True},
        )
    ]
    assert calls == ["get_context", "disable", "auto_guide"]


@pytest.mark.asyncio
async def test_disable_response_uses_feature_display_name_when_not_enabled() -> None:
    async def fake_get_feature_channel_or_none(
        guild_id: int,
        channel_id: int,
        _feature_name: str | None = None,
        *,
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
    subject._get_feature_channel_or_none = fake_get_feature_channel_or_none
    subject._cleanup_after_disable = fail_if_called
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
    membership = object()
    calls: list[str] = []

    class ConfirmView:
        value = True

        async def wait(self) -> None:
            calls.append("wait")

    async def fake_get_feature_channel_or_none(
        guild_id: int,
        channel_id: int,
        _feature_name: str | None = None,
        *,
        require_enabled: bool = False,
    ) -> object:
        assert (guild_id, channel_id, require_enabled) == (111, 222, False)
        calls.append("get_context")
        return membership

    async def fake_cleanup_before_clear(membership_arg: object) -> str:
        assert membership_arg is membership
        calls.append("auto_guide")
        return (
            register_feature_channel_base.HARD_CLEAR_LATEST_GUIDE_DELETE_FAILED_WARNING
        )

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
    subject._get_feature_channel_or_none = fake_get_feature_channel_or_none
    subject._cleanup_before_clear = fake_cleanup_before_clear
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
            register_feature_channel_base.HARD_CLEAR_LATEST_GUIDE_DELETE_FAILED_WARNING,
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

    async def fake_get_feature_channel_or_none(
        guild_id: int,
        channel_id: int,
        _feature_name: str | None = None,
        *,
        require_enabled: bool = False,
    ) -> object:
        assert (guild_id, channel_id, require_enabled) == (111, 222, False)
        calls.append("get_context")
        return context.feature_channel

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
        register_feature_channel_base,
        "get_auto_guide_state",
        fake_get_auto_guide_state,
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
    subject._get_feature_channel_or_none = fake_get_feature_channel_or_none
    subject._build_register_feature_channel_context = lambda membership: (
        context
        if membership is context.feature_channel
        else pytest.fail("unexpected membership")
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
    subject._get_feature_channel_or_none = fail_if_called
    subject._cleanup_before_clear = fail_if_called
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

    await RegisterFeatureChannelUserBase.send_guide_message(
        subject, interaction, "team.guide"
    )

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
    subject._get_register_feature_channel_context = failing_context

    await RegisterFeatureChannelBase.setup_after_enable(subject, interaction)

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
    message_user_info = MessageUpsertFeatureChannelBase._message_user_info
    log_received_message = MessageUpsertFeatureChannelBase._log_received_message

    get_message_context = (
        MessageUpsertFeatureChannelBase._get_message_feature_channel_context_or_none
    )
    feature_channel_context = await get_message_context(subject, message)
    user_info = message_user_info(message)
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
        MessageUpsertFeatureChannelBase._get_message_feature_channel_context_or_none
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

    await MessageUpsertFeatureChannelBase.on_message(subject, message)

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

    await MessageUpsertFeatureChannelBase.on_message(subject, message)

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

    await MessageUpsertFeatureChannelBase.on_message(subject, message)

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

    await MessageUpsertFeatureChannelBase.upsert_from_content_menu(
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

    await MessageUpsertFeatureChannelBase.upsert_from_content_menu(
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
async def test_context_menu_contract_failure_marks_message_and_responds_safely(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bot_user = object()
    message = FakeMessage()

    async def raise_contract_error(
        message: FakeMessage,
        _feature_channel_context: object,
    ) -> None:
        await message.add_reaction(config.PROCESSING_EMOJI)
        required_unique_header_index(
            ["private-header", "private-header"],
            "private-header",
        )

    interaction = FakeInteraction()
    log = logging.getLogger("tests.feature_channel.context_menu_contract")
    caplog.set_level(logging.WARNING, logger=log.name)
    subject = feature_channel_context_subject(
        feature_name="team_register",
        feature_display_name="Team Register",
        _get_message_feature_channel_context_or_none=(
            fake_context_menu_feature_channel_context
        ),
        _process_feature_channel_message_with_outcome=raise_contract_error,
        bot=SimpleNamespace(user=bot_user),
        logger=log,
    )

    await MessageUpsertFeatureChannelBase.upsert_from_content_menu(
        subject, interaction, message
    )

    contents = interaction_contents(interaction)
    assert len(contents) == 1
    assert_worksheet_contract_content(contents[0])
    assert "private-header" not in contents[0]
    assert "private-header" not in caplog.text
    assert interaction.followup.messages[0][1] == {"ephemeral": True}
    assert message.added_reactions == [
        config.PROCESSING_EMOJI,
        config.WARNING_EMOJI,
        "📏",
    ]
    assert message.removed_reactions == [(config.PROCESSING_EMOJI, bot_user)]


@pytest.mark.asyncio
async def test_context_menu_marks_internal_error_and_preserves_traceback() -> None:
    bot_user = object()
    message = FakeMessage()
    internal_error = RuntimeError("internal bug")

    async def raise_internal_error(
        message: FakeMessage,
        _feature_channel_context: object,
    ) -> None:
        await message.add_reaction(config.PROCESSING_EMOJI)
        raise internal_error

    interaction = FakeInteraction()
    subject = feature_channel_context_subject(
        feature_name="team_register",
        feature_display_name="Team Register",
        _get_message_feature_channel_context_or_none=(
            fake_context_menu_feature_channel_context
        ),
        _process_feature_channel_message_with_outcome=raise_internal_error,
        bot=SimpleNamespace(user=bot_user),
        logger=NullLogger(),
    )

    with pytest.raises(RuntimeError) as exc_info:
        await MessageUpsertFeatureChannelBase.upsert_from_content_menu(
            subject,
            interaction,
            message,
        )

    assert exc_info.value is internal_error
    assert exc_info.traceback[-1].name == "raise_internal_error"
    assert message.added_reactions == [
        config.PROCESSING_EMOJI,
        config.WARNING_EMOJI,
        "🚧",
    ]
    assert message.removed_reactions == [(config.PROCESSING_EMOJI, bot_user)]


@pytest.mark.asyncio
async def test_context_menu_invalid_attempt_keeps_processor_reaction() -> None:
    message = FakeMessage()

    async def process_invalid_attempt(
        message: FakeMessage,
        _feature_channel_context: object,
    ) -> object:
        await message.add_reaction(config.WARNING_EMOJI)
        await message.add_reaction(config.CONFUSED_EMOJI)
        return MessageUpsertOutcome.invalid()

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

    await MessageUpsertFeatureChannelBase.upsert_from_content_menu(
        subject, interaction, message
    )

    assert message.added_reactions == [config.WARNING_EMOJI, config.CONFUSED_EMOJI]
    assert interaction.response.deferred == [True]
    assert interaction.followup.messages == [
        (
            "⚠️ The message contains an invalid Team Register format.",
            {"ephemeral": True},
        )
    ]


@pytest.mark.asyncio
async def test_context_menu_ordinary_text_failed_followup_without_reaction() -> None:
    message = FakeMessage()

    async def process_ordinary_text(
        _message: FakeMessage,
        _feature_channel_context: object,
    ) -> object:
        return MessageUpsertOutcome.ignored()

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

    await MessageUpsertFeatureChannelBase.upsert_from_content_menu(
        subject, interaction, message
    )

    assert interaction.response.deferred == [True]
    assert message.added_reactions == []
    assert interaction.followup.messages == [
        (
            "⚠️ No Team Register data was recognized in this message.",
            {"ephemeral": True},
        )
    ]


@pytest.mark.asyncio
async def test_context_menu_success_followup_uses_feature_display_name() -> None:
    message = FakeMessage()

    async def process_valid_text(
        _message: FakeMessage,
        _feature_channel_context: object,
    ) -> object:
        return MessageUpsertOutcome.processed("{'ok': true}")

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

    await MessageUpsertFeatureChannelBase.upsert_from_content_menu(
        subject, interaction, message
    )

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
        return MessageUpsertOutcome.missing_config()

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

    await MessageUpsertFeatureChannelBase.upsert_from_content_menu(
        subject, interaction, message
    )

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
        return SimpleNamespace(guild_id=guild_id, channel_id=channel_id)

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

    subject = register_message_listener_subject(
        _get_message_feature_channel_context_or_none=get_message_context,
        _process_feature_channel_message_with_outcome=raise_google_sheets_error,
        _refresh_auto_guide_if_enabled=_noop_async,
        feature_name="team_register",
        bot=SimpleNamespace(user=bot_user),
        logger=NullLogger(),
    )

    await MessageUpsertFeatureChannelBase.on_message(subject, message)

    assert message.removed_reactions == [(config.PROCESSING_EMOJI, bot_user)]
    assert message.added_reactions == [
        config.PROCESSING_EMOJI,
        config.WARNING_EMOJI,
        "🛠️",
    ]
    assert message.reaction_events == [
        ("add", config.PROCESSING_EMOJI),
        ("add", config.WARNING_EMOJI),
        ("add", "🛠️"),
        ("remove", config.PROCESSING_EMOJI, bot_user),
    ]


@pytest.mark.asyncio
async def test_message_listener_marks_worksheet_contract_error() -> None:
    bot_user = object()
    message = FakeRegisterMessage()

    async def get_message_context(_message: FakeRegisterMessage) -> object:
        return SimpleNamespace(guild_id=111, channel_id=222)

    async def raise_contract_error(
        message: FakeRegisterMessage,
        _feature_channel_context: object,
    ) -> None:
        await message.add_reaction(config.PROCESSING_EMOJI)
        required_unique_header_index([], "required")

    subject = register_message_listener_subject(
        _get_message_feature_channel_context_or_none=get_message_context,
        _process_feature_channel_message_with_outcome=raise_contract_error,
        _refresh_auto_guide_if_enabled=_noop_async,
        feature_name="team_register",
        bot=SimpleNamespace(user=bot_user),
        logger=NullLogger(),
    )

    await MessageUpsertFeatureChannelBase.on_message(subject, message)

    assert message.added_reactions == [
        config.PROCESSING_EMOJI,
        config.WARNING_EMOJI,
        "📏",
    ]
    assert message.removed_reactions == [(config.PROCESSING_EMOJI, bot_user)]


@pytest.mark.asyncio
async def test_message_listener_marks_internal_error_and_preserves_traceback() -> None:
    bot_user = object()
    message = FakeRegisterMessage()
    internal_error = RuntimeError("internal bug")

    async def get_message_context(_message: FakeRegisterMessage) -> object:
        return SimpleNamespace(guild_id=111, channel_id=222)

    async def raise_internal_error(
        message: FakeRegisterMessage,
        _feature_channel_context: object,
    ) -> None:
        await message.add_reaction(config.PROCESSING_EMOJI)
        raise internal_error

    subject = register_message_listener_subject(
        _get_message_feature_channel_context_or_none=get_message_context,
        _process_feature_channel_message_with_outcome=raise_internal_error,
        _refresh_auto_guide_if_enabled=_noop_async,
        feature_name="team_register",
        bot=SimpleNamespace(user=bot_user),
        logger=NullLogger(),
    )

    with pytest.raises(RuntimeError) as exc_info:
        await MessageUpsertFeatureChannelBase.on_message(subject, message)

    assert exc_info.value is internal_error
    assert exc_info.traceback[-1].name == "raise_internal_error"
    assert message.added_reactions == [
        config.PROCESSING_EMOJI,
        config.WARNING_EMOJI,
        "🚧",
    ]
    assert message.removed_reactions == [(config.PROCESSING_EMOJI, bot_user)]


@pytest.mark.asyncio
async def test_listener_reaction_failure_does_not_hide_internal_error() -> None:
    internal_error = RuntimeError("internal bug")
    reaction_error = RuntimeError("reaction delivery failed")

    class ReactionFailingMessage(FakeRegisterMessage):
        async def add_reaction(self, emoji: str) -> None:
            await super().add_reaction(emoji)
            if emoji != config.PROCESSING_EMOJI:
                raise reaction_error

    message = ReactionFailingMessage()

    async def get_message_context(_message: FakeRegisterMessage) -> object:
        return SimpleNamespace(guild_id=111, channel_id=222)

    async def raise_internal_error(
        message: FakeRegisterMessage,
        _feature_channel_context: object,
    ) -> None:
        await message.add_reaction(config.PROCESSING_EMOJI)
        raise internal_error

    logger = RecordingLogger()
    subject = register_message_listener_subject(
        _get_message_feature_channel_context_or_none=get_message_context,
        _process_feature_channel_message_with_outcome=raise_internal_error,
        _refresh_auto_guide_if_enabled=_noop_async,
        feature_name="team_register",
        bot=SimpleNamespace(user=object()),
        logger=logger,
    )

    with pytest.raises(RuntimeError) as exc_info:
        await MessageUpsertFeatureChannelBase.on_message(subject, message)

    assert exc_info.value is internal_error
    assert len(logger.exceptions) == 2


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
    subject._get_register_feature_channel_context_or_none = failing_context

    await RegisterFeatureChannelUserBase._delete_user_data_after_confirmation(
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

    await RegisterFeatureChannelUserBase.send_guide_message(
        subject, interaction, "team.guide"
    )

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

    await RegisterFeatureChannelUserBase.send_guide_message(
        subject, interaction, "team.guide"
    )

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
        await RegisterFeatureChannelUserBase.send_guide_message(
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
        register_feature_channel_base,
        "get_auto_guide_state",
        fake_get_auto_guide_state,
        raising=False,
    )

    result = await RegisterFeatureChannelBase._disable_auto_guide_and_delete_message(
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
        register_feature_channel_base,
        "get_auto_guide_state",
        fake_get_auto_guide_state,
        raising=False,
    )

    result = await RegisterFeatureChannelBase._disable_auto_guide_and_delete_message(
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
        register_feature_channel_base,
        "get_auto_guide_state",
        fake_get_auto_guide_state,
        raising=False,
    )

    result = await RegisterFeatureChannelBase._disable_auto_guide_and_delete_message(
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
        register_feature_channel_base,
        "get_auto_guide_state",
        fake_get_auto_guide_state,
        raising=False,
    )

    result = await RegisterFeatureChannelBase._disable_auto_guide_and_delete_message(
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

    async def fake_get_register_feature_channel_context(source: object) -> object:
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
    subject._get_register_feature_channel_context = (
        fake_get_register_feature_channel_context
    )
    subject._build_settings_panel = fake_build_settings_panel
    subject._refresh_auto_guide_if_enabled = fake_refresh_auto_guide
    subject._disable_auto_guide_and_delete_message = fail_disable
    monkeypatch.setattr(
        register_feature_channel_base,
        "get_or_create_auto_guide_state",
        fake_get_or_create_auto_guide_state,
        raising=False,
    )

    await RegisterFeatureChannelBase.toggle_auto_guide_from_settings(
        subject,
        interaction,
        enabled=True,
        current_view=current_view,
        feature_config=fresh_config,
    )

    assert events == ["get_context", "state", "save:True", "build_panel", "refresh"]
    assert current_view.is_finished()
    assert len(interaction.original_response_edits) == 1
    edit_kwargs = interaction.original_response_edits[0][1]
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

    async def fake_get_register_feature_channel_context(_source: object) -> object:
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
    subject._get_register_feature_channel_context = (
        fake_get_register_feature_channel_context
    )
    subject._build_settings_panel = fake_build_settings_panel
    subject._refresh_auto_guide_if_enabled = fake_refresh
    subject._disable_auto_guide_and_delete_message = fake_disable_auto_guide
    monkeypatch.setattr(
        register_feature_channel_base,
        "get_or_create_auto_guide_state",
        fake_get_or_create_auto_guide_state,
        raising=False,
    )

    await RegisterFeatureChannelBase.toggle_auto_guide_from_settings(
        subject,
        interaction,
        enabled=False,
        current_view=SettingsTimeoutView(),
        feature_config=fresh_config,
    )

    assert events == ["get_context", "state", "save:False", "build_panel", "disable"]
    assert interaction.followup.messages == [
        (
            register_feature_channel_base.LATEST_GUIDE_DELETE_FAILED_WARNING,
            {"ephemeral": True},
        )
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

    async def fake_get_register_feature_channel_context(_source: object) -> object:
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
    subject._get_register_feature_channel_context = (
        fake_get_register_feature_channel_context
    )
    subject._build_settings_panel = fake_build_settings_panel
    subject._disable_auto_guide_and_delete_message = fake_disable_auto_guide
    monkeypatch.setattr(
        register_feature_channel_base,
        "get_or_create_auto_guide_state",
        fake_get_or_create_auto_guide_state,
        raising=False,
    )

    await RegisterFeatureChannelBase.toggle_auto_guide_from_settings(
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
        (
            register_feature_channel_base.LATEST_GUIDE_DELETE_FAILED_WARNING,
            {"ephemeral": True},
        )
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
        register_feature_channel_base,
        "get_auto_guide_state",
        fake_get_auto_guide_state,
        raising=False,
    )
    subject._get_configured_register_feature_channel_context = unexpected_config_lookup

    result = await RegisterFeatureChannelBase._refresh_auto_guide_if_enabled(
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
        register_feature_channel_base,
        "get_auto_guide_state",
        fake_get_auto_guide_state,
        raising=False,
    )
    subject._get_configured_register_feature_channel_context = missing_config_lookup

    result = await RegisterFeatureChannelBase._refresh_auto_guide_if_enabled(
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
    configured_context = ConfiguredRegisterFeatureChannelContext(
        guild_id=111,
        channel_id=222,
        feature_channel=context.feature_channel,
        manager=context.manager,
        feature_config=SimpleNamespace(
            sheet_url="https://sheet.example",
            landing_worksheet_id=None,
        ),
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
        register_feature_channel_base,
        "get_auto_guide_state",
        fake_get_auto_guide_state,
        raising=False,
    )
    monkeypatch.setattr(
        register_feature_channel_base,
        "get_or_create_auto_guide_state",
        fake_get_or_create_auto_guide_state,
        raising=False,
    )
    monkeypatch.setattr(
        register_feature_channel_base,
        "get_announcement_languages",
        fake_get_announcement_languages,
        raising=False,
    )
    monkeypatch.setattr(
        register_feature_channel_base,
        "render_message_template",
        fake_render_message_template,
    )
    monkeypatch.setattr(
        FeatureChannelMessageState,
        "get_or_none",
        fake_message_state_get_or_none,
    )
    subject._get_configured_register_feature_channel_context = configured_lookup

    result = await RegisterFeatureChannelBase._refresh_auto_guide_if_enabled(
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
    configured_context = ConfiguredRegisterFeatureChannelContext(
        guild_id=111,
        channel_id=222,
        feature_channel=context.feature_channel,
        manager=context.manager,
        feature_config=SimpleNamespace(
            sheet_url="https://sheet.example",
            landing_worksheet_id=None,
        ),
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
        register_feature_channel_base,
        "get_auto_guide_state",
        fake_get_auto_guide_state,
        raising=False,
    )
    monkeypatch.setattr(
        register_feature_channel_base,
        "get_or_create_auto_guide_state",
        fake_get_or_create_auto_guide_state,
        raising=False,
    )
    monkeypatch.setattr(
        register_feature_channel_base,
        "get_announcement_languages",
        fake_get_announcement_languages,
        raising=False,
    )
    monkeypatch.setattr(
        register_feature_channel_base,
        "render_message_template",
        fake_render_message_template,
    )
    monkeypatch.setattr(
        FeatureChannelMessageState,
        "get_or_none",
        fake_message_state_get_or_none,
    )
    subject._get_configured_register_feature_channel_context = configured_lookup

    result = await RegisterFeatureChannelBase._refresh_auto_guide_if_enabled(
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
    configured_context = ConfiguredRegisterFeatureChannelContext(
        guild_id=111,
        channel_id=222,
        feature_channel=context.feature_channel,
        manager=context.manager,
        feature_config=SimpleNamespace(
            sheet_url="https://sheet.example",
            landing_worksheet_id=None,
        ),
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
        register_feature_channel_base,
        "get_auto_guide_state",
        fake_get_auto_guide_state,
        raising=False,
    )
    monkeypatch.setattr(
        register_feature_channel_base,
        "get_or_create_auto_guide_state",
        fake_get_or_create_auto_guide_state,
        raising=False,
    )
    monkeypatch.setattr(
        register_feature_channel_base,
        "get_announcement_languages",
        fake_get_announcement_languages,
        raising=False,
    )
    monkeypatch.setattr(
        register_feature_channel_base,
        "render_message_template",
        fake_render_message_template,
    )
    monkeypatch.setattr(
        FeatureChannelMessageState,
        "get_or_none",
        fake_message_state_get_or_none,
    )
    subject._get_configured_register_feature_channel_context = configured_lookup

    result = await RegisterFeatureChannelBase._refresh_auto_guide_if_enabled(
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
    configured_context = ConfiguredRegisterFeatureChannelContext(
        guild_id=111,
        channel_id=222,
        feature_channel=context.feature_channel,
        manager=context.manager,
        feature_config=SimpleNamespace(
            sheet_url="https://sheet.example",
            landing_worksheet_id=None,
        ),
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
        register_feature_channel_base,
        "get_auto_guide_state",
        fake_get_auto_guide_state,
    )
    monkeypatch.setattr(
        register_feature_channel_base,
        "get_or_create_auto_guide_state",
        fake_get_or_create_auto_guide_state,
    )
    monkeypatch.setattr(
        register_feature_channel_base,
        "get_announcement_languages",
        fake_get_announcement_languages,
    )
    monkeypatch.setattr(
        register_feature_channel_base,
        "render_message_template",
        fake_render_message_template,
    )
    monkeypatch.setattr(
        FeatureChannelMessageState,
        "get_or_none",
        fake_message_state_get_or_none,
    )
    subject._get_configured_register_feature_channel_context = configured_lookup

    result = await RegisterFeatureChannelBase._refresh_auto_guide_if_enabled(
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
    configured_context = ConfiguredRegisterFeatureChannelContext(
        guild_id=111,
        channel_id=222,
        feature_channel=context.feature_channel,
        manager=context.manager,
        feature_config=SimpleNamespace(
            sheet_url="https://sheet.example",
            landing_worksheet_id=None,
        ),
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
        register_feature_channel_base,
        "get_auto_guide_state",
        fake_get_auto_guide_state,
        raising=False,
    )
    monkeypatch.setattr(
        register_feature_channel_base,
        "get_or_create_auto_guide_state",
        fake_get_or_create_auto_guide_state,
        raising=False,
    )
    monkeypatch.setattr(
        register_feature_channel_base,
        "get_announcement_languages",
        fake_get_announcement_languages,
        raising=False,
    )
    monkeypatch.setattr(
        register_feature_channel_base,
        "render_message_template",
        fake_render_message_template,
    )
    monkeypatch.setattr(
        FeatureChannelMessageState,
        "get_or_none",
        fake_message_state_get_or_none,
    )
    subject._get_configured_register_feature_channel_context = configured_lookup

    result = await RegisterFeatureChannelBase._refresh_auto_guide_if_enabled(
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
        register_feature_channel_base,
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
async def test_render_localized_embeds_preserves_language_order_and_footer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_get_announcement_languages(
        _guild_id: int,
        _logger: object,
    ) -> list[str]:
        return ["en", "ja", "zh_tw"]

    monkeypatch.setattr(
        register_feature_channel_base,
        "get_announcement_languages",
        fake_get_announcement_languages,
    )
    subject = ShiftRegister(fake_bot())

    def values_for_language(language: str) -> dict[str, object]:
        weekdays = {
            "ja": ("水", "木", "金"),
            "zh_tw": ("三", "四", "五"),
            "en": ("Wed", "Thu", "Fri"),
        }
        submission, draft, final = weekdays[language]
        return {
            "day_number": 2,
            "submission_deadline": SimpleNamespace(day=12, weekday=submission, hour=21),
            "draft_shift_proposal": SimpleNamespace(day=13, weekday=draft, hour=20),
            "final_shift_notice": SimpleNamespace(day=14, weekday=final, hour=18),
        }

    embeds = await subject._render_localized_embeds(
        111,
        template_key="shift.deadline_close",
        values_for_language=values_for_language,
        include_footer=True,
    )

    assert [embed.title for embed in embeds] == [
        "Day 2 | Shift registration is now closed 🙇\n",
        "2日目｜シフト募集を締め切りました 🙇\n",
        "第2天｜班表登記已截止 🙇\n",
    ]
    assert all(embed.color.value == config.DEFAULT_EMBED_COLOR for embed in embeds)
    assert all(embed.footer.text for embed in embeds)


def test_shift_deadline_close_view_has_only_entry_sheet_link() -> None:
    view = ShiftDeadlineCloseView(
        "https://docs.google.com/spreadsheets/d/example/edit?gid=444#gid=444"
    )

    assert len(view.children) == 1
    button = view.children[0]
    assert button.style is ButtonStyle.link
    assert button.label == "Google Sheets"
    assert str(button.emoji) == "👀"
    assert button.url.endswith("#gid=444")
    assert button.custom_id is None


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
        "cogs.base.register_feature_channel_base.render_announcement_messages",
        fake_render_announcement_messages,
    )
    monkeypatch.setattr(
        "cogs.base.register_feature_channel_base.save_manual_guide_anchor",
        _noop_async,
    )

    subject = TeamRegister(fake_bot())
    subject.bot.user = SimpleNamespace(mention="@Rhoboto")
    interaction = FakeInteraction(locale="en-US")
    interaction.followup = IdRecordingFollowup()

    await subject.send_guide_message(interaction)

    assert captured_sheet_urls == [
        "https://docs.google.com/spreadsheets/d/abc/edit?gid=333#gid=333"
    ]
    assert interaction.followup.messages == [
        ("en guide", {"ephemeral": False, "wait": True})
    ]


@pytest.mark.asyncio
async def test_shift_public_guide_uses_entry_worksheet_gid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(FeatureChannel, "get", fake_feature_channel_get)
    monkeypatch.setattr(ShiftRegister, "ManagerType", ConfiguredShiftHelpUrlManager)
    captured_values: dict[str, object] = {}

    async def fake_render_announcement_messages(
        template_key: str,
        guild_id: int,
        _logger: object = None,
        **values: object,
    ) -> list[RenderedAnnouncement]:
        assert template_key == "shift.guide"
        assert guild_id == 111
        captured_values.update(values)
        return [RenderedAnnouncement(language="en", content="en guide")]

    monkeypatch.setattr(
        "cogs.base.register_feature_channel_base.render_announcement_messages",
        fake_render_announcement_messages,
    )
    monkeypatch.setattr(
        "cogs.base.register_feature_channel_base.save_manual_guide_anchor",
        _noop_async,
    )

    subject = ShiftRegister(fake_bot())
    subject.bot.user = SimpleNamespace(mention="@Rhoboto")
    interaction = FakeInteraction(locale="en-US")
    interaction.followup = IdRecordingFollowup()

    await subject.send_guide_message(interaction)

    assert captured_values["sheet_url"] == (
        "https://docs.google.com/spreadsheets/d/abc/edit?gid=444#gid=444"
    )
    assert captured_values["team_source_channel_id"] == 987
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
        "cogs.base.register_feature_channel_user_base.render_message_template",
        fake_render_message_template,
    )

    subject = Team(fake_bot())
    subject.bot.user = SimpleNamespace(mention="@Rhoboto")
    interaction = FakeInteraction(locale="en-US")

    await subject.send_guide_message(interaction, TeamRegister.guide_template_key)

    assert captured_values["sheet_url"] == (
        "https://docs.google.com/spreadsheets/d/abc/edit?gid=333#gid=333"
    )
    assert interaction.followup.messages == [
        ("rendered team guide", {"ephemeral": True})
    ]


@pytest.mark.asyncio
async def test_shift_user_guide_uses_entry_worksheet_gid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(FeatureChannel, "get", fake_feature_channel_get)
    monkeypatch.setattr(Shift, "ManagerType", ConfiguredShiftHelpUrlManager)
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
        "cogs.base.register_feature_channel_user_base.render_message_template",
        fake_render_message_template,
    )

    subject = Shift(fake_bot())
    subject.bot.user = SimpleNamespace(mention="@Rhoboto")
    interaction = FakeInteraction(locale="en-US")

    await subject.send_guide_message(interaction, ShiftRegister.guide_template_key)

    assert captured_values["sheet_url"] == (
        "https://docs.google.com/spreadsheets/d/abc/edit?gid=444#gid=444"
    )
    assert captured_values["team_source_channel_id"] == 987
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
        "cogs.base.register_feature_channel_base.render_announcement_messages",
        fake_render_announcement_messages,
    )
    monkeypatch.setattr(
        "cogs.base.register_feature_channel_base.save_manual_guide_anchor",
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

    await RegisterFeatureChannelBase.send_guide_message(subject, interaction)

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
        "cogs.base.register_feature_channel_base.render_announcement_messages",
        fake_render_announcement_messages,
    )
    monkeypatch.setattr(
        "cogs.base.register_feature_channel_base.save_manual_guide_anchor",
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

    await RegisterFeatureChannelBase.send_guide_message(subject, interaction)

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

    await RegisterFeatureChannelBase.send_guide_message(subject, interaction)

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
        "cogs.base.register_feature_channel_base.render_announcement_messages",
        fake_render_announcement_messages,
    )

    async def fake_save_manual_guide_anchor(
        feature_channel: object,
        message_id: int,
    ) -> None:
        saved_anchors.append((feature_channel, message_id))

    monkeypatch.setattr(
        "cogs.base.register_feature_channel_base.save_manual_guide_anchor",
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

    await RegisterFeatureChannelBase.send_guide_message(subject, interaction)

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
        "cogs.base.register_feature_channel_base.render_announcement_messages",
        fake_render_announcement_messages,
    )
    monkeypatch.setattr(
        "cogs.base.register_feature_channel_base.save_manual_guide_anchor",
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

    await RegisterFeatureChannelBase.send_guide_message(subject, interaction)

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
        "cogs.base.register_feature_channel_base.render_announcement_messages",
        fake_render_announcement_messages,
    )
    monkeypatch.setattr(
        "cogs.base.register_feature_channel_base.save_manual_guide_anchor",
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
        await RegisterFeatureChannelBase.send_guide_message(subject, interaction)

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
    subject._get_register_feature_channel_context = failing_context

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
    interaction = FakeInteraction(locale="ja")
    interaction.followup.send = fail_delivery
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
    subject._get_register_feature_channel_context = failing_context

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
async def test_team_summary_pre_save_database_failure_reports_ordinary_storage_error(
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
    contents = interaction_contents(interaction)
    assert len(contents) == 1
    assert_safe_storage_content(contents[0])
    assert "Some changes may have been saved" not in contents[0]
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
    assert manager.member_by_names == {
        "alice": members[0],
        "bob": members[1],
    }
    assert interaction.followup.messages == [(None, {"embed": summary_embed})]


@pytest.mark.asyncio
async def test_team_summary_waits_then_refreshes_config_inside_channel_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(FeatureChannel, "get", fake_feature_channel_get)
    monkeypatch.setattr(
        "cogs.team_register.build_summary_embed",
        lambda _summary: SimpleNamespace(title="summary"),
    )
    channel_lock = GatedRecordingLock()
    interaction = FakeInteraction()
    subject = feature_channel_context_subject(
        feature_name="team_register",
        ManagerType=SummaryManager,
        sheet_write_lock=channel_lock,
    )

    task = asyncio.create_task(TeamRegister.summary.callback(subject, interaction))
    await channel_lock.attempted.wait()
    manager = SummaryManager.last_instance
    assert manager is not None
    manager.current_sheet_url = (
        "https://docs.google.com/spreadsheets/d/fresh-team-summary/edit"
    )
    channel_lock.release.set()
    await task

    assert manager.fresh_sheet_urls == [manager.current_sheet_url]
    assert channel_lock.keys == [222]


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
@pytest.mark.parametrize("feature_name", ["team", "shift"])
async def test_initial_settings_invalid_url_uses_safe_storage_response_without_locks(
    monkeypatch: pytest.MonkeyPatch,
    feature_name: str,
) -> None:
    manager = InvalidInitialSettingsManager()
    channel_lock = RecordingLock()
    interaction = FakeInteraction()

    if feature_name == "team":
        monkeypatch.setattr(
            ui_team_register,
            "TEAM_REGISTER_SHEET_WRITE_LOCK",
            channel_lock,
        )
        modal = TeamRegisterSheetModal(
            manager,
            sheet_url="not a Google Sheet URL",
            team_worksheet_titles=["Main Team"],
            summary_worksheet_title="Team Summary",
        )
    else:
        monkeypatch.setattr(
            ui_shift_register,
            "SHIFT_REGISTER_SHEET_WRITE_LOCK",
            channel_lock,
        )
        modal = ShiftRegisterSheetModal(
            manager,
            sheet_url="not a Google Sheet URL",
            entry_worksheet_title="Entry",
            draft_worksheet_title="Draft",
            final_schedule_worksheet_title="Final Schedule",
        )

    await modal.on_submit(interaction)

    assert interaction.response.deferred == [True]
    assert manager.upsert_calls == 0
    assert channel_lock.keys == []
    assert len(interaction.followup.messages) == 1
    content, kwargs = interaction.followup.messages[0]
    assert content is not None
    assert "Check the Google Sheet link and save the settings again." in content
    assert "Reference: `STG-" in content
    assert "Invalid Google Sheet URL" not in content
    assert kwargs == {"ephemeral": True}


@pytest.mark.asyncio
async def test_team_delete_data_performs_already_locked_manager_delete() -> None:
    manager = IntegratedTeamDeleteManager()
    user = UserInfo("alice", "Alice")

    await Team._delete_user_data(
        SimpleNamespace(),
        manager,
        user,
        manager.metadata,
    )

    assert manager.events == ["fetch_metadata", "delete"]
    assert manager.fresh_sheet_urls == []
    assert manager.users == [user]


@pytest.mark.asyncio
async def test_team_delete_refreshes_after_waiting_for_channel_lock() -> None:
    manager = IntegratedTeamDeleteManager()
    attempted = asyncio.Event()
    release = asyncio.Event()

    @asynccontextmanager
    async def channel_lock(key: object) -> object:
        assert key == 222
        manager.events.append("channel_lock")
        attempted.set()
        await release.wait()
        yield

    subject = Team(fake_bot())
    subject.FeatureChannelType = SimpleNamespace(sheet_write_lock=channel_lock)

    async def get_feature_channel_context_or_none(**_kwargs: object) -> object:
        return object()

    async def get_configured_context(_context: object) -> SimpleNamespace:
        return SimpleNamespace(channel_id=222, manager=manager)

    subject._get_register_feature_channel_context_or_none = (
        get_feature_channel_context_or_none
    )
    subject._get_configured_register_feature_channel_context = get_configured_context
    interaction = FakeInteraction(locale="en-US", user_id=333)
    source = require_guild_channel_source(
        interaction,
        action="delete feature user data",
    )

    task = asyncio.create_task(
        RegisterFeatureChannelUserBase._delete_user_data_after_confirmation(
            subject,
            interaction,
            source,
        )
    )
    await attempted.wait()
    manager.current_sheet_url = (
        "https://docs.google.com/spreadsheets/d/switched-team-delete/edit"
    )
    release.set()
    result = await task

    assert manager.events == [
        "channel_lock",
        "fresh_config",
        "fetch_metadata",
        "delete",
    ]
    assert manager.fresh_sheet_urls == [manager.current_sheet_url]
    assert result is not None


@pytest.mark.asyncio
async def test_shift_delete_data_uses_transaction_metadata() -> None:
    manager = IntegratedShiftDeleteManager()
    user = UserInfo("alice", "Alice")
    await Shift._delete_user_data(
        SimpleNamespace(),
        manager,
        user,
        manager.metadata,
    )

    assert manager.events == ["delete"]
    assert manager.fresh_sheet_urls == []
    assert manager.users == [user]


@pytest.mark.asyncio
async def test_shift_delete_fetches_metadata_inside_channel_transaction() -> None:
    manager = IntegratedShiftDeleteManager()

    @asynccontextmanager
    async def channel_lock(key: object) -> object:
        assert key == 222
        manager.events.append("channel_lock")
        yield

    subject = Shift(fake_bot())
    subject.FeatureChannelType = SimpleNamespace(sheet_write_lock=channel_lock)

    async def get_feature_channel_context_or_none(**_kwargs: object) -> object:
        return object()

    async def get_configured_context(_context: object) -> SimpleNamespace:
        return SimpleNamespace(channel_id=222, manager=manager)

    subject._get_register_feature_channel_context_or_none = (
        get_feature_channel_context_or_none
    )
    subject._get_configured_register_feature_channel_context = get_configured_context
    interaction = FakeInteraction(locale="en-US", user_id=333)
    source = require_guild_channel_source(
        interaction,
        action="delete feature user data",
    )

    result = await RegisterFeatureChannelUserBase._delete_user_data_after_confirmation(
        subject,
        interaction,
        source,
    )

    assert manager.events == [
        "channel_lock",
        "fresh_config",
        "fetch_metadata",
        "delete",
    ]
    assert result is not None


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
        register_feature_channel_user_base,
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

    await RegisterFeatureChannelUserBase.delete_callback(subject, interaction)

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
        register_feature_channel_user_base,
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

    await RegisterFeatureChannelUserBase.delete_callback(subject, interaction)

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

    result = await RegisterFeatureChannelUserBase._delete_user_data_after_confirmation(
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
        register_feature_channel_user_base,
        "ConfirmDeleteUserDataView",
        ConfirmView,
    )
    subject = feature_channel_context_subject(
        feature_name="team_register",
        feature_display_name="Team Register",
    )
    subject._delete_user_data_after_confirmation = fake_delete_after_confirmation
    interaction = FakeInteraction(locale="en-US", user_id=333)

    await RegisterFeatureChannelUserBase.delete_callback(subject, interaction)

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

    result = await RegisterFeatureChannelUserBase._delete_user_data_after_confirmation(
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

    async def fake_get_register_feature_channel_context(_source: object) -> object:
        return object()

    async def fake_get_configured_register_feature_channel_context(
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
    subject._get_register_feature_channel_context_or_none = (
        fake_get_enabled_context_or_none
    )
    subject._get_register_feature_channel_context = (
        fake_get_register_feature_channel_context
    )
    subject._get_configured_register_feature_channel_context = (
        fake_get_configured_register_feature_channel_context
    )
    interaction = FakeInteraction(locale="en-US")
    source = require_guild_channel_source(
        interaction,
        action="delete feature user data",
    )

    result = await RegisterFeatureChannelUserBase._delete_user_data_after_confirmation(
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
    subject._get_register_feature_channel_context_or_none = (
        fake_get_enabled_context_or_none
    )
    subject._get_register_feature_channel_context = fail_stale_row_lookup
    interaction = FakeInteraction(locale="en-US")
    source = require_guild_channel_source(
        interaction,
        action="delete feature user data",
    )

    result = await RegisterFeatureChannelUserBase._delete_user_data_after_confirmation(
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

    result = await RegisterFeatureChannelUserBase._delete_user_data_after_confirmation(
        subject,
        interaction,
        source,
    )

    assert result == (
        "✅ 已成功刪除您在 Google Sheets 中的隊伍編成登記資料。"
        "若也想移除 Discord 上的原始登記訊息，"
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

    result = await RegisterFeatureChannelUserBase._delete_user_data_after_confirmation(
        subject,
        interaction,
        source,
    )

    assert result == (
        "✅ Google Sheets 上の編成登録のデータを正常に削除しました。"
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
        await RegisterFeatureChannelUserBase.delete_callback(subject, interaction)

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
    view = RegisterFeatureChannelUserBase.build_auto_guide_delete_view(subject)
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
    assert [name for name, _listener in team_register.get_listeners()] == ["on_message"]
    assert [name for name, _listener in shift_register.get_listeners()] == [
        "on_message"
    ]


def test_team_and_shift_use_inherited_message_upsert_orchestration() -> None:
    assert not hasattr(FeatureChannelBase, "process_upsert_from_message")
    assert not hasattr(FeatureChannelBase, "_process_upsert_from_message_with_outcome")
    assert not hasattr(
        FeatureChannelBase, "_process_feature_channel_message_with_outcome"
    )
    assert not hasattr(FeatureChannelBase, "on_message")
    assert not hasattr(FeatureChannelBase, "upsert_from_content_menu")
    assert hasattr(
        MessageUpsertFeatureChannelBase,
        "_process_feature_channel_message_with_outcome",
    )
    assert hasattr(MessageUpsertFeatureChannelBase, "_parse_message_submission")
    assert hasattr(MessageUpsertFeatureChannelBase, "on_message")
    assert hasattr(MessageUpsertFeatureChannelBase, "upsert_from_content_menu")
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


def test_lifecycle_only_feature_does_not_register_message_surfaces() -> None:
    class LifecycleOnlyFeature(
        FeatureChannelBase,
        group_name="lifecycle_only_test",
    ):
        feature_name = "lifecycle_only_test"
        feature_display_name = "Lifecycle Only Test"

        @override
        async def setup_after_enable(self, interaction: Interaction) -> None:
            del interaction

    subject = LifecycleOnlyFeature(fake_bot())

    assert not hasattr(subject, "context_menu")
    assert subject.get_listeners() == []


def test_four_generic_message_base_registers_only_shared_message_surfaces() -> None:
    subject = RecordingMessageSubject(MessageParseResult.ignored())

    assert [name for name, _listener in subject.get_listeners()] == ["on_message"]
    assert subject.context_menu.name == "Team Register Test Upsert"
    assert subject.context_menu.callback == subject.upsert_from_content_menu


@pytest.mark.asyncio
async def test_team_register_message_upsert_uses_integrated_manager_action() -> None:
    subject = TeamRegister(fake_bot())
    lock = RecordingLock()
    subject.sheet_write_lock = lock
    manager = OrderedTeamUpsertManager(
        feature_channel_row("team_register"),
        "service.json",
    )
    context = ordered_team_upsert_context(manager)
    submission, user_info = team_register_submission()
    message = FakeRegisterMessage(content="150/740/33")

    await subject._process_configured_message_submission(
        message,
        context,
        submission,
        user_info,
    )

    assert manager.events == ["upsert"]
    assert lock.keys == [222]


@pytest.mark.asyncio
async def test_team_message_waits_then_refreshes_config_inside_channel_lock() -> None:
    subject = TeamRegister(fake_bot())
    channel_lock = GatedRecordingLock()
    subject.sheet_write_lock = channel_lock
    manager = OrderedTeamUpsertManager(
        feature_channel_row("team_register"),
        "service.json",
    )
    context = ordered_team_upsert_context(manager)
    submission, user_info = team_register_submission()
    message = FakeRegisterMessage(content="150/740/33")

    task = asyncio.create_task(
        subject._process_configured_message_submission(
            message,
            context,
            submission,
            user_info,
        )
    )
    await channel_lock.attempted.wait()
    manager.current_sheet_url = (
        "https://docs.google.com/spreadsheets/d/fresh-team-message/edit"
    )
    channel_lock.release.set()
    await task

    assert manager.fresh_sheet_urls == [manager.current_sheet_url]
    assert channel_lock.keys == [222]


@pytest.mark.asyncio
async def test_shift_message_waits_then_refreshes_config_inside_channel_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        FeatureChannel,
        "get_or_none",
        fake_enabled_feature_channel_by_id,
    )
    subject = ShiftRegister(fake_bot())
    channel_lock = GatedRecordingLock()
    subject.sheet_write_lock = channel_lock
    manager = OrderedShiftUpsertManager(
        feature_channel_row("shift_register"),
        "service.json",
    )
    context = ordered_shift_upsert_context(manager)
    submission, user_info = shift_register_submission()
    message = FakeRegisterMessage(content="4-8")

    task = asyncio.create_task(
        subject._process_configured_message_submission(
            message,
            context,
            submission,
            user_info,
        )
    )
    await channel_lock.attempted.wait()
    manager.current_sheet_url = (
        "https://docs.google.com/spreadsheets/d/fresh-shift-message/edit"
    )
    channel_lock.release.set()
    await task

    assert manager.fresh_sheet_urls == [manager.current_sheet_url]
    assert channel_lock.keys == [222]


@pytest.mark.asyncio
async def test_shift_message_validates_with_fresh_recruitment_ranges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        FeatureChannel,
        "get_or_none",
        fake_enabled_feature_channel_by_id,
    )
    subject = ShiftRegister(fake_bot())
    manager = OrderedShiftUpsertManager(
        feature_channel_row("shift_register"),
        "service.json",
    )

    async def get_fresh_sheet_config() -> ShiftRegisterConfig:
        return shift_test_config(
            manager.current_sheet_url,
            recruitment_time_ranges=[{"start": 10, "end": 12}],
        )

    monkeypatch.setattr(manager, "get_fresh_sheet_config", get_fresh_sheet_config)
    context = ordered_shift_upsert_context(manager)
    submission, user_info = shift_register_submission()
    message = FakeRegisterMessage(content="4-8")

    result = await subject._process_configured_message_submission(
        message,
        context,
        submission,
        user_info,
    )

    assert result is None
    assert manager.events == []
    assert message.reaction_events == [
        ("add", config.WARNING_EMOJI),
        ("add", config.CONFUSED_EMOJI),
    ]


@pytest.mark.asyncio
async def test_team_register_message_upsert_preserves_pre_success_database_error() -> (
    None
):
    subject = TeamRegister(fake_bot())
    raw_error = private_database_error()
    manager = OrderedTeamUpsertManager(
        feature_channel_row("team_register"),
        "service.json",
        ensure_error=raw_error,
    )
    context = ordered_team_upsert_context(manager)
    submission, user_info = team_register_submission()
    message = FakeRegisterMessage(content="150/740/33")

    with pytest.raises(DBConnectionError) as exc_info:
        await subject._process_configured_message_submission(
            message,
            context,
            submission,
            user_info,
        )

    assert manager.events == ["upsert"]
    assert exc_info.value is raw_error


@pytest.mark.asyncio
async def test_team_register_message_upsert_preserves_classified_storage_error() -> (
    None
):
    error = StorageError(
        StorageErrorKind.GOOGLE_SHEETS_TRANSIENT,
    )
    subject = TeamRegister(fake_bot())
    manager = OrderedTeamUpsertManager(
        feature_channel_row("team_register"),
        "service.json",
        team_error=error,
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

    raised_error = exc_info.value
    assert manager.events == ["upsert"]
    assert raised_error is error
    assert raised_error.kind is StorageErrorKind.GOOGLE_SHEETS_TRANSIENT


@pytest.mark.asyncio
async def test_team_register_message_upsert_preserves_summary_database_error() -> None:
    subject = TeamRegister(fake_bot())
    raw_error = private_database_error()
    manager = OrderedTeamUpsertManager(
        feature_channel_row("team_register"),
        "service.json",
        summary_error=raw_error,
    )
    context = ordered_team_upsert_context(manager)
    submission, user_info = team_register_submission()
    message = FakeRegisterMessage(content="150/740/33")

    with pytest.raises(DBConnectionError) as exc_info:
        await subject._process_configured_message_submission(
            message,
            context,
            submission,
            user_info,
        )

    assert manager.events == ["upsert"]
    assert exc_info.value is raw_error


@pytest.mark.asyncio
async def test_shift_register_message_upsert_preserves_pre_success_database_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        FeatureChannel,
        "get_or_none",
        fake_enabled_feature_channel_by_id,
    )
    subject = ShiftRegister(fake_bot())
    raw_error = private_database_error()
    manager = OrderedShiftUpsertManager(
        feature_channel_row("shift_register"),
        "service.json",
        ensure_error=raw_error,
    )
    context = ordered_shift_upsert_context(manager)
    submission, user_info = shift_register_submission()
    message = FakeRegisterMessage(content="4-8")

    with pytest.raises(DBConnectionError) as exc_info:
        await subject._process_configured_message_submission(
            message,
            context,
            submission,
            user_info,
        )

    assert manager.events == ["fetch_metadata", "log_missing", "ensure"]
    assert exc_info.value is raw_error


@pytest.mark.asyncio
@pytest.mark.parametrize("ids_changed", [False, True])
async def test_shift_register_message_upsert_failure_uses_actual_id_change(
    *,
    ids_changed: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        FeatureChannel,
        "get_or_none",
        fake_enabled_feature_channel_by_id,
    )
    raw_error = StorageError(StorageErrorKind.GOOGLE_SHEETS_TRANSIENT)
    subject = ShiftRegister(fake_bot())
    manager = OrderedShiftUpsertManager(
        feature_channel_row("shift_register"),
        "service.json",
        upsert_error=raw_error,
        ensure_changes_ids=ids_changed,
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

    assert manager.events == ["fetch_metadata", "log_missing", "ensure", "upsert"]
    error = exc_info.value
    if ids_changed:
        assert error.kind is StorageErrorKind.PARTIAL_SUCCESS
        assert isinstance(error.__cause__, StorageError)
        assert error.__cause__.kind is StorageErrorKind.GOOGLE_SHEETS_TRANSIENT
    else:
        assert error is raw_error


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
        register_feature_channel_base,
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
        register_feature_channel_base,
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


class _TimelineQuery:
    def __init__(self, value: object) -> None:
        self.value = value

    def select_related(self, *_fields: str) -> _TimelineQuery:
        return self

    async def first(self) -> object:
        return self.value

    def __await__(self) -> object:
        async def resolve() -> object:
            return self.value

        return resolve().__await__()


class _TimelineChannel:
    def __init__(self, *, name: str = "shift") -> None:
        self.name = name
        self.send_attempts: list[dict[str, object]] = []
        self.edit_names: list[str] = []

    async def send(self, **kwargs: object) -> SimpleNamespace:
        self.send_attempts.append(kwargs)
        return SimpleNamespace(id=9001)

    async def edit(self, *, name: str) -> None:
        self.edit_names.append(name)
        self.name = name


def _timeline_config(
    config_id: int = 1,
    *,
    enabled: bool = True,
) -> SimpleNamespace:
    feature_channel = SimpleNamespace(
        id=77 + config_id,
        guild_id=111,
        channel_id=222 + config_id,
        feature_name="shift_register",
        is_enabled=True,
    )
    deadline = dt.datetime(2026, 8, 12, 12, tzinfo=dt.UTC)
    return SimpleNamespace(
        id=config_id,
        feature_channel=feature_channel,
        deadline_automation_enabled=enabled,
        sheet_url="https://docs.google.com/spreadsheets/d/example/edit",
        landing_worksheet_id=444,
        entry_worksheet_id=444,
        day_number=2,
        event_date=dt.date(2026, 8, 12),
        submission_deadline_at=deadline,
        draft_shift_proposal_at=dt.datetime(2026, 8, 13, 11, tzinfo=dt.UTC),
        final_shift_notice_at=dt.datetime(2026, 8, 14, 9, tzinfo=dt.UTC),
        recruitment_time_ranges=[{"start": 4, "end": 28}],
    )


class _TimelineBot:
    def __init__(self, channel: object | None = None) -> None:
        self.tree = SimpleNamespace(add_command=lambda _command: None)
        self.user = None
        self.ready = asyncio.Event()
        self.channel = channel

    async def wait_until_ready(self) -> None:
        await self.ready.wait()

    def get_channel(self, _channel_id: int) -> object | None:
        return self.channel


class _BootstrapTimelineManager:
    instances: ClassVar[list[_BootstrapTimelineManager]] = []

    def __init__(self, feature_channel: object, _service_account_path: str) -> None:
        self.feature_channel = feature_channel
        self.reconcile_calls: list[dt.datetime] = []
        self.__class__.instances.append(self)

    async def reconcile_deadline_automation(
        self,
        *,
        now: dt.datetime,
    ) -> SimpleNamespace:
        self.reconcile_calls.append(now)
        return SimpleNamespace(schedule_change=None, auto_close_disabled=False)


@pytest.mark.asyncio
async def test_shift_timeline_bootstrap_waits_and_restores_persisted_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_item = _timeline_config()
    state = SimpleNamespace(
        event_kind=ShiftTimelineEventKind.SUBMISSION_DEADLINE,
        status=ShiftTimelineEventStatus.SCHEDULED,
        scheduled_at=config_item.submission_deadline_at,
        delivery_nonce=123,
    )
    all_query = _TimelineQuery([config_item])
    monkeypatch.setattr(
        ShiftRegisterConfig,
        "all",
        classmethod(lambda _cls: all_query),
    )

    async def fake_state_get_or_none(**_kwargs: object) -> object:
        return state

    monkeypatch.setattr(
        "cogs.shift_register.ShiftTimelineEventState.get_or_none",
        fake_state_get_or_none,
    )
    _BootstrapTimelineManager.instances = []
    bot = _TimelineBot()
    subject = ShiftRegister(bot)
    subject.ManagerType = _BootstrapTimelineManager
    scheduled: list[tuple[object, ...]] = []
    subject._timeline_scheduler.schedule = lambda **kwargs: scheduled.append(  # type: ignore[method-assign]
        tuple(kwargs.values())
    )

    await subject.cog_load()
    await asyncio.sleep(0)
    assert _BootstrapTimelineManager.instances == []
    assert scheduled == []

    bot.ready.set()
    await subject._timeline_bootstrap_task

    assert len(_BootstrapTimelineManager.instances) == 1
    assert len(_BootstrapTimelineManager.instances[0].reconcile_calls) == 1
    assert scheduled == [
        (
            config_item.id,
            ShiftTimelineEventKind.SUBMISSION_DEADLINE,
            config_item.submission_deadline_at,
            123,
        )
    ]
    await subject.cog_unload()


@pytest.mark.asyncio
async def test_shift_timeline_unload_cancels_bootstrap_before_ready() -> None:
    bot = _TimelineBot()
    subject = ShiftRegister(bot)

    await subject.cog_load()
    await subject.cog_unload()

    assert subject._timeline_bootstrap_task is None
    assert subject._pending_message_ids == {}


class _ToggleTimelineManager:
    def __init__(self) -> None:
        self.feature_channel = SimpleNamespace(id=77, guild_id=111, channel_id=222)
        self.config = _timeline_config()
        self.enabled_values: list[bool] = []

    async def get_fresh_sheet_config(self) -> SimpleNamespace:
        return self.config

    async def set_deadline_automation_enabled(
        self,
        *,
        enabled: bool,
        now: dt.datetime,
    ) -> ShiftTimelineScheduleChange:
        del now
        self.enabled_values.append(enabled)
        return ShiftTimelineScheduleChange(
            shift_register_id=self.config.id,
            event_kind=ShiftTimelineEventKind.SUBMISSION_DEADLINE,
            scheduled_at=self.config.submission_deadline_at if enabled else None,
            delivery_nonce=789 if enabled else None,
        )


@pytest.mark.asyncio
async def test_shift_auto_close_toggle_commits_then_applies_schedule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subject = ShiftRegister(fake_bot())
    manager = _ToggleTimelineManager()
    subject.sheet_write_lock = RecordingLock()
    schedule_changes: list[ShiftTimelineScheduleChange] = []
    subject._apply_timeline_schedule_change = schedule_changes.append  # type: ignore[method-assign]
    panel = SettingsPanel(embed=Embed(title="settings"), view=SettingsTimeoutView())

    async def build_panel(*_args: object, **_kwargs: object) -> SettingsPanel:
        return panel

    monkeypatch.setattr(subject, "_build_settings_panel", build_panel)
    stopped: list[bool] = []
    current_view = SettingsTimeoutView()
    current_view.shift_register_manager = manager  # type: ignore[attr-defined]
    current_view.stop = lambda: stopped.append(True)  # type: ignore[method-assign]
    interaction = FakeInteraction()

    await subject._toggle_shift_auto_close(
        interaction,
        enabled=True,
        current_view=current_view,
    )

    assert manager.enabled_values == [True]
    assert subject.sheet_write_lock.keys == [222]
    assert schedule_changes[0].delivery_nonce == 789
    assert stopped == [True]
    assert interaction.original_response_edits[-1][1]["view"] is panel.view


@pytest.mark.asyncio
async def test_shift_deadline_close_sends_then_cleans_up_and_completes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = _TimelineChannel()
    bot = _TimelineBot(channel)
    subject = ShiftRegister(bot)
    config_item = _timeline_config()
    execution = ShiftDeadlineExecution(
        event_state_id=55,
        shift_register_id=config_item.id,
        guild_id=111,
        channel_id=222,
        delivery_nonce=123,
        status=ShiftTimelineEventStatus.SCHEDULED,
        message_id=None,
    )

    class DeadlineManager:
        def __init__(self, feature_channel: object, _service_account_path: str) -> None:
            self.feature_channel = feature_channel
            self.begin_calls = 0
            self.mark_calls: list[tuple[int, int, int]] = []
            self.complete_calls: list[tuple[int, int]] = []

        async def get_fresh_sheet_config(self) -> SimpleNamespace:
            return config_item

        async def begin_submission_deadline_close(
            self,
            **_kwargs: object,
        ) -> ShiftDeadlineExecution | None:
            self.begin_calls += 1
            return execution if self.begin_calls == 1 else None

        async def mark_submission_deadline_sent(
            self,
            *,
            event_state_id: int,
            delivery_nonce: int,
            message_id: int,
        ) -> bool:
            self.mark_calls.append((event_state_id, delivery_nonce, message_id))
            return True

        async def complete_submission_deadline(
            self,
            *,
            event_state_id: int,
            delivery_nonce: int,
        ) -> bool:
            self.complete_calls.append((event_state_id, delivery_nonce))
            return True

    manager_instances: list[DeadlineManager] = []

    def manager_factory(feature_channel: object, path: str) -> DeadlineManager:
        manager = DeadlineManager(feature_channel, path)
        manager_instances.append(manager)
        return manager

    monkeypatch.setattr(subject, "ManagerType", manager_factory)
    monkeypatch.setattr(
        ShiftRegisterConfig,
        "filter",
        classmethod(lambda _cls, **_kwargs: _TimelineQuery(config_item)),
    )
    monkeypatch.setattr(
        register_feature_channel_base,
        "get_announcement_languages",
        lambda _guild_id, _logger: asyncio.sleep(0, result=["zh_tw", "ja", "en"]),
    )
    cleanup_calls: list[object] = []

    async def cleanup(context: object) -> bool:
        cleanup_calls.append(context)
        return True

    subject._disable_auto_guide_and_delete_message = cleanup  # type: ignore[method-assign]
    subject.sheet_write_lock = RecordingLock()

    await subject._handle_timeline_event(
        config_item.id,
        ShiftTimelineEventKind.SUBMISSION_DEADLINE,
        config_item.submission_deadline_at,
        123,
    )

    assert len(channel.send_attempts) == 1
    assert channel.send_attempts[0]["nonce"] == 123
    assert [embed.title for embed in channel.send_attempts[0]["embeds"]] == [
        "第2天｜班表登記已截止 🙇\n",
        "2日目｜シフト募集を締め切りました 🙇\n",
        "Day 2 | Shift registration is now closed 🙇\n",
    ]
    assert channel.edit_names == ["〆shift"]
    assert cleanup_calls
    assert manager_instances[0].mark_calls == [(55, 123, 9001)]
    assert manager_instances[0].complete_calls == [(55, 123)]


@pytest.mark.asyncio
async def test_shift_deadline_close_reuses_cached_message_when_mark_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = _TimelineChannel()
    bot = _TimelineBot(channel)
    subject = ShiftRegister(bot)
    config_item = _timeline_config()
    execution = ShiftDeadlineExecution(
        event_state_id=55,
        shift_register_id=config_item.id,
        guild_id=111,
        channel_id=222,
        delivery_nonce=123,
        status=ShiftTimelineEventStatus.SCHEDULED,
        message_id=None,
    )

    class RetryManager:
        def __init__(self, feature_channel: object, _service_account_path: str) -> None:
            self.feature_channel = feature_channel
            self.calls = 0
            self.mark_calls = 0

        async def get_fresh_sheet_config(self) -> SimpleNamespace:
            return config_item

        async def begin_submission_deadline_close(
            self,
            **_kwargs: object,
        ) -> ShiftDeadlineExecution:
            self.calls += 1
            return execution

        async def mark_submission_deadline_sent(self, **_kwargs: object) -> bool:
            self.mark_calls += 1
            if self.mark_calls == 1:
                error_message = "persist failed"
                raise RuntimeError(error_message)
            return True

        async def complete_submission_deadline(self, **_kwargs: object) -> bool:
            return True

    manager = RetryManager(config_item.feature_channel, "service.json")
    monkeypatch.setattr(subject, "ManagerType", lambda *_args: manager)
    monkeypatch.setattr(
        ShiftRegisterConfig,
        "filter",
        classmethod(lambda _cls, **_kwargs: _TimelineQuery(config_item)),
    )
    monkeypatch.setattr(
        register_feature_channel_base,
        "get_announcement_languages",
        lambda _guild_id, _logger: asyncio.sleep(0, result=["en"]),
    )
    subject._disable_auto_guide_and_delete_message = (  # type: ignore[method-assign]
        _noop_async
    )

    with pytest.raises(RuntimeError, match="persist failed"):
        await subject._handle_timeline_event(
            config_item.id,
            ShiftTimelineEventKind.SUBMISSION_DEADLINE,
            config_item.submission_deadline_at,
            123,
        )
    assert subject._pending_message_ids == {
        (config_item.id, ShiftTimelineEventKind.SUBMISSION_DEADLINE): (123, 9001)
    }

    await subject._handle_timeline_event(
        config_item.id,
        ShiftTimelineEventKind.SUBMISSION_DEADLINE,
        config_item.submission_deadline_at,
        123,
    )

    assert len(channel.send_attempts) == 1
    assert subject._pending_message_ids == {}


@pytest.mark.asyncio
async def test_shift_deadline_close_transition_failure_has_no_discord_side_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = _TimelineChannel()
    subject = ShiftRegister(_TimelineBot(channel))
    config_item = _timeline_config()

    class FailingManager:
        def __init__(self, feature_channel: object, _service_account_path: str) -> None:
            self.feature_channel = feature_channel

        async def get_fresh_sheet_config(self) -> SimpleNamespace:
            return config_item

        async def begin_submission_deadline_close(self, **_kwargs: object) -> None:
            error_message = "close transaction failed"
            raise RuntimeError(error_message)

    monkeypatch.setattr(subject, "ManagerType", FailingManager)
    monkeypatch.setattr(
        ShiftRegisterConfig,
        "filter",
        classmethod(lambda _cls, **_kwargs: _TimelineQuery(config_item)),
    )

    with pytest.raises(RuntimeError, match="close transaction failed"):
        await subject._handle_timeline_event(
            config_item.id,
            ShiftTimelineEventKind.SUBMISSION_DEADLINE,
            config_item.submission_deadline_at,
            123,
        )

    assert channel.send_attempts == []


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
        register_feature_channel_base,
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
