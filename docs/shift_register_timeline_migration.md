# Shift Register Timeline Migration

This checklist covers the one-time migration for the Shift Register timeline,
recruitment range, and `0-30` Shift Entry worksheet layout.

Use a development guild and disposable spreadsheet first. Do not run these steps
against production data without a database backup and a rollback plan.

## Scope

This migration includes:

- Persisting Shift Register timeline fields in `shift_register`.
- Persisting `recruitment_time_ranges`, defaulting to `4-28`.
- Reserving `deadline_automation_enabled`, defaulting to `false`.
- Moving Shift Entry worksheets to fixed hour columns `0-1` through `29-30`.
- Making `/shift_register info` read saved settings instead of command
  parameters.

This migration does not include:

- Scheduler jobs.
- Automatic deadline close.
- Automatic draft shift generation.
- Reminder channels.
- Role assignment from final shifts.
- `/shift_register info` removal.

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

The new Shift Entry core header is:

```text
username
display_name
0-1
1-2
...
29-30
original_message
```

Existing old worksheets with `4-5` through `27-28` are intentionally rejected by
the bot instead of being silently interpreted as the new shape.

Recommended migration:

1. Create a backup copy of the existing spreadsheet.
2. Create a new Shift Entry worksheet or replace the old header with the new
   `0-30` header.
3. If preserving old rows, map matching old hour columns into the same labels in
   the new sheet and fill new outside-range columns with `0`.
4. Keep `username`, `display_name`, and `original_message` values unchanged.
5. Leave draft and final schedule worksheets unchanged.

Trailing extra columns after `original_message` are accepted by the header guard,
but the current Shift Entry write path does not promise to preserve them. Do not
use trailing extra columns as source-of-truth data until that behavior has its
own design.

## Settings Migration

After database and sheet migration:

1. Run `/shift_register settings`.
2. Confirm the panel shows:
   - Google Sheet link.
   - Entry, draft, and final worksheet titles and IDs.
   - Final schedule anchor cell.
   - Shift Timeline.
   - Recruitment Time Range.
3. Use `Edit Shift Timeline` to save day number, event date, and milestones.
4. Use `Edit Recruitment Time Range` to confirm or change the range.
5. Run `/shift_register info` with no parameters and confirm the public
   announcement uses the saved values.

Blank timeline fields are valid. Blank recruitment range input resets to
`4-28`.

## Validation

Run automated checks before manual validation:

```shell
uv run pytest
uv run ruff check --no-fix .
uv run ruff format --check .
uv run black --check --workers 1 main.py bot cogs components models utils
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
