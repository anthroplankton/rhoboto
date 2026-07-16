# Shift Notice Design

## Status

This document records the complete approved design and validation contract for
the `shift_notice` feature. The implementation is complete on the isolated
agent branch; final integration and live Discord/Sheets validation remain
outside this branch.

## Goal

Add one guild-level public Shift Notice destination that posts an hourly visual
summary of the handoff between adjacent Final Schedule shifts. The notice must:

- aggregate every complete Shift Register source in the guild, including
  soft-disabled sources;
- resolve overlapping configured hours deterministically;
- read only the Final Schedule data needed for the current boundary and its
  continuity calculations;
- distinguish an active shift with no supporters from a shift cut by using the
  Final Schedule Runner cell;
- show ending, continuing, and starting supporters without pinging them;
- preserve the approved `scripts/local/shift_handoff_v12` visual behavior at
  true 2x size and 192 DPI;
- support Japanese, Traditional Chinese, and English announcement copy; and
- recover from transient pre-send failures without posting duplicate messages
  after an ambiguous Discord send result.

## Scope

This feature includes:

- The independent `shift_notice` FeatureChannel and command group.
- One Shift Notice destination and configuration per guild.
- A persisted minute within each JST hour.
- Setup, settings, soft-disable, hard-clear, and manual resend flows.
- A Cog-owned minute dispatcher and finite per-guild delivery tasks.
- Guild-wide Shift Register source discovery and settings-only source warnings.
- Deterministic source ownership and lazy spreadsheet reads.
- Pure schedule-frame, identity, continuity, handoff, and CUT-window logic.
- A production renderer derived from the v12 local prototype.
- Localized public embeds and a generic localized failure embed.
- Bundled deterministic font and status-emoji assets.
- Focused automated tests, manual integration checks, and ignored local preview
  images.

This feature does not include:

- Editing a Final Schedule or synchronizing Sheet state back to the database.
- Treating a visible Final Schedule time label as authoritative.
- Member notification pings.
- Delivery-history persistence, exactly-once delivery, or multi-worker
  coordination.
- A generic scheduler, notification framework, spreadsheet repository, or image
  cache.
- A custom thread pool, temporary render files, or runtime asset downloads.
- Arbitrary emoji fallback for supporter display names.
- Changes to existing Shift Register worksheet layouts, columns, commands, or
  stored identifiers.
- New environment variables, Google API scopes, or Python dependencies.

## Terminology

- **source**: one guild Shift Register configuration and its configured Final
  Schedule.
- **configured slot**: one hourly interval contained in a source's database
  `recruitment_time_ranges`.
- **physical Final span**: the continuous row axis from a source's earliest
  configured start through its latest configured end, including database gaps.
- **civil time**: the aware JST datetime used for scheduling and Discord embed
  timestamps.
- **event hour**: the source-local `0..30` hour notation used by the event and
  public schedule labels.
- **boundary**: the instant between a previous hourly interval and a next hourly
  interval.
- **active frame**: a Final Schedule row whose Runner cell is nonblank.
- **active-empty frame**: an active frame whose five supporter lanes are blank.
- **CUT frame**: an in-envelope Final Schedule interval with no active Runner, or
  an in-envelope hour with no selected source.
- **outside frame**: an interval outside the guild activity envelope. It is
  inactive but is not a CUT interval.

## Feature and Persistence Contract

### Feature identity

- Stable feature identifier and public command group: `shift_notice`.
- Display name: `Shift Notice`.
- The Cog derives directly from `FeatureChannelBase`; it is not a Shift Register
  subclass and has no registration message listener.
- Guild aggregation remains feature-owned. It must not add a cross-feature
  repository or scheduler capability to `FeatureChannelBase`.

### Configuration model

Add one table named `shift_notice_config` with:

- an integer primary key;
- a one-to-one, cascading relation to its `FeatureChannel` row;
- a guild ID with a database uniqueness constraint;
- nullable integer `minute_of_hour`;
- normal creation and update timestamps.

The unique guild ID is the database-enforced singleton boundary. A Python
Singleton object is neither required nor allowed.

`minute_of_hour` is nullable so claiming a destination and completing time setup
remain separate states. A null value means the feature is not scheduled. The UI
may prefill `45`, but only a successful modal submission persists a value.

### Lifecycle

- `enable` claims the current normal guild text channel as the destination and
  creates or reuses the feature-owned configuration.
- When the minute is null, the setup response and settings panel offer the setup
  modal. No automatic task is scheduled until setup succeeds.
- `disable` is soft: keep the FeatureChannel and Shift Notice configuration, set
  the feature disabled, and cancel any tracked delivery task.
- Re-enabling a soft-disabled destination keeps its minute.
- `disable_and_clear` is hard: delete the feature settings and cascading Shift
  Notice configuration and cancel its task.
- Normal relocation requires hard-clearing the old destination and enabling the
  new one.

### Singleton destination handling

Reuse the established Admin Notifications singleton claim pattern:

