# Shift Register Schedule Role Assignment Design

## Status

Approved design for `/shift_register assign_schedule_role` and the narrow
post-deadline availability correction for Shift finalization commands. This
document defines behavior and implementation boundaries. It does not authorize a
database migration, Git history operation, push, or deployment.

## Goal

Assign one Discord role to the people in the current editable Final Schedule.
Administrators can either add scheduled members without disturbing existing role
members or make the target role match the schedule. Identity ambiguity remains
visible and confirmation-gated, destructive effects are stated explicitly, and
Discord API calls are limited to members whose target-role membership changes.

## Scope

This feature adds:

- `/shift_register assign_schedule_role`;
- one required Discord role option;
- an `add_only` default and authoritative `replace` choice;
- an optional bounded Final Schedule A1 range;
- canonical-label resolution against current guild members;
- duplicate-member confirmation;
- UTF-16-safe multi-message previews and results;
- minimal per-member Discord role reconciliation;
- post-deadline availability for the three Shift finalization commands; and
- setup and manual-validation documentation for Discord role assignment.

The post-deadline finalization commands are:

- `generate_draft`;
- `update_schedule_from_draft`; and
- `assign_schedule_role`.

No other Shift commands change their enabled-state behavior.

This feature does not change database schema, worksheet layout, worksheet values,
dependencies, public announcement templates, or role membership for roles other
than the selected target.

## Existing Contracts

The implementation reuses:

- the Shift command group's guild-only and
  `administrator + manage_channels` defaults;
- requester-only confirmation with live callback permission checks;
- soft-disable semantics that preserve the FeatureChannel and Shift settings;
- hard-clear semantics that delete those settings;
- the saved Final Schedule anchor and current recruitment ranges;
- `A1Cell`, `A1Rectangle`, and `parse_a1_cell()`;
- `_final_role_range()` for the default five supporter columns;
- spreadsheet-scoped full-grid value reads through `GoogleSheet`;
- worksheet resource transactions;
- `DRAFT_USERNAME_SUFFIX_PATTERN`;
- `KeyAsyncLock`;
- atomic `Member.add_roles()` and `Member.remove_roles()`; and
- `_split_shift_report()` with UTF-16 length accounting.

Do not introduce a second Google Sheets read adapter, message-length algorithm,
role service abstraction, or compatibility identity path.

## Command Surface

```text
/shift_register assign_schedule_role
    role: required Discord role
    role_update_mode: optional native choice, default add_only
    final_schedule_range: optional bounded A1 rectangle
```

### `role`

The selected Discord role is required and is rendered as a mention in every
preview and result.

Before any Google Sheets read, reject a role for which
`Role.is_assignable()` is false. This covers `@everyone`, managed roles, and
roles outside the bot's hierarchy. Prefix the invalid-input response with
`⚠️` and `config.CONFUSED_EMOJI`, then use one concise body:

```text
Bot 無法新增或清除 <role>，請確認 role 類型與角色順位。
```

Also require the bot's current guild member to have `Manage Roles` before any
Google Sheets read. `Role.is_assignable()` does not check that guild permission,
and both conditions are prerequisites for changing the selected role. Report a
missing permission separately from an invalid role:

```text
⚠️ Bot 缺少 Manage Roles 權限，未讀取 Final Schedule，也未變更任何 role。
```

Reject even when the eventual role plan might have been a no-op. The command
does not read Final Schedule merely to discover a no-op when the bot cannot
perform any required role mutation. Still handle Discord API failures per
member because the permission or role hierarchy may change after the preflight.

### `role_update_mode`

Use Discord native choices:

| Display name | Value | Behavior |
| --- | --- | --- |
| `只新增` | `add_only` | Add scheduled members who do not hold the role. Never remove it. |
| `完全取代（清除班表外成員）` | `replace` | Make target-role membership match the confirmed resolvable schedule set. |

`add_only` is the default.

### `final_schedule_range`

When omitted, derive the range from the current Final Schedule contract:

- start one column to the right of the saved Runner anchor;
- end at the current main rectangle's bottom-right cell; and
- cover Encore, Honso 1-3, and Standby for every row through the maximum current
  recruitment time.

Reuse `_final_role_range()`; do not duplicate its A1 formatting.

When supplied, accept any bounded colon-delimited rectangle such as
`B2:G12`. An explicit rectangle is not restricted to five columns.

