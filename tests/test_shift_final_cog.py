from __future__ import annotations

import datetime as dt
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from cogs.shift_register import ShiftRegister, _format_final_contract_error
from tests.fakes import FakeInteraction
from utils.shift_final import (
    DEFAULT_EVENT_DAY_FORMAT,
    FinalSchedulePlan,
    FinalScheduleRow,
    FinalScheduleValidationError,
    FinalScheduleValidationKind,
    build_schedule_update_request,
)
from utils.shift_register_manager import ScheduleUpdateResult
from utils.shift_register_structs import RecruitmentTimeRanges

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


def fake_bot() -> SimpleNamespace:
    return SimpleNamespace(
        tree=SimpleNamespace(add_command=lambda _command: None),
        user=None,
    )


def final_config() -> SimpleNamespace:
    return SimpleNamespace(
        sheet_url="https://docs.google.com/spreadsheets/d/example/edit",
        draft_worksheet_id=2,
        final_schedule_worksheet_id=3,
        final_schedule_anchor_cell="B2",
        recruitment_time_ranges=[{"start": 4, "end": 6}],
        event_date=dt.date(2026, 12, 21),
    )


def final_result(config: SimpleNamespace) -> ScheduleUpdateResult:
    request = build_schedule_update_request(
        recruitment_ranges=RecruitmentTimeRanges.from_json(
            config.recruitment_time_ranges
        ),
        saved_anchor=config.final_schedule_anchor_cell,
        supplied_anchor=None,
        event_date=config.event_date,
        event_day_anchor="A1",
        event_day_format=None,
    )
    return ScheduleUpdateResult(
        request=request,
        schedule=FinalSchedulePlan(
            rows=(
                FinalScheduleRow(
                    hour=4,
                    is_recruitment=True,
                    runner="Runner A",
                    encore="Encore",
                    honso=("Main", "", ""),
                    standby="",
                ),
                FinalScheduleRow(
                    hour=5,
                    is_recruitment=True,
                    runner="Runner B",
                    encore="",
                    honso=("", "", ""),
                    standby="Standby",
                ),
            ),
            split_colors={},
        ),
    )


def test_update_schedule_from_draft_has_safe_native_length_limit() -> None:
    parameters = {
        parameter.name: parameter
        for parameter in ShiftRegister.update_schedule_from_draft.parameters
    }
    assert ShiftRegister.update_schedule_from_draft.name == (
        "update_schedule_from_draft"
    )
    event_day_format = parameters["event_day_format"]

    assert event_day_format.required is False
    assert event_day_format.min_value == 1
    assert event_day_format.max_value == 512
    assert f"Default: {DEFAULT_EVENT_DAY_FORMAT}" in str(event_day_format.description)


def test_shift_finalization_commands_do_not_require_enabled_membership() -> None:
    for command in (
        ShiftRegister.generate_draft,
        ShiftRegister.update_schedule_from_draft,
        ShiftRegister.assign_schedule_role,
        ShiftRegister.post_schedule_image,
    ):
        assert command.checks == []


def test_post_schedule_image_has_native_required_status_and_destination() -> None:
    command = ShiftRegister.post_schedule_image
    parameters = {parameter.name: parameter for parameter in command.parameters}

    assert command.name == "post_schedule_image"
    assert str(command.description) == "Post the current Final Schedule as an image."
    assert parameters["schedule_status"].required is True
    assert [
        (choice.name, choice.value) for choice in parameters["schedule_status"].choices
    ] == [
        ("Tentative", "tentative"),
        ("Confirmed", "confirmed"),
    ]
    assert all(
        choice._locale_name is not None  # noqa: SLF001
        for choice in parameters["schedule_status"].choices
    )
    assert parameters["channel"].required is False
    assert parameters["channel"].type.value == 7
    assert {item.value for item in parameters["channel"].channel_types} == {
        0,
        5,
        10,
        11,
        12,
    }
    assert parameters["final_schedule_range"].required is False
    assert command.checks == []


@pytest.mark.asyncio
@pytest.mark.parametrize("configured", [True, False])
async def test_shift_finalization_context_ignores_enabled_state(
    configured: bool,  # noqa: FBT001
) -> None:
    subject = ShiftRegister.__new__(ShiftRegister)
    source = SimpleNamespace(
        guild=SimpleNamespace(id=111),
        channel=SimpleNamespace(id=222),
    )
    feature_context = SimpleNamespace(feature_channel=SimpleNamespace(is_enabled=False))
    configured_context = SimpleNamespace(feature_config=SimpleNamespace())
    calls: list[dict[str, object]] = []

    async def get_context_or_none(**kwargs: object) -> object:
        calls.append(kwargs)
        return feature_context

    async def get_configured_context(_context: object) -> object | None:
        return configured_context if configured else None

    subject._get_register_feature_channel_context_or_none = (  # type: ignore[method-assign]  # noqa: SLF001
        get_context_or_none
    )
    subject._get_configured_register_feature_channel_context = (  # type: ignore[method-assign]  # noqa: SLF001
        get_configured_context
    )

    result = await subject._get_shift_finalization_context_or_none(source)  # noqa: SLF001

    assert result is (configured_context if configured else None)
    assert calls == [
        {
            "guild_id": 111,
            "channel_id": 222,
            "require_enabled": False,
        }
    ]


