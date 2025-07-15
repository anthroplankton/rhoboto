# rhoboto

Rhoboto is a multi-feature Discord bot built with `discord.py` 2.x, supporting team registration, per-channel feature management, and Google Sheets integration.

## Architecture
- **Core**: All features are modular cogs located in the `cogs/` directory.
- **Feature Management**: Each feature inherits from `FeatureChannelBase`, supporting enable/disable/clear per channel.
- **Google Sheets Integration**: All sheet operations use the `GoogleSheet` class in `utils/google_sheets.py`; only worksheet IDs are stored in the database.
- **Database**: Uses Tortoise ORM. Main models: `FeatureChannel` (feature state), `TeamRegisterConfig` (Google Sheet link and worksheet IDs).
- **Settings & UI**: Uses Discord Modal/View interactions. All settings/edit commands require both administrator and manage_channels permissions.

## Quick Start
1. Install dependencies:
   ```shell
   pip install -r requirements.txt
   ```
2. Configure environment variables (can use a `.env` file):
   - `DISCORD_TOKEN`: Your Discord Bot Token
   - `GOOGLE_SERVICE_ACCOUNT_PATH`: Path to your Google service account JSON
3. Start the bot:
   ```shell
   python main.py
   ```

## Main Features
- Team registration and management (synced to Google Sheets)
- Per-channel feature enable/disable/clear
- Multi-language support (EN/JA/ZH)
- Discord Modal/View settings interaction

## Code Style
- [Black](https://github.com/psf/black) (line length 88, Python 3.13)
- [Ruff](https://github.com/astral-sh/ruff) (all rules except D, COM812)
- Google Python style docstrings

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

---
For detailed developer conventions, see `.github/copilot-instructions.md`.

