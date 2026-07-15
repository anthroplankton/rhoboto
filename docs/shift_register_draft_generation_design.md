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

The live Team Summary refresh follow-up is implemented in the working tree. It
replaces the existing Summary-grid profile read with one shared
derivation from current Team worksheets and Discord members, refreshes Team
Summary without reading it back, and then generates Draft from the same in-memory
result. It also replaces automatic obsolete-Summary row deletion with the archived
row contract below.

The LLM scheduling-prompt attachment extension below is implemented in the
working tree and covered by the complete automated validation suite. Live Discord,
external LLM, and Google Sheets paste checks remain part of the manual integration
checklist. It preserves every existing Draft, worksheet, and scheduler contract
described in this document.

The non-overwriting current-Draft prompt command below is implemented in the
working tree and covered by the complete automated validation suite. Live Discord,
Google Sheets, and external-LLM checks remain in the manual integration checklist.
It adds one Summary-refresh and prompt-generation path without changing the
existing `generate_draft` behavior or writing Shift Draft.

## Goal

Improve `/shift_register generate_draft` so the generated Shift Draft uses the
configured Team Source and current Discord member state for Encore eligibility and
ISV-first scheduling, refreshes the derived Team Summary before writing formulas
that depend on it, remains usable when Team data is unavailable, preserves visually
continuous assignments, records dynamic workload notes below the draft, and
provides live candidate and participant lookup references beside it.

The design also establishes the canonical participant-name contract that a future
Draft-to-Final workflow can reuse before posting hourly Discord mention updates.

## Scope

This change includes:

- A required non-negative Encore Power threshold slash-command option.
- One shared Team Summary derivation from all configured Team worksheets, current
  Discord members, and configured Encore role IDs.
- One complete Team Summary refresh followed by direct Draft generation from the
  same in-memory result, with no Summary value read-back.
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
- Archived Summary rows that preserve administrator-owned cells during automatic
  full reconciliation.

This change does not include:

- Scheduled or automatic Draft generation.
- Draft-to-Final generation.
- Hourly Discord handoff announcements or mentions.
- A hard consecutive-hours limit or weighted ISV/load scoring.
- Database schema, Shift Entry layout, Team settings, or Team Summary layout
  changes.

## Command Contract

The command accepts a required Discord-validated, non-negative float and an
optional Discord user for the runner:

```python
async def generate_draft(
    interaction: Interaction,
    encore_power_threshold: app_commands.Range[float, 0],
    runner: User | None = None,
) -> None:
```

Discord validates that the threshold is present, numeric, and non-negative before
invoking the command and resolves the runner through the native user picker. The
runner is not a Shift Entry participant: the selected Discord identity supplies
the username used for internal exclusion and the display name used to build the
human-readable canonical Draft name. Although the runner is not a candidate, its
identity participates in the same duplicate-name and reserved-suffix checks as
Shift Entry identities. The canonical name is written to the Runner column for
every configured recruitment slot and shown in the success response;
non-recruitment rows keep the Runner column blank. The command and generation
path do not accept a free-form runner string or retain a legacy fallback.

The success response shows the threshold after the Runner line. Recruitment time
reuses
`RecruitmentTimeRanges.announcement_display()` and the established announcement
copy:

```text
### ✅ 班表草稿已產生
- Runner（ランナー）：Run
- 安可綜合力閾值：35
🔄 🔄 已同步 [Team Summary](summary worksheet URL)
‼️ 已將班表寫入 Shift Draft，並覆蓋原有內容。
- 募集時間【4-7・20-22】
- 已排入（安可｜本走；待機）：
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
‼️ 確認產生班表草稿
請先備份需要保留的內容。確認後將覆蓋 [Shift Draft](draft worksheet URL) 的以下位置：
班表：A1:G31
Notes：A{R+2}
候補：I1、閾値 I{R+1}:M{R+1}
反查：J{R+3}:L{R+5}
編成一覧：Team Source が利用可能な場合は J{R+6} から書き込みます。

Team Source 同步：
- 確認後會以目前 Discord 成員與 Team 資料更新 [Team Summary](saved summary worksheet URL)。
```

It also warns that existing cells in a Notes or candidate spill path are
preserved and may cause visible `#REF!`. The prompt does not list old signed
bot-owned blocks that regeneration may remove.

The Draft-specific view has a danger-style `確認生成` button and a secondary
`取消` button. Only the administrator who invoked the command may operate it,
and button callbacks re-check both `administrator` and `manage_channels`.
Both the confirmation heading and confirmed-processing message link the visible
`Shift Draft` text directly to the configured Draft worksheet.
Cancellation, timeout, an unauthorized interaction, or lost permissions makes no
Google Sheets request and reports that Shift Draft was not changed.

