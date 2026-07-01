# Register Settings Lifecycle Design

## Goal

Improve the Team Register and Shift Register settings experience by sharing the
common settings lifecycle while keeping feature-specific UI and persistence
logic separate.

This design covers:

- Team Register and Shift Register shared setup/current/saved settings flow
- stale setup button handling
- shared panel copy conventions
- Team Register Encore role UX refinements

It does not implement the change. It is the approved design input for a later
implementation plan.

## Current Behavior

Team Register and Shift Register already share several lower-level patterns:

- `FeatureChannelBase` owns channel enable/disable behavior.
- `ManagerBase` owns sheet config lookup, Google Sheet client caching, metadata
  fetch, worksheet creation, and sheet config upsert.
- Each feature manager maps feature-specific worksheet title inputs into the
  shared `ManagerBase.upsert_sheet_config_and_worksheets()` flow.

The settings UI lifecycle is still duplicated in the Team and Shift cogs and UI
components:

- `/enable` sends a feature-enabled response, then follows up with either setup
  UI or current settings UI.
- setup/edit modal save follows up with a saved settings panel.
- old setup panels can remain visible after settings are saved.

The stale setup panel is the main UX problem. If a user clicks an old
`Setup Team Register` or `Setup Shift Register` button after settings already
exist, the callback should not open an empty setup modal.

## Scope

The implementation should cover two layers.

Shared settings lifecycle:

- `/enable` follow-up behavior for Team Register and Shift Register
- `/settings` current setup/settings behavior for Team Register and Shift Register
- setup prompt, current settings panel, saved settings panel
- stale setup button fresh-check behavior
- shared current/saved/stale copy patterns
- shared helper for sending current/saved settings panels
- shared fresh sheet config lookup on `ManagerBase`

Feature-specific behavior:

- Team Register modal inputs, Team worksheets, summary worksheet, settings
  fields, settings buttons, and Encore role flow
- Shift Register modal inputs, Entry/Draft/Final Schedule worksheets, final
  schedule anchor cell, settings fields, and settings button

## Out of Scope

This design does not change:

- Discord slash command names
- privileged intents
- permission model
- database schema
- Google Sheets worksheet layout or columns
- existing Google Sheets error wording
- secrets, `.env`, service account files, local databases, or logs
- production deployment workflow

## Shared Lifecycle

### Enable Flow

When `/enable` is used:

1. Enable the feature channel.
2. Send the existing feature-enabled ephemeral response.
3. Follow up based on sheet config state:
   - no sheet config: show setup prompt and setup button
   - existing sheet config: show current settings panel and edit controls

The design keeps feature enablement separate from sheet configuration.

### Settings Command Flow

When `/settings` is used:

1. Defer the interaction ephemerally.
2. Use the same setup/current settings decision as the `/enable` follow-up:
   - no sheet config: show setup prompt and setup button
   - existing sheet config: show current settings panel and edit controls

This command should not have a separate settings lifecycle from `/enable`.

### Setup And Edit Save Flow

When a setup/edit modal saves successfully:

1. The feature-specific modal parses input and saves settings through its
   manager.
2. The shared settings flow helper sends a new saved settings panel.
3. The bot does not rely on deleting or editing all old setup panels.

Saved settings panels use:

- `Team Register Settings Saved`
- `Shift Register Settings Saved`

### Stale Setup Button Flow

When an old setup button is clicked:

1. Re-check sheet config without using cached manager state.
2. If no config exists, open the setup modal normally.
3. If config exists, do not open an empty modal.
4. Send a new ephemeral current settings panel.

Stale setup follow-up content:

- `Team Register is already configured for this channel. Here are the current settings.`
- `Shift Register is already configured for this channel. Here are the current settings.`

The current settings panel title remains the normal settings title. Do not add a
separate "already configured" panel type.

## Shared Copy Rules

Current settings panel titles:

- `Team Register Settings`
- `Shift Register Settings`

Saved settings panel titles:

- `Team Register Settings Saved`
- `Shift Register Settings Saved`

Team saved panel description:

```text
Your Team Register settings were saved. Use the buttons below to edit sheet settings or Encore roles.
```

Team current panel description:

```text
Team Register is configured for this channel. Use the buttons below to update sheet settings or Encore roles.
```

Shift saved panel description:

```text
Your Shift Register settings were saved. Use the button below to edit sheet settings.
```

Shift current panel description:

```text
Shift Register is configured for this channel. Use the button below to update sheet settings.
```

Footer behavior:

- Team Register keeps only the worksheet-title caveat:

  ```text
  To add worksheet titles, edit sheet settings and include all existing titles plus any new ones.
  ```

- Shift Register removes the footer because the description already explains the
  settings button.

## Shared Abstraction Boundary

Use a thin shared settings flow helper plus feature-specific adapters. Do not
build a large generic settings framework.

Shared responsibilities:

- Add `ManagerBase.get_fresh_sheet_config()` to clear `_sheet_config` and
  `_google_sheet`, then read the current sheet config.
- Provide a settings flow helper for:
  - setup prompt or current settings panel after `/enable`
  - stale setup fresh-check behavior
  - saved settings panel response after successful modal save
  - current/saved/stale copy patterns
  - sending current/saved panel responses
- Keep existing Google Sheets error handling behavior.
- Keep settings permission checks.

Feature adapter responsibilities:

- parse modal values
- call the feature manager with the correct worksheet title inputs
- apply feature-specific extra config updates
- return enough context for settings panel construction
- build feature-specific embeds and views