- If a usable destination already exists, reject a second claim and direct the
  administrator to its channel/settings.
- If the stored channel is missing or unusable, offer a requester-bound
  destructive `‼️ Replace Channel` confirmation.
- Replacement re-checks permissions and stale state, retains the configured
  minute, moves the feature ownership to the current channel, and enables it.
- Replacement is not offered merely as a convenience for a usable old channel.

The stored destination must be a normal guild `TextChannel`, not a thread or
forum post. Before claiming/replacing it, before any delivery-time Sheets work,
and immediately before sending, require the bot to have:

- `view_channel`;
- `send_messages`;
- `embed_links`; and
- `attach_files`.

An unusable destination stops before Sheets access and is logged. It does not
redirect the notice to another channel.

## Command and Settings UI Contract

### Commands

The public group contains:

- `/shift_notice enable`
- `/shift_notice settings`
- `/shift_notice disable`
- `/shift_notice disable_and_clear`
- `/shift_notice send_latest`

All commands require both `administrator` and `manage_channels`. All settings,
replacement, and edit callbacks re-check both permissions because permissions
may change while a view is open. Callbacks also verify the original requester,
expected channel, and expected configuration `updated_at` value before mutating
state.

Slash command names and descriptions use the existing per-user Discord locale
translator. Guild announcement language settings affect public notice embeds,
not interaction localization.

### Minute setup

Use these English administrator-facing controls:

- Setup button: `Set Up Shift Notice`
- Modal title: `Set Up Shift Notice`
- Input label: `Minute of Each Hour (JST)`
- Placeholder: `0–59`
- Prefilled value: `45`
- Settings field: `Notice Time`
- Persisted display example: `Every hour at :45 JST`
- Edit button: `Edit Notice Minute`

The domain helper `parse_minute_of_hour` owns the input grammar. It applies
Unicode NFKC normalization, strips surrounding whitespace, accepts only a whole
canonical decimal integer, and returns `0..59`. Invalid, missing, signed,
fractional, or out-of-range input is rejected without changing the stored value.

### Settings panel

The settings embed title is `Shift Notice Settings` and includes:

- `Notice Channel`
- `Notice Time`
- `Source Warnings`

Opening settings is database-only and makes no Google Sheets request.

The exact clean state is:

```text
✅ No source warnings.
```

When no Shift Register source rows exist, show:

```text
⚠️ No Shift Register sources are configured.
```

Otherwise, `Source Warnings` has separate groups for:

- incomplete source configurations excluded from ownership; and
- configured slots lost to an older overlapping source.

Identify a source by its Shift Register channel and stable database metadata.
Show enabled sources with `🟢` and soft-disabled sources with `⚫`. Merge adjacent
overlap losses by civil date, losing source, winning source, and consecutive
hour range. Each entry identifies the ignored loser and used winner. These are
Tier 1 configured-slot warnings only; Tier 2 physical-gap selection and runtime
Sheet failures do not appear here.

If the warning content does not fit one ephemeral response, split it losslessly
across continuation embeds or fields. Do not truncate a warning. Settings output
must remain within Discord's per-embed and aggregate message limits.

## Source Discovery and Eligibility

At build time, load all Shift Register configurations for the destination guild,
including configurations whose own FeatureChannel is soft-disabled. A
hard-cleared Shift Register is absent and therefore cannot participate.

A source is complete only when all of the following database-backed values are
usable:

- `event_date`;
- at least one valid, nonempty recruitment time range;
- Google spreadsheet URL/identity;
- Final Schedule worksheet ID; and
- valid Final Schedule anchor cell.

Incomplete sources are excluded before the ownership map is built and appear in
settings warnings. Their spreadsheet is never opened by Shift Notice.

Sort complete sources by:

1. `ShiftRegisterConfig.created_at ASC`;
2. `ShiftRegisterConfig.id ASC`.

Event date, Discord channel age, worksheet position, later settings edits, and
enabled state do not change this priority.

### Civil interval conversion

For source event date `D` and event hour `h`, calculate:

```text
civil_start = JST midnight on D + h hours
```

Hours `24..30` therefore fall on the following civil day. Do not construct a
datetime by reducing the event hour modulo 24.

Each configured range `[start, end)` claims the civil hourly slots whose source
event hours are `start` through `end - 1`.

### Guild activity envelope

The eligible outer envelope is:

```text
envelope_start = earliest configured-slot start among complete sources
envelope_end   = latest configured-slot end among complete sources
```

Every hourly boundary from `envelope_start` through `envelope_end`, inclusive,
is eligible for an automatic notice. Internal holes remain eligible, regardless
of their length. Only disabling Shift Notice stops those internal CUT notices.

No complete source means no envelope:

- automatic delivery is silent;
- no Sheets request is made;
- settings displays a warning; and
- `send_latest` responds ephemerally that no eligible boundary exists.

This state is not interpreted as an unbounded CUT.