The Summary destination link is composed from saved database configuration without
Google Sheets access. An unset source says it will not synchronize; a selected but
missing Team configuration says the setting is invalid. The confirmation wait does
not hold the channel's Sheet write lock, so normal
Shift message registration continues. On confirmation, the command reloads the
database settings before Sheets access. If the calculated destinations changed
while the prompt was open, generation stops and asks the administrator to rerun
the command. Otherwise the existing Shift channel lock plus the
worksheet-resource locks cover worksheet resolution, source reads, Summary
reconciliation, scheduling, and the Draft write. The generated schedule therefore
uses the latest Shift Entry, Team worksheet, and Discord member values available
after confirmation.

## Team Source Data Flow

Draft generation resolves Team Source metadata without a preliminary value read.
After acquiring the Entry, Draft, every configured Team worksheet, and Summary
resource lock, it issues one values batch per spreadsheet. If the Shift and Team
worksheets share a spreadsheet, one spreadsheet-scoped batch contains all of them.
It must not use Shift Entry's `Main ISV`, `Encore ISV`, or `Team Info` cells as
scheduling authority.

When Team Source is available, one shared pure derivation consumes the current
bot-owned Team worksheet rows, the current Discord members keyed by username, and
the configured Encore role IDs. It produces the complete active Summary values
once. Full Team Summary refresh and Draft generation both call this derivation;
neither reimplements display-name, role, Team-title, ISV, Power, or
`original_message` composition.

The old Summary grid is used only to validate and migrate its bot-owned header,
resolve active, archived, and reusable physical rows, and plan writes while
preserving administrator-owned cells. Old Summary display names, roles, and Team
values are not derivation inputs. Administrator columns after the unique terminal
`original_message` may be transported by the API but are not interpreted,
validated, cleared, or written during automatic reconciliation.

For each username present in at least one configured Team worksheet, a matching
current Discord member supplies `display_name` and configured Encore roles. Without
a current member match, the first Team row supplies `display_name` and Encore roles
are empty; old Summary values never fill either field. The first configured Team
worksheet supplies Main ISV/Power, the second supplies optional Encore ISV/Power,
and later worksheets remain Backup data for complete Summary presentation only.

The Summary and Draft writes are both planned before write I/O and grouped by
spreadsheet. When Team Summary and Shift Draft share one spreadsheet, one
underlying `spreadsheets.batchUpdate` contains ordered Summary subrequests followed
by Draft subrequests, so either both apply or neither applies. When they are in
different spreadsheets, the complete Summary batch is sent first and the Draft
batch second. Draft profiles are projected directly from the same derived rows;
Draft never reads the newly written Summary values back. The profile contains only
the values the scheduler needs:

- Main ISV.
- Main Power.
- Encore Team ISV, when present.
- Encore Team Power, when present.
- Whether the current Discord member has one or more configured Encore roles.

Shift availability remains represented by `Shift`. Team data stays in a separate
`username -> DraftTeamProfile` mapping passed to `ShiftScheduler`; Google Sheets,
database, and Discord objects do not enter the pure scheduler.

The generated Sheet formulas remain Summary-backed because they must stay live
after generation. Candidate and dynamic Notes formulas consume username, roles,
Main ISV/Power, and optional Encore ISV/Power from the refreshed Summary. The
reverse lookup consumes Shift Entry identity, availability, and original message;
its `編成一覧` spill imports the refreshed complete Summary row, including Backup
Team pairs. Backup Teams do not affect Python scheduling or candidate ranking.

| Data | Python authority | Live Draft formula authority |
| --- | --- | --- |
| Username, Draft name, availability, Shift message | Shift Entry | Shift Entry |
| Main ISV/Power | First Team worksheet through the shared derivation | Refreshed Summary Main pair |
| Encore ISV/Power | Second Team worksheet through the shared derivation, otherwise Main | Refreshed Summary Encore pair, otherwise Main |
| Encore roles | Current Discord member and configured role IDs | Refreshed Summary `encore_roles` |
| Backup Team pairs | Shared derivation, excluded from scheduling | Refreshed complete Summary row in `編成一覧` |
| Recruitment ranges | Shift database config | Generation-time formula constants |
| Runner | Discord user command option; username for identity deduplication and canonical display name for output | Live Draft Runner column for canonical-name collision checks and per-row candidate exclusion |
| Encore threshold | Command option for Python scheduling | Editable Draft threshold cell for live candidates |

### Archived Summary Rows

Automatic full reconciliation must not delete an obsolete Summary row because a
physical row deletion would also delete administrator-owned cells after
`original_message`. Instead, it changes only that row's `username` cell:

```text
<username> (archived)
```

Display name, roles, every Team value, `original_message`, administrator-owned
cells, and row properties remain unchanged. Discord usernames cannot contain `:`,
so the reserved prefix cannot collide with an active username. Summary indexing
classifies rows before planning mutations:

- An active row has a nonblank username without the reserved suffix.
- An archived row has one valid identity encoded as `<username> (archived)`.
- A reusable row has a completely blank bot-owned band.
- A blank-username row with other bot-owned content is occupied manual content and
  is preserved but not reused.
- The empty value ` (archived)`, a nested suffix, duplicate active username,
  duplicate archived username, or simultaneous active and archived row for one username is a
  worksheet contract error.

Both single-user upsert and full reconciliation resolve a desired username in the
same order: active row, matching archived row, reusable row, then appended row. A
returning username therefore restores its archived physical row and overwrites the
bot-owned band, including `original_message`, with current derived values; its
administrator-owned cells remain attached. Archived rows are excluded from active
Summary records. Draft formulas look up exact active usernames, so the reserved
username cannot match an active Shift participant. Archived rows are never
reassigned to another username.

Explicit confirmed Team deletion retains its existing complete-row deletion
contract. Archiving applies only to automatic full Summary reconciliation. A
permanently archived row remains as a tombstone unless an administrator explicitly
removes it; automatic compaction is forbidden because it would again move or delete
administrator-owned cells.

### Team Source Fallback

Team Source status controls fallback as follows:

| Status | Scheduling behavior | User-visible marker |
| --- | --- | --- |
| `AVAILABLE` | Refresh Summary, then use the same derived profiles for ISV scheduling. | None. |
| `UNSET` | Use the no-ISV fallback and leave Encore empty. | `⚠️` |
| `MISSING` | Use the no-ISV fallback and leave Encore empty. | `⚠️🛠️` |
| `AMBIGUOUS` | Use the no-ISV fallback and leave Encore empty. | `⚠️🛠️` |
| `INVALID` | Use the no-ISV fallback and leave Encore empty. | `⚠️🛠️` |
| `UNRESOLVED` | Use the no-ISV fallback and leave Encore empty. | `⚠️🛠️` |

A source that cannot be resolved or read remains non-blocking under the status
table, and no Summary write is attempted. Once the source grids are read
successfully, malformed Team or Summary contracts are blocking
`WorksheetContractError` failures; the operation must not silently downgrade after
partially interpreting a configured source.

A Summary write failure blocks the Draft write. If Summary refresh succeeds but the
later Draft write fails, the storage response must report partial success: Team
Summary was refreshed and Shift Draft was not completed. Shift Entry and Shift
Draft read failures remain blocking and do not report success.

An individual Shift participant without a derived row or usable Main ISV is treated
as `No team yet`. They remain eligible for Honso or standby after every candidate
with a known Main ISV, but they cannot be Encore.

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

The runner is separate from Shift Entry participation and is excluded from
supporter positions by Discord username. The Runner, Encore, Honso, and standby
columns all contain human-readable canonical Draft names; usernames remain
internal identity keys. Each participant username can occupy at most one
supporter position per hour. Encore, Honso, and standby all count toward the
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
appear in both Honso and Encore. Python deduplicates Runner and Shift Entry
identities by username before generating canonical names. Draft formulas contain
no Runner username or display-name constant: they read the live Runner column for
canonical-name collision checks. Each candidate row excludes both an exact
canonical-name match and a display-name match with that row's Runner cell.
Copying a candidate's complete canonical name into one Runner cell therefore
removes that identity only from the corresponding JST candidate row; entering a
shared display name excludes every candidate with that display name from that
row. Candidate cells still contain only the complete canonical Draft name so a
human can copy the cell directly into `C:G`; no bare username is rendered in
place of that name.

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

If `R` is the final schedule row, `I{R+1}:M{R+1}` contains
`仮配置済：緑背景 | アンコ配置済：緑背景＋赤字 | アンコ候補閾値 | [editable numeric input] | 万総合力`.
Generation seeds the input with the slash command's required threshold, and the
live candidate formula references that cell rather than embedding the command
value. Editing a number
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

