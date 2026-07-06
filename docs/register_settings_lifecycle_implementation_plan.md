# Register Settings Lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the shared Team/Shift Register settings lifecycle, fix stale setup behavior, and refine Team Register Encore role settings UX.

**Architecture:** Add a thin shared settings flow helper for settings panel copy and response routing, plus `ManagerBase.get_fresh_sheet_config()` for cache-safe state checks. Keep Team/Shift modal parsing, embed fields, view construction, and feature-specific persistence in their existing modules.

**Tech Stack:** Python 3.13, discord.py UI views/modals/buttons, Tortoise ORM models through existing managers, pytest with repo fakes, Ruff.

## Global Constraints

- Do not commit unless the user explicitly asks.
- Do not push.
- Do not edit secrets, `.env`, service account JSON files, local databases, or logs.
- Do not change Discord command names.
- Do not change privileged intents.
- Do not change database schema.
- Do not change Google Sheets worksheet layout or columns.
- Do not change existing Google Sheets error wording.
- All settings-changing callbacks must continue to require both `administrator` and `manage_channels`.
- Use repo-local cache-prefixed validation commands in managed Codex sandboxes: `env UV_CACHE_DIR=.cache/uv ...`.

---

## Discord Feature Plan Summary

**Goal:** Improve settings lifecycle UX for Team Register and Shift Register while preserving feature-specific behavior.

**Existing behavior:** `/enable` and `/settings` both route into feature-specific setup/current settings UI. Modal saves send a saved settings panel. Old setup panels can remain visible and can open empty setup modals after settings already exist.

**Proposed behavior:** Share the setup/current/saved/stale lifecycle across Team and Shift. Stale setup buttons fresh-check current config; if settings exist, they send the current settings panel instead of opening a setup modal. Team Encore role editing gets revised copy, simplified preview actions, and cancel returns to settings.

**Affected files:**
- Create: `components/ui_settings_flow.py`
- Modify: `utils/manager_base.py`
- Modify: `utils/team_register_manager.py`
- Modify: `components/ui_team_register.py`
- Modify: `components/ui_shift_register.py`
- Modify: `cogs/team_register.py`
- Modify: `cogs/shift_register.py`
- Modify: `tests/test_manager_fakes.py`
- Create: `tests/test_settings_flow.py`
- Modify: `tests/test_ui_permissions.py`
- Modify: `tests/test_team_register_encore_roles.py`

**Risk areas:**
- Discord interaction response state: setup buttons must not defer before opening modals, but must defer before sending stale current follow-ups.
- Manager cache invalidation: stale checks must clear both `_sheet_config` and `_google_sheet`.
- Team/Shift behavior drift: shared helper must not swallow feature-specific metadata fetching or view construction.
- Encore missing role IDs: removing missing IDs must stay preview-only until `Confirm Save`.
- Button style regressions: general current settings panels are stable; saved Team panel only highlights `Edit Encore Roles` when no active Encore roles are configured.

**Test plan:** Add focused pure/helper tests, UI callback tests using existing fakes, and update existing Encore role tests for revised copy and preview state.

**Manual Discord UI checklist:** Validate Team and Shift setup, edit, stale setup, saved/current panels, and Team Encore role preview/cancel/confirm in a dev guild with a disposable Google Sheet.

**Implementation steps:** Follow Tasks 1-5 below. Each task is independently testable and should be reviewed before the next task.

**What will not be touched:** command names, privileged intents, Tortoise schema, Google Sheets layout, secrets, logs, deployment workflow, existing Google Sheets error messages.

---

## File Structure

`components/ui_settings_flow.py` owns shared settings copy and interaction response helpers. It does not import Team or Shift modules.

`utils/manager_base.py` owns the shared fresh config cache invalidation method.

`components/ui_team_register.py` owns Team settings embed/view construction, Team setup/edit modal behavior, and Encore role edit/preview/cancel/save behavior.

`components/ui_shift_register.py` owns Shift settings embed/view construction and Shift setup/edit modal behavior.

`cogs/team_register.py` and `cogs/shift_register.py` keep feature command wiring but delegate the repeated setup/current decision to the shared helper.

Tests stay near current coverage: manager cache tests in `tests/test_manager_fakes.py`, shared pure helper tests in `tests/test_settings_flow.py`, Discord UI callback tests in `tests/test_ui_permissions.py`, and pure Encore embed tests in `tests/test_team_register_encore_roles.py`.

---

### Task 1: Shared Fresh Config And Lifecycle Copy Helpers

**Files:**
- Modify: `utils/manager_base.py`
- Modify: `utils/team_register_manager.py`
- Create: `components/ui_settings_flow.py`
- Modify: `tests/test_manager_fakes.py`
- Create: `tests/test_settings_flow.py`

**Interfaces:**
- Produces: `ManagerBase.get_fresh_sheet_config(self) -> TSheetConfig | None`
- Produces: `SettingsPanel(embed: discord.Embed, view: discord.ui.View)`
- Produces: `settings_title(feature_display_name: str, *, is_save_action: bool) -> str`
- Produces: `settings_description(feature_display_name: str, controls_description: str, *, is_save_action: bool) -> str`
- Produces: `stale_setup_content(feature_display_name: str) -> str`
- Produces: `send_current_panel_followup(interaction: Interaction, panel: SettingsPanel, *, content: str | None = None) -> None`
- Produces: `send_stale_setup_panel_if_configured(interaction: Interaction, manager: ManagerBase, *, feature_display_name: str, build_current_panel: Callable[[object], Awaitable[SettingsPanel]]) -> bool`

- [ ] **Step 1: Write failing manager and copy helper tests**

Add this to `tests/test_manager_fakes.py`:

```python
@pytest.mark.asyncio
async def test_shift_manager_fresh_config_invalidates_cached_google_sheet() -> None:
    manager = ShiftRegisterManager(
        make_feature_channel("shift_register"), "service.json"
    )
    old_config = SimpleNamespace(sheet_url="https://old.sheet.example")
    new_config = SimpleNamespace(sheet_url="https://new.sheet.example")
    cached_sheet = SimpleNamespace(sheet_url=old_config.sheet_url)

    class FakeSheetConfig:
        @classmethod
        async def get_or_none(cls, *, feature_channel: object) -> SimpleNamespace:
            assert feature_channel is manager.feature_channel
            return new_config

    manager.SheetConfigType = FakeSheetConfig
    manager._sheet_config = old_config  # noqa: SLF001
    manager._google_sheet = cached_sheet  # noqa: SLF001

    refreshed_config = await manager.get_fresh_sheet_config()

    assert refreshed_config is new_config
    assert manager._sheet_config is new_config  # noqa: SLF001
    assert manager._google_sheet is None  # noqa: SLF001
```

