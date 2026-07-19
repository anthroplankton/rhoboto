from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from tortoise.queryset import QuerySet

from models.feature_channel import FeatureChannel
from models.shift_notice import ShiftNoticeConfig
from models.shift_register import ShiftRegisterConfig
from utils import (
    manager_base as manager_base_module,
    shift_notice_manager as shift_notice_manager_module,
)
from utils.db import close_db, init_db
from utils.google_sheets_urls import extract_google_sheet_id
from utils.shift_notice import (
    ShiftNoticeCatalog,
    ShiftNoticeFrameState,
    ShiftNoticeSnapshotPlan,
    ShiftNoticeSourceRecord,
    build_source_catalog,
    civil_start,
)
from utils.shift_notice_manager import (
    ShiftNoticeStaleStateError,
    claim_destination,
    get_destination_config,
    get_guild_config,
    replace_unavailable_destination,
    save_minute,
)
from utils.shift_schedule_role import ScheduleRoleLabelMatch
from utils.storage_errors import StorageError, StorageErrorKind

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

EVENT_DATE = date(2026, 8, 1)
CREATED_AT = datetime(2026, 1, 1, tzinfo=UTC)


async def _start_db() -> str:
    db_url = "sqlite://:memory:"
    await asyncio.wait_for(init_db(db_url), timeout=3)
    return db_url


@pytest.mark.asyncio
async def test_first_claim_creates_one_feature_channel_and_config_pair() -> None:
    db_url = await _start_db()
    try:
        claim = await claim_destination(1001, 2001)
        config = await get_guild_config(1001)

        assert config is not None
        assert claim.config_id == config.id
        assert claim.feature_channel_id == config.feature_channel_id
        assert claim.channel_id == 2001
        assert claim.created is True
        assert claim.owns_requested_destination is True
        assert config.minute_of_hour is None
        assert await get_destination_config(1001, 2001) is not None
        assert await ShiftNoticeConfig.filter(guild_id=1001).count() == 1
        assert await FeatureChannel.filter(feature_name="shift_notice").count() == 1
    finally:
        await asyncio.wait_for(close_db(db_url), timeout=3)


