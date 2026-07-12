# Shift Register Draft Generation Design

## Status

The base design, recruitment-time and Notes snapshot follow-up, and right-side
candidate, reverse-lookup, and Draft-formatting extension are implemented and
covered by automated validation. Live Discord and Google Sheets checks remain part
of the manual integration checklist. The outer-border and lookup-card visual
hierarchy refinement is also implemented and covered by automated validation.
The revised colors, directional borders, shifted lookup, and editable candidate
threshold are also implemented and covered by automated validation.

The pre-generation overwrite confirmation and bounded Draft cleanup are also
implemented and covered by automated validation.

## Goal

Improve `/shift_register generate_draft` so the generated Shift Draft uses the
configured Team Source for Encore eligibility and ISV-first scheduling, remains
usable when Team data is unavailable, preserves visually continuous assignments,
records dynamic workload notes below the draft, and provides live candidate and
participant lookup references beside it.

The design also establishes the canonical participant-name contract that a future
Draft-to-Final workflow can reuse before posting hourly Discord mention updates.

## Scope

This change includes:

- A required non-negative Encore Power threshold slash-command option.
- Purpose-specific Team Summary reads through the existing Team Source resolver.
- Encore eligibility based on configured Encore roles and the selected team's Power.
- ISV-first Encore, Honso, and standby scheduling.
- Cross-role continuity and same-column Honso placement.
- A deterministic no-ISV fallback when Team Source data is unavailable.
- Canonical Draft participant names that remain reversible through Shift Entry.
- Dynamic Notes below the Draft schedule.
- Live per-hour Honso, Encore, and unregistered candidate blocks.
- Exact canonical-name reverse lookup for Shift Entry and Team Summary data.
- Draft-body borders and visible non-recruitment gap-row formatting.
- One atomic Google Sheets batch update for the bot-owned Draft area.
- An administrator confirmation before Draft generation touches Google Sheets.

This change does not include:

- Scheduled or automatic Draft generation.
- Draft-to-Final generation.
- Hourly Discord handoff announcements or mentions.
- A hard consecutive-hours limit or weighted ISV/load scoring.
- Database schema, Shift Entry layout, Team settings, or Team Summary layout
  changes.

## Command Contract

The command accepts a required Discord-validated, non-negative float and retains
the optional runner:

```python
async def generate_draft(
    interaction: Interaction,
    encore_power_threshold: app_commands.Range[float, 0],
    runner: str | None = None,
) -> None:
```

Discord validates that the threshold is present, numeric, and non-negative before
invoking the command. The success response shows the threshold and configured
recruitment time immediately after the Runner line. Recruitment time reuses
`RecruitmentTimeRanges.announcement_display()` and the established announcement
copy:

```text
### ✅ 班表草稿已產生
- Runner（ランナー）：Run
- 安可綜合力閾值：35
- 募集時間【4-7・20-22】
‼️ 已將班表寫入 Shift Draft，並覆蓋原有內容。
```

Discord may display required slash options before optional options in the command
UI. The response ordering is independent and remains as specified above.

### Pre-generation Confirmation

After Discord validates the command and the bot confirms from database state that
Shift Register is enabled and configured, it displays an ephemeral confirmation
view before making any Google Sheets API request. Reading database-backed Shift
settings is allowed before the prompt; opening the spreadsheet, resolving Team
Source availability, ensuring worksheets, and reading or writing cells are not.

Let `R` be the final schedule row calculated from the configured continuous
earliest-to-latest recruitment axis, including the header row. The confirmation
lists only the new write destinations:

```text
‼️ Shift Draft の次の範囲を上書きします。
班表：A1:G31
Notes：A{R+2}
候補：I1、閾値 I{R+1}:K{R+1}
反查：J{R+3}:L{R+5}
編成一覧：Team Source が利用可能な場合は J{R+6} から書き込みます。
```

It also warns that existing cells in a Notes or candidate spill path are
preserved and may cause visible `#REF!`. The prompt does not list old signed
bot-owned blocks that regeneration may remove.

