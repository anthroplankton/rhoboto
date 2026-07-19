from __future__ import annotations

import asyncio
import datetime as dt
from unittest.mock import AsyncMock

import pytest
from tortoise import Tortoise
from tortoise.exceptions import IntegrityError
from tortoise.queryset import QuerySet

import utils.shift_register_manager as shift_register_manager_module
from models.admin_notifications import (
    AdminNotificationDelivery,
    AdminNotificationDeliveryStatus,
    AdminNotificationMilestoneKind,
    AdminNotificationsConfig,
)
from models.feature_channel import FeatureChannel
from models.feature_channel_message_state import (
    FeatureChannelMessageKind,
    FeatureChannelMessageState,
    get_auto_guide_state,
    get_or_create_auto_guide_state,
    save_manual_guide_anchor,
)
from models.guild_language_settings import GuildLanguageSettings
from models.room_number import RoomNumberConfig
from models.shift_notice import ShiftNoticeConfig
from models.shift_register import ShiftRegisterConfig
from models.shift_timeline_event_state import (
    ShiftTimelineEventKind,
    ShiftTimelineEventState,
    ShiftTimelineEventStatus,
)
from models.team_register import TeamRegisterConfig
from tests.test_manager_fakes import (
    FakeEntryWorksheet,
    FakeShiftValueSheet,
    current_entry_rows,
    make_shift_metadata,
)
from utils.db import close_db, get_model_modules, init_db
from utils.google_sheets_errors import GoogleSheetsError, GoogleSheetsErrorKind
from utils.shift_register_manager import (
    AutoCloseDeadlineNotFutureError,
    ShiftRegisterManager,
)
from utils.shift_register_structs import RecruitmentTimeRanges
from utils.storage_errors import StorageError, StorageErrorKind


async def _get_deadline_state(
    shift_register: ShiftRegisterConfig,
) -> ShiftTimelineEventState | None:
    return await ShiftTimelineEventState.get_or_none(
        shift_register=shift_register,
        event_kind=ShiftTimelineEventKind.SUBMISSION_DEADLINE,
    )


@pytest.mark.asyncio
async def test_tortoise_model_registry_init_smoke() -> None:
    await Tortoise.init(db_url="sqlite://:memory:", modules=get_model_modules())
    try:
        apps = Tortoise.apps
        assert apps is not None
        assert sorted(apps["models"]) == [
            "AdminNotificationDelivery",
            "AdminNotificationsConfig",
            "FeatureChannel",
            "FeatureChannelMessageState",
            "GuildLanguageSettings",
            "RoomNumberConfig",
            "ShiftNoticeConfig",
            "ShiftRegisterConfig",
            "ShiftTimelineEventState",
            "TeamRegisterConfig",
        ]

        feature_description = FeatureChannel.describe(serializable=True)
        language_description = GuildLanguageSettings.describe(serializable=True)
        team_description = TeamRegisterConfig.describe(serializable=True)
        shift_description = ShiftRegisterConfig.describe(serializable=True)
        room_description = RoomNumberConfig.describe(serializable=True)
        shift_notice_description = ShiftNoticeConfig.describe(serializable=True)
        event_description = ShiftTimelineEventState.describe(serializable=True)
        notification_config_description = AdminNotificationsConfig.describe(
            serializable=True
        )
        notification_delivery_description = AdminNotificationDelivery.describe(
            serializable=True
        )

        assert feature_description["table"] == "feature_channel"
        assert feature_description["pk_field"]["name"] == "id"
        assert feature_description["pk_field"]["generated"] is True
        assert language_description["table"] == "guild_language_settings"
        assert language_description["pk_field"]["name"] == "id"
        assert language_description["pk_field"]["generated"] is True
        assert team_description["table"] == "team_register"
        assert shift_description["table"] == "shift_register"
        assert room_description["table"] == "room_number_config"
        assert shift_notice_description["table"] == "shift_notice_config"
        assert event_description["table"] == "shift_timeline_event_state"
        fields_by_name = {
            field["name"]: field for field in event_description["data_fields"]
        }
        assert fields_by_name["event_kind"]["constraints"]["max_length"] == 32
        assert fields_by_name["status"]["constraints"]["max_length"] == 16
        assert notification_config_description["table"] == "admin_notifications_config"
        assert notification_delivery_description["table"] == (
            "admin_notification_delivery"
        )

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
async def test_room_number_config_defaults_constraints_and_cascade() -> None:
    db_url = "sqlite://:memory:"
    await asyncio.wait_for(init_db(db_url), timeout=3)
    try:
        source = await FeatureChannel.create(
            guild_id=1001,
            channel_id=2001,
            feature_name="room_number",
        )
        config = await RoomNumberConfig.create(
            feature_channel=source,
            target_channel_id=2002,
        )

        assert config.room_number is None
        assert config.channel_name_format == "部屋番号【{room_number}】"
        assert config.recruitment_template_enabled is True
        assert config.recruitment_template_channel_id is None
        assert config.recruitment_template_message_id is None

        with pytest.raises(IntegrityError):
            await RoomNumberConfig.create(
                feature_channel=source,
                target_channel_id=2003,
            )

        other_source = await FeatureChannel.create(
            guild_id=1001,
            channel_id=2004,
            feature_name="room_number",
        )
        with pytest.raises(IntegrityError):
            await RoomNumberConfig.create(
                feature_channel=other_source,
                target_channel_id=2002,
            )

        with pytest.raises(ValueError, match="must be paired"):
            await RoomNumberConfig.create(
                feature_channel=other_source,
                target_channel_id=2005,
                recruitment_template_channel_id=2005,
            )

        with pytest.raises(ValueError, match="canonical"):
            await RoomNumberConfig.create(
                feature_channel=other_source,
                target_channel_id=2005,
                room_number="１２３４５",  # noqa: RUF001
            )

        config.room_number = "123456"
        config.recruitment_template_channel_id = 2002
        config.recruitment_template_message_id = 3001
        await config.save(
            update_fields=[
                "room_number",
                "recruitment_template_channel_id",
                "recruitment_template_message_id",
                "updated_at",
            ]
        )
        await config.refresh_from_db()

        assert config.room_number == "123456"
        assert config.recruitment_template_channel_id == 2002
        assert config.recruitment_template_message_id == 3001

        await source.delete()
        assert await RoomNumberConfig.all().count() == 0
    finally:
        await asyncio.wait_for(close_db(db_url), timeout=3)