@pytest.mark.asyncio
async def test_locked_config_queries_scope_for_update_to_config_table(
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
        if query.model is ShiftNoticeConfig:
            lock_scopes.append(of)
        return select_for_update(
            query,
            nowait=nowait,
            skip_locked=skip_locked,
            of=of,
            no_key=no_key,
        )

    monkeypatch.setattr(QuerySet, "select_for_update", record_lock_scope)
    db_url = await _start_db()
    try:
        claim = await claim_destination(1001, 2001)
        config = await ShiftNoticeConfig.get(id=claim.config_id)
        config = await save_minute(
            config.id,
            expected_updated_at=config.updated_at,
            expected_minute=None,
            new_minute=15,
            setup_only=True,
        )
        await replace_unavailable_destination(config.id, 2001, 2002)

        assert lock_scopes == [("shift_notice_config",)] * 3
    finally:
        await asyncio.wait_for(close_db(db_url), timeout=3)


@pytest.mark.asyncio
async def test_concurrent_losing_claim_returns_database_winner() -> None:
    db_url = await _start_db()
    try:
        first, second = await asyncio.gather(
            claim_destination(1001, 2001),
            claim_destination(1001, 2002),
        )

        assert first.config_id == second.config_id
        assert [first.created, second.created].count(True) == 1
        assert first.owns_requested_destination is not second.owns_requested_destination
        assert await ShiftNoticeConfig.filter(guild_id=1001).count() == 1
        assert await FeatureChannel.filter(feature_name="shift_notice").count() == 1
    finally:
        await asyncio.wait_for(close_db(db_url), timeout=3)


@pytest.mark.asyncio
async def test_same_channel_claim_reenables_and_retains_minute() -> None:
    db_url = await _start_db()
    try:
        claim = await claim_destination(1001, 2001)
        config = await ShiftNoticeConfig.get(id=claim.config_id)
        config = await save_minute(
            config.id,
            expected_updated_at=config.updated_at,
            expected_minute=None,
            new_minute=15,
            setup_only=True,
        )
        feature_channel = await FeatureChannel.get(id=config.feature_channel_id)
        feature_channel.is_enabled = False
        await feature_channel.save()

        assert await get_destination_config(1001, 2001) is not None
        assert await get_destination_config(1001, 2001, require_enabled=True) is None

        reclaimed = await claim_destination(1001, 2001)
        await feature_channel.refresh_from_db()
        retained = await ShiftNoticeConfig.get(id=config.id)

        assert reclaimed.created is False
        assert reclaimed.owns_requested_destination is True
        assert feature_channel.is_enabled is True
        assert retained.minute_of_hour == 15
        assert await get_destination_config(1001, 2001, require_enabled=True)
    finally:
        await asyncio.wait_for(close_db(db_url), timeout=3)


@pytest.mark.asyncio
async def test_soft_disable_retains_minute_and_hard_clear_cascades_config() -> None:
    db_url = await _start_db()
    try:
        claim = await claim_destination(1001, 2001)
        config = await ShiftNoticeConfig.get(id=claim.config_id)
        config = await save_minute(
            config.id,
            expected_updated_at=config.updated_at,
            expected_minute=None,
            new_minute=45,
            setup_only=True,
        )
        feature_channel = await FeatureChannel.get(id=config.feature_channel_id)

        feature_channel.is_enabled = False
        await feature_channel.save()
        retained = await ShiftNoticeConfig.get(id=config.id)
        assert retained.minute_of_hour == 45

        await feature_channel.delete()
        assert await ShiftNoticeConfig.filter(id=config.id).count() == 0
    finally:
        await asyncio.wait_for(close_db(db_url), timeout=3)


@pytest.mark.asyncio
async def test_other_channel_claim_preserves_usable_destination_owner() -> None:
    db_url = await _start_db()
    try:
        first = await claim_destination(1001, 2001)
        claim = await claim_destination(1001, 2002)

        assert claim.config_id == first.config_id
        assert claim.feature_channel_id == first.feature_channel_id
        assert claim.channel_id == 2001
        assert claim.created is False
        assert claim.owns_requested_destination is False
        assert await FeatureChannel.filter(feature_name="shift_notice").count() == 1
    finally:
        await asyncio.wait_for(close_db(db_url), timeout=3)


@pytest.mark.asyncio
async def test_replace_checks_snapshot_and_retains_minute() -> None:
    db_url = await _start_db()
    try:
        claim = await claim_destination(1001, 2001)
        config = await ShiftNoticeConfig.get(id=claim.config_id)
        config = await save_minute(
            config.id,
            expected_updated_at=config.updated_at,
            expected_minute=None,
            new_minute=42,
            setup_only=True,
        )
        feature_channel = await FeatureChannel.get(id=config.feature_channel_id)
        feature_channel.is_enabled = False
        await feature_channel.save()

        with pytest.raises(ShiftNoticeStaleStateError):
            await replace_unavailable_destination(config.id + 1, 2001, 2002)
        with pytest.raises(ShiftNoticeStaleStateError):
            await replace_unavailable_destination(config.id, 9999, 2002)

        replaced = await replace_unavailable_destination(config.id, 2001, 2002)
        await feature_channel.refresh_from_db()

        assert replaced.id == config.id
        assert replaced.minute_of_hour == 42
        assert feature_channel.channel_id == 2002
        assert feature_channel.is_enabled is True
    finally:
        await asyncio.wait_for(close_db(db_url), timeout=3)


@pytest.mark.asyncio
async def test_setup_rejects_non_null_current_minute() -> None:
    db_url = await _start_db()
    try:
        claim = await claim_destination(1001, 2001)
        config = await ShiftNoticeConfig.get(id=claim.config_id)
        config = await save_minute(
            config.id,
            expected_updated_at=config.updated_at,
            expected_minute=None,
            new_minute=10,
            setup_only=True,
        )

        with pytest.raises(ShiftNoticeStaleStateError):
            await save_minute(
                config.id,
                expected_updated_at=config.updated_at,
                expected_minute=10,
                new_minute=20,
                setup_only=True,
            )

        assert (await ShiftNoticeConfig.get(id=config.id)).minute_of_hour == 10
    finally:
        await asyncio.wait_for(close_db(db_url), timeout=3)


@pytest.mark.parametrize("mode", ["setup", "edit"])
@pytest.mark.asyncio
async def test_setup_and_edit_reject_stale_time_or_expected_minute(
    mode: str,
) -> None:
    db_url = await _start_db()
    try:
        setup_only = mode == "setup"
        claim = await claim_destination(1001, 2001)
        config = await ShiftNoticeConfig.get(id=claim.config_id)
        current_minute = None
        if not setup_only:
            config = await save_minute(
                config.id,
                expected_updated_at=config.updated_at,
                expected_minute=None,
                new_minute=15,
                setup_only=True,
            )
            current_minute = 15

        stale_at = config.updated_at
        await ShiftNoticeConfig.filter(id=config.id).update(
            updated_at=stale_at + timedelta(seconds=1)
        )

        with pytest.raises(ShiftNoticeStaleStateError):
            await save_minute(
                config.id,
                expected_updated_at=stale_at,
                expected_minute=current_minute,
                new_minute=30,
                setup_only=setup_only,
            )

        current = await ShiftNoticeConfig.get(id=config.id)
        wrong_expected = 1 if current_minute is None else current_minute + 1
        with pytest.raises(ShiftNoticeStaleStateError):
            await save_minute(
                config.id,
                expected_updated_at=current.updated_at,
                expected_minute=wrong_expected,
                new_minute=30,
                setup_only=setup_only,
            )
    finally:
        await asyncio.wait_for(close_db(db_url), timeout=3)


@pytest.mark.parametrize("new_minute", [-1, 60])
@pytest.mark.asyncio
async def test_save_minute_rejects_values_outside_hour(new_minute: int) -> None:
    db_url = await _start_db()
    try:
        claim = await claim_destination(1001, 2001)
        config = await ShiftNoticeConfig.get(id=claim.config_id)

        with pytest.raises(ValueError, match=r"0\.\.59"):
            await save_minute(
                config.id,
                expected_updated_at=config.updated_at,
                expected_minute=None,
                new_minute=new_minute,
                setup_only=True,
            )

        assert (await ShiftNoticeConfig.get(id=config.id)).minute_of_hour is None
    finally:
        await asyncio.wait_for(close_db(db_url), timeout=3)


@pytest.mark.asyncio
async def test_locked_reads_reject_feature_channel_guild_mismatch() -> None:
    db_url = await _start_db()
    try:
        feature_channel = await FeatureChannel.create(
            guild_id=2002,
            channel_id=2001,
            feature_name="shift_notice",
        )
        config = await ShiftNoticeConfig.create(
            feature_channel=feature_channel,
            guild_id=1001,
        )

        with pytest.raises(ShiftNoticeStaleStateError):
            await claim_destination(1001, 2001)
        with pytest.raises(ShiftNoticeStaleStateError):
            await replace_unavailable_destination(config.id, 2001, 2002)
        with pytest.raises(ShiftNoticeStaleStateError):
            await save_minute(
                config.id,
                expected_updated_at=config.updated_at,
                expected_minute=None,
                new_minute=30,
                setup_only=True,
            )
    finally:
        await asyncio.wait_for(close_db(db_url), timeout=3)


async def _create_shift_source(  # noqa: PLR0913
    guild_id: int,
    channel_id: int,
    *,
    is_enabled: bool = True,
    sheet_url: str = "https://docs.google.com/spreadsheets/d/source-sheet/edit",
    worksheet_id: int = 301,
    anchor: str = "B2",
    event_date: date | None = EVENT_DATE,
    ranges: object = None,
) -> tuple[FeatureChannel, ShiftRegisterConfig]:
    feature_channel = await FeatureChannel.create(
        guild_id=guild_id,
        channel_id=channel_id,
        feature_name="shift_register",
        is_enabled=is_enabled,
    )
    config = await ShiftRegisterConfig.create(
        feature_channel=feature_channel,
        sheet_url=sheet_url,
        entry_worksheet_id=worksheet_id + 1,
        draft_worksheet_id=worksheet_id + 2,
        final_schedule_worksheet_id=worksheet_id,
        final_schedule_anchor_cell=anchor,
        event_date=event_date,
        recruitment_time_ranges=(
            [{"start": 4, "end": 6}] if ranges is None else ranges
        ),
    )
    return feature_channel, config


@dataclass(frozen=True)
class _FakeWorksheet:
    id: int
    title: str
    values: list[list[object]]


class _FakeGoogleSheet:
    def __init__(
        self,
        spreadsheet_id: str,
        worksheet_values: dict[int, list[list[object]]],
        *,
        events: list[tuple[str, object]] | None = None,
        batch_failures: dict[int, Exception] | None = None,
    ) -> None:
        self.spreadsheet_id = spreadsheet_id
        self.worksheets = {
            worksheet_id: _FakeWorksheet(
                worksheet_id,
                f"Final {worksheet_id}",
                values,
            )
            for worksheet_id, values in worksheet_values.items()
        }
        self.events = events if events is not None else []
        self.batch_failures = batch_failures or {}
        self.get_calls: list[tuple[int, ...]] = []
        self.batch_calls: list[tuple[int, ...]] = []

    async def get_worksheets(
        self,
        worksheet_ids: list[int],
    ) -> dict[int, _FakeWorksheet | None]:
        call = tuple(worksheet_ids)
        self.get_calls.append(call)
        self.events.append(("get", (self.spreadsheet_id, call)))
        return {
            worksheet_id: self.worksheets.get(worksheet_id)
            for worksheet_id in worksheet_ids
        }

    async def batch_get_worksheet_values(
        self,
        worksheets: Sequence[_FakeWorksheet],
    ) -> dict[int, list[list[object]]]:
        call = tuple(worksheet.id for worksheet in worksheets)
        self.batch_calls.append(call)
        self.events.append(("batch", (self.spreadsheet_id, call)))
        failure = self.batch_failures.get(len(self.batch_calls))
        if failure is not None:
            raise failure
        return {worksheet.id: worksheet.values for worksheet in worksheets}


class _FakeGoogleSheetFactory:
    def __init__(
        self,
        sheets: dict[str, _FakeGoogleSheet],
        *,
        events: list[tuple[str, object]] | None = None,
    ) -> None:
        self.sheets = sheets
        self.events = events if events is not None else []
        self.calls: list[tuple[str, str]] = []

    def __call__(
        self,
        sheet_url: str,
        service_account_path: str,
    ) -> _FakeGoogleSheet:
        spreadsheet_id = extract_google_sheet_id(sheet_url)
        self.calls.append((sheet_url, service_account_path))
        self.events.append(("factory", spreadsheet_id))
        return self.sheets[spreadsheet_id]


class _RecordingWorksheetLock:
    def __init__(self, events: list[tuple[str, object]]) -> None:
        self.events = events

    @asynccontextmanager
    async def __call__(
        self,
        key: tuple[str, int],
    ) -> AsyncIterator[None]:
        self.events.append(("enter_lock", key))
        try:
            yield
        finally:
            self.events.append(("exit_lock", key))


def _source_record(  # noqa: PLR0913
    source_id: int,
    start: int,
    end: int,
    *,
    spreadsheet_id: str = "sheet-a",
    worksheet_id: int | None = None,
    anchor: str = "A1",
    url_suffix: str = "/edit",
    created_offset: int | None = None,
) -> ShiftNoticeSourceRecord:
    if worksheet_id is None:
        worksheet_id = 100 + source_id
    if created_offset is None:
        created_offset = source_id
    return ShiftNoticeSourceRecord(
        id=source_id,
        feature_channel_id=1000 + source_id,
        channel_id=2000 + source_id,
        is_enabled=True,
        created_at=CREATED_AT + timedelta(minutes=created_offset),
        sheet_url=(
            f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}{url_suffix}"
        ),
        final_schedule_worksheet_id=worksheet_id,
        final_schedule_anchor_cell=anchor,
        event_date=EVENT_DATE,
        recruitment_time_ranges=[{"start": start, "end": end}],
    )