The Draft-specific view has a danger-style `確認生成` button and a secondary
`取消` button. Only the administrator who invoked the command may operate it,
and button callbacks re-check both `administrator` and `manage_channels`.
Cancellation, timeout, an unauthorized interaction, or lost permissions makes no
Google Sheets request and reports that Shift Draft was not changed.

The confirmation wait does not hold the channel's Sheet write lock, so normal
Shift message registration continues. On confirmation, the command reloads the
database settings before Sheets access. If the calculated destinations changed
while the prompt was open, generation stops and asks the administrator to rerun
the command. Otherwise the existing lock covers worksheet resolution, source
reads, scheduling, and the atomic Draft write. The generated schedule therefore
uses the latest Shift Entry values available after confirmation.

## Team Source Data Flow

Draft generation must call the existing
`ShiftRegisterManager.resolve_team_source()` helper. It must not use Shift Entry's
`Main ISV`, `Encore ISV`, or `Team Info` cells as scheduling authority.

When Team Source is available, a purpose-specific manager operation reads the Team
Summary and builds a mapping from Shift Entry username to a small immutable Draft
team profile. The profile contains only the values the scheduler needs:

- Main ISV.
- Main Power.
- Encore Team ISV, when present.
- Encore Team Power, when present.
- Whether Team Summary contains one or more configured Encore roles.

Shift availability remains represented by `Shift`. Team data stays in a separate
`username -> DraftTeamProfile` mapping passed to `ShiftScheduler`; Google Sheets,
database, and Discord objects do not enter the pure scheduler.

### Team Source Fallback

Team Source status controls fallback as follows:

| Status | Scheduling behavior | User-visible marker |
| --- | --- | --- |
| `AVAILABLE` | Use Team profiles and ISV scheduling. | None. |
| `UNSET` | Use the no-ISV fallback and leave Encore empty. | `⚠️` |
| `MISSING` | Use the no-ISV fallback and leave Encore empty. | `⚠️🛠️` |
| `AMBIGUOUS` | Use the no-ISV fallback and leave Encore empty. | `⚠️🛠️` |
| `INVALID` | Use the no-ISV fallback and leave Encore empty. | `⚠️🛠️` |
| `UNRESOLVED` | Use the no-ISV fallback and leave Encore empty. | `⚠️🛠️` |

A Team Source that lacks the headers required for Draft profiles is unavailable for
this operation even if it remains usable for narrower existing Team-reference
flows. A transient auxiliary Team Summary read failure also falls back rather than
blocking Draft generation. Shift Entry and Shift Draft failures retain the existing
storage-error path and do not report success.

An individual Shift participant without a usable Team Summary row or Main ISV is
treated as `No team yet`. They remain eligible for Honso or standby after every
candidate with a known Main ISV, but they cannot be Encore.

## Encore Eligibility And Effective Values

Encore eligibility is evaluated per participant:

| Encore role | Encore Team | Encore ISV | Power checked | Eligible |
| --- | --- | --- | --- | --- |
| Present | Present | Encore Team ISV | Encore Team Power | Yes, when Power is strictly greater than the threshold. |
| Present | Absent | Main ISV | Main Power | Yes, when Power is strictly greater than the threshold. |
| Absent | Present | Encore Team ISV | Encore Team Power | No. |
| Absent | Absent | Blank | Main Power is irrelevant. | No. |

Missing Encore Power, missing Encore ISV, or a Power equal to the threshold makes
the participant ineligible for Encore. Valid Main ISV data remains available for
Honso and standby selection.

## Hourly Scheduling

The runner is excluded from supporter positions. Each username can occupy at most
one supporter position per hour. Encore, Honso, and standby all count toward the
participant's accumulated scheduled hours.

### Encore Selection

Eligible, available Encore candidates are ordered by:

1. Higher effective Encore ISV.
2. Previous-hour position class:
   1. Encore.
   2. Honso or standby.
   3. Not scheduled.
3. Fewer accumulated scheduled hours.
4. Fewer total available hours across the recruitment range.
5. Username.

The first candidate is assigned Encore. If Team Source is unavailable or no
candidate is eligible, Encore remains empty.

### Honso And Standby Selection

