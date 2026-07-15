# Room Number Capture and Recruitment Template Design

## Status and Scope

This document is the approved behavior and implementation contract for the
Room Number feature. It adds a channel-scoped Discord workflow that records a
Project Sekai room number, updates a configured channel name, and renders an
optional recruitment template with X intent links.

Writing this design does not by itself authorize application implementation,
production database migration, deployment, staging, commits, pushes, pull
requests, integration, branch deletion, or worktree removal. Those operations
remain separately gated by `AGENTS.md` and the approved implementation plan.

## Goals

- Capture a room number from either an owning source channel or its configured
  target channel.
- Persist the current room number independently from recruitment-template
  configuration.
- Rename the target channel using an administrator-configured format.
- Fetch and render a live recruitment-template message with five X intent
  links.
- Support source and target as distinct channels or as the same channel.
- Preserve one source-owned lifecycle and settings surface across both channel
  memberships.
- Make Discord rate-limit waits and partial failures visible without implying
  that successfully persisted room state was lost.
- Prefer the existing FeatureChannel and message-upsert architecture over a
  parallel listener, manager hierarchy, or scheduler.

## Non-Goals

This feature does not add:

- a command alias such as `/room`;
- a manual slash `update` command;
- special support for threads, forum/media channels, announcement channels,
  voice/stage channels, or categories;
- Google Sheets access;
- a privileged intent, environment variable, OAuth scope, or dependency;
- persisted recruitment-template text;
- a template cache or message edit/delete listener;
- a persistent Room output-message ID;
- old target-name restoration;
- a rename cooldown field, hard-coded rate-limit duration, debounce service,
  background delivery worker, or startup reconciliation worker;
- a raw template message-ID input or pointer-clear action; or
- a data backfill for existing features.

## Naming

| Surface | Name |
| --- | --- |
| Cog/class | `RoomNumber` |
| Stable feature identifier | `room_number` |
| Slash command group | `/room_number` |
| Framework display name | `Room Number` |
| Japanese feature label | `部屋番号` |
| Manual room context menu | `部屋番号を設定` |
| Manual template context menu | `募集テンプレに設定` |

The command group inherits `enable`, `settings`, `disable`, and
`disable_and_clear`. It adds no manual room-update slash command because normal
messages and `部屋番号を設定` already cover that operation.

## Architecture

### Inheritance

`RoomNumber` directly extends `MessageUpsertFeatureChannelBase`:

```text
FeatureChannelBase
└── MessageUpsertFeatureChannelBase
    ├── RegisterFeatureChannelBase
    │   ├── TeamRegister
    │   └── ShiftRegister
    └── RoomNumber
```

The shared base continues to own:

- FeatureChannel lifecycle commands and checks;
- enabled membership lookup;
- bot/DM/message guards;
- message-author `UserInfo` construction;
- the room parser adapter and typed message outcome;
- the inherited message listener; and
- construction, permission checks, registration, and unload cleanup for the
  manual room context menu.

The shared base gains only an overridable context-menu name whose default
preserves existing Team/Shift names. Room sets it to `部屋番号を設定`.

Room owns all feature-specific behavior:

- source-owned config resolution from either membership;
- target validation and relinking;
- room persistence and latest-wins coordination;
- recruitment-template automatic and manual capture;
- channel formatting and rename behavior;
- X intent rendering;
- Room embeds, views, reactions, and partial-failure copy; and
- paired membership lifecycle mutations.

Room does not inherit `RegisterFeatureChannelBase`, Google Sheets managers,
register guides, announcements, or Latest Guide behavior. It also does not add
a generic multi-channel manager or shared Room error-rendering framework.

### Ownership and Memberships

```text
Source FeatureChannel ── owns ── RoomNumberConfig
       │                         │
       │                         └── target_channel_id
       │                                   │
       └──────── captures room      Target FeatureChannel
```

- The channel where `/room_number enable` runs is the sole lifecycle and
  settings owner, called **source**.
- The explicitly selected channel whose name and public output are managed is
  **target**.
- Source and target both have `room_number` FeatureChannel memberships and both
  accept automatic room messages and `部屋番号を設定`.
- Only current target accepts automatic template capture and
  `募集テンプレに設定`.
