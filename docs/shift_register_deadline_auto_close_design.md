# Shift Register Deadline Auto Close Design

## Status

The consolidated design in this document is approved as the behavior and rollout
baseline for file-level implementation planning. It does not authorize production
migration, deployment, or Git history operations. Code implementation still
requires a separately selected Rhoboto execution mode.

## Workflow Context

This is a Shift Register feature change affecting Discord settings UI, public
localized announcements, FeatureChannel lifecycle behavior, bot permissions,
runtime scheduling, and Tortoise ORM schema.

The design keeps the scheduler and persistence inside the Shift timeline domain.
It reuses existing FeatureChannel, announcement-language, timeline-value,
auto-guide cleanup, Google Sheets link, and settings-permission contracts. It does
not introduce a general job framework or a new dependency.

## Goal

When an enabled Shift Register reaches its saved Submission Deadline, Rhoboto must:

1. stop accepting Shift registrations;
2. send one closing message containing one embed per configured announcement
   language;
3. preserve access to the Shift Entry worksheet through a shared
   `👀 Google Sheets` link button;
4. disable and delete the Latest Guide Message;
5. prefix the Discord channel name with `〆`; and
6. recover safely across deadline edits, manual lifecycle actions, Discord
   failures, extension unloads, bot restarts, and an already-past deadline.

The implementation must schedule the exact saved deadline. It must not poll once
per minute or add a scheduler package.

## Existing Behavior

- `ShiftRegisterConfig` already stores `submission_deadline_at`,
  `draft_shift_proposal_at`, `final_shift_notice_at`, and the reserved
  `deadline_automation_enabled` flag.
- Shift Timeline settings save the timestamps but do not schedule runtime work.
- `deadline_automation_enabled` defaults to `false` and has no current UI or
  runtime behavior.
- Normal message registration is gated by `FeatureChannel.is_enabled`.
- Manual soft disable preserves feature settings, disables Latest Guide, deletes
  its message when possible, and clears its stored message ID.
- Hard clear removes the channel's FeatureChannel-backed settings.
- Latest Guide renders one embed per configured announcement language, in saved
  order, using `config.DEFAULT_EMBED_COLOR`.
- Latest Guide's `👀 Google Sheets` button links to the configured landing Shift
  Entry worksheet.
- Shift settings callbacks require both `administrator` and `manage_channels` and
  recheck them when a button or modal is submitted.
- Rhoboto loads cogs and initializes Tortoise concurrently in `setup_hook()`.
- Rhoboto currently closes the database before discord.py unloads cogs, so a
  long-lived scheduler needs an explicit shutdown-order correction.
- The repository has no Aerich or equivalent tracked migration module.

## Proposed Behavior

### Auto Close setting

The Shift settings panel adds an `Auto Close` field and toggle. Existing Shift
Register configurations remain disabled by default.

Enabled status:

```text
- 🟢 `Enabled` : Shift Register will be disabled automatically at `2026-08-14 21:00 JST`.
```

The timestamp is the saved Submission Deadline rendered in JST.

Disabled status:

```text
- ⚫ `Disabled` : No automatic close is scheduled. Enable this to disable Shift Register at the saved Submission Deadline.
```

The toggle mirrors Latest Guide styling:

| Current state | Label | Style | Action |
| --- | --- | --- | --- |
| Disabled | `Enable Auto Close` | Primary | Validate and schedule |
| Enabled | `Disable Auto Close` | Secondary | Disable and cancel |

The callback rechecks `administrator` and `manage_channels`, resolves fresh Shift
settings, and never trusts the state shown by an old settings view.

Enabling requires a saved Submission Deadline strictly later than the current
time. A missing or non-future deadline makes no database change and returns:

```text
⚠️ Set a future Submission Deadline in Edit Shift Timeline before enabling Auto Close.
```

Saving Shift Timeline always saves valid parsed timeline input. If Auto Close is
enabled and the saved Submission Deadline becomes missing or non-future, the same
transaction disables Auto Close and removes its active event state. The response
also includes:

```text
⚠️ Auto Close was disabled because Submission Deadline is not set to a future time.
```