### Hourly source ownership

Resolve each civil hourly slot using two tiers:

1. **Tier 1 — configured slot:** complete sources whose configured database
   recruitment ranges contain the slot.
2. **Tier 2 — physical Final gap:** only when Tier 1 is empty, complete sources
   whose continuous earliest-to-latest physical Final span contains the slot.

Within a tier, the oldest sorted source wins. Tier 1 always beats Tier 2 even if
the Tier 2 source is older. If neither tier has a candidate, the in-envelope slot
is a source-less CUT.

Once selected, a source retains ownership for that build:

- a blank Runner does not fall through to another source;
- invalid lane content does not fall through;
- a read failure does not fall through; and
- an older source is never replaced by newer data because its row looks empty.

Only Tier 1 collisions produce settings overlap warnings. For every loser, record
the source that actually won after sorting, then merge consecutive losses with the
same date/loser/winner tuple.

## Final Schedule Read Contract

### Authoritative rectangle

Visible Final Schedule time-range labels are non-authoritative. Shift Notice must
not read, parse, validate, or display time based on those cells.

For a source whose earliest configured event hour is `first_hour`, source event
hour `h` maps to:

```text
row = final_schedule_anchor_cell.row + (h - first_hour)
```

The configured anchor column is Runner. The following five cells are, in order:

1. Encore (`アンコ`)
2. Honso 1 (`本走`)
3. Honso 2 (`本走`)
4. Honso 3 (`本走`)
5. Standby (`待機`)

For every newly needed source, issue a spreadsheet-scoped values batch read for
the source's complete configured Final span, then project only Runner plus these
five lanes and the database-derived row axis. Transported administrator-owned
cells outside that contract are not interpreted.

### Lazy frontier

Build the database ownership map before opening any spreadsheet.

The initial read frontier contains only the selected sources needed for the
previous and next frames at the target boundary. Deduplicate the worksheets, group
newly required worksheets by spreadsheet, and issue one `values.batchGet` request
per spreadsheet for that frontier. Different spreadsheets necessarily use
separate requests.

Expand the frontier only for one of these reasons:

- a person's consecutive duty reaches the leading or trailing edge of a loaded
  source and the immediately adjacent civil hour has another selected source; or
- deciding the visible CUT run or a truncation ellipsis requires an adjacent
  source-owned row.

Group every new expansion frontier by spreadsheet in the same way. A source is
read at most once per notice. Stop a person's traversal at absence, an in-envelope
CUT, outside the envelope, or a slot with no owner.

If an adjacent read is required to calculate a displayed duration, CUT row, or
ellipsis, failure of that read fails the entire payload. Do not publish partial
durations or a shortened card as a fallback.

Acquire the existing deterministic worksheet resource locks through
`worksheet_transactions()`. Acquire the complete-source candidate lock set once,
before the first Sheet request, so a later lazy expansion cannot invert lock
order; acquiring a resource lock does not fetch that worksheet's values. Locks
prevent conflicting bot operations within the process, but reads across separate
spreadsheets are only a best-effort snapshot; there is no distributed or
cross-spreadsheet transaction.

## Frame and Handoff Domain Contract

### Runner authority

Runner is a state sentinel only and is not one of the five displayed supporters.

- Blank Runner: the frame is inactive/CUT; ignore all residual lane names.
- Nonblank Runner with blank lanes: the frame is active-empty.
- Nonblank Runner with one or more lane names: the frame is active-staffed.
- Outside the guild envelope: the frame is structurally outside and inactive,
  but is not a CUT interval.

Reading Runner never updates database recruitment ranges or any Sheet value.

### Boundary classification

For target boundary `B`, compare the previous interval `[B-1h, B)` and next
interval `[B, B+1h)`:

| Previous | Next | Case |
| --- | --- | --- |
| active | active | `TRANSITION` |
| active | inactive or outside | `END` |
| inactive or outside | active | `START` |
| inactive | inactive, with an in-envelope side | `CUT` |

An active-to-CUT boundary is an `END` image and explicitly says that the next
interval is cut. A CUT-to-active boundary is a `START` image. CUT-to-CUT uses the
CUT timeline. When the next side is outside the envelope, do not append the
internal-CUT sentence.

The normal START and END cases can contain five visually inactive lanes when the
Runner says the shift exists but no supporter is assigned.

### Event labels and civil timestamps

Every selected frame carries both its aware civil start and source-local event
hour. Public text and image ranges use those source-local event labels, including
`25時` and `25:00–26:00`. The clock emoji and Discord timestamp use the actual
civil JST instant, so event `25時` uses `🕐` and a next-day `01:00` timestamp.

At a cross-source boundary, previous and next labels come from their respective
selected sources; do not force them to agree if differently dated source
configurations express the same civil instant differently. If one side has no
source, derive its adjacent label from the source-owned side when that remains
within the `0..30` event axis. A wholly source-less CUT uses civil JST hour labels.
No fallback ever uses a Final Schedule label cell.

