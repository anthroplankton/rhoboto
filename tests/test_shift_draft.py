from __future__ import annotations

import copy
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

import pytest

from tests.fakes import FakeWorksheet
from utils import shift_register_manager
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
from utils.structs_base import UserInfo, WorksheetContractError
from utils.team_register_structs import (
    SummaryWorksheetMetadata,
    TeamRegisterGoogleSheetsMetadata,
    TeamWorksheetContent,
    TeamWorksheetMetadata,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence


def make_feature_channel() -> SimpleNamespace:
    return SimpleNamespace(guild_id=1, channel_id=2, feature_name="shift_register")


class DraftBatchFakeWorksheet(FakeWorksheet):
    def __init__(
        self,
        *,
        old_axis_rows: list[list[object]] | None = None,
        old_threshold_labels: list[list[object]] | None = None,
        old_lookup_labels: list[list[object]] | None = None,
        row_count: int = 100,
        col_count: int = 20,
        **kwargs: object,
    ) -> None:
        kwargs.setdefault("worksheet_id", 2)
        super().__init__(**kwargs)
        self.row_count = row_count
        self.col_count = col_count
        self.old_axis_rows = old_axis_rows or []
        self.old_threshold_labels = old_threshold_labels or []
        self.old_lookup_labels = old_lookup_labels or []
        height = max(
            len(self.old_axis_rows),
            len(self.old_threshold_labels),
            len(self.old_lookup_labels),
        )
        self.values = [[""] * 10 for _ in range(height)]
        for row, value in enumerate(self.old_axis_rows):
            self.values[row][0:1] = value[:1]
        for row, value in enumerate(self.old_threshold_labels):
            self.values[row][8:9] = value[:1]
        for row, value in enumerate(self.old_lookup_labels):
            self.values[row][9:10] = value[:1]
        self.typed_batches: list[list[dict[str, object]]] = []
        self.formula_ranges: list[set[str]] = []
        self.background_updates: list[list[tuple[str, str]]] = []
        self.border_updates: list[list[tuple[str, str | None, str, Sequence[str]]]] = []
        self.format_updates: list[list[tuple[str, dict[str, object], str]]] = []
        self.frozen_column_counts: list[int | None] = []
        self.conditional_format_rules: list[dict[str, object]] = []
        self.conditional_format_rule_deletes: list[list[int]] = []
        self.conditional_format_rule_adds: list[list[dict[str, object]]] = []
        self.typed_minimums: list[tuple[int | None, int | None]] = []

    def typed_update_requests(  # noqa: PLR0913
        self,
        data: list[dict[str, object]],
        *,
        formula_ranges: set[str],
        background_updates: Sequence[tuple[str, str]] = (),
        border_updates: Sequence[tuple[str, str | None, str, Sequence[str]]] = (),
        format_updates: Sequence[tuple[str, dict[str, object], str]] = (),
        conditional_format_rule_deletes: Sequence[int] = (),
        conditional_format_rule_adds: Sequence[dict[str, object]] = (),
        frozen_column_count: int | None = None,
        min_rows: int | None = None,
        min_cols: int | None = None,
    ) -> list[dict[str, object]]:
        self.typed_batches.append(data)
        self.formula_ranges.append(formula_ranges)
        self.background_updates.append(list(background_updates))
        self.border_updates.append(list(border_updates))
        self.format_updates.append(list(format_updates))
        self.conditional_format_rule_deletes.append(
            list(conditional_format_rule_deletes)
        )
        self.conditional_format_rule_adds.append(list(conditional_format_rule_adds))
        self.frozen_column_counts.append(frozen_column_count)
        self.typed_minimums.append((min_rows, min_cols))
        return data

    async def batch_update_typed_values(
        self,
        data: list[dict[str, object]],
        **kwargs: object,
    ) -> None:
        self.typed_update_requests(data, **kwargs)  # type: ignore[arg-type]

    async def get_conditional_format_rules(self) -> list[dict[str, object]]:
        return self.conditional_format_rules


class EntryRangeFakeWorksheet(FakeWorksheet):
    def __init__(
        self,
        range_values: list[list[list[object]]],
        *,
        row_count: int = 100,
        col_count: int = 40,
    ) -> None:
        super().__init__(title="Shift Entry")
        self.row_count = row_count
        self.col_count = col_count
        self.range_values = range_values
        header_rows, identity_rows, availability_rows = range_values
        self.values = [list(row) for row in header_rows]
        participant_count = max(len(identity_rows), len(availability_rows))
        for index in range(participant_count):
            identity = identity_rows[index] if index < len(identity_rows) else []
            availability = (
                availability_rows[index] if index < len(availability_rows) else []
            )
            self.values.append(
                [
                    *identity,
                    *("" for _ in range(max(0, 5 - len(identity)))),
                    *availability,
                ]
            )


class DraftValueGoogleSheet:
    sheet_url = "https://docs.google.com/spreadsheets/d/shift-draft/edit"

    def __init__(self) -> None:
        self.batch_reads: list[list[int]] = []
        self.batch_updates: list[tuple[list[object], list[dict[str, object]]]] = []
        self.batch_update_error: Exception | None = None

    async def batch_get_worksheet_values(
        self,
        worksheets: list[FakeWorksheet],
    ) -> dict[int, list[list[object]]]:
        self.batch_reads.append([worksheet.id for worksheet in worksheets])
        return {
            worksheet.id: copy.deepcopy(worksheet.values) for worksheet in worksheets
        }

    async def batch_update_grid(
        self,
        mutations: list[object],
        *,
        worksheet_requests: list[dict[str, object]] = (),
    ) -> None:
        self.batch_updates.append((list(mutations), list(worksheet_requests)))
        if self.batch_update_error is not None:
            raise self.batch_update_error


def configure_draft_value_sheet(
    manager: ShiftRegisterManager,
) -> DraftValueGoogleSheet:
    sheet = DraftValueGoogleSheet()
    manager._google_sheet = sheet  # type: ignore[assignment]  # noqa: SLF001
    return sheet


def build_entry_ranges(
    rows: list[tuple[str, str, set[int]]],
) -> list[list[list[object]]]:
    identities = []
    availability = []
    for username, display_name, slots in rows:
        identities.append([username, display_name, ""])
        availability.append(
            [
                *(1 if index in slots else 0 for index in range(30)),
                "",
            ]
        )
    return [
        [EntryWorksheetContent.count_row(), EntryWorksheetContent.COLUMNS],
        identities,
        availability,
    ]


def build_entry_grid(
    rows: list[tuple[str, str, set[int]]],
) -> list[list[object]]:
    grid = [EntryWorksheetContent.count_row(), EntryWorksheetContent.COLUMNS]
    for username, display_name, slots in rows:
        grid.append(
            [
                username,
                display_name,
                "",
                "ignored spill",
                "ignored spill",
                *(1 if index in slots else 0 for index in range(30)),
                "message",
                "ignored admin",
            ]
        )
    return grid


def test_entry_state_projects_owned_columns_from_complete_grid() -> None:
    layout, identities, participants = shift_register_manager._entry_state_from_grid(  # noqa: SLF001
        build_entry_grid([("alice", "Alice", {4, 6})])
    )

    assert layout == []
    assert identities == [["alice", "Alice"]]
    assert participants == [(3, "alice", "", False)]


def test_draft_control_state_projects_only_signed_control_columns() -> None:
    grid = [[""] * 10 for _ in range(37)]
    grid[0][0] = "JST"
    grid[1][0] = "4-5"
    grid[31][0] = '=LET(owner, "rhoboto-shift-draft-notes", owner)'
    grid[3][8] = DraftWorksheetContent.CANDIDATE_THRESHOLD_LABEL
    grid[5][9] = "名前を貼り付け"

    project_controls = shift_register_manager._draft_control_state_from_grid  # noqa: SLF001
    axis, threshold, lookup = project_controls(grid)

    assert axis[-1] == ['=LET(owner, "rhoboto-shift-draft-notes", owner)']
    assert threshold == [[], [], [], [DraftWorksheetContent.CANDIDATE_THRESHOLD_LABEL]]
    assert lookup[-1] == ["名前を貼り付け"]


def make_shift(username: str, slots: Iterable[int]) -> Shift:
    return Shift(
        username=username,
        display_name=username.capitalize(),
        original_message="",
        slots=set(slots),
    )


def make_runner() -> UserInfo:
    return UserInfo(username="runner", display_name="Run")


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


def test_shifts_from_ranges_rejects_nonbinary_availability() -> None:
    availability = [0] * len(ShiftParser.HOUR_LABELS)
    availability[4] = 2

    with pytest.raises(WorksheetContractError):
        EntryWorksheetContent.shifts_from_ranges(
            [EntryWorksheetContent.COLUMNS],
            [["alice", "Alice"]],
            [[*availability, "original"]],
        )


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
        runner=make_runner(),
    )

    frame = DraftWorksheetContent.from_schedule(schedule)

    assert list(frame.columns) == DraftWorksheetContent.COLUMNS
    assert list(frame["JST"]) == ["4-5", "5-6"]
    assert (frame["ランナー"] == "Run").all()
    first_row = frame.iloc[0]
    assert {first_row["アンコ"], first_row["本走①"]} == {"A", "B"}
    # Only two people, so the standby seat stays empty.
    assert first_row["待機"] == ""


