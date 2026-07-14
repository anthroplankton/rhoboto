# Feature Channel Base Refactor Design

## Status

This is the approved architecture design for refactoring the feature-channel
base hierarchy before implementing additional feature-channel types.

- Approved: July 14, 2026
- Four-generic message-base refinement approved: July 14, 2026
- Scope: architecture and behavior-preserving refactor design
- Implementation status: implemented by the July 14, 2026 refactor

Implementation followed a separately reviewed plan. Durable discoveries made
during planning or implementation update the documentation surfaces listed in
[Documentation Maintenance](#documentation-maintenance).

## Goal

Refactor the current `cogs/base/feature_channel_base.py` responsibilities so
that:

- all channel-scoped features share one small lifecycle core;
- message-driven upsert features share their stable message pipeline;
- Team Register and Shift Register retain their complete Google Sheets,
  settings, guide, announcement, reaction, and public-user behavior;
- future non-Sheets features do not inherit meaningless manager, worksheet,
  guide, or announcement contracts; and
- required extension points are explicit, typed, and enforced with
  `abstractmethod` and `override`.

The refactor is a clean cut. It must not retain aliases, compatibility
re-exports, fallback APIs, or parallel legacy/new paths.

## Scope

The refactor covers:

- feature-channel lifecycle inheritance;
- message listener and message context-menu upsert orchestration;
- Team/Shift register-specific base behavior;
- register manager/config contexts;
- Team/Shift public delete and guide behavior;
- module ownership and imports;
- typing, runtime narrowing, state ownership, and error boundaries; and
- automated and manual validation requirements.

## Non-Goals

The refactor does not implement the three future features described below. It
also does not change:

- Discord command names, descriptions, or permission requirements;
- stable feature names or stored identifiers;
- privileged intents;
- the `FeatureChannel` database schema;
- Team/Shift feature config schemas;
- Google Sheets worksheet layouts, columns, IDs, or value contracts;
- announcement, guide, settings, or reaction behavior;
- scheduler semantics; or
- production deployment behavior.

## Planned Feature Summaries

These summaries preserve the architecture-significant requirements currently
known for later feature design and implementation. Details under
[Deferred Future-Feature Questions](#deferred-future-feature-questions) remain
open and are not part of this refactor's behavior contract.

### Public Hourly Shift Summary

Purpose:

- send a public, all-user shift summary at a configured minute of every hour in
  JST; and
- aggregate the guild's Shift Register configuration needed to present event
  day/date, recruitment time range, Final worksheet, and Final Schedule anchor
  information.

Known persistence and UI:

- its own enabled `FeatureChannel` membership;
- a feature-specific config model;
- the minute within each hour, such as minute `45`; and
- an initial setup modal followed by feature-specific settings UI.

This feature does not inherit Google Sheets manager, register guide,
announcement, Latest Guide, or message parser behavior. Any scheduler remains
owned by this feature.

### Room Number Capture And Update

Purpose:

- recognize room-number text such as `[部屋番号【12345】]`;
- update a configured target channel from messages in either the source or
  target channel;
- support a recruitment-message template; and
- later provide a button containing a room-number-aware recruitment template
  and a Twitter intent-post link.

Known persistence and UI:

- the command channel is the sole owner/source;
- a linked target `FeatureChannel` membership in the same guild;
- target Discord channel ID;
- recruitment-template source message ID;
- channel-name format;
- an initial setup modal containing the channel-name format and other initial
  inputs; and
- source-owned settings, disable, and hard-clear operations.

Both source and target participate in message capture and message context-menu
upsert. Only the source owns lifecycle and settings. A target message first
passes an enabled feature-membership check, then the Room feature resolves the
source-owned config.

The Room parser conforms to the shared message parser contract. It can return
ignored or parsed, but does not produce invalid attempts. A parsed room number
still can fail later during owner/target lookup, persistence, channel rename,
Discord permission checks, or rate-limit handling. The parsed result retains
real `UserInfo`, allowing later output to show who changed the room number.

### Administrator Notification Channel

Purpose:

- send administrator-facing reminders in a dedicated feature channel; and
- optionally remind administrators about configured Shift timeline events.

Known persistence and UI:

- its own enabled `FeatureChannel` membership;
- a feature-specific config model;
- reminder lead time, such as five minutes;
- Discord roles and users to mention;
- an enable/disable control for Shift timeline reminders; and
- an initial setup modal followed by feature-specific settings UI.

This feature is independent from the public hourly summary. It does not deliver
the public summary feature's messages. Its scheduler remains feature-owned.

### Capability Matrix

| Capability | Team | Shift | Public summary | Room number | Admin notification |
| --- | --- | --- | --- | --- | --- |
| Channel lifecycle | Yes | Yes | Yes | Yes | Yes |
| Initial setup flow | Yes | Yes | Yes | Yes | Yes |
| Message parser/listener | Yes | Yes | No | Yes | No |
| Message context-menu upsert | Yes | Yes | No | Yes | No |
| Google Sheets manager | Yes | Yes | No | No | No |
| Register guide/Latest Guide | Yes | Yes | No | No | No |
| Feature-owned scheduler | No | Shift timeline | Hourly | No | Reminder |
| Cross-channel relation | No | Team Source reference | No | Source + target memberships | No |

The matrix records architectural ownership, not a promise that every listed
future behavior is already implemented.

## Approved Hierarchy

```text
FeatureChannelBase
├── MessageUpsertFeatureChannelBase[
│       MessageContextT,
│       ConfiguredContextT,
│       SubmissionT,
│       UpsertResultT,
│   ]
│   ├── RegisterFeatureChannelBase[
│   │       ConfigT,
│   │       MetadataT,
│   │       ManagerT,
│   │       SubmissionT,
│   │       UpsertResultT,
│   │   ]
│   │   ├── TeamRegister
│   │   └── ShiftRegister
│   └── RoomNumberFeature
├── PublicHourlyShiftSummaryFeature
└── AdministratorNotificationFeature
```

The hierarchy uses inheritance only for stable is-a relationships. Settings UI
continues to use composition through `components/ui_settings_flow.py`. Do not
introduce setup, settings, parser, guide, scheduler, or error-policy mixins
unless later implementations demonstrate a second genuinely identical
orthogonal capability that cannot be expressed by the approved hierarchy or an
ordinary helper.

## Module Ownership

```text
cogs/base/
├── feature_channel_base.py
├── message_upsert_feature_channel_base.py
├── register_feature_channel_context.py
├── register_feature_channel_base.py
└── register_feature_channel_user_base.py
```

### `feature_channel_base.py`

Owns only generic channel-scoped behavior:

- `FeatureChannelBase`;
- `FeatureNotEnabled` and generic storage-check failure handling;
- administrator/manage-channel permission defaults;
- `/enable`, `/disable`, and `/disable_and_clear` orchestration;
- default single-row enable, soft-disable, and hard-clear persistence;
- enabled-feature predicates and `is_enabled()`; and
- the abstract `setup_after_enable()` contract.

It must not import `ManagerBase`, Google Sheets types, register contexts,
message parsers, guide/announcement helpers, or Latest Guide state.

### `message_upsert_feature_channel_base.py`

Owns the stable Discord message pipeline:

- `MessageUpsertFeatureChannelBase`;
- the parser Protocol and `ParserType` contract;
- `MessageParseStatus` and `MessageParseResult[T]`;
- `MessageUpsertStatus` and `MessageUpsertOutcome[T]`;
- enabled feature-membership lookup for the message's guild/channel;
- bot/DM/channel guards;
- message-author `UserInfo` construction;
- `_parse_message_submission()`;
- received-message logging;
- invalid-message reactions;
- the deterministic typed message-processing template;
- one inherited `on_message` listener; and
- message context-menu construction, permission check, and registration.

`MessageUpsertStatus` and `MessageUpsertOutcome` remove their current leading
underscores because Register and Room consume them across module boundaries.
Their fields and semantics do not change. Do not leave private-name aliases.

### `register_feature_channel_context.py`

Owns typed Google Sheets register context construction:

- `RegisterFeatureChannelContext[ManagerT]`;
- `ConfiguredRegisterFeatureChannelContext[ConfigT, ManagerT]`;
- manager construction using the configured Google service-account path;
- register config lookup; and
- register-specific missing-config copy.

The old `feature_channel_context.py` is deleted. Parse results move to the
message-upsert module, and register contexts receive register-specific names.

### `register_feature_channel_base.py`

Owns Team/Shift administrator behavior:

- `RegisterFeatureChannelBase`;
- Google Sheets and worksheet error policy;
- register setup/current-settings flow;
- feature-specific settings panel hooks;
- configured submission processing hooks;
- processing/success and storage/worksheet reaction timing;
- manual guide and announcement rendering;
- Latest Guide state, refresh, deletion, and persistent buttons; and
- Register implementations of message listener/context-menu wrappers.

### `register_feature_channel_user_base.py`

Owns Team/Shift public-user behavior:

- `RegisterFeatureChannelUserBase`;
- public delete confirmation and execution;
- public guide rendering; and
- the typed `FeatureChannelType` relationship associating a public cog with its
  matching administrator register cog and child-owned sheet lock.

Team, Shift, and all affected tests update imports and inheritance in the same
change. There are no re-exports from old modules.

## Generic Lifecycle Template

`FeatureChannelBase` keeps concrete lifecycle commands and default single-row
persistence. Only behavior every feature must provide is abstract.

```python
class FeatureChannelBase(..., ABC):
    @abstractmethod
    async def setup_after_enable(
        self,
        interaction: Interaction,
    ) -> None: ...

    async def _enable_channel(self, guild_id: int, channel_id: int) -> None: ...
    async def _disable_channel(self, guild_id: int, channel_id: int) -> bool: ...
    async def _clear_feature_settings(
        self,
        guild_id: int,
        channel_id: int,
    ) -> None: ...
```

Shift and the future Room feature override the persistence methods with
`@override`. Other single-owner features use the complete defaults.

### Enable

1. Validate that the current channel may initiate this feature's lifecycle.
2. Enable the feature membership.
3. Send the existing ephemeral enabled response.
4. Call abstract `setup_after_enable()`.

The core shares setup orchestration, not config schemas or modal fields.

### Soft Disable

1. Run lifecycle-owner preflight.
2. Resolve the enabled membership.
3. Perform the feature's disable mutation.
4. Run optional post-disable cleanup.
5. Send the existing response and any cleanup warning.

Register overrides the cleanup hook to disable and delete Latest Guide state
after the feature is disabled.

### Hard Clear

1. Run lifecycle-owner preflight before showing destructive UI.
2. Show the existing confirmation flow.
3. Resolve the feature membership.
4. Run optional pre-clear cleanup.
5. Perform the feature's clear mutation.
6. Send the existing response and any cleanup warning.

Register deletes the prior Latest Guide message before cascade deletion removes
the persisted message state needed to find it.

### Room Ownership Preflight

The lifecycle core exposes one typed preflight hook. The default accepts the
current channel as owner. Room overrides it so a linked target cannot initiate
settings, disable, or hard clear. Room later overrides soft-disable and hard
clear mutations to update both memberships and the source-owned config.

Do not generalize this into a multi-channel manager. Source/target ownership is
a Room model and Room behavior concern.

## Message-Upsert Template

The shared message base is parameterized by unconfigured message context,
configured context, submission, and operation-result types:

```python
class MessageUpsertFeatureChannelBase[
    MessageContextT,
    ConfiguredContextT,
    SubmissionT,
    UpsertResultT,
](FeatureChannelBase):
    ParserType: type[MessageSubmissionParser[SubmissionT]]
```

Its concrete processor preserves the current ordering:

1. Log safe message metadata.
2. Parse the message through the configured parser.
3. Return `IGNORED` when no submission is recognized.
4. Resolve the feature-specific configured context.
5. Return `MISSING_CONFIG` when setup is incomplete.
6. Apply shared invalid-message reactions and return `INVALID` when the parser
   reports invalid attempts.
7. Guard and narrow the parsed submission and `UserInfo` invariants.
8. Invoke the typed configured-submission hook.
9. Return `PROCESSED` with the feature result.

Resolving config before invalid reactions is intentional and preserves current
Team/Shift behavior: an enabled but unconfigured channel remains silent even for
register-like invalid input.

The message base requires five typed extension points:

- `_build_message_context()` adapts an enabled membership row into the
  feature's operation context;
- `_get_configured_message_context()` narrows that context after loading
  feature-specific configuration;
- `_process_configured_message_submission()` performs the typed update;
- `_process_enabled_message()` owns listener-specific error and post-processing
  policy; and
- `_process_context_menu_message()` owns context-menu rendering and error
  policy.

Of these, the only domain hooks called by the deterministic processor itself
are:

```python
@abstractmethod
async def _get_configured_message_context(
    self,
    context: MessageContextT,
) -> ConfiguredContextT | None: ...

@abstractmethod
async def _process_configured_message_submission(
    self,
    message: Message,
    context: ConfiguredContextT,
    submission: SubmissionT,
    user_info: UserInfo,
) -> UpsertResultT | None: ...
```

Register resolves a concrete manager/config context. Room later resolves a
source-owned config plus target context.

Keeping the unconfigured and configured contexts as separate type parameters
preserves one operation-scoped manager across message parsing, configured
processing, and Register's `finally`-based Latest Guide refresh. Rebuilding the
manager after processing would add a database read and a new failure surface;
a lazy resolver callback would hide the same state transition from inheritance
and type narrowing.

The complete listener and context-menu error/rendering policies remain in
feature wrappers. Register needs worksheet, storage, and Latest Guide behavior;
Room needs Discord target and rename behavior. Both wrappers call the same
deterministic processor. Do not build a generic error-rendering framework.

## Parser And Reaction Contracts

`MessageSubmissionParser[T]` keeps the current stable contract:

```python
class MessageSubmissionParser[T](Protocol):
    @classmethod
    def parse_submission(
        cls,
        user_info: UserInfo,
        lines: list[str],
    ) -> SubmissionParseResult[T]: ...
```

`_parse_message_submission()` remains one concrete synchronous adapter method.
It has no I/O and must not remain `async`.

Team and Shift may produce ignored, invalid, or parsed results. Room returns a
room number or no submission and always leaves `invalid_attempts` empty. A
shared status enum does not require every parser to reach every status.

The existing invalid-input helper moves to the message base and is renamed from
`_add_invalid_registration_reactions()` to
`_add_invalid_message_reactions()`. It retains `WARNING_EMOJI` plus
`CONFUSED_EMOJI`. Room inherits but does not call it because its parser does not
produce invalid attempts.

Do not wrap every reaction utility in another base method. Continue using
`add_reaction_if_possible()` and `transition_processing_reaction()` directly.
Each feature owns the moment when processing starts, succeeds, or fails.

## Register Generics And Typed Contexts

The register base binds its complete type relationship:

```python
class RegisterFeatureChannelBase[
    ConfigT: SheetConfigBase,
    MetadataT: GoogleSheetsMetadata,
    ManagerT: ManagerBase[ConfigT, MetadataT],
    SubmissionT,
    UpsertResultT,
](
    MessageUpsertFeatureChannelBase[
        RegisterFeatureChannelContext[ManagerT],
        ConfiguredRegisterFeatureChannelContext[ConfigT, ManagerT],
        SubmissionT,
        UpsertResultT,
    ],
):
    ...
```

The configured context preserves the concrete config type:

```python
@dataclass(frozen=True)
class ConfiguredRegisterFeatureChannelContext[ConfigT, ManagerT]:
    guild_id: int
    channel_id: int
    feature_channel: FeatureChannel
    manager: ManagerT
    feature_config: ConfigT
```

Concrete cogs state the relationship explicitly. For example, Shift binds
`ShiftRegisterConfig`, `ShiftRegisterGoogleSheetsMetadata`,
`ShiftRegisterManager`, `Shift`, and its operation-result type.

This removes:

- `sheet_config: object` settings hooks;
- `cast(ShiftRegisterConfig, context.feature_config)` calls; and
- broad manager/config context annotations.

`ManagerBase` remains a Google Sheets manager. Do not generalize it for reminder
minutes, Discord targets, role/user selections, toggles, or other non-Sheets
settings.

## Typing And Runtime-Narrowing Rules

The refactor treats typing as an acceptance criterion.

- Required subclass behavior uses `@abstractmethod`.
- Every concrete implementation or specialization uses `@override`.
- Required `bot`, `logger`, config, manager, and context fields are accessed
  directly.
- Test fakes implement the same required fields and Protocols as production
  objects.
- `MessageParseResult` checks submission and `UserInfo` invariants once before
  passing narrowed values to processing hooks.
- Discord interaction/message channels use existing source guards plus native
  `Messageable` types or the narrow capability Protocol actually required.
- Wrapped Discord command errors use explicit `isinstance` narrowing before
  reading `.original`.
- Dynamic `getattr` remains only for genuinely dynamic framework capability
  discovery or data-derived field names.
- Cross-module shared contracts use public names. Do not import
  underscore-prefixed symbols from sibling modules.
- Do not add `Any`, broad `object`, defaulted required-field access, or casts to
  compensate for an incomplete contract.

No new type-checker dependency is required by this refactor. Annotations must
remain compatible with Python 3.13 and the repository's Ruff configuration.

## Mutable State Ownership

Mutable state belongs to the lowest class or module that owns its semantics and
lifecycle.

- A generic base may declare a required capability but must not instantiate a
  feature-specific lock, cache, scheduler, or service merely to avoid repeating
  one child declaration.
- Team and Shift own distinct lock instances/references. Because the lock
  instance already namespaces the feature, `channel_id` is a sufficient key;
  a base-owned lock keyed by `(feature_name, channel_id)` duplicates identity at
  the wrong layer.
- `RegisterFeatureChannelUserBase.FeatureChannelType` intentionally associates
  a public cog with its matching administrator cog so both use the same
  child-owned sheet lock.
- A scheduler belongs to the concrete scheduled feature and follows its cog
  load/unload lifecycle.
- Managers and ORM configs are operation-scoped; views and modals are
  interaction-scoped.
- A base may instantiate truly universal per-cog infrastructure, such as its
  bound logger or context-menu object, when it owns the complete lifecycle and
  every subclass has the same contract.

Apply this ownership test to future mutable state instead of blindly placing
all instances at the base, child, or module level.

## Error Ownership

| Layer | Error responsibility |
| --- | --- |
| Lifecycle core | Permissions, `FeatureNotEnabled`, generic DB/storage checks |
| Message-upsert base | Membership lookup failure and parsed-result invariants |
| Register base | Worksheet contract, Google Sheets/storage, Latest Guide cleanup |
| Room feature | Target resolution, Discord permissions, rename/rate-limit failure |
| Unknown failure | Apply the correct internal-failure marker, then re-raise |

Feature-specific wrappers may adapt expected errors to listener reactions or
ephemeral context-menu responses. They must not classify parse-valid execution
failures as invalid input or silently swallow unknown exceptions.

## Persistence Boundaries

`FeatureChannel` remains the lifecycle and membership anchor containing guild,
channel, feature name, and enabled state. It does not become a universal config
or ownership graph.

- Team/Shift configs remain separate models keyed by `FeatureChannel`.
- Each future feature receives its own config model when implemented.
- Room stores source ownership and the linked target membership in its own
  model; both channels have Room feature memberships for listener gating.
- Summary and notification settings remain independent models and features.
- Cross-feature reads, such as public summary aggregation over Shift Register
  configs, do not turn `ManagerBase` into a generic settings repository.

This refactor itself does not require a Tortoise migration.

## Behavior-Preservation Contract

The implementation must preserve:

- Team/Shift slash command names, descriptions, permissions, and feature names;
- context-menu display names;
- exactly one inherited `on_message` listener per Team/Shift cog;
- bot, DM, disabled-channel, and ordinary-message ignore behavior;
- silent invalid input before initial configuration;
- existing invalid, processing, success, worksheet, storage, and internal
  reaction ordering;
- context-menu responses and ephemeral/public visibility;
- Latest Guide refresh and disable/hard-clear cleanup ordering and warnings;
- Team/Shift settings, guides, announcements, and public delete flows;
- the intentional administrator/public sheet-lock relationship; and
- Shift scheduler enable, disable, clear, bootstrap, and deadline behavior.

## Validation

### Automated Coverage

Add or preserve tests proving:

- a core-only test subclass has lifecycle commands but no listener or context
  menu;
- a message-upsert test subclass has one listener and one context menu;
- ignored, invalid, missing-config, and processed outcomes retain their current
  ordering;
- Team/Shift runtime command and listener sets are unchanged;
- all current context-menu, reaction, settings, Latest Guide, public delete, and
  scheduler behavior remains intact;
- Team/Shift settings builders receive concrete config types;
- test doubles satisfy complete typed contracts; and
- no old modules, class names, aliases, or private cross-module imports remain.

### Repository Validation

Use managed-sandbox command forms:

```shell
UV_CACHE_DIR=.cache/uv uv run ruff check --no-fix .
UV_CACHE_DIR=.cache/uv uv run ruff format --check .
UV_CACHE_DIR=.cache/uv uv run pytest
UV_CACHE_DIR=.cache/uv uv run python -m compileall bot cogs components models utils
```

### Manual Validation

After implementation, use `docs/manual_integration_validation.md` to exercise
Team/Shift enable, setup, listener, context menu, settings, guide, disable, hard
clear, public delete, and Shift scheduler behavior in a development guild with
disposable Google Sheets. Add reusable checks to that runbook only when the
implemented behavior exists.

## Deferred Future-Feature Questions

These questions do not block the base refactor. Resolve them in each feature's
own design before changing schema or behavior.

### Public Hourly Shift Summary

- Whether the feature allows one or multiple enabled summary channels per
  guild.
- Exact aggregation rules when Shift Registers are disabled, incomplete, or
  missing Final worksheet metadata.
- Message layout, localization, mentions, and links.
- Missed-run, restart, duplicate-delivery, retry, and idempotency policy.

### Room Number Capture And Update

- Exact normalized grammar, digit-length bounds, and multiple-match precedence.
- Channel-name format grammar, Discord length constraints, and unchanged-name
  handling.
- Template-message ownership, edit/delete behavior, validation, and maximum
  rendered length.
- Target deletion, permission loss, relinking, and source/target disable rules.
- Rename rate-limit, coalescing, retry, and user-facing failure behavior.
- Exact Twitter intent-post content and URL encoding.

### Administrator Notification Channel

- Whether the feature allows one or multiple enabled notification channels per
  guild.
- Exact Shift timeline events and states that trigger reminders.
- Mention limits, role/user precedence, `allowed_mentions`, and deleted-member
  handling.
- Restart, missed-reminder, retry, and idempotency policy.

### All Future Features

- Exact Tortoise schemas and migration/rollout plans.
- Localized command, modal, settings, success, and error copy.
- Settings callback permission and stale-view behavior.
- Manual integration validation scenarios.

## Documentation Maintenance

Keep durable follow-up discoveries in the surface that owns them:

- update this document for approved feature-channel hierarchy, responsibility,
  ownership, or future-feature constraints;
- update `docs/runtime_architecture_review.md` for repository-wide architecture
  or typed-contract principles and debt;
- update `docs/manual_integration_validation.md` after implemented behavior adds
  reusable Discord, Sheets, scheduler, or deployment checks;
- create a feature-specific design or migration plan before changing commands,
  feature names, schema, stored identifiers, or worksheet contracts; and
- keep `.planning/` and other agent working-memory artifacts untracked.

Do not turn incidental implementation notes into durable rules. Record only
verified behavior, approved decisions, reusable constraints, and validation
evidence.