### Canonical person identity

Resolve each nonblank lane label with the same exact boundary used by
`assign_schedule_role`:

1. A terminal `⟨@username⟩` suffix selects that exact Discord username.
2. Without the suffix, match exact `display_name`.
3. One exact match resolves to the Discord user ID.
4. Multiple exact display-name matches remain one unresolved raw schedule identity
   for continuity, but expand to every exact Discord candidate in the embed.
5. No match remains the exact raw label identity and displays as safely escaped
   text.

Do not fuzzy match, case-fold, or NFKC-normalize natural-language names. A suffix
whose exact username is absent remains unresolved rather than falling back to a
display-name guess.

Two different labels that uniquely resolve to the same Discord user ID are the
same person. A duplicate display-name group remains label-backed because the
schedule does not identify which candidate is intended.

### Ending, continuing, and starting sets

Calculate supporter presence from the five lanes after canonicalization and
before duplicate mention expansion. Role does not affect these sets:

```text
ending    = previous_people - next_people
continuing = previous_people ∩ next_people
starting  = next_people - previous_people
```

An unresolved raw identity still counts as a person. A person who changes roles
appears under Continuing, while the image depicts the role movement. If a person
is accidentally repeated in multiple lanes, count the person once in embed sets
and once per hour for duration calculations.

### Honso column alignment

Roles remain fixed as Encore, three Honso columns, and Standby. Before visual
status classification, reorder only the next frame's three Honso cells. Evaluate
at most the six permutations and choose lexicographically by:

1. fewest shared canonical people changing Honso column index;
2. least total shared-person column distance;
3. fewest changes from the next frame's original order; and
4. stable original permutation order.

The previous frame is never reordered. Cross-role Encore/Honso/Standby movement
remains a visible move or handoff. This is a narrow pairwise adaptation of the
existing Final Schedule Honso-ordering objective; implementation may extract a
small public domain helper but must not import a private helper blindly or change
Final Schedule behavior incidentally.

For a cross-column movement, retain whether the visible diagonal status is on
the source (upper) or destination (lower) endpoint. Right-to-left uses
`↙️継続` on the upper-right source and `継続↙️` on the lower-left destination;
left-to-right uses `継続↘️` on the upper-left source and `↘️継続` on the
lower-right destination. When both endpoints are otherwise empty, show the
movement status at both endpoints. If a lane has people in both frames but the
people differ, keep that lane as `交代` instead. The pinned `2198.png` and
`2199.png` assets remain unchanged.

### Cumulative and remaining hours

For each canonical person shown in the previous frame, cumulative time is the
number of consecutive hourly frames ending at and including the previous frame.
For each canonical person shown in the next frame, remaining time is the number
of consecutive hourly frames starting at and including the next frame.

Continuity is person-based across role changes and source boundaries. Absence,
CUT, a source-less slot, or the outside envelope stops traversal. Duration is an
integer rendered as `Nh`. The same person appearing twice in one frame still adds
only one hour.

## CUT Timeline Contract

The CUT card is focused on the next interval at the target boundary. Include only
the same contiguous CUT run; an active or outside frame ends the run.

Show at most seven hourly rows:

1. include the current CUT interval;
2. take up to three contiguous earlier CUT intervals;
3. take up to three contiguous later CUT intervals;
4. if one side has fewer than three, backfill the unused capacity by continuing
   farther on the other side; and
5. render the chosen rows chronologically.

If the contiguous run continues above or below the visible seven rows, render
`…` at the corresponding card edge. The ellipsis is an edge marker and does not
consume one of the seven hour rows. Detecting an ellipsis may require inspecting
one additional adjacent interval through the lazy frontier.

The CUT card banner title marks the visible range with the horizontal U+2026
ellipsis when the run is truncated:

```text
14–21｜シフトカット
…14–21｜シフトカット
14–21…｜シフトカット
…14–21…｜シフトカット
```

Build the range from the chronological first visible start and last visible end,
without numerically sorting event-hour labels. Thus a visible run crossing
source-local day notation, such as `26–28` followed by `4–6`, is titled
`…26–6…｜シフトカット` when both edges continue. The highlighted next-time
band keeps its `この時間｜シフトカット` marker, and source/channel names are
not shown publicly.

The CUT banner and current-row marker share one separator axis. Position that
axis from the measured widths of both complete visible text groups so their
combined visual center stays balanced in the schedule area, including the
`…` title forms. The blank left banner cell uses the CUT header background as a
continuous band; normal-card label cells keep their existing background.

At v12 logical scale, a seven-row CUT card is `986 × 400`; the production image
is `1972 × 800`.

## Renderer and Asset Contract

Treat `scripts/local/shift_handoff_v12` as the behavioral and visual reference,
not as production architecture. Port the smallest production subset and preserve:

- geometry and palette;
- lane order and Japanese lane labels;
- status vocabulary and movement arrows;
- START, TRANSITION, END, and CUT card appearance;
- inactive-lane treatment;
- cumulative and remaining-hour placement; and
- long-name behavior: shrink first, then use a middle ellipsis.