def test_from_schedule_omits_runner_outside_recruitment_slots() -> None:
    schedule = ShiftScheduler.assign(
        [],
        [4, 5, 6],
        runner=UserInfo(username="runner", display_name="Run"),
    )

    frame = DraftWorksheetContent.from_schedule(
        schedule,
        recruitment_slots={4, 6},
    )

    assert list(frame["ランナー"]) == ["Run", "", "Run"]


def test_candidate_formula_masks_each_row_by_canonical_runner_cell() -> None:
    runner = UserInfo(username="runner_user", display_name="Alice")
    schedule = ShiftScheduler.assign(
        [
            Shift(
                username="runner_user",
                display_name="Alice",
                original_message="",
                slots={4},
            )
        ],
        [4],
        runner=runner,
    )

    formula = DraftWorksheetContent.candidate_formula(
        schedule,
        entry_worksheet_title="Shift Entry",
        recruitment_slots={4},
        encore_power_threshold_cell="L2",
        team_source=None,
    )

    assert "runnerUsername" not in formula
    assert "runnerBaseNames" in formula
    assert "N(runnerBaseNames = name) * N(runnerNames <> runnerBaseNames)" in formula
    assert "runnerNames, B2:B2" in formula
    assert "runnerName, INDEX(runnerNames, XMATCH(hour, hourSlots, 0))" in formula
    assert "N(keys <> runnerName) * N(names <> runnerName)" in formula
    assert "runnerEligible(hour)" in formula
    assert "FILTER(HSTACK(keys, scores, entryOrder), mask)" in formula


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
            roles_header="encore_roles",
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
    assert "runnerNames, B2:B2" in formula
    assert "N(runnerBaseNames = name) * N(runnerNames <> runnerBaseNames)" in formula
    assert "runnerUsername" not in formula
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


def test_candidate_formula_uses_hourly_availability_and_team_rules() -> None:
    schedule = DraftSchedule(
        runner="Run",
        hours=[4, 5, 6],
        assignments=[HourShiftAssignment(hour) for hour in (4, 5, 6)],
        display_names={},
    )

    formula = DraftWorksheetContent.candidate_formula(
        schedule,
        entry_worksheet_title="Shift Entry",
        recruitment_slots={4, 6},
        encore_power_threshold_cell="J5",
        team_source=DraftNotesTeamSource(
            sheet_url="https://team.example",
            worksheet_title="Team Summary",
            import_last_column="G",
            username_header="username",
            roles_header="encore_roles",
            main_isv_header="Main Team ISV",
            main_power_header="Main Team Power",
            encore_isv_header="Encore Team ISV",
            encore_power_header="Encore Team Power",
        ),
    )

    assert formula.startswith("=LET(")
    assert "本走候補（実効値：高→低）" in formula  # noqa: RUF001
    assert "アンコ候補（実効値：高→低）" in formula  # noqa: RUF001
    assert "編成未登録" in formula
    assert 'rolesHeader, "encore_roles"' in formula
    assert "threshold, IF(ISNUMBER(J5), J5, NA())" in formula
    assert "effectivePower > threshold" in formula
    assert "> 35" not in formula
    assert "recruitmentSlots, {4;6}" in formula
    assert "'Shift Entry'!F3:AI" in formula
    assert "runnerNames, B2:B4" in formula
    assert "runnerName, INDEX(runnerNames, XMATCH(hour, hourSlots, 0))" in formula
    assert "N(keys <> runnerName) * N(names <> runnerName)" in formula
    assert "runnerUsername" not in formula
    assert "SORT(" in formula
    assert (
        "HSTACK(honsoBlock, blankColumn, encoreBlock, blankColumn, unregisteredBlock)"
    ) in formula
    assert formula.count("(") == formula.count(")")


