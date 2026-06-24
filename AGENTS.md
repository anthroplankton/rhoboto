# Repository Guidelines

## Project Structure & Module Organization

Rhoboto is a Python 3.13 Discord bot built on `discord.py`. `main.py` is the entrypoint and auto-loads public cog modules from `cogs/`. Core setup, configuration, and translation live in `bot/`. Feature cogs live in `cogs/`, with shared enable/disable behavior in `cogs/base/feature_channel_base.py`. Discord UI views and modals are in `components/`. Tortoise ORM models are in `models/`; reusable services such as Google Sheets access, database setup, logging, and managers are in `utils/`. Runtime databases and logs belong under `data/` and should not be committed.

## Build, Test, and Development Commands

- `uv sync`: install runtime and developer dependencies from `pyproject.toml` and `uv.lock`.
- `uv run python main.py`: run the bot locally after setting required environment variables.
- `pre-commit install`: enable Black and Ruff hooks for commits.
- `pre-commit run --all-files`: run all configured formatting and lint checks.
- `uv run ruff check --no-fix .` and `uv run ruff format .`: lint and format with Ruff.
- `uv run black --check main.py bot cogs components models utils`: check Black formatting.

Deployment is configured by `.github/workflows/deploy.yml` and `Procfile` for Heroku (`worker: python main.py`).

## Coding Style & Naming Conventions

Use Black formatting with 88-character lines and Ruff settings from `pyproject.toml`. Ruff enables all lint rule families except `D` and `COM812`; keep imports sorted by Ruff/isort. Use Google-style docstrings for public modules, classes, and functions. Name cogs and feature modules in snake_case, for example `team_register.py`, and keep matching managers and structs in `utils/*_manager.py` and `utils/*_structs.py`. Prefer async APIs for Discord, database, and Google Sheets work.

## Testing Guidelines

No automated test suite is currently present. Before opening a PR, run `pre-commit run --all-files` and manually exercise affected Discord commands, modals, views, permissions, and Google Sheets flows in a development guild. When adding coverage, place tests under `tests/` using `test_*.py` names and add new dependencies to the relevant `pyproject.toml` dependency group.

## Commit & Pull Request Guidelines

Recent history uses Conventional Commit-style messages such as `feat: ...`, `fix: ...`, `docs(scope): ...`, `chore(scope): ...`, and `i18n: ...`. Keep commits focused and use a scope when helpful. Pull requests should include a behavior summary, manual validation steps, linked issues when applicable, screenshots for changed embeds/views, and notes for environment, database, or Google Sheets changes.

## Security & Configuration Tips

Required configuration is loaded from environment variables or `.env`: `DISCORD_TOKEN`, `DATABASE_URL`, and `GOOGLE_SERVICE_ACCOUNT_PATH`. Never commit tokens, service account JSON files, local databases, or logs.