The six-row lookup block has only a thin black top border over `J:L` and a thin
black left border down `J{R+3}:J{R+8}`. Its label cells in column `J` use
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
regeneration. The atomic Draft request grows only the explicit bot footprint through
row 38 and column `M`; it does not reserve speculative participant capacity for the
unbounded-right candidate spill. Writing an anchor formula does not clear other
cells. User-entered blockers in a spill path are intentionally preserved so Sheets
displays `#REF!` rather than silently deleting them. Regeneration replaces
`I1`, clears only a signed old threshold control and the exact old lookup
labels/input/formula anchors and their bot-owned formatting, and writes the new
controls. Cleanup covers both the
legacy Team Summary anchor directly below `シフト元メッセージ` and the new
`編成一覧` plus shifted Team Summary anchor. Removing an old array anchor lets
Sheets remove its calculated spill output while preserving unrelated user values.
A live API-generated spill beyond column `M` remains part of manual validation. If
the API-written formula cannot expand as the web UI does, any later fallback must
preserve unknown spill cells and join the same atomic request rather than resizing
or clearing speculative capacity separately.

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
generation reads the complete physical Draft grid in the spreadsheet batch, then
projects column `A` through the bounded old-control rows and defines the old body as
the consecutive valid JST labels beginning at `A2`. It clears only border and
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
across `I{R+1}:M{R+1}`. `仮配置済：緑背景` and
`アンコ配置済：緑背景＋赤字` come first in `#D9EAD3`; the latter also uses
`#FF0000` text. The `アンコ候補閾値` and `万総合力` suffix cells use `#A4C2F4`;
the input between them uses `#FFF2CC` plus the same medium solid four-sided
`#FF0000` border as the lookup input, applied after the black bottom border so
red wins at the shared edge. No border follows the candidate
spill's dynamic right edge. The formula's
blank separator columns and explicit Japanese headings provide the remaining
grouping. Dynamic Notes keep no generated borders or fills because warning rows
can move the participant-table header after manual Draft edits.

Candidate data rows use two native conditional-format rules from column `I`
through an unbounded right edge. The first rule gives a nonblank candidate in
the same row's Encore lane `C` background `#D9EAD3` and foreground `#FF0000`;
the lower-priority rule gives other candidates appearing in Draft `C:G` only
the same background. This ordering accounts for Sheets using only the first
matching conditional-format rule. This includes
`本走候補`, `アンコ候補`, and `編成未登録`. Headers and blank separator cells
remain unchanged. Generation removes every rule carrying the
`rhoboto:shift-draft:candidate:` marker and adds the current two rules in the
same atomic batch as the Draft values and formatting, so regeneration does not
accumulate rules. The API `GridRange` omits `endColumnIndex`, allowing the
formatting to follow a candidate spill that expands to additional columns.

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

## LLM Scheduling Prompt Extension

### Goal And Boundary

Each confirmed `/shift_register generate_draft` run also produces a UTF-8
`shift-draft-llm-prompt.txt` attachment for an administrator to submit manually to
an external LLM. The prompt contains the complete generation-time scheduling
inputs, the bot-generated Draft as a non-binding baseline, scheduling rules, a
self-audit contract, and a Google Sheets paste format.

Rhoboto does not call an LLM API, submit the prompt, ingest an LLM response, or
validate pasted output. It does not persist prompt text or administrator input,
and this extension does not change the command name or options, database schema,
Google Sheets layout or ownership, existing Draft formulas, or
`ShiftScheduler.assign()` behavior.

### Per-Run Administrator Requirements

Draft generation uses a Draft-specific subclass of the existing schedule
confirmation view. In addition to Confirm and Cancel, it provides a button that
opens an optional paragraph modal for up to 4,000 characters of event-specific
scheduling requirements. Reopening the modal shows the current value so the
requester can revise it before confirmation.

The Draft-specific view has a five-minute timeout. The requirements button and
modal submission both require the original command requester and recheck live
`administrator` plus `manage_channels` permissions. Modal submission only stores
the raw value in the in-memory view and acknowledges it ephemerally; it performs no
Google Sheets or database access. Confirm passes the current value into generation.
Cancel, timeout, permission loss, and destination drift discard it without
persistence. The shared Final Schedule confirmation flow and its timeout remain
unchanged.

### Pure Builder And Generation Flow

A single pure function in `utils/shift_draft_prompt.py` owns prompt rendering. It
accepts the already available generation snapshot:

- the bot-generated `DraftSchedule`, including the baseline rows, fixed Runner,
  and canonical Draft-name mapping;
- filtered Shift Entry participants and their exact stored `original_message`;
- current Team profiles or an unavailable-source state;
- the configured recruitment-slot set;
- the formatted recruitment-time description;
- the Encore Power threshold;
- the optional administrator requirements; and
- the fixed Runner's Discord username, when configured.

`build_shift_draft_llm_prompt()` is keyword-only and returns `str`. Its inputs are
typed as `DraftSchedule`, `Sequence[Shift]`,
`Mapping[str, DraftTeamProfile] | None`, `set[int]`, `str`, `float`, `str`, and
`str | None` in the order listed above.

