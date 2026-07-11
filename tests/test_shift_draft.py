from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import pandas as pd
import pytest

from tests.fakes import FakeWorksheet
from utils.shift_register_manager import (
    TEAM_SOURCE_UNSET_DRAFT_WARNING,
    DraftTeamProfileResolution,
    ShiftRegisterManager,
    TeamSourceStatus,
)
from utils.shift_register_structs import (
    DraftNotesTeamSource,
    DraftWorksheetContent,
    DraftWorksheetMetadata,
    EntryWorksheetContent,
    EntryWorksheetMetadata,
    FinalScheduleWorksheetMetadata,
    Shift,
    ShiftParser,
    ShiftRegisterGoogleSheetsMetadata,
)
from utils.shift_scheduler import (
    DraftSchedule,
    DraftTeamProfile,
    HourShiftAssignment,
    ShiftScheduler,
)
from utils.storage_errors import StorageError, StorageErrorKind

if TYPE_CHECKING:
    from collections.abc import Iterable


def make_feature_channel() -> SimpleNamespace:
    return SimpleNamespace(guild_id=1, channel_id=2, feature_name="shift_register")


class DraftBatchFakeWorksheet(FakeWorksheet):
    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self.typed_batches: list[list[dict[str, object]]] = []
        self.formula_ranges: list[set[str]] = []

    async def batch_update_typed_values(
        self,
        data: list[dict[str, object]],
        *,
        formula_ranges: set[str],
    ) -> None:
        self.typed_batches.append(data)
        self.formula_ranges.append(formula_ranges)


class EntryRangeFakeWorksheet(FakeWorksheet):
    def __init__(self, range_values: list[list[list[object]]]) -> None:
        super().__init__(title="Shift Entry")
        self.range_values = range_values
        self.batch_get_calls: list[list[str]] = []
        self.ignored_values = {
            "A1": "count formula row",
            "C3:E3": "Team display formulas",
            "AK3": "admin-owned value",
        }

    async def batch_get_values(
        self,
        ranges: list[str],
    ) -> list[list[list[object]]]:
        self.batch_get_calls.append(ranges)
        assert ranges == ["2:2", "A3:B", "F3:AJ"]
        return self.range_values

    async def to_frame(self) -> pd.DataFrame:
        msg = "draft generation must not use the legacy whole-frame read"
        raise AssertionError(msg)


def build_entry_frame(rows: list[tuple[str, str, set[int]]]) -> pd.DataFrame:
    records = []
    for username, display_name, slots in rows:
        record: dict[str, object] = {
            "username": username,
            "display_name": display_name,
            "original_message": "",
        }
        for index, label in enumerate(ShiftParser.HOUR_LABELS):
            record[label] = 1 if index in slots else 0
        records.append(record)
    return pd.DataFrame(records, columns=EntryWorksheetContent.COLUMNS)


def build_entry_ranges(
    rows: list[tuple[str, str, set[int]]],
) -> list[list[list[object]]]:
    identities = []
    availability = []
    for username, display_name, slots in rows:
        identities.append([username, display_name])
        availability.append(
            [
                *(1 if index in slots else 0 for index in range(30)),
                "",
            ]
        )
    return [[EntryWorksheetContent.COLUMNS], identities, availability]


def make_shift(username: str, slots: Iterable[int]) -> Shift:
    return Shift(
        username=username,
        display_name=username.capitalize(),
        original_message="",
        slots=set(slots),
    )


def test_to_shifts_reads_slots_from_worksheet() -> None:
    frame = build_entry_frame([("alice", "Alice", {4, 5, 6})])
    shift_df, plain_df = EntryWorksheetContent.standardize_dataframe(frame)
    content = EntryWorksheetContent(shift_df, plain_df)

    shifts = content.to_shifts()

    assert len(shifts) == 1
    shift = shifts[0]
    assert shift.username == "alice"
    assert shift.display_name == "Alice"
    assert 4 in shift
    assert 6 in shift
    assert 7 not in shift


def test_shifts_from_ranges_reads_current_entry_owned_columns() -> None:
    availability = [
        1 if index in {4, 6} else 0 for index in range(len(ShiftParser.HOUR_LABELS))
    ]

    shifts = EntryWorksheetContent.shifts_from_ranges(
        [EntryWorksheetContent.COLUMNS],
        [["alice", "Alice"]],
        [[*availability, "original"]],
    )

    assert shifts == [
        Shift(
            username="alice",
            display_name="Alice",
            original_message="original",
            slots={4, 6},
        )
    ]