def _catalog(*records: ShiftNoticeSourceRecord) -> ShiftNoticeCatalog:
    return build_source_catalog(records)


def _active_rows(count: int, label: str = "") -> list[list[object]]:
    return [["Runner", label, "", "", "", ""] for _ in range(count)]


def _cut_rows(count: int) -> list[list[object]]:
    return [["", "residual", "names", "are", "ignored", ""] for _ in range(count)]


def _matching_resolver(
    calls: list[tuple[str, ...]],
) -> shift_notice_manager_module.ShiftNoticeLabelResolver:
    def resolve(labels: Sequence[str]) -> tuple[ScheduleRoleLabelMatch, ...]:
        call = tuple(labels)
        calls.append(call)
        return tuple(ScheduleRoleLabelMatch(label, (10,)) for label in call)

    return resolve


@pytest.mark.asyncio
async def test_load_source_catalog_is_guild_scoped_and_database_only() -> None:
    db_url = await _start_db()
    try:
        _, enabled = await _create_shift_source(1001, 2001)
        _, disabled = await _create_shift_source(
            1001,
            2002,
            is_enabled=False,
            sheet_url="https://docs.google.com/spreadsheets/d/disabled/edit",
            worksheet_id=401,
            anchor="C3",
            ranges=[{"start": 6, "end": 8}],
        )
        _, other_guild = await _create_shift_source(1002, 2003)
        hard_feature, hard_deleted = await _create_shift_source(1001, 2004)
        _, incomplete = await _create_shift_source(
            1001,
            2005,
            sheet_url="https://docs.google.com/spreadsheets/d/incomplete/edit",
            worksheet_id=501,
            anchor="D4",
            event_date=None,
            ranges=[],
        )
        await hard_feature.delete()
        factory = _FakeGoogleSheetFactory({})
        manager = shift_notice_manager_module.ShiftNoticeManager(
            "service-account.json",
            google_sheet_factory=factory,
        )

        catalog = await manager.load_source_catalog(1001)

        complete_by_id = {source.id: source for source in catalog.complete_sources}
        assert set(complete_by_id) == {enabled.id, disabled.id}
        assert other_guild.id not in complete_by_id
        assert hard_deleted.id not in complete_by_id
        assert tuple(source.id for source in catalog.incomplete_sources) == (
            incomplete.id,
        )
        source = complete_by_id[disabled.id]
        assert source.feature_channel_id == disabled.feature_channel_id
        assert source.channel_id == 2002
        assert source.is_enabled is False
        assert source.created_at == disabled.created_at
        assert source.sheet_url == disabled.sheet_url
        assert source.final_schedule_worksheet_id == 401
        assert source.final_schedule_anchor_cell.a1 == "C3"
        assert source.event_date == EVENT_DATE
        assert [(item.start, item.end) for item in source.recruitment_time_ranges] == [
            (6, 8)
        ]
        warning = catalog.incomplete_sources[0]
        assert warning.id == incomplete.id
        assert warning.feature_channel_id == incomplete.feature_channel_id
        assert warning.channel_id == 2005
        assert warning.created_at == incomplete.created_at
        assert warning.sheet_url == incomplete.sheet_url
        assert warning.final_schedule_worksheet_id == 501
        assert warning.final_schedule_anchor_cell == "D4"
        assert warning.event_date is None
        assert warning.recruitment_time_ranges == []
        assert factory.calls == []
    finally:
        await asyncio.wait_for(close_db(db_url), timeout=3)


