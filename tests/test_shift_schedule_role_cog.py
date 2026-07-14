from __future__ import annotations

# ruff: noqa: E501, FBT001, FLY002, RUF001, SLF001
import asyncio
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from cogs import shift_register
from cogs.shift_register import (
    ScheduleRoleExecutionResult,
    ScheduleRolePreviewSnapshot,
    ShiftRegister,
    _apply_schedule_role_plan,
    _format_schedule_role_preview,
    _format_schedule_role_result,
    _replace_schedule_role_state_line,
)
from components.ui_shift_register import ScheduleRoleDecision
from tests.fakes import FakeInteraction
from utils.shift_final import parse_a1_range
from utils.shift_register_manager import FinalScheduleRoleSource
from utils.shift_schedule_role import (
    DuplicateScheduleRoleGroup,
    ScheduleRolePlan,
    ScheduleRoleResolution,
    ScheduleRoleUpdateMode,
)


@dataclass(frozen=True)
class FakeRole:
    id: int
    mention: str


class FakeMember:
    def __init__(
        self,
        member_id: int,
        *,
        name: str | None = None,
        display_name: str | None = None,
    ) -> None:
        self.id = member_id
        self.name = name or f"user{member_id}"
        self.display_name = display_name or self.name
        self.mention = f"<@{member_id}>"
        self.calls: list[tuple[str, int, bool]] = []
        self.fail_add = False
        self.fail_remove = False

    async def add_roles(self, role: FakeRole, *, atomic: bool) -> None:
        self.calls.append(("add", role.id, atomic))
        if self.fail_add:
            raise ExpectedHTTPError

    async def remove_roles(self, role: FakeRole, *, atomic: bool) -> None:
        self.calls.append(("remove", role.id, atomic))
        if self.fail_remove:
            raise ExpectedHTTPError


class ExpectedHTTPError(Exception):
    pass


class CommandRole:
    def __init__(
        self,
        role_id: int,
        members: list[FakeMember],
        *,
        assignable: bool = True,
    ) -> None:
        self.id = role_id
        self.mention = f"<@&{role_id}>"
        self.members = members
        self.assignable = assignable
        self.assignable_checks = 0

    def is_assignable(self) -> bool:
        self.assignable_checks += 1
        return self.assignable


class WorkflowMessage:
    def __init__(self) -> None:
        self.edits: list[tuple[object, dict[str, object]]] = []

    async def edit(self, content: object = None, **kwargs: object) -> None:
        self.edits.append((content, kwargs))


class WorkflowInteraction(FakeInteraction):
    async def edit_original_response(
        self,
        content: object = None,
        **kwargs: object,
    ) -> WorkflowMessage:
        self.original_response_edits.append((content, kwargs))
        self.control_message = WorkflowMessage()
        return self.control_message


class SnapshotManager:
    def __init__(
        self, sources: list[FinalScheduleRoleSource], events: list[str]
    ) -> None:
        self.sources = iter(sources)
        self.events = events

    async def fetch_google_sheets_metadata(self) -> object:
        return object()

    def log_missing_worksheet_warnings(self, _metadata: object) -> None:
        pass

    async def read_final_schedule_role_source(
        self,
        *_args: object,
        **_kwargs: object,
    ) -> FinalScheduleRoleSource:
        event = "initial_final_read" if not self.events else "revalidation_final_read"
        self.events.append(event)
        return next(self.sources)


class RecordingRoleLock:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def __call__(self, key: tuple[int, int]) -> RecordingRoleLock:
        assert key == (111, 90)
        self.events.append("role_lock_enter")
        return self

    async def __aenter__(self) -> None:
        pass

    async def __aexit__(self, *_args: object) -> None:
        self.events.append("role_lock_exit")