def test_from_schedule_renders_lane_columns() -> None:
    shifts = [make_shift("a", {4, 5}), make_shift("b", {4, 5})]
    schedule = ShiftScheduler.assign(
        shifts,
        [4, 5],
        team_profiles={
            "a": DraftTeamProfile(
                main_isv=200,
                main_power=40,
                has_encore_role=True,
            )
        },
        encore_power_threshold=35,
        runner="Run",
    )

    frame = DraftWorksheetContent.from_schedule(schedule)

    assert list(frame.columns) == DraftWorksheetContent.COLUMNS
    assert list(frame["JST"]) == ["4-5", "5-6"]
    assert (frame["ランナー"] == "Run").all()
    first_row = frame.iloc[0]
    assert {first_row["アンコ"], first_row["本走①"]} == {"A", "B"}
    # Only two people, so the standby seat stays empty.
    assert first_row["待機"] == ""


def test_from_schedule_with_no_hours_is_header_only() -> None:
    schedule = ShiftScheduler.assign([], [], runner=None)

    frame = DraftWorksheetContent.from_schedule(schedule)

    assert list(frame.columns) == DraftWorksheetContent.COLUMNS
    assert frame.empty


def test_notes_formula_uses_exact_canonical_keys_and_dynamic_schedule() -> None:
    shifts = [
        Shift(
            username="alice_one",
            display_name="Ali*ce",
            original_message="4-8 no gaps",
            slots={4},
        ),
        Shift(
            username="alice_two",
            display_name="Ali*ce",
            original_message="6-8",
            slots={4},
        ),
    ]
    schedule = ShiftScheduler.assign(
        shifts,
        [4],
        team_profiles={},
        encore_power_threshold=35,
    )

    formula = DraftWorksheetContent.notes_formula(
        schedule,
        entry_worksheet_title="Shift Entry",
        recruitment_time_range="4-7・20-22",
        team_source=DraftNotesTeamSource(
            sheet_url="https://team.example",
            worksheet_title="Team Summary",
            import_last_column="G",
            username_header="username",
            main_isv_header="Main Team ISV",
            main_power_header="Main Team Power",
            encore_isv_header="Encore Team ISV",
            encore_power_header="Encore Team Power",
        ),
        team_source_warning=(
            "⚠️ Team Sourceが未設定のため、今回はISVを使用せず、"
            "アンコを空欄にしています。"
        ),
    )

    assert formula.startswith("=LET(")
    assert "C2:G2" in formula
    assert "C2:C2" in formula
    assert "⟨@[a-z0-9._]{2,32}⟩$" in formula
    assert "SUMPRODUCT(N(names = name)) > 1" in formula
    assert "SUMPRODUCT(N(shifts = person))" in formula
    assert "SUMPRODUCT(N(row = person))" in formula
    assert "SUMPRODUCT(N(encore = person))" in formula
    assert "XMATCH(person, keys, 0)" in formula
    assert "XLOOKUP(person, keys, usernames" in formula
    assert "COUNTIF(" not in formula
    assert "名前の表示ルール" in formula
    assert "シフトを調整するときは、名前全体をコピーしてください" in formula
    assert "シフト合計" in formula
    assert "最長連続" in formula
    assert "アンコ" in formula
    assert "アンコール" not in formula
    assert 'teamSourceUrl, "https://team.example"' in formula
    assert "IMPORTRANGE(teamSourceUrl, \"'Team Summary'!A:G\")" in formula
    assert 'mainIsvHeader, "Main Team ISV"' in formula
    assert 'mainPowerHeader, "Main Team Power"' in formula
    assert 'encoreIsvHeader, "Encore Team ISV"' in formula
    assert 'encorePowerHeader, "Encore Team Power"' in formula
    assert '"シフト合計（h）"' in formula  # noqa: RUF001
    assert '"最長連続（h）"' in formula  # noqa: RUF001
    assert '"アンコ（h）"' in formula  # noqa: RUF001
    assert '"内部編成"' in formula
    assert '"アンコ編成"' in formula
    assert '"編成状態"' in formula
    assert '"未登録"' in formula
    assert '"元メッセージ"' in formula
    assert "編成欄の表示順：実効値/総合力" in formula  # noqa: RUF001
    assert "⚠️ 参加者を特定できません" in formula
    assert "metaCandidates, VSTACK(" in formula
    assert 'metaLines, FILTER(metaCandidates, metaCandidates <> "")' in formula
    assert "4-8 no gaps" not in formula
    assert "募集時間【4-7・20-22】" in formula
    assert "VSTACK(meta, blankRow, headers, statRows, blankRow, legendRows)" in formula
    assert "stats, SORT(HSTACK(" in formula
    assert "2, FALSE, 3, FALSE, 4, FALSE, 1, TRUE" in formula
    assert "body, IFERROR(" not in formula
    assert formula.count("(") == formula.count(")")


