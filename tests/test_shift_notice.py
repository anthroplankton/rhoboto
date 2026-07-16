import itertools as it
from datetime import UTC, date, datetime, timedelta
from types import MappingProxyType

import pytest

from utils.shift_final import A1Cell
from utils.shift_notice import (
    JST,
    CanonicalPersonKey,
    ShiftNoticeCaseKind,
    ShiftNoticeCatalog,
    ShiftNoticeFrame,
    ShiftNoticeFrameState,
    ShiftNoticeOverlapLoss,
    ShiftNoticePerson,
    ShiftNoticeSourceRecord,
    align_next_honso,
    boundary_for_scheduled_tick,
    build_source_catalog,
    civil_start,
    latest_reached_boundary,
    parse_minute_of_hour,
    plan_snapshot,
    project_source_frames,
    scheduled_tick_for_boundary,
)
from utils.shift_register_structs import HourRange
from utils.shift_schedule_role import ScheduleRoleLabelMatch
from utils.structs_base import WorksheetContractError

EVENT_DATE = date(2026, 8, 1)
CREATED_AT = datetime(2026, 1, 1, tzinfo=UTC)
DEFAULT = object()


def at_event_hour(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 8, 1, tzinfo=JST) + timedelta(
        hours=hour,
        minutes=minute,
    )


def make_record(  # noqa: PLR0913
    source_id: int,
    *,
    created_at: datetime = CREATED_AT,
    is_enabled: bool = True,
    event_date: date | None = EVENT_DATE,
    ranges: object = DEFAULT,
    sheet_url: object = DEFAULT,
    worksheet_id: int | None = 1,
    anchor: str = "B2",
) -> ShiftNoticeSourceRecord:
    if ranges is DEFAULT:
        ranges = [{"start": 4, "end": 8}]
    if sheet_url is DEFAULT:
        sheet_url = f"https://docs.google.com/spreadsheets/d/sheet-{source_id}/edit"
    return ShiftNoticeSourceRecord(
        id=source_id,
        feature_channel_id=source_id + 100,
        channel_id=source_id + 200,
        is_enabled=is_enabled,
        created_at=created_at,
        sheet_url=sheet_url,
        final_schedule_worksheet_id=worksheet_id,
        final_schedule_anchor_cell=anchor,
        event_date=event_date,
        recruitment_time_ranges=ranges,
    )


def make_person(label: str, *member_ids: int) -> ShiftNoticePerson:
    key = ("member", member_ids[0]) if len(member_ids) == 1 else ("label", label)
    return ShiftNoticePerson(
        key=key,
        schedule_label=label,
        candidate_member_ids=member_ids,
    )


def make_frame(
    hour: int,
    *people: ShiftNoticePerson | None,
    source_id: int = 1,
    state: ShiftNoticeFrameState | None = None,
    event_hour: int | None = None,
) -> ShiftNoticeFrame:
    lanes = (*people, *(None for _ in range(5 - len(people))))
    if state is None:
        state = (
            ShiftNoticeFrameState.ACTIVE_STAFFED
            if any(lanes)
            else ShiftNoticeFrameState.ACTIVE_EMPTY
        )
    return ShiftNoticeFrame(
        civil_start=at_event_hour(hour),
        event_hour=hour if event_hour is None else event_hour,
        source_id=source_id,
        state=state,
        lanes=lanes,
    )


def make_catalog(*source_ranges: tuple[int, int]) -> ShiftNoticeCatalog:
    return build_source_catalog(
        [
            make_record(
                source_id,
                created_at=CREATED_AT + timedelta(minutes=source_id),
                ranges=[{"start": start, "end": end}],
            )
            for source_id, (start, end) in enumerate(source_ranges, start=1)
        ]
    )


def person_keys(
    people: tuple[ShiftNoticePerson, ...],
) -> tuple[CanonicalPersonKey, ...]:
    return tuple(person.key for person in people)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0", 0),
        ("59", 59),
        (" 45\t", 45),
        ("４５", 45),  # noqa: RUF001
    ],
)
def test_parse_minute_of_hour_accepts_canonical_and_full_width_digits(
    raw: str,
    expected: int,
) -> None:
    assert parse_minute_of_hour(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["", " \t", "+1", "-1", "1.0", "١٢", "60", "99"],
)
def test_parse_minute_of_hour_rejects_invalid_input(raw: str) -> None:
    with pytest.raises(ValueError, match="minute"):
        parse_minute_of_hour(raw)


