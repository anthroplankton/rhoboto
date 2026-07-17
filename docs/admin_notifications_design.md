# Admin Notifications Design

## Status

This document records the approved design for the new centralized administrator
notification channel. It defines the first release's Discord commands, settings,
Shift timeline reminder behavior, persistence, delivery recovery, schema rollout,
and validation contract.

Writing this design does not authorize application implementation, a database
migration, deployment, or any Git history operation. Those require a reviewed
implementation plan and the repository's separate execution gates.

## Purpose

Each guild may configure one normal text channel as its centralized
`Admin Notifications` destination. The first release sends advance reminders for
the three persisted Shift Register timeline milestones:

- Submission Deadline;
- Draft Shift Proposal; and
- Final Shift Notice.

The destination is administrator-facing. It is independent from Shift Register's
public timeline announcement, Auto Close, Latest Guide, and message-registration
lifecycle.

## Scope

The first release includes:

- `/admin_notifications` as a new channel-scoped feature command group;
- one destination per guild;
- setup and settings for one shared reminder lead time and one shared mention set;
- one toggle covering all three Shift timeline milestone kinds;
- all persisted Shift Register configurations in the guild as automatic sources;
- persisted occurrence state, restart reconciliation, catch-up, and bounded retry;
- reminder rendering in the guild's configured Announcement Languages;
- event-specific Google Sheets worksheet links; and
- explicit schema migration and validation guidance.

The first release does not include:

- multiple Admin Notifications destinations per guild;
- source-channel or milestone-specific mention settings;
- notification sources other than Shift Register;
- a generic provider interface, registry, event bus, payload format, job queue, or
  polling worker;
- batching simultaneous reminders;
- preview, test-send, or normal move commands;
- thread, forum-post, or voice-channel-text destinations;
- automatic Draft generation, schedule updates, role assignment, or schedule image
  posting;
- a new dependency, environment variable, privileged intent, or Google API scope;
- Google Sheets worksheet, column, value, formula, or ownership changes; or
- strict transactional exactly-once delivery across Discord and the database.

## Naming and Feature Hierarchy

The stable names are:

| Surface | Value |
| --- | --- |
| Command group | `admin_notifications` |
| Stored `FeatureChannel.feature_name` | `admin_notifications` |
| Human-facing display name | `Admin Notifications` |

`AdminNotifications` directly subclasses `FeatureChannelBase`. It does not inherit
`MessageUpsertFeatureChannelBase`, a Register base, a Sheets manager, message
parsing, context-menu upsert, guide behavior, or any other unused capability.

`FeatureChannel` remains the lifecycle and destination-membership row. All
notification-specific preferences and delivery state live in feature-owned models.
No scheduler or settings capability is added to `FeatureChannelBase` or
`ManagerBase`.

## Command Surface

The command group exposes only:

```text
/admin_notifications enable
/admin_notifications settings
/admin_notifications disable
/admin_notifications disable_and_clear
```

The `settings` description is:

```text
Show and edit current feature settings for this channel.
```

The lifecycle commands retain the existing `FeatureChannelBase` semantics and
responses except for the guild-singleton preflight and unavailable-destination
recovery described below.

`settings`, `disable`, and `disable_and_clear` operate only in the stored
destination. Invoking them elsewhere cannot create, move, edit, or clear the guild
singleton. An incomplete reservation in the owning channel makes `settings` show
the setup button again instead of a normal configured panel.

The administrator interaction UI remains English, matching the current Register
settings surfaces. Automatic reminder content alone follows Announcement
Languages. This release does not broaden localization changes across the shared
lifecycle commands.

## Permissions and Destination Requirements

Every command and every settings, modal, toggle, and replacement callback requires
both:

- `Administrator`; and
- `Manage Channels`.

Callbacks recheck permissions at submission time, bind controls to the requesting
administrator where applicable, and reject stale configuration snapshots. An open
view never treats the permissions or state captured when it was rendered as
authoritative.

The destination must be a normal guild text channel. Enable and replacement
preflight require the bot to be able to view the channel and send messages there.
Threads, forum posts, and voice-channel text chat are rejected in the first
release.

`Read Message History` is optional. It improves ambiguous-send recovery but is not
allowed to block otherwise valid reminder delivery.

## Guild Singleton and Destination Recovery