Create `tests/test_settings_flow.py`:

```python
from __future__ import annotations

from discord import Embed
from discord.ui import View

from components.ui_settings_flow import (
    SettingsPanel,
    settings_description,
    settings_title,
    stale_setup_content,
)


def test_settings_title_uses_current_and_saved_forms() -> None:
    assert (
        settings_title("Team Register", is_save_action=False)
        == "Team Register Settings"
    )
    assert (
        settings_title("Team Register", is_save_action=True)
        == "Team Register Settings Saved"
    )


def test_settings_description_uses_current_and_saved_forms() -> None:
    controls = "Use the buttons below to update sheet settings or Encore roles."

    assert settings_description(
        "Team Register",
        controls,
        is_save_action=False,
    ) == (
        "Team Register is configured for this channel. "
        "Use the buttons below to update sheet settings or Encore roles."
    )
    assert settings_description(
        "Team Register",
        "Use the buttons below to edit sheet settings or Encore roles.",
        is_save_action=True,
    ) == (
        "Your Team Register settings were saved. "
        "Use the buttons below to edit sheet settings or Encore roles."
    )


def test_stale_setup_content_is_neutral() -> None:
    assert stale_setup_content("Shift Register") == (
        "Shift Register is already configured for this channel. "
        "Here are the current settings."
    )


def test_settings_panel_holds_embed_and_view() -> None:
    embed = Embed(title="Team Register Settings")
    view = View()

    panel = SettingsPanel(embed=embed, view=view)

    assert panel.embed is embed
    assert panel.view is view
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
env UV_CACHE_DIR=.cache/uv uv run pytest tests/test_manager_fakes.py::test_shift_manager_fresh_config_invalidates_cached_google_sheet tests/test_settings_flow.py -q
```

Expected: fail because `ManagerBase.get_fresh_sheet_config` and `components.ui_settings_flow` do not exist.

- [ ] **Step 3: Implement `ManagerBase.get_fresh_sheet_config()`**

In `utils/manager_base.py`, add this method after `get_sheet_config()`:

```python
    async def get_fresh_sheet_config(self) -> TSheetConfig | None:
        """Return current sheet config without using cached manager state."""
        self._sheet_config = None
        self._google_sheet = None
        return await self.get_sheet_config_or_none()
```

In `utils/team_register_manager.py`, remove the duplicate `get_fresh_sheet_config()` override:

```python
    async def get_fresh_sheet_config(self) -> TeamRegisterConfig | None:
        """Return the current Team Register config without using cached state."""
        self._sheet_config = None
        self._google_sheet = None
        return await self.get_sheet_config_or_none()
```

- [ ] **Step 4: Implement `components/ui_settings_flow.py`**

Create `components/ui_settings_flow.py`:

```python
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from discord import Embed, Interaction
from discord.ui import View

# Current code routes classified storage failures through storage-error helpers.
from utils.google_sheets_errors import GoogleSheetsError
from utils.manager_base import ManagerBase


@dataclass(frozen=True)
class SettingsPanel:
    embed: Embed
    view: View


def settings_title(feature_display_name: str, *, is_save_action: bool) -> str:
    suffix = "Settings Saved" if is_save_action else "Settings"
    return f"{feature_display_name} {suffix}"


def settings_description(
    feature_display_name: str,
    controls_description: str,
    *,
    is_save_action: bool,
) -> str:
    prefix = (
        f"Your {feature_display_name} settings were saved."
        if is_save_action
        else f"{feature_display_name} is configured for this channel."
    )
    return f"{prefix} {controls_description}"


def stale_setup_content(feature_display_name: str) -> str:
    return (
        f"{feature_display_name} is already configured for this channel. "
        "Here are the current settings."
    )


async def send_current_panel_followup(
    interaction: Interaction,
    panel: SettingsPanel,
    *,
    content: str | None = None,
) -> None:
    await interaction.followup.send(
        content=content,
        embed=panel.embed,
        view=panel.view,
        ephemeral=True,
    )


async def send_stale_setup_panel_if_configured(
    interaction: Interaction,
    manager: ManagerBase,
    *,
    feature_display_name: str,
    build_current_panel: Callable[[object], Awaitable[SettingsPanel]],
) -> bool:
    sheet_config = await manager.get_fresh_sheet_config()
    if sheet_config is None:
        return False

    await interaction.response.defer(ephemeral=True)
    try:
        panel = await build_current_panel(sheet_config)
    except GoogleSheetsError:
        # Current code routes classified storage failures through storage-error helpers.
        return True

    await send_current_panel_followup(
        interaction,
        panel,
        content=stale_setup_content(feature_display_name),
    )
    return True
```

- [ ] **Step 5: Run focused tests**

Run:

```bash
env UV_CACHE_DIR=.cache/uv uv run pytest tests/test_manager_fakes.py::test_team_manager_fresh_config_invalidates_cached_google_sheet tests/test_manager_fakes.py::test_shift_manager_fresh_config_invalidates_cached_google_sheet tests/test_settings_flow.py -q
```

Expected: all selected tests pass.

- [ ] **Step 6: Review checkpoint**

Do not commit. Check:

```bash
git diff -- utils/manager_base.py utils/team_register_manager.py components/ui_settings_flow.py tests/test_manager_fakes.py tests/test_settings_flow.py
```

Expected: only Task 1 files changed.

---

### Task 2: Shared Team And Shift Settings Lifecycle

**Files:**
- Modify: `components/ui_team_register.py`
- Modify: `components/ui_shift_register.py`
- Modify: `cogs/team_register.py`
- Modify: `cogs/shift_register.py`
- Modify: `tests/test_ui_permissions.py`

**Interfaces:**
- Consumes: `SettingsPanel`
- Consumes: `settings_title(...)`
- Consumes: `settings_description(...)`
- Consumes: `send_stale_setup_panel_if_configured(...)`
- Produces: `build_team_register_settings_panel(...) -> SettingsPanel`
- Produces: `build_shift_register_settings_panel(...) -> SettingsPanel`

- [ ] **Step 1: Extend test fakes for stale setup checks**

In `tests/test_ui_permissions.py`, update `RecordingShiftRegisterManager`:

```python
class RecordingShiftRegisterManager:
    def __init__(self) -> None:
        self.upsert_calls: list[dict[str, object]] = []
        self.anchor_updates: list[str] = []
        self.config_exists = True
        self.metadata = SimpleNamespace(
            sheet_url="https://sheet.example",
            entry_worksheets=SimpleNamespace(title="Entry", id=101),
            draft_worksheet=SimpleNamespace(title="Draft", id=102),
            final_schedule_worksheet=SimpleNamespace(title="Final", id=103),
        )

    async def get_sheet_config(self) -> SimpleNamespace:
        if not self.config_exists:
            msg = "Sheet configuration not found."
            raise RuntimeError(msg)
        return SimpleNamespace(
            sheet_url="https://sheet.example",
            final_schedule_anchor_cell=self.anchor_updates[-1]
            if self.anchor_updates
            else "B2",
        )

    async def get_fresh_sheet_config(self) -> SimpleNamespace | None:
        if not self.config_exists:
            return None
        return await self.get_sheet_config()

    async def fetch_google_sheets_metadata(self) -> SimpleNamespace:
        return self.metadata
```

Keep the existing `upsert_sheet_config_and_worksheets()` and
`update_final_schedule_anchor_cell()` methods in the same fake class.

- [ ] **Step 2: Write failing Team stale setup and copy tests**

Update existing tests so setup buttons represent missing config:

```python
@pytest.mark.asyncio
async def test_team_settings_button_allows_authorized_user() -> None:
    manager = RecordingTeamRegisterManager()
    manager.config_exists = False
    interaction = FakeInteraction()
    button = TeamRegisterButton("Set Up Team Register", manager)

    await button.callback(interaction)

    assert len(interaction.response.modals) == 1
    assert isinstance(interaction.response.modals[0], TeamRegisterSheetModal)
    assert interaction.response.messages == []
```

Add Team stale setup test:

```python
@pytest.mark.asyncio
async def test_team_setup_button_with_existing_config_sends_current_panel() -> None:
    manager = RecordingTeamRegisterManager()
    interaction = FakeInteraction()
    button = TeamRegisterButton("Set Up Team Register", manager)

    await button.callback(interaction)

    assert interaction.response.deferred == [True]
    assert interaction.response.modals == []
    assert len(interaction.followup.messages) == 1
    content, kwargs = interaction.followup.messages[0]
    assert content == (
        "Team Register is already configured for this channel. "
        "Here are the current settings."
    )
    assert kwargs["ephemeral"] is True
    assert kwargs["embed"].title == "Team Register Settings"
    assert kwargs["embed"].description == (
        "Team Register is configured for this channel. "
        "Use the buttons below to update sheet settings or Encore roles."
    )
```

Add saved/current copy assertions to `test_team_modal_submit_allows_authorized_user()`:

```python
    _, kwargs = interaction.followup.messages[0]
    embed = kwargs["embed"]
    assert embed.title == "Team Register Settings Saved"
    assert embed.description == (
        "Your Team Register settings were saved. "
        "Use the buttons below to edit sheet settings or Encore roles."
    )
```

- [ ] **Step 3: Write failing Shift stale setup and copy tests**

Update existing Shift setup button test:

```python
@pytest.mark.asyncio
async def test_shift_settings_button_allows_authorized_user() -> None:
    manager = RecordingShiftRegisterManager()
    manager.config_exists = False
    interaction = FakeInteraction()
    button = ShiftRegisterButton("Set Up Shift Register", manager)

    await button.callback(interaction)

    assert len(interaction.response.modals) == 1
    assert isinstance(interaction.response.modals[0], ShiftRegisterSheetModal)
    assert interaction.response.messages == []
```

Add Shift stale setup test:

```python
@pytest.mark.asyncio
async def test_shift_setup_button_with_existing_config_sends_current_panel() -> None:
    manager = RecordingShiftRegisterManager()
    interaction = FakeInteraction()
    button = ShiftRegisterButton("Set Up Shift Register", manager)

    await button.callback(interaction)

    assert interaction.response.deferred == [True]
    assert interaction.response.modals == []
    assert len(interaction.followup.messages) == 1
    content, kwargs = interaction.followup.messages[0]
    assert content == (
        "Shift Register is already configured for this channel. "
        "Here are the current settings."
    )
    assert kwargs["ephemeral"] is True
    assert kwargs["embed"].title == "Shift Register Settings"
    assert kwargs["embed"].description == (
        "Shift Register is configured for this channel. "
        "Use the button below to update sheet settings."
    )
    assert kwargs["embed"].footer.text is None
```

Add Shift edit-button missing-settings guard test:

```python
@pytest.mark.asyncio
async def test_shift_edit_settings_button_uses_fresh_missing_settings_guard() -> None:
    manager = RecordingShiftRegisterManager()
    manager.config_exists = False
    interaction = FakeInteraction()
    view = ShiftRegisterView(
        manager,
        has_existing_settings=True,
        sheet_url="https://sheet.example",
        entry_worksheet_title="Entry",
        draft_worksheet_title="Draft",
        final_schedule_worksheet_title="Final",
        final_schedule_anchor_cell="B2",
    )

    await child_with_label(view, "Edit Shift Register Settings").callback(interaction)

    assert interaction.response.messages == [
        (
            "Shift Register settings are no longer configured for this channel.",
            {"ephemeral": True},
        )
    ]
    assert interaction.response.modals == []
```

Add Shift edit-modal missing-settings guard test:

```python
@pytest.mark.asyncio
async def test_shift_edit_modal_submit_uses_fresh_missing_settings_guard() -> None:
    manager = RecordingShiftRegisterManager()
    manager.config_exists = False
    interaction = FakeInteraction()
    modal = ShiftRegisterSheetModal(
        manager,
        sheet_url="https://sheet.example",
        entry_worksheet_title="Entry",
        draft_worksheet_title="Draft",
        final_schedule_worksheet_title="Final",
        final_schedule_anchor_cell="B2",
        requires_existing_settings=True,
    )

    await modal.on_submit(interaction)

    assert interaction.response.messages == [
        (
            "Shift Register settings are no longer configured for this channel.",
            {"ephemeral": True},
        )
    ]
    assert interaction.response.deferred == []
    assert manager.upsert_calls == []
    assert manager.anchor_updates == []
```

Add saved copy assertion to `test_shift_modal_submit_allows_authorized_user()`:

```python
    _, kwargs = interaction.followup.messages[0]
    embed = kwargs["embed"]
    assert embed.title == "Shift Register Settings Saved"
    assert embed.description == (
        "Your Shift Register settings were saved. "
        "Use the button below to edit sheet settings."
    )
    assert embed.footer.text is None
```

- [ ] **Step 4: Run UI tests to verify failures**

Run:

```bash
env UV_CACHE_DIR=.cache/uv uv run pytest tests/test_ui_permissions.py -q
```

Expected: stale setup and copy tests fail because current buttons open modals and embeds lack the new title/description/footer behavior.