def test_tick_and_boundary_conversion_uses_the_preceding_civil_hour() -> None:
    boundary = at_event_hour(14)

    assert scheduled_tick_for_boundary(boundary, 0) == boundary
    assert scheduled_tick_for_boundary(boundary, 1) == at_event_hour(13, 1)
    assert scheduled_tick_for_boundary(boundary, 45) == at_event_hour(13, 45)
    assert scheduled_tick_for_boundary(boundary, 59) == at_event_hour(13, 59)
    assert boundary_for_scheduled_tick(at_event_hour(13, 45), 45) == boundary
    assert boundary_for_scheduled_tick(at_event_hour(14), 0) == boundary


def test_latest_reached_boundary_is_bounded_by_the_envelope() -> None:
    envelope_start = at_event_hour(4)
    envelope_end = at_event_hour(8)

    assert (
        latest_reached_boundary(
            at_event_hour(3, 44),
            45,
            envelope_start,
            envelope_end,
        )
        is None
    )
    assert latest_reached_boundary(
        at_event_hour(5, 10),
        45,
        envelope_start,
        envelope_end,
    ) == at_event_hour(5)
    assert (
        latest_reached_boundary(
            at_event_hour(9),
            45,
            envelope_start,
            envelope_end,
        )
        == envelope_end
    )


def test_time_helpers_require_aware_jst_datetimes() -> None:
    naive = at_event_hour(14).replace(tzinfo=None)
    utc = datetime(2026, 8, 1, 14, tzinfo=UTC)
    jst = at_event_hour(14)

    with pytest.raises(ValueError, match="JST"):
        scheduled_tick_for_boundary(naive, 45)
    with pytest.raises(ValueError, match="JST"):
        scheduled_tick_for_boundary(utc, 45)
    with pytest.raises(ValueError, match="JST"):
        boundary_for_scheduled_tick(naive, 45)
    with pytest.raises(ValueError, match="JST"):
        boundary_for_scheduled_tick(utc, 45)
    with pytest.raises(ValueError, match="JST"):
        latest_reached_boundary(naive, 45, jst, jst)
    with pytest.raises(ValueError, match="JST"):
        latest_reached_boundary(jst, 45, naive, jst)


def test_case_kinds_have_stable_values() -> None:
    assert tuple(kind.value for kind in ShiftNoticeCaseKind) == (
        "start",
        "transition",
        "end",
        "cut",
    )


def test_source_converts_event_hours_24_through_30_without_modulo() -> None:
    catalog = build_source_catalog([make_record(1, ranges=[{"start": 24, "end": 30}])])

    source = catalog.complete_sources[0]
    assert source.spreadsheet_id == "sheet-1"
    assert source.final_schedule_anchor_cell == A1Cell(row=2, column=2, a1="B2")
    assert source.first_hour == 24
    assert source.end_hour == 30
    assert source.civil_start(24) == at_event_hour(24)
    assert source.civil_start(30) == at_event_hour(30)
    assert source.event_hour(at_event_hour(30)) == 30
    assert civil_start(EVENT_DATE, 30) == at_event_hour(30)


def test_catalog_normalizes_multiple_ranges_and_unions_the_outer_envelope() -> None:
    records = [
        make_record(
            1,
            ranges=[{"start": 8, "end": 10}, {"start": 4, "end": 6}],
        ),
        make_record(2, ranges=[{"start": 12, "end": 14}]),
    ]

    catalog = build_source_catalog(records)

    assert catalog.complete_sources[0].recruitment_time_ranges == (
        HourRange(4, 6),
        HourRange(8, 10),
    )
    assert catalog.envelope_start == at_event_hour(4)
    assert catalog.envelope_end == at_event_hour(14)


def test_catalog_orders_sources_by_creation_time_then_id() -> None:
    older = CREATED_AT - timedelta(days=1)
    records = [
        make_record(3),
        make_record(2, created_at=older),
        make_record(1),
    ]

    catalog = build_source_catalog(records)

    assert tuple(source.id for source in catalog.complete_sources) == (2, 1, 3)