The approved production differences are limited to:

- explicit Runner-derived active state;
- canonical-person Honso pre-alignment;
- event hours through 30;
- the seven-row CUT window and edge ellipses;
- true 2x raster dimensions and 192-DPI metadata;
- a bundled Noto font; and
- bundled PNG Twemoji status assets.

Do not port the prototype CLI, catalog generator, review wireframes, SVG rendering
stack, or prototype-only dependency files.

### Raster output

- Normal logical card width: `986 px`.
- Normal physical card width: `1972 px`.
- Double every coordinate, dimension, border, font size, and icon placement from
  the logical v12 design.
- Save PNG DPI metadata as `(192, 192)`.
- Render off the event loop with `asyncio.to_thread()`.
- Keep the resulting PNG in memory and attach it with
  `discord.File(BytesIO(...))`.
- Validate the final byte length against `guild.filesize_limit` before sending.
- Do not add a custom executor, temporary file, or image cache.

### Font and emoji assets

Bundle the full `NotoSansCJKjp-VF.otf` variable font, approximately 29.3 MB, and
use its regular and bold axes through Pillow. Bundle its OFL license beside the
asset.

Bundle the six fixed official Twemoji 17.0.3 `72x72` PNG assets used by the v12
status vocabulary and include the required CC-BY attribution. They are `🔃`
(`1f503`), `↘️` (`2198`), `↙️` (`2199`), `⏬` (`23ec`), `⏹️` (`23f9`), and `⬇️`
(`2b07`). Pin the upstream version and record the vendored asset origin in the
implementation change.

Pillow is already a dependency. Do not add `resvg`, an emoji package, or runtime
font discovery/downloads. Arbitrary emoji in supporter names receive no special
fallback: keep the name unchanged, allow an unsupported glyph to render as tofu,
and rely on the embed identity as authoritative.

## Public Discord Message Contract

### Message shape

Resolve public languages through the existing announcement-language helper and
preserve the returned fallback and ordering behavior. One delivery uses one
Discord message with:

- one PNG attachment for a normal payload;
- one localized embed per configured guild announcement language, in configured
  order;
- the attachment image displayed only by the first embed; and
- `AllowedMentions.none()`.

Later language embeds contain the same localized state and member data without a
second image reference. A failure delivery is image-free and contains one
localized failure embed per configured language.

Each embed uses three possible fields, all `inline=False` and in this fixed order:

1. Ending
2. Continuing
3. Starting

Field applicability is structural:

- previous frame active: show Ending;
- both frames active: show Continuing;
- next frame active: show Starting.

Every applicable field is present even when its set is empty; use the localized
empty value. CUT-to-CUT has no fields. Thus an active-empty to active-empty
transition shows all three fields with empty values. A transition may show only
Starting or only Ending as nonempty while the other applicable fields explicitly
answer that nobody continues or leaves.

Render resolved candidates as `<@user_id>`, separated compactly by `、`. Expand
every exact duplicate display-name candidate. Render unresolved names as escaped
raw text. Because allowed mentions are disabled, these references are visual and
clickable but produce no ping.

### Discord limits

Validate title, description, footer, field-name, field-value, per-embed, embed
count, aggregate-embed, attachment, and message limits before calling
`channel.send`. Measure text with the project's Discord-compatible UTF-16 helper.

In particular, if duplicate-name expansion makes one logical field exceed 1024
UTF-16 units, or all localized embeds exceed an aggregate message limit, treat
the normal payload as a deterministic failure. Do not truncate names, collapse
duplicate candidates, split one logical group into continuation fields, or send
multiple public messages.

### Clock and metadata

Map the target boundary's civil JST hour to the ordinary clock-face emoji:

```text
00/12 -> 🕛, 01/13 -> 🕐, ..., 11/23 -> 🕚
```

Normal embeds use the target boundary aware datetime as `Embed.timestamp`, not
render completion or send time. This anchors source-local `25時` labels to the
correct next-day civil date.

## Localized Copy

Substitute the actual previous and next source-local event-hour labels described
above. The examples show a 13-to-14 boundary.

### Japanese

Normal title:

```text
🕑 14時｜シフト交代インフォ
```

Descriptions:

- TRANSITION:
  `13時のシフトが終わり、14時のシフトが始まる時点での交代内容と、その前後のシフト状況です。`
- START:
  `14時のシフトが始まる時点での交代内容と、その前後のシフト状況です。`
- END:
  `13時のシフトが終わる時点での交代内容と、その前後のシフト状況です。`
- CUT first line:
  `13時から14時へ切り替わる時点でのシフト状況です。`
- When the next in-envelope interval is CUT, append:
  `14–15時はシフトカットです。`
- When the next frame is active-empty, append:
  `次枠に支援者様がいません。`

Fields and empty value:

```text
⏹️ 終了
⏩ 継続
▶️ 開始
なし
```

Normal footer:

```text
シフト時刻：JST｜敬称略
```

Automatic failure title, description, and footer:

```text
⚠️ 14時｜シフト交代インフォ
シフト交代情報を表示できませんでした。
管理者は /shift_notice send_latest で再送できます。
シフト時刻：JST
```

The failure embed has no image, member fields, technical cause, or `敬称略`.

### Traditional Chinese

Normal title:

```text
🕑 14時｜換班資訊
```

Descriptions:

- TRANSITION:
  `13時的班次結束、14時的班次開始時，以下是換班內容及前後班次狀況。`
- START:
  `14時的班次開始時，以下是換班內容及前後班次狀況。`
- END:
  `13時的班次結束時，以下是換班內容及前後班次狀況。`
- CUT first line:
  `以下是13時至14時交界的班次狀況。`
- When the next in-envelope interval is CUT, append:
  `14–15時無排定班次。`
- When the next frame is active-empty, append:
  `下一班沒有支援者。`

Fields and empty value:

```text
⏹️ 結束
⏩ 繼續
▶️ 開始
無
```

Normal footer:

```text
班次時間：JST｜敬稱從略
```

Automatic failure title, description, and footer:

```text
⚠️ 14時｜換班資訊
無法顯示換班資訊。
管理員可使用 /shift_notice send_latest 重新發送。
班次時間：JST
```

### English

Normal title:

```text
🕑 14:00｜Shift Handoff Info
```

Descriptions:

- TRANSITION:
  `This shows the handoff as the 13:00 shift ends and the 14:00 shift begins, together with the surrounding shift status.`
- START:
  `This shows the handoff as the 14:00 shift begins, together with the surrounding shift status.`
- END:
  `This shows the handoff as the 13:00 shift ends, together with the surrounding shift status.`
- CUT first line:
  `This shows the shift status at the 13:00–14:00 boundary.`
- When the next in-envelope interval is CUT, append:
  `No shift is scheduled for 14:00–15:00.`
- When the next frame is active-empty, append:
  `No supporters are assigned to the next shift.`

Fields and empty value:

```text
⏹️ Ending
⏩ Continuing
▶️ Starting
None
```

Normal footer:

```text
Shift time: JST｜Honorifics omitted
```

Automatic failure title, description, and footer:

```text
⚠️ 14:00｜Shift Handoff Info
Shift handoff information could not be displayed.
Administrators can resend it with /shift_notice send_latest.
Shift time: JST
```

English source-local event labels keep the event axis, for example
`25:00–26:00`; they are not converted to `01:00–02:00` in public copy.

## Scheduling and Delivery Contract

### Dispatcher

The Shift Notice Cog owns one `discord.ext.tasks.loop(minutes=1)` dispatcher,
aligned to exact minute boundaries. A pass finds enabled, configured guilds whose
tick falls in the following minute and creates a finite per-guild task.

Examples:

- At `13:44`, a `minute_of_hour=45` configuration schedules tick `13:45`.
- At `13:59`, a `minute_of_hour=0` configuration schedules tick `14:00`.

Ready/bootstrap, successful enable, and successful minute edits also schedule a
still-future tick less than one minute away, so they do not depend on the previous
dispatcher pass. A tick that has already been reached is never automatically
backfilled.

Maintain process-local de-duplication by `(guild_id, scheduled_tick)`. Track at
most one finite task per guild; a newly valid future tick replaces or cancels a
stale task. Cog unload, disable, hard clear, replacement, and relevant settings
changes cancel obsolete tasks.

The deployment contract is one production bot worker. Multiple bot workers can
duplicate messages; no distributed lock or history row is introduced.

### Tick-to-boundary semantics

- A tick at minute `0` targets the boundary at that same civil hour.
- A tick at minute `1..59` targets the boundary at the next civil hour.

Thus a `13:45` tick describes the boundary at `14:00`, while a `14:00` tick also
describes the boundary at `14:00`.

Automatic work proceeds only when the target boundary lies within the complete
source envelope, inclusive.

### Pre-render timing

Each finite task:

1. waits until `scheduled_tick - 30 seconds`;
2. reloads database state, checks the destination, builds the ownership map,
   performs lazy reads, and renders one immutable payload snapshot;
3. if ready early, waits until the scheduled tick;
4. immediately before send, reloads and verifies enabled state, owner channel,
   minute, expected configuration identity, and task tick;
5. silently cancels if the task became stale; and
6. sends immediately if preparation completed after the scheduled tick.

The final revalidation makes no second Sheet read. A normal payload may therefore
reflect Sheet state from approximately 30 seconds before delivery.

### Retry and failure delivery

Retry only transient pre-send work, such as a temporary database lock or Google
Sheets transport/rate-limit error. Use bounded backoff and never start a new
attempt at or after:

```text
target boundary + 5 minutes
```