@pytest.mark.asyncio
async def test_build_snapshot_batches_initial_owners_by_spreadsheet() -> None:
    catalog = _catalog(
        _source_record(1, 3, 4),
        _source_record(2, 4, 5, url_suffix="/edit?gid=102"),
        _source_record(3, 5, 6, url_suffix="/edit#gid=103"),
        _source_record(4, 6, 7),
    )
    sheet = _FakeGoogleSheet(
        "sheet-a",
        {worksheet_id: _active_rows(1) for worksheet_id in range(101, 105)},
    )
    factory = _FakeGoogleSheetFactory({"sheet-a": sheet})
    manager = shift_notice_manager_module.ShiftNoticeManager(
        "service-account.json",
        google_sheet_factory=factory,
    )

    snapshot = await manager.build_snapshot(
        catalog,
        civil_start(EVENT_DATE, 5),
        _matching_resolver([]),
    )

    assert snapshot.previous.source_id == 2
    assert snapshot.next.source_id == 3
    assert len(factory.calls) == 1
    assert sheet.get_calls == [(102, 103)]
    assert sheet.batch_calls == [(102, 103)]


@pytest.mark.asyncio
async def test_build_snapshot_batches_each_spreadsheet_once_per_frontier() -> None:
    catalog = _catalog(
        _source_record(1, 4, 5, spreadsheet_id="sheet-a"),
        _source_record(2, 5, 6, spreadsheet_id="sheet-b"),
    )
    first = _FakeGoogleSheet("sheet-a", {101: _active_rows(1)})
    second = _FakeGoogleSheet("sheet-b", {102: _active_rows(1)})
    factory = _FakeGoogleSheetFactory({"sheet-a": first, "sheet-b": second})
    manager = shift_notice_manager_module.ShiftNoticeManager(
        "service-account.json",
        google_sheet_factory=factory,
    )

    snapshot = await manager.build_snapshot(
        catalog,
        civil_start(EVENT_DATE, 5),
        _matching_resolver([]),
    )

    assert (snapshot.previous.source_id, snapshot.next.source_id) == (1, 2)
    assert first.get_calls == [(101,)]
    assert first.batch_calls == [(101,)]
    assert second.get_calls == [(102,)]
    assert second.batch_calls == [(102,)]