After removing the selected Encore participant, remaining available candidates are
ordered by:

1. Known Main ISV before missing Main ISV; among known values, higher is better.
2. Scheduled in any supporter position during the previous hour before not
   scheduled.
3. Fewer accumulated scheduled hours.
4. Fewer total available hours across the recruitment range.
5. Username.

Select at most four candidates. When fewer than four are selected, fill Honso
columns first and standby last.

When four are selected, the participant with the lowest Main ISV is assigned
standby. Missing Main ISV ranks below every known Main ISV. If the lowest Main ISV
is tied, prefer the previous-hour standby participant; remaining ties use fewer
accumulated scheduled hours and then username.

### Column Continuity

Continuity is defined across roles rather than only within one slot:

| Current assignment decision | Previous-hour position priority |
| --- | --- |
| Encore selection | Encore, then Honso or standby, then not scheduled. |
| Honso/standby candidate selection | Any supporter position, then not scheduled. |
| Standby tie | Standby, then Honso or Encore, then not scheduled. |
| Honso column placement | Same Honso column, another Honso column, Encore or standby, then not scheduled. |

Continuity applies only to adjacent Draft hours. Normally empty rows between
configured recruitment ranges clear prior positions, while accumulated scheduled
hours remain global across the complete Draft.

After Encore and standby are fixed, participants selected for Honso retain the same
Honso column when possible. Remaining participants fill open Honso columns by Main
ISV and the existing deterministic tie-breakers. Moving the current hour's lowest
Main ISV participant to standby takes precedence over same-column continuity.

## Canonical Draft Names

Shift Entry remains the source of `username`, `display_name`, and
`original_message`. Draft cells normally show `display_name`. The reserved username
suffix is:

```python
r"⟨@([a-z0-9._]{2,32})⟩$"
```

For every participant:

```text
if display_name is duplicated in Shift Entry
or display_name already matches the reserved suffix:
    draft_name = display_name + " ⟨@" + username + "⟩"
else:
    draft_name = display_name
```

Modern Discord usernames are unique lowercase strings containing only `a-z`,
`0-9`, `_`, and `.`, with a length of 2-32 characters. They cannot contain the
suffix delimiters. Because raw display names that already end with the reserved
shape are extended again, the resulting canonical Draft names are unique without
an iterative collision loop.

Python owns canonical-name generation and parsing. Google Sheets reconstructs the
same complete canonical keys from Shift Entry and uses exact-key lookup; it must not
trust a suffix match alone. A manually entered name that does not exactly match a
canonical key is unresolved rather than guessed.

This contract is intended for future Draft-to-Final work. That workflow must resolve
the complete canonical Draft name to exactly one Shift Entry username before
looking up the current guild member and producing a Discord mention. Username or
display-name changes after Draft generation may require regenerating the Draft; a
persistent Discord user ID is intentionally outside this version.

## Shift Draft Layout

The current visible Draft remains:

```text
A     B          C       D      E      F      G
JST | ランナー | アンコ | 本走① | 本走② | 本走③ | 待機
```

Draft rows form one continuous hourly Y-axis from the earliest configured
recruitment-range start through the latest configured range end. For example,
`4-12, 20-28` renders every row from `4-5` through `27-28`, including the normally
empty `12-13` through `19-20` rows. Recruitment ranges still control which hours
participants may register; they do not remove rows from the Draft. Stale or manual
Shift Entry availability values outside the configured recruitment ranges are
ignored for scheduling.

The ownership boundary is:

- `A1:G31`: bot-owned Draft schedule value area.
- `A:H` below the schedule: bot-owned dynamic Notes spill.
- `I+`: shared candidate and reverse-lookup display region. The bot owns only the
  candidate anchor at `I1`, the signed candidate-threshold control below the Draft,
  and the exact lookup labels, input, status, and formula anchors derived from the
  old and new Draft row extents. Other user-entered values remain untouched.

The candidate formula is anchored at `I1` and spills through the final Draft hour
row. It emits three horizontal blocks with one blank separator column between
blocks and no trailing separator:

```text
本走候補（実効値：高→低） | [blank] | アンコ候補（実効値：高→低） | [blank] | 編成未登録
```