- [ ] **Step 5: Implement Team settings panel builder and stale setup behavior**

In `components/ui_team_register.py`, import shared helpers:

```python
from components.ui_settings_flow import (
    SettingsPanel,
    send_stale_setup_panel_if_configured,
    settings_description,
    settings_title,
)
```

Add constants near existing Team constants:

```python
TEAM_REGISTER_DISPLAY_NAME = "Team Register"
TEAM_REGISTER_CURRENT_CONTROLS = (
    "Use the buttons below to update sheet settings or Encore roles."
)
TEAM_REGISTER_SAVED_CONTROLS = (
    "Use the buttons below to edit sheet settings or Encore roles."
)
TEAM_REGISTER_WORKSHEET_FOOTER = (
    "To add worksheet titles, edit sheet settings and include all existing titles "
    "plus any new ones."
)
```

Add this helper before `TeamRegisterSheetModal`:

```python
async def build_team_register_settings_panel(
    team_register_manager: TeamRegisterManager,
    interaction: Interaction,
    team_register: object,
    *,
    is_save_action: bool = False,
    metadata: TeamRegisterGoogleSheetsMetadata | None = None,
) -> SettingsPanel:
    active_metadata = (
        metadata or await team_register_manager.fetch_google_sheets_metadata()
    )
    roles = list(interaction.guild.roles) if interaction.guild else []
    encore_role_ids = list(getattr(team_register, "encore_role_ids", []))
    embed = build_current_settings_embed(
        sheet_url=team_register.sheet_url,
        metadata=active_metadata,
        encore_role_ids=encore_role_ids,
        color=config.DEFAULT_EMBED_COLOR,
        roles=roles,
        is_save_action=is_save_action,
    )
    view = TeamRegisterView(
        team_register_manager=team_register_manager,
        has_existing_settings=True,
        roles=roles,
        encore_role_ids=encore_role_ids,
        metadata=active_metadata,
        is_save_action=is_save_action,
    )
    return SettingsPanel(embed=embed, view=view)
```

Update `TeamRegisterView.__init__()` to accept `is_save_action` and pass button style state:

```python
        is_save_action: bool = False,
```

Remove this line from `TeamRegisterView.__init__()` because the constructor now
uses the values to decide button emphasis:

```python
        del roles, encore_role_ids
```

Keep the setup/edit button creation, then change Encore button addition:

```python
        if metadata is not None:
            role_resolution = resolve_encore_roles(encore_role_ids or [], roles or [])
            self.add_item(
                EditEncoreRolesButton(
                    team_register_manager,
                    metadata=metadata,
                    style=(
                        ButtonStyle.primary
                        if is_save_action and not role_resolution.active_roles
                        else ButtonStyle.secondary
                    ),
                )
            )
```

Update `EditEncoreRolesButton.__init__()`:

```python
    def __init__(
        self,
        team_register_manager: TeamRegisterManager,
        *,
        metadata: TeamRegisterGoogleSheetsMetadata,
        style: ButtonStyle = ButtonStyle.secondary,
    ) -> None:
        super().__init__(label="Edit Encore Roles", style=style)
```

Update `TeamRegisterButton.callback()`:

```python
        async def build_current_panel(team_register: object) -> SettingsPanel:
            return await build_team_register_settings_panel(
                self.team_register_manager,
                interaction,
                team_register,
            )

        if not self.requires_existing_settings:
            handled = await send_stale_setup_panel_if_configured(
                interaction,
                self.team_register_manager,
                feature_display_name=TEAM_REGISTER_DISPLAY_NAME,
                build_current_panel=build_current_panel,
            )
            if handled:
                return
```

Keep the existing `requires_existing_settings` guard for edit buttons after this block.

Update `TeamRegisterSheetModal.on_submit()` after saving:

```python
            team_register = await self.team_register_manager.get_sheet_config()
```

Replace the manual embed/view construction with:

```python
        panel = await build_team_register_settings_panel(
            self.team_register_manager,
            interaction,
            team_register,
            is_save_action=True,
            metadata=metadata,
        )

        await interaction.followup.send(
            embed=panel.embed,
            view=panel.view,
            ephemeral=True,
        )
```

Update `build_current_settings_embed()` title and description:

```python
    embed = Embed(
        title=settings_title(
            TEAM_REGISTER_DISPLAY_NAME,
            is_save_action=is_save_action,
        ),
        color=color,
    )
    embed.description = settings_description(
        TEAM_REGISTER_DISPLAY_NAME,
        TEAM_REGISTER_SAVED_CONTROLS
        if is_save_action
        else TEAM_REGISTER_CURRENT_CONTROLS,
        is_save_action=is_save_action,
    )
```

Update the footer:

```python
    embed.set_footer(text=TEAM_REGISTER_WORKSHEET_FOOTER)
```

- [ ] **Step 6: Implement Shift settings panel builder and stale setup behavior**

In `components/ui_shift_register.py`, import shared helpers:

```python
from components.ui_settings_flow import (
    SettingsPanel,
    send_stale_setup_panel_if_configured,
    settings_description,
    settings_title,
)
```

Add constants:

```python
SHIFT_REGISTER_DISPLAY_NAME = "Shift Register"
SHIFT_REGISTER_CURRENT_CONTROLS = "Use the button below to update sheet settings."
SHIFT_REGISTER_SAVED_CONTROLS = "Use the button below to edit sheet settings."
```

Add this helper before `ShiftRegisterSheetModal`:

```python
async def build_shift_register_settings_panel(
    shift_register_manager: ShiftRegisterManager,
    shift_register: object,
    *,
    is_save_action: bool = False,
    metadata: ShiftRegisterGoogleSheetsMetadata | None = None,
) -> SettingsPanel:
    active_metadata = (
        metadata or await shift_register_manager.fetch_google_sheets_metadata()
    )
    final_schedule_anchor_cell = getattr(
        shift_register,
        "final_schedule_anchor_cell",
        "A1",
    )
    embed = build_current_settings_embed(
        sheet_url=shift_register.sheet_url,
        metadata=active_metadata,
        final_schedule_anchor_cell=final_schedule_anchor_cell,
        color=config.DEFAULT_EMBED_COLOR,
        is_save_action=is_save_action,
    )
    view = ShiftRegisterView(
        shift_register_manager=shift_register_manager,
        has_existing_settings=True,
        sheet_url=shift_register.sheet_url,
        entry_worksheet_title=active_metadata.entry_worksheets.title,
        draft_worksheet_title=active_metadata.draft_worksheet.title,
        final_schedule_worksheet_title=active_metadata.final_schedule_worksheet.title,
        final_schedule_anchor_cell=final_schedule_anchor_cell,
    )
    return SettingsPanel(embed=embed, view=view)
```

