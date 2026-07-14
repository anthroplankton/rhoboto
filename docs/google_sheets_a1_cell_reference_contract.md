# Google Sheets A1 Cell Reference Contract

## Status

Known limitation. Keep the current behavior until one project-wide migration is
approved; do not widen an individual command or worksheet field independently.

## Current Contract

User-configurable Google Sheets cell anchors accept one A1 cell reference with a
maximum length of eight characters. Shift Register persists
`final_schedule_anchor_cell` in a `CharField(max_length=8)`, and Final Schedule
generation currently applies the same limit to both its persisted main anchor and
its per-run event-day anchor.

This is narrower than Google Sheets' full grid boundary: `XFD10000000` is a valid
cell reference but has eleven characters. The event-day anchor is not persisted,
but it intentionally remains subject to the same project contract for now.

## Future Migration

Widening this contract must be handled as one migration rather than a local Final
Schedule exception. The change must:

- define one shared A1 cell-reference parser and maximum matching Google Sheets;
- widen every persisted anchor field, including the database schema;
- update setup/settings inputs, command validation, managers, and worksheet
  contracts together;
- remove incompatible fallback validation rather than retain parallel Legacy
  paths; and
- update automated tests, design documentation, and manual integration checks.

Existing shorter anchors remain valid. Until that migration is approved, the
eight-character limit is the canonical project behavior.