@pytest.mark.parametrize(
    ("event_date", "ranges", "sheet_url", "worksheet_id", "anchor"),
    [
        (None, [{"start": 4, "end": 8}], DEFAULT, 1, "B2"),
        (EVENT_DATE, None, DEFAULT, 1, "B2"),
        (EVENT_DATE, [], DEFAULT, 1, "B2"),
        (EVENT_DATE, [{"start": 5, "end": 5}], DEFAULT, 1, "B2"),
        (EVENT_DATE, [{"start": 4, "end": 8}], "https://example.com", 1, "B2"),
        (EVENT_DATE, [{"start": 4, "end": 8}], DEFAULT, None, "B2"),
        (EVENT_DATE, [{"start": 4, "end": 8}], DEFAULT, 0, "B2"),
        (EVENT_DATE, [{"start": 4, "end": 8}], DEFAULT, -1, "B2"),
        (EVENT_DATE, [{"start": 4, "end": 8}], DEFAULT, 1, "not-a-cell"),
    ],
)
def test_incomplete_sources_are_retained_but_never_own_slots(
    event_date: date | None,
    ranges: object,
    sheet_url: object,
    worksheet_id: int | None,
    anchor: str,
) -> None:
    incomplete = make_record(
        1,
        created_at=CREATED_AT - timedelta(days=1),
        event_date=event_date,
        ranges=ranges,
        sheet_url=sheet_url,
        worksheet_id=worksheet_id,
        anchor=anchor,
    )
    complete = make_record(2)

    catalog = build_source_catalog([incomplete, complete])

    assert catalog.incomplete_sources == (incomplete,)
    assert set(catalog.slot_owners.values()) == {complete.id}


def test_tier_one_uses_the_oldest_winner_and_merges_each_losers_hours() -> None:
    records = [
        make_record(30, ranges=[{"start": 4, "end": 8}]),
        make_record(
            20,
            created_at=CREATED_AT + timedelta(minutes=1),
            ranges=[{"start": 5, "end": 8}],
        ),
        make_record(
            10,
            created_at=CREATED_AT + timedelta(minutes=2),
            ranges=[{"start": 6, "end": 8}],
        ),
    ]

    catalog = build_source_catalog(records)

    assert catalog.overlap_losses == (
        ShiftNoticeOverlapLoss(at_event_hour(5), at_event_hour(8), 20, 30),
        ShiftNoticeOverlapLoss(at_event_hour(6), at_event_hour(8), 10, 30),
    )


def test_overlap_losses_do_not_merge_across_civil_dates() -> None:
    records = [
        make_record(1, ranges=[{"start": 23, "end": 26}]),
        make_record(
            2,
            created_at=CREATED_AT + timedelta(minutes=1),
            ranges=[{"start": 23, "end": 26}],
        ),
    ]

    catalog = build_source_catalog(records)

    assert catalog.overlap_losses == (
        ShiftNoticeOverlapLoss(at_event_hour(23), at_event_hour(24), 2, 1),
        ShiftNoticeOverlapLoss(at_event_hour(24), at_event_hour(26), 2, 1),
    )


def test_tier_one_beats_an_older_tier_two_candidate() -> None:
    records = [
        make_record(
            1,
            ranges=[{"start": 4, "end": 5}, {"start": 6, "end": 8}],
        ),
        make_record(
            2,
            created_at=CREATED_AT + timedelta(minutes=1),
            ranges=[{"start": 5, "end": 6}],
        ),
    ]

    catalog = build_source_catalog(records)

    assert catalog.slot_owners[at_event_hour(5)] == 2
    assert catalog.slot_owners[at_event_hour(6)] == 1


def test_tier_two_uses_oldest_source_when_multiple_hulls_cover_a_gap() -> None:
    ranges = [{"start": 4, "end": 5}, {"start": 7, "end": 8}]
    older = make_record(20, ranges=ranges)
    newer = make_record(
        10,
        created_at=CREATED_AT + timedelta(minutes=1),
        ranges=ranges,
    )

    catalog = build_source_catalog([newer, older])

    assert catalog.slot_owners[at_event_hour(5)] == older.id


