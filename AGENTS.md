# Repository Guidelines

## Project Structure & Module Organization

Rhoboto is a Python 3.13 Discord bot built on `discord.py`. `main.py` is the entrypoint and auto-loads public cog modules from `cogs/`. Core setup, configuration, and translation live in `bot/`. Feature cogs live in `cogs/`, with shared enable/disable behavior in `cogs/base/feature_channel_base.py`. Discord UI views and modals are in `components/`. Tortoise ORM models are in `models/`; reusable services such as Google Sheets access, database setup, logging, and managers are in `utils/`. Runtime databases and logs belong under `data/` and should not be committed.

## Codex Operating Rules

Preserve existing behavior unless the user explicitly asks for a change. Do not push. Do not commit unless explicitly asked. Do not edit secrets, `.env`, service account JSON files, local databases, or logs. For documentation-only tasks, do not modify application code.

Do not change Discord command names, privileged intents, database schema, or Google Sheets column layout without an explicit migration plan that covers compatibility, rollout, and validation.

## Project Conventions

Use `FeatureChannelBase` for channel-scoped feature enable/disable/clear behavior. Soft disable should keep settings and set `is_enabled=False`; hard clear should delete the feature settings for that channel. Use `feature_enabled_prefix_command_predicate` and `feature_enabled_app_command_predicate` for guarded commands, and keep custom exceptions such as `FeatureNotEnabled` flowing through centralized cog error handlers.

Route Google Sheets work through `GoogleSheet` and the relevant manager APIs. Store worksheet IDs in the database, not worksheet objects or titles as durable identifiers. Settings changes should go through Discord Modal/View flows, and user-facing embeds should show worksheet titles together with worksheet IDs. All settings/edit commands must require both `administrator` and `manage_channels` permissions.

## Build, Test, and Development Commands

- `uv sync`: install runtime and developer dependencies from `pyproject.toml` and `uv.lock`.
- `uv run python main.py`: run the bot locally after setting required environment variables.
- `pre-commit install`: enable Black and Ruff hooks for commits.
- `pre-commit run --all-files`: run all configured formatting and lint checks.
- `uv run ruff check .`: lint with Ruff.
- `uv run ruff format --check .`: check Ruff formatting.
- `uv run black --check main.py bot cogs components models utils`: check Black formatting.

Deployment is configured by `.github/workflows/deploy.yml` and `Procfile` for Heroku (`worker: python main.py`).

## Coding Style & Naming Conventions

Use Black formatting with 88-character lines and Ruff settings from `pyproject.toml`. Ruff enables all lint rule families except `D` and `COM812`; keep imports sorted by Ruff/isort. Use Google-style docstrings for public modules, classes, and functions. Name cogs and feature modules in snake_case, for example `team_register.py`, and keep matching managers and structs in `utils/*_manager.py` and `utils/*_structs.py`. Prefer async APIs for Discord, database, and Google Sheets work.

## Testing Guidelines

No automated test suite is currently present. Before opening a PR, run `uv run ruff check .`, `uv run ruff format --check .`, and `pre-commit run --all-files`; once tests exist, run the test suite as well. Manually exercise affected Discord commands, modals, views, permissions, and Google Sheets flows in a development guild. When adding coverage, place tests under `tests/` using `test_*.py` names and add new dependencies to the relevant `pyproject.toml` dependency group.

## Commit & Pull Request Guidelines

Recent history uses Conventional Commit-style messages such as `feat: ...`, `fix: ...`, `docs(scope): ...`, `chore(scope): ...`, and `i18n: ...`. Keep commits focused and use a scope when helpful, but only commit when the user explicitly asks. Pull requests should include a behavior summary, manual validation steps, linked issues when applicable, screenshots for changed embeds/views, and notes for environment, database, or Google Sheets changes.

## Security & Configuration Tips

Required configuration is loaded from environment variables or `.env`: `DISCORD_TOKEN`, `DATABASE_URL`, and `GOOGLE_SERVICE_ACCOUNT_PATH`. Never commit tokens, service account JSON files, local databases, or logs.