Add Shift missing-settings helpers near the Shift constants:

```python
SHIFT_REGISTER_SETTINGS_MISSING_MESSAGE = (
    "Shift Register settings are no longer configured for this channel."
)


async def send_shift_settings_missing(interaction: Interaction) -> None:
    await interaction.response.send_message(
        SHIFT_REGISTER_SETTINGS_MISSING_MESSAGE,
        ephemeral=True,
    )


async def get_fresh_shift_register_config_or_respond(
    shift_register_manager: ShiftRegisterManager,
    interaction: Interaction,
) -> object | None:
    shift_register = await shift_register_manager.get_fresh_sheet_config()
    if shift_register is None:
        await send_shift_settings_missing(interaction)
        return None
    return shift_register
```

Update `ShiftRegisterSheetModal.__init__()` to accept and store
`requires_existing_settings`:

```python
        *,
        requires_existing_settings: bool = False,
```

Set it after assigning `self.shift_register_manager`:

```python
        self.requires_existing_settings = requires_existing_settings
```

At the start of `ShiftRegisterSheetModal.on_submit()`, after permission check
and before deferring, add:

```python
        if self.requires_existing_settings:
            shift_register = await get_fresh_shift_register_config_or_respond(
                self.shift_register_manager,
                interaction,
            )
            if shift_register is None:
                return
```

Update `ShiftRegisterButton.__init__()` to accept and store
`requires_existing_settings`:

```python
        *,
        requires_existing_settings: bool = False,
```

Store it:

```python
        self.requires_existing_settings = requires_existing_settings
```

Update `ShiftRegisterButton.callback()`:

```python
        async def build_current_panel(shift_register: object) -> SettingsPanel:
            return await build_shift_register_settings_panel(
                self.shift_register_manager,
                shift_register,
            )

        sheet_url = self.sheet_url
        final_schedule_anchor_cell = self.final_schedule_anchor_cell
        if self.requires_existing_settings:
            shift_register = await get_fresh_shift_register_config_or_respond(
                self.shift_register_manager,
                interaction,
            )
            if shift_register is None:
                return
            sheet_url = shift_register.sheet_url
            final_schedule_anchor_cell = shift_register.final_schedule_anchor_cell
        else:
            handled = await send_stale_setup_panel_if_configured(
                interaction,
                self.shift_register_manager,
                feature_display_name=SHIFT_REGISTER_DISPLAY_NAME,
                build_current_panel=build_current_panel,
            )
            if handled:
                return
```

Pass the resolved values and `requires_existing_settings` into
`ShiftRegisterSheetModal`:

```python
        await interaction.response.send_modal(
            ShiftRegisterSheetModal(
                shift_register_manager=self.shift_register_manager,
                sheet_url=sheet_url,
                entry_worksheet_title=self.entry_worksheet_title,
                draft_worksheet_title=self.draft_worksheet_title,
                final_schedule_worksheet_title=self.final_schedule_worksheet_title,
                final_schedule_anchor_cell=final_schedule_anchor_cell,
                requires_existing_settings=self.requires_existing_settings,
            )
        )
```

Update `ShiftRegisterView.__init__()` to pass the flag:

```python
            requires_existing_settings=has_existing_settings,
```

Update `ShiftRegisterSheetModal.on_submit()` after updating the anchor cell:

```python
        shift_register = await self.shift_register_manager.get_sheet_config()
        panel = await build_shift_register_settings_panel(
            self.shift_register_manager,
            shift_register,
            is_save_action=True,
            metadata=metadata,
        )

        await interaction.followup.send(
            embed=panel.embed,
            view=panel.view,
            ephemeral=True,
        )
```

Update `build_current_settings_embed()`:

```python
    embed = Embed(
        title=settings_title(
            SHIFT_REGISTER_DISPLAY_NAME,
            is_save_action=is_save_action,
        ),
        color=color,
    )
    embed.description = settings_description(
        SHIFT_REGISTER_DISPLAY_NAME,
        SHIFT_REGISTER_SAVED_CONTROLS
        if is_save_action
        else SHIFT_REGISTER_CURRENT_CONTROLS,
        is_save_action=is_save_action,
    )
```

Remove this footer call:

```python
    embed.set_footer(text="To edit sheet settings, use the settings button.")
```

- [ ] **Step 7: Update cogs to use shared setup/current decision**

In `cogs/team_register.py`, import:

```python
from components.ui_settings_flow import send_current_panel_followup
```

In the Team imports from `components.ui_team_register`, include:

```python
    TeamRegisterView,
    build_team_register_settings_panel,
```

Simplify configured branch in `setup_after_enable()`:

```python
        if team_register_config is None:
            content = (
                "Team Register is not yet configured for this channel. "
                "Click below to set up."
            )
            view = TeamRegisterView(team_register_manager=manager)
            await interaction.followup.send(content=content, view=view, ephemeral=True)
            return

        try:
            panel = await build_team_register_settings_panel(
                manager,
                interaction,
                team_register_config,
            )
        except GoogleSheetsError:
            # Current code routes classified storage failures through storage-error helpers.
            return

        await send_current_panel_followup(interaction, panel)
```

In `cogs/shift_register.py`, import:

```python
from components.ui_settings_flow import send_current_panel_followup
```

In the Shift imports from `components.ui_shift_register`, include:

```python
    ShiftRegisterView,
    build_shift_register_settings_panel,
```

Simplify configured branch in `setup_after_enable()`:

```python
        if shift_register_config is None:
            content = (
                "Shift Register is not yet configured for this channel. "
                "Click below to set up."
            )
            view = ShiftRegisterView(shift_register_manager=manager)
            await interaction.followup.send(content=content, view=view, ephemeral=True)
            return

        try:
            panel = await build_shift_register_settings_panel(
                manager,
                shift_register_config,
            )
        except GoogleSheetsError:
            # Current code routes classified storage failures through storage-error helpers.
            return

        await send_current_panel_followup(interaction, panel)
```

- [ ] **Step 8: Run focused lifecycle tests**

Run:

```bash
env UV_CACHE_DIR=.cache/uv uv run pytest tests/test_settings_flow.py tests/test_ui_permissions.py -q
```

Expected: all selected tests pass after the Task 2 implementation is complete.

- [ ] **Step 9: Review checkpoint**

Do not commit. Check:

```bash
git diff -- components/ui_settings_flow.py components/ui_team_register.py components/ui_shift_register.py cogs/team_register.py cogs/shift_register.py tests/test_ui_permissions.py
```