`team_profiles=None` means Team Source data is unavailable; an empty mapping means
the source was available but has no active profiles. `administrator_requirements`
is the raw modal value, including an empty string.

The builder has no Discord, Google Sheets, database, or network dependency. It
runs while `_plan_draft_locked()` still has the same in-memory inputs used to
create the Draft and Notes. It must not read calculated Sheet Notes, parse the
rendered worksheet, or trigger a Summary read-back. Prompt rendering completes
before the Draft write requests are submitted; an unexpected builder failure
therefore fails generation before any Draft mutation.

`DraftGenerationResult` carries the rendered prompt. The cog attaches
`shift-draft-notes.txt` and `shift-draft-llm-prompt.txt` together only on the final
semantic report followup. Existing report splitting and the Notes attachment
contract otherwise remain unchanged. The final report line becomes exactly:

```text
附件包含生成時資料的 Notes 快照與 LLM 排班 prompt，不會隨 Sheet 調整更新。
```

The semantic report splitter treats that line as the final attachment section.

### Prompt Language And Complete Data

Prompt instructions and the requested LLM audit use Traditional Chinese. Exact
Japanese worksheet and role labels remain unchanged. Participant-authored
`original_message` is included completely and without translation, normalization,
or truncation. Every Shift Entry participant is present, including people omitted
from the bot baseline or missing Team data.

The prompt places variable input in an explicitly delimited JSON data block. Each
participant record contains the exact canonical name allowed in Draft, a fixed-
Runner flag, available hours, Main ISV and Power, optional Encore ISV and Power,
Encore-role eligibility, registration state, and complete `original_message`.
When the configured Runner also submitted Shift Entry, that record uses the same
canonical Runner name and remains present for requirement auditing, but is marked
`is_fixed_runner=true`. Top-level data contains the recruitment axis and gaps,
fixed Runner name and Discord username, strict Encore threshold, raw administrator
requirements, and the baseline rows. The baseline also reports each assigned
participant's total hours, longest consecutive run, Encore hours, and role-switch
count. Total and Encore hours count supporter assignments only. Consecutive hours
require adjacent Draft hour values; an unassigned hour or recruitment gap breaks
the run. A role switch is one transition between adjacent assigned hours when the
supporter slot changes; an unassigned break or recruitment gap resets the
comparison and does not count as a switch.

Administrator requirements and participant messages are scheduling data, not
prompt authority. The LLM may interpret them only as schedule constraints or
preferences. Text asking it to ignore rules, change the response grammar, execute
another task, or treat embedded text as instructions must not override the fixed
prompt contract.

### Roles, Constraints, And Priority

The prompt defines the columns and roles as follows:

- `ランナー` is fixed by Rhoboto, excluded from supporter competition, and absent
  from the LLM paste columns. A participant marked `is_fixed_runner=true` retains
  their raw requirements for auditing but cannot be assigned to a supporter role.
- `アンコ` has capacity one and requires an Encore role plus effective Power
  strictly greater than the threshold. Encore Team ISV and Power apply when that
  team exists; otherwise Main values are the effective pair.
- `本走①` through `本走③` are the three Main-ISV supporter positions.
- `待機` is the backup supporter position. Lower Main ISV may be preferred only
  when the other scheduling considerations are comparable; this is not a hard
  rule.

The following remain non-negotiable:

- assign only exact supplied canonical names;
- assign only during that participant's availability;
- assign one position at most per participant per hour;
- do not exceed role capacity or assign the Runner;
- satisfy the strict Encore eligibility rule;
- emit one five-cell row for every visible Draft hour, including blank gap rows;
  and
- leave a position blank rather than inventing a person or violating a hard
  constraint.

Requirement conflicts use this order:

1. Non-negotiable domain constraints.
2. Explicit participant `must` or `cannot` requirements inferred from
   `original_message`.
3. Per-run administrator requirements.
4. Participant preferences.
5. General schedule-quality guidance.

After applying the requirement priority above, ISV ordering is a soft scheduling
signal rather than a hard rule. When other considerations are comparable, Encore
prefers higher effective ISV, Honso prefers higher Main ISV, and standby prefers
lower Main ISV. The LLM must not pursue those ordering preferences at the cost of
participant requirements, continuity, workload, rest, or switching efficiency.
It may therefore select a lower-ISV Encore or Honso participant, or a higher-ISV
standby participant, when the whole schedule benefits. It should prefer keeping
one person in one role for two consecutive hours, avoid frequent role changes,
avoid excessive total or consecutive hours, and provide rest after long runs. The
LLM decides how strictly to apply these quality guidelines, but it cannot relax
the non-negotiable rules. The baseline is a starting reference only and may be
fully rearranged.