`AdminNotificationsConfig.guild_id` has a unique database constraint. The
application also validates that the duplicated guild ID matches its owning
`FeatureChannel` row. This makes the one-destination-per-guild rule a storage
invariant rather than only a UI check.

Enable creates the `FeatureChannel` and its guild-unique
`AdminNotificationsConfig` reservation atomically. The reservation exists before
the setup modal is submitted, so concurrent enables in different channels cannot
both claim the guild. A null `reminder_lead_minutes` is the single incomplete-setup
state; no additional setup boolean is stored.

Enabling follows these cases:

1. When no guild config exists, the current text channel and incomplete config
   reservation are created atomically, the inherited ephemeral enabled response is
   sent, and initial setup is offered.
2. When the same destination already owns the config, `enable` re-enables its
   `FeatureChannel`, shows the current settings when configured, and reconciles
   reminders.
3. When another usable destination owns the config, enabling is blocked even if
   that destination is currently soft-disabled. The response is:

   ```text
   Admin Notifications is already configured in #channel. Use `/admin_notifications settings` there.
   ```

4. When the stored destination has been deleted, is no longer a normal text
   channel, or the bot can no longer view/send there, enabling in a new valid text
   channel presents a requester-bound destructive-attention confirmation:

   ```text
   ‼️ The configured Admin Notifications channel is unavailable. Replace it with this channel?
   ```

   The buttons are `Replace Channel` and `Cancel`.

Confirmed replacement updates the existing `FeatureChannel.channel_id` within the
same guild, sets it enabled, and preserves the notification config, mentions,
toggle, delivery history, and occurrence nonces. It then reconciles the guild.
There is no normal destination-move command while the existing destination remains
usable.

## Initial Setup

`FeatureChannelBase.enable()` has already consumed the interaction's initial
response before `setup_after_enable()` runs. Admin Notifications therefore reuses
the established lifecycle-compatible handoff:

1. `enable` sends its inherited ephemeral success response;
2. an ephemeral setup view is sent as a follow-up;
3. the setup button rechecks requester, permissions, destination, and stale state;
4. the button opens the modal with `interaction.response.send_modal(...)`; and
5. successful modal submission completes the reserved config and renders settings.

The initial prompt is:

```text
Admin Notifications is not yet configured for this channel. Click below to set up.
```

The setup button and modal use:

| Surface | Text |
| --- | --- |
| Button | `Set Up Admin Notifications` |
| Modal title | `Set Up Admin Notifications` |
| Input label | `Lead Time (minutes)` |
| Default | `10` |
| Placeholder | `1–1440` |

The modal contains no mention selector and no reminder toggle. Successful setup
stores the validated lead value on the reservation, keeps its empty mention set,
and leaves Shift Timeline Reminders disabled. Scheduling starts only after an
administrator explicitly enables the toggle in settings.

An incomplete reservation survives restart and soft disable. Re-enabling its same
destination shows the setup button again. Another usable destination remains
blocked, unavailable-destination replacement moves the same reservation, and hard
clear deletes it.

## Lead-Time Parsing

One small feature-owned parser owns the lead-time grammar. Discord callbacks pass
the raw modal value to it instead of duplicating normalization or range checks.

The parser:

1. applies `unicodedata.normalize("NFKC", value).strip()`;
2. accepts canonical digits only;
3. converts the value to an integer; and
4. requires an inclusive range of 1 through 1440 minutes.

Full-width digits are accepted through NFKC normalization. Signs, decimals, zero,
negative values, unit suffixes, and values above 1440 remain invalid.

Invalid input makes no database change and returns:

```text
⚠️ {CONFUSED_EMOJI} Lead Time must be a whole number from 1 to 1440 minutes. No settings were changed.
```

The implementation uses `config.CONFUSED_EMOJI`; it does not hard-code the custom
emoji's rendered Discord string.

## Settings Panel

The current panel is structurally:

```text
Admin Notifications Settings

Admin Notifications is configured for this channel.
Select mentions or use the buttons below to update reminders.

Notification Channel
#admin-notifications

Lead Time
10 minutes before each milestone

Mentions
@Role @User
or None

Missing Mentions
None

Unmentionable Roles
None

Shift Timeline Reminders
⚫ Disabled

Scheduled Reminders
0
```

After a successful edit, the title is:

```text
Admin Notifications Settings Saved
```

The main view uses two component rows:

1. `MentionableSelect(min_values=0, max_values=25)` with placeholder
   `Select roles or users to mention`;
2. `Edit Lead Time` and the state-appropriate
   `Enable Shift Timeline Reminders` or
   `Disable Shift Timeline Reminders` button.

The mention selector saves immediately and refreshes the same settings panel. The
lead-time button opens `Edit Reminder Lead Time`, prefilled with the saved value.
There is no nested mention editor or separate save/cancel stage.

The settings panel reports the number of currently active scheduled reminder tasks.
An enabled toggle with no future milestone is valid and displays zero without a
warning. Later Shift timeline edits are discovered through reconciliation.

## Mention Selection and Delivery Safety

One global selection applies to every Shift source and all three milestone kinds.
An empty selection still sends reminders without pinging anyone.

Role IDs and user IDs are stored in separate bounded JSON lists. Typed saved
defaults are passed back to the native `MentionableSelect`, including retained
missing IDs, so an administrator can explicitly deselect and remove them. The
combined saved selection may not exceed Discord's native maximum of 25.

The guild's `@everyone` role is always rejected. `@here` is never a selectable
stored target. Template text and source names never expand the allowed ping set.

At settings-save time, any selected role must either be mentionable or be
mentionable by the bot under its current Discord permissions. An invalid selection
does not replace the previous saved selection and returns:

```text
⚠️ {CONFUSED_EMOJI} The selected roles cannot currently be mentioned. Make them mentionable and try again; no settings were changed.
```

If a saved role is deleted, a saved user leaves the guild, or a saved role later
becomes unmentionable:

- retain the typed ID;
- show deleted/departed targets under `Missing Mentions`;
- show resolved but unusable roles under `Unmentionable Roles`;
- skip the unavailable target during delivery without blocking the message; and
- do not mutate settings as a side effect of delivery.

A retained user becomes active again after rejoining. A retained role becomes
active again when it is recreated only if its stored ID still resolves; a newly
created role with a different ID is a different target. A retained unmentionable
role becomes active again when its mentionability or the bot's applicable
permission is restored.

Every send uses an explicit `AllowedMentions` containing only the currently
resolved, permitted saved roles and users, with `everyone=False` and
`replied_user=False`. The source channel mention remains display metadata and does
not ping channel members.

## Persistence

### `AdminNotificationsConfig`

The feature-owned config uses table `admin_notifications_config`:

| Field | Contract |
| --- | --- |
| `id` | Integer primary key |
| `feature_channel_id` | Unique one-to-one FK to `feature_channel`, `ON DELETE CASCADE` |
| `guild_id` | Guild snowflake with a unique constraint |
| `reminder_lead_minutes` | Nullable integer; null only before setup, otherwise application-validated 1–1440 |
| `mention_role_ids` | JSON list of role snowflakes, default `[]` |
| `mention_user_ids` | JSON list of user snowflakes, default `[]` |
| `shift_timeline_reminders_enabled` | Boolean, default `false` |
| timestamps | Existing `TimestampMixin` fields |

The duplicated guild ID is intentional. It provides the feature-specific guild
singleton without changing the shared `FeatureChannel` schema.

The setup modal's displayed default is 10; there is no database default of 10.
Reconciliation ignores an incomplete reservation because its reminder toggle is
false and it has no valid lead value.

### `AdminNotificationDelivery`

Persisted occurrence and delivery state uses table
`admin_notification_delivery`:

| Field | Contract |
| --- | --- |
| `id` | Integer primary key |
| `admin_notifications_config_id` | FK to notification config, `ON DELETE CASCADE` |
| `shift_register_id` | FK to `shift_register`, `ON DELETE CASCADE` |
| `milestone_kind` | Character enum with explicit capacity 32 |
| `milestone_at` | Aware saved Shift milestone datetime |
| `reminder_at` | Aware computed reminder datetime |
| `delivery_nonce` | Stable positive signed 63-bit value |
| `status` | Character enum with explicit capacity 16 |
| `attempted_at` | Nullable aware datetime persisted before a Discord attempt |
| `message_id` | Nullable Discord message snowflake |
| timestamps | Existing `TimestampMixin` fields |

Occurrence identity is:

```text
UNIQUE (
  admin_notifications_config_id,
  shift_register_id,
  milestone_kind,
  milestone_at
)
```

The milestone kinds are:

- `submission_deadline`;
- `draft_shift_proposal`; and
- `final_shift_notice`.