If an enabled future deadline changes, the persisted event state is reset and the
sleeping task is replaced. Editing only the day number, event date, draft proposal,
or final notice does not replace an unchanged deadline task; the closing message
reads those values fresh when it runs.

### Settings layout

The settings view assigns explicit Discord rows:

```text
[Disable Latest Guide] [Disable Auto Close]
[Edit Sheet Settings]  [Edit Team Source]
[Edit Recruitment Time Range] [Edit Shift Timeline]
```

Each toggle label changes independently with its current state. The same layout is
used after saves and replacement-panel refreshes.

### Durable Shift timeline event state

Add a Shift-specific Tortoise model:

```text
Model: ShiftTimelineEventState
Table: shift_timeline_event_state
```

The table contains:

| Field | Contract |
| --- | --- |
| `id` | Integer primary key |
| `shift_register_id` | Foreign key to `shift_register`, `ON DELETE CASCADE` |
| `event_kind` | Character enum with explicit `max_length=32` |
| `scheduled_at` | Aware event timestamp |
| `delivery_nonce` | Stable positive signed 63-bit value used for Discord send retries |
| `status` | Character enum with explicit `max_length=16` |
| `message_id` | Nullable Discord message ID |
| timestamps | Existing `TimestampMixin` fields |

There is one row per Shift Register and event kind:

```text
UNIQUE (shift_register_id, event_kind)
```

The current implementation adds and executes only the
`submission_deadline` event kind. Explicit field lengths leave room for the known
`draft_shift_proposal` and `final_shift_notice` names without implementing their
reminder behavior now.

The event-state lifecycle is:

```text
scheduled -> sent -> completed
```

- `scheduled`: the event is active and no Discord message ID has been persisted.
- `sent`: Discord returned a message and its ID has been persisted. Restart
  recovery must not send it again.
- `completed`: required workflow completion was persisted. Startup ignores it.

Enabling or changing the deadline resets the row to `scheduled`, clears
`message_id`, and assigns a fresh nonce. Disabling the automation or clearing the
feature deletes the active row. A completed row may be reset for a later explicit
event cycle.

`deadline_automation_enabled` remains the administrator-facing Auto Close toggle.
The event-state row is operational delivery state, not a second user preference.
The manager updates both in one database transaction.

This model is intentionally Shift-specific. The known deadline, draft proposal,
and final notice events all belong to Shift Register. A generic cross-feature job
model, handler registry, payload format, or worker queue is deferred until another
feature actually has the same lifecycle.

### Schema migration and rollout

Fresh databases create `shift_timeline_event_state` through the registered current
Tortoise models.

Existing deployments must apply a reviewed database-specific migration before
deploying code that queries the new model:

1. back up the database and stop the bot worker;
2. create the table, foreign key, unique constraint, indexes, and timestamp fields
   matching the reviewed Tortoise schema;
3. verify the existing `shift_register` and `feature_channel` rows are unchanged;
4. verify the new table is empty and accepts one state per Shift Register/event
   kind;
5. deploy the application code; and
6. run the startup and Discord validation checklist below.

No normal data backfill is required because the existing automation flag is a
reserved field that defaults to `false`. Startup still repairs drift safely:

- a valid enabled Auto Close without an event-state row receives a new scheduled
  row;
- a missing or invalid deadline with an enabled flag is disabled and logged; and
- a disabled Auto Close does not receive an event-state row.

Do not rely on `generate_schemas()` to alter an existing production database. This
repository supports both SQLite and non-SQLite URLs, so the migration instructions
must not present one database-specific `ALTER` or `CREATE` statement as universal.

For rollback, stop the worker and deploy the previous application version first.
The old version ignores the new table, so the table may remain temporarily and be
dropped only after rollback verification.

### Runtime scheduler

Add a narrow `ShiftTimelineScheduler`. It owns a mapping keyed by Shift Register
and event kind and uses one sleeping `asyncio.Task` for each active event.

Each task calls `discord.utils.sleep_until(scheduled_at)`. Scheduling the same key
cancels and replaces the old task. The scheduler supports only the operations
needed here:

- schedule or replace one event;
- cancel one event;
- cancel all events for one Shift Register; and
- cancel and await all tasks during unload.

It does not use `discord.ext.tasks.loop`, fixed-interval polling, threads, a new
dependency, a generic job registry, or a persistent worker queue.

`cog_load()` starts only a bootstrap task. The bootstrap waits for
`bot.wait_until_ready()` before it queries Tortoise, because database initialization
and extension loading currently run concurrently. It then reads active Shift event
state once and restores sleeping tasks. A past-due event runs immediately.

`cog_unload()` cancels and awaits the bootstrap and all scheduler tasks. Bot
shutdown must unload cogs before closing Tortoise connections. `Rhoboto.close()`
therefore changes to call discord.py shutdown/cog unload before `close_db()`, with
database closure protected by `finally`. Tests must lock down this order.

### Schedule update boundary

The scheduler remains owned by the Shift cog; the manager and UI do not own
runtime tasks.

Shift settings receive one narrow schedule-changed callback from the cog. After a
successful transaction, the callback schedules, replaces, or cancels the relevant
task. If cancellation races a waking task, the task's fresh database validation
makes the old execution a no-op.

No background task attempts to infer settings edits. Immediate rescheduling is an
explicit consequence of a successful toggle or timeline save.

### Deadline execution

At wake-up, the handler re-reads the event row, Shift configuration, FeatureChannel
state, and saved deadline. It proceeds only when all identifiers, event kind,
nonce, status, automation flag, and scheduled timestamp still match.

The deadline workflow is:

1. acquire the existing per-channel Shift write lock;
2. revalidate current database state;
3. atomically set `FeatureChannel.is_enabled=false` while keeping Auto Close and
   the event row pending;
4. release the database transaction and render the latest configured closing
   message;
5. send one Discord message with the event row's stable nonce;
6. persist `status=sent` and the returned Discord `message_id`;
7. best-effort disable/delete Latest Guide and prefix the channel name;
8. atomically set `deadline_automation_enabled=false` and
   `status=completed`; and
9. let the scheduler remove the finished task.

The Shift write lock serializes the deadline transition with in-flight Shift
writes. Registration must recheck FeatureChannel state inside that locked boundary
so a submission that only passed an earlier stale enabled check cannot write after
the close transition wins the lock.

Once the database close transition succeeds, a Discord failure never re-enables
Shift Register. The disabled state is fail-closed.

### Discord nonce and message persistence

Every newly scheduled event receives a random positive signed 63-bit nonce before
the first send. All retries for that event reuse it.

discord.py 2.7.1 sends `enforce_nonce=true` when `Messageable.send(nonce=...)` is
used. Discord deduplicates the same author's matching nonce only within the past
few minutes. The returned existing message is handled like a successful send.

Reference: <https://docs.discord.com/developers/resources/message#create-message>

The nonce narrows, but cannot eliminate, the distributed transaction gap. If
Discord accepts the message and the process remains down beyond Discord's nonce
window before `message_id` is persisted, restart recovery can send one duplicate.
Strict permanent exactly-once delivery is not claimed.

### Failure and retry behavior

Retries are failure-driven delayed tasks, not polling. The delay is:

```text
min(60 * 2**attempt, 3600) seconds
```

This yields 1, 2, 4, 8, and so on minutes, capped at one hour. Retry count is
in-memory and resets after restart; it is not persisted because it is not needed
for correctness.

Before each retry, the handler re-reads current database state. Timeline changes,
toggle disable, manual lifecycle actions, hard clear, and cog unload therefore
stop stale work even if task cancellation races.

Failure boundaries are:

| Failure | Result |
| --- | --- |
| Initial atomic close fails | No Discord side effect; keep `scheduled` and retry |
| Announcement render/send fails | Shift stays disabled; keep `scheduled`; leave Latest Guide available; retry announcement |
| Message-state persistence fails after send | In the same process retry persistence only; do not intentionally resend |
| Process stops before message-state persistence | Restore `scheduled` and resend with the same nonce |
| Latest Guide deletion fails | Log; do not retry or roll back closure |
| Channel rename fails | Log; do not retry or roll back closure |
| Final completion persistence fails | Keep `sent`; retry cleanup/completion without resending announcement |