def test_tier_two_owns_gaps_inside_one_sources_physical_hull() -> None:
    catalog = build_source_catalog(
        [
            make_record(
                1,
                ranges=[{"start": 4, "end": 5}, {"start": 7, "end": 8}],
            )
        ]
    )

    assert catalog.slot_owners == {
        at_event_hour(4): 1,
        at_event_hour(5): 1,
        at_event_hour(6): 1,
        at_event_hour(7): 1,
    }


def test_internal_envelope_holes_without_a_source_are_cuts() -> None:
    catalog = build_source_catalog(
        [
            make_record(1, ranges=[{"start": 4, "end": 5}]),
            make_record(2, ranges=[{"start": 7, "end": 8}]),
        ]
    )

    assert catalog.slot_owners == {
        at_event_hour(4): 1,
        at_event_hour(5): None,
        at_event_hour(6): None,
        at_event_hour(7): 2,
    }


def test_no_complete_source_has_no_envelope() -> None:
    incomplete = make_record(1, event_date=None)

    catalog = build_source_catalog([incomplete])

    assert catalog.complete_sources == ()
    assert catalog.incomplete_sources == (incomplete,)
    assert catalog.slot_owners == {}
    assert catalog.envelope_start is None
    assert catalog.envelope_end is None
    assert catalog.overlap_losses == ()


def test_enabled_state_is_metadata_and_does_not_change_ownership() -> None:
    records = [
        make_record(1, is_enabled=False),
        make_record(
            2,
            created_at=CREATED_AT + timedelta(minutes=1),
            is_enabled=True,
        ),
    ]

    catalog = build_source_catalog(records)

    assert tuple(source.is_enabled for source in catalog.complete_sources) == (
        False,
        True,
    )
    assert set(catalog.slot_owners.values()) == {1}


def test_project_source_frames_uses_the_runner_rectangle_and_event_axis() -> None:
    catalog = build_source_catalog(
        [make_record(1, ranges=[{"start": 24, "end": 28}], anchor="C4")]
    )
    source = catalog.complete_sources[0]
    alice = ScheduleRoleLabelMatch("Alice", (10,))
    worksheet_values = [
        [123, "bogus visible time"],
        [],
        [],
        [999, "24:00", "Runner", "Alice", "", "", "", "", 456],
        [999, "25:00", "Runner", "", "", "", "", "", 456],
        [999, "26:00", "", "residual", "names", "are", "ignored", "", 456],
    ]

    frames = project_source_frames(
        source,
        worksheet_values,
        {alice.label: alice},
    )

    assert tuple(frames) == tuple(at_event_hour(hour) for hour in range(24, 28))
    assert frames[at_event_hour(24)].lanes[0] == make_person("Alice", 10)
    assert frames[at_event_hour(24)].state is ShiftNoticeFrameState.ACTIVE_STAFFED
    assert frames[at_event_hour(25)].state is ShiftNoticeFrameState.ACTIVE_EMPTY
    assert frames[at_event_hour(26)].state is ShiftNoticeFrameState.CUT
    assert frames[at_event_hour(26)].lanes == (None,) * 5
    assert frames[at_event_hour(27)].state is ShiftNoticeFrameState.CUT
    assert frames[at_event_hour(25)].event_hour == 25
    assert frames[at_event_hour(25)].civil_start == datetime(
        2026,
        8,
        2,
        1,
        tzinfo=JST,
    )


@pytest.mark.parametrize(
    "six_cells",
    [
        (7, "", "", "", "", ""),
        ("Runner", 7, "", "", "", ""),
    ],
)
def test_project_source_frames_rejects_non_text_without_source_fallback(
    six_cells: tuple[object, ...],
) -> None:
    catalog = build_source_catalog(
        [
            make_record(1, ranges=[{"start": 4, "end": 5}]),
            make_record(
                2,
                created_at=CREATED_AT + timedelta(minutes=1),
                ranges=[{"start": 4, "end": 5}],
            ),
        ]
    )
    assert catalog.slot_owners[at_event_hour(4)] == 1

    with pytest.raises(WorksheetContractError) as error:
        project_source_frames(
            catalog.complete_sources[0],
            [[], ["ignored", *six_cells]],
            {},
        )

    assert error.value.log_hint == "final_schedule_role_value_not_text"
    assert catalog.slot_owners[at_event_hour(4)] == 1


