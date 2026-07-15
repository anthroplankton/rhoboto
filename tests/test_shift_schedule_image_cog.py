from __future__ import annotations

# ruff: noqa: RUF001
import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from discord import Forbidden, HTTPException

from bot import config
from cogs import shift_register as shift_module
from cogs.shift_register import ShiftRegister
from tests.fakes import FakeInteraction
from utils.google_sheets_errors import GoogleSheetsError, GoogleSheetsErrorKind
from utils.manager_base import SheetConfigNotFoundError
from utils.shift_register_manager import FinalScheduleImageRangeError
from utils.shift_schedule_image import (
    ScheduleImageRenderError,
    ScheduleImageTooLargeError,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


class FakePermissions:
    def __init__(
        self,
        *,
        send_messages: bool = True,
        send_messages_in_threads: bool = True,
        attach_files: bool = True,
    ) -> None:
        self.send_messages = send_messages
        self.send_messages_in_threads = send_messages_in_threads
        self.attach_files = attach_files


class FakeTextChannel:
    def __init__(
        self,
        channel_id: int = 222,
        *,
        permissions: FakePermissions | None = None,
        events: list[str] | None = None,
        send_error: Exception | None = None,
        jump_url: str = "https://discord.example/messages/900",
    ) -> None:
        self.id = channel_id
        self.mention = f"<#{channel_id}>"
        self.permissions = permissions or FakePermissions()
        self.events = events
        self.send_error = send_error
        self.jump_url = jump_url
        self.permission_members: list[object] = []
        self.send_calls: list[dict[str, object]] = []
        self.attachments: list[tuple[str, bytes]] = []

    def permissions_for(self, member: object) -> FakePermissions:
        self.permission_members.append(member)
        return self.permissions

    async def send(self, **kwargs: object) -> SimpleNamespace:
        if self.events is not None:
            self.events.append("discord_send")
        self.send_calls.append(kwargs)
        file = kwargs["file"]
        filename = file.filename
        content = file.fp.read()
        file.close()
        self.attachments.append((filename, content))
        if self.send_error is not None:
            raise self.send_error
        return SimpleNamespace(id=900, jump_url=self.jump_url)


class FakeThread(FakeTextChannel):
    pass


class RecordingManager:
    def __init__(
        self,
        events: list[str],
        *,
        export_error: Exception | None = None,
    ) -> None:
        self.events = events
        self.export_error = export_error
        self.metadata_calls = 0
        self.export_calls: list[object] = []

    async def fetch_google_sheets_metadata(self) -> object:
        self.events.append("metadata")
        self.metadata_calls += 1
        return object()

    async def export_final_schedule_pdf(
        self,
        _metadata: object,
        *,
        final_schedule_range: object,
    ) -> bytes:
        self.events.append("pdf_export")
        self.export_calls.append(final_schedule_range)
        if self.export_error is not None:
            raise self.export_error
        return b"%PDF-test"


def fake_bot() -> SimpleNamespace:
    return SimpleNamespace(
        tree=SimpleNamespace(add_command=lambda _command: None),
        user=None,
    )


def fake_http_exception(status: int = 500) -> HTTPException:
    response = SimpleNamespace(status=status, reason="Failure")
    return HTTPException(response, "failure")


def fake_forbidden() -> Forbidden:
    response = SimpleNamespace(status=403, reason="Forbidden")
    return Forbidden(response, "forbidden")


_DEFAULT_MEMBER = object()


def setup_command(  # noqa: PLR0913
    monkeypatch: pytest.MonkeyPatch,
    *,
    current_channel: object,
    events: list[str] | None = None,
    manager: RecordingManager | None = None,
    context_available: bool = True,
    member: object | None = _DEFAULT_MEMBER,
    filesize_limit: int = 1_000_000,
    fresh_error: Exception | None = None,
    render_bytes: bytes = b"PNG-test",
    render_error: Exception | None = None,
    interaction: FakeInteraction | None = None,
) -> tuple[
    ShiftRegister,
    FakeInteraction,
    RecordingManager,
    list[str],
    SimpleNamespace,
]:
    event_log = events if events is not None else []
    command_manager = manager or RecordingManager(event_log)
    guild = SimpleNamespace(id=111, me=member, filesize_limit=filesize_limit)
    source = SimpleNamespace(guild=guild, channel=current_channel)
    command_interaction = interaction or FakeInteraction()
    context_calls: list[str] = []

    async def get_context(_source: object) -> object | None:
        context_calls.append("context")
        if not context_available:
            return None
        return SimpleNamespace(manager=command_manager)

    @asynccontextmanager
    async def fresh_transaction(
        *_args: object,
        **_kwargs: object,
    ) -> AsyncIterator[SimpleNamespace]:
        event_log.append("feature_lock_enter")
        if fresh_error is not None:
            raise fresh_error
        try:
            yield SimpleNamespace()
        finally:
            event_log.append("feature_lock_exit")

    async def immediate_to_thread(_function: object, *_args: object) -> bytes:
        event_log.append("render")
        if render_error is not None:
            raise render_error
        return render_bytes

    monkeypatch.setattr(shift_module, "TextChannel", FakeTextChannel)
    monkeypatch.setattr(shift_module, "Thread", FakeThread)
    monkeypatch.setattr(
        shift_module,
        "require_guild_channel_source",
        lambda *_args, **_kwargs: source,
    )
    monkeypatch.setattr(
        shift_module,
        "fresh_shift_channel_transaction",
        fresh_transaction,
    )
    monkeypatch.setattr(shift_module.asyncio, "to_thread", immediate_to_thread)

    subject = ShiftRegister(fake_bot())
    subject._get_shift_finalization_context_or_none = get_context  # type: ignore[method-assign]  # noqa: SLF001
    command_interaction.context_calls = context_calls
    return subject, command_interaction, command_manager, event_log, source


async def invoke(
    subject: ShiftRegister,
    interaction: FakeInteraction,
    *,
    status: str = "tentative",
    channel: object | None = None,
    range_a1: str | None = None,
) -> None:
    await ShiftRegister.post_schedule_image.callback(
        subject,
        interaction,
        status,
        channel,
        range_a1,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("range_a1", ["A1", "A:A", "B2:A1"])
async def test_post_schedule_image_rejects_invalid_range_before_context(
    monkeypatch: pytest.MonkeyPatch,
    range_a1: str,
) -> None:
    destination = FakeTextChannel()
    subject, interaction, manager, events, _source = setup_command(
        monkeypatch,
        current_channel=destination,
    )

    await invoke(subject, interaction, range_a1=range_a1)

    assert interaction.original_response_edits == [
        (
            f"⚠️ {config.CONFUSED_EMOJI} Final Schedule Range 格式無效，未發布圖片。",
            {},
        )
    ]
    assert interaction.context_calls == []
    assert manager.metadata_calls == 0
    assert manager.export_calls == []
    assert events == []
    assert destination.send_calls == []


@pytest.mark.asyncio
async def test_post_schedule_image_rejects_unsupported_current_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = SimpleNamespace(id=222, mention="<#222>")
    subject, interaction, manager, events, _source = setup_command(
        monkeypatch,
        current_channel=current,
    )

    await invoke(subject, interaction)

    assert interaction.original_response_edits == [
        ("⚠️ 請指定文字頻道或討論串作為發布目的地；未發布圖片。", {})
    ]
    assert interaction.context_calls == []
    assert manager.metadata_calls == 0
    assert manager.export_calls == []
    assert events == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("destination_type", "permissions", "permission_label"),
    [
        (
            FakeTextChannel,
            FakePermissions(send_messages=False),
            "Send Messages 與 Attach Files",
        ),
        (
            FakeThread,
            FakePermissions(send_messages_in_threads=False),
            "Send Messages in Threads 與 Attach Files",
        ),
    ],
)
async def test_post_schedule_image_preflights_destination_permissions(
    monkeypatch: pytest.MonkeyPatch,
    destination_type: type[FakeTextChannel],
    permissions: FakePermissions,
    permission_label: str,
) -> None:
    current = FakeTextChannel()
    destination = destination_type(333, permissions=permissions)
    subject, interaction, manager, events, _source = setup_command(
        monkeypatch,
        current_channel=current,
    )

    await invoke(subject, interaction, channel=destination)

    assert interaction.original_response_edits == [
        (
            f"⚠️ Bot 無法在 {destination.mention} 發布班表圖片；"
            f"需要 {permission_label} 權限。",
            {},
        )
    ]
    assert interaction.context_calls == []
    assert manager.metadata_calls == 0
    assert manager.export_calls == []
    assert events == []
    assert destination.send_calls == []


@pytest.mark.asyncio
async def test_post_schedule_image_missing_bot_member_uses_central_error_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = FakeTextChannel()
    subject, interaction, manager, events, source = setup_command(
        monkeypatch,
        current_channel=destination,
        member=None,
    )
    routed: list[tuple[Exception, object, str]] = []

    async def route_error(
        _interaction: object,
        exc: Exception,
        *,
        source: object,
        operation: str,
    ) -> None:
        routed.append((exc, source, operation))

    subject._send_interaction_storage_error_or_raise = route_error  # type: ignore[method-assign]  # noqa: SLF001

    await invoke(subject, interaction)

    assert len(routed) == 1
    assert type(routed[0][0]) is RuntimeError
    assert str(routed[0][0]) == ""
    assert routed[0][1:] == (source, "shift_register_post_schedule_image")
    assert interaction.context_calls == []
    assert manager.metadata_calls == 0
    assert manager.export_calls == []
    assert events == []
    assert destination.send_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("use_thread", "status", "range_a1", "filename"),
    [
        (False, "tentative", None, "shift-schedule-tentative.png"),
        (True, "confirmed", "B2:D4", "shift-schedule-confirmed.png"),
    ],
)
async def test_post_schedule_image_posts_one_attachment_after_lock(
    monkeypatch: pytest.MonkeyPatch,
    use_thread: bool,  # noqa: FBT001
    status: str,
    range_a1: str | None,
    filename: str,
) -> None:
    events: list[str] = []
    current = FakeTextChannel(events=events)
    alternate = FakeThread(333, events=events)
    destination = alternate if use_thread else current
    subject, interaction, manager, event_log, source = setup_command(
        monkeypatch,
        current_channel=current,
        events=events,
    )

    await invoke(
        subject,
        interaction,
        status=status,
        channel=alternate if use_thread else None,
        range_a1=range_a1,
    )

    assert event_log == [
        "feature_lock_enter",
        "metadata",
        "pdf_export",
        "feature_lock_exit",
        "render",
        "discord_send",
    ]
    assert len(destination.send_calls) == 1
    assert set(destination.send_calls[0]) == {"file"}
    assert destination.attachments == [(filename, b"PNG-test")]
    assert destination.permission_members == [source.guild.me]
    assert interaction.original_response_edits[-1] == (destination.jump_url, {})
    assert interaction.response.deferred == [True]
    selected_range = manager.export_calls[0]
    assert (selected_range.a1 if selected_range is not None else None) == range_a1