Delivery statuses are:

| Status | Meaning |
| --- | --- |
| `scheduled` | The occurrence is active and no send has been confirmed. |
| `sent` | Discord delivery has been confirmed directly or by nonce readback. |
| `expired` | The occurrence was discovered only after its milestone. |
| `failed` | Delivery ended without success, either from a non-retryable defect or after retry attempts reached the milestone cutoff. |

Sent rows are immutable history until their notification config or Shift Register
config is hard-cleared. The first release adds no pruning job. Superseded unsent
rows may be deleted during reconciliation.

No retry payload, error body, persisted exponential-backoff counter, or generic job
metadata is stored. Backoff count is in memory and restarts at one minute after a
process restart. Persisted occurrence, nonce, attempt, and milestone state still
preserve catch-up, cutoff, and ambiguous-send recovery.

Reconciliation backoff is also in memory. It persists no payload or delta because
every attempt recomputes the complete current guild state.

## Shift Source Eligibility

The source set is every persisted `ShiftRegisterConfig` whose owning
`FeatureChannel.guild_id` matches the notification guild. Source discovery does
not filter on `FeatureChannel.is_enabled`.

This is deliberate: Auto Close or a manual soft disable may end recruitment while
Draft generation, schedule updates, role assignment, and schedule image posting
remain valid before the activity. Hard clear deletes the Shift config and is the
actual end of reminder eligibility.

No source-channel picker or per-source preference is added.

## Reminder Occurrences

For each eligible Shift config, reconciliation considers at most:

- `submission_deadline_at`;
- `draft_shift_proposal_at`; and
- `final_shift_notice_at`.

An unset milestone is skipped without a settings error. For a set milestone:

```text
reminder_at = milestone_at - reminder_lead_minutes
```

Given the current aware UTC time:

- `milestone_at <= now`: create or retain terminal `expired` state and do not send;
- `reminder_at > now`: schedule at `reminder_at`;
- `reminder_at <= now < milestone_at`: schedule an immediate one-time catch-up;
- matching `sent` occurrence: retain history and do not send again.

Changing a milestone datetime creates a new occurrence. A prior sent occurrence
remains immutable history; the new datetime is independently eligible under the
normal future/catch-up rules. A superseded unsent occurrence is canceled and may
be deleted. Clearing a milestone cancels and removes its unsent occurrence.

Changing the global lead time recalculates `reminder_at` for unsent occurrences
only. It never redelivers an unchanged already-sent milestone. A changed milestone
datetime is the only normal path to a new occurrence for the same source and kind.

## Reconciliation Architecture

The notification cog owns:

- one async lock per guild;
- a task map for active delivery occurrences;
- one coalesced reconciliation request/retry path per guild;
- startup/bootstrap reconciliation; and
- cog unload cleanup.

`reconcile_guild(guild_id)` is the sole calculation boundary. Under the guild lock,
it:

1. reads the guild singleton, feature enabled state, and Shift reminder toggle;
2. reads every persisted Shift config in the guild regardless of soft-enable state;
3. computes the desired occurrence set under the rules above;
4. preserves sent history and reconciles unsent rows;
5. cancels obsolete tasks; and
6. schedules one `discord.utils.sleep_until(...)` task per desired unsent
   occurrence.

The whole-guild scan is intentionally O(number of Shift configs), with at most
three desired occurrences per config. It is the smallest consistent design for
the expected guild scale. Add incremental deltas only if measured scale makes this
bounded rebuild inadequate.

Reconciliation is requested:

- after the bot reports readiness, for every configured notification guild;
- after Admin Notifications enable, re-enable, replacement, lead-time change,
  toggle change, soft disable, or hard clear as applicable;
- after a successful Shift timeline save; and
- after a successful Shift Register hard clear.

Shift soft disable does not trigger reconciliation because it does not change
source eligibility. Shift timeline save and hard clear make one narrow optional
call to `request_reconcile_guild(guild_id)` for the affected guild. If the
notification cog is not loaded, the callback is a no-op; startup reconciliation
remains authoritative. No generic event bus or provider protocol is introduced.

Requests are coalesced per guild. A newer request wakes the same retry path rather
than appending event deltas, and the next attempt always reads current database
state. If reconciliation fails, it retries after one minute with exponential
backoff capped at one hour until it succeeds or the cog unloads. This is retry for
an explicit state-change request, not periodic polling.