def test_project_source_frames_canonicalizes_people_without_discord_objects() -> None:
    source = build_source_catalog(
        [make_record(1, ranges=[{"start": 4, "end": 5}], anchor="A1")]
    ).complete_sources[0]
    matches = {
        match.label: match
        for match in (
            ScheduleRoleLabelMatch("Alias A", (10,)),
            ScheduleRoleLabelMatch("Alias B", (10,)),
            ScheduleRoleLabelMatch("Duplicate", (20, 21)),
            ScheduleRoleLabelMatch("Ghost ⟨@missing⟩", ()),
        )
    }

    frame = project_source_frames(
        source,
        [
            [
                "Runner",
                "Alias A",
                "Alias B",
                "Duplicate",
                "Unresolved",
                "Ghost ⟨@missing⟩",
            ]
        ],
        matches,
    )[at_event_hour(4)]

    assert frame.lanes == (
        make_person("Alias A", 10),
        make_person("Alias B", 10),
        make_person("Duplicate", 20, 21),
        make_person("Unresolved"),
        make_person("Ghost ⟨@missing⟩"),
    )
    assert frame.lanes[0].key == frame.lanes[1].key
    assert frame.lanes[2].key == ("label", "Duplicate")
    assert frame.lanes[2].candidate_member_ids == (20, 21)
    assert frame.lanes[3].key == ("label", "Unresolved")
    assert frame.lanes[4].key == ("label", "Ghost ⟨@missing⟩")


@pytest.mark.parametrize(
    ("matched_label", "near_collision"),
    [
        ("Alice", "alice"),
        ("Café", "Cafe\u0301"),
        ("Robert", "Robrt"),
    ],
)
def test_project_source_frames_keeps_near_identity_collisions_as_raw_labels(
    matched_label: str,
    near_collision: str,
) -> None:
    source = build_source_catalog(
        [make_record(1, ranges=[{"start": 4, "end": 5}], anchor="A1")]
    ).complete_sources[0]

    frame = project_source_frames(
        source,
        [["Runner", near_collision]],
        {matched_label: ScheduleRoleLabelMatch(matched_label, (10,))},
    )[at_event_hour(4)]

    assert frame.lanes[0] == make_person(near_collision)


def test_shift_notice_frame_requires_exactly_five_lanes() -> None:
    with pytest.raises(ValueError, match="five"):
        ShiftNoticeFrame(
            civil_start=at_event_hour(4),
            event_hour=4,
            source_id=1,
            state=ShiftNoticeFrameState.ACTIVE_EMPTY,
            lanes=(),
        )


@pytest.mark.parametrize(
    "next_order",
    tuple(it.permutations(("A", "B", "C"))),
)
def test_align_next_honso_recovers_all_six_previous_orders(
    next_order: tuple[str, str, str],
) -> None:
    people = {label: make_person(label) for label in ("A", "B", "C")}
    previous = make_frame(
        4,
        None,
        people["A"],
        people["B"],
        people["C"],
        None,
    )
    next_frame = make_frame(
        5,
        None,
        *(people[label] for label in next_order),
        None,
    )

    aligned = align_next_honso(previous, next_frame)

    assert tuple(person.schedule_label for person in aligned.lanes[1:4]) == (
        "A",
        "B",
        "C",
    )
    assert previous.lanes[1:4] == (people["A"], people["B"], people["C"])


@pytest.mark.parametrize(
    ("previous_labels", "next_labels", "expected_labels"),
    [
        (("A", "A", "B"), ("B", "B", "C"), ("C", "B", "B")),
        (("A", "A", "A"), ("B", "A", "C"), ("A", "B", "C")),
        (("A", "A", "A"), ("B", "A", "A"), ("A", "B", "A")),
    ],
)
def test_align_next_honso_uses_distance_original_order_and_stable_ties(
    previous_labels: tuple[str, str, str],
    next_labels: tuple[str, str, str],
    expected_labels: tuple[str, str, str],
) -> None:
    people = {label: make_person(label) for label in ("A", "B", "C")}
    previous = make_frame(
        4,
        None,
        *(people[label] for label in previous_labels),
        None,
    )
    next_frame = make_frame(
        5,
        None,
        *(people[label] for label in next_labels),
        None,
    )

    aligned = align_next_honso(previous, next_frame)

    assert tuple(person.schedule_label for person in aligned.lanes[1:4]) == (
        expected_labels
    )