@pytest.mark.asyncio
async def test_post_schedule_image_hard_clear_uses_missing_config_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = FakeTextChannel()
    interaction = FakeInteraction(locale="zh-TW")
    subject, interaction, manager, events, _source = setup_command(
        monkeypatch,
        current_channel=destination,
        context_available=False,
        interaction=interaction,
    )

    await invoke(subject, interaction)

    assert interaction.followup.messages == [
        ("⚠️ 此頻道尚未設定班表登記。", {"ephemeral": True})
    ]
    assert manager.metadata_calls == 0
    assert manager.export_calls == []
    assert events == []
    assert destination.send_calls == []


@pytest.mark.asyncio
async def test_post_schedule_image_racing_hard_clear_stops_before_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = FakeTextChannel()
    interaction = FakeInteraction(locale="zh-TW")
    error = SheetConfigNotFoundError(
        SimpleNamespace(
            feature_name="shift_register",
            guild_id=111,
            channel_id=222,
        )
    )
    subject, interaction, manager, events, _source = setup_command(
        monkeypatch,
        current_channel=destination,
        fresh_error=error,
        interaction=interaction,
    )

    await invoke(subject, interaction)

    assert interaction.followup.messages == [
        ("⚠️ 此頻道尚未設定班表登記。", {"ephemeral": True})
    ]
    assert manager.metadata_calls == 0
    assert manager.export_calls == []
    assert events == ["feature_lock_enter"]
    assert destination.send_calls == []


