# Shift Register Schedule Image Posting Design

## Status

Approved design for `/shift_register post_schedule_image`. This document defines
feature behavior and implementation boundaries. It does not authorize
implementation, a database migration, Google OAuth scope expansion, Git history
operations, push, or deployment.

## Goal

Post the current configured Final Schedule to a selected Discord text channel or
thread as one PNG while preserving Google Sheets' rendered presentation. The
administrator explicitly labels the attachment as tentative or confirmed through
its filename. The workflow automates the former manual PDF/image export without
writing to the worksheet.

## Scope

This feature adds:

- `/shift_register post_schedule_image`;
- one required native `schedule_status` choice;
- an optional native destination `channel`;
- an optional strict Final Schedule A1 rectangle;
- automatic discovery of the Final worksheet's value/formula data rectangle;
- authenticated, range-bounded Google PDF export;
- local PDF rasterization and multi-page PNG composition;
- attachment-only Discord posting;
- Japanese and Traditional Chinese Discord command localizations; and
- automated and manual integration validation.

This feature never writes to Google Sheets. It does not change database schema,
stored configuration, worksheet layout, command names outside this new command,
public announcement templates, or the current Final generation and role-assignment
contracts.

## Reused Contracts

The implementation reuses:

- the Shift command group's guild-only and
  `administrator + manage_channels` defaults;
- the existing `require_enabled=False` Shift finalization context;
- soft-disable and hard-clear behavior;
- `A1Rectangle` and the strict `parse_a1_range()` parser;
- spreadsheet-scoped full-grid value reads through `GoogleSheet` with formula
  rendering;
- the existing feature-channel and worksheet-resource lock order;
- configured Final worksheet IDs rather than worksheet titles;
- the existing Google authentication and safe storage-error boundary;
- in-memory `discord.File(BytesIO(...), filename=...)` attachment delivery; and
- centralized Discord application-command error handling.

Do not introduce a second Google authentication client, browser runtime, custom
Sheets renderer, image-posting service abstraction, confirmation view, or new lock
type.

## Command Surface

```text
/shift_register post_schedule_image
    schedule_status: required native choice
    channel: optional Text Channel or Thread
    final_schedule_range: optional strict A1 rectangle
```

The command defers ephemerally before external work. It is non-destructive and
does not present a confirmation view.

### `schedule_status`

The option is required and has no default. Its internal values are stable and are
not inferred from worksheet content:

| Default choice | Internal value | Attachment filename |
| --- | --- | --- |
| `Tentative` | `tentative` | `shift-schedule-tentative.png` |
| `Confirmed` | `confirmed` | `shift-schedule-confirmed.png` |

The choice changes only the attachment filename. It does not alter worksheet
data, overlay text onto the image, add public message content, or trigger another
workflow.

### `channel`

Use a native `TextChannel | Thread | None` option.

- Omitted: post to the interaction's current text channel or thread.
- Supplied: post to that text channel or thread in the same guild.
- Reject categories, forums, voice channels, stage channels, and DMs through the
  native option type.

Resolve and validate the destination before Google Sheets work. The destination
message contains only the image attachment: no text, embed, source link, role
mention, or status label.

### `final_schedule_range`

When supplied, parse one strict colon-delimited rectangle such as `A1:J30` with
the existing Final Schedule A1 parser. The explicit rectangle is authoritative:

- export the complete selected rectangle;
- do not auto-trim its rows or columns;
- reject open-ended, sheet-qualified, absolute, multi-range, reversed, malformed,
  or out-of-grid input; and
- require it to fit the configured Final worksheet's current physical grid.

When omitted, discover the range from Final worksheet values and formulas as
defined below.

## Finalization Availability

This command follows the current finalization boundary rather than the open
registration boundary.

- Soft-disabled, still configured Shift Register: allowed.
- Hard-cleared Shift Register settings: unavailable through the existing missing
  configuration response.
- Missing configured Final worksheet: fail without creating, repairing, or
  replacing a worksheet.

No other Shift command changes its enabled-state behavior.

## Operation Flow

The command performs these steps in order:

1. Defer the interaction ephemerally.
2. Resolve the guild/channel source.
3. Parse an explicit `final_schedule_range`, if present.
4. Resolve the destination and preflight the Bot's effective permissions.
5. Acquire the existing Shift feature-channel lock.
6. Resolve fresh configured finalization context with `require_enabled=False`.
7. Fetch current worksheet metadata and require the configured Final worksheet.
8. Ask the manager to select the range and export PDF bytes while holding the
   Final worksheet resource lock.