def test_align_next_honso_does_not_hide_cross_role_movement() -> None:
    people = {label: make_person(label) for label in ("A", "B", "C", "D", "E")}
    previous = make_frame(
        4,
        people["A"],
        people["B"],
        people["C"],
        people["D"],
        people["E"],
    )
    next_frame = make_frame(
        5,
        people["B"],
        people["A"],
        people["C"],
        people["E"],
        people["D"],
    )

    assert align_next_honso(previous, next_frame) == next_frame


def test_snapshot_people_are_pure_previous_and_next_key_sets() -> None:
    catalog = make_catalog((4, 6))
    alice = make_person("Alice", 10)
    alice_alias = make_person("Alice Alias", 10)
    bob = make_person("Bob", 20)
    carol = make_person("Carol", 30)
    loaded = {
        at_event_hour(4): make_frame(4, alice, bob, bob),
        at_event_hour(5): make_frame(5, carol, None, None, None, alice_alias),
    }

    snapshot = plan_snapshot(catalog, loaded, at_event_hour(5)).snapshot

    assert snapshot is not None
    assert person_keys(snapshot.ending) == (("member", 20),)
    assert person_keys(snapshot.continuing) == (("member", 10),)
    assert person_keys(snapshot.starting) == (("member", 30),)
    assert snapshot.next.lanes[4] == alice_alias


def test_plan_snapshot_requires_both_boundary_sources_before_planning() -> None:
    catalog = make_catalog((4, 5), (5, 6))

    plan = plan_snapshot(catalog, {}, at_event_hour(5))

    assert plan.missing_source_ids == frozenset({1, 2})
    assert plan.snapshot is None


def test_duration_frontier_is_complete_before_returning_immutable_hours() -> None:
    catalog = make_catalog((3, 4), (4, 5), (5, 6), (6, 7))
    previous_label = make_person("Previous label", 10)
    next_label = make_person("Next label", 10)
    loaded = {
        at_event_hour(4): make_frame(
            4,
            previous_label,
            source_id=2,
        ),
        at_event_hour(5): make_frame(
            5,
            None,
            next_label,
            next_label,
            source_id=3,
        ),
    }

    incomplete = plan_snapshot(catalog, loaded, at_event_hour(5))

    assert incomplete.missing_source_ids == frozenset({1, 4})
    assert incomplete.snapshot is None

    loaded.update(
        {
            at_event_hour(3): make_frame(
                3,
                None,
                None,
                previous_label,
                source_id=1,
            ),
            at_event_hour(6): make_frame(
                6,
                None,
                None,
                None,
                next_label,
                source_id=4,
            ),
        }
    )
    complete = plan_snapshot(catalog, loaded, at_event_hour(5))

    assert complete.missing_source_ids == frozenset()
    assert complete.snapshot is not None
    assert complete.snapshot.cumulative_hours == {("member", 10): 2}
    assert complete.snapshot.remaining_hours == {("member", 10): 2}
    assert isinstance(complete.snapshot.cumulative_hours, MappingProxyType)
    assert isinstance(complete.snapshot.remaining_hours, MappingProxyType)
    assert complete.snapshot.next.lanes[1:3] == (next_label, next_label)


@pytest.mark.parametrize("stop_kind", ["absence", "cut", "source_less", "outside"])
def test_cumulative_duration_stops_at_each_domain_boundary(stop_kind: str) -> None:
    person = make_person("Person", 10)
    other = make_person("Other", 20)
    if stop_kind == "source_less":
        catalog = make_catalog((2, 3), (4, 6))
        source_id = 2
    elif stop_kind == "outside":
        catalog = make_catalog((4, 6))
        source_id = 1
    else:
        catalog = make_catalog((3, 6))
        source_id = 1

    loaded = {
        at_event_hour(4): make_frame(4, person, source_id=source_id),
        at_event_hour(5): make_frame(5, source_id=source_id),
    }
    if stop_kind == "absence":
        loaded[at_event_hour(3)] = make_frame(3, other, source_id=source_id)
    elif stop_kind == "cut":
        loaded[at_event_hour(3)] = make_frame(
            3,
            source_id=source_id,
            state=ShiftNoticeFrameState.CUT,
        )

    plan = plan_snapshot(catalog, loaded, at_event_hour(5))

    assert plan.missing_source_ids == frozenset()
    assert plan.snapshot is not None
    assert plan.snapshot.cumulative_hours == {person.key: 1}


