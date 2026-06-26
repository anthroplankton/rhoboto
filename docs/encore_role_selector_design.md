# Encore Role Selector Design

## Goal

Replace the temporary hardcoded Encore role picker with a Discord-native
Role Select flow that can choose roles from the guild without relying on a
custom 25-option list.

The flow must protect production settings by previewing changes before saving.
It must preserve existing Team Register behavior outside Encore role settings.

## Current Behavior

Team Register stores Encore roles as role IDs in
`TeamRegisterConfig.encore_role_ids`.

The current role picker is a custom string select. It can only show up to 25
custom options and has previously used hardcoded role IDs as a workaround.
Selecting roles writes immediately to the database by replacing the stored
`encore_role_ids` with the selected role IDs.

Team submissions and summary refreshes only consume the stored role IDs. They
match member roles against `encore_role_ids` and write matching role names into
the Google Sheets summary.

## Proposed Behavior

Use Discord's auto-populated Role Select for Encore role editing.

The first-level Team Register settings view should show the current settings
embed and two buttons:

- `Edit Team Register Settings`
- `Edit Encore Roles`

`Edit Team Register Settings` continues to open the existing sheet settings
modal. `Edit Encore Roles` enters a separate Encore role editing flow in the
same ephemeral message.

The Encore role editing flow is:

1. `settings view`
2. `role edit view`
3. `preview draft`
4. `saved settings view` or `cancelled disabled preview`

The Role Select uses:

- `min_values=0`
- `max_values=25`
- `default_values` for stored Encore role IDs that still exist in the guild

Stored role IDs that no longer resolve to guild roles are treated as missing
role IDs. They are not shown in Role Select defaults, but they are retained in
the draft unless the user explicitly removes them.

## Settings View

The first-level settings embed should show active Encore roles and missing role
IDs separately.

`Encore Roles` should show the roles that still resolve in the guild. If there
are no active Encore roles, it should say that no active Encore roles are
configured.

`Missing Encore Role IDs` should be shown only when stored IDs no longer resolve
to guild roles. It should explain that the IDs are retained until removed during
the Encore role edit flow.

## Role Edit View

The role edit view should be simple.

When there are no missing role IDs, it should show a short `Edit Encore Roles`
embed, a Role Select, and a `Back to Settings` button.

When missing role IDs exist, it should additionally show a
`Missing Encore Role IDs` field. Active roles do not need to be repeated in the
embed because they are represented by Role Select default values.

`Back to Settings` does not write to the database. It returns the same ephemeral
message to the first-level settings view.

## Preview Draft

Selecting roles does not write to the database. It edits the same message into a
preview draft.

The draft contains:

- selected guild roles from Role Select
- retained missing role IDs from the previous database state

The preview should show:

- selected Encore roles
- retained missing role IDs, if any
- `⚠ Warnings`, only when `@everyone` is selected

`@everyone` is allowed. The warning should explain that every member will be
marked in Google Sheets.

Managed integration roles are allowed and do not need a warning because Encore
roles are used as Google Sheets markers rather than Discord authorization.

The preview view should include:

- `Confirm Save`
- `Cancel`
- `Remove Missing From Draft`, only when the draft retains missing role IDs

`Remove Missing From Draft` does not write to the database. It only edits the
draft so the next `Confirm Save` will omit missing role IDs.

`Cancel` does not write to the database. It leaves the message on the preview,
disables all controls, and shows that no changes were saved.

## Save Semantics

`Confirm Save` is the only action that writes Encore role settings.

Saving replaces the stored `encore_role_ids` with the draft role ID list. By
default, that list includes selected guild role IDs plus retained missing role
IDs. If the user removes missing IDs from the draft, those IDs are omitted from
the saved list.

After a successful save, the bot should show a saved current settings embed and
the first-level settings buttons so the user can continue adjusting settings.

## Permissions

All settings-changing callbacks must re-check the existing settings permission
rule: the user must have both `administrator` and `manage_channels`.

This applies to:

- `Edit Team Register Settings`
- `Edit Encore Roles`
- Role Select preview creation
- `Remove Missing From Draft`
- `Confirm Save`
- `Cancel`
- `Back to Settings`

Although `Cancel` and `Back to Settings` do not write to the database, checking
permissions keeps the UI behavior consistent after permissions change while a
view is open.

## Error Handling

If there are more than 25 active stored Encore roles, the bot cannot safely
preselect them all because Role Select defaults and selected values are capped
at 25. The edit flow should not silently drop roles. It should show a clear
error and avoid entering the Role Select edit view.

If saved role IDs no longer resolve to guild roles, keep them as missing role
IDs by default. Do not remove them unless the user removes them from the preview
draft and confirms the save.

If saving succeeds but refreshing the full settings embed fails because Google
Sheets metadata cannot be fetched, the bot should report that Encore roles were
saved but the settings view could not be refreshed. The save must not be
reported as failed after the database write has completed.

If the settings record is missing during an edit callback, the bot should show a
clear ephemeral error and avoid writing anything.

## Affected Files

Expected implementation surface:

- `components/ui_team_register.py`
- `utils/team_register_manager.py`
- `tests/test_role_options.py`
- `tests/test_ui_permissions.py`

A focused pure-helper test file may be added if the role ID splitting and draft
logic is extracted.

## Out of Scope

This design does not change:

- Discord command names
- privileged intents
- database schema
- Google Sheets worksheet titles, IDs, or columns
- team parsing
- summary worksheet layout
- secrets, `.env`, service account files, local databases, or logs
- deployment workflows

## Test Plan

Pure logic tests should cover:

- splitting stored Encore role IDs into active roles and missing IDs
- retaining missing IDs by default
- removing missing IDs from the draft without touching selected roles
- detecting `@everyone` for warnings
- rejecting the edit flow when more than 25 active stored roles must be
  preselected

UI callback tests should cover:

- unauthorized users cannot enter role editing, create previews, remove missing
  IDs, or confirm saves
- `Edit Encore Roles` edits the current message into the role edit view
- selecting roles creates a preview without updating the database
- `Confirm Save` updates the database once with the draft role IDs
- `Cancel` does not update the database and disables preview controls
- `Remove Missing From Draft` updates only the draft view
- successful save returns to a saved current settings view

Manual Discord validation should cover:

- selecting a role that would not have appeared in the old first 25 custom
  options
- existing stored roles appearing as selected defaults
- `@everyone` showing a `⚠ Warnings` field in preview
- missing role IDs appearing in settings, role edit, and preview surfaces
- `Cancel` leaving production settings unchanged
- `Confirm Save` updating summary behavior through the stored
  `encore_role_ids`
- permission removal after opening the view preventing later mutation