9. Release both locks after PDF export completes.
10. Rasterize and compose the PNG in `asyncio.to_thread()`.
11. Enforce the destination guild's upload-size limit.
12. Send an attachment-only message to the destination.
13. Replace the ephemeral response content with `message.jump_url` alone.

Do not hold a database transaction, feature-channel lock, or worksheet lock while
performing local image conversion or sending the Discord message.

If the Discord image message succeeds but editing the ephemeral response fails,
do not delete or repost the image. Log only the operation context and posted
message identifiers needed for diagnosis.

## Automatic Range Discovery

Within the Final worksheet resource transaction, issue one existing
spreadsheet-scoped full-grid values read for the Final worksheet. Keep
`valueRenderOption=FORMULA` so a formula counts as data even when its evaluated
display value is empty.

Find the minimum rectangle containing every cell whose physical value is not
`None` and not the empty string. Consequences:

- formulas count as data;
- numeric zero and Boolean false count as data;
- whitespace-only strings count as data;
- ragged rows are valid;
- empty cells outside the rectangle are excluded;
- outer style-only cells are excluded; and
- every blank cell and row inside the rectangle remains selected.

If no value or formula exists, stop with a concise Final Schedule empty-data
error. Do not call the PDF export endpoint and do not post to Discord.

The full-grid value read and rendered export are necessarily separate Google
requests: the values API discovers geometry but does not return pixels. An
explicit range skips the values read and performs only the rendered export.

## Google PDF Export Boundary

Add one range-bounded PDF export method to `GoogleSheet`. Cogs and managers must
not construct or call the Google export URL directly.

The adapter uses the existing authenticated Google credential and the
Google-maintained Sheets PDF-export pattern:

- identify the spreadsheet and Final worksheet `gid`;
- convert the inclusive A1 rectangle to the export endpoint's zero-based,
  end-exclusive `r1`, `c1`, `r2`, and `c2` parameters;
- request PDF format in landscape orientation and fit to width;
- use 0.1-inch top, bottom, left, and right page margins;
- omit sheet names, print titles, page numbers, and row/column headings;
- do not repeat frozen rows across pages; and
- preserve the worksheet's current gridline visibility from worksheet metadata.

The adapter must validate:

- a successful HTTP response;
- an `application/pdf` content type;
- nonempty response bytes; and
- that authentication, permission, quota, missing-resource, and transient
  failures cross the existing safe Google Sheets error boundary.

Do not log the request URL, spreadsheet ID, worksheet ID, response body, PDF
bytes, or private worksheet content. Error logs use the current sanitized
operation context and exception classification.

The first implementation uses the existing Sheets OAuth scope. Live integration
validation with the deployed service-account model is a release gate. If the
authenticated web export rejects that scope, stop and seek separate approval for
an OAuth scope change; do not add Drive scope as a fallback.

The endpoint is demonstrated by a Google-maintained sample but is not a formal
Sheets REST resource method. Keeping it behind `GoogleSheet` is the compatibility
boundary if Google changes it.

## PDF-to-PNG Rendering

Add these runtime dependencies and update `uv.lock`:

- `pypdfium2` for PDFium-backed PDF page rasterization; and
- `Pillow` for page cropping, composition, border creation, and PNG encoding.

Use their prebuilt wheels; do not add Poppler, Ghostscript, ImageMagick, Chromium,
Playwright, or another operating-system package. PDF rendering and Pillow work
run inside `asyncio.to_thread()` so Discord's event loop is not blocked.

Render at 192 DPI onto an opaque white RGB background. Reject a PDF with no
pages, invalid dimensions, decoding failures, or an output plan larger than
`25,000,000` pixels before allocating the combined image.

### Paper whitespace and page composition

Google PDF margins are 0.1 inches. Remove only blank outer paper canvas:

- use one common horizontal crop across every page so column geometry remains
  aligned;
- crop leading outer paper only from the first page;
- crop trailing outer paper only from the last page;
- retain intermediate page height so blank rows inside the selected range are
  not lost; and
- retain a full page when no non-white crop boundary can be found.

Join pages vertically in source order with a zero-pixel gap. Add one uniform
24-pixel white border around the final combined image, then encode it as PNG.
Single-page output follows the same rules.

Do not silently lower DPI, resize an oversized image, split it into multiple
attachments, or substitute the PDF. A smaller explicit range is the supported
administrator correction when the selected content exceeds a safety or upload
limit.

## Discord Permission and Posting Contract

The invoking administrator continues to require the Shift command group's
`administrator` and `manage_channels` permissions.

Before any Sheets request, use the Bot member's effective destination permissions
to require:

- text channel: `send_messages` and `attach_files`; or
- thread: `send_messages_in_threads` and `attach_files`.

The preflight is an early failure optimization, not an authorization substitute.
The final send must still handle Discord `Forbidden`, deleted or archived/locked
destinations, permission drift, and other HTTP failures.

Construct the post with `discord.File(BytesIO(png_bytes), filename=...)` and call
the destination's normal message send API without `content` or `embed`. Compare
the PNG byte length with the destination guild's current `filesize_limit`; do not
hard-code Discord's default byte ceiling.

On success, use the returned Discord message's raw `jump_url` as the complete
ephemeral response content. Do not wrap it in Markdown or add a prefix, status,
or success sentence; Discord supplies its native message-link UI.

Do not automatically retry the Discord post because a retry could create a
duplicate image message.

## Discord UI Localization

Stable default command and option identifiers remain English. Add Japanese and
Traditional Chinese UI localization through `bot/translator.py` and Discord's
locale handling.

| Surface | Default | Japanese | Traditional Chinese |
| --- | --- | --- | --- |
| Command | `post_schedule_image` | `現行シフト画像投稿` | `發布班表圖片` |
| Description | Post the current Final Schedule as an image. | 現行シフトを画像として投稿します。 | 將現行班表發布為圖片。 |
| `schedule_status` | `schedule_status` | `シフト状態` | `班表狀態` |
| `channel` | `channel` | `投稿先` | `頻道` |
| `final_schedule_range` | `final_schedule_range` | `現行シフト範囲` | `班表範圍` |

Localize visible choice names while preserving their internal values:

| Internal value | Default | Japanese | Traditional Chinese |
| --- | --- | --- | --- |
| `tentative` | `Tentative` | `仮` | `暫定` |
| `confirmed` | `Confirmed` | `確定` | `確定` |

Option descriptions must concisely communicate these approved semantics:

- status is required and appears in the attachment filename;
- destination defaults to the current channel; and
- the range is optional and accepts a rectangle such as `A1:J30`.

The attachment-only destination post needs no `resources/messages/` template.
Operational errors remain on the current inline Shift administrator-response
surface.

## Error Handling

Validate cheap local conditions before expensive or private external access.
Expected failures edit the ephemeral response and never send a destination
message:

| Failure | Marker intent | Behavior |
| --- | --- | --- |
| Invalid A1 range | `⚠️` + `config.CONFUSED_EMOJI` | State that the range is invalid and nothing was posted. |
| Empty Final worksheet or range outside its grid | `⚠️📏` | State that no publishable Final range exists. |
| Missing Shift/Final configuration | Existing missing-config/storage copy | Read or post nothing. |
| Missing destination send/attach permission | `⚠️` | Name the destination and required permission class. |
| Google auth, permission, quota, or export failure | Existing `⚠️🛠️` storage route | Log only sanitized context and error reference. |
| Invalid PDF or local renderer failure | `⚠️🚧` | State that image generation failed; do not expose PDF contents. |
| Pixel or upload-size limit exceeded | `⚠️` | Ask for a smaller explicit range; do not resize automatically. |
| Discord send failure | `⚠️🛠️` | State that no image message was posted. |

Unexpected exceptions continue through the existing centralized unexpected-error
path. Do not include worksheet values, exported bytes, private URLs, or raw
external response bodies in user copy or logs.

## Persistence and Migration

This feature adds no model, field, database migration, environment variable,
saved posting history, cache, worksheet, worksheet format, or Google Sheet write.

The only dependency changes are `pypdfium2`, `Pillow`, and the corresponding
`uv.lock` update. The only Discord registration change is the new subcommand and
its localizations.

## Automated Validation

Add focused tests for these boundaries.

### Range logic

- minimum rectangle over ragged rows;
- formulas whose displayed result may be empty;
- zero, false, whitespace, and ordinary strings;
- internal empty rows and cells;
- style-only outer cells absent from value data; and
- a completely empty Final grid.

### Google adapter

- exact A1-to-export-coordinate conversion;
- worksheet and range query parameters;
- 0.1-inch margins, fit, metadata suppression, frozen-row, and gridline settings;
- authenticated request construction without logging identifiers;
- PDF status, content-type, and empty-body validation; and
- safe exception classification.

Tests use mocked authenticated transport. They never require a real spreadsheet,
network request, service-account credential, or private identifier.

### Renderer

- one-page PNG output at the requested scale;
- multi-page source-order stitching;
- common horizontal crop and retained intermediate page height;
- zero page gap and one 24-pixel final border;
- all-white page handling;
- invalid/empty PDF handling; and
- the 25-million-pixel guard.