@pytest.mark.parametrize(
    ("boundary", "state_at_four", "state_at_five", "expected_case"),
    [
        (4, ShiftNoticeFrameState.ACTIVE_EMPTY, ShiftNoticeFrameState.CUT, "start"),
        (
            5,
            ShiftNoticeFrameState.ACTIVE_EMPTY,
            ShiftNoticeFrameState.ACTIVE_STAFFED,
            "transition",
        ),
        (5, ShiftNoticeFrameState.ACTIVE_EMPTY, ShiftNoticeFrameState.CUT, "end"),
        (5, ShiftNoticeFrameState.CUT, ShiftNoticeFrameState.ACTIVE_EMPTY, "start"),
        (5, ShiftNoticeFrameState.CUT, ShiftNoticeFrameState.CUT, "cut"),
        (6, ShiftNoticeFrameState.CUT, ShiftNoticeFrameState.ACTIVE_EMPTY, "end"),
    ],
)
def test_plan_snapshot_classifies_every_boundary_shape(
    boundary: int,
    state_at_four: ShiftNoticeFrameState,
    state_at_five: ShiftNoticeFrameState,
    expected_case: str,
) -> None:
    catalog = make_catalog((4, 6))
    loaded = {
        at_event_hour(4): make_frame(4, state=state_at_four),
        at_event_hour(5): make_frame(5, state=state_at_five),
    }

    snapshot = plan_snapshot(catalog, loaded, at_event_hour(boundary)).snapshot

    assert snapshot is not None
    assert snapshot.case.value == expected_case
    if boundary == 4:
        assert snapshot.previous.state is ShiftNoticeFrameState.OUTSIDE
        assert snapshot.previous.state is not ShiftNoticeFrameState.CUT
    if boundary == 6:
        assert snapshot.next.state is ShiftNoticeFrameState.OUTSIDE
        assert snapshot.case is ShiftNoticeCaseKind.END


def test_cut_window_selects_seven_rows_and_probes_both_ellipses() -> None:
    catalog = make_catalog((2, 12))
    loaded = {
        at_event_hour(hour): make_frame(
            hour,
            state=ShiftNoticeFrameState.CUT,
        )
        for hour in range(2, 12)
    }

    snapshot = plan_snapshot(catalog, loaded, at_event_hour(6)).snapshot

    assert snapshot is not None
    assert snapshot.case is ShiftNoticeCaseKind.CUT
    assert snapshot.cut_window is not None
    assert tuple(row.civil_start for row in snapshot.cut_window.rows) == tuple(
        at_event_hour(hour) for hour in range(3, 10)
    )
    assert snapshot.cut_window.truncated_before is True
    assert snapshot.cut_window.truncated_after is True


def test_cut_window_backfills_unused_capacity_from_the_open_side() -> None:
    catalog = make_catalog((3, 12))
    loaded = {
        at_event_hour(3): make_frame(3),
        **{
            at_event_hour(hour): make_frame(
                hour,
                state=ShiftNoticeFrameState.CUT,
            )
            for hour in range(4, 12)
        },
    }

    snapshot = plan_snapshot(catalog, loaded, at_event_hour(5)).snapshot

    assert snapshot is not None
    assert snapshot.cut_window is not None
    assert tuple(row.civil_start for row in snapshot.cut_window.rows) == tuple(
        at_event_hour(hour) for hour in range(4, 11)
    )
    assert snapshot.cut_window.truncated_before is False
    assert snapshot.cut_window.truncated_after is True


def test_cut_window_backfills_before_when_the_after_side_closes() -> None:
    catalog = make_catalog((2, 12))
    loaded = {
        **{
            at_event_hour(hour): make_frame(
                hour,
                state=ShiftNoticeFrameState.CUT,
            )
            for hour in range(2, 11)
        },
        at_event_hour(11): make_frame(11),
    }

    snapshot = plan_snapshot(catalog, loaded, at_event_hour(9)).snapshot

    assert snapshot is not None
    assert snapshot.cut_window is not None
    assert tuple(row.civil_start for row in snapshot.cut_window.rows) == tuple(
        at_event_hour(hour) for hour in range(4, 11)
    )
    assert snapshot.cut_window.truncated_before is True
    assert snapshot.cut_window.truncated_after is False