- If source equals target, one FeatureChannel row acts in both roles.
- If source and target differ, the config uses two FeatureChannel rows.
- Apart from same-config source equals target, one guild text channel may not
  participate as source or target in another Room config. Enable and relink
  check both roles transactionally and reject ambiguous reuse ephemerally.
- A linked target cannot own settings, disable, or hard-clear operations.

## Supported Channels and Permissions

### Channel Type

Source and target must both be ordinary guild text channels
(`ChannelType.text` / `GUILD_TEXT`).

- `/room_number enable` rejects unsupported invocation channels ephemerally.
- The target Channel Select lists only ordinary guild text channels.
- Threads, forum/media, announcement, voice/stage, and category channels are
  intentionally unsupported.

### Administrator Permissions

All slash commands, settings buttons, target selects, modals, and both context
menus require the current actor to have both:

- `administrator`; and
- `manage_channels`.

Discord default permissions are only the first gate. Every callback rechecks
both permissions because they may change while a view is open.

Automatic room capture remains available to any non-bot message author in an
enabled source or target. Automatic template replacement is more privileged:
the target-message author must currently have both permissions. Manual
`募集テンプレに設定` trusts the authorized invoking administrator, so the
selected message itself may have another author.

### Bot Target Permissions

Before saving a new or relinked target, check the bot's effective permissions in
that channel:

- View Channel;
- Send Messages;
- Embed Links;
- Read Message History; and
- Manage Channels.

If any are missing, list them in an ephemeral response and mutate neither
config nor memberships. Runtime operations recheck because permissions may
later change. Add Reactions is best effort and does not block setup.

## Persistence

### `RoomNumberConfig`

Add one timestamped source-owned config model and table:

| Field | Contract |
| --- | --- |
| `id` | Integer primary key |
| `feature_channel` | Unique source FeatureChannel relation |
| `target_channel_id` | Unique Discord target-channel snowflake |
| `room_number` | Nullable canonical 5-6 ASCII digits, max length 6 |
| `channel_name_format` | Default `部屋番号【{room_number}】` |
| `recruitment_template_enabled` | Boolean, default `true` |
| `recruitment_template_channel_id` | Nullable Discord source-channel snowflake |
| `recruitment_template_message_id` | Nullable Discord source-message snowflake |
| `created_at` / `updated_at` | Standard timestamp mixin fields |

The template channel/message fields form one atomic pointer. They must both be
null or both be populated. Model/service writes enforce the pair invariant, and
the production migration must include the equivalent database constraint.

The config does not duplicate source channel ID because the source
FeatureChannel relation already owns it. It does not store template text,
output-message IDs, actor identity, a delivery generation, an old channel name,
or a rename timestamp.

The target unique constraint prevents multiple configs from selecting the same
target. A transaction-level cross-role check additionally prevents a channel
that is a source from becoming another config's target, and vice versa.

### Template Pointer Independence

The stored template channel/message pointer remains unchanged when target is
relinked. This permits a live template source in the former target channel.
Automatic or context-menu capture always replaces both pointer fields together.

If the saved message is deleted, edited into invalid content, or temporarily
inaccessible, preserve the pointer and enabled state. Each later room render
retries the live fetch until an administrator selects a valid replacement.

### Schema Migration and Rollout

Fresh databases create the new table from the registered Tortoise model.

Existing deployments must apply a reviewed database-specific migration before
deploying code that queries the model:

1. stop the bot worker and back up the database;
2. create the Room config table with its source foreign key, target uniqueness,
   pointer-pair constraint, defaults, lengths, and timestamps;
3. verify existing FeatureChannel, Team, Shift, and other config tables remain
   unchanged;
4. verify the new table is empty and enforces source uniqueness, target
   uniqueness, pointer pairing, and cascade behavior in a disposable
   transaction;
5. deploy the application code;
6. start the worker; and
7. run startup and Discord integration validation.

No backfill is required because Room is new. `generate_schemas()` remains a
fresh-database path and is not a production migration mechanism. This feature
does not introduce Aerich or claim that one database-specific SQL statement is
portable across every supported `DATABASE_URL`.

For application rollback, stop the worker, deploy the previous version first,
and restart it. The old model registry may temporarily ignore the Room table
while rollback is validated. Dropping the table or removing Room
FeatureChannel rows is a separate, explicitly approved destructive operation.