Use generated in-memory test fixtures. Do not commit exported schedules or private
worksheet screenshots.

### Manager and locks

- omitted range performs exactly one full Final value read before export;
- explicit range performs no value read;
- empty auto-range performs no export;
- explicit out-of-grid input performs no export;
- the worksheet resource lock covers read and PDF export; and
- no worksheet write method is called.

### Cog and localization

- native required choices and no status default;
- optional text-channel/thread destination and optional range;
- exact default/Japanese/Traditional Chinese command and choice mappings;
- invalid range and missing permission fail before context/Sheets access;
- soft-disable works and hard-clear does not access Sheets;
- destination send has one file and no public content/embed;
- status-specific attachment filenames;
- upload-size failure sends nothing; and
- successful ephemeral content equals raw `message.jump_url`.

Run the focused tests, non-mutating Ruff checks, the full pytest suite, lockfile
check, and compile validation using the managed-sandbox commands documented in
`docs/agent_harness.md`.

## Manual Integration Validation

Add reusable rows to `docs/manual_integration_validation.md` covering:

- service-account PDF export with the existing Sheets scope;
- automatic and explicit ranges;
- merged cells, fonts, CJK text, colors, borders, alignment, formulas, row and
  column sizes, styled gap rows, timezone columns, gridlines, and the lower legend;
- single-page and multi-page schedules;
- zero page gaps and the final 24-pixel border;
- current and alternate text-channel destinations;
- public/private thread destinations where available;
- missing send and attach permissions;
- tentative and confirmed filenames;
- English, Japanese, and Traditional Chinese Discord UI; and
- soft-disable and hard-clear behavior.

Any user-provided live validation Sheet is transient private input. Never place
its URL, spreadsheet ID, worksheet ID, cell contents, screenshot, or export in a
test, fixture, document, planning file, log, or committed artifact.

## Rollout and Rollback

Before rollout:

1. verify Python 3.13 and deployment-platform wheels resolve through `uv sync`;
2. pass the automated validation suite;
3. validate authenticated range PDF export in a development guild;
4. compare representative PNGs with their live Final worksheet presentation;
5. verify attachment-only posting and raw jump-link response; and
6. synchronize the new slash command/localizations through the normal bot startup
   flow.

If the current OAuth scope cannot export PDF, stop rollout. OAuth scope expansion
requires a separate approved design update and deployment validation.

Rollback removes the command, renderer code, and two dependencies. No database or
worksheet rollback is needed because this feature persists no data and writes no
Google Sheet content.

## Risks and Limits

- The authenticated Sheets PDF URL is represented in a Google-maintained sample
  but is not a formal Sheets REST endpoint. Adapter isolation and live validation
  reduce, but do not eliminate, compatibility risk.
- Process-local locks serialize Bot operations only. A human can edit Final while
  export is running; the image represents Google's rendered state at export time.
- Server-side Google rendering should preserve fonts and Sheets presentation, but
  representative CJK/font output remains a manual release gate.
- PDFium wheel availability is deployment-platform dependent and must pass locked
  dependency installation before rollout.
- Very large ranges are rejected rather than silently made unreadable.

## Out of Scope

- scheduled or automatic posting;
- replacing, editing, or deleting a previous schedule image;
- posting history or status persistence;
- status text, watermark, or overlay inside the PNG;
- public message text, embed, source link, or role mention;
- PDF attachment delivery;
- multiple PNG attachments;
- image caching or deduplication;
- browser automation;
- a custom Google Sheets renderer;
- Drive OAuth scope or Drive API export fallback; and
- any Final worksheet value, formula, style, validation, note, or layout change.

## Success Criteria

The feature is complete when an authorized administrator can explicitly choose a
tentative or confirmed status, optionally select a text channel/thread and exact
Final range, and receive one attachment-only Discord image message whose PNG
preserves the selected Google-rendered schedule. Omitted range discovery includes
the complete value/formula data rectangle, multi-page output remains one image,
the ephemeral response is the raw Discord message URL, and every privacy,
permission, lock, size, localization, and no-write boundary above is verified.

## References

- [Google Sheets values batchGet](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets.values/batchGet)
- [Google Workspace export formats](https://developers.google.com/workspace/drive/api/guides/ref-export-formats)
- [Google-maintained Sheets PDF generation sample](https://developers.google.com/apps-script/samples/automations/generate-pdfs)
- [discord.py Guild file-size limit](https://discordpy.readthedocs.io/en/stable/api.html#discord.Guild.filesize_limit)