def test_source_less_overnight_cut_rows_keep_the_catalog_event_axis() -> None:
    catalog = make_catalog((23, 24), (27, 28))
    loaded = {
        at_event_hour(23): make_frame(23, source_id=1),
        at_event_hour(27): make_frame(27, source_id=2),
    }

    snapshot = plan_snapshot(catalog, loaded, at_event_hour(25)).snapshot

    assert snapshot is not None
    assert snapshot.cut_window is not None
    assert tuple(row.civil_start.hour for row in snapshot.cut_window.rows) == (
        0,
        1,
        2,
    )
    assert tuple(row.event_hour for row in snapshot.cut_window.rows) == (24, 25, 26)


def test_source_less_event_hour_uses_one_axis_at_cut_and_owned_boundaries() -> None:
    catalog = build_source_catalog(
        [
            make_record(1, ranges=[{"start": 23, "end": 24}]),
            make_record(
                2,
                created_at=CREATED_AT + timedelta(minutes=1),
                event_date=EVENT_DATE + timedelta(days=1),
                ranges=[{"start": 2, "end": 3}],
            ),
        ]
    )
    loaded = {
        at_event_hour(23): make_frame(23, source_id=1),
        at_event_hour(26): make_frame(26, source_id=2, event_hour=2),
    }

    cut_snapshot = plan_snapshot(catalog, loaded, at_event_hour(25)).snapshot
    boundary_snapshot = plan_snapshot(catalog, loaded, at_event_hour(26)).snapshot

    assert cut_snapshot is not None
    assert cut_snapshot.cut_window is not None
    cut_event_hour = next(
        row.event_hour
        for row in cut_snapshot.cut_window.rows
        if row.civil_start == at_event_hour(25)
    )
    assert boundary_snapshot is not None
    assert boundary_snapshot.previous.civil_start == at_event_hour(25)
    assert (cut_event_hour, boundary_snapshot.previous.event_hour) == (25, 25)


def test_cut_window_never_crosses_an_active_or_outside_frame() -> None:
    catalog = make_catalog((3, 7))
    loaded = {
        at_event_hour(3): make_frame(3),
        **{
            at_event_hour(hour): make_frame(
                hour,
                state=ShiftNoticeFrameState.CUT,
            )
            for hour in range(4, 7)
        },
    }

    snapshot = plan_snapshot(catalog, loaded, at_event_hour(5)).snapshot

    assert snapshot is not None
    assert snapshot.cut_window is not None
    assert tuple(row.civil_start for row in snapshot.cut_window.rows) == tuple(
        at_event_hour(hour) for hour in range(4, 7)
    )
    assert snapshot.cut_window.truncated_before is False
    assert snapshot.cut_window.truncated_after is False
    assert all(
        row.state is ShiftNoticeFrameState.CUT for row in snapshot.cut_window.rows
    )


def test_cut_ellipsis_frontier_returns_every_probe_source_together() -> None:
    catalog = make_catalog((1, 2), (2, 9), (9, 10))
    loaded = {
        at_event_hour(hour): make_frame(
            hour,
            source_id=2,
            state=ShiftNoticeFrameState.CUT,
        )
        for hour in range(2, 9)
    }

    incomplete = plan_snapshot(catalog, loaded, at_event_hour(5))

    assert incomplete.missing_source_ids == frozenset({1, 3})
    assert incomplete.snapshot is None

    loaded[at_event_hour(1)] = make_frame(
        1,
        source_id=1,
        state=ShiftNoticeFrameState.CUT,
    )
    loaded[at_event_hour(9)] = make_frame(
        9,
        source_id=3,
        state=ShiftNoticeFrameState.CUT,
    )
    complete = plan_snapshot(catalog, loaded, at_event_hour(5))

    assert complete.missing_source_ids == frozenset()
    assert complete.snapshot is not None
    assert complete.snapshot.cut_window is not None
    assert tuple(row.civil_start for row in complete.snapshot.cut_window.rows) == tuple(
        at_event_hour(hour) for hour in range(2, 9)
    )
    assert complete.snapshot.cut_window.truncated_before is True
    assert complete.snapshot.cut_window.truncated_after is True
