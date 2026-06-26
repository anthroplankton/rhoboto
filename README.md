# rhoboto

Rhoboto is a multi-feature Discord bot built with `discord.py` 2.x, supporting team registration, per-channel feature management, and Google Sheets integration.

## Architecture
- **Core**: All features are modular cogs located in the `cogs/` directory.
- **Feature Management**: Each feature inherits from `FeatureChannelBase`, supporting enable/disable/clear per channel.
- **Google Sheets Integration**: All sheet operations use the `GoogleSheet` class in `utils/google_sheets.py`; only worksheet IDs are stored in the database.
- **Database**: Uses Tortoise ORM. Main models: `FeatureChannel` (feature state), `TeamRegisterConfig` (Google Sheet link and worksheet IDs).
- **Settings & UI**: Uses Discord Modal/View interactions. All settings/edit commands require both administrator and manage_channels permissions.

## Quick Start
1. Install uv if needed:
   ```shell
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```
2. Install dependencies:
   ```shell
   uv sync
   ```
3. Configure environment variables (can use a `.env` file):
   - `DISCORD_TOKEN`: Your Discord Bot Token
   - `GOOGLE_SERVICE_ACCOUNT_PATH`: Path to your Google service account JSON
4. Start the bot:
   ```shell
   uv run python main.py
   ```

## Main Features
- Team registration and management (synced to Google Sheets)
- Per-channel feature enable/disable/clear
- Multi-language support (EN/JA/ZH)
- Discord Modal/View settings interaction

## Code Style
- [Black](https://github.com/psf/black) (line length 88, Python 3.13)
- [Ruff](https://github.com/astral-sh/ruff) (all rules except D, COM812, UP046)
- Google Python style docstrings

Run local checks with:
```shell
uv run ruff check --no-fix .
uv run ruff format --check .
uv run black --check --workers 1 main.py bot cogs components models utils
uv run pytest
```

Run the CI-style coverage gate with:

```shell
uv run pytest --cov=bot --cov=cogs --cov=components --cov=models --cov=utils --cov-report=term-missing --cov-fail-under=35
```

## Key Files
- `main.py`: Entrypoint, auto-loads all cogs
- `bot/config.py`: Loads environment variables and config
- `cogs/base/feature_channel_base.py`: Feature management base class
- `utils/google_sheets.py`: GoogleSheet API wrapper
- `models/feature_channel.py`, `models/team_register.py`: Database models
- `components/ui_team_register.py`: Modal/View settings interaction

## Contribution & Debugging
- Add new features by creating a cog and inheriting from `FeatureChannelBase`
- All database changes are logged
- Debug using Discord UI and log files
- See `docs/project_setup.md` for the project setup, validation, deployment, and agent harness contract
- Use `docs/manual_integration_validation.md` for manual Discord and Google Sheets validation
- See `docs/runtime_architecture_review.md` for current runtime architecture risks and priorities
- Agent/developer guidance is centralized in `AGENTS.md`; `.github/copilot-instructions.md` is only a compatibility pointer, and `.codex/config.toml` stores repo-local Codex defaults.

---
For detailed developer conventions, see `AGENTS.md`.