Each block is at least one column wide and expands only to the maximum candidate
count present in one of its JST rows. Every row lists only participants available
in that configured recruitment hour; continuous-axis gap rows stay empty even if
Shift Entry contains a stale or manual value outside the configured slots.
Participants already assigned in `C:G` remain listed, and one participant may
appear in both Honso and Encore. Runner is excluded from all three blocks.
Candidate cells contain only the complete canonical Draft name so a human can copy
the cell directly into `C:G`.

With an available Team Source, Honso candidates require Main ISV and sort by Main
ISV descending. Encore candidates require nonblank `encore_roles`, strict effective
Power greater than the editable Sheet threshold, and usable effective ISV. Encore Team
ISV and Power are effective when that team exists; otherwise Main values apply.
Encore sorts by effective ISV descending. Equal ISV values use Shift Entry row
order. `編成未登録` contains available participants without a matching Team Summary
row or usable Main ISV and also uses Shift Entry row order.

When Team Source is unavailable, the Honso header becomes `本走候補（登録順）` and
lists all available non-runner participants in Shift Entry row order. Encore and
unregistered blocks retain their headers but have blank participant rows. A
source-level failure must not label every participant unregistered. Later
`IMPORTRANGE` or formula failures remain visible instead of silently changing the
formula to fallback behavior; regeneration re-resolves Team Source status.

If `R` is the final schedule row, `I{R+1}:K{R+1}` contains
`アンコ候補閾値 | [editable numeric input] | 万総合力`. Generation seeds the
input with the slash command's required threshold, and the live candidate formula
references that cell rather than embedding the command value. Editing a number
recalculates Encore candidates immediately. Blank or nonnumeric input intentionally
produces a visible candidate-formula error instead of being treated as zero or
silently falling back to the command value. Regeneration replaces the input with
the newly supplied command threshold. This editable value changes only the live
`アンコ候補` block; the already generated left Draft schedule is not reassigned
until the command runs again.

After the blank row below the schedule, Notes still own `A:H`; column `I` separates
Notes from the reverse lookup. The lookup begins at `J` one row below the Notes
heading. If `R` is the final schedule row, the lookup layout is:

```text
J{R+3} 名前を貼り付け       | K{R+3} [manual canonical name] | L{R+3} [status]
J{R+4} シフト時間           | K{R+4} [formula]
J{R+5} シフト元メッセージ   | K{R+5} [formula]
J{R+6} 編成一覧
J{R+7} [Team Summary headers, horizontal spill]
J{R+8} [matching Team Summary row]
```

Lookup accepts only an exact canonical-name match. A nonblank unresolved input
leaves result cells blank and shows `⚠️ 参加者を特定できません` in `L{R+3}` without
repeating the adjacent input. `シフト時間` is reconstructed from current Shift
Entry hour cells as compact ranges such as `2-4・10-12`; `シフト元メッセージ`
preserves the stored message unchanged. With Team Source available, one two-row
spill returns its complete current header and matching row. A participant missing
from Team Summary receives the headers and a blank data row. Without Team Source,
Shift lookup still works but the Team Summary spill is not written.

The three-row lookup control has only a thin black top border over `J:L` and a thin
black left border down `J{R+3}:J{R+5}`. Its label cells in column `J` use
`#A4C2F4`. The manual input cell `K{R+3}` uses `#FFF2CC` plus a medium solid
`#FF0000` border on all four sides; result and status cells remain white.
`編成一覧` occupies the row
between the Shift fields and imported Team Summary so the following `username`,
`display_name`, `encore_roles`, and other source columns are visibly identified as
Team Summary data. When Team Source is available, the `編成一覧` row uses
`#A4C2F4` across the same fixed `J:L` width. It is omitted with the Team
Summary spill when Team Source is unavailable.

