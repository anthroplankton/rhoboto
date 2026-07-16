from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from tortoise.exceptions import IntegrityError
from tortoise.transactions import in_transaction

from models.feature_channel import FeatureChannel
from models.shift_notice import ShiftNoticeConfig
from models.shift_register import ShiftRegisterConfig
from utils.google_sheets import GoogleSheet
from utils.manager_base import worksheet_transaction_key, worksheet_transactions
from utils.shift_notice import (
    ShiftNoticeCatalog,
    ShiftNoticeSnapshot,
    ShiftNoticeSource,
    ShiftNoticeSourceRecord,
    build_source_catalog,
    plan_snapshot,
    project_source_frames,
)
from utils.shift_schedule_role import ScheduleRoleLabelMatch
from utils.storage_errors import StorageError, StorageErrorKind

if TYPE_CHECKING:
    from datetime import datetime

MINUTE_OF_HOUR_MAX = 59

type ShiftNoticeLabelResolver = Callable[
    [Sequence[str]],
    tuple[ScheduleRoleLabelMatch, ...],
]


def _mask_unowned_source_rows(
    source: ShiftNoticeSource,
    worksheet_values: Sequence[Sequence[object]],
    catalog: ShiftNoticeCatalog,
) -> list[Sequence[object]]:
    rows = list(worksheet_values)
    first_row = source.final_schedule_anchor_cell.row - 1
    for event_hour in range(source.first_hour, source.end_hour):
        if catalog.slot_owners.get(source.civil_start(event_hour)) == source.id:
            continue
        row_index = first_row + event_hour - source.first_hour
        if row_index < len(rows):
            rows[row_index] = ()
    return rows


class ShiftNoticeConfigNotFoundError(LookupError):
    pass


class ShiftNoticeStaleStateError(RuntimeError):
    pass


@dataclass(frozen=True)
class ShiftNoticeDestinationClaim:
    config_id: int
    feature_channel_id: int
    channel_id: int
    created: bool
    owns_requested_destination: bool


