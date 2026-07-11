from __future__ import annotations

import asyncio
import datetime as dt

import pytest
from tortoise import Tortoise
from tortoise.exceptions import IntegrityError

from models.feature_channel import FeatureChannel
from models.feature_channel_message_state import (
    FeatureChannelMessageKind,
    FeatureChannelMessageState,
    get_auto_guide_state,
    get_or_create_auto_guide_state,
    save_manual_guide_anchor,
)
from models.guild_language_settings import GuildLanguageSettings
from models.shift_register import ShiftRegisterConfig
from models.team_register import TeamRegisterConfig
from utils.db import close_db, get_model_modules, init_db
from utils.shift_register_manager import ShiftRegisterManager
from utils.shift_register_structs import RecruitmentTimeRanges


@pytest.mark.asyncio
async def test_tortoise_model_registry_init_smoke() -> None:
    await Tortoise.init(db_url="sqlite://:memory:", modules=get_model_modules())
    try:
        apps = Tortoise.apps
        assert apps is not None
        assert sorted(apps["models"]) == [
            "FeatureChannel",
            "FeatureChannelMessageState",
            "GuildLanguageSettings",
            "ShiftRegisterConfig",
            "TeamRegisterConfig",
        ]

        feature_description = FeatureChannel.describe(serializable=True)
        language_description = GuildLanguageSettings.describe(serializable=True)
        team_description = TeamRegisterConfig.describe(serializable=True)
        shift_description = ShiftRegisterConfig.describe(serializable=True)

        assert feature_description["table"] == "feature_channel"
        assert feature_description["pk_field"]["name"] == "id"
        assert feature_description["pk_field"]["generated"] is True
        assert language_description["table"] == "guild_language_settings"
        assert language_description["pk_field"]["name"] == "id"
        assert language_description["pk_field"]["generated"] is True
        assert team_description["table"] == "team_register"
        assert shift_description["table"] == "shift_register"

        language_settings = GuildLanguageSettings(guild_id=1001)
        team_config = TeamRegisterConfig(
            sheet_url="https://sheet.example",
            team_worksheet_ids=[101, 102],
            summary_worksheet_id=199,
        )
        shift_config = ShiftRegisterConfig(
            sheet_url="https://sheet.example",
            entry_worksheet_id=201,
            draft_worksheet_id=202,
            final_schedule_worksheet_id=203,
        )

        assert language_settings.announcement_languages == ["ja"]
        assert team_config.get_worksheet_ids() == [101, 102, 199]
        assert shift_config.get_worksheet_ids() == [201, 202, 203]
        assert team_config.landing_worksheet_id == 199
        assert shift_config.landing_worksheet_id == 201
        assert shift_config.day_number is None
        assert shift_config.event_date is None
        assert shift_config.submission_deadline_at is None
        assert shift_config.draft_shift_proposal_at is None
        assert shift_config.final_shift_notice_at is None
        assert shift_config.recruitment_time_ranges == [{"start": 4, "end": 28}]
        assert shift_config.deadline_automation_enabled is False
        assert shift_config.team_source_feature_channel_id is None
    finally:
        await Tortoise.close_connections()


@pytest.mark.asyncio
async def test_shift_team_source_relation_is_nullable_and_set_null() -> None:
    db_url = "sqlite://:memory:"
    await asyncio.wait_for(init_db(db_url), timeout=3)
    try:
        shift_channel = await FeatureChannel.create(
            guild_id=1001,
            channel_id=2001,
            feature_name="shift_register",
        )
        team_channel = await FeatureChannel.create(
            guild_id=1001,
            channel_id=2002,
            feature_name="team_register",
        )
        config = await ShiftRegisterConfig.create(
            feature_channel=shift_channel,
            sheet_url="https://shift.sheet.example",
            entry_worksheet_id=1,
            draft_worksheet_id=2,
            final_schedule_worksheet_id=3,
        )

        assert config.team_source_feature_channel_id is None

        config.team_source_feature_channel_id = team_channel.id
        await config.save(
            update_fields=["team_source_feature_channel_id", "updated_at"]
        )
        await config.refresh_from_db()
        assert config.team_source_feature_channel_id == team_channel.id

        await team_channel.delete()
        await config.refresh_from_db()
        assert config.team_source_feature_channel_id is None
    finally:
        await asyncio.wait_for(close_db(db_url), timeout=3)