## Setup, Settings, and Lifecycle

### Initial Enable

`/room_number enable` first creates the source membership and opens the settings
flow. This is an enabled-but-unconfigured state: without a complete Room config,
both room and template listeners remain inert.

The administrator must explicitly select target, including when target should
be source itself. There is no silent self-target default. After validation, one
transaction creates the complete config with:

- selected target;
- default channel format `部屋番号【{room_number}】`;
- null room number;
- recruitment-template handling enabled;
- null template pointer; and
- the required deduplicated target membership.

Before the first template is captured, settings show `有効（未設定）`.

### Settings Panel

The ephemeral settings embed is titled `部屋番号設定` and shows:

```text
Sourceチャンネル
#source

Targetチャンネル
#target

現在の部屋番号
12345

チャンネル名形式
部屋番号【{room_number}】

募集テンプレ
🟢 有効

テンプレ元メッセージ
#target・メッセージを表示・ID: 123456789012345678
```

Missing values use an explicit unset label. The template entry retains the
stored channel/message IDs and jump link even when the source is no longer the
current target or cannot currently be fetched.

The only mutable controls are:

- a Channel Select with placeholder `Targetチャンネルを選択`;
- a `チャンネル名形式を編集` button that opens a modal; and
- a dynamic `募集テンプレを無効化` / `募集テンプレを有効化` button.

There is no raw ID input and no pointer-clear button.

### Recruitment-Template Toggle

Disabling recruitment-template handling:

- preserves both pointer IDs;
- stops automatic template capture;
- skips template fetching and validation during room updates; and
- omits the template field and all X buttons.

Manual `募集テンプレに設定` remains available while disabled so an
administrator can stage a validated pointer. Re-enabling starts using the saved
pointer and resumes automatic capture. Re-enabling is allowed with a missing or
currently invalid pointer so the feature can wait for a new automatic capture.

Changing the pointer never renames a channel or publishes a room embed. An
automatic replacement adds `🔄`; manual replacement replies ephemerally. To
render the changed template immediately, explicitly resubmit the current room
number.

### Target Relink

Relink first validates channel type, channel ownership, feature-role conflicts,
administrator permissions, and bot permissions. One database transaction then:

- changes `target_channel_id`;
- creates/enables the new target membership;
- removes the old target-only membership; and
- preserves room number, channel format, template toggle, and template pointer.

When source was the old target, its source membership remains. When source is
the new target, no duplicate row is created.

After commit, if a current room exists, apply its rendered name to new target.
Do not publish a public recruitment embed and do not restore the old target's
name. If Discord rename fails, retain the committed new link and return an
ephemeral partial-success response. Reselecting target or resubmitting the room
retries application.

### Channel-Format Edit

The modal validates the new format before persistence. After a successful save:

- if no room exists, make no Discord change;
- if the rendered target name is unchanged, skip the API call; and
- otherwise apply the current room to target without a public embed.

If Discord rename fails after commit, retain the new format and return an
ephemeral partial-success response rather than rolling back the administrator's
choice.

### Soft Disable, Re-enable, and Hard Clear

`/room_number disable` is source-owned. It disables both deduplicated
memberships while preserving the full config: room, format, target, template
toggle, and template pointer. Both listeners and context-menu checks become
inactive.

Running `/room_number enable` again from source re-enables the existing source
and target memberships and reopens settings without target reselection.

After confirmation, `/room_number disable_and_clear` deletes the config and
both memberships. It does not restore a Discord channel name, delete existing
Room embeds, or delete the user-authored template message.

## Room-Number Parsing

Automatic room capture and `部屋番号を設定` share one domain parser.

1. Treat the entire message text as the candidate, not individual lines.
2. Apply Unicode NFKC normalization.
3. Remove only leading and trailing Unicode whitespace.
4. Require full match against `[0-9]{5,6}`.

Examples:

| Input | Result |
| --- | --- |
| `12345` | `12345` |
| `１２３４５６` | `123456` |
| ` 12345 ` | `12345` |
| `1234` / `1234567` | ignored |
| `部屋番号【12345】` | ignored |
| `123 45` | ignored |
| `12345\n` | `12345` because the newline is outer whitespace |
| `12345\n募集` | ignored |
| non-ASCII digits not converted by NFKC | ignored |