A successful announcement controls announcement retry. Rename and Latest Guide
cleanup do not. Their failure must not create duplicate closing announcements.

### Manual lifecycle behavior

Manual Shift lifecycle actions remain authoritative:

- Soft disable atomically disables FeatureChannel, sets Auto Close false, and
  deletes active Shift event state before cancelling its task. Existing Latest
  Guide cleanup remains unchanged.
- Hard clear cancels the task and deletes FeatureChannel-backed settings; the new
  event row is removed by foreign-key cascade.
- Manual enable clears any stale, already-due pending Auto Close state and cancels
  its retry. It does not reuse an expired deadline.
- Auto Close is one-shot. Successful completion sets its toggle false.
- Re-enabling Shift Register does not remove the `〆` channel prefix.

The Shift cog should narrowly override the existing enable, soft-disable, and
hard-clear persistence boundaries instead of duplicating slash-command flows or
changing Team Register behavior.

### Channel rename

After the closing announcement is durably marked `sent`, rename the channel using
this idempotent rule:

```text
already starts with 〆 -> unchanged
otherwise             -> 〆 + current_name[:99]
```

Discord channel names currently allow at most 100 characters. An existing
100-character name therefore loses only its final character when the prefix is
added. The original name is not stored, and no later operation infers a reverse
rename.

Rename failure does not block registration closure, announcement completion, or
Latest Guide cleanup.

### Closing message delivery

The closing announcement matches Latest Guide's locale delivery model:

- read the configured announcement languages in saved order;
- render one embed per language;
- send all embeds in one Discord message;
- use `config.DEFAULT_EMBED_COLOR` for every embed; and
- attach one shared `👀 Google Sheets` link button.

The Google Sheets URL is the same gid-aware landing Shift Entry worksheet URL used
by Latest Guide. The link button label remains the product name `Google Sheets`.
The closing view contains no delete button and no Full Guide button.

Extract only the reusable localized embed-rendering loop from
`FeatureChannelBase`. Preserve `_render_auto_guide_embeds()` as a wrapper. Do not
add optional-delete branches to `AutoGuideButtonsView`; the closing message uses a
small Shift-specific link-only view.

The closing renderer reuses `build_shift_timeline_template_values()` so dates,
JST conversion, zero-padding, and locale weekday values remain aligned with Shift
Timeline and Latest Guide.

### Approved Japanese copy

Title with day number:

```text
{{ day_number }}日目｜シフト登録の受付を自動で締め切りました 🙇
```

Title without day number:

```text
シフト登録の受付を自動で締め切りました 🙇
```

Description:

```text
ご提出くださった皆さま、ありがとうございました！
定刻となりましたので、シフト募集を締め切らせていただきます。

- 仮シフト提示：　{{ draft_shift_proposal.day }}日（{{ draft_shift_proposal.weekday }}）{{ draft_shift_proposal.hour }}時
- 確定シフト提示：{{ final_shift_notice.day }}日（{{ final_shift_notice.weekday }}）{{ final_shift_notice.hour }}時
```

Each milestone row is conditional and is omitted when its timestamp is absent.

Footer:

```text
募集締切：{{ submission_deadline.day }}日（{{ submission_deadline.weekday }}）{{ submission_deadline.hour }}時（JST）
```

### Approved Traditional Chinese copy

Title with day number:

```text
第{{ day_number }}天｜班表登記已自動截止 🙇
```

Title without day number:

```text
班表登記已自動截止 🙇
```

Description:

```text
感謝大家登記班表！
募集截止時間已到，班表登記到此結束。

- 暫定班表公布：{{ draft_shift_proposal.day }}日（{{ draft_shift_proposal.weekday }}）{{ draft_shift_proposal.hour }}時
- 確定班表公布：{{ final_shift_notice.day }}日（{{ final_shift_notice.weekday }}）{{ final_shift_notice.hour }}時
```