def test_candidate_formula_falls_back_to_entry_order_without_team_source() -> None:
    schedule = DraftSchedule(
        runner=None,
        hours=[4],
        assignments=[HourShiftAssignment(4)],
        display_names={},
    )

    formula = DraftWorksheetContent.candidate_formula(
        schedule,
        entry_worksheet_title="Shift Entry",
        recruitment_slots={4},
        encore_power_threshold_cell="J5",
        team_source=None,
    )

    assert "本走候補（登録順）" in formula  # noqa: RUF001
    assert "IMPORTRANGE" not in formula
    assert "honsoEligible, MAP(usernames, LAMBDA(username, TRUE))" in formula
    assert "encoreEligible, MAP(usernames, LAMBDA(username, FALSE))" in formula
    assert "unregistered, MAP(usernames, LAMBDA(username, FALSE))" in formula
    assert formula.count("(") == formula.count(")")


def test_lookup_updates_build_exact_layout_and_cleanup() -> None:
    schedule = DraftSchedule(
        runner=None,
        hours=[4, 5],
        assignments=[HourShiftAssignment(4), HourShiftAssignment(5)],
        display_names={},
    )
    team_source = DraftNotesTeamSource(
        sheet_url="https://team.example",
        worksheet_title="Team Summary",
        import_last_column="G",
        username_header="username",
        roles_header="encore_roles",
        main_isv_header="Main Team ISV",
        main_power_header="Main Team Power",
        encore_isv_header="Encore Team ISV",
        encore_power_header="Encore Team Power",
    )

    updates, formula_ranges = DraftWorksheetContent.lookup_updates(
        schedule,
        old_lookup_row=9,
        entry_worksheet_title="Shift Entry",
        team_source=team_source,
    )

    assert {"range": "J9:L13", "values": []} in updates
    assert {"range": "J6:K6", "values": [["名前を貼り付け", ""]]} in updates
    assert {"range": "J7", "values": [["シフト時間"]]} in updates
    assert {"range": "J8", "values": [["シフト元メッセージ"]]} in updates
    assert {"range": "J9", "values": [["編成一覧"]]} in updates
    assert {"L6", "K7", "K8", "J10"} <= formula_ranges
    assert "J9" not in formula_ranges

    formulas = {
        str(update["range"]): str(update["values"][0][0])
        for update in updates
        if str(update["range"]) in formula_ranges
    }
    assert "⚠️ 参加者を特定できません" in formulas["L6"]
    assert "K6" in formulas["L6"]
    assert "XMATCH(inputName, keys, 0)" in formulas["L6"]
    assert all("runnerNames, B2:B3" in formula for formula in formulas.values())
    assert all("runnerUsername" not in formula for formula in formulas.values())
    assert 'TEXTJOIN("・", TRUE' in formulas["K7"]
    assert "AJ3:AJ" in formulas["K8"]
    assert "IMPORTRANGE" in formulas["J10"]
    assert "'Team Summary'!A:G" in formulas["J10"]
    assert "VSTACK(teamHeaders" in formulas["J10"]
    assert (
        'matchCount, IF(matchedUsername = "", 0, '
        "SUMPRODUCT(N(teamUsernames = matchedUsername)))"
    ) in formulas["J10"]
    assert all(
        formula.count("(") == formula.count(")") for formula in formulas.values()
    )


def test_lookup_updates_omit_team_formula_and_old_cleanup_without_source() -> None:
    schedule = DraftSchedule(
        runner=None,
        hours=[4],
        assignments=[HourShiftAssignment(4)],
        display_names={},
    )

    updates, formula_ranges = DraftWorksheetContent.lookup_updates(
        schedule,
        old_lookup_row=None,
        entry_worksheet_title="Shift Entry",
        team_source=None,
    )

    assert not any(update["values"] == [] for update in updates)
    assert formula_ranges == {"L5", "K6", "K7"}
    assert not any(update["range"] == "J8" for update in updates)
    assert not any(update["values"] == [["編成一覧"]] for update in updates)


def test_old_draft_layout_detection_requires_owned_labels() -> None:
    assert (
        shift_register_manager._old_draft_last_row(  # noqa: SLF001
            [["JST"], ["4-5"], ["5-6"], [], ["メモ"]]
        )
        == 3
    )
    assert shift_register_manager._old_draft_last_row([]) == 1  # noqa: SLF001
    assert (
        shift_register_manager._old_draft_last_row(  # noqa: SLF001
            [["manual"], ["4-5"]]
        )
        == 1
    )

    lookup_labels = [
        [""],
        [""],
        [""],
        [""],
        ["名前を貼り付け"],
        ["シフト時間"],
        ["シフト元メッセージ"],
    ]
    assert (
        shift_register_manager._old_lookup_row(  # noqa: SLF001
            old_last_row=3,
            lookup_labels=lookup_labels,
        )
        == 5
    )
    lookup_labels.insert(4, [""])
    assert (
        shift_register_manager._old_lookup_row(  # noqa: SLF001
            old_last_row=3,
            lookup_labels=lookup_labels,
        )
        == 6
    )
    assert (
        shift_register_manager._old_lookup_row(  # noqa: SLF001
            old_last_row=3,
            lookup_labels=[["manual"]],
        )
        is None
    )
    threshold_labels = [[""] for _ in range(4)]
    threshold_labels[3] = [DraftWorksheetContent.CANDIDATE_THRESHOLD_LABEL]
    assert (
        shift_register_manager._old_candidate_threshold_row(  # noqa: SLF001
            old_last_row=3,
            threshold_labels=threshold_labels,
        )
        == 4
    )
    assert (
        shift_register_manager._old_candidate_threshold_row(  # noqa: SLF001
            old_last_row=3,
            threshold_labels=[["manual"] for _ in range(4)],
        )
        is None
    )
    assert (
        shift_register_manager._old_candidate_threshold_row(  # noqa: SLF001
            old_last_row=3,
            threshold_labels=[
                [""],
                [""],
                [""],
                ["アンコ候補総合力閾値"],
            ],
        )
        is None
    )