A successful Shift timeline save or hard clear remains authoritative and is never
rolled back because notification reconciliation failed. Direct Admin Notifications
lead, toggle, and destination changes await their first reconciliation attempt so
the refreshed Scheduled Reminders value is meaningful. If that first attempt
fails, the saved setting remains, the existing generic settings partial-success
response is shown, and the coalesced background retry continues.

Cog loading and Tortoise initialization currently run concurrently in
`Rhoboto.setup_hook()`. The notification bootstrap task therefore follows the
existing scheduler pattern and waits for bot readiness before querying models.
Cog unload cancels and awaits the bootstrap, reconciliation retry paths, and all
delivery tasks before database shutdown.

## Toggle and Feature Lifecycle

Enabling Shift Timeline Reminders is valid even when there are no current future
milestones. It persists `true`, reconciles, and reports zero scheduled reminders.
Future Shift timeline saves schedule work automatically.

Disabling the Shift reminder toggle cancels all pending notification tasks and
sends nothing while disabled. It preserves lead time, mention IDs, sent history,
and feature membership. Re-enabling performs a full guild reconciliation and may
immediately catch up occurrences whose reminder time passed while their milestone
remains future.

Soft-disabling Admin Notifications has the same pause semantics and also retains
the toggle value and persisted delivery state. Settings are available again after
re-enabling the original destination.

`disable_and_clear` cancels tasks before deleting the `FeatureChannel`. Cascades
then delete notification config and delivery rows. A later setup is a new feature
configuration with no retained delivery history.

## Delivery Validation and Retry

At task wake-up and before every retry, delivery re-reads and verifies:

- the notification config still exists;
- its `FeatureChannel` is enabled;
- Shift Timeline Reminders are enabled;
- the destination is still the configured normal text channel;
- the Shift config still exists in the same guild;
- the exact milestone kind and datetime still match the occurrence;
- `reminder_at` still equals the current milestone minus the current saved lead;
- the milestone remains strictly in the future; and
- the delivery row is still unsent.

A stale task whose milestone or computed reminder time no longer matches requests
a fresh guild reconciliation and exits without sending.

The guild lock serializes reconciliation, settings mutations, and the
validate/send/mark transition. Separate occurrences in the same guild remain
separate Discord messages but send sequentially. This deliberately favors simple,
race-free state over concurrent administrator reminders.

At send time, the task reads the latest:

- saved mention IDs and their current guild resolution;
- guild Announcement Languages and order;
- source channel mention;
- milestone datetime; and
- relevant saved spreadsheet and worksheet IDs.

Expected Discord or external delivery failures retry after one minute with
exponential backoff capped at one hour. Every attempt remains bounded by
`milestone_at` and performs the full validation again. When the cutoff is reached:

- an occurrence with prior failed attempts becomes `failed`;
- an occurrence discovered only after the milestone becomes `expired`; and
- no stale reminder is sent after the milestone.

A deterministic template/configuration defect, such as missing tracked localized
copy or content exceeding Discord's limit, is logged and treated as a terminal
internal failure rather than retried as though it were a transient Discord outage.
Tracked template and maximum-length tests make this a pre-deployment failure.

## Discord and Database Crash Boundary

Discord send and the database sent marker cannot be committed atomically. Local
discord.py supports a message nonce but does not expose server-enforced nonce
deduplication. The feature therefore provides ordinary restart idempotency plus
best-effort ambiguous-send recovery, not strict exactly-once delivery.

For each attempt:

1. persist the stable nonce and `attempted_at` before calling Discord;
2. send the message with that nonce and explicit allowed mentions;
3. on normal success, persist `message_id` and `sent` status;
4. after an ambiguous result or restart, perform a bounded recent-history lookup
   around `attempted_at`, restricted to messages authored by this bot;
5. when a matching nonce is found, persist its message ID and mark `sent` without
   another send; and
6. when history cannot confirm the message, retry under the normal cutoff rules.

The readback inspects at most 100 messages beginning one minute before the
persisted attempt time, then filters to this bot and the stable nonce. This bounded
window is sufficient for the expected low-volume dedicated channel. Increase or
paginate it only if measured channel traffic makes recovery misses material.
Missing `Read Message History`, an unavailable destination, or a history API error
does not suppress a valid resend.