def test_assign_schedule_role_has_native_options_and_default() -> None:
    command = ShiftRegister.assign_schedule_role
    parameters = {parameter.name: parameter for parameter in command.parameters}

    assert command.name == "assign_schedule_role"
    assert parameters["role"].required is True
    assert parameters["role"].type.value == 8
    assert parameters["role_update_mode"].required is False
    assert [
        (choice.name, choice.value) for choice in parameters["role_update_mode"].choices
    ] == [
        ("只新增", "add_only"),
        ("完全取代（清除班表外成員）", "replace"),
    ]
    assert parameters["final_schedule_range"].required is False
    assert command.checks == []


@pytest.mark.asyncio
async def test_read_schedule_role_snapshot_resolves_current_guild_members() -> None:
    subject = ShiftRegister.__new__(ShiftRegister)
    members = (
        SimpleNamespace(id=1, name="alice", display_name="Alice"),
        SimpleNamespace(id=2, name="bob", display_name="Bob"),
    )
    source = FinalScheduleRoleSource(
        selected_range=parse_a1_range("B2:G12"),
        projected_values=((2, 2, "Alice"), (3, 2, "Bob")),
        labels=("Alice", "Bob"),
    )
    calls: list[str] = []

    class Manager:
        async def fetch_google_sheets_metadata(self) -> object:
            calls.append("metadata")
            return object()

        def log_missing_worksheet_warnings(self, _metadata: object) -> None:
            calls.append("warnings")

        async def read_final_schedule_role_source(
            self, *_args: object, **_kwargs: object
        ) -> FinalScheduleRoleSource:
            calls.append("final")
            return source

    context = SimpleNamespace(
        manager=Manager(),
        feature_config=SimpleNamespace(
            sheet_url="https://sheet.example",
            final_schedule_worksheet_id=3,
            final_schedule_anchor_cell="B2",
            recruitment_time_ranges=[{"start": 4, "end": 6}],
        ),
    )

    snapshot = await subject._read_schedule_role_snapshot(
        context,
        members=members,
        role_id=90,
        mode=ScheduleRoleUpdateMode.ADD_ONLY,
        final_schedule_range=parse_a1_range("B2:G12"),
    )

    assert isinstance(snapshot, ScheduleRolePreviewSnapshot)
    assert snapshot.sheet_url == "https://sheet.example"
    assert snapshot.final_schedule_worksheet_id == 3
    assert snapshot.role_id == 90
    assert snapshot.resolution.unique_member_ids == (1, 2)
    assert calls == ["metadata", "warnings", "final"]


def role_source(*labels: str) -> FinalScheduleRoleSource:
    return FinalScheduleRoleSource(
        selected_range=parse_a1_range("B2:G12"),
        projected_values=(),
        labels=labels,
    )


def role_context(manager: SnapshotManager) -> SimpleNamespace:
    return SimpleNamespace(
        manager=manager,
        feature_config=SimpleNamespace(
            sheet_url="https://sheet.example",
            final_schedule_worksheet_id=3,
            final_schedule_anchor_cell="B2",
            recruitment_time_ranges=[{"start": 4, "end": 6}],
        ),
    )


def make_role_subject(
    context: SimpleNamespace,
    events: list[str],
) -> ShiftRegister:
    subject = ShiftRegister.__new__(ShiftRegister)
    subject.logger = SimpleNamespace(warning=lambda *_args, **_kwargs: None)

    async def get_context(_source: object) -> SimpleNamespace:
        return context

    subject._get_shift_finalization_context_or_none = get_context  # type: ignore[method-assign]
    subject.schedule_role_lock = RecordingRoleLock(events)
    return subject