def test_old_notes_detection_requires_expected_signed_formula() -> None:
    rows = [[""] for _ in range(33)]
    rows[32] = ['=LET(owner, "rhoboto-shift-draft-notes", shifts, C2:G31, shifts)']
    assert (
        shift_register_manager._old_notes_row(  # noqa: SLF001
            old_last_row=31,
            rows=rows,
        )
        == 33
    )

    rows[32] = ["=LET(shifts, C2:G31, encore, C2:C31, hourSlots, {0})"]
    assert (
        shift_register_manager._old_notes_row(  # noqa: SLF001
            old_last_row=31,
            rows=rows,
        )
        == 33
    )

    for unrelated in ("manual value", "=SUM(A1:A2)", 1):
        rows[32] = [unrelated]
        assert (
            shift_register_manager._old_notes_row(  # noqa: SLF001
                old_last_row=31,
                rows=rows,
            )
            is None
        )


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
async def test_generate_draft_writes_draft_worksheet(  # noqa: PLR0915
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = ShiftRegisterManager(make_feature_channel(), "service.json")
    value_sheet = configure_draft_value_sheet(manager)
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
    old_axis_rows = [
        ["JST"],
        *([f"{hour}-{hour + 1}"] for hour in range(2, 24)),
    ]
    old_lookup_labels = [[""] for _ in range(36)]
    old_lookup_labels[24:27] = [
        ["名前を貼り付け"],
        ["シフト時間"],
        ["シフト元メッセージ"],
    ]
    old_threshold_labels = [[""] for _ in range(32)]
    old_threshold_labels[23] = [DraftWorksheetContent.CANDIDATE_THRESHOLD_LABEL]
    draft_worksheet = DraftBatchFakeWorksheet(
        title="Shift Draft",
        worksheet_id=2,
        old_axis_rows=old_axis_rows,
        old_threshold_labels=old_threshold_labels,
        old_lookup_labels=old_lookup_labels,
    )
    draft_worksheet.conditional_format_rules = [
        {
            "booleanRule": {
                "condition": {
                    "values": [
                        {
                            "userEnteredValue": (
                                '=AND(I2<>"",N("rhoboto:shift-draft:candidate:v0")=0)'
                            )
                        }
                    ]
                }
            }
        },
        {"booleanRule": {"condition": {"values": []}}},
    ]
    metadata = ShiftRegisterGoogleSheetsMetadata(
        "https://docs.google.com/spreadsheets/d/shift-draft/edit",
        [
            EntryWorksheetMetadata(1, "Shift Entry", entry_worksheet),
            DraftWorksheetMetadata(2, "Shift Draft", draft_worksheet),
            FinalScheduleWorksheetMetadata(3, "Shift Final Schedule", None),
        ],
    )

    main = FakeWorksheet(title="Main Team", worksheet_id=101)
    main.values = [
        TeamWorksheetContent.COLUMNS,
        ["alice", "Team Alice", 150, 740, 33.4, "main"],
        ["bob", "Team Bob", 140, 680, 32.0, "main"],
        ["carol", "Team Carol", 130, 620, 30.0, "main"],
    ]
    main.row_count = 100
    main.col_count = 20
    encore = FakeWorksheet(title="Encore Team", worksheet_id=102)
    encore.values = [TeamWorksheetContent.COLUMNS]
    encore.row_count = 100
    encore.col_count = 20
    summary = FakeWorksheet(title="Team Summary", worksheet_id=201)
    summary.values = [
        [
            "username",
            "display_name",
            "encore_roles",
            "Main Team ISV",
            "Main Team Power",
            "Encore Team ISV",
            "Encore Team Power",
            "original_message",
        ]
    ]
    summary.row_count = 100
    summary.col_count = 20
    source_config = SimpleNamespace(
        sheet_url=metadata.sheet_url,
        landing_worksheet_id=201,
        encore_role_ids=[],
    )
    source_metadata = TeamRegisterGoogleSheetsMetadata.from_subtyped_worksheets(
        metadata.sheet_url,
        [
            TeamWorksheetMetadata(101, "Main Team", main),
            TeamWorksheetMetadata(102, "Encore Team", encore),
            SummaryWorksheetMetadata(201, "Team Summary", summary),
        ],
    )

    def resolve_profiles(
        _source: object,
        _summaries: object,
    ) -> DraftTeamProfileResolution:
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
                roles_header="encore_roles",
                main_isv_header="Main Team ISV",
                main_power_header="Main Team Power",
                encore_isv_header="Encore Team ISV",
                encore_power_header="Encore Team Power",
            ),
        )

    monkeypatch.setattr(
        manager,
        "_resolve_team_source_metadata",
        AsyncMock(
            return_value=(
                TeamSourceStatus.AVAILABLE,
                source_config,
                source_metadata,
            )
        ),
    )
    monkeypatch.setattr(
        manager,
        "_draft_profiles_from_summary",
        resolve_profiles,
    )

    result = await manager.generate_draft(
        metadata,
        member_by_names={},
        encore_power_threshold=35,
        runner=make_runner(),
    )

    data = draft_worksheet.typed_batches[-1]
    assert data[0]["range"] == "A1:G31"
    assert data[0]["values"][0] == DraftWorksheetContent.COLUMNS
    assert data[0]["values"][1][0] == "4-5"
    assert data[0]["values"][1][1] == "Run"
    assert [row[0] for row in data[0]["values"][1:]] == [
        f"{hour}-{hour + 1}" for hour in range(4, 22)
    ]
    assert not any(str(item["range"]).startswith("H") for item in data)
    assert {"range": "I24:M24", "values": []} in data
    assert {
        "range": "I20:M20",
        "values": [
            [
                "仮配置済：緑背景",  # noqa: RUF001
                "アンコ配置済：緑背景＋赤字",  # noqa: RUF001
                "アンコ候補閾値",
                35,
                "万総合力",
            ]
        ],
    } in data
    assert {"range": "J25:L29", "values": []} in data
    assert {"range": "J25", "values": [["編成一覧"]]} in data
    notes_update = next(item for item in data if item["range"] == "A21")
    candidate_update = next(item for item in data if item["range"] == "I1")
    assert str(notes_update["values"][0][0]).startswith("=LET(")
    assert DraftWorksheetContent.NOTES_FORMULA_SIGNATURE in str(
        notes_update["values"][0][0]
    )
    assert "募集時間【4-7・20-22】" in str(notes_update["values"][0][0])
    assert "本走候補（実効値：高→低）" in str(  # noqa: RUF001
        candidate_update["values"][0][0]
    )
    assert "threshold, IF(ISNUMBER(L20), L20, NA())" in str(
        candidate_update["values"][0][0]
    )
    assert {"A21", "I1", "L22", "K23", "K24", "J26"} <= (
        draft_worksheet.formula_ranges[-1]
    )
    assert not any(str(item["range"]).startswith("I:") for item in data)
    assert draft_worksheet.background_updates[-1][0] == ("A1:G23", "#FFFFFF")
    assert draft_worksheet.background_updates[-1][1:] == [
        *((f"B{row}:G{row}", "#CCCCCC") for row in range(5, 18)),
        ("I24:M24", "#FFFFFF"),
        ("I20:J20", "#D9EAD3"),
        ("K20", "#A4C2F4"),
        ("L20", "#FFF2CC"),
        ("M20", "#A4C2F4"),
        ("J25:L29", "#FFFFFF"),
        ("J22:L24", "#FFFFFF"),
        ("J22:J24", "#A4C2F4"),
        ("K22", "#FFF2CC"),
        ("J25:L25", "#A4C2F4"),
    ]
    assert draft_worksheet.border_updates[-1] == [
        ("A1:G23", None, "NONE", shift_register_manager.BORDER_NAMES),
        (
            "A1:G19",
            "#000000",
            "SOLID",
            shift_register_manager.OUTER_BORDER_SIDES,
        ),
        ("A1:G1", "#000000", "SOLID", ("bottom",)),
        ("I1:I24", None, "NONE", shift_register_manager.BORDER_NAMES),
        ("J24:M24", None, "NONE", shift_register_manager.BORDER_NAMES),
        ("I1:I20", "#000000", "SOLID", ("left",)),
        (
            "I20:M20",
            "#000000",
            "SOLID",
            shift_register_manager.OUTER_BORDER_SIDES,
        ),
        ("L20", "#FF0000", "SOLID_MEDIUM", ("top", "bottom", "left", "right")),
        (
            "B2:G19",
            "#FF0000",
            "SOLID_MEDIUM",
            shift_register_manager.OUTER_BORDER_SIDES,
        ),
        ("J25:L30", None, "NONE", shift_register_manager.BORDER_NAMES),
        ("J22:L27", None, "NONE", shift_register_manager.BORDER_NAMES),
        ("J22:L22", "#000000", "SOLID", ("top",)),
        ("J22:J27", "#000000", "SOLID", ("left",)),
        (
            "K22",
            "#FF0000",
            "SOLID_MEDIUM",
            ("top", "bottom", "left", "right"),
        ),
    ]
    assert draft_worksheet.format_updates[-1] == [
        (
            "J24",
            {
                "textFormat": {
                    "foregroundColorStyle": {
                        "rgbColor": {"red": 0.0, "green": 0.0, "blue": 0.0}
                    }
                }
            },
            "userEnteredFormat.textFormat.foregroundColorStyle",
        ),
        (
            "M24",
            {
                "textFormat": {
                    "foregroundColorStyle": {
                        "rgbColor": {"red": 0.0, "green": 0.0, "blue": 0.0}
                    }
                }
            },
            "userEnteredFormat.textFormat.foregroundColorStyle",
        ),
        (
            "J20",
            {
                "textFormat": {
                    "foregroundColorStyle": {
                        "rgbColor": {"red": 1.0, "green": 0.0, "blue": 0.0}
                    }
                }
            },
            "userEnteredFormat.textFormat.foregroundColorStyle",
        ),
        (
            "M20",
            {
                "textFormat": {
                    "foregroundColorStyle": {
                        "rgbColor": {"red": 0.0, "green": 0.0, "blue": 0.0}
                    }
                }
            },
            "userEnteredFormat.textFormat.foregroundColorStyle",
        ),
    ]
    assert draft_worksheet.frozen_column_counts[-1] == 1
    assert draft_worksheet.conditional_format_rule_deletes[-1] == [0]
    candidate_rules = list(reversed(draft_worksheet.conditional_format_rule_adds[-1]))
    assert len(candidate_rules) == 2
    assert all(
        rule["ranges"]
        == [
            {
                "sheetId": 2,
                "startRowIndex": 1,
                "endRowIndex": 19,
                "startColumnIndex": 8,
            }
        ]
        for rule in candidate_rules
    )
    assert candidate_rules[0]["booleanRule"] == {
        "condition": {
            "type": "CUSTOM_FORMULA",
            "values": [
                {
                    "userEnteredValue": (
                        '=AND(I2<>"",$C2=I2,N("rhoboto:shift-draft:candidate:v1")=0)'
                    )
                }
            ],
        },
        "format": {
            "backgroundColorStyle": {
                "rgbColor": {
                    "red": 0.8509803921568627,
                    "green": 0.9176470588235294,
                    "blue": 0.8274509803921568,
                }
            },
            "textFormat": {
                "foregroundColorStyle": {
                    "rgbColor": {"red": 1.0, "green": 0.0, "blue": 0.0}
                }
            },
        },
    }
    assert candidate_rules[1]["booleanRule"]["condition"]["values"] == [
        {
            "userEnteredValue": (
                '=AND(I2<>"",SUMPRODUCT(N($C2:$G2=I2))>0,'
                'N("rhoboto:shift-draft:candidate:v1")=0)'
            )
        }
    ]
    assert candidate_rules[1]["booleanRule"]["format"] == {
        "backgroundColorStyle": {
            "rgbColor": {
                "red": 0.8509803921568627,
                "green": 0.9176470588235294,
                "blue": 0.8274509803921568,
            }
        }
    }
    assert draft_worksheet.typed_minimums == [(38, 13)]
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
    assert value_sheet.batch_reads == [[1, 2, 101, 102, 201]]


