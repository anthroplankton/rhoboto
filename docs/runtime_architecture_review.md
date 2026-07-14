# Rhoboto Runtime Architecture Review

This document preserves the app-runtime findings from the June 25, 2026
architecture review. It is a backlog reference for future runtime work, not a
project setup or harness plan. Re-check the current code before starting any
large refactor.

## Executive Summary

Rhoboto has a sound architecture for its current size: Discord cogs, feature
lifecycle behavior, database models, Google Sheets managers, and parser/content
logic are separated into recognizable layers. The main remaining risks are
concentrated in repeated team/shift workflows, metadata/open overhead, and
operational retry behavior. The Google Sheets adapter, typed row-local writes,
spreadsheet-scoped value batching, domain error boundary, and complete Shift
Draft/Final Schedule generation described as missing in the original review have
since been implemented.

## Current Architecture

- `main.py` initializes logging, validates runtime configuration, discovers cog
  modules, and starts the bot.
- `bot/bot.py` owns startup, shutdown, cog loading, database lifecycle,
  translation setup, and slash command sync.
- `cogs/base/feature_channel_base.py` centralizes channel-scoped enable,
  disable, clear, permission, guard, delete, and guide behavior.
- `cogs/team_register.py` and `cogs/shift_register.py` implement feature-specific
  parsing, setup views, Google Sheets manager usage, and upsert flows.
- `models/` stores feature state and per-feature Google Sheets settings through
  Tortoise ORM.
- `utils/google_sheets.py`, `utils/manager_base.py`, and feature managers form
  the Google Sheets access layer.
- `utils/*_structs.py` contains parser, metadata, and worksheet content logic.

## Strengths

- `FeatureChannelBase` gives features a consistent lifecycle and permission
  model.
- Database state is simple: `FeatureChannel` anchors channel state, while
  feature configs store sheet URLs and worksheet IDs.
- Managers keep cogs away from direct gspread calls.
- Parser and worksheet content logic are mostly pure Python and Pandas code, so
  they are more testable than Discord interaction code.
- Automated tests now cover parsers, worksheet structures, manager fakes,
  startup behavior, message templates, reactions, and database lifecycle paths.
- CI separates lint, format, pytest coverage, and compile checks.

## Remaining Design Risks

- Spreadsheet-scoped value reads intentionally return complete physical grids to
  reduce request count. Payload grows with worksheet size even though managers
  project only contract-owned data and ignore administrator-owned columns.
- Transient Google API failures are centrally classified but do not yet have a
  general retry/backoff policy.
- Team and shift cogs repeat setup, worksheet ensure, metadata fetch, locking,
  reaction, and write patterns.
- Each loaded feature checks message state on incoming messages. This is fine at
  low volume but may need caching for active guilds.
- The SQLite event-loop keepalive is a pragmatic compatibility workaround and
  should be revisited as Python, aiosqlite, and Tortoise versions change.

## Typed Contract Debt

The Final Schedule follow-up removed `getattr(..., default)` access for declared
`ShiftRegisterConfig` fields from the Shift cog, settings UI, and manager. Those
fallbacks hid incomplete test fakes and could also hide model/schema drift at
runtime. The affected fakes now implement the same required fields as the model.

This was a Shift-focused cleanup, not a project-wide audit. Future debt work
should inspect `getattr`, `hasattr`, broad `object`/`Any` annotations, and defaulted
mapping reads at model, config, and worksheet-contract boundaries. For each use:

- access declared required fields directly and give helpers the narrow concrete
  type or protocol they consume;
- make test fakes satisfy that same contract instead of preserving production
  fallbacks; and
- retain dynamic access only when names are intentionally data-derived, such as
  `Summary` values keyed by `<worksheet title> ISV` and `<worksheet title> Power`,
  or when adapting an external framework object whose shape is genuinely dynamic.

Before removing any compatibility fallback, verify the database/schema rollout
and legacy data contract. Treat required migrations separately rather than
turning a local type cleanup into an implicit behavior change.

## Unfinished Functionality

- Shift Register supports Entry, Draft, and Final Schedule manager workflows,
  settings, guide, and timeline messages.
- Manual Discord and Google Sheets validation has a runbook, but concrete
  validation results still need to be recorded for a development guild and
  disposable spreadsheet.
- Internationalization is partial. Guide and timeline templates exist, but many
  setup, success, and error messages remain hard-coded in English.
- Settings validation should better cover sheet URL format, spreadsheet access,
  duplicate worksheet titles, empty worksheet titles, and Discord modal limits.
- There is no administrator-facing audit summary for settings changes,
  worksheet creation, hard clear, or destructive user data deletion.

## Google Sheets Improvement Plan

1. Add retry/backoff around transient API failures and rate-limit responses.
2. Evaluate spreadsheet and worksheet metadata caching only with explicit
   invalidation rules.
3. Keep spreadsheet-scoped batch reads and typed row-local/grid writes as the sole
   value I/O paths; do not restore worksheet-local compatibility reads.
4. Continue storing worksheet IDs in the database; titles should remain display
   and setup input data only.

## Performance Opportunities

- Cache `(guild_id, channel_id, feature_name) -> is_enabled`, invalidated on
  enable, disable, and clear.
- Cache worksheet ID/title metadata to reduce repeated `worksheets()` calls.
- Measure complete-grid payload cost separately from API request count.
- Avoid opening and authorizing the same spreadsheet repeatedly in hot paths.

## Recommended Priority Order

1. Add retry/backoff and request/payload observability for Google Sheets.
2. Add user-facing validation and error messages for remaining sheet setup failures.
3. Extract shared team/shift upsert lifecycle code where behavior is genuinely
   identical.
4. Expand localization and administrator-facing audit messages.
