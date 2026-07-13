# Shift Register Team Source Design

## Status

Implemented in the working tree. Automated validation passed; database migration
rollout and manual Discord and Google Sheets integration validation remain.

## Goal

Generalize Shift Register's current Team Summary integration into a reusable Team
Source. The same resolved source must support the Shift settings panel, existing
Shift Entry formulas, and Draft generation without duplicating
Team Register configuration or worksheet metadata.

This change also establishes one configuration-level contract for the worksheet
used as each register feature's default user-facing Google Sheet destination.

## Existing Behavior

`ShiftRegisterManager.resolve_team_source()` follows the explicitly saved Team
Register FeatureChannel. An unset selection produces `UNSET`; invalid metadata and
temporarily unreadable data remain distinct states.

For a unique source, the resolver loads all configured Team worksheets and the Team
Summary worksheet. One complete Summary grid read validates the header and provides
the row data needed by Draft profiles. Header-derived projections stop at the unique
terminal `original_message`; physically returned administrator columns after that
marker are not consumed. The Shift settings panel shows the Team Register channel
and uses its configured landing worksheet as the displayed source.

Team Register and Shift Register guide links separately override
`_guide_worksheet_id()` in both their feature-management and user-command cogs. Team
links target the Summary worksheet; Shift links target the Entry worksheet.

## Design

### Landing Worksheet Contract

`SheetConfigBase` will define an abstract `landing_worksheet_id` property. This
property identifies the worksheet used as the feature's default user-facing Google
Sheet destination.

- `TeamRegisterConfig.landing_worksheet_id` returns `summary_worksheet_id`.
- `ShiftRegisterConfig.landing_worksheet_id` returns `entry_worksheet_id`.

The shared `_guide_sheet_url()` implementations will use
`feature_config.landing_worksheet_id`. The two base `_guide_worksheet_id()` methods
and the four Team/Shift cog overrides will be removed.

This is a Python model contract only. It does not add a Tortoise field or require a
database migration.

### Team Source Types

The existing Team Summary Source types will be replaced without compatibility
aliases:

```python
class TeamSourceStatus(StrEnum):
    AVAILABLE = "available"
    UNSET = "unset"
    MISSING = "missing"
    AMBIGUOUS = "ambiguous"
    INVALID = "invalid"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class TeamSummaryColumns:
    username: int
    roles: int
    main_isv: int
    encore_isv: int | None
    import_last_column: str


@dataclass(frozen=True)
class TeamSource:
    config: TeamRegisterConfig
    metadata: TeamRegisterGoogleSheetsMetadata
    summary_columns: TeamSummaryColumns


@dataclass(frozen=True)
class TeamSourceResolution:
    status: TeamSourceStatus
    source: TeamSource | None = None
```

`TeamSource` composes the existing Team Register config and Google Sheets metadata.
It does not copy `sheet_url`, worksheet IDs, worksheet titles, or the source channel
ID into a parallel data-transfer object. `TeamSummaryColumns` contains only derived
Summary-header positions that are not already represented by those existing objects.

The following names will change:

- `TeamSummarySourceStatus` to `TeamSourceStatus`.
- `TeamSummaryFormulaSource` to `TeamSource` plus `TeamSummaryColumns`.
- `TeamSummarySourceResolution` to `TeamSourceResolution`.
- `resolve_team_summary_source()` to `resolve_team_source()`.

### Source Resolution

`ShiftRegisterManager.resolve_team_source()` will:

1. Read the selected Team FeatureChannel ID from `ShiftRegisterConfig`.
2. Return `UNSET` when that ID is null. It must not query or use a sole Team
   Register as a runtime fallback.
3. Query the selected Team Register configuration by its FeatureChannel ID,
   retaining `select_related("feature_channel")`. A missing saved configuration
   returns `INVALID`.
4. Use the selected config's `get_worksheet_ids()` and `sheet_url` to load the
   configured worksheets.
5. Build `TeamRegisterGoogleSheetsMetadata` from those worksheets.
6. Return `INVALID` if a configured worksheet is missing or the configured landing
   worksheet cannot be found in the metadata.
7. Batch-read the complete Summary value grid once and validate its header.
8. Resolve the username, encore-role, Main ISV, optional Encore ISV, and final import
   column positions into `TeamSummaryColumns`.
9. Return `AVAILABLE` with `TeamSource(config, metadata, summary_columns)`.

Invalid URLs and missing worksheets remain `INVALID`. Transient or otherwise
unavailable Google Sheets reads remain `UNRESOLVED` and are logged without exposing
private Sheet data.

The Summary header remains required because Shift Entry formulas currently consume
the Summary projection. Generalizing the source name does not weaken that contract.

### Shift Entry Formula Integration

The generated Google Sheets formula and Shift Entry worksheet layout will not
change. Formula inputs will instead be read from the composed source:

- Sheet URL from `source.config.sheet_url`.
- Summary title from `source.metadata.summary_worksheet.title`.
- Header positions from `source.summary_columns`.

Formula state behavior is:

- `AVAILABLE` creates or repairs the expected formula.
- `UNSET` and `INVALID` clear stale Team formula anchors and do not create new
  Team references.
- `UNRESOLVED` preserves existing formulas so a temporary external failure does not
  erase a working source reference.

### Shift Settings Panel

The settings field will be renamed from `Team Summary Source` to `Team Source`.

For an available source, it will show:

```text
- Channel = #team-register
- Google Sheet = Open Team Register Sheet (link)
```

The displayed worksheet will be selected by
`source.config.landing_worksheet_id` and resolved from `source.metadata.worksheets`.
The formatter must not directly select `metadata.summary_worksheet`. The landing
worksheet title and ID are not repeated in Shift settings. The link still targets
the configured landing worksheet through its `gid` URL.

The unavailable-state messages will use the general `Team Source` term. `UNSET`
will say that no Team source is selected and that Shift registrations continue
without Team references. The panel will not list every Team worksheet. The Team
Register landing worksheet remains the canonical user-facing entry point.

### Channel Selection

Shift settings provide `Edit Team Source`, which stores a nullable selected Team
FeatureChannel on `ShiftRegisterConfig`. A null value means `UNSET`; it never
selects a Team Register implicitly.

After the initial Shift Sheet Modal saves successfully, the bot opens the same Team
Source view used by `Edit Team Source`. The view explains that Team Source is
optional: registrations continue without it, but Team references are not created.

The view uses Discord's native `ChannelSelect`; it does not render its own channel
option list. Before opening the selector, it checks how many Team Registers are
configured in the guild:

- Zero configured Team Registers: show that no Team Register is available and offer
  only `Set Later` during initial setup or `Back to Settings` from editing.
- Exactly one configured Team Register: keep `ChannelSelect` and preselect that
  channel. This is a UI draft only; it is not stored or used until confirmation.
- More than one configured Team Register: keep `ChannelSelect` with no preselection.

`Apply & Repair` validates the chosen same-guild Team Register, its worksheets, and
its Summary header again, persists the FeatureChannel ID, and repairs populated
Shift Entry column C formula anchors using current worksheet metadata. This second
validation is required because a source can change while the view is open.

`Set Later` and `Back to Settings` make no database or Google Sheets changes. The
former is shown only after first-time Sheet setup; the latter is shown only from the
configured settings panel. After `Apply & Repair`, the returned settings panel must
include all normal controls, including `Edit Team Source` and `Enable` or `Disable`
Latest Guide as appropriate.

An invalid saved source does not silently switch to a different Team Register.

### Draft Generation

Draft generation resolves Team Source metadata before acquiring worksheet locks,
then reads the complete Team Summary grid once inside the locked phase. The same
grid supplies both header validation and the purpose-specific
`username -> DraftTeamProfile` projection. If Team Summary shares the Shift
spreadsheet, Summary is included in the same values batch as Entry and Draft;
otherwise each spreadsheet receives one batch request. Administrator columns after
the Summary terminal marker may be transported but are not interpreted or imported.

## Affected Files

### Application Code

- `models/base/sheet_config_base.py`
  - Add the abstract `landing_worksheet_id` property.
- `models/team_register.py`
  - Return `summary_worksheet_id` as the landing worksheet.
- `models/shift_register.py`
  - Return `entry_worksheet_id` as the landing worksheet.
  - Add the nullable `team_source_feature_channel` relation with `SET NULL` deletion
    behavior. Existing Shift Register rows remain unset.
- `cogs/base/feature_channel_base.py`
  - Make both shared guide URL helpers use `landing_worksheet_id`.
  - Remove both `_guide_worksheet_id()` methods.
- `cogs/team_register.py`
  - Remove the Team Register guide worksheet override.
- `cogs/team.py`
  - Remove the Team user-command guide worksheet override.
- `cogs/shift_register.py`
  - Remove the Shift Register guide worksheet override.
- `cogs/shift.py`
  - Remove the Shift user-command guide worksheet override.
- `utils/shift_register_manager.py`
  - Replace Team Summary Source types and resolver names.
  - Compose existing Team config and metadata.
  - Store and resolve only an explicitly selected Team source.
  - Provide the configured-Team count and sole-channel hint needed by the UI.
  - Preserve Summary-column validation and Shift Entry formula behavior.
- `components/ui_shift_register.py`
  - Resolve and format `Team Source`.
  - Select the displayed worksheet through `landing_worksheet_id`.
  - Open optional Team Source selection after initial Sheet setup.
  - Preserve Latest Guide controls when returning from Team Source repair.

### Automated Tests