@pytest.mark.asyncio
async def test_generate_draft_uses_shared_live_summary_without_readback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = ShiftRegisterManager(make_feature_channel(), "service.json")
    value_sheet = configure_draft_value_sheet(manager)
    manager._sheet_config = SimpleNamespace(  # noqa: SLF001
        recruitment_time_ranges=[{"start": 4, "end": 5}]
    )
    entry_worksheet = EntryRangeFakeWorksheet(
        build_entry_ranges([("alice", "Shift Alice", {4})])
    )
    draft_worksheet = DraftBatchFakeWorksheet(title="Shift Draft", worksheet_id=2)
    metadata = ShiftRegisterGoogleSheetsMetadata(
        value_sheet.sheet_url,
        [
            EntryWorksheetMetadata(1, "Shift Entry", entry_worksheet),
            DraftWorksheetMetadata(2, "Shift Draft", draft_worksheet),
            FinalScheduleWorksheetMetadata(3, "Shift Final Schedule", None),
        ],
    )
    main = FakeWorksheet(title="Main Team", worksheet_id=101)
    main.values = [
        TeamWorksheetContent.COLUMNS,
        ["alice", "Team Alice", 150, 740, 33.4, "main"],
    ]
    main.row_count = 100
    main.col_count = 20
    encore = FakeWorksheet(title="Encore Team", worksheet_id=102)
    encore.values = [
        TeamWorksheetContent.COLUMNS,
        ["alice", "Team Alice", 140, 680, 35.3, "encore"],
    ]
    encore.row_count = 100
    encore.col_count = 20
    summary = FakeWorksheet(title="Team Summary", worksheet_id=201)
    summary.values = [
        [
            "username",
            "display_name",
            "encore_roles",
            "Main Team ISV",
            "Main Team Power",
            "Encore Team ISV",
            "Encore Team Power",
            "original_message",
        ],
        ["alice", "Stale Alice", "Stale Role", 1, 2, 3, 4, "stale"],
    ]
    summary.row_count = 100
    summary.col_count = 20
    source_config = SimpleNamespace(
        sheet_url=value_sheet.sheet_url,
        landing_worksheet_id=201,
        encore_role_ids=[10],
    )
    source_metadata = TeamRegisterGoogleSheetsMetadata.from_subtyped_worksheets(
        value_sheet.sheet_url,
        [
            TeamWorksheetMetadata(101, "Main Team", main),
            TeamWorksheetMetadata(102, "Encore Team", encore),
            SummaryWorksheetMetadata(201, "Team Summary", summary),
        ],
    )
    monkeypatch.setattr(
        manager,
        "_resolve_team_source_metadata",
        AsyncMock(
            return_value=(
                TeamSourceStatus.AVAILABLE,
                source_config,
                source_metadata,
            )
        ),
    )

    result = await manager.generate_draft(
        metadata,
        member_by_names={
            "alice": SimpleNamespace(
                display_name="Discord Alice",
                roles=[SimpleNamespace(id=10, name="Encore")],
            )
        },
        encore_power_threshold=35,
    )

    assert value_sheet.batch_reads == [[1, 2, 101, 102, 201]]
    assert len(value_sheet.batch_updates) == 1
    summary_mutations, draft_requests = value_sheet.batch_updates[0]
    assert summary_mutations
    assert draft_requests == draft_worksheet.typed_batches[-1]
    assert any(
        "Discord Alice" in mutation.rows[0]
        for mutation in summary_mutations
        if hasattr(mutation, "rows")
    )
    assert result.team_source_status is TeamSourceStatus.AVAILABLE
    assert (
        result.team_summary_url
        == "https://docs.google.com/spreadsheets/d/shift-draft/edit?gid=201#gid=201"
    )
    assert result.unregistered_usernames == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure_stage", "structure_changed"),
    [
        (None, False),
        ("planning", False),
        ("contract", False),
        ("repair", False),
        ("summary", False),
        ("draft", False),
        ("draft", True),
    ],
)
async def test_generate_draft_writes_once_per_separate_spreadsheet(
    monkeypatch: pytest.MonkeyPatch,
    *,
    failure_stage: str | None,
    structure_changed: bool,
) -> None:
    manager = ShiftRegisterManager(make_feature_channel(), "service.json")
    shift_sheet = configure_draft_value_sheet(manager)
    entry = EntryRangeFakeWorksheet([[], [], []])
    draft = DraftBatchFakeWorksheet(title="Shift Draft", worksheet_id=2)
    metadata = ShiftRegisterGoogleSheetsMetadata(
        shift_sheet.sheet_url,
        [
            EntryWorksheetMetadata(1, "Shift Entry", entry),
            DraftWorksheetMetadata(2, "Shift Draft", draft),
            FinalScheduleWorksheetMetadata(3, "Shift Final Schedule", None),
        ],
    )
    main = FakeWorksheet(title="Main Team", worksheet_id=101)
    summary = FakeWorksheet(title="Team Summary", worksheet_id=201)
    source_config = SimpleNamespace(
        sheet_url="https://docs.google.com/spreadsheets/d/team-source/edit",
        encore_role_ids=[],
    )
    source_metadata = TeamRegisterGoogleSheetsMetadata.from_subtyped_worksheets(
        source_config.sheet_url,
        [
            TeamWorksheetMetadata(101, "Main Team", main),
            SummaryWorksheetMetadata(201, "Team Summary", summary),
        ],
    )
    source = shift_register_manager.TeamSource(
        source_config,
        source_metadata,
        shift_register_manager.TeamSummaryColumns(1, 3, 4, 5, None, None, "F"),
    )
    resolution = shift_register_manager.TeamSourceResolution(
        TeamSourceStatus.AVAILABLE,
        source,
    )
    read_resolution = (
        shift_register_manager.TeamSourceResolution(TeamSourceStatus.INVALID)
        if failure_stage in {"contract", "repair"}
        else resolution
    )
    source_sheet = DraftValueGoogleSheet()
    source_sheet.sheet_url = source_config.sheet_url
    if failure_stage == "summary":
        source_sheet.batch_update_error = StorageError(
            StorageErrorKind.GOOGLE_SHEETS_TRANSIENT
        )
    if failure_stage == "draft":
        shift_sheet.batch_update_error = StorageError(
            StorageErrorKind.GOOGLE_SHEETS_TRANSIENT
        )
    planned_result = SimpleNamespace()
    monkeypatch.setattr(
        manager,
        "_resolve_team_source_metadata",
        AsyncMock(
            return_value=(TeamSourceStatus.AVAILABLE, source_config, source_metadata)
        ),
    )
    monkeypatch.setattr(
        manager,
        "_read_shift_and_team_source_locked",
        AsyncMock(
            return_value=(
                {entry.id: entry.values, draft.id: draft.values},
                read_resolution,
                {},
            )
        ),
    )
    monkeypatch.setattr(
        manager,
        "_summary_grid_plan",
        Mock(
            side_effect=(
                WorksheetContractError if failure_stage == "contract" else None
            ),
            return_value=SimpleNamespace(
                summary_headers=(),
                summaries=(),
                mutations=("summary",),
            ),
        ),
    )
    monkeypatch.setattr(manager, "_build_team_source", Mock(return_value=resolution))
    monkeypatch.setattr(
        manager,
        "_draft_profiles_from_summary",
        Mock(return_value=DraftTeamProfileResolution(TeamSourceStatus.AVAILABLE, {})),
    )
    monkeypatch.setattr(
        manager,
        "_plan_draft_locked",
        AsyncMock(
            side_effect=(
                WorksheetContractError if failure_stage == "planning" else None
            ),
            return_value=(planned_result, [{"draft": True}]),
        ),
    )
    if structure_changed:
        monkeypatch.setattr(
            manager,
            "_ensure_current_worksheets",
            AsyncMock(return_value=(metadata, True)),
        )
    monkeypatch.setattr(
        shift_register_manager,
        "GoogleSheet",
        lambda _url, _path: source_sheet,
    )

    if failure_stage in {"planning", "contract"}:
        with pytest.raises(WorksheetContractError):
            await manager.generate_draft(
                metadata,
                member_by_names={},
                encore_power_threshold=35,
            )
    elif failure_stage in {"summary", "draft"}:
        with pytest.raises(StorageError) as exc_info:
            await manager.generate_draft(
                metadata,
                member_by_names={},
                encore_power_threshold=35,
            )
        if failure_stage == "summary":
            assert exc_info.value.kind is StorageErrorKind.GOOGLE_SHEETS_TRANSIENT
        else:
            assert exc_info.value.kind is StorageErrorKind.PARTIAL_SUCCESS
            assert exc_info.value.log_hint == "team_summary_refreshed_draft_incomplete"
            assert isinstance(exc_info.value.__cause__, StorageError)
            assert (
                exc_info.value.__cause__.kind
                is StorageErrorKind.GOOGLE_SHEETS_TRANSIENT
            )
    else:
        result = await manager.generate_draft(
            metadata,
            member_by_names={},
            encore_power_threshold=35,
        )
        assert result is planned_result

    assert source_sheet.batch_updates == (
        [] if failure_stage in {"planning", "contract"} else [(["summary"], [])]
    )
    assert shift_sheet.batch_updates == (
        [([], [{"draft": True}])] if failure_stage in {None, "repair", "draft"} else []
    )