@pytest.mark.asyncio
async def test_build_snapshot_deduplicates_a_shared_final_worksheet() -> None:
    catalog = _catalog(
        _source_record(1, 4, 5, worksheet_id=200),
        _source_record(2, 5, 6, worksheet_id=200),
    )
    sheet = _FakeGoogleSheet("sheet-a", {200: _active_rows(1)})
    factory = _FakeGoogleSheetFactory({"sheet-a": sheet})
    manager = shift_notice_manager_module.ShiftNoticeManager(
        "service-account.json",
        google_sheet_factory=factory,
    )

    snapshot = await manager.build_snapshot(
        catalog,
        civil_start(EVENT_DATE, 5),
        _matching_resolver([]),
    )

    assert (snapshot.previous.source_id, snapshot.next.source_id) == (1, 2)
    assert sheet.get_calls == [(200,)]
    assert sheet.batch_calls == [(200,)]


@pytest.mark.asyncio
async def test_build_snapshot_reuses_duration_grids_and_label_matches() -> None:
    catalog = _catalog(
        _source_record(1, 3, 4, worksheet_id=200),
        _source_record(2, 4, 5, worksheet_id=200),
        _source_record(3, 5, 6, worksheet_id=300),
        _source_record(4, 6, 7, worksheet_id=400),
    )
    sheet = _FakeGoogleSheet(
        "sheet-a",
        {
            200: _active_rows(1, "Alice"),
            300: _active_rows(1, "Alice"),
            400: _active_rows(1, "Alice"),
        },
    )
    resolver_calls: list[tuple[str, ...]] = []
    factory = _FakeGoogleSheetFactory({"sheet-a": sheet})
    manager = shift_notice_manager_module.ShiftNoticeManager(
        "service-account.json",
        google_sheet_factory=factory,
    )

    snapshot = await manager.build_snapshot(
        catalog,
        civil_start(EVENT_DATE, 5),
        _matching_resolver(resolver_calls),
    )

    assert sheet.get_calls == [(200, 300), (400,)]
    assert sheet.batch_calls == [(200, 300), (400,)]
    assert resolver_calls == [("Alice",)]
    assert snapshot.cumulative_hours == {("member", 10): 2}
    assert snapshot.remaining_hours == {("member", 10): 2}
    assert sum(len(call) for call in sheet.batch_calls) == len(
        {worksheet_id for call in sheet.batch_calls for worksheet_id in call}
    )