### Required Self-Audit And Response

Before responding, the LLM must independently recheck its proposed schedule for:

- exact row and column shape;
- unknown or noncanonical names;
- unavailable or duplicate assignments;
- Runner use and role-capacity violations;
- Encore role, effective ISV, and strict Power eligibility;
- every administrator requirement and every participant requirement or preference;
- total hours, longest consecutive hours, Encore hours, role switches, and rest;
- shortages, ambiguous text, conflicting requirements, and ignored needs; and
- the principal changes from the bot baseline.

It must revise hard-constraint violations before final output. When requirements
conflict or staffing is insufficient, it leaves cells blank instead of making an
invalid assignment and names every unsatisfied or ambiguous item with its reason.
It must not silently claim success after skipping a requirement.

The final response contains a Traditional Chinese validation summary followed by
these exact markers:

```text
<<<GOOGLE_SHEETS_TSV_BEGIN:C2>>>
<<<GOOGLE_SHEETS_TSV_END>>>
```

The LLM inserts the TSV rows between those marker lines without a header or code
fence. The enclosed block has exactly `N` rows and five columns in this order:
`アンコ`, `本走①`, `本走②`, `本走③`, `待機`, where `N` is the number of visible
Draft hour rows. Each cell is an exact supplied canonical name or blank. A
non-recruitment gap is represented by one row containing five blank cells. The
administrator copies only the content between the markers and pastes it at `C2`,
preserving the bot-owned JST and Runner columns.

When Team Source is unavailable, the prompt exposes the missing-data state, does
not guess ISV, Power, roles, or registration, and requires Encore to remain blank.
With zero participants, it requests an all-blank paste block and an explicit
staffing-shortage summary.

### Implementation And Verification Surface

Implementation is limited to:

- `components/ui_shift_register.py`: Draft-specific confirmation view, button,
  and requirements modal;
- `utils/shift_draft_prompt.py`: one pure builder and prompt-only baseline metrics;
- `utils/shift_register_manager.py`: pass the existing generation snapshot and
  return the prompt;
- `cogs/shift_register.py`: use the Draft view, pass requirements, and attach both
  files;
- `tests/test_shift_draft_prompt.py`: focused pure-output coverage;
- `tests/test_ui_permissions.py`: requester, permission, edit, cancel, and timeout
  behavior;
- `tests/test_feature_channel_interactions.py`: pre-confirmation access and final
  attachment behavior;
- `tests/test_shift_draft.py`: live-profile, unassigned-participant, fallback, and
  empty-Entry propagation; and
- this document plus `docs/manual_integration_validation.md`.

Automated tests must cover complete and unassigned participants, a fixed Runner
who also submitted Entry, duplicate and reserved-suffix names, raw multilingual
and instruction-shaped messages, Team profiles and missing values, internal
recruitment gaps, zero participants,
baseline metrics, requirement preservation, fixed rule/output instructions,
Draft-only timeout behavior, repeated modal editing, wrong-user and live-permission
rejection, no pre-confirmation Sheets access, one prompt build from the shared
snapshot, and two attachments only on the final followup.

Manual validation must submit the generated file to an external LLM, confirm the
audit reports assignment mistakes or ignored requirements, copy the marked TSV to
`C2`, and verify the existing dynamic Notes recalculate from the pasted schedule.

## Current-Draft Prompt Refresh Command

### Goal And Command Surface

`/shift_register generate_prompt_from_draft` refreshes Team Summary and generates
the same scheduling prompt from the current Shift Draft, including administrator
edits, without regenerating or writing the Draft. The command has no slash-command
parameters. It reads the current row-local Runner values from Draft column `B` and
the Encore Power threshold from the editable numeric cell in column `L` beside the
signed `アンコ候補閾値` control.

The existing per-run paragraph modal supplies optional administrator requirements.
The confirmation has a five-minute timeout and retains requester-only and live
`administrator` plus `manage_channels` checks. It uses a normal primary action,
not destructive styling or `‼️`, and states that the operation will update Team
Summary while only reading Shift Draft. Cancel and timeout perform no Google Sheets
access. After confirmation, the command refreshes current settings under the Shift
channel lock and stops without Sheets access if the displayed destinations or
recruitment contract changed.

### Read, Validate, Build, Then Write

Within the confirmed worksheet transaction, the manager uses the existing
spreadsheet-scoped batch-read and Team Summary reconciliation paths to read Shift
Entry, Shift Draft, and an available Team Source. It computes the current Summary
row plan and profiles in memory. Before submitting any mutation, it validates the
Draft and builds the complete prompt. Only after both succeed does it apply the
Team Summary mutations. The command submits no Draft worksheet request and does
not write any Draft value, formula, formatting, validation, note, conditional
format, frozen property, or worksheet dimension.