Candidate and lookup formulas import the exact Team Summary width resolved at
generation. Existing values remain live; Team Summary schema-width changes require
regeneration. The implementation relies on Google Sheets' native array spill to
expand into empty cells and add columns as needed; it does not pre-size the grid or
reserve speculative participant capacity. Writing an anchor formula does not clear
other cells. User-entered blockers in a spill path are intentionally preserved so
Sheets displays `#REF!` rather than silently deleting them. Regeneration replaces
`I1`, clears only a signed old threshold control and the exact old lookup
labels/input/formula anchors and their bot-owned formatting, and writes the new
controls. Cleanup covers both the
legacy Team Summary anchor directly below `シフト元メッセージ` and the new
`編成一覧` plus shifted Team Summary anchor. Removing an old array anchor lets
Sheets remove its calculated spill output while preserving unrelated user values.
A live API-generated spill beyond the current last column is part of manual
validation. Add `ensure_size()` only if that validation demonstrates that
API-written formulas do not receive the same native expansion as formulas entered
in the web UI.

The old lookup cells are treated as bot-owned only when the three expected labels
`名前を貼り付け`, `シフト時間`, and `シフト元メッセージ` appear at either the
legacy or shifted exact rows derived from the old Draft extent. The old candidate
threshold is bot-owned only when `アンコ候補閾値` appears at its exact row. Any
other label is preserved as unrelated user content. A movable old Notes anchor is
bot-owned only at the row derived from the old Draft extent and when its formula
contains the Rhoboto Shift Draft Notes ownership signature. Existing pre-signature
Rhoboto Notes formulas are recognized by their legacy formula structure for
migration. Other values at that position are preserved and may block the new spill
with visible `#REF!`.
A first pre-feature run or manually occupied `I+` area without the corresponding
signature is preserved and may visibly block the new output.

No threshold is persisted in the database and setup does not initialize these
formulas. An administrator may call generate with zero Shift Entry rows to seed the
editable Sheet threshold and initialize the formulas.

The left Draft body has a `#000000` thin solid outer border over dynamic range
`A1:G{R}` plus one bottom border under the header row `A1:G1`. It has no inner
body grid. Active recruitment rows use background `#FFFFFF`; visible min-max-axis
rows outside the configured recruitment slots use `#CCCCCC` only in `B:G`, leaving
the JST label in column `A` white. Rows are not hidden. Before overwriting,
generation reads `A1:A31` and defines the old body
as the consecutive valid JST labels beginning at `A2`. It clears only border and
background fields over the union of old and new body extents, then reapplies the
new body formatting. This prevents stale gray rows and borders after a shorter
regeneration without changing font, bold, alignment, column width, validation, or
cell notes.

Every successful Draft generation freezes exactly the leftmost column with
`gridProperties.frozenColumnCount = 1`, keeping JST visible during horizontal
scrolling. The field mask targets only `gridProperties.frozenColumnCount`, so any
existing frozen-row setting is preserved.

The candidate spill keeps no background fill. A thin black left border runs from
`I1` through the threshold-control row. One thin black bottom border is applied
across `I{R+1}:K{R+1}`. The label and `万総合力` suffix cells use `#A4C2F4`;
the middle input uses `#FFF2CC` plus the same medium solid four-sided `#FF0000`
border as the lookup input, applied after the black bottom border so red wins at
the shared edge. No border follows the candidate spill's dynamic right edge. The formula's
blank separator columns and explicit Japanese headings provide the remaining
grouping. Dynamic Notes keep no generated borders or fills because warning rows
can move the participant-table header after manual Draft edits.

## Dynamic Notes

After the final schedule row, leave one blank row and write one spill formula that
produces:

```text
メモ
募集時間【4-7・20-22】

名前 | シフト合計（h） | 最長連続（h） | アンコ（h） | 内部編成 | アンコ編成 | 編成状態 | 元メッセージ
Alice | 6 | 4 | 1 | 268/33.4 | 310/38.2 | | original message
Bob | 3 | 2 | 1 | 240/30 | | | original message
Carol | 2 | 1 | 0 | | | 未登録 | original message

名前の表示ルール：通常は表示名を使用します。同じ表示名がある場合や、表示名が「⟨@username⟩」形式で終わる場合は、末尾に実際のユーザー名が付きます。シフトを調整するときは、名前全体をコピーしてください。
編成欄の表示順：実効値/総合力
```