@pytest.mark.asyncio
async def test_build_snapshot_cut_frontier_reads_only_adjacent_sources() -> None:
    catalog = _catalog(
        _source_record(1, 1, 2),
        _source_record(2, 2, 9),
        _source_record(3, 9, 10),
    )
    sheet = _FakeGoogleSheet(
        "sheet-a",
        {
            101: _cut_rows(1),
            102: _cut_rows(7),
            103: _cut_rows(1),
        },
    )
    factory = _FakeGoogleSheetFactory({"sheet-a": sheet})
    manager = shift_notice_manager_module.ShiftNoticeManager(
        "service-account.json",
        google_sheet_factory=factory,
    )

    snapshot = await manager.build_snapshot(
        catalog,
        civil_start(EVENT_DATE, 5),
        _matching_resolver([]),
    )

    assert snapshot.cut_window is not None
    assert sheet.get_calls == [(102,), (101, 103)]
    assert sheet.batch_calls == [(102,), (101, 103)]


@pytest.mark.asyncio
async def test_build_snapshot_locks_every_complete_source_before_sheets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = _catalog(
        _source_record(1, 3, 4, spreadsheet_id="sheet-b", worksheet_id=200),
        _source_record(2, 4, 5, spreadsheet_id="sheet-a", worksheet_id=300),
        _source_record(3, 5, 6, spreadsheet_id="sheet-a", worksheet_id=100),
        _source_record(4, 6, 7, spreadsheet_id="sheet-a", worksheet_id=100),
    )
    events: list[tuple[str, object]] = []
    monkeypatch.setattr(
        manager_base_module,
        "WORKSHEET_TRANSACTION_LOCK",
        _RecordingWorksheetLock(events),
    )
    first = _FakeGoogleSheet(
        "sheet-a",
        {100: _active_rows(1), 300: _active_rows(1)},
        events=events,
    )
    second = _FakeGoogleSheet("sheet-b", {200: _active_rows(1)}, events=events)
    factory = _FakeGoogleSheetFactory(
        {"sheet-a": first, "sheet-b": second},
        events=events,
    )
    manager = shift_notice_manager_module.ShiftNoticeManager(
        "service-account.json",
        google_sheet_factory=factory,
    )

    await manager.build_snapshot(
        catalog,
        civil_start(EVENT_DATE, 5),
        _matching_resolver([]),
    )

    expected = [
        ("enter_lock", ("sheet-a", 100)),
        ("enter_lock", ("sheet-a", 300)),
        ("enter_lock", ("sheet-b", 200)),
    ]
    assert events[:3] == expected
    assert [event for event in events if event[0] == "enter_lock"] == expected
    assert events.index(("factory", "sheet-a")) > 2
    assert {extract_google_sheet_id(call[0]) for call in factory.calls} == {"sheet-a"}