The parser reports a room or no submission. Nonmatching automatic messages are
silently ignored and do not produce invalid-attempt reactions. Explicit
`部屋番号を設定` returns an ephemeral validation error when the selected message
does not contain one valid whole-message room.

## Restricted Format Grammar

Use `string.Formatter.parse()` as the standard-library parser, then render only
approved fields. Do not call unrestricted user-controlled `str.format()` and do
not add Jinja or another template dependency.

Both formats support standard brace escaping:

- `{{` renders literal `{`;
- `}}` renders literal `}`;
- `{{}}` renders literal `{}`; and
- escaped field-like text does not count as a placeholder.

Reject:

- unmatched braces;
- empty `{}` fields;
- unknown field names;
- numeric fields;
- attribute or index traversal;
- conversions such as `!r`; and
- format specifications after `:`.

Do not NFKC-normalize format literals or recruitment content.

### Channel-Name Format

The only allowed field is `{room_number}`. It is required at least once and may
repeat. Before saving, render with a six-digit sample room and require the final
Discord name to contain 1-100 characters. This rendered check, rather than a
separate literal-length formula, covers repeated fields and escaped braces.

The default is:

```text
部屋番号【{room_number}】
```

### Recruitment Template

Allowed fields are:

- `{room_number}`: required at least once, repeatable; and
- `{people}`: optional and repeatable.

All room occurrences receive the same current room. All people occurrences in
one rendering receive the same value. The bot supplies only an empty string or
one bare digit from `1` through `4`; it never inserts `@` or another symbol.
Template authors own any desired surrounding syntax.

## Recruitment-Template Capture and Source of Truth

### Candidate Grammar

For automatic capture and explicit template selection:

1. remove only whole-message outer whitespace;
2. preserve all internal text and line breaks;
3. inspect the final nonblank line; and
4. require that line to contain `#プロセカ協力` or `#プロセカ募集`.

The hashtag uses the equivalent of:

```python
r"#プロセカ(?:協力|募集)(?!\w)"
```

Python Unicode `\w` makes a following letter, number, or `_` invalid.
Punctuation is accepted, no boundary is required before `#`, and a longer token
such as `#プロセカ募集abc` does not qualify.

Examples:

| Final line | Candidate |
| --- | --- |
| `#プロセカ募集` | yes |
| `募集です #プロセカ募集！` | yes |
| `本文#プロセカ協力` | yes |
| `#プロセカ募集abc` | no |

The hashtag remains part of the template and the eventual X post.

### Automatic Capture

Automatic capture runs only for a message in current target, only while
template handling is enabled, and only when the message author currently has
both administrator and manage_channels. A valid candidate atomically replaces
the channel/message pointer and receives `🔄`.

If a hashtag candidate has invalid format or rendered limits, retain the prior
pointer and add `⚠️📏`. A storage failure retains the prior pointer and adds
`⚠️🛠️`. Do not post public diagnostic text for automatic candidates.

### Manual Capture

`募集テンプレに設定` is target-only and rechecks the invoking administrator's
permissions. It uses the same candidate, format, and length validation, but
returns exact success or validation/storage failure details ephemerally. It may
replace a pointer while template handling is disabled.

### Live Fetch

The pointer, not a text snapshot, is source of truth. For each room output:

- skip the fetch entirely when template handling is disabled;
- fetch the saved message once from its stored source channel;
- trim and revalidate its current content;
- reuse that one fetched text for preview and all five links; and
- discard it after the operation.

There is no edit/delete listener and no cache. Live edits therefore appear on
the next room update, while deletion, permission loss, or invalid edits produce
the safe template-error output without clearing the pointer.

## Recruitment Rendering and X Intents

### Substitutions

Build five renderings from one fetched template:

| Button | `{people}` value |
| --- | --- |
| `Xに投稿` | empty string |
| `1` | `1` |
| `2` | `2` |
| `3` | `3` |
| `4` | `4` |

The empty rendering is also the `ツイ募テンプレ` field preview. All buttons are
link buttons in one row and require no callback or persistent view state.