@pytest.mark.asyncio
async def test_admin_notifications_config_singletons_defaults_and_feature_cascade() -> (
    None
):
    db_url = "sqlite://:memory:"
    await asyncio.wait_for(init_db(db_url), timeout=3)
    try:
        feature_channel = await FeatureChannel.create(
            guild_id=1001,
            channel_id=2001,
            feature_name="admin_notifications",
        )
        config = await AdminNotificationsConfig.create(
            feature_channel=feature_channel,
            guild_id=1001,
        )

        assert config.reminder_lead_minutes is None
        assert config.mention_role_ids == []
        assert config.mention_user_ids == []
        assert config.shift_timeline_reminders_enabled is False

        with pytest.raises(IntegrityError):
            await AdminNotificationsConfig.create(
                feature_channel=await FeatureChannel.create(
                    guild_id=1001,
                    channel_id=2002,
                    feature_name="admin_notifications",
                ),
                guild_id=1001,
            )

        with pytest.raises(IntegrityError):
            await AdminNotificationsConfig.create(
                feature_channel=feature_channel,
                guild_id=1002,
            )

        assert await AdminNotificationsConfig.all().count() == 1
    finally:
        await asyncio.wait_for(close_db(db_url), timeout=3)


@pytest.mark.asyncio
async def test_shift_notice_config_singleton_nullable_minute_and_feature_cascade() -> (
    None
):
    db_url = "sqlite://:memory:"
    await asyncio.wait_for(init_db(db_url), timeout=3)
    try:
        shift_feature_channel = await FeatureChannel.create(
            guild_id=1001,
            channel_id=2000,
            feature_name="shift_register",
        )
        shift_register = await ShiftRegisterConfig.create(
            feature_channel=shift_feature_channel,
            sheet_url="https://docs.google.com/spreadsheets/d/shift/edit",
            entry_worksheet_id=101,
            draft_worksheet_id=102,
            final_schedule_worksheet_id=103,
        )
        shift_rows_before = await ShiftRegisterConfig.filter(
            id=shift_register.id
        ).values()
        feature_channel = await FeatureChannel.create(
            guild_id=1001,
            channel_id=2001,
            feature_name="shift_notice",
        )
        config = await ShiftNoticeConfig.create(
            feature_channel=feature_channel,
            guild_id=1001,
        )

        assert config.minute_of_hour is None

        with pytest.raises(IntegrityError):
            await ShiftNoticeConfig.create(
                feature_channel=await FeatureChannel.create(
                    guild_id=1001,
                    channel_id=2002,
                    feature_name="shift_notice",
                ),
                guild_id=1001,
            )

        with pytest.raises(IntegrityError):
            await ShiftNoticeConfig.create(
                feature_channel=feature_channel,
                guild_id=1002,
            )

        await feature_channel.delete()

        assert await ShiftNoticeConfig.all().count() == 0
        assert await ShiftRegisterConfig.filter(id=shift_register.id).values() == (
            shift_rows_before
        )
    finally:
        await asyncio.wait_for(close_db(db_url), timeout=3)