@pytest.mark.asyncio
async def test_feature_channel_message_state_enum_unique_and_cascade() -> None:
    db_url = "sqlite://:memory:"
    await asyncio.wait_for(init_db(db_url), timeout=3)
    try:
        feature_channel = await FeatureChannel.create(
            guild_id=1001,
            channel_id=2002,
            feature_name="team_register",
        )

        auto_state = await get_or_create_auto_guide_state(feature_channel)
        assert auto_state.message_kind is FeatureChannelMessageKind.AUTO_GUIDE
        assert auto_state.is_enabled is False
        assert auto_state.message_id is None

        auto_state.is_enabled = True
        auto_state.message_id = 123456789012345678
        await auto_state.save()

        fetched_auto_state = await get_auto_guide_state(feature_channel)
        assert fetched_auto_state is not None
        assert fetched_auto_state.message_kind is FeatureChannelMessageKind.AUTO_GUIDE
        assert fetched_auto_state.message_id == 123456789012345678

        manual_state = await save_manual_guide_anchor(
            feature_channel,
            987654321098765432,
        )
        assert manual_state.message_kind is FeatureChannelMessageKind.MANUAL_GUIDE
        assert manual_state.is_enabled is True
        assert manual_state.message_id == 987654321098765432

        with pytest.raises(IntegrityError):
            await FeatureChannelMessageState.create(
                feature_channel=feature_channel,
                message_kind=FeatureChannelMessageKind.AUTO_GUIDE,
                is_enabled=False,
            )

        await feature_channel.delete()
        assert await FeatureChannelMessageState.all().count() == 0
    finally:
        await asyncio.wait_for(close_db(db_url), timeout=3)


@pytest.mark.asyncio
async def test_shift_manager_updates_timeline_fields() -> None:
    db_url = "sqlite://:memory:"
    await asyncio.wait_for(init_db(db_url), timeout=3)
    try:
        feature_channel = await FeatureChannel.create(
            guild_id=1001,
            channel_id=2002,
            feature_name="shift_register",
        )
        config = await ShiftRegisterConfig.create(
            feature_channel=feature_channel,
            sheet_url="https://sheet.example",
            entry_worksheet_id=1,
            draft_worksheet_id=2,
            final_schedule_worksheet_id=3,
        )
        manager = ShiftRegisterManager(feature_channel, "service.json")
        deadline = dt.datetime(2026, 8, 12, 12, tzinfo=dt.UTC)
        draft = dt.datetime(2026, 8, 13, 11, tzinfo=dt.UTC)
        final = dt.datetime(2026, 8, 14, 9, tzinfo=dt.UTC)

        await manager.update_timeline(
            day_number=2,
            event_date=dt.date(2026, 8, 12),
            submission_deadline_at=deadline,
            draft_shift_proposal_at=draft,
            final_shift_notice_at=final,
        )

        await config.refresh_from_db()
        assert config.day_number == 2
        assert config.event_date == dt.date(2026, 8, 12)
        assert config.submission_deadline_at == deadline
        assert config.draft_shift_proposal_at == draft
        assert config.final_shift_notice_at == final
    finally:
        await asyncio.wait_for(close_db(db_url), timeout=3)


@pytest.mark.asyncio
async def test_shift_manager_timeline_preserves_fresh_ranges() -> None:
    db_url = "sqlite://:memory:"
    await asyncio.wait_for(init_db(db_url), timeout=3)
    try:
        feature_channel = await FeatureChannel.create(
            guild_id=1003,
            channel_id=2004,
            feature_name="shift_register",
        )
        config = await ShiftRegisterConfig.create(
            feature_channel=feature_channel,
            sheet_url="https://sheet.example",
            entry_worksheet_id=1,
            draft_worksheet_id=2,
            final_schedule_worksheet_id=3,
        )
        manager = ShiftRegisterManager(feature_channel, "service.json")
        await manager.get_sheet_config()
        fresh_config = await ShiftRegisterConfig.get(id=config.id)
        fresh_config.recruitment_time_ranges = [{"start": 0, "end": 30}]
        await fresh_config.save()
        deadline = dt.datetime(2026, 8, 12, 12, tzinfo=dt.UTC)
        draft = dt.datetime(2026, 8, 13, 11, tzinfo=dt.UTC)
        final = dt.datetime(2026, 8, 14, 9, tzinfo=dt.UTC)

        await manager.update_timeline(
            day_number=2,
            event_date=dt.date(2026, 8, 12),
            submission_deadline_at=deadline,
            draft_shift_proposal_at=draft,
            final_shift_notice_at=final,
        )

        fetched = await ShiftRegisterConfig.get(id=config.id)
        assert fetched.recruitment_time_ranges == [{"start": 0, "end": 30}]
        assert fetched.day_number == 2
        assert fetched.event_date == dt.date(2026, 8, 12)
        assert fetched.submission_deadline_at == deadline
        assert fetched.draft_shift_proposal_at == draft
        assert fetched.final_shift_notice_at == final
    finally:
        await asyncio.wait_for(close_db(db_url), timeout=3)