Build intent URLs as Unicode IRIs. Keep non-ASCII template code points literal
in the Discord button URL, and encode the ASCII query value with
`application/x-www-form-urlencoded` rules: spaces become `+`, while other
reserved or control characters such as line breaks, `#`, `%`, `&`, and `=` are
percent-encoded:

```text
https://x.com/intent/tweet?text=<encoded rendered template>
```

Emoji, variation selectors, ZWJ sequences, and CJK text remain unchanged in the
Discord URL field. The Discord client or browser maps the IRI to a UTF-8
percent-encoded URI when opening it, and X receives the decoded Unicode text.

### Length Validation

Validate at pointer capture and again after each live fetch. Capture-time
validation uses a six-digit sample room and all five people values; runtime
validation uses the actual current room.

For every relevant rendering:

- the empty-people embed preview must fit the 1024-character embed-field limit;
- conservative X weight must not exceed 280, counting ASCII code points as 1
  and every other Unicode code point as 2; and
- every actual IRI string supplied as a Discord link-button URL must fit the
  512-character limit.

This deliberately conservative X count may reject a small set of text that X's
full URL/Unicode algorithm would accept. It avoids a new dependency and never
alters accepted content. Because Discord does not document client-side IRI
normalization, desktop and mobile validation must confirm that Unicode intent
links open X with the complete decoded template.

## Room Update Flow

### Locks and Delivery Generation

Room uses two `KeyAsyncLock` instances keyed by source channel ID:

- a short **state lock** serializes fresh config reads and room writes; and
- a longer **delivery lock** serializes target output and rename work.

The cog also maintains a transient integer delivery generation per source.
After a room save succeeds under the state lock, increment the generation and
capture it in that request. Compare generation, not room text, before and after
delivery. This correctly handles ABA sequences such as
`12345 -> 67890 -> 12345` and rapid same-room messages.

Disable/hard-clear remove the source generation. Cog unload or process restart
naturally clears all generations. The generation is coordination state, not
durable domain state, so it is not added to the database.

Settings mutations that change target or channel format use the same lock order
and invalidate older in-flight delivery before applying the current room. All
paths acquire locks in one documented order to prevent deadlocks.

### Latest-Wins Sequence

For a valid configured automatic or manual room request:

1. add `config.PROCESSING_EMOJI` to the selected/triggering message if possible;
2. acquire the state lock;
3. reload config and membership state;
4. persist the canonical room;
5. increment/capture delivery generation;
6. release the state lock;
7. acquire the delivery lock;
8. reload config and generation;
9. if the request is already stale, remove processing and stop without output;
10. resolve target and build/fetch/validate output;
11. send the public embed before a potentially delayed rename;
12. skip channel edit if target already has the desired name, otherwise await
    `channel.edit(name=...)`;
13. reload config and generation after the Discord call;
14. if superseded, invalidate the already-sent embed, remove processing, and
    let the latest queued request correct target;
15. if still latest and naming succeeded, remove the pending description and
    transition processing to `✅`; or
16. if still latest and naming failed, replace the description with the rename
    error and remove processing without an error reaction.

If the same room is resubmitted after the previous operation completes, it
publishes a fresh embed and skips an unchanged rename. If equivalent requests
are concurrent, only the highest generation completes public latest output.

### Discord Rate Limits

Do not hard-code Discord's rename limit. The locked discord.py client honors
route headers and `retry_after`; Room awaits that native handling.

An edit already issued to Discord and sleeping inside discord.py cannot be
safely cancelled. It may apply an older room briefly. The post-edit generation
check invalidates its output and the latest queued delivery corrects target.
There is no background scheduler or retry clock.

### Restart Boundary

Database persistence and Discord rename are not atomic. If the process stops
after the room commit but before rename, the durable room may be newer than the
channel name. Because no startup worker is introduced, resubmitting the same
room or applying target/format through settings repairs the name.

## Embeds, Buttons, and Reactions

### Normal Output

The fixed title is:

```text
部屋番号【12345】
```

When rename is required, send the embed first with this description:

```text
Discord側のチャンネル名変更回数の制限により、反映まで時間がかかる場合があります。
現在、チャンネル名を更新しています。
```

When the name is already correct, omit the description from the initial send.
After rename succeeds, edit the same message to remove the pending description.
Do not resend a duplicate output.

When a valid template is enabled, add one field:

```text
ツイ募テンプレ
<room template rendered with empty people>
```

Add link buttons in this order:

```text
[ Xに投稿 ] [ 1 ] [ 2 ] [ 3 ] [ 4 ]
```

Use the embed's native timestamp. The footer is:

```text
部屋番号更新：表示名（@username）
```

There is no honorific. Automatic capture uses the room-message author. Manual
`部屋番号を設定` uses the invoking administrator because that user initiated the
state change.

### Template Disabled, Missing, or Invalid

When template handling is disabled, omit both the field and buttons entirely.

When enabled without a pointer, show the `ツイ募テンプレ` field with no
buttons:

```text
募集テンプレが設定されていません。
```

When fetch or validation fails, show the field with no X buttons:

```text
募集テンプレを読み込めませんでした。設定を確認してください。
```

Template failure does not affect rename or `✅`.

### Rename Failure

If the target embed was sent but rename fails, retain valid template content
and buttons and replace the pending description with:

```text
チャンネル名を更新できませんでした。
設定されたチャンネルと、@Bot の「チャンネルの管理」権限を確認してください。
```

`@Bot` is the dynamic bot mention. Do not add `⚠️`, `🛠️`, or another error
reaction to the room trigger. Remove processing and add no `✅`.

### Superseded In-Flight Output

If a request becomes stale after its embed or rename was already in flight,
edit its embed to:

```text
部屋番号【12345】

新しい部屋番号が設定されたため、この募集情報は無効です。
```

Remove the recruitment field and every button. Keep its original footer and
timestamp. Remove processing from that old trigger without adding `✅`.

### Target Output Failure

If target is deleted, inaccessible, or cannot receive the output embed, still
persist the room and attempt any target rename that remains possible.

When source differs and can receive messages, send an error-only fallback there
without template text or buttons:

```text
部屋番号【12345】

部屋番号は保存されましたが、設定された送信先チャンネルを利用できませんでした。
設定内容と、@Bot の「チャンネルを見る」「メッセージを送信」権限を確認してください。
```

Use the dynamic bot mention. If source equals target or source delivery also
fails, log only; do not add an error reaction and do not DM the author.

### Room-Persistence Failure

If the room database write fails:

- remove processing;
- add no success or error reaction;
- do not rename;
- do not fetch/render the template; and
- do not publish target recruitment output.

In the triggering channel, send a safe embed:

```text
部屋番号を更新できませんでした

時間をおいて、もう一度お試しください。
エラー参照ID：`ABC123`
```

The reference is generated through the existing storage-error conventions and
the same reference appears in sanitized logs. If this send also fails, log only.
Membership/config lookup failure before a listener can prove the message belongs
to Room remains log-only, matching the shared base boundary.

### Output Edit Failure

The message returned by the initial send is operation-local only. If users
delete it, or a later success/failure/stale edit fails, log the failure and do
not resend or persist the output ID. Target-naming success still controls `✅`.

## Context-Menu Results

`部屋番号を設定`:

- is available only in enabled source/target memberships;
- parses the selected message with the whole-message room parser;
- attributes output to the invoking administrator;
- uses the same persistence/delivery/latest-wins flow as automatic capture; and
- returns explicit validation or operation status ephemerally.

`募集テンプレに設定`:

- is current-target-only;
- rechecks both administrator permissions;
- validates the selected message immediately;
- atomically replaces both pointer IDs;
- is available even while template handling is disabled; and
- reports exact results ephemerally without publishing a room output.

## Implementation Surface

The expected narrow production surface is:

- `cogs/room_number.py`: Room cog, contexts, lifecycle overrides, listeners,
  context menus, locks/generation, persistence orchestration, and Discord flow;
- `components/ui_room_number.py`: settings view/modal/select/toggle, embed
  builders, link-button view, and feature-local user copy;
- `models/room_number.py`: `RoomNumberConfig`;
- `utils/room_number.py`: room parser, restricted format parser/renderer,
  hashtag candidate parser, conservative length validation, and X URL builder;
- `cogs/base/message_upsert_feature_channel_base.py`: only the overridable
  context-menu name, preserving existing defaults;
- `bot/translator.py`: slash group/command names and descriptions where current
  translator conventions require them; and