Expected: settings flow helper is shared; feature-specific panel builders stay in feature UI modules.

---

### Task 3: Team Register Encore Role UX

**Files:**
- Modify: `components/ui_team_register.py`
- Modify: `tests/test_team_register_encore_roles.py`
- Modify: `tests/test_ui_permissions.py`

**Interfaces:**
- Consumes: `build_team_register_settings_panel(...)`
- Produces: `EncoreRolePreviewView(..., removed_missing_role_ids: Sequence[int] = ())`
- Produces: `RemoveMissingIdsButton`
- Removes: preview-level `Remove Missing From Draft` button behavior

- [ ] **Step 1: Write failing pure embed tests for new Encore copy**

In `tests/test_team_register_encore_roles.py`, update retained missing expectation:

```python
def test_encore_role_preview_omits_warning_without_everyone() -> None:
    embed = build_encore_role_preview_embed(
        selected_roles=[FakeRole(id=20, name="Encore", position=1)],
        retained_missing_role_ids=(99,),
        guild_id=111,
    )

    field_by_name = {field.name: field.value for field in embed.fields}
    assert "⚠ Warnings" not in field_by_name
    assert field_by_name["Retained Missing Role IDs"] == (
        "`99`\nThese IDs will stay saved after you confirm."
    )
```

Add preview description and removed missing test:

```python
def test_encore_role_preview_shows_not_saved_description() -> None:
    embed = build_encore_role_preview_embed(
        selected_roles=[],
        retained_missing_role_ids=(),
        guild_id=111,
    )

    assert embed.description == (
        "Review the Encore roles before saving. "
        "Changes are not saved until you confirm."
    )


def test_encore_role_preview_shows_removed_missing_ids() -> None:
    embed = build_encore_role_preview_embed(
        selected_roles=[FakeRole(id=20, name="Encore", position=1)],
        retained_missing_role_ids=(),
        removed_missing_role_ids=(99,),
        guild_id=111,
    )

    field_by_name = {field.name: field.value for field in embed.fields}
    assert field_by_name["Removed Missing Role IDs"] == (
        "`99`\nThese IDs will be removed when you confirm."
    )
```

- [ ] **Step 2: Write failing UI tests for revised buttons and cancel**

In `tests/test_ui_permissions.py`, update import expectations after implementation:

```python
from components.ui_team_register import (
    BackToTeamSettingsButton,
    EditEncoreRolesButton,
    EncoreRoleEditView,
    EncoreRolePreviewView,
    EncoreRoleSelect,
    TeamRegisterButton,
    TeamRegisterSheetModal,
    TeamRegisterView,
)
```

Replace `test_remove_missing_updates_preview_without_saving()`:

```python
@pytest.mark.asyncio
async def test_remove_missing_ids_from_edit_view_previews_removal_without_saving() -> None:
    manager = RecordingTeamRegisterManager()
    role = FakeRole(id=1, name="Encore", position=10)
    interaction = FakeInteraction(roles=[role])
    view = EncoreRoleEditView(
        manager,
        metadata=team_register_metadata(),
        roles=[role],
        encore_role_ids=[1, 99],
        retained_missing_role_ids=[99],
    )

    await child_with_label(view, "Remove Missing IDs").callback(interaction)

    assert manager.encore_role_id_updates == []
    _, edit_kwargs = interaction.response.edits[0]
    updated_view = edit_kwargs["view"]
    assert isinstance(updated_view, EncoreRolePreviewView)
    assert updated_view.selected_roles == (role,)
    assert updated_view.retained_missing_role_ids == ()
    assert updated_view.removed_missing_role_ids == (99,)
    assert child_with_label(updated_view, "Confirm Save") is not None
    assert child_with_label(updated_view, "Cancel") is not None
    assert all(
        getattr(child, "label", None) != "Remove Missing IDs"
        for child in updated_view.children
    )
```

Replace `test_missing_only_edit_view_can_preview_missing_cleanup()`:

```python
@pytest.mark.asyncio
async def test_missing_only_edit_view_can_preview_missing_cleanup() -> None:
    manager = RecordingTeamRegisterManager()
    interaction = FakeInteraction(roles=[])
    view = EncoreRoleEditView(
        manager,
        metadata=team_register_metadata(),
        roles=[],
        encore_role_ids=[99],
        retained_missing_role_ids=[99],
    )

    await child_with_label(view, "Remove Missing IDs").callback(interaction)

    assert manager.encore_role_id_updates == []
    _, edit_kwargs = interaction.response.edits[0]
    updated_view = edit_kwargs["view"]
    assert isinstance(updated_view, EncoreRolePreviewView)
    assert updated_view.selected_roles == ()
    assert updated_view.retained_missing_role_ids == ()
    assert updated_view.removed_missing_role_ids == (99,)
```

Replace `test_remove_missing_denies_unauthorized_user()`:

```python
@pytest.mark.asyncio
async def test_remove_missing_denies_unauthorized_user() -> None:
    manager = RecordingTeamRegisterManager()
    role = FakeRole(id=1, name="Encore", position=10)
    interaction = unauthorized_interaction()
    view = EncoreRoleEditView(
        manager,
        metadata=team_register_metadata(),
        roles=[role],
        encore_role_ids=[1, 99],
        retained_missing_role_ids=[99],
    )

    await child_with_label(view, "Remove Missing IDs").callback(interaction)

    assert_permission_denied(interaction)
    assert manager.encore_role_id_updates == []
    assert interaction.response.edits == []
```

Replace `test_remove_missing_uses_fresh_missing_settings_guard()`:

```python
@pytest.mark.asyncio
async def test_remove_missing_uses_fresh_missing_settings_guard() -> None:
    manager = RecordingTeamRegisterManager()
    manager.config_exists = False
    role = FakeRole(id=1, name="Encore", position=10)
    interaction = FakeInteraction(roles=[role])
    view = EncoreRoleEditView(
        manager,
        metadata=team_register_metadata(),
        roles=[role],
        encore_role_ids=[1, 99],
        retained_missing_role_ids=[99],
    )

    await child_with_label(view, "Remove Missing IDs").callback(interaction)

    assert interaction.response.messages == [
        (
            "Team Register settings are no longer configured for this channel.",
            {"ephemeral": True},
        )
    ]
    assert manager.encore_role_id_updates == []
    assert interaction.response.edits == []
```

Replace cancel test:

```python
@pytest.mark.asyncio
async def test_encore_role_cancel_returns_to_settings_without_saving() -> None:
    manager = RecordingTeamRegisterManager()
    role = FakeRole(id=1, name="Encore", position=10)
    interaction = FakeInteraction(roles=[role])
    view = EncoreRolePreviewView(
        manager,
        selected_roles=[role],
        retained_missing_role_ids=[99],
        metadata=team_register_metadata(),
    )

    await child_with_label(view, "Cancel").callback(interaction)

    assert manager.encore_role_id_updates == []
    content, edit_kwargs = interaction.response.edits[0]
    assert content == "Cancelled. No changes saved."
    assert edit_kwargs["embed"].title == "Team Register Settings"
    assert isinstance(edit_kwargs["view"], TeamRegisterView)
```

Add confirm removed missing test:

```python
@pytest.mark.asyncio
async def test_encore_role_confirm_omits_removed_missing_ids() -> None:
    manager = RecordingTeamRegisterManager()
    role = FakeRole(id=1, name="Encore", position=10)
    interaction = FakeInteraction(roles=[role])
    view = EncoreRolePreviewView(
        manager,
        selected_roles=[role],
        retained_missing_role_ids=[],
        removed_missing_role_ids=[99],
        metadata=team_register_metadata(),
    )

    await child_with_label(view, "Confirm Save").callback(interaction)

    assert manager.encore_role_id_updates == [[1]]
```

- [ ] **Step 3: Run tests to verify failures**

Run:

```bash
env UV_CACHE_DIR=.cache/uv uv run pytest tests/test_team_register_encore_roles.py tests/test_ui_permissions.py -q
```

Expected: fail because preview embed does not accept `removed_missing_role_ids`, remove button label is old, preview still has remove button, and cancel disables preview.

- [ ] **Step 4: Implement Team Encore role copy and preview state**

In `components/ui_team_register.py`, update `build_encore_role_edit_embed()`:

```python
    embed.description = "Choose Discord roles to show for matching members."
    if retained_missing_role_ids:
        embed.add_field(
            name="Missing Encore Role IDs",
            value=(
                f"{format_role_ids(retained_missing_role_ids)}\n"
                "Retained until removed during Encore role editing."
            ),
            inline=False,
        )
```

Update `build_encore_role_preview_embed()` signature and body:

```python
def build_encore_role_preview_embed(
    selected_roles: Sequence[Role],
    retained_missing_role_ids: Sequence[int],
    guild_id: int | None,
    removed_missing_role_ids: Sequence[int] = (),
) -> Embed:
    embed = Embed(title="Preview Encore Role Changes", color=config.DEFAULT_EMBED_COLOR)
    embed.description = (
        "Review the Encore roles before saving. "
        "Changes are not saved until you confirm."
    )
    embed.add_field(
        name="Selected Encore Roles",
        value=(
            format_role_mentions(selected_roles)
            if selected_roles
            else "No active encore roles selected."
        ),
        inline=False,
    )
    if retained_missing_role_ids:
        embed.add_field(
            name="Retained Missing Role IDs",
            value=(
                f"{format_role_ids(retained_missing_role_ids)}\n"
                "These IDs will stay saved after you confirm."
            ),
            inline=False,
        )
    if removed_missing_role_ids:
        embed.add_field(
            name="Removed Missing Role IDs",
            value=(
                f"{format_role_ids(removed_missing_role_ids)}\n"
                "These IDs will be removed when you confirm."
            ),
            inline=False,
        )
```

Keep the existing `@everyone` warning block after these missing-ID fields.

Update empty Encore field in `build_current_settings_embed()`:

```python
        encore_roles_value = (
            "No encore roles set yet. Use Edit Encore Roles to choose Discord "
            "roles to show for matching members."
        )
```

Update settings missing role IDs field:

```python
            value=(
                f"{format_role_ids(role_resolution.missing_role_ids)}\n"
                "Retained until removed during Encore role editing."
            ),
```

- [ ] **Step 5: Implement edit-level remove missing and preview-only buttons**

Rename the button class:

```python
class RemoveMissingIdsButton(Button):
    def __init__(self) -> None:
        super().__init__(label="Remove Missing IDs", style=ButtonStyle.danger)
```

Use this callback:

```python
    async def callback(self, interaction: Interaction) -> None:
        view = self.view
        if not isinstance(view, EncoreRoleEditView):
            return
        if not await require_settings_permissions(interaction):
            return

        team_register = await get_fresh_team_register_config_or_respond(
            view.team_register_manager,
            interaction,
        )
        if team_register is None:
            return

        updated_view = EncoreRolePreviewView(
            team_register_manager=view.team_register_manager,
            selected_roles=view.active_roles,
            retained_missing_role_ids=(),
            removed_missing_role_ids=view.retained_missing_role_ids,
            metadata=view.metadata,
        )
        await interaction.response.edit_message(
            content=None,
            embed=build_encore_role_preview_embed(
                selected_roles=view.active_roles,
                retained_missing_role_ids=(),
                removed_missing_role_ids=view.retained_missing_role_ids,
                guild_id=interaction.guild.id if interaction.guild else None,
            ),
            view=updated_view,
        )
```

Update `EncoreRoleEditView.__init__()`:

```python
        if self.retained_missing_role_ids:
            self.add_item(RemoveMissingIdsButton())
```

Update `EncoreRolePreviewView.__init__()`:

```python
        removed_missing_role_ids: Sequence[int] = (),
```

Inside it:

```python
        self.removed_missing_role_ids = tuple(removed_missing_role_ids)
        self.add_item(ConfirmEncoreRolesButton())
        self.add_item(CancelEncoreRolesButton())
```

Do not add a remove button in preview.

Update `EncoreRoleSelect.callback()` to pass no removed IDs:

```python
            view=EncoreRolePreviewView(
                team_register_manager=self.team_register_manager,
                selected_roles=selected_roles,
                retained_missing_role_ids=self.retained_missing_role_ids,
                removed_missing_role_ids=(),
                metadata=self.metadata,
            ),
```

Update the preview embed call there:

```python
                removed_missing_role_ids=(),
```

- [ ] **Step 6: Implement cancel returning to settings**

Replace `CancelEncoreRolesButton.callback()` body after permission and guard:

```python
        team_register = await get_fresh_team_register_config_or_respond(
            view.team_register_manager,
            interaction,
        )
        if team_register is None:
            return

        roles = list(interaction.guild.roles) if interaction.guild else []
        await interaction.response.edit_message(
            content="Cancelled. No changes saved.",
            embed=build_current_settings_embed(
                sheet_url=team_register.sheet_url,
                metadata=view.metadata,
                encore_role_ids=team_register.encore_role_ids,
                color=config.DEFAULT_EMBED_COLOR,
                roles=roles,
            ),
            view=TeamRegisterView(
                team_register_manager=view.team_register_manager,
                has_existing_settings=True,
                roles=roles,
                encore_role_ids=team_register.encore_role_ids,
                metadata=view.metadata,
            ),
        )
```