@pytest.mark.asyncio
async def test_admin_notification_delivery_occurrence_unique_and_both_cascades() -> (
    None
):
    db_url = "sqlite://:memory:"
    await asyncio.wait_for(init_db(db_url), timeout=3)
    try:
        notification_channel = await FeatureChannel.create(
            guild_id=1001,
            channel_id=2001,
            feature_name="admin_notifications",
        )
        notification_config = await AdminNotificationsConfig.create(
            feature_channel=notification_channel,
            guild_id=1001,
        )
        shift_channel_one = await FeatureChannel.create(
            guild_id=1001,
            channel_id=2002,
            feature_name="shift_register",
        )
        shift_channel_two = await FeatureChannel.create(
            guild_id=1001,
            channel_id=2003,
            feature_name="shift_register",
        )
        shift_one = await ShiftRegisterConfig.create(
            feature_channel=shift_channel_one,
            sheet_url="https://shift-one.example",
            entry_worksheet_id=1,
            draft_worksheet_id=2,
            final_schedule_worksheet_id=3,
        )
        shift_two = await ShiftRegisterConfig.create(
            feature_channel=shift_channel_two,
            sheet_url="https://shift-two.example",
            entry_worksheet_id=4,
            draft_worksheet_id=5,
            final_schedule_worksheet_id=6,
        )
        milestone_at = dt.datetime(2026, 8, 14, 12, tzinfo=dt.UTC)
        deliveries = [
            await AdminNotificationDelivery.create(
                admin_notifications_config=notification_config,
                shift_register=shift_one,
                milestone_kind=kind,
                milestone_at=milestone_at,
                reminder_at=milestone_at - dt.timedelta(minutes=10),
                delivery_nonce=index,
                status=status,
            )
            for index, (kind, status) in enumerate(
                (
                    (
                        AdminNotificationMilestoneKind.SUBMISSION_DEADLINE,
                        AdminNotificationDeliveryStatus.SCHEDULED,
                    ),
                    (
                        AdminNotificationMilestoneKind.DRAFT_SHIFT_PROPOSAL,
                        AdminNotificationDeliveryStatus.SENT,
                    ),
                    (
                        AdminNotificationMilestoneKind.FINAL_SHIFT_NOTICE,
                        AdminNotificationDeliveryStatus.EXPIRED,
                    ),
                ),
                start=1,
            )
        ]
        failed_delivery = await AdminNotificationDelivery.create(
            admin_notifications_config=notification_config,
            shift_register=shift_two,
            milestone_kind=AdminNotificationMilestoneKind.SUBMISSION_DEADLINE,
            milestone_at=milestone_at,
            reminder_at=milestone_at - dt.timedelta(minutes=10),
            delivery_nonce=4,
            status=AdminNotificationDeliveryStatus.FAILED,
        )

        assert [delivery.status for delivery in deliveries] == [
            AdminNotificationDeliveryStatus.SCHEDULED,
            AdminNotificationDeliveryStatus.SENT,
            AdminNotificationDeliveryStatus.EXPIRED,
        ]
        with pytest.raises(IntegrityError):
            await AdminNotificationDelivery.create(
                admin_notifications_config=notification_config,
                shift_register=shift_one,
                milestone_kind=AdminNotificationMilestoneKind.SUBMISSION_DEADLINE,
                milestone_at=milestone_at,
                reminder_at=milestone_at - dt.timedelta(minutes=10),
                delivery_nonce=99,
            )

        await shift_one.delete()
        assert (
            await AdminNotificationDelivery.filter(id=deliveries[0].id).exists()
            is False
        )
        assert await AdminNotificationDelivery.filter(id=failed_delivery.id).exists()

        await notification_channel.delete()
        assert await AdminNotificationsConfig.all().count() == 0
        assert await AdminNotificationDelivery.all().count() == 0
    finally:
        await asyncio.wait_for(close_db(db_url), timeout=3)


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
async def test_shift_manager_reads_saved_team_source_discord_channel_id() -> None:
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
            team_source_feature_channel=team_channel,
            sheet_url="https://shift.sheet.example",
            entry_worksheet_id=1,
            draft_worksheet_id=2,
            final_schedule_worksheet_id=3,
        )
        manager = ShiftRegisterManager(shift_channel, "service.json")
        manager._sheet_config = config  # noqa: SLF001

        assert await manager.get_saved_team_source_channel_id() == 2002

        config.team_source_feature_channel_id = None
        assert await manager.get_saved_team_source_channel_id() is None
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
async def test_shift_timeline_event_state_unique_defaults_and_cascade() -> None:
    db_url = "sqlite://:memory:"
    await asyncio.wait_for(init_db(db_url), timeout=3)
    try:
        feature_channel = await FeatureChannel.create(
            guild_id=1001,
            channel_id=2002,
            feature_name="shift_register",
        )
        shift_register = await ShiftRegisterConfig.create(
            feature_channel=feature_channel,
            sheet_url="https://sheet.example",
            entry_worksheet_id=1,
            draft_worksheet_id=2,
            final_schedule_worksheet_id=3,
        )
        scheduled_at = dt.datetime(2026, 8, 14, 12, tzinfo=dt.UTC)
        state = await ShiftTimelineEventState.create(
            shift_register=shift_register,
            event_kind=ShiftTimelineEventKind.SUBMISSION_DEADLINE,
            scheduled_at=scheduled_at,
            delivery_nonce=123456789,
        )

        assert state.status is ShiftTimelineEventStatus.SCHEDULED
        assert state.message_id is None
        with pytest.raises(IntegrityError):
            await ShiftTimelineEventState.create(
                shift_register=shift_register,
                event_kind=ShiftTimelineEventKind.SUBMISSION_DEADLINE,
                scheduled_at=scheduled_at,
                delivery_nonce=987654321,
            )

        await shift_register.delete()
        assert await ShiftTimelineEventState.all().count() == 0
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
        worksheet = FakeEntryWorksheet(current_entry_rows())
        metadata = make_shift_metadata(worksheet)
        manager.fetch_google_sheets_metadata = AsyncMock(return_value=metadata)
        manager._sync_entry_presentation_locked = AsyncMock()  # noqa: SLF001
        manager._google_sheet = FakeShiftValueSheet()  # noqa: SLF001

        await manager.update_recruitment_time_ranges(ranges)

        await config.refresh_from_db()
        assert config.recruitment_time_ranges == [{"start": 4, "end": 12}]
        manager._sync_entry_presentation_locked.assert_awaited_once_with(  # noqa: SLF001
            metadata,
            ranges,
            entry_grid=worksheet.rows,
            force=True,
        )
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
        manager.fetch_google_sheets_metadata = AsyncMock(
            return_value=make_shift_metadata(FakeEntryWorksheet(current_entry_rows()))
        )
        manager.sync_entry_presentation = AsyncMock()
        manager._google_sheet = FakeShiftValueSheet()  # noqa: SLF001
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
async def test_shift_manager_range_sheet_failure_is_partial_after_database_save() -> (
    None
):
    db_url = "sqlite://:memory:"
    await asyncio.wait_for(init_db(db_url), timeout=3)
    try:
        feature_channel = await FeatureChannel.create(
            guild_id=1006,
            channel_id=2007,
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
        manager.fetch_google_sheets_metadata = AsyncMock(
            return_value=make_shift_metadata(FakeEntryWorksheet(current_entry_rows()))
        )
        manager._google_sheet = FakeShiftValueSheet()  # noqa: SLF001
        manager._sync_entry_presentation_locked = AsyncMock(  # noqa: SLF001
            side_effect=GoogleSheetsError(
                GoogleSheetsErrorKind.TRANSIENT,
                "temporary",
            )
        )
        ranges = RecruitmentTimeRanges.from_modal_input("4-12, 20-28")

        with pytest.raises(StorageError) as exc_info:
            await manager.update_recruitment_time_ranges(ranges)

        assert exc_info.value.kind is StorageErrorKind.PARTIAL_SUCCESS
        await config.refresh_from_db()
        assert config.recruitment_time_ranges == [
            {"start": 4, "end": 12},
            {"start": 20, "end": 28},
        ]
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


async def _create_deadline_manager(
    *,
    deadline: dt.datetime | None = None,
) -> tuple[FeatureChannel, ShiftRegisterConfig, ShiftRegisterManager]:
    feature_channel = await FeatureChannel.create(
        guild_id=1001,
        channel_id=2002,
        feature_name="shift_register",
    )
    config = await ShiftRegisterConfig.create(
        feature_channel=feature_channel,
        sheet_url="https://shift.sheet.example",
        entry_worksheet_id=1,
        draft_worksheet_id=2,
        final_schedule_worksheet_id=3,
        submission_deadline_at=deadline,
    )
    return (
        feature_channel,
        config,
        ShiftRegisterManager(feature_channel, "service.json"),
    )


@pytest.mark.asyncio
async def test_deadline_event_lock_scopes_for_update_to_event_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_scopes: list[tuple[str, ...]] = []
    select_for_update = QuerySet.select_for_update

    def record_lock_scope(
        query: QuerySet,
        *,
        nowait: bool = False,
        skip_locked: bool = False,
        of: tuple[str, ...] = (),
        no_key: bool = False,
    ) -> QuerySet:
        if query.model is ShiftTimelineEventState:
            lock_scopes.append(of)
        return select_for_update(
            query,
            nowait=nowait,
            skip_locked=skip_locked,
            of=of,
            no_key=no_key,
        )

    monkeypatch.setattr(QuerySet, "select_for_update", record_lock_scope)
    db_url = "sqlite://:memory:"
    await asyncio.wait_for(init_db(db_url), timeout=3)
    try:
        deadline = dt.datetime(2026, 8, 1, 12, tzinfo=dt.UTC)
        _channel, config, manager = await _create_deadline_manager(deadline=deadline)
        state = await ShiftTimelineEventState.create(
            shift_register=config,
            event_kind=ShiftTimelineEventKind.SUBMISSION_DEADLINE,
            scheduled_at=deadline,
            delivery_nonce=123,
        )

        assert await manager.mark_submission_deadline_sent(
            event_state_id=state.id,
            delivery_nonce=state.delivery_nonce,
            message_id=456,
        )
        assert lock_scopes == [("shift_timeline_event_state",)]
    finally:
        await asyncio.wait_for(close_db(db_url), timeout=3)


@pytest.mark.asyncio
async def test_deadline_automation_enable_disable_and_nonce_rotation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_url = "sqlite://:memory:"
    await asyncio.wait_for(init_db(db_url), timeout=3)
    try:
        now = dt.datetime(2026, 8, 1, 12, tzinfo=dt.UTC)
        deadline = now + dt.timedelta(days=1)
        _channel, config, manager = await _create_deadline_manager(deadline=deadline)
        monkeypatch.setattr(
            shift_register_manager_module,
            "_new_delivery_nonce",
            lambda: 111,
        )

        change = await manager.set_deadline_automation_enabled(enabled=True, now=now)
        await config.refresh_from_db()
        state = await _get_deadline_state(config)
        assert config.deadline_automation_enabled is True
        assert state is not None
        assert state.status is ShiftTimelineEventStatus.SCHEDULED
        assert state.scheduled_at == deadline
        assert state.delivery_nonce == 111
        assert change.delivery_nonce == 111

        monkeypatch.setattr(
            shift_register_manager_module,
            "_new_delivery_nonce",
            lambda: 222,
        )
        change = await manager.set_deadline_automation_enabled(enabled=True, now=now)
        await config.refresh_from_db()
        state = await _get_deadline_state(config)
        assert state is not None
        assert state.delivery_nonce == 222
        assert state.message_id is None
        assert change.delivery_nonce == 222

        change = await manager.set_deadline_automation_enabled(enabled=False, now=now)
        await config.refresh_from_db()
        assert config.deadline_automation_enabled is False
        assert await _get_deadline_state(config) is None
        assert change.scheduled_at is None
        assert change.delivery_nonce is None
    finally:
        await asyncio.wait_for(close_db(db_url), timeout=3)


@pytest.mark.asyncio
async def test_deadline_automation_rejects_nonfuture_enable_without_mutation() -> None:
    db_url = "sqlite://:memory:"
    await asyncio.wait_for(init_db(db_url), timeout=3)
    try:
        now = dt.datetime(2026, 8, 1, 12, tzinfo=dt.UTC)
        for deadline in (None, now, now - dt.timedelta(seconds=1)):
            _channel, config, manager = await _create_deadline_manager(
                deadline=deadline
            )
            with pytest.raises(AutoCloseDeadlineNotFutureError):
                await manager.set_deadline_automation_enabled(enabled=True, now=now)
            await config.refresh_from_db()
            assert config.deadline_automation_enabled is False
            assert await _get_deadline_state(config) is None
            await config.delete()
            await config.feature_channel.delete()
    finally:
        await asyncio.wait_for(close_db(db_url), timeout=3)


@pytest.mark.asyncio
async def test_deadline_automation_timeline_reconciliation_and_invalidating_save() -> (
    None
):
    db_url = "sqlite://:memory:"
    await asyncio.wait_for(init_db(db_url), timeout=3)
    try:
        now = dt.datetime(2026, 8, 1, 12, tzinfo=dt.UTC)
        first_deadline = now + dt.timedelta(days=1)
        second_deadline = now + dt.timedelta(days=2)
        _channel, config, manager = await _create_deadline_manager(
            deadline=first_deadline
        )
        await manager.set_deadline_automation_enabled(enabled=True, now=now)
        state = await _get_deadline_state(config)
        assert state is not None
        first_nonce = state.delivery_nonce

        result = await manager.update_timeline(
            day_number=2,
            event_date=dt.date(2026, 8, 2),
            submission_deadline_at=first_deadline,
            draft_shift_proposal_at=now + dt.timedelta(days=3),
            final_shift_notice_at=now + dt.timedelta(days=4),
            now=now,
        )
        assert result.schedule_change is None
        await config.refresh_from_db()
        state = await _get_deadline_state(config)
        assert state is not None
        assert state.delivery_nonce == first_nonce

        result = await manager.update_timeline(
            day_number=3,
            event_date=dt.date(2026, 8, 3),
            submission_deadline_at=second_deadline,
            draft_shift_proposal_at=None,
            final_shift_notice_at=None,
            now=now,
        )
        assert result.schedule_change is not None
        assert result.schedule_change.scheduled_at == second_deadline
        await config.refresh_from_db()
        state = await _get_deadline_state(config)
        assert state is not None
        assert state.scheduled_at == second_deadline
        assert state.delivery_nonce != first_nonce
        assert state.message_id is None

        result = await manager.update_timeline(
            day_number=4,
            event_date=dt.date(2026, 8, 4),
            submission_deadline_at=now,
            draft_shift_proposal_at=None,
            final_shift_notice_at=None,
            now=now,
        )
        assert result.auto_close_disabled is True
        assert result.schedule_change is not None
        await config.refresh_from_db()
        assert config.day_number == 4
        assert config.event_date == dt.date(2026, 8, 4)
        assert config.submission_deadline_at == now
        assert config.deadline_automation_enabled is False
        assert await _get_deadline_state(config) is None
    finally:
        await asyncio.wait_for(close_db(db_url), timeout=3)


@pytest.mark.asyncio
async def test_deadline_automation_reconcile_repairs_and_cancels_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_url = "sqlite://:memory:"
    await asyncio.wait_for(init_db(db_url), timeout=3)
    try:
        now = dt.datetime(2026, 8, 1, 12, tzinfo=dt.UTC)
        deadline = now + dt.timedelta(days=1)
        _channel, config, manager = await _create_deadline_manager(deadline=deadline)
        config.deadline_automation_enabled = True
        await config.save(update_fields=["deadline_automation_enabled", "updated_at"])
        monkeypatch.setattr(
            shift_register_manager_module,
            "_new_delivery_nonce",
            lambda: 333,
        )

        result = await manager.reconcile_deadline_automation(now=now)
        assert result.schedule_change is not None
        state = await _get_deadline_state(config)
        assert state is not None
        assert state.delivery_nonce == 333

        result = await manager.reconcile_deadline_automation(now=now)
        assert result.schedule_change is None
        state = await _get_deadline_state(config)
        assert state is not None
        assert state.delivery_nonce == 333

        state.status = ShiftTimelineEventStatus.COMPLETED
        await state.save(update_fields=["status", "updated_at"])
        config.deadline_automation_enabled = False
        await config.save(update_fields=["deadline_automation_enabled", "updated_at"])
        result = await manager.reconcile_deadline_automation(now=now)
        assert result.schedule_change is None
        assert await _get_deadline_state(config) is not None

        config.deadline_automation_enabled = True
        config.submission_deadline_at = now - dt.timedelta(seconds=1)
        await config.save(
            update_fields=[
                "deadline_automation_enabled",
                "submission_deadline_at",
                "updated_at",
            ]
        )
        result = await manager.reconcile_deadline_automation(now=now)
        assert result.auto_close_disabled is True
        assert await _get_deadline_state(config) is None
        await config.refresh_from_db()
        assert config.deadline_automation_enabled is False
    finally:
        await asyncio.wait_for(close_db(db_url), timeout=3)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [ShiftTimelineEventStatus.SCHEDULED, ShiftTimelineEventStatus.SENT],
)
async def test_deadline_automation_reconcile_preserves_past_active_state(
    status: ShiftTimelineEventStatus,
) -> None:
    db_url = "sqlite://:memory:"
    await asyncio.wait_for(init_db(db_url), timeout=3)
    try:
        now = dt.datetime(2026, 8, 1, 12, tzinfo=dt.UTC)
        deadline = now - dt.timedelta(seconds=1)
        _channel, config, manager = await _create_deadline_manager(deadline=deadline)
        config.deadline_automation_enabled = True
        await config.save(update_fields=["deadline_automation_enabled", "updated_at"])
        state = await ShiftTimelineEventState.create(
            shift_register=config,
            event_kind=ShiftTimelineEventKind.SUBMISSION_DEADLINE,
            scheduled_at=deadline,
            delivery_nonce=123,
            status=status,
            message_id=456 if status is ShiftTimelineEventStatus.SENT else None,
        )

        result = await manager.reconcile_deadline_automation(now=now)

        assert result.schedule_change is None
        assert result.auto_close_disabled is False
        await config.refresh_from_db()
        await state.refresh_from_db()
        assert config.deadline_automation_enabled is True
        assert state.status is status
        assert state.scheduled_at == deadline
        assert state.delivery_nonce == 123
    finally:
        await asyncio.wait_for(close_db(db_url), timeout=3)