- `docs/manual_integration_validation.md`: Room Discord checks and the schema
  rollout verification defined by this design.

Do not create `RoomNumberManager`. One cog plus small pure helpers is sufficient
for one model and one Discord integration.

The exact implementation plan may consolidate tests, but should prefer focused
new Room test modules over adding broad unrelated fixtures to existing files.

## Automated Validation

### Pure Parser and Renderer Tests

- NFKC full-width digits, outer whitespace, 5/6 boundaries, non-ASCII digits,
  wrappers, internal whitespace, and multiline input.
- Standard brace escaping and unmatched braces.
- Required/repeated `{room_number}`.
- Optional/repeated `{people}` and empty/`1`-`4` substitution.
- Rejection of empty/unknown/numeric/traversal/conversion/spec fields.
- Six-digit rendered channel-name 1-100 boundary.
- Final-line hashtag matching, punctuation, no prefix boundary, and Unicode
  word-character suffix rejection.
- Emoji/ZWJ preservation.
- Embed-field, conservative X weight, and every actual encoded-URL boundary.

### Model and Lifecycle Tests

- Fresh-schema CRUD and defaults.
- Unique source and unique target.
- Template pointer pair invariant.
- Source equals target membership deduplication.
- Distinct two-membership setup.
- Cross-role conflict rejection.
- Enabled-but-unconfigured listener behavior.
- Soft disable/re-enable preservation.
- Hard clear cascade without Discord rollback.
- Target relink membership replacement and pointer preservation.
- Target/format partial success after committed settings.

### Cog and UI Tests

- All command and stale-view callback permission checks.
- Target channel-type and bot-permission preflight.
- Source/target room listener and context-menu gating.
- Target-only template capture and menu gating.
- Template toggle, disabled manual preparation, and no pointer clearing.
- Automatic template `🔄`, `⚠️📏`, and `⚠️🛠️` outcomes.
- One live template fetch per room render.
- Five button order, values, URLs, and no-buttons states.
- Same-name rename skip and same-room refresh.
- Pending description before rename and edit after success/failure.
- Storage, target output, template, and rename partial-failure copy.
- Processing/`✅` semantics.
- Two-lock latest-wins queue skipping.
- ABA and rapid same-room generation handling.
- In-flight supersession removes stale field/buttons and has no `✅`.
- Cog unload/context-menu/generation cleanup.
- Existing Team and Shift context-menu names remain unchanged.

Run focused tests during development, then the full managed-sandbox CI command
set from `docs/agent_harness.md`, including lock verification, non-mutating Ruff
checks, full coverage pytest, and compileall.

## Manual Integration Validation

Add durable checks to `docs/manual_integration_validation.md` for:

- enable from a supported/unsupported channel;
- enabled-but-unconfigured behavior;
- explicit self-target and distinct target setup;
- target bot-permission failures and later permission loss;
- administrator permission loss after opening every settings control;
- whole-message ASCII/full-width room parsing from source and target;
- manual room attribution to the invoking administrator;
- repeated same-room output with unchanged rename skipped;
- rapid room changes, rate-limit delay, queued stale skipping, in-flight stale
  invalidation, and final latest target name;
- channel format escaping, validation, immediate application, and partial
  failure;
- automatic/manual template selection, author/invoker permission distinctions,
  and disabled manual preparation;
- template live edits, deletion, permission loss, pointer retention, and later
  recovery;
- all five X buttons on desktop/mobile and emoji rendering;
- target output fallback and safe error references;
- target relink preserving room/format/toggle/pointer without restoring old
  name;
- soft disable/re-enable and confirmed hard clear; and
- application restart after a persisted room but before rename, repaired by
  same-room resubmission or settings.

Schema rollout validation must confirm the new table and constraints, no
backfill, and unchanged existing tables before the cog is enabled in production.

## Completion Criteria

The feature is ready for implementation handoff only when:

- this design has no unresolved behavioral placeholders;
- a file-level implementation plan maps every behavior and migration boundary
  to tests;
- an execution mode is explicitly selected;
- implementation occurs in the approved isolated workspace without touching
  unrelated canonical work;
- automated validation passes; and
- the final handoff reports changed files, schema/deployment notes, manual
  validation, residual crash/rate-limit boundaries, and exact Git operations
  still awaiting approval.