Each milestone row is conditional and is omitted when its timestamp is absent.

Footer:

```text
募集截止：{{ submission_deadline.day }}日（{{ submission_deadline.weekday }}）{{ submission_deadline.hour }}時（JST）
```

### Approved English copy

Title with day number:

```text
Day {{ day_number }} | Shift registration has been automatically closed 🙇
```

Title without day number:

```text
Shift registration has been automatically closed 🙇
```

Description:

```text
Thank you, everyone, for your submissions!
The submission deadline has been reached, so shift registration is now closed.

- Draft shift proposal: {{ draft_shift_proposal.day }} ({{ draft_shift_proposal.weekday }}) {{ draft_shift_proposal.hour }}:00
- Final shift notice: {{ final_shift_notice.day }} ({{ final_shift_notice.weekday }}) {{ final_shift_notice.hour }}:00
```

Each milestone row is conditional and is omitted when its timestamp is absent.

Footer:

```text
Submission deadline: {{ submission_deadline.day }} ({{ submission_deadline.weekday }}) {{ submission_deadline.hour }}:00 JST
```

### Japanese Discord mockup

```text
┌────────────────────────────────────────────────────────────┐
│ 3日目｜シフト登録の受付を自動で締め切りました 🙇           │
│                                                            │
│ ご提出くださった皆さま、ありがとうございました！          │
│ 定刻となりましたので、シフト募集を締め切らせていただきます。│
│                                                            │
│ - 仮シフト提示：　15日（土）18時                           │
│ - 確定シフト提示：16日（日）20時                           │
│                                                            │
│ 募集締切：14日（金）21時（JST）                            │
└────────────────────────────────────────────────────────────┘
[👀 Google Sheets]
```

The other configured locales appear as separate embeds in the same message.

### Discord permissions and invite rollout

Renaming a guild channel requires effective `MANAGE_CHANNELS`. The currently
documented invite permission integers do not grant it.

Update the documented invite permissions:

| Variant | Current | New |
| --- | ---: | ---: |
| Base | `347200` | `347216` |
| Manage Roles variant | `268782656` | `268782672` |

Existing installations do not gain the permission automatically. Setup and
manual-validation documentation must tell administrators to reauthorize the bot
with the revised invite or grant Manage Channels manually. Channel-specific
overrides may still deny it.

The settings-user permission contract does not change: Auto Close controls require
both `administrator` and `manage_channels`.

## Affected Files

| File | Planned responsibility |
| --- | --- |
| `models/shift_timeline_event_state.py` | New Shift event kind/status enums and durable state model |
| `models/shift_register.py` | Replace the reserved automation description with the active contract |
| `utils/shift_timeline_scheduler.py` | Sleeping task map, cancellation, and failure backoff |
| `utils/shift_register_manager.py` | Atomic toggle, timeline, close, sent, completion, and lifecycle transitions |
| `cogs/shift_register.py` | Scheduler ownership, startup restore, deadline orchestration, rendering, sending, rename, and Shift lifecycle overrides |
| `cogs/base/feature_channel_base.py` | Extract only the existing localized embed loop |
| `components/ui_shift_register.py` | Auto Close field/button, explicit rows, callbacks, warnings, and Sheets-only close view |
| `bot/bot.py` | Unload cogs and scheduler tasks before closing Tortoise |
| `resources/messages/shift/deadline_close/*` | JA, ZH-TW, and EN title/description/footer templates |
| `tests/test_db_models.py` | Model registration, defaults, constraints, cascade, and state transitions |
| `tests/test_shift_timeline_scheduler.py` | Direct scheduling, replace/cancel, past due, retry, and unload behavior |
| `tests/test_message_templates.py` | Exact localized copy, optional rows, fallback titles, and footer |
| `tests/test_ui_permissions.py` | Button state/style/rows, permission recheck, and enable guard |
| `tests/test_feature_channel_interactions.py` | Runtime close, restart state, message/view, cleanup, rename, and failures |
| `tests/test_bot_startup.py` | Database/cog shutdown order |
| `docs/project_setup.md` | Revised invite permissions and existing-installation guidance |
| `docs/shift_register_timeline_migration.md` | New event-state table migration and automatic-close follow-up scope |
| `docs/manual_integration_validation.md` | Discord, restart, permission, locale, and failure checklist |