Add one small bounded-range parser alongside the current Final A1 helpers:

1. require exactly one colon and two nonempty endpoints;
2. parse both endpoints with `parse_a1_cell()`;
3. preserve that helper's NFKC normalization, uppercase rendering, and Google
   grid bounds; and
4. reject a start row or column after its corresponding end.

Reject open-ended, sheet-qualified, absolute, multi-range, or reversed input:

```text
Final Schedule Range 格式無效，未變更任何 role。
```

Prefix this invalid-input response with `⚠️` and
`config.CONFUSED_EMOJI`; do not hard-code the configured custom emoji.

An explicit range does not require the saved anchor to calculate its geometry,
but the configured Final worksheet must still exist.

## Post-Deadline Availability

`FeatureChannel.is_enabled` means registration is open. It is not the
availability boundary for finalization.

Remove the enabled-feature app-command predicate from the three approved
finalization commands. Each command must instead:

1. resolve the channel's FeatureChannel with the existing
   `require_enabled=False` context path;
2. require the corresponding Shift Sheet configuration; and
3. use the existing missing-configuration response when either no longer
   exists.

Consequences:

- soft-disabled channel: finalization commands remain available;
- hard-cleared channel: finalization commands remain unavailable; and
- normal Shift submissions and all other guarded commands keep their current
  behavior.

Discord user permissions do not change. Button callbacks remain requester-only
and recheck live `administrator + manage_channels`.

## Final Schedule Read

The command reads only the configured Final worksheet. It does not read Shift
Entry, Draft, Team, or Team Summary.

Within the Final worksheet resource transaction:

1. issue one spreadsheet-scoped full-grid value read through `GoogleSheet`;
2. project only the selected rectangle in the manager;
3. scan cells in row-major order;
4. ignore physically empty cells; and
5. deduplicate repeated nonempty labels by exact string while preserving their
   first appearance.

Do not normalize, trim, case-fold, or partially match identity text. A nonempty
cell containing extra whitespace is a distinct unresolved label.

The command never writes to Google Sheets.

## Identity Resolution

Build current-guild indexes from `guild.members`. The existing Server Members
Intent remains required. Resolve every exact Final label through one of two
mutually exclusive canonical paths.

### Terminal username suffix

If the complete label ends with the reserved
`DRAFT_USERNAME_SUFFIX_PATTERN`:

1. extract only the terminal username;
2. find the current guild member whose Discord username exactly equals it;
3. resolve that member when found; and
4. otherwise mark the complete label unresolved.

Do not fall back to the display-name prefix when the username is absent.

Examples:

- `Alice ⟨@alice_1⟩` resolves only username `alice_1`.
- `Alice ⟨@old_name⟩` is unresolved when `old_name` is not in the guild.
- `Alice ⟨@other⟩ ⟨@alice_1⟩` reads only the final suffix.

### Unsuffixed canonical display name

If the label does not end with the reserved suffix:

1. compare the complete label only with current guild display names;
2. one exact match resolves;
3. multiple exact matches form one duplicate confirmation group; and
4. no exact match is unresolved.

Do not compare an unsuffixed label with Discord usernames. Raw manually entered
username support is outside this design.

This split matches `build_draft_display_names()`: duplicate display names and
display names that already resemble the reserved suffix receive a terminal
username suffix, while ordinary unique display names remain unsuffixed.

Resolved members and duplicate candidates are deduplicated by Discord user ID
before planning role changes. Duplicate candidate groups render their Final
label in safe inline-code/plain-text form and their members as mentions.

## Role Membership Plan

Let:

- `U` be members resolved uniquely from Final labels;
- `D` be the union of every duplicate group's candidate members;
- `T` be the final confirmed target-member set;
- `C` be the current members of the selected role;
- `A = T - C` be members requiring an add call;
- `K = T ∩ C` be scheduled members already holding the role; and
- `R = C - T` be members requiring a remove call in `replace`.

For `add_only`:

- add `A`;
- report `K`; and
- perform no removals.

For `replace`:

- add `A`;
- report `K`; and
- remove `R`.

Never clear every member and then re-add the schedule. Never call the Discord API
for `K`. Each member in `A` or `R` receives at most one atomic target-role
operation, and unrelated roles are untouched.

