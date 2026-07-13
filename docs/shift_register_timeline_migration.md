# Shift Register Timeline Migration

This checklist covers the one-time migration for the Shift Register timeline,
recruitment range, and bot-managed Shift Entry count/Team layout.

Use a development guild and disposable spreadsheet first. Do not run these steps
against production data without a database backup and a rollback plan.

## Scope

This migration includes:

- Persisting Shift Register timeline fields in `shift_register`.
- Persisting `recruitment_time_ranges`, defaulting to `4-28`.
- Reserving `deadline_automation_enabled`, defaulting to `false`.
- Moving Shift Entry worksheets to the count row plus fixed `A:AJ` layout.
- Showing Team Summary ISV/Encore information in Shift Entry through formulas.
- Persisting an optional Team Register source for each Shift Register.
- Repairing Shift Entry Team formulas after Team Summary worksheet renames.
- Preserving administrator-owned Shift Entry cells from `AK` onward.
- Making `/shift_register announce_timeline` read saved settings instead of command
  parameters.

This migration does not include:

- Scheduler jobs.
- Automatic deadline close.
- Automatic draft shift generation.
- Reminder channels.
- Role assignment from final shifts.
- `/shift_register announce_timeline` removal.

## Database Migration

New installs can create the schema directly through the current Tortoise models.
Existing databases need a one-time schema migration because `generate_schemas()`
does not safely migrate existing tables.

Add these nullable timeline columns to `shift_register`:

- `day_number`
- `event_date`
- `submission_deadline_at`
- `draft_shift_proposal_at`
- `final_shift_notice_at`

Add these non-null columns with defaults:

- `recruitment_time_ranges`, default JSON:
  `[{"start": 4, "end": 28}]`
- `deadline_automation_enabled`, default `false`

Add nullable `team_source_feature_channel_id` to `shift_register` as a foreign key
to `feature_channel.id` with `ON DELETE SET NULL`. Existing rows stay `null`; no
backfill is required.

This repository does not track an Aerich or equivalent migration module.
`generate_schemas()` is not a safe existing-production migration mechanism. Back up
the database, stop the bot, inspect the schema generated from the current model in a
disposable database, then apply the reviewed database-specific equivalent. Do not
reuse one `ALTER TABLE` command across SQLite and the production database; SQLite
may require a table rebuild.

Backfill existing rows:

- timeline columns stay `null`
- empty or null `recruitment_time_ranges` becomes `[{"start": 4, "end": 28}]`
- null `deadline_automation_enabled` becomes `false`

Verification query checklist:

- Every row in `shift_register` has non-empty `recruitment_time_ranges`.
- Every existing row has `deadline_automation_enabled = false`.
- Existing sheet URL and worksheet ID columns are unchanged.
- Existing `final_schedule_anchor_cell` values are unchanged.
- `team_source_feature_channel_id` exists, is nullable, and references
  `feature_channel.id` with `ON DELETE SET NULL`.
- Deleting a selected Team FeatureChannel clears the Shift relation without
  deleting Shift settings.

## Shift Entry Worksheet Migration

The bot-owned Shift Entry layout is:

```text
Row 1: count formulas in F:AI
Row 2: username | display_name | Main ISV | Encore ISV |
       Team Info | 0-1 | ... | 29-30 | original_message
Rows 3+: participant data
```

Value ownership is range-specific: the bot owns `A1`, `F1:AI1`, `A2:AJ2`, and
participant-row `A:C` plus `F:AJ`. It reads the fixed header only within `A:AJ`
and participant values only from `A:C` plus `F:AJ`. `C` contains the participant's
Team formula; `D:E` are its spill area and are not directly read, validated,
cleared, or value-written, so a manual blocker and its visible `#REF!` are
preserved. Row-1 `B:E` and `AJ` values are likewise preserved. Columns `AK`
onward are administrator-owned and are not read or written by normal registration.

Existing row-1-header worksheets and old worksheets with `4-5` through `27-28`
are intentionally rejected instead of being silently reinterpreted.

Recommended migration:

1. Create a backup copy of the existing spreadsheet.
2. In the existing Shift Entry worksheet, insert one row above the legacy header.
3. Insert three columns before legacy column `C`. Native Google Sheets insertion
   moves each participant's hour data and all trailing cell values, formulas,
   formatting, validation, and notes together.