@pytest.mark.asyncio
async def test_build_snapshot_projects_only_database_derived_rectangle() -> None:
    catalog = _catalog(_source_record(1, 4, 5, anchor="C2"))
    outside = object()
    sheet = _FakeGoogleSheet(
        "sheet-a",
        {
            101: [
                [outside, 7, "outside"],
                [outside, outside, "Runner", "Alice", "", "", "", "", outside],
                [outside],
            ]
        },
    )
    resolver_calls: list[tuple[str, ...]] = []
    factory = _FakeGoogleSheetFactory({"sheet-a": sheet})
    manager = shift_notice_manager_module.ShiftNoticeManager(
        "service-account.json",
        google_sheet_factory=factory,
    )

    snapshot = await manager.build_snapshot(
        catalog,
        civil_start(EVENT_DATE, 4),
        _matching_resolver(resolver_calls),
    )

    assert snapshot.next.state is ShiftNoticeFrameState.ACTIVE_STAFFED
    assert snapshot.next.lanes[0] is not None
    assert snapshot.next.lanes[0].schedule_label == "Alice"
    assert resolver_calls == [("Alice",)]


@pytest.mark.asyncio
async def test_build_snapshot_masks_poisoned_overlap_loser_rows() -> None:
    catalog = _catalog(
        _source_record(1, 5, 6, worksheet_id=101),
        _source_record(2, 4, 6, worksheet_id=102),
    )
    sheet = _FakeGoogleSheet(
        "sheet-a",
        {
            101: _active_rows(1, "Next owner"),
            102: [
                _active_rows(1, "Previous owner")[0],
                ["Runner", "Ignored loser label", 7, "", "", ""],
            ],
        },
    )
    resolver_calls: list[tuple[str, ...]] = []
    factory = _FakeGoogleSheetFactory({"sheet-a": sheet})
    manager = shift_notice_manager_module.ShiftNoticeManager(
        "service-account.json",
        google_sheet_factory=factory,
    )

    snapshot = await manager.build_snapshot(
        catalog,
        civil_start(EVENT_DATE, 5),
        _matching_resolver(resolver_calls),
    )

    assert (snapshot.previous.source_id, snapshot.next.source_id) == (2, 1)
    assert resolver_calls == [("Next owner", "Previous owner")]
    assert all("Ignored loser label" not in labels for labels in resolver_calls)