class ShiftNoticeManager:
    def __init__(
        self,
        service_account_path: str,
        *,
        google_sheet_factory: Callable[[str, str], GoogleSheet] = GoogleSheet,
    ) -> None:
        self.service_account_path = service_account_path
        self._google_sheet_factory = google_sheet_factory

    async def load_source_catalog(self, guild_id: int) -> ShiftNoticeCatalog:
        configs = await ShiftRegisterConfig.filter(
            feature_channel__guild_id=guild_id
        ).select_related("feature_channel")
        return build_source_catalog(
            tuple(
                ShiftNoticeSourceRecord(
                    id=config.id,
                    feature_channel_id=config.feature_channel_id,
                    channel_id=config.feature_channel.channel_id,
                    is_enabled=config.feature_channel.is_enabled,
                    created_at=config.created_at,
                    sheet_url=config.sheet_url,
                    final_schedule_worksheet_id=config.final_schedule_worksheet_id,
                    final_schedule_anchor_cell=config.final_schedule_anchor_cell,
                    event_date=config.event_date,
                    recruitment_time_ranges=config.recruitment_time_ranges,
                )
                for config in configs
            )
        )

    async def build_snapshot(  # noqa: C901, PLR0912, PLR0915
        self,
        catalog: ShiftNoticeCatalog,
        target_boundary: datetime,
        resolve_labels: ShiftNoticeLabelResolver,
    ) -> ShiftNoticeSnapshot:
        sources_by_id = {source.id: source for source in catalog.complete_sources}
        resources = [
            worksheet_transaction_key(
                source.sheet_url,
                source.final_schedule_worksheet_id,
            )
            for source in catalog.complete_sources
        ]
        google_sheets: dict[str, GoogleSheet] = {}
        worksheets: dict[tuple[str, int], object] = {}
        grids: dict[tuple[str, int], list[list[object]]] = {}
        loaded_source_ids: set[int] = set()
        loaded_frames = {}
        label_matches: dict[str, ScheduleRoleLabelMatch] = {}
        resolved_labels: set[str] = set()

        async with worksheet_transactions(resources):
            while True:
                plan = plan_snapshot(catalog, loaded_frames, target_boundary)
                if plan.snapshot is not None:
                    if plan.missing_source_ids:
                        msg = "Completed Shift Notice plan still requires sources."
                        raise RuntimeError(msg)
                    return plan.snapshot

                missing_source_ids = plan.missing_source_ids
                if not missing_source_ids:
                    msg = "Shift Notice snapshot planner made no progress."
                    raise RuntimeError(msg)
                if missing_source_ids & loaded_source_ids:
                    msg = (
                        "Shift Notice snapshot planner requested an already loaded "
                        "source."
                    )
                    raise RuntimeError(msg)
                if not missing_source_ids.issubset(sources_by_id):
                    msg = "Shift Notice snapshot planner requested an unknown source."
                    raise RuntimeError(msg)
                frontier = tuple(
                    source
                    for source in catalog.complete_sources
                    if source.id in missing_source_ids
                )

                groups: dict[str, list[ShiftNoticeSource]] = {}
                for source in frontier:
                    groups.setdefault(source.spreadsheet_id, []).append(source)

                for spreadsheet_id, sources in groups.items():
                    sheet = google_sheets.get(spreadsheet_id)
                    if sheet is None:
                        sheet = self._google_sheet_factory(
                            sources[0].sheet_url,
                            self.service_account_path,
                        )
                        google_sheets[spreadsheet_id] = sheet

                    worksheet_ids = sorted(
                        {
                            source.final_schedule_worksheet_id
                            for source in sources
                            if (
                                spreadsheet_id,
                                source.final_schedule_worksheet_id,
                            )
                            not in worksheets
                        }
                    )
                    if worksheet_ids:
                        resolved = await sheet.get_worksheets(worksheet_ids)
                        for worksheet_id in worksheet_ids:
                            worksheet = resolved.get(worksheet_id)
                            if worksheet is None:
                                raise StorageError(
                                    StorageErrorKind.GOOGLE_SHEETS_MISSING_WORKSHEET
                                )
                            worksheets[(spreadsheet_id, worksheet_id)] = worksheet

                    unread_ids = sorted(
                        {
                            source.final_schedule_worksheet_id
                            for source in sources
                            if (
                                spreadsheet_id,
                                source.final_schedule_worksheet_id,
                            )
                            not in grids
                        }
                    )
                    if unread_ids:
                        values = await sheet.batch_get_worksheet_values(
                            [
                                worksheets[(spreadsheet_id, worksheet_id)]
                                for worksheet_id in unread_ids
                            ]
                        )
                        for worksheet_id in unread_ids:
                            grids[(spreadsheet_id, worksheet_id)] = values[worksheet_id]

                masked_grids = {
                    source.id: _mask_unowned_source_rows(
                        source,
                        grids[
                            (
                                source.spreadsheet_id,
                                source.final_schedule_worksheet_id,
                            )
                        ],
                        catalog,
                    )
                    for source in frontier
                }
                new_labels: list[str] = []
                for source in frontier:
                    frames = project_source_frames(
                        source,
                        masked_grids[source.id],
                        label_matches,
                    )
                    for frame in frames.values():
                        for person in frame.lanes:
                            if (
                                person is not None
                                and person.schedule_label not in resolved_labels
                                and person.schedule_label not in new_labels
                            ):
                                new_labels.append(person.schedule_label)

                if new_labels:
                    label_matches.update(
                        {
                            match.label: match
                            for match in resolve_labels(tuple(new_labels))
                        }
                    )
                    resolved_labels.update(new_labels)

                for source in frontier:
                    frames = project_source_frames(
                        source,
                        masked_grids[source.id],
                        label_matches,
                    )
                    loaded_frames.update(
                        {
                            start: frame
                            for start, frame in frames.items()
                            if catalog.slot_owners.get(start) == source.id
                        }
                    )
                    loaded_source_ids.add(source.id)


async def _get_config_with_feature(
    guild_id: int,
    *,
    using_db: object | None = None,
    lock: bool = False,
) -> ShiftNoticeConfig | None:
    query = ShiftNoticeConfig.filter(guild_id=guild_id)
    if using_db is not None:
        query = query.using_db(using_db)
    query = query.select_related("feature_channel")
    if lock:
        query = query.select_for_update()
    config = await query.first()
    if config is None:
        return None
    if config.feature_channel.guild_id != config.guild_id:
        raise ShiftNoticeStaleStateError
    return config


async def get_guild_config(
    guild_id: int,
    *,
    using_db: object | None = None,
) -> ShiftNoticeConfig | None:
    return await _get_config_with_feature(guild_id, using_db=using_db)


async def get_destination_config(
    guild_id: int,
    channel_id: int,
    *,
    require_enabled: bool = False,
) -> ShiftNoticeConfig | None:
    config = await get_guild_config(guild_id)
    if config is None or config.feature_channel.channel_id != channel_id:
        return None
    if require_enabled and not config.feature_channel.is_enabled:
        return None
    return config