@pytest.mark.asyncio
async def test_assign_schedule_role_add_only_reads_once_and_changes_only_missing_members() -> (
    None
):
    events: list[str] = []
    alice = FakeMember(1, name="alice", display_name="Alice")
    bob = FakeMember(2, name="bob", display_name="Bob")
    role = CommandRole(90, [bob])
    guild = SimpleNamespace(id=111, members=[alice, bob])
    interaction = WorkflowInteraction(guild=guild)
    manager = SnapshotManager([role_source("Alice", "Bob", "Missing")], events)
    subject = make_role_subject(role_context(manager), events)

    await ShiftRegister.assign_schedule_role.callback(subject, interaction, role)

    assert role.assignable_checks == 1
    assert events == ["initial_final_read", "role_lock_enter", "role_lock_exit"]
    assert alice.calls == [("add", 90, True)]
    assert bob.calls == []
    assert interaction.original_response_edits[-1][1] == {"view": None}
    assert "原本已有 <@&90>：<@2>" in interaction.original_response_edits[-1][0]
    assert "⚠️ 找不到對應的 Discord 成員：`Missing`" in (
        interaction.original_response_edits[-1][0] or ""
    )


@pytest.mark.asyncio
async def test_assign_schedule_role_duplicate_preview_does_not_mutate_while_waiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    events: list[str] = []
    alice_one = FakeMember(1, name="alice_one", display_name="Alice")
    alice_two = FakeMember(2, name="alice_two", display_name="Alice")
    role = CommandRole(90, [])
    interaction = WorkflowInteraction(
        guild=SimpleNamespace(id=111, members=[alice_one, alice_two])
    )
    manager = SnapshotManager([role_source("Alice")], events)
    subject = make_role_subject(role_context(manager), events)

    class PendingView:
        decision = None

        def __init__(self, **_kwargs: object) -> None:
            pass

        async def wait(self) -> None:
            started.set()
            await release.wait()

    monkeypatch.setattr(
        "cogs.shift_register.AssignScheduleRoleConfirmView", PendingView
    )
    task = asyncio.create_task(
        ShiftRegister.assign_schedule_role.callback(subject, interaction, role)
    )
    await started.wait()

    assert alice_one.calls == []
    assert alice_two.calls == []
    release.set()
    await task
    assert events == ["initial_final_read"]


@pytest.mark.asyncio
async def test_assign_schedule_role_replace_revalidates_before_role_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class EventMember(FakeMember):
        async def add_roles(self, role: FakeRole, *, atomic: bool) -> None:
            events.append("role_calls")
            await super().add_roles(role, atomic=atomic)

        async def remove_roles(self, role: FakeRole, *, atomic: bool) -> None:
            events.append("role_calls")
            await super().remove_roles(role, atomic=atomic)

    alice = EventMember(1, name="alice", display_name="Alice")
    stale = EventMember(2, name="stale", display_name="Stale")
    role = CommandRole(90, [stale])
    interaction = WorkflowInteraction(
        guild=SimpleNamespace(id=111, members=[alice, stale])
    )
    manager = SnapshotManager([role_source("Alice"), role_source("Alice")], events)
    subject = make_role_subject(role_context(manager), events)

    class ConfirmView:
        decision = ScheduleRoleDecision.CONFIRM

        def __init__(self, **_kwargs: object) -> None:
            pass

        async def wait(self) -> None:
            events.append("wait_for_decision")

    monkeypatch.setattr(
        "cogs.shift_register.AssignScheduleRoleConfirmView", ConfirmView
    )
    await ShiftRegister.assign_schedule_role.callback(
        subject,
        interaction,
        role,
        "replace",
    )

    assert events == [
        "initial_final_read",
        "wait_for_decision",
        "revalidation_final_read",
        "role_lock_enter",
        "role_calls",
        "role_calls",
        "role_lock_exit",
    ]
    assert alice.calls == [("add", 90, True)]
    assert stale.calls == [("remove", 90, True)]
    assert interaction.followup.messages
    assert "### ✅ role 更新結果" in interaction.followup.messages[-1][0]
    assert interaction.control_message.edits[0][1] == {"view": None}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("decision", "expected_adds", "expected_skipped"),
    [
        (ScheduleRoleDecision.INCLUDE, (2,), False),
        (ScheduleRoleDecision.SKIP, (), True),
    ],
)
async def test_assign_schedule_role_duplicate_decision_controls_all_mutations(
    monkeypatch: pytest.MonkeyPatch,
    decision: ScheduleRoleDecision,
    expected_adds: tuple[int, ...],
    expected_skipped: bool,
) -> None:
    events: list[str] = []
    first = FakeMember(1, name="alice_one", display_name="Alice")
    second = FakeMember(2, name="alice_two", display_name="Alice")
    role = CommandRole(90, [first])
    interaction = WorkflowInteraction(
        guild=SimpleNamespace(id=111, members=[first, second])
    )
    manager = SnapshotManager([role_source("Alice"), role_source("Alice")], events)
    subject = make_role_subject(role_context(manager), events)

    class DecisionView:
        def __init__(self, **_kwargs: object) -> None:
            self.decision = decision

        async def wait(self) -> None:
            events.append("wait_for_decision")

    monkeypatch.setattr(
        "cogs.shift_register.AssignScheduleRoleConfirmView", DecisionView
    )
    await ShiftRegister.assign_schedule_role.callback(subject, interaction, role)

    assert first.calls == []
    assert second.calls == ([("add", 90, True)] if expected_adds else [])
    if expected_skipped:
        assert "已略過重複的成員：" in "\n".join(
            content or "" for content, _kwargs in interaction.followup.messages
        )


