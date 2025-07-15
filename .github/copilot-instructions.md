
# Copilot Instructions for rhoboto Discord Bot Project

## Project Overview
- **Discord bot** using `discord.py` (2.x), organized with modular cogs in `cogs/`.
- **Features**: Each feature is a cog inheriting from `FeatureChannelBase` (`cogs/base/feature_channel_base.py`), supporting per-channel enable/disable/clear.
- **Google Sheets**: All worksheet operations are via `utils/google_sheets.py` (`GoogleSheet` class). Only worksheet IDs are stored in the DB.
- **Database**: Uses Tortoise ORM. Key models: `FeatureChannel` (per-channel state), `TeamRegister` (Google Sheet link, worksheet IDs).
- **Settings/UI**: Uses Discord Modal and View patterns for setup/edit, ephemeral embeds for feedback. Worksheet titles/IDs are always shown in embeds.

## Key Patterns & Conventions
- Use `FeatureChannelBase` for feature enable/disable/clear. Soft disable sets `is_enabled=False`, hard clear deletes all settings.
- Use `feature_enabled_prefix_command_predicate` and `feature_enabled_app_command_predicate` for command guards.
- Always access Google Sheets via `GoogleSheet` class. Use lazy cache and batch worksheet queries for performance.
- For settings, use Discord Modal (`SheetModal`) and View (`TeamRegisterView`). Always provide an edit/setup button.
- Worksheet info is always shown as a list with title and ID. Use emoji or escaped emoji for status indicators.
- Use custom exceptions (e.g., `FeatureNotEnabled`) and centralized error handlers in cogs.
- All settings/edit commands require both `administrator` and `manage_channels` permissions.

## Developer Workflow
- **Add new features**: Create a new cog in `cogs/`, inherit from `FeatureChannelBase`, implement feature logic.
- **Google Sheets**: Update only via modal/view flows. Do not manipulate worksheet IDs directly.
- **Testing**: Manual via Discord UI. No automated test suite detected.
- **Debugging**: Use ephemeral embeds and logs. All DB changes are logged (see logger usage in base class).

## Integration Points
- **External**: Google Sheets API (via service account path in config), Discord API.
- **Internal**: All feature state flows through `FeatureChannel` and related models. Cross-feature queries use ORM filters.

## Code Style & Linting
- Use [Black](https://github.com/psf/black) (line length 88, target Python 3.13).
- Use [Ruff](https://github.com/astral-sh/ruff) for linting and import sorting; all rules enabled except D, COM812.
- Docstrings: Google Python style for all functions, classes, and modules.
- See `pyproject.toml` for exact configuration.

## Examples
- To add a new feature with per-channel enable/disable:
  - Create `cogs/myfeature.py`, inherit from `FeatureChannelBase`.
  - Use `@app_commands.check(FeatureChannelBase.feature_enabled_app_command_predicate(feature_name))` for slash commands.
  - Use modal/view for settings, and embed for status display.
- To show all features and their enabled state in a channel:
  - Query `FeatureChannel` for the channel, display each feature with status emoji (escaped if needed).

## Key Files
- `cogs/base/feature_channel_base.py`: Feature management base class, predicates, error handling.
- `utils/google_sheets.py`: GoogleSheet class, worksheet operations.
- `models/feature_channel.py`, `models/team_register.py`: DB models.
- `cogs/team_register.py`: Example of full feature setup/edit flow.
- `cogs/features.py`: Example of status query for all features.

---
If any conventions or workflows are unclear, please ask for clarification or examples from the codebase.