@pytest.mark.asyncio
async def test_generate_draft_accepts_completely_empty_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = ShiftRegisterManager(make_feature_channel(), "service.json")
    value_sheet = configure_draft_value_sheet(manager)
    manager._sheet_config = SimpleNamespace(  # noqa: SLF001
        recruitment_time_ranges=[{"start": 4, "end": 5}]
    )
    entry_worksheet = EntryRangeFakeWorksheet([[], [], []])
    draft_worksheet = DraftBatchFakeWorksheet(
        title="Shift Draft",
        row_count=1,
        col_count=1,
    )
    metadata = ShiftRegisterGoogleSheetsMetadata(
        "https://docs.google.com/spreadsheets/d/shift-draft/edit",
        [
            EntryWorksheetMetadata(1, "Shift Entry", entry_worksheet),
            DraftWorksheetMetadata(2, "Shift Draft", draft_worksheet),
            FinalScheduleWorksheetMetadata(3, "Shift Final Schedule", None),
        ],
    )

    monkeypatch.setattr(
        manager,
        "_resolve_team_source_metadata",
        AsyncMock(return_value=(TeamSourceStatus.UNSET, None, None)),
    )

    result = await manager.generate_draft(
        metadata,
        member_by_names={},
        encore_power_threshold=35,
    )

    assert result.schedule.display_names == {}
    assert result.team_summary_url is None
    assert result.schedule.hours == [4]
    assert {"A4", "I1"} <= draft_worksheet.formula_ranges[-1]
    assert value_sheet.batch_reads == [[1, 2]]
    assert len(value_sheet.batch_updates) == 1
    assert draft_worksheet.typed_minimums == [(38, 13)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("old_notes_value", "clears_old_anchor"),
    [
        (
            '=LET(owner, "rhoboto-shift-draft-notes", shifts, C2:G31, shifts)',
            True,
        ),
        ("manual value", False),
    ],
)
async def test_generate_draft_clears_only_signed_old_notes_anchor(
    monkeypatch: pytest.MonkeyPatch,
    old_notes_value: str,
    *,
    clears_old_anchor: bool,
) -> None:
    manager = ShiftRegisterManager(make_feature_channel(), "service.json")
    configure_draft_value_sheet(manager)
    manager._sheet_config = SimpleNamespace(  # noqa: SLF001
        recruitment_time_ranges=[{"start": 4, "end": 5}]
    )
    entry_worksheet = EntryRangeFakeWorksheet(build_entry_ranges([]))
    old_axis_rows = [
        ["JST"],
        *([f"{hour}-{hour + 1}"] for hour in range(30)),
        [""],
        [old_notes_value],
    ]
    draft_worksheet = DraftBatchFakeWorksheet(
        title="Shift Draft",
        old_axis_rows=old_axis_rows,
    )
    metadata = ShiftRegisterGoogleSheetsMetadata(
        "https://docs.google.com/spreadsheets/d/shift-draft/edit",
        [
            EntryWorksheetMetadata(1, "Shift Entry", entry_worksheet),
            DraftWorksheetMetadata(2, "Shift Draft", draft_worksheet),
            FinalScheduleWorksheetMetadata(3, "Shift Final Schedule", None),
        ],
    )

    monkeypatch.setattr(
        manager,
        "_resolve_team_source_metadata",
        AsyncMock(return_value=(TeamSourceStatus.UNSET, None, None)),
    )

    await manager.generate_draft(
        metadata,
        member_by_names={},
        encore_power_threshold=35,
    )

    cleared_ranges = {
        str(item["range"])
        for item in draft_worksheet.typed_batches[-1]
        if item["values"] == []
    }
    assert ("A33" in cleared_ranges) is clears_old_anchor


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
    configure_draft_value_sheet(manager)
    manager._sheet_config = SimpleNamespace(  # noqa: SLF001
        recruitment_time_ranges=[{"start": 4, "end": 5}]
    )
    entry_worksheet = EntryRangeFakeWorksheet(
        build_entry_ranges([("alice", "Alice", {4})])
    )
    draft_worksheet = DraftBatchFakeWorksheet(title="Shift Draft")
    metadata = ShiftRegisterGoogleSheetsMetadata(
        "https://docs.google.com/spreadsheets/d/shift-draft/edit",
        [
            EntryWorksheetMetadata(1, "Shift Entry", entry_worksheet),
            DraftWorksheetMetadata(2, "Shift Draft", draft_worksheet),
            FinalScheduleWorksheetMetadata(3, "Shift Final Schedule", None),
        ],
    )

    monkeypatch.setattr(
        manager,
        "_resolve_team_source_metadata",
        AsyncMock(return_value=(status, None, None)),
    )

    result = await manager.generate_draft(
        metadata,
        member_by_names={},
        encore_power_threshold=35,
    )

    assignment = result.schedule.assignments[0]
    assert "encore" not in assignment.supporter_usernames_by_slot
    assert result.team_source_warning is not None
    assert result.team_summary_url is None
    assert result.unregistered_usernames == ()
    expected_marker = "⚠️ " if status is TeamSourceStatus.UNSET else "⚠️🛠️ "
    assert result.team_source_warning.startswith(expected_marker)
    notes_update = next(
        item for item in draft_worksheet.typed_batches[-1] if item["range"] == "A4"
    )
    formula = str(notes_update["values"][0][0])
    assert result.team_source_warning in formula
    assert draft_worksheet.background_updates[-1][-7:] == [
        ("I3:J3", "#D9EAD3"),
        ("K3", "#A4C2F4"),
        ("L3", "#FFF2CC"),
        ("M3", "#A4C2F4"),
        ("J5:L7", "#FFFFFF"),
        ("J5:J7", "#A4C2F4"),
        ("K5", "#FFF2CC"),
    ]
    assert ("J8:L8", "#A4C2F4") not in draft_worksheet.background_updates[-1]
    assert draft_worksheet.border_updates[-1][-4:] == [
        ("J5:L10", None, "NONE", shift_register_manager.BORDER_NAMES),
        ("J5:L5", "#000000", "SOLID", ("top",)),
        ("J5:J10", "#000000", "SOLID", ("left",)),
        (
            "K5",
            "#FF0000",
            "SOLID_MEDIUM",
            ("top", "bottom", "left", "right"),
        ),
    ]
    assert draft_worksheet.frozen_column_counts[-1] == 1