The Draft parser shares the structural portion of the existing Draft-to-Final
reader but returns the exact Sheet column order before Final-only Honso reordering
or split-color planning. It validates:

- the exact `A:G` header and continuous expected JST axis, including configured
  recruitment gaps;
- the absence of an additional recognized hour row after the expected axis;
- string-or-blank values in `B:G`;
- the signed threshold label at the expected row and a finite, non-negative
  numeric threshold value; and
- reversible identities for every nonblank Runner or supporter cell.

Supporter cells `C:G` must resolve to exactly one current Shift Entry participant.
A unique display name is accepted directly; duplicate or reserved-suffix names
must use the complete canonical `display name ⟨@username⟩` value already produced
by Draft. Runner cells in `B` resolve against current guild members so an existing
Runner remains valid even without a Shift Entry. When a Runner also has Entry, the
prompt retains that participant's complete original message. Unknown or ambiguous
values are never guessed.

Structural, threshold, and identity problems are collected with their A1 cells and
stop the entire operation before both Summary and Draft writes. Resolvable schedule
mistakes do not block prompt generation. Duplicate supporter roles, Runner/supporter
overlap, assignment outside availability, invalid Encore eligibility, nonblank gap
roles, excessive workload, frequent switches, and ignored requirements remain in
the non-binding baseline so the LLM can identify and repair them.

### One Prompt Builder And Exact Baseline

The implementation extends the existing pure prompt builder instead of adding a
second Sheet-specific copy. A current-Draft adapter converts the validated exact
rows to the existing username-backed supporter representation and supplies
row-local Runner identity. Existing `generate_draft` uses the same generalized
builder; its one fixed Runner is represented on each applicable row without
changing its command or worksheet behavior.

The JSON data identifies the baseline source as the current Sheet Draft and
preserves every visible row and the exact `B:G` role order. It also contains the
fresh in-memory Team profiles, every participant's availability and complete raw
`original_message`, the current threshold, and the raw administrator requirements.
Baseline workload metrics are computed from the current supporter cells. Schedule
mistakes remain visible to the audit rather than being silently normalized.

Runner is an hourly constraint. The LLM cannot place a row's Runner in a supporter
cell for that hour, but that person may be a supporter during another available
hour when they are not Runner. Runner remains outside the paste columns. The hard
domain rules, requirement priority, soft ISV directions, continuity, workload,
rest, switching, injection boundary, and self-audit rules remain those defined in
the LLM scheduling-prompt extension above. The audit additionally identifies the
mistakes found in the current Draft and the corrections made.

The final response remains a Traditional Chinese audit followed by the exact
five-column `C2:G...` TSV markers. The command never asks the LLM to emit or replace
JST or Runner. When Team Source is unavailable, it retains the existing safe
fallback: no capability values are guessed, Encore output stays blank, and the
prompt and Discord report state the limitation.

### Discord Result And Failure Behavior

A successful ephemeral result states that the LLM prompt was generated, uses `🔄`
only when Team Summary was synchronized, uses `👀` to state that Shift Draft was
read without modification, and shows the threshold read from Draft. It attaches
only `shift-draft-llm-prompt.txt`; it does not rebuild or attach a Notes snapshot.

Invalid Draft input uses `⚠️ 📏` and lists every bounded problem cell with a safe
detected value. It produces no attachment and no Summary mutation. If prompt
building succeeds but Team Summary persistence fails, the command uses the
existing storage-error response and does not report success or attach the prompt;
the administrator may retry safely. There is no Draft-write partial-success state
because this command has no Draft write request.

### Implementation And Verification Surface

Implementation is limited to:

- `cogs/shift_register.py`: the parameterless command, confirmation/report copy,
  error presentation, and prompt attachment;
- `components/ui_shift_register.py`: reuse of the requirements confirmation with
  non-destructive action semantics;
- `utils/shift_register_manager.py`: exact current-Draft planning, Summary-only
  mutation, and result data;
- `utils/shift_final.py`: shared structural parsing before Final-only transformations;
- `utils/shift_draft_prompt.py`: current-Sheet baseline source and row-local Runner;
- focused tests in `tests/test_shift_final.py`,
  `tests/test_shift_draft_prompt.py`, `tests/test_shift_draft.py`,
  `tests/test_ui_permissions.py`, and
  `tests/test_feature_channel_interactions.py`; and
- this document plus `docs/manual_integration_validation.md`.