4. Leave new `C:E` blank, or set them to `Main ISV`, `Encore ISV`, and
   `Team Info`.
5. Confirm hours are now `F:AI`, `original_message` is `AJ`, and all manual columns
   start at `AK`.
6. Leave draft and final schedule worksheets unchanged.
7. Trigger one Shift registration. The bot initializes or repairs row 1, row 2,
   and participant formulas without writing `AK+`.
8. In Google Sheets, grant `IMPORTRANGE` **Allow access** once for the Team source
   spreadsheet. Formula results remain unavailable until this connection is allowed.

Do not manually copy only visible values during this migration; doing so can detach
formulas, validation, formatting, or notes from their participant row.

### Shift Entry presentation

The bot owns the presentation of `A:AJ`, including conditional formatting over the
`D:E` spill area, while leaving administrator columns from `AK` onward unchanged.
Presentation ownership does not grant value ownership over the preserved ranges
described above. `A1` and `A2:AJ2` use a `#3C78D8` background with bold
white text. Row 2 has black top and bottom borders, with additional right borders
after `display_name`, `Team Info`, and `29-30`. Columns `A:E` are frozen. Column
widths are 100 px for `A:B`, 60 px for `C:E`, and 40 px for
`F:AI`; the bot does not change the width of `AJ`.

Participant rows use native conditional formatting so Filter views retain visible
orange/pink alternation after filtering or sorting. `A:E` and `AJ` receive the
row color. Availability value `1` uses the same row color, while `0` remains
white; both digits use a nearby low-contrast font color so they remain legible at
close range without dominating the color blocks. Columns outside the configured
recruitment min-max range are hidden. Hour columns inside min-max gaps remain
visible with a `#CCCCCC` background, preserving the continuous time axis.

Rhoboto conditional-format formulas contain a no-op
`rhoboto:shift-entry:` marker. Before initialization or repair, the bot reads the
worksheet rule metadata. If the marked rules exactly match the desired rules, it
does not add them again. Otherwise, one atomic Sheets batch deletes only marked
rules in descending index order and adds the complete replacement set; unmarked
administrator rules remain unchanged. The same Sheet write lock covers the
metadata read and atomic write.

Initial Sheet setup applies the layout even when no participant rows exist. It
writes the count and header rows, installs formatting from row 3 onward, and does
not create a placeholder participant. Existing Sheets are repaired on the next
Shift submission or recruitment-range save. Saving a recruitment range updates
the database first and then immediately applies its visibility and gap formatting;
if the Sheet write fails, the setting remains saved and Discord reports partial
success so the same value can be retried safely.

## Settings Migration

After database and sheet migration:

1. Run `/shift_register settings`.
2. Confirm the panel shows:
   - Google Sheet link.
   - Entry, draft, and final worksheet titles and IDs.
   - Team Source status and, when uniquely resolved, its channel and Team Register
     Google Sheet link.
   - Final schedule anchor cell.
   - Shift Timeline.
   - Recruitment Time Range.
3. Use `Edit Shift Timeline` to save day number, event date, and milestones.
4. Use `Edit Recruitment Time Range` to confirm or change the range.
5. Use `Edit Team Source` to select the configured Team Register channel, then
   press `Apply & Repair`.
6. Rename Team Summary in the development Sheet, reapply the same source, and
   confirm populated Shift Entry column C formulas use the new worksheet title.
7. Run `/shift_register announce_timeline` with no parameters and confirm the public
   announcement uses the saved values.

Blank timeline fields are valid. Blank recruitment range input resets to
`4-28`.

## Validation

Run automated checks before manual validation:

```shell
uv run pytest
uv run ruff check --no-fix .
uv run ruff format --check .
```

Manual checks are listed in `docs/manual_integration_validation.md`.

## Rollback Notes

If the migration must be rolled back:

- Stop the bot before changing database schema or worksheet headers.
- Restore the database backup and spreadsheet backup together.
- When rolling back only Team source selection, deploy code that no longer reads
  the relation before removing its constraint and column.
- Re-enable the bot only after slash commands and settings panels match the
  restored code version.

Do not mix the new bot code with old Shift Entry worksheet headers. The bot will
reject old headers to avoid corrupting entries.