Without duplicate groups, `T = U`. When duplicate groups are included,
`T = U ∪ D`. When they are skipped, `T = U`. A member present in both
`U` and `D` therefore remains targeted even when duplicate groups are
skipped. This command does not provide per-group or per-member selection.

An unresolved label contributes no member to `T`. This allows `replace` to
complete when a scheduled person has left the guild, while the warning makes the
potential renamed-member case explicit.

### Empty schedule

- `add_only`: report no additions and leave the role unchanged.
- `replace`: preview every current role member for clearing; apply only after
  confirmation.

## Preview And Confirmation

### `add_only`

If every resolved label is unique, execute immediately and report unresolved
labels with the result. If any duplicate group exists, do not mutate any role
member before the duplicate decision.

Buttons:

- `包含重複成員並執行`;
- `略過重複成員並執行`; and
- `取消`.

### `replace`

Always show a no-mutation preview because the command may clear members.

Without duplicate groups:

- `確認清除並更新`;
- `取消`.

With duplicate groups:

- `包含重複成員並執行`;
- `略過重複成員並執行`; and
- `取消`.

The include/skip decision is also the destructive confirmation; do not add a
second confirmation screen.

The view accepts only the original command user. Every callback rechecks live
settings permissions. Cancellation, timeout, or requester permission loss
removes controls and makes no role change. Another user receives the existing
unauthorized response without consuming the requester's view.

## Snapshot Revalidation And Locking

The preview records:

- configured spreadsheet and Final worksheet identifiers;
- selected canonical A1 rectangle;
- exact projected cell values;
- selected role ID and update mode; and
- resolved, duplicate, and unresolved label groups.

After confirmation, reacquire the Final worksheet read transaction and compare
the current configuration, resolved rectangle, and projected values with the
preview snapshot. Rebuild current-guild identity indexes and rerun the two-path
resolver; compare resolved user IDs, duplicate groups, and unresolved labels
with the displayed preview. On any Sheet or identity drift:

```text
⚠️ Final Schedule 或 Discord 成員資料已變更，未變更任何 role；請重新執行 command。
```

Release the worksheet transaction before Discord role operations.

Reuse `KeyAsyncLock` with `(guild_id, role_id)` to serialize commands that
target the same role across Shift feature channels. After taking the role lock,
read the latest cached current role members, calculate the set difference once,
and perform the minimal operations.

Do not nest the role lock with worksheet locks. Unrelated roles remain
concurrent. External Discord administrators can still change role membership
outside the bot's process; the bot reports its own operation outcomes rather
than attempting a distributed lock.

## Screen A Copy

Roles and known members use Discord mentions. Unresolved Final labels use the
existing safe Markdown name formatter; labels containing backticks must not
break inline code.

The first status line is mandatory. Every other line is omitted only when its
category is empty.

### Preview

```text
### ‼️ role 更新確認
將賦予 <role>：<user>／なし
原本已有 <role>：<user>
將清除以下成員的 <role>：<user>
⚠️ 找不到對應的 Discord 成員：`user`
重複的成員：
- `name`：<user>、<user>

若繼續，將清除班表外成員的 <role>。
若略過，將清除未被其他班表名稱辨識的重複成員之 <role>。
尚未變更任何 role，請確認後再繼續。
```

Use `‼️` for destructive `replace` confirmation and `⚠️` for an
`add_only` duplicate confirmation. `add_only` omits all clearing copy.

The preview's assigned/already-held sets contain only uniquely resolved members.
The preview clear line contains removals that are unconditional regardless of
the duplicate decision. Current role members belonging to duplicate groups are
covered by the explicit skip warning and appear in the actual result after the
decision. A duplicate candidate already present in the unique target set is
retained under either decision.

For unresolved labels in `replace`, add:

```text
若其仍在 guild，所持有的 <role> 也會被清除。
```

### Result

```text
### ✅ role 更新結果
已經賦予 <role>：<user>／なし
原本已有 <role>：<user>
已清除以下成員的 <role>：<user>
⚠️ 找不到對應的 Discord 成員：`user`
⚠️ 無法賦予 <role>：<user>
⚠️ 無法清除 <role>：<user>
已包含／已略過重複的成員：
- `name`：<user>、<user>
```

Use `✅` when no identity or Discord API error exists. Use `⚠️` when either
category exists. Successful add/remove lines list only calls that succeeded.
`原本已有` contains only final target members who held the role when the
execution diff was calculated, not unrelated current role members.