@pytest.mark.asyncio
async def test_shift_manager_updates_recruitment_time_ranges() -> None:
    db_url = "sqlite://:memory:"
    await asyncio.wait_for(init_db(db_url), timeout=3)
    try:
        feature_channel = await FeatureChannel.create(
            guild_id=1002,
            channel_id=2003,
            feature_name="shift_register",
        )
        config = await ShiftRegisterConfig.create(
            feature_channel=feature_channel,
            sheet_url="https://sheet.example",
            entry_worksheet_id=1,
            draft_worksheet_id=2,
            final_schedule_worksheet_id=3,
        )
        manager = ShiftRegisterManager(feature_channel, "service.json")
        ranges = RecruitmentTimeRanges.from_modal_input("4-8, 8-12")

        await manager.update_recruitment_time_ranges(ranges)

        await config.refresh_from_db()
        assert config.recruitment_time_ranges == [{"start": 4, "end": 12}]
    finally:
        await asyncio.wait_for(close_db(db_url), timeout=3)


@pytest.mark.asyncio
async def test_shift_manager_update_ranges_preserves_fresh_timeline_fields() -> None:
    db_url = "sqlite://:memory:"
    await asyncio.wait_for(init_db(db_url), timeout=3)
    try:
        feature_channel = await FeatureChannel.create(
            guild_id=1004,
            channel_id=2005,
            feature_name="shift_register",
        )
        config = await ShiftRegisterConfig.create(
            feature_channel=feature_channel,
            sheet_url="https://sheet.example",
            entry_worksheet_id=1,
            draft_worksheet_id=2,
            final_schedule_worksheet_id=3,
        )
        manager = ShiftRegisterManager(feature_channel, "service.json")
        await manager.get_sheet_config()
        deadline = dt.datetime(2026, 8, 12, 12, tzinfo=dt.UTC)
        fresh_config = await ShiftRegisterConfig.get(id=config.id)
        fresh_config.day_number = 2
        fresh_config.event_date = dt.date(2026, 8, 12)
        fresh_config.submission_deadline_at = deadline
        await fresh_config.save()
        ranges = RecruitmentTimeRanges.from_modal_input("4-8, 8-12")

        await manager.update_recruitment_time_ranges(ranges)

        fetched = await ShiftRegisterConfig.get(id=config.id)
        assert fetched.recruitment_time_ranges == [{"start": 4, "end": 12}]
        assert fetched.day_number == 2
        assert fetched.event_date == dt.date(2026, 8, 12)
        assert fetched.submission_deadline_at == deadline
    finally:
        await asyncio.wait_for(close_db(db_url), timeout=3)


@pytest.mark.asyncio
async def test_sqlite_init_generates_schema_and_supports_crud() -> None:
    db_url = "sqlite://:memory:"
    await asyncio.wait_for(init_db(db_url), timeout=3)
    try:
        feature_channel = await FeatureChannel.create(
            guild_id=1001,
            channel_id=2002,
            feature_name="team_register",
            is_enabled=True,
        )

        fetched = await FeatureChannel.get(id=feature_channel.id)

        assert fetched.guild_id == 1001
        assert fetched.channel_id == 2002
        assert fetched.feature_name == "team_register"
        assert fetched.is_enabled is True
    finally:
        await asyncio.wait_for(close_db(db_url), timeout=3)


@pytest.mark.asyncio
async def test_init_db_enables_global_fallback_for_cross_task_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_kwargs: dict[str, object] = {}

    async def fake_init(*_args: object, **kwargs: object) -> None:
        init_kwargs.update(kwargs)

    async def fake_generate_schemas() -> None:
        return None

    monkeypatch.setattr(Tortoise, "init", fake_init)
    monkeypatch.setattr(Tortoise, "generate_schemas", fake_generate_schemas)

    await init_db("postgres://example.invalid/rhoboto")

    assert init_kwargs["_enable_global_fallback"] is True


@pytest.mark.asyncio
async def test_sqlite_init_cancellation_stops_keepalive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    close_calls = 0
    db_url = "sqlite://:memory:"

    async def never_finish_init(*_args: object, **_kwargs: object) -> None:
        await asyncio.Event().wait()

    async def fake_close_connections() -> None:
        nonlocal close_calls
        close_calls += 1

    monkeypatch.setattr(Tortoise, "init", never_finish_init)
    monkeypatch.setattr(Tortoise, "close_connections", fake_close_connections)

    try:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(init_db(db_url), timeout=0.05)

        assert close_calls == 1
        assert [
            task
            for task in asyncio.all_tasks()
            if task.get_name() == "sqlite-aiosqlite-keepalive" and not task.done()
        ] == []
    finally:
        await close_db(db_url)