Team adapter responsibilities:

- Team worksheet titles and summary worksheet title
- fetching `encore_role_ids` after settings save
- Team settings embed fields and settings view buttons
- Encore role empty, missing, edit, preview, cancel, and save flow

Shift adapter responsibilities:

- entry, draft, and final schedule worksheet titles
- saving `final_schedule_anchor_cell`
- Shift settings embed fields and settings view button
- final schedule anchor cell display

Do not force the modal classes into a shared generic modal. Their fields differ
enough that the common behavior should live around the lifecycle, not inside a
single form abstraction.

## Team Register Encore Roles

Encore roles remain Team Register-specific.

### Settings Panel Field Copy

If active Encore roles exist, the `Encore Roles` field should show only role
mentions.

If no active Encore roles exist, the field should show:

```text
No encore roles set yet. Use Edit Encore Roles to choose Discord roles to show for matching members.
```

If missing role IDs exist, show a separate `Missing Encore Role IDs` field with
a compact explanation:

```text
`123`, `456`
Retained until removed during Encore role editing.
```

### Settings Panel Button Emphasis

Setup prompt:

- `Setup Team Register` remains primary because it is the only next step.

General current settings panel:

- `Edit Team Register Settings` uses secondary.
- `Edit Encore Roles` uses secondary.

Saved settings panel:

- if no active Encore roles are configured, `Edit Encore Roles` uses primary and
  `Edit Team Register Settings` uses secondary.
- if active Encore roles exist, both buttons use secondary.

This keeps regular settings panels stable while allowing the immediate
post-setup panel to point toward the likely next step.

### Edit Flow

`Edit Encore Roles` opens a role edit view.

Description:

```text
Choose Discord roles to show for matching members.
```

The edit view contains:

- Role Select
- `Remove Missing IDs`, only when missing role IDs exist
- `Back to Settings`

Selecting roles creates a preview where missing IDs are retained by default.

Clicking `Remove Missing IDs` creates a preview where missing IDs are marked for
removal. It does not write to the database.

`Back to Settings` returns to the current settings panel without writing.

### Preview Flow

Preview title:

```text
Preview Encore Role Changes
```

Preview description:

```text
Review the Encore roles before saving. Changes are not saved until you confirm.
```

The preview shows selected roles.

If missing IDs are retained:

```text
Retained Missing Role IDs
`123`, `456`
These IDs will stay saved after you confirm.
```

If missing IDs are marked for removal:

```text
Removed Missing Role IDs
`123`, `456`
These IDs will be removed when you confirm.
```

Preview buttons:

- `Confirm Save`
- `Cancel`

Do not show `Remove Missing IDs` in the preview. The preview is only for
confirming or cancelling the draft.

### Save And Cancel

`Confirm Save` is the only Encore role action that writes to the database.

When missing IDs are retained, saving writes selected role IDs plus retained
missing IDs.

When missing IDs are marked for removal, saving writes only selected role IDs.

After save, return to the shared `Team Register Settings Saved` panel.

`Cancel` does not write to the database. It returns to the `Team Register
Settings` panel with:

```text
Cancelled. No changes saved.
```

Do not leave the user on a disabled preview after cancelling.

## Permissions

Do not broaden settings UI permissions.

All settings-changing callbacks continue to require both `administrator` and
`manage_channels`.

Permission checks should remain on non-writing navigation callbacks such as
`Back to Settings`, `Cancel`, and stale setup conversion. This keeps behavior
consistent if permissions change while a view is open.

## Error Handling

Do not change existing Google Sheets error copy in this design.

If settings save fails before the database write, use existing Google Sheets
error handling.

If Encore roles save succeeds but refreshing the settings panel fails, report
that Encore roles were saved but the settings view could not be refreshed.

If stale setup fresh-check finds no config, open the setup modal. Treat this as
a normal state, not an error.

If stale setup fresh-check finds config but metadata fetch fails, use existing
Google Sheets error handling.

## Test Plan

Shared lifecycle tests:

- `/enable` with no config shows setup prompt.
- `/enable` with config shows current settings panel.
- stale setup click with no config opens setup modal.
- stale setup click with config does not open an empty modal and sends current
  settings panel.
- saved panel uses saved title and description.
- current panel uses current title and description.

Team-specific tests:

- empty Encore roles field uses the new copy.
- active Encore roles field only shows role mentions.
- missing role IDs field uses compact spacing.
- saved panel with no active Encore roles makes `Edit Encore Roles` primary.
- saved panel with active Encore roles makes both settings buttons secondary.
- current settings panel makes both settings buttons secondary.
- `Remove Missing IDs` appears in edit view only when missing IDs exist.
- `Remove Missing IDs` enters preview without writing.
- preview buttons are only `Confirm Save` and `Cancel`.
- retained missing IDs are preserved on confirm.
- removed missing IDs are omitted on confirm.
- `Cancel` does not write and returns to current settings panel.

Shift-specific tests:

- stale setup click with config does not open an empty modal and sends current
  settings panel.
- saved/current descriptions are present.
- Shift settings footer is removed.
- final schedule anchor cell is still saved and shown.

Manual validation:

- Use a development guild and a disposable Google Sheet.
- Validate Team Register setup, edit, stale setup, saved panel, current panel,
  Encore role edit, remove missing IDs, preview, confirm, and cancel.
- Validate Shift Register setup, edit, stale setup, saved panel, current panel,
  and final schedule anchor cell.
- Do not use production Sheets as the first validation surface.
