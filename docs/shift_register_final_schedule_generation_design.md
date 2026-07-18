# Shift Register Final Schedule Generation Design

## Status

Approved design for `/shift_register update_schedule_from_draft`. This document defines the
behavior and implementation contract. It does not authorize a database migration,
Git history operation, or compatibility alias.

## Goal

Generate a static, styled Final Schedule from the administrator-reviewed Shift
Draft while preserving the surrounding administrator-owned Final worksheet
template. The command follows the same permission, confirmation, locking, error,
and ephemeral-reply patterns as `generate_draft`.

## Scope

This feature adds:

- `/shift_register update_schedule_from_draft`;
- a strict optional override for the persisted Final table anchor;
- optional activity-date output from the saved Shift Timeline event date;
- deterministic Honso-column continuity optimization;
- split-shift detection and static formatting;
- Draft-compatible per-hour success output;
- explicit Final worksheet ownership and validation contracts; and
- synchronized design and manual-validation documentation.

It does not add a database field, change the worksheet column layout, change the
setup default anchor, resolve canonical names to Discord identities, or preserve
Draft formulas as formulas.

## Reused Contracts

The implementation reuses, and does not fork, these existing behaviors:

- the Shift command group's `administrator` and `manage_channels` defaults;
- requester-only destructive confirmation with callback permission recheck;
- `fresh_shift_channel_transaction` and worksheet-resource transactions;
- spreadsheet-scoped value batch reads with `FORMULA` rendering;
- `GoogleSheet` typed `spreadsheets.batchUpdate` request construction;
- worksheet links produced by `google_sheet_url_with_gid()`;
- canonical Draft cell labels as exact presentation strings;
- Draft's `安可｜本走；待機` per-hour report grammar; and
- UTF-16-aware, semantic Discord report splitting.

The current Draft-specific confirmation view and report splitter should receive
neutral Shift-generation names and be used by both commands. Do not retain a
Draft-named compatibility wrapper.

Reuse the existing safe-name formatter, worksheet-link pattern, cancellation and
timeout copy structure, storage-error routing, and per-hour
`安可｜本走；待機` renderer wherever behavior is identical. Extract the
smallest neutral Shift helper rather than copying Final-only variants or building
a generic UI/report framework.

## Command Surface

```text
/shift_register update_schedule_from_draft
    final_schedule_anchor_cell: optional string
    event_day_anchor_cell: optional string
    event_day_format: optional string, 1-512 characters
```

### `final_schedule_anchor_cell`

The anchor is the first Final data row and the Runner column. It is not a header
anchor.

- Omitted: use the saved `final_schedule_anchor_cell`.
- Supplied: normalize with NFKC, strip surrounding whitespace, uppercase, and
  strictly parse one A1 cell reference.
- Reject ranges, sheet-qualified references, absolute `$` references, row zero,
  values outside Google Sheets bounds, and values that cannot fit the existing DB
  field.
- Never redirect invalid input to `A1`.
- Persist a changed supplied anchor only after the Final Sheet batch succeeds.

The setup default remains `A1`. Changing that default, making the field nullable,
or requiring a first-generation anchor is a separate future migration.

### `event_day_anchor_cell`

This parameter is per-run and is never persisted.

It currently follows the project-wide eight-character A1 cell-reference contract,
including its known Google grid-boundary limitation. See
`docs/google_sheets_a1_cell_reference_contract.md`; do not widen this parameter
without the shared database and parser migration described there.

- Omitted: do not write an activity date.
- Invalid: skip the date write and disclose the reason in confirmation.
- Inside the main Final rectangle: skip the date write and disclose the overlap.
- Valid but outside the current grid: grow the grid in the same atomic Sheet
  batch before writing the value.

An invalid optional date target does not block main Final generation.

### `event_day_format`

This parameter is per-run and is never persisted.

The Discord option accepts 1-512 characters. Its option description includes the
default format so administrators can see it before submitting the command.

- Date anchor present and format omitted: use the default format.
- Date anchor omitted: ignore a supplied format and disclose that it is unused.
- Invalid format: skip the date write and disclose the reason.
- Missing saved `event_date`: skip the date write and disclose the reason.

The default is:

```text
{MM}月{DD}日 {dddd_ja} {dddd_en}, {MMMM_en} {DD}
```

Example:

```text
12月22日 月曜日 Monday, December 22
```

Literal text is rendered exactly as submitted. Within recognized ASCII `{...}`
syntax, only the token identifier is normalized with NFKC before matching this
allowlist. For example, `１２月・{ＭＭ}` renders as `１２月・12`; the natural-language
literal is not normalized.

| Category | Tokens |
| --- | --- |
| Year | `{YYYY}`, `{YY}` |
| Month number | `{M}`, `{MM}` |
| English month | `{MMM_en}`, `{MMMM_en}` |
| Day | `{D}`, `{DD}` |
| English ordinal day | `{Do_en}` |
| Japanese weekday | `{ddd_ja}`, `{dddd_ja}` |
| Traditional Chinese weekday | `{ddd_zh_tw}`, `{dddd_zh_tw}` |
| English weekday | `{ddd_en}`, `{dddd_en}` |
| Literal braces | `{{`, `}}` |

Unknown tokens and unmatched braces invalidate the whole optional date format.
Time, timezone, week, quarter, and Japanese-era tokens are out of scope.

## Pre-Read Confirmation

The command reads only DB-backed settings before confirmation. It makes no Google
API request before the administrator confirms.

The DB Timeline determines the expected source height and therefore permits exact
pre-read disclosure of:

- the linked Shift Draft source range, `B2:G{last row}`;
- the linked Final Schedule destination rectangle;
- the optional date cell and rendered preview;
- whether a supplied Final anchor will be saved after success;
- the need to back up Final Schedule;
- the fact that only the current rectangle is overwritten; and
- warnings for an omitted, invalid, unused, or overlapping optional date input.

The prompt warns that a shorter rerun never clears an old tail below the current
rectangle. Confirmation, cancel, timeout, requester checks, and live callback
permission checks match `generate_draft`.

After confirmation, the interaction enters the normal processing state. The
command obtains the Shift channel lock, refreshes configuration, and compares the
current confirmation fingerprint with the displayed one. A change to the Sheet
URL, Draft or Final worksheet ID, Timeline/recruitment axis, event date, saved
anchor, or calculated destinations aborts with no Google value read or mutation
and asks the administrator to rerun the command.

If metadata lookup finds a configured Draft or Final worksheet missing, reuse the
existing Shift worksheet repair machinery to create the missing worksheet and
save the replacement ID. Because this changes the confirmed link or destination,
stop immediately after repair: do not value-read Draft or generate Final. Reply
with `⚠️📏`, state that worksheet settings were repaired, and require the
administrator to rerun the command and confirm the new destination.

## Worksheet Read Contract

After confirmation and configuration revalidation, the manager obtains metadata
and locks the Draft and Final worksheet resources in deterministic order.

It issues one spreadsheet-scoped value batch read for the Draft worksheet. The
adapter returns the complete physical grid with `FORMULA` rendering. The manager
projects only Draft contract cells:

- row 1, columns `A:G`, for the exact Draft header;
- `A2:A{last row}`, for the JST slot axis; and
- `B2:G{last row}`, for Runner, Encore, Honso 1-3, and Standby values.

Physically returned cells in `H+` are not interpreted, validated, logged, cleared,
formatted, or written. Header and data are not fetched in separate calls, and no
worksheet-local reader or fallback is allowed.

Final worksheet values are never read. One narrow `spreadsheets.get` request
reads only the current destination Runner range's
`effectiveFormat.backgroundColorStyle` plus the spreadsheet theme colors needed
to resolve theme-backed fills to concrete RGB. Default-format cells contribute no
color, duplicate colors are collapsed, and cells outside the current Runner range
are not inspected. Worksheet metadata may also be used to plan required row or
column growth. A failed or malformed format read follows the normal classified
Sheets error path; it does not fall back to the old fixed palette.

## Draft Validation

Validation occurs before constructing any mutation request.

The Draft is invalid when:

- it is completely empty;
- row 1 does not match the current `A:G` Draft contract;
- any expected `A2:A{last row}` JST label is missing or different, or the next
  column-A cell continues with another recognized JST slot label;
- a nonblank role value is not a string; or
- one exact canonical label occupies more than one of Encore, Honso 1-3, and
  Standby in the same row.

Structural validation errors carry the expected and detected values plus the
cell coordinate when available. Discord renders those values in bounded,
mention-safe text so the administrator can repair Draft without exposing an
internal validation label.