@pytest.mark.asyncio
async def test_submission_deadline_close_delivery_transitions_are_idempotent() -> None:
    db_url = "sqlite://:memory:"
    await asyncio.wait_for(init_db(db_url), timeout=3)
    try:
        now = dt.datetime(2026, 8, 1, 12, tzinfo=dt.UTC)
        deadline = now + dt.timedelta(days=1)
        feature_channel, config, manager = await _create_deadline_manager(
            deadline=deadline
        )
        await manager.set_deadline_automation_enabled(enabled=True, now=now)
        state = await _get_deadline_state(config)
        assert state is not None

        assert (
            await manager.begin_submission_deadline_close(
                expected_scheduled_at=deadline,
                expected_delivery_nonce=state.delivery_nonce + 1,
                now=deadline,
            )
            is None
        )
        assert (
            await manager.begin_submission_deadline_close(
                expected_scheduled_at=deadline - dt.timedelta(seconds=1),
                expected_delivery_nonce=state.delivery_nonce,
                now=deadline,
            )
            is None
        )
        assert (
            await manager.begin_submission_deadline_close(
                expected_scheduled_at=deadline,
                expected_delivery_nonce=state.delivery_nonce,
                now=deadline - dt.timedelta(seconds=1),
            )
            is None
        )

        fresh_config = await ShiftRegisterConfig.get(id=config.id)
        fresh_config.submission_deadline_at = deadline + dt.timedelta(hours=1)
        await fresh_config.save(update_fields=["submission_deadline_at", "updated_at"])
        assert (
            await manager.begin_submission_deadline_close(
                expected_scheduled_at=deadline,
                expected_delivery_nonce=state.delivery_nonce,
                now=deadline,
            )
            is None
        )
        await feature_channel.refresh_from_db()
        assert feature_channel.is_enabled is True
        fresh_config.submission_deadline_at = deadline
        await fresh_config.save(update_fields=["submission_deadline_at", "updated_at"])

        execution = await manager.begin_submission_deadline_close(
            expected_scheduled_at=deadline,
            expected_delivery_nonce=state.delivery_nonce,
            now=deadline,
        )
        assert execution is not None
        await feature_channel.refresh_from_db()
        assert feature_channel.is_enabled is False
        await config.refresh_from_db()
        await state.refresh_from_db()
        assert config.deadline_automation_enabled is True
        assert state.status is ShiftTimelineEventStatus.SCHEDULED

        assert await manager.mark_submission_deadline_sent(
            event_state_id=state.id,
            delivery_nonce=state.delivery_nonce,
            message_id=555,
        )
        assert await manager.mark_submission_deadline_sent(
            event_state_id=state.id,
            delivery_nonce=state.delivery_nonce,
            message_id=555,
        )
        assert not await manager.mark_submission_deadline_sent(
            event_state_id=state.id,
            delivery_nonce=state.delivery_nonce,
            message_id=556,
        )
        assert not await manager.mark_submission_deadline_sent(
            event_state_id=state.id,
            delivery_nonce=state.delivery_nonce + 1,
            message_id=555,
        )
        assert await manager.complete_submission_deadline(
            event_state_id=state.id,
            delivery_nonce=state.delivery_nonce,
        )
        await config.refresh_from_db()
        await state.refresh_from_db()
        assert config.deadline_automation_enabled is False
        assert state.status is ShiftTimelineEventStatus.COMPLETED
        assert state.message_id == 555
        assert not await manager.complete_submission_deadline(
            event_state_id=state.id,
            delivery_nonce=state.delivery_nonce,
        )
        assert (
            await manager.begin_submission_deadline_close(
                expected_scheduled_at=deadline,
                expected_delivery_nonce=state.delivery_nonce,
                now=deadline,
            )
            is None
        )
    finally:
        await asyncio.wait_for(close_db(db_url), timeout=3)