`components/ui_auto_guide.py` supplies the existing Google Sheets label and button
conventions but does not need a generalized closing-message view.

## Risk Areas

- Discord send and database persistence cannot be one atomic transaction. Stable
  nonce plus persisted message state narrows but does not eliminate duplicate
  delivery after a long process outage.
- A deadline can race a settings edit, manual lifecycle action, or in-flight Shift
  write. Shared locking, database transactions, stable event identity, and fresh
  validation are required at every wake and retry.
- Bot startup loads cogs concurrently with DB initialization. Direct DB access in
  `cog_load()` would race startup.
- Bot shutdown currently closes DB before cog unload. Leaving that order would let
  a scheduler task access closed Tortoise connections.
- Existing installations require a real schema rollout; `generate_schemas()` is
  not an existing-table migration mechanism.
- Missing Send Messages or Embed Links permission delays the announcement while
  Shift remains closed.
- Missing Manage Channels prevents only rename. It must not roll back closure or
  duplicate the announcement.
- Channel names at Discord's limit require deterministic truncation.
- Multiple simultaneously running bot worker processes are not supported by the
  first version. A DB claim lease is required before horizontally scaling this
  scheduler.

## Test Plan

### Model and manager tests

- New model is discovered by `get_model_modules()` and generated in fresh SQLite.
- Event kind and status fields have explicit capacity and the expected defaults.
- Unique Shift Register/event kind constraint is enforced.
- Hard clear cascades event-state deletion.
- Enable transaction writes the flag and scheduled event together.
- Invalid timeline save writes timeline values, disables Auto Close, and removes
  event state together.
- Deadline replacement resets nonce/status/message ID.
- Manual disable and enable cancel pending state atomically.
- Due close sets FeatureChannel disabled without prematurely completing event
  state.
- Sent and completed transitions preserve their invariants.

### Scheduler tests

- A future event sleeps directly until its deadline without periodic queries.
- A past event invokes its handler immediately.
- Scheduling the same key cancels and replaces the old task.
- Cancel one, cancel channel, and unload cancellation are idempotent.
- `CancelledError` is not converted into a retry.
- Failures use 1, 2, 4, 8-minute delays capped at one hour.
- Tests inject time/sleep behavior and never wait in real time.

### Template and UI tests

- JA, ZH-TW, and EN copy renders exactly as approved.
- Day-number titles fall back correctly when day number is absent.
- Draft and final rows are independently conditional.
- The configured deadline always appears in the localized footer with JST.
- Locale embed order matches saved announcement-language order.
- Every embed uses `config.DEFAULT_EMBED_COLOR`.
- The closing view contains only `👀 Google Sheets` and the correct Entry gid URL.
- Settings fields render enabled/disabled text and JST deadline correctly.
- Buttons have the approved labels, styles, and explicit rows.
- Stale-view callbacks recheck permissions and fresh settings.
- Missing/past deadline enable and invalidating timeline-save warnings match exact
  copy and database behavior.

### Runtime interaction tests

- Startup restoration waits for readiness and loads each active event once.
- A due scheduled event disables registration and sends one multi-embed message.
- A `sent` restart state skips announcement and resumes cleanup.
- A `completed` restart state does nothing.
- A changed nonce/deadline or removed event makes an old waking task a no-op.
- Announcement failure leaves Shift disabled, Latest Guide available, and event
  scheduled for retry.
- Send success plus state-write retry does not intentionally resend in-process.
- Auto Guide delete and rename failures complete without announcement retry.
- Manual disable, enable, hard clear, and cog unload cancel pending work.
- A registration racing closure rechecks enabled state inside the shared lock.
- Rename is one-way, idempotent, and truncates an unprefixed 100-character name to
  `〆` plus its first 99 characters.
- Shutdown unloads/cancels cogs before closing the database.