- `tests/fakes.py`
  - Add the landing worksheet contract to shared configured-manager fakes.
- `tests/test_db_models.py`
  - Verify Team and Shift landing worksheet IDs.
  - Verify model initialization remains unchanged.
- `tests/test_feature_channel_interactions.py`
  - Remove fixture binding for `_guide_worksheet_id()`.
  - Preserve Team guide-to-Summary and Shift guide-to-Entry URL coverage.
- `tests/test_manager_fakes.py`
  - Update all Team Source types, resolver names, and fakes.
  - Verify available sources retain the original config and existing metadata.
  - Verify unset sources are not implicitly resolved from a sole Team Register.
  - Preserve invalid, unresolved, Main-only, Main-and-Encore, renamed-header,
    formula-update, and formula-clearing coverage.
- `tests/test_ui_permissions.py`
  - Update the recording manager and settings field name.
  - Verify the available state displays the source channel and landing worksheet.
  - Verify the formatter follows `landing_worksheet_id` rather than assuming Summary.
  - Verify zero, one, and multiple configured-Team entry states; `Set Later`; and
    `Back to Settings`.
  - Verify successful Team Source repair returns all settings controls, including
    the Latest Guide control.

`tests/test_worksheet_structs.py` should not require behavior changes because the
formula builder contract and generated formula remain unchanged.

### Documentation

- `docs/shift_register_timeline_migration.md`
  - Rename the settings status to Team Source and describe the landing worksheet.
  - Preserve current Summary-backed `IMPORTRANGE` migration instructions.
- `docs/manual_integration_validation.md`
  - Update Team Source settings cases, deferred setup, candidate availability, and
    Latest Guide control preservation checks.

## Risk Areas

- The landing worksheet ID must resolve to an item in the loaded Team metadata;
  otherwise the source is `INVALID`.
- Summary header validation remains mandatory for existing Shift Entry formulas.
- Removing the unique-source fallback means existing Shift Registers with a null
  selected source require an administrator to choose one before Team formulas resume.
- Adding the selected-source relation requires a reviewed database migration; schema
  generation is not a production migration mechanism.
- `UNRESOLVED` must continue preserving existing formulas.
- `TeamSource.config.feature_channel.channel_id` depends on retaining
  `select_related("feature_channel")`.
- Components must not import Team Register cogs to obtain worksheet-selection logic.
- Guide, auto-guide, help, and delete flows must keep producing the same Sheet URLs.
- No Discord permissions, command names, or Google Sheets layout may change.

## Automated Validation

Run focused tests in the managed Codex sandbox:

```shell
env UV_CACHE_DIR=.cache/uv uv run pytest \
  tests/test_db_models.py \
  tests/test_feature_channel_interactions.py \
  tests/test_manager_fakes.py \
  tests/test_ui_permissions.py
```

Run static checks:

```shell
env UV_CACHE_DIR=.cache/uv uv run ruff check --no-fix \
  models cogs components utils tests
env UV_CACHE_DIR=.cache/uv uv run ruff format --check \
  models cogs components utils tests
```

Run full behavior verification before handoff:

```shell
env UV_CACHE_DIR=.cache/uv uv run pytest \
  --cov=bot --cov=cogs --cov=components --cov=models --cov=utils \
  --cov-report=term-missing --cov-fail-under=35
env UV_CACHE_DIR=.cache/uv uv run python -m compileall -q \
  main.py bot cogs components models utils
```

## Manual Discord and Google Sheets Validation

1. Confirm Team guide, auto-guide, and related buttons still open Team Summary.
2. Confirm Shift guide still opens Shift Entry.
3. Open `/shift_register settings` and confirm the field is named `Team Source`.
4. Complete first-time Shift Sheet setup with zero, one, and multiple configured Team
   Registers. Confirm zero shows no selector, one preselects its channel, and
   multiple leaves the selector blank.
5. Confirm `Set Later` and `Back to Settings` make no database or Google Sheets
   changes.
6. Confirm only `Apply & Repair` persists the selected source and that the returned
   panel retains `Edit Team Source` and the correct Latest Guide control.
7. Confirm unset, invalid, and temporarily unavailable sources show the correct
   distinct status.
8. Submit a Shift and confirm availability still updates.
9. Confirm an available source populates the Shift Entry Team formula result.
10. Confirm unset and invalid sources clear stale Team formula anchors.
11. Confirm a transient unresolved source preserves an existing formula.
12. Confirm first-time cross-Sheet use still supports the required `IMPORTRANGE`
    access authorization.

## Out of Scope

- Google Sheets worksheet, column, or formula changes.
- Final Schedule generation.
- Listing all Team worksheet links in Shift settings.
- Discord command, permission, or localized public-template changes.
- Compatibility aliases for the old Team Summary Source names.
- Unrelated refactors.