def test_final_contract_error_is_actionable() -> None:
    content = _format_final_contract_error(
        FinalScheduleValidationError(
            FinalScheduleValidationKind.AXIS,
            row=5,
            column=1,
            expected="7-8",
            detected="8-9",
        )
    )

    assert "第 5 列、第 1 欄" in content
    assert "預期：7-8" in content  # noqa: RUF001
    assert "實際：8-9" in content  # noqa: RUF001
    assert "`axis`" not in content


@pytest.mark.asyncio
async def test_update_schedule_from_draft_confirms_before_metadata_and_writes_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = final_config()
    result = final_result(config)
    events: list[str] = []

    class Manager:
        async def get_fresh_sheet_config(self) -> SimpleNamespace:
            events.append("config")
            return config

        async def fetch_google_sheets_metadata(self) -> object:
            events.append("metadata")
            return object()

        def log_missing_worksheet_warnings(self, _metadata: object) -> None:
            events.append("warnings")

        async def update_schedule_from_draft(
            self,
            _metadata: object,
            *,
            request: object,
        ) -> ScheduleUpdateResult:
            events.append("write")
            assert request == result.request
            return result

    manager = Manager()

    async def get_feature_context(**_kwargs: object) -> object:
        events.append("feature")
        return object()

    async def get_configured_context(_context: object) -> SimpleNamespace:
        events.append("context")
        return SimpleNamespace(manager=manager, feature_config=config)

    @asynccontextmanager
    async def unlocked(
        *_args: object,
        **_kwargs: object,
    ) -> AsyncIterator[SimpleNamespace]:
        events.append("lock")
        yield config

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
            assert destination_label == "Final Schedule"
            assert destination_url.endswith("#gid=3")

        async def wait(self) -> None:
            events.append("confirm")

    monkeypatch.setattr(
        "cogs.shift_register.GenerateShiftScheduleConfirmView",
        ConfirmView,
    )
    monkeypatch.setattr("cogs.shift_register.fresh_shift_channel_transaction", unlocked)
    subject = ShiftRegister(fake_bot())
    subject._get_register_feature_channel_context_or_none = get_feature_context  # type: ignore[method-assign]  # noqa: SLF001
    subject._get_configured_register_feature_channel_context = get_configured_context  # type: ignore[method-assign]  # noqa: SLF001
    interaction = FakeInteraction()

    await ShiftRegister.update_schedule_from_draft.callback(
        subject,
        interaction,
        None,
        "A1",
        None,
    )

    assert events.index("confirm") < events.index("metadata")
    assert events.index("metadata") < events.index("write")
    prompt, prompt_kwargs = interaction.original_response_edits[0]
    assert "B2:G3" in prompt
    assert prompt_kwargs["view"].__class__ is ConfirmView
    report = interaction.original_response_edits[-1][0]
    assert "### ✅ 確定班表已產生" in report
    assert "`Runner A`、`Runner B`" in report
    assert "缺 `2`" in report


@pytest.mark.asyncio
async def test_update_schedule_from_draft_invalid_main_anchor_does_not_create_view(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = final_config()
    called = False

    async def get_feature_context(**_kwargs: object) -> object:
        return object()

    async def get_configured_context(_context: object) -> SimpleNamespace:
        return SimpleNamespace(
            manager=SimpleNamespace(),
            feature_config=config,
        )

    class UnexpectedView:
        def __init__(self, **_kwargs: object) -> None:
            nonlocal called
            called = True

    monkeypatch.setattr(
        "cogs.shift_register.GenerateShiftScheduleConfirmView",
        UnexpectedView,
    )
    subject = ShiftRegister(fake_bot())
    subject._get_register_feature_channel_context_or_none = get_feature_context  # type: ignore[method-assign]  # noqa: SLF001
    subject._get_configured_register_feature_channel_context = get_configured_context  # type: ignore[method-assign]  # noqa: SLF001
    interaction = FakeInteraction()

    await ShiftRegister.update_schedule_from_draft.callback(
        subject,
        interaction,
        "not-a-cell",
        None,
        None,
    )

    assert not called
    assert "Final Schedule Anchor Cell" in interaction.original_response_edits[-1][0]