Run focused tests first, then the managed-sandbox forms of Ruff, full pytest with
the repository coverage floor, and compileall defined by the repository runbook.

## Manual Discord UI Checklist

- Confirm existing Shift channels show Auto Close disabled by default.
- Confirm the six settings buttons render in the approved three rows.
- Remove either administrator or manage_channels after opening the panel; confirm
  the callback refuses the change.
- Attempt enable with missing, equal-to-now, and past deadlines; confirm no state
  change and exact warning copy.
- Enable with a future deadline and confirm status time is JST.
- Edit the enabled deadline to another future time and confirm only the new time
  fires.
- Clear or move the enabled deadline into the past and confirm Auto Close disables
  with the exact warning.
- Restart before the deadline and confirm restoration.
- Stop the bot across the deadline, restart, and confirm immediate close.
- Configure JA, ZH-TW, and EN in different saved orders and confirm one message,
  separate ordered embeds, default color, exact copy, conditional milestone rows,
  and localized footer.
- Click `👀 Google Sheets` and confirm the Shift Entry worksheet gid opens.
- Confirm the old Latest Guide is deleted after the closing message is sent.
- Test normal, already-prefixed, and 100-character channel names.
- Remove Manage Channels and confirm registration still closes and the
  announcement completes while rename is logged as failed.
- Remove Send Messages or Embed Links and confirm Shift closes, Latest Guide
  remains temporarily available, and the announcement retries after permission is
  restored.
- Manually disable before the deadline and confirm no later close message.
- Hard clear before the deadline and confirm no later task or state.
- Exercise a pending retry, manually enable Shift, and confirm the old workflow is
  cancelled without removing `〆`.
- Reauthorize an existing installation with the revised permission integer and
  verify effective Manage Channels including channel overrides.

## Implementation Steps

1. Add failing model tests and the `ShiftTimelineEventState` model; document the
   reviewed migration contract.
2. Add failing scheduler tests and implement the small direct-sleep scheduler.
3. Add manager transition tests and implement atomic event/config/FeatureChannel
   persistence under the Shift lock.
4. Add startup, shutdown, and deadline orchestration tests; wire scheduler
   lifecycle into the Shift cog and correct bot shutdown order.
5. Add UI tests; implement Auto Close status, toggle, rows, guards, and explicit
   schedule-change callbacks.
6. Add exact template tests, localized resources, the small base embed-loop
   extraction, and the Sheets-only closing view.
7. Implement closing send, durable sent state, Latest Guide cleanup, channel
   rename, completion, and retry boundaries.
8. Update setup, migration, and manual-integration documentation.
9. Run focused checks, full CI-equivalent validation, and the manual Discord
   checklist before handoff.

## What Will Not Be Touched

- Team Register behavior or settings UI.
- Discord slash command names, context-menu names, or command localization.
- Privileged intents.
- Google Sheets worksheet layout, columns, formulas, stored worksheet IDs, or
  manager API ownership.
- Draft or Final generation behavior.
- Actual draft proposal or final notice reminder delivery.
- Reverse channel rename or original-name persistence.
- Auto Guide delete-button behavior or a generalized AutoGuide view.
- A general cross-feature scheduler, handler registry, background worker, or new
  dependency.
- Multi-worker claim leases or permanent exactly-once guarantees.
- Secrets, runtime databases, logs, generated artifacts, deployment workflow, or
  Git history.

## Approval Status

Approved design decisions:

- localized closing copy and Japanese mockup;
- one ordered multi-locale message with separate embeds;
- default embed color, deadline footer, and shared Sheets link;
- Auto Close status, toggle labels/styles, guards, and settings rows;
- one-way bounded `〆` rename;
- direct sleeping asyncio scheduler with startup restore and complete cancellation;
- durable Shift-specific event-state schema and migration direction;
- fail-closed database behavior and failure-driven retry boundaries; and
- Manage Channels invite and existing-installation rollout.

The consolidated written specification, including the requirement to unload cogs
before closing Tortoise, is approved. Implementation remains unapproved until the
file-level implementation plan is reviewed and a Rhoboto execution mode is
selected.