@pytest.mark.asyncio
async def test_post_schedule_image_reports_empty_or_out_of_grid_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    manager = RecordingManager(events, export_error=FinalScheduleImageRangeError())
    destination = FakeTextChannel()
    subject, interaction, manager, _events, _source = setup_command(
        monkeypatch,
        current_channel=destination,
        events=events,
        manager=manager,
    )

    await invoke(subject, interaction)

    assert interaction.original_response_edits[-1] == (
        "⚠️📏 Final Schedule 沒有可發布的資料範圍，或指定範圍超出 worksheet；"
        "未發布圖片。",
        {},
    )
    assert manager.metadata_calls == 1
    assert destination.send_calls == []
    assert "render" not in events


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("render_error", "message"),
    [
        (ScheduleImageRenderError(), "⚠️🚧 班表圖片產生失敗，未發布圖片。"),
        (
            ScheduleImageTooLargeError(),
            "⚠️ 班表圖片過大，請指定較小的 Final Schedule Range；未發布圖片。",
        ),
    ],
)
async def test_post_schedule_image_reports_render_failures(
    monkeypatch: pytest.MonkeyPatch,
    render_error: Exception,
    message: str,
) -> None:
    destination = FakeTextChannel()
    subject, interaction, _manager, _events, _source = setup_command(
        monkeypatch,
        current_channel=destination,
        render_error=render_error,
    )

    await invoke(subject, interaction)

    assert interaction.original_response_edits[-1] == (message, {})
    assert destination.send_calls == []