- [ ] **Step 7: Confirm save uses retained IDs only**

Keep `ConfirmEncoreRolesButton.callback()` role ID calculation as retained-only:

```python
        role_ids = unique_role_ids(
            [role.id for role in view.selected_roles]
            + list(view.retained_missing_role_ids)
        )
```

No removed IDs are added to this list.

Pass `removed_missing_role_ids` in existing preview rebuilds only when building removed previews.

- [ ] **Step 8: Run focused Encore tests**

Run:

```bash
env UV_CACHE_DIR=.cache/uv uv run pytest tests/test_team_register_encore_roles.py tests/test_ui_permissions.py -q
```

Expected: all Team UI and pure Encore tests pass.

- [ ] **Step 9: Review checkpoint**

Do not commit. Check:

```bash
git diff -- components/ui_team_register.py tests/test_team_register_encore_roles.py tests/test_ui_permissions.py
```

Expected: preview has only Confirm/Cancel, remove missing exists only in edit view, cancel returns to settings.

---

### Task 4: Cog-Level Regression Coverage For `/settings` And `/enable`

**Files:**
- Modify: `tests/test_feature_channel_interactions.py`
- Modify: `cogs/team_register.py`
- Modify: `cogs/shift_register.py`

**Interfaces:**
- Consumes: `build_team_register_settings_panel(...)`
- Consumes: `build_shift_register_settings_panel(...)`
- Consumes: `send_current_panel_followup(...)`

- [ ] **Step 1: Inspect existing feature interaction tests**

Read:

```bash
sed -n '1,260p' tests/test_feature_channel_interactions.py
```

Use the existing pattern in this file: call command callbacks with a
`SimpleNamespace` subject instead of instantiating full cogs.

- [ ] **Step 2: Add `/settings` command routing tests**

In `tests/test_feature_channel_interactions.py`, update the cog imports:

```python
from cogs.shift_register import ShiftRegister
from cogs.team_register import TeamRegister
```

Add these tests:

```python
@pytest.mark.asyncio
async def test_team_settings_command_defers_and_reuses_setup_after_enable() -> None:
    called = 0

    async def fake_setup_after_enable(interaction: object) -> None:
        nonlocal called
        called += 1

    subject = SimpleNamespace(setup_after_enable=fake_setup_after_enable)
    interaction = FakeInteraction()

    await TeamRegister.settings.callback(subject, interaction)

    assert interaction.response.deferred == [True]
    assert called == 1


@pytest.mark.asyncio
async def test_shift_settings_command_defers_and_reuses_setup_after_enable() -> None:
    called = 0

    async def fake_setup_after_enable(interaction: object) -> None:
        nonlocal called
        called += 1

    subject = SimpleNamespace(setup_after_enable=fake_setup_after_enable)
    interaction = FakeInteraction()

    await ShiftRegister.settings.callback(subject, interaction)

    assert interaction.response.deferred == [True]
    assert called == 1
```

- [ ] **Step 3: Run feature interaction tests**

Run:

```bash
env UV_CACHE_DIR=.cache/uv uv run pytest tests/test_feature_channel_interactions.py -q
```

Expected: pass. The tests call command callbacks with a `SimpleNamespace`
subject and do not instantiate the cogs.

- [ ] **Step 4: Review checkpoint**

Do not commit. Check:

```bash
git diff -- tests/test_feature_channel_interactions.py cogs/team_register.py cogs/shift_register.py
```

Expected: cogs still expose the same command names and route settings through setup/current lifecycle.

---

### Task 5: Full Validation And Manual Checklist

**Files:**
- No required file changes.
- Optional validation reference: `docs/manual_integration_validation.md`

**Interfaces:**
- Consumes all implemented behavior from Tasks 1-4.
- Produces verification evidence for final response.

- [ ] **Step 1: Run focused automated tests**

Run:

```bash
env UV_CACHE_DIR=.cache/uv uv run pytest tests/test_manager_fakes.py tests/test_settings_flow.py tests/test_team_register_encore_roles.py tests/test_ui_permissions.py tests/test_feature_channel_interactions.py -q
```

Expected: all selected tests pass.

- [ ] **Step 2: Run repo lint checks without modifying files**

Run:

```bash
env UV_CACHE_DIR=.cache/uv uv run ruff check --no-fix .
```

Expected: exit code 0.

Run:

```bash
env UV_CACHE_DIR=.cache/uv uv run ruff format --check .
```

Expected: exit code 0.

- [ ] **Step 3: Run full automated test suite**

Run:

```bash
env UV_CACHE_DIR=.cache/uv uv run pytest
```

Expected: all tests pass.

- [ ] **Step 4: Run compile check**

Run:

```bash
env UV_CACHE_DIR=.cache/uv uv run python -m compileall main.py bot cogs components models utils tests
```

Expected: command exits 0.

- [ ] **Step 5: Manual Discord validation in a dev guild**

Use a development guild and disposable Google Sheet. Do not use production
Sheets as the first validation surface.

Team Register checklist:

- Run `/team_register enable` with no config and confirm setup prompt appears.
- Complete setup and confirm `Team Register Settings Saved` appears.
- Confirm saved description mentions editing sheet settings or Encore roles.
- Confirm `Edit Encore Roles` is primary only when no active Encore roles exist.
- Click an old `Set Up Team Register` panel and confirm it sends current settings instead of an empty modal.
- Run `/team_register settings` and confirm stable current settings panel.
- Open `Edit Encore Roles`; confirm description says `Choose Discord roles to show for matching members.`
- With missing role IDs, click `Remove Missing IDs`; confirm preview shows `Removed Missing Role IDs`.
- Click `Cancel`; confirm no settings are written and the current settings panel returns.
- Repeat and click `Confirm Save`; confirm saved panel returns.

Shift Register checklist:

- Run `/shift_register enable` with no config and confirm setup prompt appears.
- Complete setup and confirm `Shift Register Settings Saved` appears.
- Confirm saved/current descriptions mention the settings button.
- Confirm no Shift footer remains.
- Click an old `Set Up Shift Register` panel and confirm it sends current settings instead of an empty modal.
- Run `/shift_register settings` and confirm stable current settings panel.
- Edit settings and confirm `final_schedule_anchor_cell` is still saved and displayed.

- [ ] **Step 6: Final review checkpoint**

Do not commit. Summarize:

```bash
git status --short
git diff --stat
```

Expected: modified implementation/test files plus design/plan docs only. No secrets, logs, local DB files, or service account files appear.