All bot-authored Notes labels, legends, fallback warnings, and unresolved-name
messages are Japanese. The current administrator-facing Discord report may reuse
the same Japanese fallback warning; broader localization remains deferred.

The formula reads the current manually adjusted `C:G` schedule and:

- Reconstructs complete canonical keys from Shift Entry username and display name.
- Shows the configured recruitment time using the established
  `募集時間【...】` copy and middle-dot-separated range formatter.
- Resolves every scheduled canonical name by exact match.
- Counts total scheduled hours across `C:G`.
- Computes the longest consecutive scheduled run.
- Counts Encore hours from column `C`.
- Looks up `original_message` by username.
- Imports the configured Team Summary once, locating username, ISV, and Power by
  the resolved header names rather than fixed source-column positions.
- Displays Main and Encore Team values as compact `ISV/Power` pairs under
  `内部編成` and `アンコ編成`; a missing single value is `—`, while a wholly
  absent pair is blank.
- Preserves the stored user-authored message unchanged under `元メッセージ`.
- Marks participants without a usable Main ISV as `未登録` only when Team Source
  is available.
- Shows the persistent Japanese Team Source fallback warning when applicable.
- Shows `⚠️ 参加者を特定できません` for a nonblank value that does not resolve
  exactly.

Participant lines are sorted for workload review, not alphabetically: total
scheduled hours descending, longest consecutive run descending, Encore hours
descending, then canonical name ascending as the deterministic tie-breaker. The
generation-time text attachment uses the same order.

Exact duplicate display-name detection and row/role counts use array-aware exact
comparisons such as `SUMPRODUCT(N(values = target))`; they must not use wildcard
matching or scalarized `SUM(N(...))` expressions inside `MAP`/`BYROW`. Error
handling stays at narrow optional-data boundaries. The complete Notes body must
not be wrapped in `IFERROR(..., "")`, because that would silently erase all
participant lines when one calculation fails.

Runner hours are excluded from Notes workload counts.
Normally empty rows between configured recruitment ranges break the longest
consecutive run.

The formula remains a single anchor-cell spill. Source and unresolved-participant
warnings appear after recruitment time, followed by one fixed blank row before the
table. A second fixed blank row separates participant rows from the canonical-name
and `実効値/総合力` legends.

## Atomic Draft Write

Draft generation extends the typed worksheet batch boundary so values, formulas,
exact old lookup cleanup, background cleanup, and borders use one underlying
`spreadsheets.batchUpdate` call with ordered subrequests:

1. Clear and replace only `A1:G31` with the raw header and schedule rows. This
   fixed value boundary removes stale schedule rows without clearing later rows
   in columns `A:G`.
2. Clear an old movable Notes anchor only when its expected position and Rhoboto
   formula signature both match. Removing the anchor removes its calculated
   `A:H` spill; column `H` is not cleared independently or without a row bound.
3. Clear only a signed old candidate-threshold control and the exact old
   reverse-lookup labels, pasted input, formula anchors, and bot-owned formatting
   calculated from the old Draft extent. Unrelated `I+` values remain untouched;
   a value blocking a new spill produces visible `#REF!`.
4. Clear old Draft background and all borders over the old/new body union, then
   apply the new white body, `B:G` gap backgrounds, thin outer border, and header
   separator. Apply the candidate-control, lookup-control, and `編成一覧`
   formatting at their new rows.
5. Write the command threshold, Notes, candidate, lookup-status, Shift-time,
   Shift-message, and optional Team Summary formulas to their exact anchor cells.
6. Set the Draft worksheet's frozen column count to `1` with an
   `updateSheetProperties` subrequest whose field mask is only
   `gridProperties.frozenColumnCount`.

The first range is not listed in `formula_ranges`, so user-derived display names
that start with `=` remain strings. Only exact formula anchors are formula-enabled.
Cell-value requests retain the `userEnteredValue` field mask; format requests name
only background and border fields, preserving unrelated cell properties.

All value, formula, clear, background, border, and frozen-column subrequests are
members of this same spreadsheet batch request. Google Sheets applies them
atomically. An invalid subrequest causes the entire batch to fail, so a successful
clear cannot be followed by a failed partial replacement.

