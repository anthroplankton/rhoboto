# Repository Guidelines

## Project Structure & Module Organization

Rhoboto is a Python 3.13 Discord bot built on `discord.py`. `main.py` is the entrypoint and auto-loads public cog modules from `cogs/`. Core setup, configuration, and translation live in `bot/`. Feature cogs live in `cogs/`, with shared enable/disable behavior in `cogs/base/feature_channel_base.py`. Discord UI views and modals are in `components/`. Tortoise ORM models are in `models/`; reusable services such as Google Sheets access, database setup, logging, and managers are in `utils/`. Runtime databases and logs belong under `data/` and should not be committed.

## Codex Operating Rules

Preserve existing behavior unless the user explicitly asks for a change. Do not push. Do not commit unless explicitly asked. Do not edit secrets, `.env`, service account JSON files, local databases, or logs. For documentation-only tasks, do not modify application code.

Do not change Discord command names, privileged intents, database schema, or Google Sheets column layout without an explicit migration plan that covers compatibility, rollout, and validation.

Agent notes and persisted memory should record reusable engineering facts only. Do not store raw secrets, full environment values, service account contents, private identifiers, or private user/agent conversation context; rewrite them as neutral constraints, decisions, and validation evidence.

## Agent Workflow Routing

Use `$discord-bot-feature-plan` before adding or changing Discord features, cogs, slash commands, context menus, permissions, Google Sheets workflows, Tortoise ORM models, schema, or user-facing localized text. Use `$safe-discord-refactor` for behavior-preserving refactors of cogs, managers, parsers, Tortoise access, Google Sheets access, or Discord UI.

Use Superpowers brainstorming for unclear or broad design work: new workflows, architecture changes, UX-heavy behavior, rollout-sensitive changes, or requests with multiple viable approaches. Keep designs brief for small changes and get user approval before editing code. Repository rules still apply when using Superpowers: do not commit unless explicitly asked, and do not create new long-lived docs paths unless the user approves the spec location.

Use `$planning-with-files` for multi-step investigations, architecture plans, or work likely to span many tool calls or context resets. Treat `.planning/` as local agent working memory, not project documentation.

The repo intentionally tracks `.codex/config.toml` for project-local Codex defaults. Keep personal tokens, machine-specific paths, and private context out of that file.

## Project Conventions

Use `FeatureChannelBase` for channel-scoped feature enable/disable/clear behavior. Soft disable should keep settings and set `is_enabled=False`; hard clear should delete the feature settings for that channel. Use `feature_enabled_prefix_command_predicate` and `feature_enabled_app_command_predicate` for guarded commands, and keep custom exceptions such as `FeatureNotEnabled` flowing through centralized cog error handlers.

Route Google Sheets work through `GoogleSheet` and the relevant manager APIs. Store worksheet IDs in the database, not worksheet objects or titles as durable identifiers. Settings changes should go through Discord Modal/View flows, and user-facing embeds should show worksheet titles together with worksheet IDs. All settings/edit commands must require both `administrator` and `manage_channels` permissions. Do not broaden settings UI visibility, persistence, or callback reach without a permission review.

## Build, Test, and Development Commands

- `uv sync`: install runtime and developer dependencies from `pyproject.toml` and `uv.lock`.
- `uv lock --check`: verify that `uv.lock` is consistent with `pyproject.toml`.
- `uv run python main.py`: run the bot locally after setting required environment variables.
- `pre-commit install`: enable Black and Ruff hooks for commits.
- `pre-commit run --all-files`: run all configured hooks; this may modify files because Black formats code and Ruff is configured with `--fix`.
- `uv run ruff check --no-fix .`: lint with Ruff without modifying files.
- `uv run ruff format --check .`: check Ruff formatting.
- `uv run black --check --workers 1 main.py bot cogs components models utils`: check Black formatting.
- `uv run pytest`: run the automated test suite.

CI is configured in `.github/workflows/ci.yml` and runs `uv lock --check`, `uv sync --locked`, Ruff lint with `--no-fix`, Ruff format check, Black format check, pytest with coverage over `bot`, `cogs`, `components`, `models`, and `utils`, and `compileall`. Deployment is configured by `.github/workflows/deploy.yml`, `.profile`, and `Procfile` for Heroku (`worker: python main.py`).

In managed Codex sandboxes, use the repo-local cache-prefixed validation commands documented in `docs/project_setup.md` instead of the bare local command forms above.

## Coding Style & Naming Conventions

Use Black formatting with 88-character lines and Ruff settings from `pyproject.toml`. Ruff enables all lint rule families except `D`, `COM812`, and `UP046`; keep imports sorted by Ruff/isort. Use Google-style docstrings for new public modules, classes, and functions where they clarify behavior. Name cogs and feature modules in snake_case, for example `team_register.py`, and keep matching managers and structs in `utils/*_manager.py` and `utils/*_structs.py`. Prefer async APIs for Discord, database, and Google Sheets work.

## Testing Guidelines

Tests live under `tests/` and use pytest with `test_*.py` naming. Prefer focused tests for pure parsing, manager, database, and message-template behavior before touching Discord API code. Use `uv run pytest tests/path_or_file.py` for focused local debugging, and run the full CI-style coverage command before review when behavior changes affect shared paths:

```shell
uv run pytest --cov=bot --cov=cogs --cov=components --cov=models --cov=utils --cov-report=term-missing --cov-fail-under=35
```

Manually exercise affected Discord commands, modals, views, permissions, and Google Sheets flows in a development guild. Use `docs/manual_integration_validation.md` for Discord and Google Sheets integration checks.

## Commit & Pull Request Guidelines

Recent history uses Conventional Commit-style messages such as `feat: ...`, `fix: ...`, `docs(scope): ...`, `chore(scope): ...`, and `i18n: ...`. Keep commits focused and use a scope when helpful, but only commit when the user explicitly asks. Pull requests should include a behavior summary, manual validation steps, linked issues when applicable, screenshots for changed embeds/views, and notes for environment, database, or Google Sheets changes.

## Security & Configuration Tips

Configuration is loaded from environment variables or `.env`. `DISCORD_TOKEN` is required at startup. `DATABASE_URL` defaults to `sqlite://data/db.sqlite3`, and `GOOGLE_SERVICE_ACCOUNT_PATH` defaults to `bot/service_account.json`; set both explicitly for deployment and integration validation. Never commit tokens, service account JSON files, local databases, or logs.
