# Rhoboto Runtime Architecture Review

This document preserves the app-runtime findings from the June 25, 2026
architecture review. It is a backlog reference for future runtime work, not a
project setup or harness plan. Re-check the current code before starting any
large refactor.

## Executive Summary

Rhoboto has a sound architecture for its current size: Discord cogs, feature
lifecycle behavior, database models, Google Sheets managers, and parser/content
logic are separated into recognizable layers. The main remaining risks are
concentrated in Google Sheets integration, repeated team/shift workflows,
incomplete Shift Register scheduling behavior, and performance limits from
whole-worksheet reads and writes.

## Current Architecture

- `main.py` initializes logging, validates runtime configuration, discovers cog
  modules, and starts the bot.
- `bot/bot.py` owns startup, shutdown, cog loading, database lifecycle,
  translation setup, and slash command sync.
- `cogs/base/feature_channel_base.py` centralizes channel-scoped enable,
  disable, clear, permission, guard, delete, and help behavior.
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

## Design Risks

- `utils/google_sheets.py` mutates third-party worksheet objects by assigning
  `ws.__class__ = AsyncioGspreadWorksheet`, which depends on gspread internals.
- `to_frame()` reads a whole worksheet, and `update_from_dataframe()` writes a
  whole worksheet. This is simple but will become slow and quota-heavy as data
  grows.
- Google API errors are not centrally classified. Permission, quota, invalid
  URL, missing worksheet, and transient failures should map to distinct domain
  errors and user-facing responses.
- Team and shift cogs repeat setup, worksheet ensure, metadata fetch, locking,
  reaction, and write patterns.
- Each loaded feature checks message state on incoming messages. This is fine at
  low volume but may need caching for active guilds.
- The SQLite event-loop keepalive is a pragmatic compatibility workaround and
  should be revisited as Python, aiosqlite, and Tortoise versions change.

## Unfinished Functionality

- Shift Register supports entry worksheet updates, settings, help, and info
  messages, but draft worksheet and final schedule worksheet generation are not
  complete manager workflows.
- Manual Discord and Google Sheets validation has a runbook, but concrete
  validation results still need to be recorded for a development guild and
  disposable spreadsheet.
- Internationalization is partial. Help and info templates exist, but many
  setup, success, and error messages remain hard-coded in English.
- Settings validation should better cover sheet URL format, spreadsheet access,
  duplicate worksheet titles, empty worksheet titles, and Discord modal limits.
- There is no administrator-facing audit summary for settings changes,
  worksheet creation, hard clear, or destructive user data deletion.

## Google Sheets Improvement Plan

1. Replace worksheet `__class__` mutation with a composition adapter such as
   `WorksheetAdapter`.
2. Add a Google Sheets error boundary with domain errors such as
   `SheetPermissionError`, `SheetQuotaError`, `SheetNotFoundError`, and
   `SheetTransientError`.
3. Add retry/backoff around transient API failures and rate-limit responses.
4. Cache spreadsheet and worksheet metadata by sheet URL for a short TTL.
5. Prefer row-level or range-level updates for upsert/delete operations instead
   of rewriting entire worksheets.
6. Continue storing worksheet IDs in the database; titles should remain display
   and setup input data only.

## Performance Opportunities

- Cache `(guild_id, channel_id, feature_name) -> is_enabled`, invalidated on
  enable, disable, and clear.
- Cache worksheet ID/title metadata to reduce repeated `worksheets()` calls.
- Move user updates toward row-level writes keyed by Discord user.
- Narrow lock granularity after row-level writes are available.
- Avoid opening and authorizing the same spreadsheet repeatedly in hot paths.

## Recommended Priority Order

1. Refactor the Google Sheets wrapper to composition and centralized error
   handling.
2. Add user-facing validation and error messages for sheet setup failures.
3. Define and implement missing Shift Register draft/final schedule workflows.
4. Introduce row-level Google Sheets updates and worksheet metadata caching.
5. Extract shared team/shift upsert lifecycle code.
6. Expand localization and administrator-facing audit messages.