This policy prioritizes not silently losing an administrator reminder. A duplicate
remains possible only in the narrow case where Discord accepted the message, the
process stopped before marking it sent, and history cannot confirm the nonce.

## Message Composition

The normal path sends one Discord message per occurrence. The only exception is
the documented ambiguous-send crash window, where recovery may resend because the
first Discord acceptance cannot be confirmed. Each normal message contains:

1. the currently resolved and permitted configured role/user mentions once,
   space-separated and omitted when empty;
2. one localized content block per configured Announcement Language, in saved
   order and separated by normal blank-line spacing; and
3. one `👀 Google Sheets` link button.

The existing Japanese default applies when the guild has no saved Announcement
Languages. The administrator who last edited settings does not determine automatic
message language.

All configured language templates must render successfully before sending. The
approved three-language content plus 25 maximum-width Discord mentions remains
below Discord's 2,000 UTF-16-unit content limit; a regression test protects that
combined maximum.

Simultaneous occurrences are not batched. They send independently and may ping the
same configured targets more than once. Add aggregation only if real guild volume
shows that repeated pings are a practical problem.

The Sheets button label is always `👀 Google Sheets`, with milestone-specific deep
links built from already-saved IDs:

| Milestone | Worksheet |
| --- | --- |
| Submission Deadline | Entry |
| Draft Shift Proposal | Draft |
| Final Shift Notice | Final Schedule |

Link construction reuses the existing Google Sheets URL helper. It performs no
Google Sheets API read or write.

## Localized Reminder Templates

The templates are owned by Admin Notifications and separated by Shift milestone
and language, for example:

```text
resources/messages/admin_notifications/shift/submission_deadline.ja.md
resources/messages/admin_notifications/shift/draft_shift_proposal.ja.md
resources/messages/admin_notifications/shift/final_shift_notice.ja.md
```

Matching `.zh_tw.md` and `.en.md` files complete the nine tracked templates. The
shared mention line and Sheets button are composed outside localized template
content.

### Japanese

Submission Deadline:

```md
## ⏰ 募集締切が近づいています

シフト登録：{{ source_channel }}
募集締切：{{ milestone_full_timestamp }}（{{ milestone_relative_timestamp }}）

提出状況を確認し、締切後に `/shift_register generate_draft` で仮シフトを作成できるよう準備してください。
```

Draft Shift Proposal:

```md
## ⏰ 仮シフト提示が近づいています

シフト登録：{{ source_channel }}
仮シフト提示：{{ milestone_full_timestamp }}（{{ milestone_relative_timestamp }}）

Shift Draft を確認し、必要に応じて `/shift_register generate_draft` で更新してください。
確認後、`/shift_register update_schedule_from_draft` で現行シフトへ反映し、`/shift_register post_schedule_image`（シフト状態：仮）で投稿してください。
```

Final Shift Notice:

```md
## ⏰ 確定シフト提示が近づいています

シフト登録：{{ source_channel }}
確定シフト提示：{{ milestone_full_timestamp }}（{{ milestone_relative_timestamp }}）

現行シフトを最終確認し、必要に応じて `/shift_register update_schedule_from_draft` で Shift Draft の変更を反映してください。
確定後、`/shift_register assign_schedule_role` で対象ロールを更新し、`/shift_register post_schedule_image`（シフト状態：確定）で投稿してください。
```

### Traditional Chinese

Submission Deadline:

```md
## ⏰ 募集截止時間即將到來

班表登記：{{ source_channel }}
募集截止：{{ milestone_full_timestamp }}（{{ milestone_relative_timestamp }}）

請確認登記狀況，並準備在截止後使用 `/shift_register generate_draft` 產生暫定班表。
```

Draft Shift Proposal:

```md
## ⏰ 暫定班表公布時間即將到來

班表登記：{{ source_channel }}
暫定班表公布：{{ milestone_full_timestamp }}（{{ milestone_relative_timestamp }}）

請確認 Shift Draft，並視需要使用 `/shift_register generate_draft` 更新。
確認後，使用 `/shift_register update_schedule_from_draft` 套用至現行班表，再以 `/shift_register post_schedule_image`（班表狀態：暫定）發布。
```

Final Shift Notice:

```md
## ⏰ 確定班表公布時間即將到來

班表登記：{{ source_channel }}
確定班表公布：{{ milestone_full_timestamp }}（{{ milestone_relative_timestamp }}）

請最後確認現行班表，並視需要使用 `/shift_register update_schedule_from_draft` 套用 Shift Draft 的變更。
確認後，使用 `/shift_register assign_schedule_role` 更新指定身分組，再以 `/shift_register post_schedule_image`（班表狀態：確定）發布。
```

### English

Submission Deadline:

```md
## ⏰ Submission deadline approaching

Shift registration: {{ source_channel }}
Submission deadline: {{ milestone_full_timestamp }} ({{ milestone_relative_timestamp }})

Review the submissions and prepare to create the draft shift schedule with `/shift_register generate_draft` after the deadline.
```

Draft Shift Proposal:

```md
## ⏰ Draft shift proposal approaching

Shift registration: {{ source_channel }}
Draft shift proposal: {{ milestone_full_timestamp }} ({{ milestone_relative_timestamp }})

Review the Shift Draft and update it with `/shift_register generate_draft` if needed.
After review, apply it to the current schedule with `/shift_register update_schedule_from_draft`, then publish it with `/shift_register post_schedule_image` (Schedule status: Tentative).
```

Final Shift Notice:

```md
## ⏰ Final shift notice approaching

Shift registration: {{ source_channel }}
Final shift notice: {{ milestone_full_timestamp }} ({{ milestone_relative_timestamp }})

Review the current schedule one last time and apply any Shift Draft changes with `/shift_register update_schedule_from_draft` if needed.
Once confirmed, update the selected role with `/shift_register assign_schedule_role`, then publish it with `/shift_register post_schedule_image` (Schedule status: Confirmed).
```

## Schema Migration and Rollout

Fresh databases create `admin_notifications_config` and
`admin_notification_delivery` from the registered current Tortoise models.

Existing deployments must apply a reviewed database-specific migration before
deploying code that queries either new model:

1. back up the database and stop the bot worker;
2. create `admin_notifications_config` with its one-to-one FK, guild unique
   constraint, nullable lead column, defaults for JSON/toggle fields, and
   timestamps;
3. create `admin_notification_delivery` with both cascading FKs, enum capacities,
   datetimes, nonce, status, nullable fields, occurrence unique constraint, and
   timestamps;
4. verify existing `feature_channel` and `shift_register` rows and schemas are
   unchanged;
5. verify both new tables are empty and enforce their singleton, occurrence, and
   cascade contracts in a disposable transaction;
6. deploy application code; and
7. start the worker and run startup plus Discord validation.

No backfill is required because the feature and both tables are new.
`generate_schemas()` remains the fresh-database path; it is not a safe production
migration mechanism for existing databases. The repository does not introduce
Aerich or another migration framework for this feature. SQLite and non-SQLite
deployments require their reviewed database-specific equivalent rather than one
universal SQL script.

Deployment requires no Google Sheets migration, Discord privileged-intent change,
environment variable, dependency installation, or OAuth-scope expansion.

## Rollback

For an application rollback:

1. stop the worker;
2. deploy the previous application version first; and
3. restart and verify its existing features.

The previous model registry ignores the two new tables, so they may remain while
the rollback is evaluated. Stored `admin_notifications` FeatureChannel rows may
still appear in `/features` even when the cog is absent.

A full destructive cleanup is a separate approved operation. After backup, remove
the `admin_notifications` FeatureChannel rows so cascades remove their feature data,
verify existing Shift rows remain unchanged, and only then drop the two new tables.
Do not make destructive cleanup part of an automatic application rollback.

## Automated Validation

### Models and pure domain logic

Tests must cover:

- config defaults, one-to-one ownership, and unique guild singleton;
- one atomic pre-setup reservation under concurrent enables, nullable-lead recovery
  after restart/soft disable, and hard-clear deletion;
- occurrence uniqueness and both notification/Shift cascade directions;
- NFKC lead parsing and every rejected grammar/boundary case;
- desired occurrences for future, catch-up, expired, null, changed, and cleared
  milestones;
- lead-time edits changing only unsent occurrence schedules;
- all persisted Shift configs, including soft-disabled sources;
- sent-history idempotency and changed-datetime new occurrences;
- explicit allowed mentions, empty selection, `@everyone`, missing targets,
  unmentionable roles, and restored targets;
- the three exact templates in every supported language;
- saved Announcement Language order, one shared mention line, and one message per
  occurrence;
