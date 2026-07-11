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
- Moving Shift Entry worksheets to the count row plus fixed `A:AJ` bot layout.
- Showing Team Summary ISV/Encore information in Shift Entry through formulas.
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

Backfill existing rows:

- timeline columns stay `null`
- empty or null `recruitment_time_ranges` becomes `[{"start": 4, "end": 28}]`
- null `deadline_automation_enabled` becomes `false`

Verification query checklist:

- Every row in `shift_register` has non-empty `recruitment_time_ranges`.
- Every existing row has `deadline_automation_enabled = false`.
- Existing sheet URL and worksheet ID columns are unchanged.
- Existing `final_schedule_anchor_cell` values are unchanged.

## Shift Entry Worksheet Migration

The bot-owned Shift Entry layout is:

```text
Row 1: count formulas in F:AI
Row 2: username | display_name | Main ISV | Encore ISV |
       Team Info | 0-1 | ... | 29-30 | original_message
Rows 3+: participant data
```

Columns `A:AJ` are bot-owned. Columns `AK` onward are administrator-owned; normal
registration writes never target them. `C` contains the participant's Team formula,
while `D:E` are spill results and are not written directly.

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
5. Run `/shift_register announce_timeline` with no parameters and confirm the public
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
- Re-enable the bot only after slash commands and settings panels match the
  restored code version.

Do not mix the new bot code with old Shift Entry worksheet headers. The bot will
reject old headers to avoid corrupting entries.