## Discord Message Delivery

Reuse `_split_shift_report()` for previews and results:

- measure the 2,000-unit limit in UTF-16;
- keep one message when the report fits;
- place the title and mandatory first line in the first chunk when they fit;
- prefer complete line boundaries afterward;
- split an oversized mention line at `、`; and
- use the existing hard fallback only when one segment itself cannot fit.

Attach confirmation controls to the final preview chunk so the buttons appear
below the complete preview.

On confirmation, edit the final preview chunk to remove controls and show the
processing state, then send the complete result as new chunks. Preserve the old
`role 更新確認` title so it cannot be mistaken for the later
`role 更新結果`. Cancellation, timeout, and permission loss likewise remove
the controls and state that no role was changed.

Do not add an attachment or a second message splitter.

## Discord API Failure Semantics

Use atomic `Member.add_roles(role)` and `Member.remove_roles(role)`.
Discord exposes target-role membership changes per guild member, so the set
difference is the minimum request count.

One member's failure does not stop independent remaining members. Collect add
and remove outcomes separately and do not roll back successes. Rollback would
add calls, can fail independently, and could overwrite a concurrent external
administrator action.

Expected API failures are rendered through:

- `⚠️ 無法賦予 <role>：...`; and
- `⚠️ 無法清除 <role>：...`.

Do not expose raw Discord exception bodies in user-facing content. Log bounded
operation context without schedule contents or private identifiers beyond the
existing logging contract.

## Fatal Error Boundaries

The following stop before role mutation:

- invalid explicit A1 range;
- unassignable selected role;
- bot missing `Manage Roles`;
- missing FeatureChannel or Shift Sheet configuration;
- missing Final worksheet;
- Google Sheets read/structure failure;
- cancellation or timeout;
- requester permission loss; and
- post-preview configuration, range, value, or guild-identity drift.

Route storage failures through existing centralized storage-error helpers.

Identity resolution failures are not fatal. Duplicate identities require a
decision. Discord member-role API failures are partial-operation results.

## Pure Logic Boundary

Add one small `utils/shift_schedule_role.py` module for:

- exact two-path current-guild identity resolution;
- duplicate and unresolved grouping;
- member-ID deduplication;
- target-set construction after the duplicate decision; and
- `add_only` / `replace` set-difference planning.

The module may depend on the smallest member attributes required for resolution,
but it must not call Discord, Google Sheets, or the database. Do not add a
service class, protocol hierarchy, factory, or extensible role-policy framework.

The cog owns Discord interactions, views, mentions, role API calls, and report
delivery. The manager owns Final worksheet reads and projection. The UI module
owns buttons and requester/live-permission enforcement.

## Affected Files

| File | Change |
| --- | --- |
| `utils/shift_schedule_role.py` | New focused identity and reconciliation logic |
| `utils/shift_final.py` | Strict bounded A1 rectangle parser composed from existing helpers |
| `utils/shift_register_manager.py` | Locked Final read and selected-range projection |
| `cogs/shift_register.py` | Command, choices, finalization availability, orchestration, role lock, copy, and delivery |
| `components/ui_shift_register.py` | Confirm/include/skip/cancel view behavior |
| `tests/test_shift_schedule_role.py` | Pure identity and set-difference coverage |
| `tests/test_shift_final.py` | Bounded-range parser coverage |
| `tests/test_shift_schedule_role_cog.py` | New command, snapshot, role calls, reports, and delivery |
| `tests/test_shift_final_cog.py` | Disabled-state availability for `update_schedule_from_draft` |
| `tests/test_feature_channel_interactions.py` | Disabled-state availability for `generate_draft` and shared splitter regression |
| `tests/test_ui_permissions.py` | Requester, permission-loss, cancel, and duplicate-choice callbacks |
| `tests/test_manager_fakes.py` | Final read/projection and call-shape coverage |
| `docs/project_setup.md` | Activate the Manage Roles invite variant and correct the existing manual-grant typo |
| `docs/manual_integration_validation.md` | Post-deadline and Discord/Sheets validation matrix |

Keep the test split aligned with current ownership; do not move unrelated tests
or perform a formatting sweep.

## Automated Validation

Add focused tests for:

- valid, NFKC-equivalent, bounded, reversed, open-ended, and invalid A1 ranges;
- omitted default range and unrestricted explicit rectangle behavior;
- exact terminal-suffix username resolution;
- missing suffix username without display fallback;
- unique, duplicate, and missing unsuffixed display names;
- repeated Final labels and member-ID deduplication;
- a duplicate candidate already resolved uniquely by another Final label;
- `add_only` as the native default;
- both native choice values and display labels;
- add-only, replacement, include, skip, no-op, and empty-target set differences;
- unresolved labels in both modes;
- minimal role calls and preservation of unrelated roles;
- missing `Manage Roles` rejected before context or Google Sheets access;
- individual add/remove failures without rollback;
- soft-disabled availability and hard-cleared rejection for all three
  finalization commands;
- requester-only callbacks and live permission loss;
- cancel, timeout, Sheet drift, and guild-identity drift with zero role
  mutations;
- same-role lock serialization;
- every Screen A conditional line and title state;
- role/member mentions versus safe unresolved labels; and
- every UTF-16 chunk at or below 2,000 units, including long mention lines and
  controls on the final preview chunk.

Run focused tests first, then the repository's non-mutating Ruff checks and full
pytest/coverage command using the managed-sandbox cache forms from
`docs/agent_harness.md`.

## Manual Discord And Google Sheets Validation

In a development guild:

1. soft-disable Shift Register and confirm all three finalization commands work;
2. hard-clear it and confirm all three reject missing configuration;
3. test the derived default and several explicit rectangles;
4. test `add_only`, `replace`, a no-op, and an empty schedule;
5. test unique suffix, unique display name, duplicate display names, and a
   member who left the guild;
6. test include, skip, cancel, timeout, another user, and requester permission
   loss;
7. edit Final, rename a resolved member, and add/remove a duplicate candidate
   while separate previews are open; confirm each drift causes no role mutation;
8. test `@everyone`, a managed role, and a role above the bot;
9. remove `Manage Roles` and confirm the command stops before any Final
   Schedule request or role call;
10. verify unrelated roles are unchanged;
11. run concurrent requests against the same and different roles;
12. create enough mentions to require several Discord messages and verify the
    view is attached only to the final preview chunk; and
13. inspect request logging to confirm one Final read for immediate add-only,
    one validation re-read after a preview, and one role endpoint call per
    changed member.

## Risks And Mitigations

- **Member left or username changed:** suffix lookup becomes unresolved; report
  it and never fall back to another display name.
- **Display name becomes duplicated:** require explicit include/skip instead of
  choosing one member.
- **Destructive empty replacement:** always preview the complete clear set.
- **Final edited during confirmation:** compare the exact snapshot and abort.
- **Guild identity changes during confirmation:** rerun resolution, compare
  resolved IDs/groups, and abort before role mutation.
- **Concurrent bot requests:** serialize by guild and target role, then calculate
  from current role members.
- **External administrator race:** use atomic single-role endpoints and report
  outcomes; do not attempt rollback or distributed locking.
- **Missing Manage Roles:** fail before Final Schedule access; retain native API
  failure reporting for permission drift after the preflight.
- **Large mention output:** reuse UTF-16 line-aware splitting and put controls on
  the last preview chunk.
- **Partial Discord failure:** retain successful operations and report add/remove
  failures separately.

## Explicit Exclusions

- No Tortoise model or migration.
- No worksheet layout, formatting, formula, or value write.
- No Shift Entry identity read.
- No persistent Discord user ID migration.
- No raw-username fallback for unsuffixed Final labels.
- No fuzzy, normalized, partial, or cross-path identity matching.
- No per-duplicate-member selection UI.
- No role changes beyond the selected target.
- No cross-process or external-administrator lock.
- No dependency change.
- No localization-template expansion for these administrator-only ephemeral
  messages.
- No behavior change for Shift settings or announcement commands.
- No modification to unrelated in-progress deadline design/template work.

## Implementation Order

1. Add and test the bounded A1 parser and pure role-plan logic.
2. Add and test the locked Final read/projection manager boundary.
3. Add and test the role confirmation view and Screen A formatting/delivery.
4. Add the command, role execution lock, snapshot revalidation, and partial
   result handling.
5. Correct the enabled-state guard for the three finalization commands.
6. Update setup and manual-validation documentation.
7. Run focused and full non-mutating validation.

Implementation remains blocked until this written specification is reviewed and
an approved file-level implementation plan and Rhoboto execution mode are
selected.