Every attempt rebuilds the snapshot under the same target and ownership rules.
Deterministic validation, data-contract, asset, rendering, image-size, and Discord
payload-limit failures do not retry.

If a deterministic failure is discovered during the 30-second pre-render window,
retain the failure state and wait until the scheduled tick before publishing the
generic failure embed. If transient attempts remain unsuccessful, publish the
generic failure once when the retry window is exhausted. A normal payload or a
failure payload calls `channel.send` exactly once.

Any Discord send exception is delivery-ambiguous. Never retry that send and never
follow it with a failure embed. Log the exception only.

The dispatcher and finite tasks isolate exceptions so one guild cannot terminate
the recurring dispatcher or another guild's work.

## `/shift_notice send_latest`

`send_latest` is an explicit administrator recovery action with no parameters.
It is valid only when:

- invoked in the enabled owner channel;
- the caller still has `administrator` and `manage_channels`; and
- an eligible boundary exists whose configured tick has already been reached.

Resolve the latest such boundary from the current configuration and source
envelope. Before the first eligible tick, report that nothing is available. After
the envelope closes, the command may resend the final eligible boundary, normally
the final END notice.

The command performs one immediate build with no pre-render delay and no automatic
retry window. A successful send posts the same normal public message and returns
an ephemeral success response. A pre-send failure is reported ephemerally and
logged; it does not post the generic public failure embed. A Discord send exception
is not retried.

There is no delivery history or idempotency check. The administrator explicitly
accepts that `send_latest` may create a duplicate public notice.

## Failure and Observability Contract

Use structured logs with, where applicable:

- operation;
- guild, destination channel, and Shift Notice configuration IDs;
- selected Shift Register source and worksheet IDs;
- scheduled tick and target boundary;
- attempt number and retry deadline; and
- exception class and safe diagnostic message.

Do not log supporter names, worksheet cell values, rendered member fields, tokens,
or other payload/private data. Public failure embeds contain no reference ID or
technical cause.

Behavior by failure point:

- No complete source/envelope: silent automatic skip, settings warning.
- Missing/unusable destination: stop before Sheets, log only.
- Selected source read failure: no fallback source; retry only if transient.
- Required lazy adjacent read failure: fail the complete payload.
- Deterministic render/payload failure: generic automatic failure at the proper
  send time.
- Normal or failure `channel.send` exception: log only, no second send.
- Manual pre-send failure: ephemeral response plus structured log, no public
  failure embed.

## Architecture Boundaries

Implementation must preserve these responsibility boundaries:

- **Model/config access:** Shift Notice persistence, guild uniqueness, and source
  metadata queries.
- **Cog:** commands, dispatcher, finite-task registry, lifecycle cancellation,
  Discord sends, and top-level retry orchestration.
- **Components:** setup/edit/replacement views and modals using existing
  permission/requester/stale-state patterns.
- **Google Sheets adapter/managers:** spreadsheet grouping, worksheet locks, and
  projection of the approved Final rectangle.
- **Pure domain logic:** civil/event time mapping, ownership, frames, identities,
  Honso alignment, member sets, durations, CUT windows, and localized payload
  inputs. No Discord, ORM, or worksheet objects enter this layer.
- **Pure renderer:** typed render input to PNG bytes. No database, Sheets, Discord,
  filesystem temp state, or network access.
- **Message templates/localization:** public announcement copy under the existing
  `resources/messages/` system where applicable; slash localization remains in
  the translator.

Do not add a pass-through context object, generic notification base, or general
repository merely to share one call site. Prefer small typed values at each
integration boundary.

## Schema Rollout and Compatibility

The rollout is additive:

- create only `shift_notice_config`;
- rely on the existing model discovery and schema-generation startup path;
- perform no backfill;
- require an administrator to enable and set up each guild explicitly; and
- leave existing Shift Register rows and Sheets untouched.

Old application code ignores the new table, so rollback may leave it in place.
Do not drop data as part of rollback. No existing column, command, worksheet
layout, API scope, dependency, environment variable, or stored identifier changes.

The implementation plan must enumerate the exact model discovery, deployment,
and manual rollback checks before schema-changing code is written.

## Automated Validation

Add focused tests for at least the following:

### Configuration and UI

- `parse_minute_of_hour` NFKC, whitespace, valid bounds, and invalid grammar.
- Nullable setup state and `0..59` persistence.
- Guild uniqueness and cascading hard clear.
- Soft disable/re-enable minute retention.
- Existing usable destination rejection and unusable destination replacement.
- Requester, stale timestamp, channel, administrator, and manage-channels checks.
- Normal TextChannel and required bot-permission enforcement.
- DB-only settings behavior, exact clean/no-source copy, disabled markers,
  incomplete sources, merged overlap warnings, and lossless continuation output.

### Ownership and time