Runner is excluded from the same-hour duplicate-role check. Duplicate conflicts
list every affected slot, exact canonical label, and occupied roles in compact
Draft-style rows:

```text
⚠️📏 Shift Draft 有同時段重複排入，Final Schedule 未生成。

- 衝突（同一人重複位置）：
  - -# `15-16`：`Name`（安可、本走 2）

Final 主範圍、活動日期與 DB anchor 均未變更。
```

Structural and conflict reports use the shared Discord splitter. They are not
classified as storage failures.

A structurally valid Draft with no role assignments is valid. Final generation
still writes the current blank six-column rectangle, styles the role cells, and
reports `已排入…：なし`.

## Static Snapshot Semantics

The Final table copies only Draft `B2:G{last row}`. It does not copy the Draft
header or JST labels.

All values are written as typed literal strings or blanks. Final does not follow
later Draft changes. Manually inserted Draft formulas are outside the supported
schedule contract; because reads use `FORMULA` rendering, any returned formula
text is written as literal text rather than recreated as a Final formula.

Canonical labels are compared, copied, colored, and reported by exact string
equality. This feature does not parse them or attempt Discord mentions. A future
shift-reminder feature will own canonical-name-to-identity resolution; do not add
a provisional parser or Legacy lookup path here.

## Final Table Transformation

Pure Final transformation logic lives in `utils/shift_final.py`. It has no Discord,
Google API, or database dependency.

### Honso Ordering

Only Honso 1-3 may be reordered. Runner, Encore, and Standby remain fixed.

For each recruitment row, enumerate the unique permutations of its three Honso
values, including blanks. There are at most `3! = 6` states. Dynamic programming
selects the globally minimum lexicographic total cost:

1. number of people present in adjacent considered rows who change Honso column;
2. total absolute Honso-column movement distance; and
3. number of nonblank people placed in a different Honso column from that row's
   original Draft position.

Stable permutation ordering is the final deterministic tie-break. New or departing
people have no transition cost.

Non-recruitment rows keep their Draft Honso order and are omitted from DP states.
Transitions compare the nearest recruitment rows on either side of a gray gap.
Thus semantic split detection remains strict while the rendered Honso columns
stay visually continuous across the gap.

### Split-Shift Detection

Split detection examines exact nonblank labels in Encore, Honso, and Standby.
Runner is excluded, and Honso 1-3 form one role category.

A person is split when successive appearances:

- change between Encore, Honso, and Standby;
- are separated by at least one unassigned Timeline row; or
- cross a non-recruitment row, which is always a hard semantic boundary.

Honso-column movement alone is not a role change. A split person receives one
background color across all of that person's role cells; a gray non-recruitment
background overrides the split background.

### Dynamic Split Palette

The complete split-person count `N` is known before colors are assigned. Generate
`N` equally spaced HSL hues using:

- starting hue `35°`;
- saturation `45%`; and
- lightness `86%`.

Order split people by first appearance in the transformed Final table. Assign the
starting hue first, then repeatedly choose the remaining hue that maximizes its
minimum circular distance from all already assigned hues. Candidate order breaks
ties.
This maximizes the minimum hue spacing for the known set and keeps early visual
neighbors distinct without a dependency or graph-coloring system.

When the current Final Runner range contains administrator-defined chromatic
backgrounds, treat their concrete RGB hues as already occupied. Keep the complete
equal-spaced split palette intact and test each whole-degree rotation, selecting
the rotation that maximizes the minimum circular hue distance between every split
color and every occupied Runner hue. The smallest rotation wins ties, so a run
with no chromatic Runner background preserves the existing palette exactly.
White, black, and low-saturation gray backgrounds do not reserve a hue. Runner
labels remain excluded from split detection.

Use Python's standard-library `colorsys`; do not add a color dependency.

## Final Formatting Ownership

The Final anchor is the first data row and Runner column. The bot writes exactly
the current six-column by Timeline-height rectangle and never clears below it.

The value write includes Runner, but no format request may target the Runner
column. Administrator-defined Runner background, foreground, border, alignment,
font, and number formatting remain untouched.

Within the five role columns only, reset the current rectangle's background to
`#FFFFFF` and text foreground to `#000000`, then apply:

1. split-person backgrounds;
2. non-recruitment background `#CCCCCC`, overriding split backgrounds; and
3. nonblank Encore text foreground `#FF0000`.

Only background and foreground fields are bot-owned. Borders, alignment, font
family/size, number formats, validation, notes, and other properties are not part
of the request. No conditional-format rule is created.

## Atomic Write And Anchor Persistence

One `spreadsheets.batchUpdate` atomically contains:

- the current six-column literal value rectangle, including blank clears;
- role-column background and foreground updates;
- an eligible optional date value; and
- required grid growth.

The persisted `final_schedule_anchor_cell` describes the location of the last
successfully generated Final, not a pending intent. The precise order is:

```text
hold Shift channel lock
→ refresh configuration
→ lock Draft and Final worksheet resources
→ batch-read Draft
→ read the exact Final Runner effective-background range
→ validate and build Final plan
→ atomic Sheets batchUpdate
→ save a changed explicit Final anchor
→ release locks
→ send the success report
```

An omitted anchor or a supplied value equal to the saved value causes no DB write.

- Sheets failure: no Final request is committed and the DB anchor is unchanged;
  use the existing external-storage error path.
- Sheets success followed by DB failure: do not attempt a destructive Sheet
  rollback. Report `⚠️🛠️` partial success, name the actual Final range, state that
  the DB anchor was not updated, and require a retry with the same explicit anchor.
- DB success: only then may the command report complete success.

No pending-anchor field, outbox, automatic Sheet rollback, read-back polling, or
extra Final value verification request is added.

## Success Report

The first section uses the Final worksheet deep link and reports:

- all nonblank Runner labels, deduplicated by first appearance, or `なし`;
- configured recruitment time ranges;
- the exact overwritten main rectangle;
- the date cell and rendered value, or the reason it was not written; and
- dynamic warnings, omitted as a section when none exist.

Assignments match Draft's existing grammar and include only recruitment slots:

```text
- 已排入（安可｜本走；待機）：
  - -# `15-16`：`Encore`｜`Honso 1`、`Honso 2`、缺 `1`；`Standby`
```

Encore and Standby empties render `缺`; Honso shortage renders `缺` followed by
a backticked number. When every
role cell is empty, render `- 已排入（安可｜本走；待機）：なし` and omit hourly
rows. Do not report unassigned or formation-unregistered people.

Names use the existing safe Markdown display formatter. Exact canonical labels
containing backticks use escaped plain text rather than inline code.

Generalize the existing Draft report splitter into one Shift report splitter.
Measure Discord content in UTF-16 code units and cap every message at 2,000. Keep
one message when it fits. On overflow, the first preferred boundary is immediately
before the assignment section; later boundaries are section starts, newlines, and
`、`, followed by the existing hard-line fallback.

## Error And Cancellation Results

- Invalid main anchor: invalid-input response before confirmation; no fallback.
- Cancel: `✖️`, remove controls, no change.
- Timeout: `✖️`, remove controls, no change.
- Permission loss in callback: existing settings-permission response, no change.
- Confirmation fingerprint drift: `⚠️`, ask the administrator to rerun, no Google
  value read or mutation.
- Missing configured Draft/Final worksheet: repair the worksheet/settings, then
  `⚠️📏` require a new command confirmation; do not read Draft values or generate
  Final in the repair attempt.
- Malformed Draft or conflict: `⚠️📏`, actionable expected-versus-detected or
  slot/person details, no mutation.
- Google failure: existing `⚠️🛠️` storage handling, DB unchanged.
- Post-Sheet DB failure: explicit `⚠️🛠️` partial success as defined above.
- Unexpected failure: existing `⚠️🚧` centralized path.

## Code Boundaries

- `cogs/shift_register.py`: command parameters, confirmation fingerprint, Discord
  orchestration, success/error formatting, and follow-up sending.
- `components/`: the smallest neutral shared Shift generation confirmation view;
  requester and callback permission enforcement.
- `utils/shift_final.py`: pure parsing, date rendering, Draft-to-Final validation,
  Honso DP, split detection, palette, and Final plan structures.
- `utils/shift_register_manager.py`: metadata resolution, worksheet transactions,
  spreadsheet batch read, projection, typed request assembly, Sheet execution,
  and anchor persistence result.
- `utils/google_sheets.py`: reuse existing batch-read and typed batch-write APIs;
  add no worksheet-local reader or Final-specific adapter abstraction.

