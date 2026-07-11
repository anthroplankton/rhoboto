# Shift Register Team Source Design

## Status

Implemented. Automated validation is complete; manual Discord and Google Sheets
integration validation remains.

## Goal

Generalize Shift Register's current Team Summary integration into a reusable Team
Source. The same resolved source must support the Shift settings panel, existing
Shift Entry formulas, and a future draft-generation workflow without duplicating
Team Register configuration or worksheet metadata.

This change also establishes one configuration-level contract for the worksheet
used as each register feature's default user-facing Google Sheet destination.

## Existing Behavior

`ShiftRegisterManager.resolve_team_summary_source()` queries Team Register
configurations in the Shift Register guild. No configuration produces `MISSING`,
more than one produces `AMBIGUOUS`, and exactly one is inspected as the source.

For a unique source, the resolver loads all configured Team worksheets and the Team
Summary worksheet. It validates the Summary header and records the column positions
needed by the Shift Entry formula. The Shift settings panel then shows the Team
Register channel and explicitly uses the Summary worksheet as the displayed source.

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

1. Query Team Register configurations whose FeatureChannel has the Shift Register's
   `guild_id`, retaining `select_related("feature_channel")`.
2. Return `MISSING` for no configuration and `AMBIGUOUS` for multiple
   configurations.
3. Use the unique config's `get_worksheet_ids()` and `sheet_url` to load the
   configured worksheets.
4. Build `TeamRegisterGoogleSheetsMetadata` from those worksheets.
5. Return `INVALID` if a configured worksheet is missing or the configured landing
   worksheet cannot be found in the metadata.
6. Read and validate the Summary worksheet header.
7. Resolve the username, encore-role, Main ISV, optional Encore ISV, and final import
   column positions into `TeamSummaryColumns`.
8. Return `AVAILABLE` with `TeamSource(config, metadata, summary_columns)`.

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

Formula state behavior remains unchanged:

- `AVAILABLE` creates or repairs the expected formula.
- `MISSING`, `AMBIGUOUS`, and `INVALID` clear stale Team formula anchors.
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
The formatter must not directly select `metadata.summary_worksheet`. Team Summary is
The landing worksheet title and ID are not repeated in Shift settings. The link still
targets the configured landing worksheet through its `gid` URL.

The unavailable-state messages will use the general `Team Source` term while
preserving the existing status distinctions. The panel will not list every Team
worksheet. The Team Register landing worksheet remains the canonical user-facing
entry point.

### Future Channel Selection

This change will not add source selection or modify the database schema. Until source
selection is implemented, a guild must have exactly one Team Register configuration
for automatic resolution.

A future migration may persist the selected Team FeatureChannel using a stable
relation or identifier on `ShiftRegisterConfig`. At that point,
`resolve_team_source()` should resolve the selected configuration first and retain
the current unique-source behavior only for an unset selection.

### Future Draft Generation

Draft generation is outside this change. A future draft workflow may reuse the
resolved source through `source.metadata.team_worksheets` and
`source.metadata.summary_worksheet`, depending on its approved data contract.

`resolve_team_source()` will not load all registered member rows. A separate,
purpose-specific operation should read Team data only when draft generation needs it.

## Affected Files

### Application Code

- `models/base/sheet_config_base.py`
  - Add the abstract `landing_worksheet_id` property.
- `models/team_register.py`
  - Return `summary_worksheet_id` as the landing worksheet.
- `models/shift_register.py`
  - Return `entry_worksheet_id` as the landing worksheet.
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
  - Preserve Summary-column validation and Shift Entry formula behavior.
- `components/ui_shift_register.py`
  - Resolve and format `Team Source`.
  - Select the displayed worksheet through `landing_worksheet_id`.

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
  - Preserve missing, ambiguous, invalid, unresolved, Main-only, Main-and-Encore,
    renamed-header, formula-update, and formula-clearing coverage.
- `tests/test_ui_permissions.py`
  - Update the recording manager and settings field name.
  - Verify the available state displays the source channel and landing worksheet.
  - Verify the formatter follows `landing_worksheet_id` rather than assuming Summary.
  - Preserve unavailable-state and field-order coverage.

`tests/test_worksheet_structs.py` should not require behavior changes because the
formula builder contract and generated formula remain unchanged.

### Documentation

- `docs/shift_register_timeline_migration.md`
  - Rename the settings status to Team Source and describe the landing worksheet.
  - Preserve current Summary-backed `IMPORTRANGE` migration instructions.
- `docs/manual_integration_validation.md`
  - Update Team Source settings cases and add landing worksheet consistency checks.

## Risk Areas

- The landing worksheet ID must resolve to an item in the loaded Team metadata;
  otherwise the source is `INVALID`.
- Summary header validation remains mandatory for existing Shift Entry formulas.
- `UNRESOLVED` must continue preserving existing formulas.
- `TeamSource.config.feature_channel.channel_id` depends on retaining
  `select_related("feature_channel")`.
- Components must not import Team Register cogs to obtain worksheet-selection logic.
- Guide, auto-guide, help, and delete flows must keep producing the same Sheet URLs.
- No Discord permissions, command names, database schema, or Google Sheets layout may
  change.

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
4. With one Team Register, confirm the field shows its channel and landing worksheet
   link and ID.
5. Confirm missing, multiple, invalid, and temporarily unavailable sources show the
   correct distinct status.
6. Submit a Shift and confirm availability still updates.
7. Confirm an available source populates the Shift Entry Team formula result.
8. Confirm missing, ambiguous, and invalid sources clear stale Team formula anchors.
9. Confirm a transient unresolved source preserves an existing formula.
10. Confirm first-time cross-Sheet use still supports the required `IMPORTRANGE`
    access authorization.

## Out of Scope

- Team source channel selection UI or persistence.
- Database migrations or new model fields.
- Google Sheets worksheet, column, or formula changes.
- Draft-generation implementation.
- Listing all Team worksheet links in Shift settings.
- Discord command, permission, or localized public-template changes.
- Compatibility aliases for the old Team Summary Source names.
- Unrelated refactors.