@pytest.mark.asyncio
async def test_assign_schedule_role_replace_empty_target_clears_current_members(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    first = FakeMember(1, name="first", display_name="First")
    second = FakeMember(2, name="second", display_name="Second")
    role = CommandRole(90, [first, second])
    interaction = WorkflowInteraction(
        guild=SimpleNamespace(id=111, members=[first, second])
    )
    manager = SnapshotManager([role_source(), role_source()], events)
    subject = make_role_subject(role_context(manager), events)

    class ConfirmView:
        decision = ScheduleRoleDecision.CONFIRM

        def __init__(self, **_kwargs: object) -> None:
            pass

        async def wait(self) -> None:
            events.append("wait_for_decision")

    monkeypatch.setattr(
        "cogs.shift_register.AssignScheduleRoleConfirmView", ConfirmView
    )
    await ShiftRegister.assign_schedule_role.callback(
        subject, interaction, role, "replace"
    )

    assert first.calls == [("remove", 90, True)]
    assert second.calls == [("remove", 90, True)]
    assert "已清除以下成員的 <@&90>：<@1>、<@2>" in (
        interaction.followup.messages[-1][0] or ""
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("decision", "expected_state"),
    [
        (None, "✖️ 確認逾時，未變更任何 role。"),
        (ScheduleRoleDecision.CANCEL, "✖️ 已取消，未變更任何 role。"),
        (
            ScheduleRoleDecision.PERMISSION_LOST,
            "⚠️ 權限已變更，未變更任何 role。",
        ),
    ],
)
async def test_assign_schedule_role_terminal_preview_decisions_do_not_revalidate(
    monkeypatch: pytest.MonkeyPatch,
    decision: ScheduleRoleDecision | None,
    expected_state: str,
) -> None:
    events: list[str] = []
    member = FakeMember(1, name="alice", display_name="Alice")
    role = CommandRole(90, [member])
    interaction = WorkflowInteraction(guild=SimpleNamespace(id=111, members=[member]))
    manager = SnapshotManager([role_source("Alice")], events)
    subject = make_role_subject(role_context(manager), events)

    class TerminalView:
        def __init__(self, **_kwargs: object) -> None:
            self.decision = decision

        async def wait(self) -> None:
            events.append("wait_for_decision")

    monkeypatch.setattr(
        "cogs.shift_register.AssignScheduleRoleConfirmView", TerminalView
    )
    await ShiftRegister.assign_schedule_role.callback(
        subject, interaction, role, "replace"
    )

    assert events == ["initial_final_read", "wait_for_decision"]
    assert member.calls == []
    assert interaction.control_message.edits[-1][0].endswith(expected_state)
    assert interaction.control_message.edits[-1][1] == {"view": None}


@pytest.mark.asyncio
async def test_assign_schedule_role_rejects_role_and_range_before_context_access() -> (
    None
):
    context_calls = 0
    member = FakeMember(1, name="alice", display_name="Alice")
    interaction = WorkflowInteraction(guild=SimpleNamespace(id=111, members=[member]))
    subject = ShiftRegister.__new__(ShiftRegister)
    subject.logger = SimpleNamespace(warning=lambda *_args, **_kwargs: None)

    async def unexpected_context(_source: object) -> None:
        nonlocal context_calls
        context_calls += 1

    subject._get_shift_finalization_context_or_none = unexpected_context  # type: ignore[method-assign]
    unassignable = CommandRole(90, [], assignable=False)
    await ShiftRegister.assign_schedule_role.callback(
        subject, interaction, unassignable
    )
    assert context_calls == 0
    assert "Bot 無法新增或清除" in (interaction.original_response_edits[-1][0] or "")

    assignable = CommandRole(90, [], assignable=True)
    await ShiftRegister.assign_schedule_role.callback(
        subject,
        interaction,
        assignable,
        "add_only",
        "A1",
    )
    assert context_calls == 0
    assert "Final Schedule Range 格式無效" in (
        interaction.original_response_edits[-1][0] or ""
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed_source",
    [
        FinalScheduleRoleSource(
            selected_range=parse_a1_range("C2:G12"),
            projected_values=(),
            labels=("Alice",),
        ),
        FinalScheduleRoleSource(
            selected_range=parse_a1_range("B2:G12"),
            projected_values=((2, 2, "Changed"),),
            labels=("Alice",),
        ),
        FinalScheduleRoleSource(
            selected_range=parse_a1_range("B2:G12"),
            projected_values=(),
            labels=("Bob",),
        ),
        FinalScheduleRoleSource(
            selected_range=parse_a1_range("B2:G12"),
            projected_values=(),
            labels=("Missing",),
        ),
    ],
    ids=["range", "projected-values", "resolved-identity", "unresolved-label"],
)
async def test_assign_schedule_role_aborts_when_final_snapshot_drifts(
    monkeypatch: pytest.MonkeyPatch,
    changed_source: FinalScheduleRoleSource,
) -> None:
    events: list[str] = []
    alice = FakeMember(1, name="alice", display_name="Alice")
    bob = FakeMember(2, name="bob", display_name="Bob")
    role = CommandRole(90, [])
    interaction = WorkflowInteraction(
        guild=SimpleNamespace(id=111, members=[alice, bob])
    )
    manager = SnapshotManager([role_source("Alice"), changed_source], events)
    subject = make_role_subject(role_context(manager), events)

    class ConfirmView:
        decision = ScheduleRoleDecision.CONFIRM

        def __init__(self, **_kwargs: object) -> None:
            pass

        async def wait(self) -> None:
            events.append("wait_for_decision")

    monkeypatch.setattr(
        "cogs.shift_register.AssignScheduleRoleConfirmView", ConfirmView
    )
    await ShiftRegister.assign_schedule_role.callback(
        subject, interaction, role, "replace"
    )

    assert events == [
        "initial_final_read",
        "wait_for_decision",
        "revalidation_final_read",
    ]
    assert alice.calls == []
    assert bob.calls == []
    assert interaction.control_message.edits[-1][0].endswith(
        "⚠️ Final Schedule 或 Discord 成員資料已變更，未變更任何 role；"
        "請重新執行 command。"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["sheet_url", "final_schedule_worksheet_id"])
async def test_assign_schedule_role_aborts_when_config_snapshot_drifts(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    events: list[str] = []
    member = FakeMember(1, name="alice", display_name="Alice")
    role = CommandRole(90, [])
    interaction = WorkflowInteraction(guild=SimpleNamespace(id=111, members=[member]))
    manager = SnapshotManager([role_source("Alice"), role_source("Alice")], events)
    first_context = role_context(manager)
    second_config = SimpleNamespace(
        sheet_url=(
            "https://changed.example"
            if field == "sheet_url"
            else first_context.feature_config.sheet_url
        ),
        final_schedule_worksheet_id=(
            99
            if field == "final_schedule_worksheet_id"
            else first_context.feature_config.final_schedule_worksheet_id
        ),
        final_schedule_anchor_cell="B2",
        recruitment_time_ranges=[{"start": 4, "end": 6}],
    )
    second_context = SimpleNamespace(manager=manager, feature_config=second_config)
    contexts = iter([first_context, second_context])
    subject = ShiftRegister.__new__(ShiftRegister)
    subject.logger = SimpleNamespace(warning=lambda *_args, **_kwargs: None)

    async def get_context(_source: object) -> SimpleNamespace:
        return next(contexts)

    subject._get_shift_finalization_context_or_none = get_context  # type: ignore[method-assign]
    subject.schedule_role_lock = RecordingRoleLock(events)

    class ConfirmView:
        decision = ScheduleRoleDecision.CONFIRM

        def __init__(self, **_kwargs: object) -> None:
            pass

        async def wait(self) -> None:
            events.append("wait_for_decision")

    monkeypatch.setattr(
        "cogs.shift_register.AssignScheduleRoleConfirmView", ConfirmView
    )
    await ShiftRegister.assign_schedule_role.callback(
        subject, interaction, role, "replace"
    )

    assert events == [
        "initial_final_read",
        "wait_for_decision",
        "revalidation_final_read",
    ]
    assert member.calls == []
    assert interaction.control_message.edits[-1][0].endswith(
        "⚠️ Final Schedule 或 Discord 成員資料已變更，未變更任何 role；"
        "請重新執行 command。"
    )


def test_schedule_role_preview_uses_mentions_and_preserves_all_warning_lines() -> None:
    role = FakeRole(90, "<@&90>")
    members = {member_id: FakeMember(member_id) for member_id in (1, 2, 3, 4, 5)}
    resolution = ScheduleRoleResolution(
        unique_member_ids=(1, 2),
        duplicate_groups=(DuplicateScheduleRoleGroup("Alice", (4, 5)),),
        unresolved_labels=("Missing",),
    )
    plan = ScheduleRolePlan(
        add_member_ids=(1,),
        already_member_ids=(2,),
        remove_member_ids=(3,),
    )

    content = _format_schedule_role_preview(
        role,
        resolution,
        plan,
        members,
        mode=ScheduleRoleUpdateMode.REPLACE,
    )

    assert content == "\n".join(
        [
            "### ‼️ role 更新確認",
            "將賦予 <@&90>：<@1>",
            "原本已有 <@&90>：<@2>",
            "將清除以下成員的 <@&90>：<@3>",
            "⚠️ 找不到對應的 Discord 成員：`Missing`",
            "重複的成員：",
            "- `Alice`：<@4>、<@5>",
            "",
            "若繼續，將清除班表外成員的 <@&90>。",
            "若略過，將清除未被其他班表名稱辨識的重複成員之 <@&90>。",
            "若其仍在 guild，所持有的 <@&90> 也會被清除。",
            "尚未變更任何 role，請確認後再繼續。",
        ]
    )


def test_schedule_role_add_only_duplicate_preview_has_no_clearing_copy() -> None:
    role = FakeRole(90, "<@&90>")
    members = {member_id: FakeMember(member_id) for member_id in (1, 4, 5)}
    resolution = ScheduleRoleResolution(
        unique_member_ids=(1,),
        duplicate_groups=(DuplicateScheduleRoleGroup("Alice", (4, 5)),),
        unresolved_labels=(),
    )

    content = _format_schedule_role_preview(
        role,
        resolution,
        ScheduleRolePlan((1,), (), ()),
        members,
        mode=ScheduleRoleUpdateMode.ADD_ONLY,
    )

    assert content.startswith("### ⚠️ role 更新確認\n")
    assert "清除" not in content
    assert "尚未變更任何 role，請確認後再繼續。" in content


def test_schedule_role_preview_mandatory_assignment_line_uses_none_marker() -> None:
    content = _format_schedule_role_preview(
        FakeRole(90, "<@&90>"),
        ScheduleRoleResolution((), (), ()),
        ScheduleRolePlan((), (), ()),
        {},
        mode=ScheduleRoleUpdateMode.ADD_ONLY,
    )

    assert "將賦予 <@&90>：なし" in content


def test_schedule_role_result_reports_successes_failures_and_duplicate_decision() -> (
    None
):
    role = FakeRole(90, "<@&90>")
    members = {member_id: FakeMember(member_id) for member_id in (1, 2, 4, 5)}
    resolution = ScheduleRoleResolution(
        unique_member_ids=(1,),
        duplicate_groups=(DuplicateScheduleRoleGroup("Alice", (4, 5)),),
        unresolved_labels=(),
    )
    plan = ScheduleRolePlan((1,), (2,), (2,))
    execution = ScheduleRoleExecutionResult((1,), (2,), (), ())

    content = _format_schedule_role_result(
        role,
        resolution,
        plan,
        execution,
        members,
        duplicate_decision=ScheduleRoleDecision.INCLUDE,
    )

    assert content.startswith("### ✅ role 更新結果\n")
    assert "已經賦予 <@&90>：<@1>" in content
    assert "原本已有 <@&90>：<@2>" in content
    assert "已清除以下成員的 <@&90>：<@2>" in content
    assert "已包含重複的成員：" in content


def test_schedule_role_result_marks_identity_and_api_failures() -> None:
    content = _format_schedule_role_result(
        FakeRole(90, "<@&90>"),
        ScheduleRoleResolution((), (), ("Missing",)),
        ScheduleRolePlan((1,), (), (2,)),
        ScheduleRoleExecutionResult((), (), (1,), (2,)),
        {1: FakeMember(1), 2: FakeMember(2)},
        duplicate_decision=ScheduleRoleDecision.SKIP,
    )

    assert content.startswith("### ⚠️ role 更新結果\n")
    assert "⚠️ 找不到對應的 Discord 成員：`Missing`" in content
    assert "⚠️ 無法賦予 <@&90>：<@1>" in content
    assert "⚠️ 無法清除 <@&90>：<@2>" in content
    assert "已略過重複的成員：" not in content


def test_replace_schedule_role_state_line_preserves_report_body() -> None:
    assert _replace_schedule_role_state_line("title\nbody\npending", "done") == (
        "title\nbody\ndone"
    )


@pytest.mark.asyncio
async def test_apply_schedule_role_plan_adds_then_removes_and_keeps_partial_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    role = FakeRole(90, "<@&90>")
    members = {member_id: FakeMember(member_id) for member_id in (1, 2, 3, 4, 5)}
    members[1].fail_add = True
    members[4].fail_remove = True
    monkeypatch.setattr(shift_register, "HTTPException", ExpectedHTTPError)

    result = await _apply_schedule_role_plan(
        role,
        ScheduleRolePlan(
            add_member_ids=(1, 2),
            already_member_ids=(3,),
            remove_member_ids=(4, 5),
        ),
        members,
    )

    assert result == ScheduleRoleExecutionResult(
        added_member_ids=(2,),
        removed_member_ids=(5,),
        add_failed_member_ids=(1,),
        remove_failed_member_ids=(4,),
    )
    assert members[3].calls == []
    assert [
        call for member_id in (1, 2, 4, 5) for call in members[member_id].calls
    ] == [
        ("add", 90, True),
        ("add", 90, True),
        ("remove", 90, True),
        ("remove", 90, True),
    ]