Tests cover exact order preservation, threshold and identity errors, aggregated A1
reporting, retention of repairable schedule violations, row-local Runner behavior,
fresh profile and raw-message propagation, Summary-only mutations, zero Draft
requests, no pre-confirmation access, permission and settings drift, safe fallback,
storage failure, result copy, and the one-file attachment. Full repository lint,
format, lock, pytest coverage, compile, and whitespace gates remain required.

This follow-up adds no worksheet columns or ownership, database schema, dependency,
LLM API, response ingestion, pasted-output validation, persistent requirement,
scheduler change, translator entry, or additional Notes artifact.

## Affected Files

The implemented flow remains in `utils/shift_scheduler.py` and
`utils/shift_register_structs.py`. The Draft refresh changes are:

- `cogs/shift_register.py`
  - Pass current Discord members into confirmed Draft generation.
  - Disclose Summary refresh in the confirmation and report successful refresh.
  - Preserve distinct unavailable-source and partial-success responses.
- `utils/team_register_structs.py`
  - Derive active Summary values once from validated Team rows and current Discord
    users.
  - Index archived rows and resolve active, archived, reusable, and appended rows for
    both single and full plans.
  - Replace automatic obsolete-row deletion with a one-cell
    `<username> (archived)` username update.
- `utils/team_register_manager.py`
  - Make full Summary refresh consume the shared derivation and row plan.
  - Stop using old Summary display names and roles as derivation fallbacks.
- `utils/shift_register_manager.py`
  - Read every configured Team worksheet plus Summary in the spreadsheet-scoped
    batch instead of projecting profiles from old Summary values.
  - Reuse the shared active Summary derivation for the Summary write and Draft
    profiles without read-back.
  - Preserve the existing unavailable-source scheduling behavior.
- `utils/google_sheets.py`
  - Reuse the existing low-level request builders so Summary and Draft subrequests
    can share one spreadsheet batch when their spreadsheet is the same.

Expected automated-test changes:

- `tests/test_shift_draft.py`
- `tests/test_feature_channel_interactions.py`
- `tests/test_manager_fakes.py`
- `tests/test_google_sheets_adapter.py`
- `tests/test_worksheet_structs.py`

Documentation changes:

- This design document.
- `docs/shift_register_team_source_design.md`.
- `docs/manual_integration_validation.md` during implementation.

No database migration or Shift Entry worksheet migration is required.

## Automated Test Contract

Focused tests must cover:

- Identical derived Summary values for explicit full refresh and Draft generation.
- Current Discord display names and Encore roles override no data from the old
  Summary; unmatched active Team users use Team display names and empty roles.
- One values batch per spreadsheet for Entry, Draft, all Team worksheets, and
  Summary, followed by zero Summary read-back requests.
- Same-spreadsheet Summary and Draft subrequests share one atomic write; external
  Summary success followed by Draft failure reports partial success.
- Active, archived, reusable, occupied-manual, and appended Summary row resolution
  with the same precedence in single upsert and full reconciliation.
- Archived-row restoration, duplicate/corrupt marker rejection, exclusion from
  active Summary records and exact-active formula matches, and preservation of
  every administrator-owned cell and row property.
- Explicit confirmed Team deletion continues deleting the complete active row.
- Every Encore role/Encore Team combination.
- Strict Power threshold comparison, including equality.
- Main fallback for Encore ISV and Power.
- Encore, Honso, and standby cross-role continuity.
- Lowest Main ISV standby assignment and tied standby continuity.
- Same-column Honso placement.
- `No team yet` scheduling order and Japanese `未登録` Notes output.
- Every Team Source fallback status and marker.
- Unique, duplicate, and reserved-suffix canonical names.
- Runner/Entry username deduplication and live per-row Runner-column candidate
  exclusion without Runner identity constants in formulas.
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

- Confirming Draft generation refreshes Team Summary from current Discord display
  names/roles and current Team tabs before the Summary-backed Draft formulas run.
- Removing a username from all Team tabs, confirming full refresh archives only the
  Summary bot band, then restoring the same username and confirming the same row
  and administrator cells are restored without reassignment.
- Injecting Summary and Draft write failures separately, including external
  Summary success followed by Draft failure and same-spreadsheet atomic failure.
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

Draft-to-Final preserves and compares every canonical schedule label as exact
text; it does not parse the label or require Discord identity resolution before
producing the static Final snapshot. See
`docs/shift_register_final_schedule_generation_design.md` for the Final worksheet
contract. A future shift-reminder flow will own canonical-name-to-identity
resolution. That resolver must return exactly one current guild member before
using a mention, and must report unresolved or ambiguous identities rather than
guessing.