def test_notes_formula_handles_empty_schedule() -> None:
    formula = DraftWorksheetContent.notes_formula(
        ShiftScheduler.assign([], []),
        entry_worksheet_title="Shift Entry",
        recruitment_time_range="4-28",
        team_source=None,
        team_source_warning=None,
    )

    assert "C2:G2" in formula
    assert 'VSTACK("メモ"' in formula
    assert "名前の表示ルール" in formula


def test_notes_formula_resets_consecutive_run_across_hour_gaps() -> None:
    schedule = ShiftScheduler.assign(
        [make_shift("alice", {4, 5, 20, 21})],
        [4, 5, 20, 21],
    )

    formula = DraftWorksheetContent.notes_formula(
        schedule,
        entry_worksheet_title="Shift Entry",
        recruitment_time_range="4-7・20-22",
        team_source=None,
        team_source_warning=None,
    )

    assert "hourSlots, {4;5;20;21}" in formula
    assert "INDEX(hourSlots, MAX(1, i - 1)) + 1" in formula


def test_notes_snapshot_matches_initial_schedule_and_complete_messages() -> None:
    shifts = [
        Shift(
            username="alice",
            display_name="Alice",
            original_message="4-7 ⏎  20-22／希望あり",  # noqa: RUF001
            slots={4, 5, 20},
        ),
        Shift(
            username="bob",
            display_name="Bob",
            original_message="4-6／補足",  # noqa: RUF001
            slots={4, 5, 20},
        ),
    ]
    schedule = DraftSchedule(
        runner=None,
        hours=[4, 5, 20],
        assignments=[
            HourShiftAssignment(
                4,
                {"encore": "alice", "honso_1": "bob"},
            ),
            HourShiftAssignment(
                5,
                {"encore": "alice", "standby": "bob"},
            ),
            HourShiftAssignment(20, {"honso_1": "bob"}),
        ],
        display_names={"alice": "Alice", "bob": "Bob"},
    )

    snapshot = DraftWorksheetContent.notes_snapshot(
        schedule,
        shifts=shifts,
        recruitment_time_range="4-7・20-22",
        team_profiles={
            "alice": DraftTeamProfile(
                main_isv=200,
                main_power=40,
                encore_isv=250,
                encore_power=50,
            )
        },
        team_source_warning=None,
    )

    assert snapshot == (
        "メモ\n募集時間【4-7・20-22】\n\n"
        "Bob：シフト合計 3h｜最長連続 2h｜アンコ 0h｜内部編成 未登録｜"  # noqa: RUF001
        "元メッセージ：4-6／補足\n"  # noqa: RUF001
        "Alice：シフト合計 2h｜最長連続 2h｜アンコ 2h｜"  # noqa: RUF001
        "内部編成 200/40｜アンコ編成 250/50｜"  # noqa: RUF001
        "元メッセージ：4-7 ⏎  20-22／希望あり\n\n"  # noqa: RUF001
        f"{DraftWorksheetContent.CANONICAL_NAME_LEGEND}\n"
        f"{DraftWorksheetContent.TEAM_VALUE_LEGEND}"
    )

    warning_snapshot = DraftWorksheetContent.notes_snapshot(
        schedule,
        shifts=shifts,
        recruitment_time_range="4-7・20-22",
        team_profiles=None,
        team_source_warning=TEAM_SOURCE_UNSET_DRAFT_WARNING,
    )
    assert warning_snapshot.splitlines()[2] == TEAM_SOURCE_UNSET_DRAFT_WARNING
    assert warning_snapshot.splitlines()[3] == ""
    assert warning_snapshot.splitlines()[4].startswith("Bob：")  # noqa: RUF001