@pytest.mark.asyncio
async def test_post_schedule_image_rejects_png_above_guild_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = FakeTextChannel()
    subject, interaction, _manager, _events, _source = setup_command(
        monkeypatch,
        current_channel=destination,
        filesize_limit=4,
        render_bytes=b"12345",
    )

    await invoke(subject, interaction)

    assert interaction.original_response_edits[-1] == (
        "⚠️ 班表圖片過大，請指定較小的 Final Schedule Range；未發布圖片。",
        {},
    )
    assert destination.send_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("send_error", [fake_http_exception(), fake_forbidden()])
async def test_post_schedule_image_does_not_retry_discord_send_failure(
    monkeypatch: pytest.MonkeyPatch,
    send_error: Exception,
) -> None:
    destination = FakeTextChannel(send_error=send_error)
    subject, interaction, _manager, _events, _source = setup_command(
        monkeypatch,
        current_channel=destination,
    )

    await invoke(subject, interaction)

    assert interaction.original_response_edits[-1] == (
        "⚠️🛠️ Discord 無法發布班表圖片，未建立圖片訊息。",
        {},
    )
    assert len(destination.send_calls) == 1


class EditFailingInteraction(FakeInteraction):
    async def edit_original_response(
        self,
        _content: object = None,
        **_kwargs: object,
    ) -> None:
        raise fake_http_exception()


@pytest.mark.asyncio
async def test_post_schedule_image_keeps_post_when_success_edit_fails(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    destination = FakeTextChannel()
    interaction = EditFailingInteraction()
    subject, interaction, _manager, _events, _source = setup_command(
        monkeypatch,
        current_channel=destination,
        interaction=interaction,
    )

    with caplog.at_level(logging.WARNING):
        await invoke(subject, interaction)

    assert len(destination.send_calls) == 1
    assert destination.attachments == [("shift-schedule-tentative.png", b"PNG-test")]
    assert caplog.records[-1].getMessage() == (
        "Posted schedule image but failed to edit success response. "
        "operation=shift_register_post_schedule_image guild=111 channel=222 "
        "message=900"
    )


@pytest.mark.asyncio
async def test_post_schedule_image_routes_google_error_with_safe_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    google_error = GoogleSheetsError(
        GoogleSheetsErrorKind.TRANSIENT,
        "safe message",
        operation="export_worksheet",
    )
    manager = RecordingManager(events, export_error=google_error)
    destination = FakeTextChannel()
    subject, interaction, _manager, _events, source = setup_command(
        monkeypatch,
        current_channel=destination,
        events=events,
        manager=manager,
    )
    routed: list[tuple[Exception, object, str]] = []

    async def route_error(
        _interaction: object,
        exc: Exception,
        *,
        source: object,
        operation: str,
    ) -> None:
        routed.append((exc, source, operation))

    subject._send_interaction_storage_error_or_raise = route_error  # type: ignore[method-assign]  # noqa: SLF001

    await invoke(subject, interaction)

    assert routed == [(google_error, source, "shift_register_post_schedule_image")]
    assert destination.send_calls == []