@pytest.mark.asyncio
async def test_generate_draft_rejects_old_entry_header() -> None:
    manager = ShiftRegisterManager(make_feature_channel(), "service.json")
    configure_draft_value_sheet(manager)
    manager._sheet_config = SimpleNamespace(  # noqa: SLF001
        recruitment_time_ranges=[{"start": 4, "end": 7}],
        team_source_feature_channel_id=None,
    )
    old_columns = [
        "username",
        "display_name",
        *[f"{hour}-{hour + 1}" for hour in range(4, 28)],
        "original_message",
    ]
    entry_worksheet = EntryRangeFakeWorksheet([[[], old_columns], [], []])
    draft_worksheet = DraftBatchFakeWorksheet(title="Shift Draft")
    metadata = ShiftRegisterGoogleSheetsMetadata(
        "https://docs.google.com/spreadsheets/d/shift-draft/edit",
        [
            EntryWorksheetMetadata(1, "Shift Entry", entry_worksheet),
            DraftWorksheetMetadata(2, "Shift Draft", draft_worksheet),
            FinalScheduleWorksheetMetadata(3, "Shift Final Schedule", None),
        ],
    )

    with pytest.raises(WorksheetContractError):
        await manager.generate_draft(
            metadata,
            member_by_names={},
            encore_power_threshold=35,
            runner=make_runner(),
        )

    assert draft_worksheet.typed_batches == []