- Entry, Draft, and Final Schedule button destinations; and
- maximum 25-target, three-language content staying within Discord's UTF-16 limit.

### Interactions and scheduler

Focused fake-based tests must cover:

- Administrator plus Manage Channels command and callback checks;
- permission loss, wrong requester, stale view, cancel, and timeout paths;
- normal-text-channel-only enable and destination permission preflight;
- same-channel enable, concurrent pre-setup enable, available singleton block, and
  unavailable replacement;
- setup-button-to-modal flow and default `10`;
- direct MentionableSelect persistence and settings refresh;
- zero-schedule toggle enable;
- soft disable, re-enable catch-up, and hard clear;
- guild reconciliation after Shift timeline save and Shift hard clear;
- startup reconciliation only after readiness;
- coalesced reconciliation requests, wake-up on a newer request, exponential
  reconciliation retry, and retry cleanup on unload;
- successful Shift mutations remaining committed when notification reconciliation
  initially fails, plus generic partial-success handling for direct notification
  settings;
- task schedule, cancel, rebuild, retry, cutoff, and unload without real sleeps;
- per-guild serialization and independent simultaneous occurrences; and
- direct success, restart idempotency, nonce history recovery, unavailable history,
  and the documented duplicate-risk resend branch.

## Manual Integration Validation

Add reusable checks to `docs/manual_integration_validation.md` and exercise them in
a development guild:

1. Enable in a normal text channel, follow the setup button, verify the modal
   defaults to 10, and confirm Shift reminders remain disabled.
2. Save empty, mixed role/user, maximum-size, missing, rejoined, unmentionable, and
   restored targets; verify `@everyone` is rejected and only allowed targets ping.
3. Enable with no future milestones and confirm Scheduled Reminders is zero.
4. Configure multiple Shift sources, including a soft-disabled one, and verify all
   three future milestones create independent reminders.
5. Verify unset milestones are absent, past reminders catch up once while their
   milestone remains future, and past milestones never send.
6. Edit lead time, change and clear milestones, restart the bot, and verify
   rescheduling plus sent-history idempotency.
7. Configure Japanese, Traditional Chinese, and English in different orders; verify
   one Discord message, one mention line, exact block order, and one Sheets button.
8. Verify deadline, Draft, and Final reminders deep-link Entry, Draft, and Final
   Schedule respectively without a Google API call.
9. Remove destination send permission and verify bounded retries stop at the
   milestone; restore permission before cutoff and verify one successful send.
10. Delete or make the destination unusable, verify another usable destination is
    blocked only when the old destination remains usable, then confirm the
    requester-bound replacement flow preserves settings and history.
11. Soft-disable and re-enable Admin Notifications, then disable and re-enable the
    Shift reminder toggle; verify pause, reconciliation, and catch-up behavior.
12. Hard-clear a Shift source and then Admin Notifications; verify the appropriate
    delivery rows cascade and no tasks survive.
13. Remove Administrator or Manage Channels after opening each settings flow and
    verify callbacks make no state change.
14. Inject reconciliation failure after a successful Shift timeline save and after
    a direct notification setting change; verify the Shift write remains committed,
    direct settings report partial success, requests coalesce, and retry eventually
    rebuilds the latest guild state.

## Implementation Verification Commands

Use the managed-sandbox forms documented in `docs/agent_harness.md`. The eventual
implementation plan must include, at minimum:

```shell
env UV_CACHE_DIR=.cache/uv uv run ruff check --no-fix .
env UV_CACHE_DIR=.cache/uv uv run ruff format --check .
env UV_CACHE_DIR=.cache/uv uv run pytest --cov=bot --cov=cogs --cov=components --cov=models --cov=utils --cov-report=term-missing --cov-fail-under=35
env UV_CACHE_DIR=.cache/uv uv run python -m compileall bot cogs components models utils
```

The implementation plan adds exact focused pytest commands after the file layout
is reviewed. Non-mutating checks run before any completion claim.

## Deferred Extensions

The following require separate evidence and design approval:

- another notification source;
- multiple destinations or per-source routing;
- batching reminders or suppressing repeated simultaneous pings;
- thread/forum destinations;
- delivery-history pruning;
- a persistent retry queue or persisted backoff timing;
- server-enforced deduplication if discord.py exposes a supported mechanism; or
- a generic notification provider/event architecture.

The first release should not scaffold any of these paths.
