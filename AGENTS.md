# Repository Guidelines

## Project Structure & Module Organization

Rhoboto is a Python 3.13 Discord bot built on `discord.py`. `main.py` initializes logging, validates runtime config, discovers public cog modules under `cogs/`, and starts `Rhoboto`. Core bot setup, configuration, startup/shutdown, and command translation live in `bot/`. Feature cogs live in `cogs/`, with shared channel-scoped feature lifecycle behavior in `cogs/base/feature_channel_base.py`. Discord UI views, modals, buttons, selects, and shared settings-panel helpers live in `components/`. Tortoise ORM models live in `models/`, with shared model helpers in `models/base/`.

Reusable services and domain logic live in `utils/`, including Google Sheets access, managers, parsers, message-template rendering, reactions, logging, locks, and database setup. Localized public announcement/help templates live under `resources/messages/`. Tests live in `tests/` and use pytest plus repo fakes. Durable project documentation lives in `docs/`; `docs/project_setup.md` owns setup, validation, deployment, and agent harness details. Secrets belong under `secrets/`, while runtime databases, logs, and generated runtime artifacts belong under `data/`. Agent support files live in `.agents/` and `.codex/`.

## Safety, Privacy, and Change Boundaries

### Change Scope and Behavior Preservation

- Make the narrowest change that satisfies the user's request. Do not include unrelated refactors, cleanup, behavior changes, dependency changes, formatting sweeps, or file moves.
- Preserve public interfaces, command names, feature names, data formats, database schema, stored identifiers, operational workflows, Discord permissions, and Google Sheets layouts unless the user explicitly requests that change and the change has an approved compatibility, rollout, and validation plan.
- Documentation-only requests are documentation-only. Do not modify application code, tests, dependencies, configuration, generated files, or runtime artifacts unless the user separately approves that broader scope.

### Git Operations

- Do not stage, commit, or push unless the user explicitly asks for that exact git operation. A request to implement, edit, fix, or review is not a request to stage, commit, or push.

### Secrets and Private Data

- Do not read, print, copy, edit, stage, or commit secrets, `.env`, service account JSON files, tokens, or credential material. When needed, verify only existence, paths, ignore status, staging status, or configuration key names without exposing values.
- Do not edit, stage, or commit local databases, logs, spreadsheet exports, or private identifiers. For diagnostics, inspect only the minimum necessary metadata, schema, counts, or redacted excerpts; do not dump contents or expose private data unless the user explicitly requests it and the output is sanitized.

### Migration Boundaries

- Do not change Discord command names, privileged intents, database schema, or Google Sheets worksheet layout or columns without an explicit migration plan covering compatibility, rollout, and validation.

## Agent Memory and Durable Decisions

Agent working memory files, including `.planning/`, `.superpowers/`, `docs/superpowers/`, subagent ledgers, reports, and temporary plans, are local working memory rather than project documentation. Do not commit them directly.

The repo intentionally tracks `.codex/config.toml` for project-local Codex defaults. Keep personal tokens, machine-specific paths, and private context out of that file.

When an agent artifact contains durable decisions, rewrite only the reusable, neutral content into the tracked documentation surface that matches its audience and scope:

- `AGENTS.md` for agent operating rules and repository-wide coding guidance.
- `docs/project_setup.md` for setup, validation, deployment, and agent harness contracts.
- Feature design docs or implementation plans for feature behavior, compatibility, rollout, and migration decisions.
- `docs/manual_integration_validation.md` for reusable Discord, Google Sheets, and deployment validation checks.

Agent notes and persisted memory should record reusable engineering facts only. Do not store raw secrets, full environment values, service account contents, private identifiers, or private user/agent conversation context; rewrite them as neutral constraints, decisions, and validation evidence.

Tracked documentation and agent-authored guidance should use repository-relative paths and neutral environment descriptions. Do not record local absolute paths, usernames, machine-specific directories, private identifiers, or private conversation context.

## Agent Workflow Routing