@pytest.mark.asyncio
async def test_generate_draft_rejects_nonbinary_entry_before_draft_write() -> None:
    manager = ShiftRegisterManager(make_feature_channel(), "service.json")
    value_sheet = configure_draft_value_sheet(manager)
    manager._sheet_config = SimpleNamespace(  # noqa: SLF001
        recruitment_time_ranges=[{"start": 4, "end": 7}],
        team_source_feature_channel_id=None,
    )
    entry_ranges = build_entry_ranges([("bob", "Bob", {4, 5})])
    entry_ranges[2][0][4] = "not binary"
    entry_worksheet = EntryRangeFakeWorksheet(entry_ranges)
    draft_worksheet = DraftBatchFakeWorksheet(title="Shift Draft")
    metadata = ShiftRegisterGoogleSheetsMetadata(
        "https://docs.google.com/spreadsheets/d/shift-draft/edit",
        [
            EntryWorksheetMetadata(1, "Shift Entry", entry_worksheet),
            DraftWorksheetMetadata(2, "Shift Draft", draft_worksheet),
            FinalScheduleWorksheetMetadata(3, "Shift Final Schedule", None),
        ],
    )

    with pytest.raises(WorksheetContractError):
        await manager.generate_draft(
            metadata,
            member_by_names={},
            encore_power_threshold=35,
        )

    assert value_sheet.batch_reads == [[1, 2]]
    assert draft_worksheet.typed_batches == []


@pytest.mark.asyncio
async def test_generate_draft_raises_when_draft_worksheet_missing() -> None:
    manager = ShiftRegisterManager(make_feature_channel(), "service.json")
    configure_draft_value_sheet(manager)
    manager._sheet_config = SimpleNamespace(  # noqa: SLF001
        recruitment_time_ranges=[{"start": 4, "end": 7}]
    )
    entry_worksheet = FakeWorksheet(title="Shift Entry")
    metadata = ShiftRegisterGoogleSheetsMetadata(
        "https://docs.google.com/spreadsheets/d/shift-draft/edit",
        [
            EntryWorksheetMetadata(1, "Shift Entry", entry_worksheet),
            DraftWorksheetMetadata(2, "Shift Draft", None),
            FinalScheduleWorksheetMetadata(3, "Shift Final Schedule", None),
        ],
    )

    async def keep_missing_metadata(
        _metadata: ShiftRegisterGoogleSheetsMetadata,
        *,
        required_worksheets: object,
    ) -> tuple[ShiftRegisterGoogleSheetsMetadata, bool]:
        del required_worksheets
        return metadata, False

    manager._ensure_current_worksheets = keep_missing_metadata  # type: ignore[method-assign]  # noqa: SLF001

    with pytest.raises(StorageError) as exc_info:
        await manager.generate_draft(
            metadata,
            member_by_names={},
            encore_power_threshold=35,
            runner=make_runner(),
        )

    assert exc_info.value.kind is StorageErrorKind.GOOGLE_SHEETS_MISSING_WORKSHEET