## Discord Report

The existing per-hour Draft report remains, including shortages and unassigned
participants. It shows only configured recruitment slots, not the empty rows that
exist solely to keep the worksheet's min-max Y-axis continuous. It reports the
Encore Power threshold near Runner and places `募集時間【...】` immediately before
the assigned section. When Team Source is available, one `⚠️ 編成未登録：...` line
lists every Draft candidate without a usable Main ISV using the same Discord
mention/canonical-name formatter as assigned and unassigned participants. When
Team Source is unavailable, only the existing source warning is shown so every
candidate is not falsely labeled unregistered.

When Shift Entry contains no participants, the report keeps Runner, threshold,
overwrite notice, source warning, recruitment time, and attachment, but replaces
the repetitive per-hour all-shortage lines with
`- 已排入（安可｜本走；待機）：なし`. Normal per-hour output resumes when participants
exist.

The ephemeral reply also attaches UTF-8 `shift-draft-notes.txt`. The attachment is
a self-contained snapshot of the generation-time Notes inputs: the `メモ` heading,
recruitment time, applicable Team Source warning, the two legends, and every
participant's workload and Team values plus complete stored `original_message`
(including the existing ` ⏎  ` line-separator markers). Participant rows use a
labeled narrative format separated by `｜`; absent optional Team segments are
omitted instead of producing repeated empty separators, and a missing Main Team
is labeled `内部編成 未登録` so the status remains unambiguous. It
therefore intentionally repeats the short recruitment-time
and warning lines already visible in the reply. The reply states that the attached
snapshot represents the generation-time input data rather than a readback of the
calculated Sheet cells. It uses the same semantic content and participant ordering
as the initial Notes, but adapts the multi-column Sheet table for plain-text
readability and does not update after manual Sheet or Team Summary changes; the
Sheet spill formula remains the dynamic source of truth.

Build the attachment directly from the same generation inputs. Do not write the
formula and read its calculated value back from Google Sheets, which would add an
API call and a recalculation race.

The attachment explanation is the final reply line, after assigned and optional
unassigned sections.

Source warnings are non-blocking because a no-ISV Draft was still generated.
Shift Entry or Shift Draft storage failures remain blocking and use the existing
storage-error response.

## Affected Files

Expected application changes:

- `cogs/shift_register.py`
  - Add and describe the required range-validated threshold.
  - Pass the threshold into Draft generation.
  - Report the threshold and Team Source fallback status.
- `utils/shift_register_manager.py`
  - Reuse `resolve_team_source()`.
  - Read purpose-specific Team Summary profiles.
  - Fall back safely for unavailable auxiliary Team data.
  - Read the old Draft extent and write schedule, formulas, clears, and formats
    through one typed batch.
- `utils/shift_scheduler.py`
  - Add the Draft team profile boundary.
  - Implement eligibility, ISV ordering, cross-role continuity, standby selection,
    and canonical Draft names.
- `utils/shift_register_structs.py`
  - Render canonical Draft names.
  - Build the dynamic Notes formula and combined Draft write rows.
  - Build candidate and reverse-lookup formulas from shared canonical-name and Team
    Source metadata.
- `utils/google_sheets.py`
  - Support the narrow border-side selection required for outer borders and header
    separators in the existing typed batch request.

Expected automated-test changes:

- `tests/test_shift_scheduler.py`
- `tests/test_shift_draft.py`
- `tests/test_feature_channel_interactions.py`
- `tests/test_manager_fakes.py`
- `tests/test_google_sheets_adapter.py`
- Any shared fake requiring the new Team Summary Power columns or typed Draft batch.

Documentation changes:

- This design document.
- `docs/manual_integration_validation.md` during implementation.

No database migration or Shift Entry worksheet migration is required.

## Automated Test Contract

Focused tests must cover:

- Every Encore role/Encore Team combination.
- Strict Power threshold comparison, including equality.
- Main fallback for Encore ISV and Power.
- Encore, Honso, and standby cross-role continuity.
- Lowest Main ISV standby assignment and tied standby continuity.
- Same-column Honso placement.
- `No team yet` scheduling order and Japanese `未登録` Notes output.
- Every Team Source fallback status and marker.
- Unique, duplicate, and reserved-suffix canonical names.
- Exact canonical-name resolution and unknown manual values.
- Total, longest-consecutive, Encore-hour, compact Main/Encore `ISV/Power`, and
  original-message Notes values.
- Workload-first Notes and attachment ordering, including canonical-name ties.
- Exact duplicate canonical-name reconstruction and visible formula failures
  instead of a silently empty Notes body.
- Exact initial semantic parity between the Sheet Notes content and the UTF-8 text
  attachment, including the complete stored original messages.
- One atomic typed batch, bounded `A1:G31` clearing, signed old Notes-anchor
  cleanup, raw user-derived strings, exact old lookup-anchor cleanup, unrelated
  values outside owned cells, and narrowly scoped formatting.
- Confirmation destination calculation without Google Sheets access; confirm,
  cancel, timeout, wrong-user, lost-permission, and settings-change behavior.
- The confirmation wait does not hold the Sheet write lock used by Shift message
  registration.
- Discord report ordering, recruitment-time display, configured-slot filtering,
  existing shortage/unassigned behavior, and snapshot notice.
- Per-hour candidate availability, scheduled-person inclusion, runner exclusion,
  cross-block overlap, ISV ordering, row-order ties, and no-source fallback.
- Candidate spill padding, one-column minimum blocks, separator columns, native
  column expansion, and structural Team Summary regeneration boundaries.
- Exact reverse lookup, compact Shift ranges, preserved original message, unknown
  input warning, complete Team Summary row, and source-unavailable behavior.
- Dynamic `#000000` Draft outer/header borders, directional candidate/lookup
  borders, colored controls, `#FFFFFF`/`#CCCCCC` row fills, and
  shorter-regeneration cleanup.
- One atomic `updateSheetProperties` subrequest freezes column `A` without changing
  frozen rows.
- Zero-participant initialization output without repetitive hourly shortages.

## Manual Validation

Implementation must add corresponding cases to
`docs/manual_integration_validation.md`, including:

- Discord-native threshold validation, recruitment-time display, attachment, and
  report placement.
- Team Source available, unset, invalid, and temporarily unreadable behavior.
- Encore eligibility and strict threshold boundaries.
- Cross-role continuity and lowest-ISV standby placement.
- Manual Draft rearrangement followed by dynamic Japanese Notes recalculation,
  including longest-run resets across recruitment-range gaps.
- Confirming the attachment remains the generation-time snapshot after a manual
  Draft rearrangement while Sheet Notes update dynamically.
- Duplicate and reserved-suffix display names.
- Pre-generation destination display, confirm, cancel, timeout, wrong-user,
  permission-loss, and changed-settings behavior with no pre-confirmation Google
  Sheets request.
- Shorter regeneration clearing stale `A1:G31` values, removing only signed old
  Notes and lookup anchors, rebuilding them at new rows, and preserving unrelated
  values outside bot-owned cells.
- Candidate and reverse-lookup recalculation after Shift Entry and Team Summary
  value edits, including a new participant that expands beyond the prior last
  worksheet column.
- Exact-name copy from candidates into Draft and reverse lookup of the same value.
- Visible gray non-recruitment `B:G` cells, Draft outer/header borders,
  candidate/lookup controls, `編成一覧` styling, and no stale formatting after a
  shorter regeneration.
- Column `A` remains frozen after regeneration while any existing frozen-row count
  is unchanged.
- API-generated spill expansion beyond the worksheet's previous final column; add
  explicit resizing only if this live check fails.
- A user value in each spill path remains intact and produces visible `#REF!`
  rather than being deleted during regeneration.
- Google Sheets write-failure injection confirming atomic preservation of the old
  Draft.

## Future Compatibility

Draft-to-Final must validate every nonblank schedule cell against the canonical
name mapping before producing a Final schedule. Final-to-Discord handoff messages
must resolve the resulting username to exactly one current guild member and use the
member mention. Failure to resolve or ambiguity must be reported rather than
guessing.