- `created_at, id` deterministic ordering.
- Tier 1 overlap winners and losers.
- Tier 1 over Tier 2 regardless of age.
- Tier 2 internal physical-gap ownership.
- No fallthrough for blank Runner or a selected read failure.
- Source-less internal CUT and no-source/no-envelope behavior.
- Multiple ranges, long internal gaps, and distinct spreadsheets.
- Event hours `24..30`, civil next-day conversion, clock emoji, source-local copy,
  and target timestamps.
- Row addressing from anchor and DB hours while bogus visible Sheet labels are
  ignored.

### Lazy Sheets reads

- Initial previous/next frontier only.
- One values batch per spreadsheet per frontier.
- Deduplication when frames share a worksheet.
- Adjacent source loading only at a duration or CUT edge.
- Every source read at most once per notice.
- Stops at absence, CUT, source-less hour, or envelope edge.
- Required adjacent read failure rejects the whole payload.
- Deterministic worksheet lock ordering and best-effort cross-spreadsheet snapshot
  behavior.

### Frame and identity domain

- Runner blank ignores residual five-lane data.
- Active-empty versus CUT.
- All START, TRANSITION, END, and CUT combinations.
- Person-based Ending/Continuing/Starting across role changes.
- Exact username suffix, unique display name, duplicate display name, unresolved
  label, and two labels resolving to one user.
- Duplicate candidate expansion with visual-only mentions.
- Six-permutation Honso alignment and stable tie-breaking.
- Cross-role movements remain visible.
- Cumulative and remaining hours within and across sources.
- Duplicate lane occurrence counts one person-hour.
- Seven-row CUT selection, one-sided backfill, chronology, and both ellipsis edges.

### Renderer and message payload

- Normal width `1972 px`, seven-row CUT size `1972 × 800`, and 192-DPI metadata.
- Approved v12 status, inactive lane, geometry, long-name, and movement behavior.
- Bundled font/emoji loading without network or system fonts.
- Japanese image role labels under every announcement language.
- Exact Japanese, Traditional Chinese, and English copy branches.
- Three fixed non-inline field applicability rules and localized empty values.
- One attachment, multiple ordered embeds, and image only on the first embed.
- `AllowedMentions.none()` and safe unresolved label escaping.
- UTF-16 field limits, aggregate embed limits, upload limit, and deterministic
  failure without truncation.
- Generic localized automatic failure payload contains no image, fields, names,
  technical details, or honorific note.

### Scheduler and delivery

- Exact-minute alignment and next-minute selection.
- Minute `0` boundary-now and minute `1..59` next-boundary semantics.
- Ready/enable/edit scheduling for a still-future tick under one minute away.
- No reached-tick startup backfill.
- Per-guild task replacement, de-duplication, cancellation, and Cog unload.
- Thirty-second pre-render, late readiness, final config revalidation, and no
  second Sheet read.
- Transient-only bounded retry and the strict boundary-plus-five-minute cutoff.
- Deterministic failure waits for the scheduled tick.
- Exactly one Discord send attempt and no failure follow-up after an ambiguous
  send exception.
- One guild failure does not stop the dispatcher.
- `send_latest` before the first tick, during the envelope, after the final
  boundary, success, pre-send failure, and intentional duplicate behavior.

Run the complete managed-sandbox validation suite documented in
`docs/agent_harness.md`, including lock verification, non-mutating Ruff checks,
pytest with project coverage, and compileall.

## Manual Integration Validation

Update `docs/manual_integration_validation.md` with reusable checks for:

- enable/setup/edit/settings/disable/re-enable/hard-clear;
- singleton rejection and unusable-channel replacement;
- command and callback permission loss;
- destination bot permissions;
- one and multiple Shift Register sources, including soft-disabled sources;
- same-spreadsheet and cross-spreadsheet lazy reads;
- overlap warning presentation;
- a deliberately incorrect visible Final time label;
- Runner-controlled staffed, active-empty, and CUT rows;
- 24-to-30-hour scheduling and timestamps;
- Japanese, Traditional Chinese, English, and multi-language deliveries;
- no supporter pings;
- transient retry, deterministic public failure, and ambiguous send behavior;
- `send_latest`; and
- actual Discord rendering of the 1972-pixel normal and CUT images.

After implementation, create the ignored directory:

```text
scripts/local/shift_handoff_summary_previews/
```

Generate review PNGs with the production renderer for START, TRANSITION, END,
active-empty, long-name, and CUT cases with leading-only, middle, trailing-only,
and both-edge ellipses. These files are local review artifacts and must not be
committed.

## Acceptance Criteria

The feature is ready for implementation review only when:

- this design has a separately approved implementation plan and execution mode;
- the implementation remains isolated from unrelated working-tree changes;
- all public and settings copy above is exact;
- all spreadsheet reads follow the database-derived, lazy, spreadsheet-scoped
  contract;
- the renderer matches v12 except for the explicitly approved differences;
- automated validation passes without changing unrelated files;
- manual Discord/Sheets checks and local preview images are available for review;
  and
- no staging, commit, integration, push, worktree removal, or branch deletion is
  performed without its separately required approval.