Do not merge Final canonical strings into the username-based `ShiftScheduler` or
create a shared Draft/Final identity model. Reuse Draft's presentation helpers
where semantics match; do not generalize unrelated Draft scheduling behavior.

## Automated Validation

Add focused pure tests for:

- every date token, default output, braces, token-only NFKC input, literal-text
  fidelity, the 512-character command bound, and invalid formats;
- strict main/date A1 parsing and overlap handling;
- Draft header/JST/shape validation and exact `H+` exclusion;
- same-hour duplicate-role conflicts and compact reporting data;
- Honso DP continuity, distance, stable tie-break, blanks, and gap bridging;
- split detection across missing recruitment rows, non-recruitment rows, and role
  changes, while ignoring Honso-column movement and Runner;
- deterministic equal-hue/farthest-first color assignment; and
- valid empty schedules.

The implemented manager/adapter tests cover:

- one complete Draft value batch read, no Final value read, and one exact Final
  Runner effective-background read;
- one atomic typed batch containing values, selective format fields, optional date,
  and growth;
- literal leading-`=` values and formulas never re-created in Final;
- no Runner format fields and no writes outside the current rectangle/date cell;
- split-palette stability without Runner colors and deterministic rotation away
  from RGB and theme-backed Runner colors;
- date omission, invalidity, overlap, and grid growth;
- Sheets failure leaving DB unchanged; and
- post-Sheet DB failure returning explicit partial success.

The implemented interaction tests cover:

- command names, optional parameter types, and default permissions;
- zero Google API calls before confirmation;
- requester restriction, callback permission loss, cancel, and timeout;
- confirmation fingerprint drift;
- missing worksheet repair stopping before Draft value read and requiring a fresh
  confirmation;
- processing then actionable structural/conflict failure;
- Draft-compatible success output and empty handling; and
- semantic splitting with every UTF-16 chunk at or below 2,000 units.

Validation runs focused tests first, followed by the managed-sandbox Ruff checks,
full pytest with the repository coverage gate, compileall, and `git diff --check`.

## Manual Validation

`docs/manual_integration_validation.md` now includes scenarios covering:

- confirmation before every Google request;
- exact source/destination/date disclosure and Sheet links;
- permissions, cancellation, timeout, and configuration drift;
- Draft structure and duplicate-role errors with zero mutation;
- literal values, Honso continuity across gray gaps, Runner-aware split colors,
  Encore red text, and preserved Runner formatting;
- current-rectangle-only overwrite and a shorter rerun's retained old tail;
- default/custom/invalid/missing/overlapping date behavior;
- grid growth;
- empty assignments;
- Sheets failure and post-Sheet DB partial success; and
- long reports, with the assignment section as the first preferred split.

## Documentation Status

The implementation and documentation now:

- assign identity resolution in `docs/shift_register_draft_generation_design.md`
  to the future reminder flow, not this Final generation command;
- replace the global Final ownership language in
  `docs/manual_integration_validation.md` with per-command ownership;
- clarify that `generate_draft` excludes Final from its read plan while
  `update_schedule_from_draft` reads Draft values plus only the exact Final Runner
  effective-background range, never Final values; and
- mark Final Schedule generation complete in `docs/runtime_architecture_review.md`.

The Timeline migration's historical instruction to leave Final unchanged, Team
Source's feature-local Final exclusion, settings lifecycle design, and historical
implementation plans remain accurate in their own scope and should not be
rewritten.

## Compatibility And Rollout

- No DB migration or new dependency.
- Existing `final_schedule_worksheet_id`, `final_schedule_anchor_cell`, and
  `event_date` fields are reused.
- Existing setup default `A1` remains.
- No worksheet column or header migration.
- No command alias, fallback reader, or Legacy path.
- Administrators should first validate against a disposable Final template and
  back it up before production use.

## Out Of Scope

- canonical-name-to-Discord identity resolution and shift-reminder messages;
- automatic cleanup of stale Final rows below a shorter current rectangle;
- Final worksheet value read-back or reconciliation;
- formulas or conditional formatting in Final;
- persisting the event-day anchor or format;
- changing setup/settings UI or the `A1` default;
- localization expansion beyond the existing command/reply conventions; and
- generalized optimizer, palette dependency, pending-write state, or outbox.
