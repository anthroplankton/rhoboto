# Shift Register Draft Generation Design

## Status

The base design and the recruitment-time and Notes snapshot follow-up are
implemented and covered by automated validation. Live Discord and Google Sheets
checks remain part of the manual integration checklist.

## Goal

Improve `/shift_register generate_draft` so the generated Shift Draft uses the
configured Team Source for Encore eligibility and ISV-first scheduling, remains
usable when Team data is unavailable, preserves visually continuous assignments,
and records dynamic workload notes below the draft.

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
- One atomic Google Sheets batch update for the bot-owned Draft area.

This change does not include:

- Scheduled or automatic Draft generation.
- Candidate Honso, candidate Encore, or no-team-yet sections to the right of the
  Draft.
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

- `A:G`: bot-owned Draft schedule.
- `A:H` below the schedule: bot-owned dynamic Notes spill.
- `I+`: untouched by this change and reserved for future candidate Honso, candidate
  Encore, and no-team-yet sections.

Future candidate Honso and candidate Encore sections may order candidates visually
by ISV from high to low in opposite horizontal directions. That layout is deferred
and must not be scaffolded in this implementation.

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

Draft generation reuses
`AsyncioGspreadWorksheet.batch_update_typed_values()` and performs one underlying
`spreadsheets.batchUpdate` call with ordered subrequests:

1. Update range `A:G` with the raw header and schedule rows. Because the specified
   range is larger than the supplied rows, remaining `userEnteredValue` cells in
   `A:G` are cleared, removing stale schedules and Notes.
2. Clear `H` from the Notes anchor row downward so stale values cannot block the
   eight-column spill while preserving the future `I+` candidate area.
3. Write the Notes formula to its calculated cell as a formula.

The first range is not listed in `formula_ranges`, so user-derived display names
that start with `=` remain strings. Only the Notes cell is formula-enabled. The
field mask remains `userEnteredValue`, preserving formatting, validation, and cell
notes. Columns `I+` are outside the request.

Google Sheets applies all subrequests atomically. An invalid subrequest causes the
entire batch to fail, so a successful clear cannot be followed by a failed partial
replacement.

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
  - Write the schedule and Notes through one typed batch.
- `utils/shift_scheduler.py`
  - Add the Draft team profile boundary.
  - Implement eligibility, ISV ordering, cross-role continuity, standby selection,
    and canonical Draft names.
- `utils/shift_register_structs.py`
  - Render canonical Draft names.
  - Build the dynamic Notes formula and combined Draft write rows.

Expected automated-test changes:

- `tests/test_shift_scheduler.py`
- `tests/test_shift_draft.py`
- `tests/test_feature_channel_interactions.py`
- `tests/test_manager_fakes.py`
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
- One atomic typed batch, stale `A:G` and Notes-column `H` clearing, raw
  user-derived strings, and no
  `I+` mutation.
- Discord report ordering, recruitment-time display, configured-slot filtering,
  existing shortage/unassigned behavior, and snapshot notice.

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
- Shorter regeneration clearing stale `A:G` values while preserving `I+`.
- Google Sheets write-failure injection confirming atomic preservation of the old
  Draft.

## Future Compatibility

The future right-side candidate sections must reuse canonical Draft names and Team
profiles rather than create a second participant identity format. Candidate Honso
and candidate Encore may contain the same participant, and already-scheduled
participants remain listed. The future no-team-yet section lists Shift participants
without usable Team profiles.

Draft-to-Final must validate every nonblank schedule cell against the canonical
name mapping before producing a Final schedule. Final-to-Discord handoff messages
must resolve the resulting username to exactly one current guild member and use the
member mention. Failure to resolve or ambiguity must be reported rather than
guessing.