@pytest.mark.asyncio
async def test_generate_draft_writes_draft_worksheet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = ShiftRegisterManager(make_feature_channel(), "service.json")
    manager._sheet_config = SimpleNamespace(  # noqa: SLF001
        recruitment_time_ranges=[
            {"start": 4, "end": 7},
            {"start": 20, "end": 22},
        ]
    )
    entry_ranges = build_entry_ranges(
        [
            ("alice", "Alice", {4, 5, 6, 10, 20, 21}),
            ("bob", "Bob", {4, 5}),
            ("carol", "Carol", {6}),
        ]
    )
    entry_ranges[2][0][-1] = "4-7 ⏎  20-22／希望あり"  # noqa: RUF001
    entry_worksheet = EntryRangeFakeWorksheet(entry_ranges)
    draft_worksheet = DraftBatchFakeWorksheet(title="Shift Draft")
    metadata = ShiftRegisterGoogleSheetsMetadata(
        "https://sheet.example",
        [
            EntryWorksheetMetadata(1, "Shift Entry", entry_worksheet),
            DraftWorksheetMetadata(2, "Shift Draft", draft_worksheet),
            FinalScheduleWorksheetMetadata(3, "Shift Final Schedule", None),
        ],
    )

    async def resolve_profiles() -> DraftTeamProfileResolution:
        return DraftTeamProfileResolution(
            TeamSourceStatus.AVAILABLE,
            {
                "alice": DraftTeamProfile(
                    main_isv=200,
                    main_power=40,
                    has_encore_role=True,
                ),
                "bob": DraftTeamProfile(main_isv=190, main_power=40),
                "carol": DraftTeamProfile(main_isv=None, main_power=40),
            },
            DraftNotesTeamSource(
                sheet_url="https://team.example",
                worksheet_title="Team Summary",
                import_last_column="G",
                username_header="username",
                main_isv_header="Main Team ISV",
                main_power_header="Main Team Power",
                encore_isv_header="Encore Team ISV",
                encore_power_header="Encore Team Power",
            ),
        )

    monkeypatch.setattr(manager, "resolve_draft_team_profiles", resolve_profiles)

    result = await manager.generate_draft(
        metadata,
        encore_power_threshold=35,
        runner="Run",
    )

    data = draft_worksheet.typed_batches[-1]
    assert data[0]["range"] == "A:G"
    assert data[0]["values"][0] == DraftWorksheetContent.COLUMNS
    assert data[0]["values"][1][0] == "4-5"
    assert data[0]["values"][1][1] == "Run"
    assert [row[0] for row in data[0]["values"][1:]] == [
        f"{hour}-{hour + 1}" for hour in range(4, 22)
    ]
    assert data[1] == {"range": "H21:H", "values": []}
    assert data[2]["range"] == "A21"
    assert draft_worksheet.formula_ranges[-1] == {"A21"}
    assert str(data[2]["values"][0][0]).startswith("=LET(")
    assert "募集時間【4-7・20-22】" in str(data[2]["values"][0][0])
    assert result.schedule.hours == list(range(4, 22))
    assert all(
        not assignment.supporter_usernames_by_slot
        for assignment in result.schedule.assignments
        if 7 <= assignment.hour < 20
    )
    assert result.team_source_status is TeamSourceStatus.AVAILABLE
    assert result.unregistered_usernames == ("carol",)
    assert result.recruitment_ranges.announcement_display() == "4-7・20-22"
    assert result.notes_snapshot.startswith(
        "メモ\n募集時間【4-7・20-22】\n\nAlice：シフト合計"  # noqa: RUF001
    )
    assert result.notes_snapshot.endswith(DraftWorksheetContent.TEAM_VALUE_LEGEND)
    assert "4-7 ⏎  20-22／希望あり" in result.notes_snapshot  # noqa: RUF001
    assert entry_worksheet.batch_get_calls == [["2:2", "A3:B", "F3:AJ"]]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [
        TeamSourceStatus.UNSET,
        TeamSourceStatus.MISSING,
        TeamSourceStatus.AMBIGUOUS,
        TeamSourceStatus.INVALID,
        TeamSourceStatus.UNRESOLVED,
    ],
)
async def test_generate_draft_falls_back_without_team_profiles(
    monkeypatch: pytest.MonkeyPatch,
    status: TeamSourceStatus,
) -> None:
    manager = ShiftRegisterManager(make_feature_channel(), "service.json")
    manager._sheet_config = SimpleNamespace(  # noqa: SLF001
        recruitment_time_ranges=[{"start": 4, "end": 5}]
    )
    entry_worksheet = EntryRangeFakeWorksheet(
        build_entry_ranges([("alice", "Alice", {4})])
    )
    draft_worksheet = DraftBatchFakeWorksheet(title="Shift Draft")
    metadata = ShiftRegisterGoogleSheetsMetadata(
        "https://sheet.example",
        [
            EntryWorksheetMetadata(1, "Shift Entry", entry_worksheet),
            DraftWorksheetMetadata(2, "Shift Draft", draft_worksheet),
            FinalScheduleWorksheetMetadata(3, "Shift Final Schedule", None),
        ],
    )

    async def resolve_profiles() -> DraftTeamProfileResolution:
        return DraftTeamProfileResolution(status, {})

    monkeypatch.setattr(manager, "resolve_draft_team_profiles", resolve_profiles)

    result = await manager.generate_draft(
        metadata,
        encore_power_threshold=35,
    )

    assignment = result.schedule.assignments[0]
    assert "encore" not in assignment.supporter_usernames_by_slot
    assert result.team_source_warning is not None
    assert result.unregistered_usernames == ()
    expected_marker = "⚠️ " if status is TeamSourceStatus.UNSET else "⚠️🛠️ "
    assert result.team_source_warning.startswith(expected_marker)
    formula = str(draft_worksheet.typed_batches[-1][2]["values"][0][0])
    assert result.team_source_warning in formula