@pytest.mark.asyncio
async def test_build_snapshot_missing_selected_worksheet_does_not_fall_back() -> None:
    catalog = _catalog(
        _source_record(1, 4, 5, worksheet_id=101),
        _source_record(2, 4, 5, worksheet_id=102),
    )
    sheet = _FakeGoogleSheet("sheet-a", {102: _active_rows(1)})
    factory = _FakeGoogleSheetFactory({"sheet-a": sheet})
    manager = shift_notice_manager_module.ShiftNoticeManager(
        "service-account.json",
        google_sheet_factory=factory,
    )

    with pytest.raises(StorageError) as error:
        await manager.build_snapshot(
            catalog,
            civil_start(EVENT_DATE, 4),
            _matching_resolver([]),
        )

    assert error.value.kind is StorageErrorKind.GOOGLE_SHEETS_MISSING_WORKSHEET
    assert sheet.get_calls == [(101,)]
    assert sheet.batch_calls == []


@pytest.mark.asyncio
async def test_build_snapshot_rejects_required_adjacent_read_failure() -> None:
    catalog = _catalog(
        _source_record(1, 3, 4),
        _source_record(2, 4, 5),
        _source_record(3, 5, 6),
        _source_record(4, 6, 7),
    )
    sheet = _FakeGoogleSheet(
        "sheet-a",
        {worksheet_id: _active_rows(1, "Alice") for worksheet_id in range(101, 105)},
        batch_failures={2: RuntimeError("adjacent read failed")},
    )
    factory = _FakeGoogleSheetFactory({"sheet-a": sheet})
    manager = shift_notice_manager_module.ShiftNoticeManager(
        "service-account.json",
        google_sheet_factory=factory,
    )

    with pytest.raises(RuntimeError, match="adjacent read failed"):
        await manager.build_snapshot(
            catalog,
            civil_start(EVENT_DATE, 5),
            _matching_resolver([]),
        )

    assert sheet.batch_calls == [(102, 103), (101, 104)]


@pytest.mark.asyncio
async def test_build_snapshot_separate_spreadsheets_are_best_effort() -> None:
    events: list[tuple[str, object]] = []
    catalog = _catalog(
        _source_record(1, 4, 5, spreadsheet_id="sheet-a"),
        _source_record(2, 5, 6, spreadsheet_id="sheet-b"),
    )
    first = _FakeGoogleSheet("sheet-a", {101: _active_rows(1)}, events=events)
    second = _FakeGoogleSheet(
        "sheet-b",
        {102: _active_rows(1)},
        events=events,
        batch_failures={1: RuntimeError("second spreadsheet failed")},
    )
    factory = _FakeGoogleSheetFactory(
        {"sheet-a": first, "sheet-b": second},
        events=events,
    )
    manager = shift_notice_manager_module.ShiftNoticeManager(
        "service-account.json",
        google_sheet_factory=factory,
    )

    with pytest.raises(RuntimeError, match="second spreadsheet failed"):
        await manager.build_snapshot(
            catalog,
            civil_start(EVENT_DATE, 5),
            _matching_resolver([]),
        )

    assert first.batch_calls == [(101,)]
    assert second.batch_calls == [(102,)]
    assert events.index(("batch", ("sheet-a", (101,)))) < events.index(
        ("batch", ("sheet-b", (102,)))
    )


@pytest.mark.asyncio
async def test_build_snapshot_rejects_a_repeated_loaded_source_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = _catalog(_source_record(1, 4, 5))
    sheet = _FakeGoogleSheet("sheet-a", {101: _active_rows(1)})
    factory = _FakeGoogleSheetFactory({"sheet-a": sheet})
    manager = shift_notice_manager_module.ShiftNoticeManager(
        "service-account.json",
        google_sheet_factory=factory,
    )

    def stalled_plan(*_args: object) -> ShiftNoticeSnapshotPlan:
        return ShiftNoticeSnapshotPlan(frozenset({1}), None)

    monkeypatch.setattr(shift_notice_manager_module, "plan_snapshot", stalled_plan)

    with pytest.raises(RuntimeError, match="already loaded"):
        await manager.build_snapshot(
            catalog,
            civil_start(EVENT_DATE, 4),
            _matching_resolver([]),
        )

    assert sheet.batch_calls == [(101,)]
