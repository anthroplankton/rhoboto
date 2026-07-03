from __future__ import annotations

import asyncio

import pytest
from tortoise import Tortoise

from models.feature_channel import FeatureChannel
from models.guild_language_settings import GuildLanguageSettings
from models.shift_register import ShiftRegisterConfig
from models.team_register import TeamRegisterConfig
from utils.db import close_db, get_model_modules, init_db


@pytest.mark.asyncio
async def test_tortoise_model_registry_init_smoke() -> None:
    await Tortoise.init(db_url="sqlite://:memory:", modules=get_model_modules())
    try:
        apps = Tortoise.apps
        assert apps is not None
        assert sorted(apps["models"]) == [
            "FeatureChannel",
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
    finally:
        await Tortoise.close_connections()


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