Use `$discord-bot-feature-plan` before adding or changing Discord features, cogs, slash commands, context menus, permissions, settings UI, Google Sheets workflows, Tortoise ORM models or schema, feature behavior, or user-facing localized text.

Use `$safe-discord-refactor` for behavior-preserving refactors of cogs, managers, parsers, Tortoise access, Google Sheets access, or Discord UI.

Use Superpowers brainstorming for unclear or broad design work: new workflows, architecture changes, UX-heavy behavior, rollout-sensitive changes, or requests with multiple viable approaches. Repository rules still apply when using Superpowers: do not commit unless explicitly asked, and do not create new long-lived docs paths unless the user approves the spec location.

Use `$grill-me` to stress-test an existing proposal, plan, or design after the main direction is clear. It is a review aid, not a replacement for Superpowers brainstorming, feature planning, or refactor planning.

Use `$planning-with-files` for multi-step investigations, architecture plans, or work likely to span many tool calls or context resets. It is persistent working memory, not a replacement for Superpowers design or implementation workflows.

Subagent-Driven Development is an optional execution strategy, not a default requirement for every plan. Use it only when the user, an approved plan, or the task shape calls for independent task-level agent work. In this repository, SDD is report/diff-based rather than commit-based: implementer subagents edit only assigned files in the canonical checkout or provide patches, and reviewers work from task briefs, implementation reports, and scoped git diffs. Repository rules override generic SDD prompts: subagents must not stage, commit, or push unless the user explicitly asks for that exact git operation. Treat the current repository checkout as canonical by default. Temporary worktrees are optional isolation tools, not the default SDD mode; use one only with explicit user approval or an approved plan, then sync completed changes back to the canonical checkout at each checkpoint and verify the canonical `git status --short`.

## Project Conventions

Use `FeatureChannelBase` for channel-scoped feature enable, disable, hard clear, guarded commands, help flows, and message-listener gating. Soft disable should keep settings and set `is_enabled=False`; hard clear should delete the feature settings for that channel. Use `feature_enabled_prefix_command_predicate` and `feature_enabled_app_command_predicate` for guarded commands, and keep feature lifecycle exceptions such as `FeatureNotEnabled` flowing through centralized cog error handlers.

Route Google Sheets work through `GoogleSheet` and the relevant manager APIs; cogs and UI callbacks should not call third-party worksheet APIs directly. Store worksheet IDs in the database, not worksheet objects or worksheet titles as durable identifiers. Worksheet titles are setup input and display metadata; user-facing settings embeds should show worksheet titles together with worksheet IDs when available.

Settings changes should go through Discord Modal/View flows. All settings/edit commands and settings-changing callbacks must require both `administrator` and `manage_channels` permissions. Re-check permissions inside callbacks because permissions may change while a view is open. Do not broaden settings UI visibility, persistence, or callback reach without a permission review.

User-facing public announcement and help content should use `resources/messages/` templates where that template system applies. Slash command names and descriptions are localized through `bot/translator.py` and Discord locale handling; guild-level announcement language settings must not change individual users' command localization.

When an approved migration plan changes command names, feature names, privileged intents, Tortoise schema, stored worksheet ID semantics, Google Sheets worksheet layout, or worksheet columns, update the corresponding tests, documentation, and manual validation checklist in the same change set.

## Build, Test, and Development Commands

Read `docs/project_setup.md` before changing project setup, dependencies, validation commands, CI, deployment, `.codex/`, `.agents/`, or agent harness behavior. That document owns the detailed setup, validation, deployment, and sandbox command contract.

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

## Configuration Tips

Configuration is loaded from environment variables or `.env`. `DISCORD_TOKEN` is required at startup. `DATABASE_URL` defaults to `sqlite://data/db.sqlite3`, and `GOOGLE_SERVICE_ACCOUNT_PATH` defaults to `secrets/service_account.json`; set both explicitly for deployment and integration validation. Follow the Safety, Privacy, and Change Boundaries section when handling credentials, local runtime files, logs, and spreadsheet exports.