def _claim_from_config(
    config: ShiftNoticeConfig,
    *,
    requested_channel_id: int,
    created: bool,
) -> ShiftNoticeDestinationClaim:
    feature_channel = config.feature_channel
    return ShiftNoticeDestinationClaim(
        config_id=config.id,
        feature_channel_id=feature_channel.id,
        channel_id=feature_channel.channel_id,
        created=created,
        owns_requested_destination=feature_channel.channel_id == requested_channel_id,
    )


async def claim_destination(
    guild_id: int,
    channel_id: int,
) -> ShiftNoticeDestinationClaim:
    try:
        async with in_transaction() as connection:
            config = await _get_config_with_feature(
                guild_id,
                using_db=connection,
                lock=True,
            )
            if config is not None:
                feature_channel = await (
                    FeatureChannel.filter(id=config.feature_channel_id)
                    .using_db(connection)
                    .select_for_update()
                    .first()
                )
                if (
                    feature_channel is None
                    or feature_channel.guild_id != config.guild_id
                ):
                    raise ShiftNoticeStaleStateError
                if (
                    feature_channel.channel_id == channel_id
                    and not feature_channel.is_enabled
                ):
                    feature_channel.is_enabled = True
                    await feature_channel.save(
                        using_db=connection,
                        update_fields=["is_enabled", "updated_at"],
                    )
                    config.feature_channel = feature_channel
                return _claim_from_config(
                    config,
                    requested_channel_id=channel_id,
                    created=False,
                )

            feature_channel = await (
                FeatureChannel.filter(
                    guild_id=guild_id,
                    channel_id=channel_id,
                    feature_name="shift_notice",
                )
                .using_db(connection)
                .first()
            )
            if feature_channel is None:
                feature_channel = await FeatureChannel.create(
                    using_db=connection,
                    guild_id=guild_id,
                    channel_id=channel_id,
                    feature_name="shift_notice",
                )
            config = await ShiftNoticeConfig.create(
                using_db=connection,
                feature_channel=feature_channel,
                guild_id=guild_id,
            )
            config.feature_channel = feature_channel
            return _claim_from_config(
                config,
                requested_channel_id=channel_id,
                created=True,
            )
    except IntegrityError:
        winner = await get_guild_config(guild_id)
        if winner is None:
            raise
        return _claim_from_config(
            winner,
            requested_channel_id=channel_id,
            created=False,
        )


async def replace_unavailable_destination(
    config_id: int,
    expected_channel_id: int,
    new_channel_id: int,
) -> ShiftNoticeConfig:
    async with in_transaction() as connection:
        config = await (
            ShiftNoticeConfig.filter(id=config_id)
            .using_db(connection)
            .select_related("feature_channel")
            .select_for_update()
            .first()
        )
        if config is None:
            raise ShiftNoticeStaleStateError
        feature_channel = await (
            FeatureChannel.filter(id=config.feature_channel_id)
            .using_db(connection)
            .select_for_update()
            .first()
        )
        if (
            feature_channel is None
            or feature_channel.guild_id != config.guild_id
            or feature_channel.channel_id != expected_channel_id
        ):
            raise ShiftNoticeStaleStateError
        feature_channel.channel_id = new_channel_id
        feature_channel.is_enabled = True
        await feature_channel.save(
            using_db=connection,
            update_fields=["channel_id", "is_enabled", "updated_at"],
        )
        config.feature_channel = feature_channel
        return config


async def _get_locked_config(
    config_id: int,
    connection: object,
) -> ShiftNoticeConfig:
    config = await (
        ShiftNoticeConfig.filter(id=config_id)
        .using_db(connection)
        .select_related("feature_channel")
        .select_for_update()
        .first()
    )
    if config is None:
        raise ShiftNoticeConfigNotFoundError(config_id)
    if config.guild_id != config.feature_channel.guild_id:
        raise ShiftNoticeStaleStateError
    return config


async def save_minute(
    config_id: int,
    *,
    expected_updated_at: datetime,
    expected_minute: int | None,
    new_minute: int,
    setup_only: bool,
) -> ShiftNoticeConfig:
    if not 0 <= new_minute <= MINUTE_OF_HOUR_MAX:
        message = "new_minute must be in 0..59"
        raise ValueError(message)

    async with in_transaction() as connection:
        config = await _get_locked_config(config_id, connection)
        if (
            config.updated_at != expected_updated_at
            or config.minute_of_hour != expected_minute
            or (setup_only and config.minute_of_hour is not None)
        ):
            raise ShiftNoticeStaleStateError
        config.minute_of_hour = new_minute
        await config.save(
            using_db=connection,
            update_fields=["minute_of_hour", "updated_at"],
        )
        return config