@pytest.mark.asyncio
async def test_generate_draft_rejects_old_entry_header() -> None:
    manager = ShiftRegisterManager(make_feature_channel(), "service.json")
    manager._sheet_config = SimpleNamespace(  # noqa: SLF001
        recruitment_time_ranges=[{"start": 4, "end": 7}]
    )
    old_columns = [
        "username",
        "display_name",
        *[f"{hour}-{hour + 1}" for hour in range(4, 28)],
        "original_message",
    ]
    entry_worksheet = EntryRangeFakeWorksheet(
        [[old_columns], [], []],
    )
    draft_worksheet = FakeWorksheet(title="Shift Draft")
    metadata = ShiftRegisterGoogleSheetsMetadata(
        "https://sheet.example",
        [
            EntryWorksheetMetadata(1, "Shift Entry", entry_worksheet),
            DraftWorksheetMetadata(2, "Shift Draft", draft_worksheet),
            FinalScheduleWorksheetMetadata(3, "Shift Final Schedule", None),
        ],
    )

    with pytest.raises(StorageError) as exc_info:
        await manager.generate_draft(
            metadata,
            encore_power_threshold=35,
            runner="Run",
        )

    assert exc_info.value.kind is StorageErrorKind.MALFORMED_SHEET
    assert draft_worksheet.updated_frames == []


@pytest.mark.asyncio
async def test_generate_draft_raises_when_draft_worksheet_missing() -> None:
    manager = ShiftRegisterManager(make_feature_channel(), "service.json")
    manager._sheet_config = SimpleNamespace(  # noqa: SLF001
        recruitment_time_ranges=[{"start": 4, "end": 7}]
    )
    entry_worksheet = FakeWorksheet(
        title="Shift Entry",
        frame=build_entry_frame([("alice", "Alice", {4, 5})]),
    )
    metadata = ShiftRegisterGoogleSheetsMetadata(
        "https://sheet.example",
        [
            EntryWorksheetMetadata(1, "Shift Entry", entry_worksheet),
            DraftWorksheetMetadata(2, "Shift Draft", None),
            FinalScheduleWorksheetMetadata(3, "Shift Final Schedule", None),
        ],
    )

    with pytest.raises(StorageError) as exc_info:
        await manager.generate_draft(
            metadata,
            encore_power_threshold=35,
            runner="Run",
        )

    assert exc_info.value.kind is StorageErrorKind.GOOGLE_SHEETS_MISSING_WORKSHEET