@pytest.mark.asyncio
async def test_manual_lifecycle_clears_deadline_state_and_hard_clear_cascades() -> None:
    db_url = "sqlite://:memory:"
    await asyncio.wait_for(init_db(db_url), timeout=3)
    try:
        now = dt.datetime(2026, 8, 1, 12, tzinfo=dt.UTC)
        deadline = now + dt.timedelta(days=1)
        feature_channel, config, manager = await _create_deadline_manager(
            deadline=deadline
        )
        await manager.set_deadline_automation_enabled(enabled=True, now=now)

        config_id = await manager.set_manual_feature_enabled(enabled=False)
        assert config_id == config.id
        await feature_channel.refresh_from_db()
        await config.refresh_from_db()
        assert feature_channel.is_enabled is False
        assert config.deadline_automation_enabled is False
        assert await _get_deadline_state(config) is None

        config.deadline_automation_enabled = True
        await config.save(update_fields=["deadline_automation_enabled", "updated_at"])
        await ShiftTimelineEventState.create(
            shift_register=config,
            event_kind=ShiftTimelineEventKind.SUBMISSION_DEADLINE,
            scheduled_at=deadline,
            delivery_nonce=1,
        )
        config_id = await manager.set_manual_feature_enabled(enabled=True)
        assert config_id == config.id
        await feature_channel.refresh_from_db()
        assert feature_channel.is_enabled is True
        assert await _get_deadline_state(config) is None

        config.deadline_automation_enabled = True
        await config.save(update_fields=["deadline_automation_enabled", "updated_at"])
        await ShiftTimelineEventState.create(
            shift_register=config,
            event_kind=ShiftTimelineEventKind.SUBMISSION_DEADLINE,
            scheduled_at=deadline,
            delivery_nonce=2,
        )
        assert await manager.clear_feature_settings() == config.id
        assert await FeatureChannel.get_or_none(id=feature_channel.id) is None
        assert await ShiftTimelineEventState.all().count() == 0
    finally:
        await asyncio.wait_for(close_db(db_url), timeout=3)
