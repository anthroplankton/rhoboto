# Repository Guidelines

## Project Structure & Module Organization

Rhoboto is a Python 3.13 Discord bot built on `discord.py`. `main.py` initializes logging, validates runtime config, discovers public cog modules under `cogs/`, and starts `Rhoboto`. Core bot setup, configuration, startup/shutdown, and command translation live in `bot/`. Feature cogs live in `cogs/`, with shared channel-scoped feature lifecycle behavior in `cogs/base/feature_channel_base.py`. Discord UI views, modals, buttons, selects, and shared settings-panel helpers live in `components/`. Tortoise ORM models live in `models/`, with shared model helpers in `models/base/`.

Reusable services and domain logic live in `utils/`, including Google Sheets access, managers, parsers, message-template rendering, reactions, logging, locks, and database setup. Localized public announcement/help templates live under `resources/messages/`. Tests live in `tests/` and use pytest plus repo fakes. Durable project documentation lives in `docs/`; `docs/project_setup.md` owns general setup, validation, local configuration, and deployment, while `docs/agent_harness.md` owns Codex, agent harness, and managed sandbox details. Secrets belong under `secrets/`, while runtime databases, logs, and generated runtime artifacts belong under `data/`. Agent support files live in `.agents/` and `.codex/`.

## Safety, Privacy, and Change Boundaries

### Change Scope and Behavior Preservation

- Make the narrowest change that satisfies the user's request. Do not include unrelated refactors, cleanup, behavior changes, dependency changes, formatting sweeps, or file moves.
- Preserve public interfaces, command names, feature names, data formats, database schema, stored identifiers, operational workflows, Discord permissions, and Google Sheets layouts unless the user explicitly requests that change and the change has an approved compatibility, rollout, and validation plan.
- Documentation-only requests are documentation-only. Do not modify application code, tests, dependencies, configuration, generated files, or runtime artifacts unless the user separately approves that broader scope.

### Git Operations

- Do not stage, commit, push, merge, rebase, cherry-pick into a target branch, delete branches, or remove worktrees unless the user explicitly asks for that exact git operation or an approved plan grants that exact operation.
- A request to implement, edit, fix, review, use Superpowers, or use Subagent-Driven Development is not by itself permission to modify final project history.
- Local checkpoint commits are allowed only when the user or an approved plan explicitly selects isolated agent-branch SDD mode, and only on disposable `agent/*` branches in an isolated worktree.
- Never push, open a pull request, merge, rebase, cherry-pick into the target branch, force-push, delete branches, or remove worktrees without separate explicit approval.

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
- `docs/project_setup.md` for general setup, validation, local configuration, and deployment contracts.
- `docs/agent_harness.md` for Codex, agent harness, managed sandbox commands, and detailed Superpowers/SDD execution contracts.
- Feature design docs or implementation plans for feature behavior, compatibility, rollout, and migration decisions.
- `docs/manual_integration_validation.md` for reusable Discord, Google Sheets, and deployment validation checks.

Agent notes and persisted memory should record reusable engineering facts only. Do not store raw secrets, full environment values, service account contents, private identifiers, or private user/agent conversation context; rewrite them as neutral constraints, decisions, and validation evidence.

Tracked documentation and agent-authored guidance should use repository-relative paths and neutral environment descriptions. Do not record local absolute paths, usernames, machine-specific directories, private identifiers, or private conversation context.

## Agent Workflow Routing

Use `$discord-bot-feature-plan` before adding or changing Discord features, cogs, slash commands, context menus, permissions, settings UI, Google Sheets workflows, Tortoise ORM models or schema, feature behavior, or user-facing localized text.

Use `$safe-discord-refactor` for behavior-preserving refactors of cogs, managers, parsers, Tortoise access, Google Sheets access, or Discord UI.

Use Superpowers brainstorming for unclear or broad design work: new workflows, architecture changes, UX-heavy behavior, rollout-sensitive changes, or requests with multiple viable approaches. Repository rules still apply when using Superpowers: follow the Rhoboto execution mode selection rules in `docs/agent_harness.md`, and do not create new long-lived docs paths unless the user approves the spec location.

Use `$grill-me` to stress-test an existing proposal, plan, or design after the main direction is clear. It is a review aid, not a replacement for Superpowers brainstorming, feature planning, or refactor planning.

Use `$planning-with-files` for multi-step investigations, architecture plans, or work likely to span many tool calls or context resets. In Rhoboto, keep planning files in ignored local working memory, preferably under `.planning/`. It is persistent working memory, not a replacement for Superpowers design or implementation workflows.

Before substantial Superpowers or SDD execution, follow `docs/agent_harness.md` for Rhoboto execution modes, worktree rules, review checkpoints, and final handoff gates. Keep this file as the short routing surface; do not duplicate the detailed runbook here.

## Project Conventions

Use `FeatureChannelBase` for channel-scoped feature enable, disable, hard clear, guarded commands, help flows, and message-listener gating. Soft disable should keep settings and set `is_enabled=False`; hard clear should delete the feature settings for that channel. Use `feature_enabled_prefix_command_predicate` and `feature_enabled_app_command_predicate` for guarded commands, and keep feature lifecycle exceptions such as `FeatureNotEnabled` flowing through centralized cog error handlers. Use `feature` for stable feature identifiers and registry/listing surfaces, such as `feature_name` and the `/features` command. Use `FeatureChannel` terminology for channel-scoped feature state and operation context: a guild/channel plus feature identifier row, its enabled state, and feature-specific manager access.

At framework and integration boundaries, prefer small helpers that make boundary responsibilities explicit: validate runtime invariants, normalize structured inputs, or narrow types where data enters a feature flow. Return the original source object or the specific value callers need when that is sufficient, and name helpers to match what they return. Use protocols or narrow return types to describe the smallest capability shared code depends on. Add context/value containers only when they model stable domain state or remove meaningful duplication; avoid pass-through containers that mainly carry framework objects across layers.

Route Google Sheets work through `GoogleSheet` and the relevant manager APIs; cogs and UI callbacks should not call third-party worksheet APIs directly. Store worksheet IDs in the database, not worksheet objects or worksheet titles as durable identifiers. Worksheet titles are setup input and display metadata; user-facing settings embeds should show worksheet titles together with worksheet IDs when available.

Settings changes should go through Discord Modal/View flows. All settings/edit commands and settings-changing callbacks must require both `administrator` and `manage_channels` permissions. Re-check permissions inside callbacks because permissions may change while a view is open. Do not broaden settings UI visibility, persistence, or callback reach without a permission review.

User-facing public announcement and help content should use `resources/messages/` templates where that template system applies. Slash command names and descriptions are localized through `bot/translator.py` and Discord locale handling; guild-level announcement language settings must not change individual users' command localization.

Use emoji and custom reaction markers consistently by intent, not by feature, in user-visible Team/Shift Register flows. Keep localized text aligned with the same marker meaning.

| Marker | Meaning | Use when |
| --- | --- | --- |
| `‼️` | High-attention destructive guidance or confirmation. | A user-visible action or instruction may overwrite, delete, or replace existing data. |
| `config.PROCESSING_EMOJI` | Operation in progress. | The bot has accepted an action and is still processing it. |
| `✅` | Success. | The requested operation completed successfully. |
| `✖️` | Cancelled with no changes applied. | A user-visible flow ends before applying changes. |
| `⚠️` | Blocked, failed, abnormal, or needs correction. | A flow cannot continue, may not have completed, or needs user/admin action. |
| `🛠️` | External service or repair-needed failure. | A failure likely needs external-service, configuration, or maintainer repair. |
| `⤴️` | Replied-message reference. | Text points users to the message this bot message replies to. |
| `🟢` / `⚫` | Enabled or disabled status. | Showing enabled or disabled state. |
| `config.CONFUSED_EMOJI` | Invalid-input companion marker. | Pair after `⚠️` when configured user input is invalid. |

The custom markers are configured in `bot/config.py`; do not hard-code their rendered Discord emoji strings in copy or tests when the config constant applies.

For structured user-submitted text that encodes a bot-defined grammar, such as numbers, dates, times, time ranges, and register submission formats, apply Unicode NFKC normalization at the domain parser or helper boundary before applying canonical parsing rules. Keep Discord UI callbacks, managers, and database models from duplicating normalization or parsing concerns; pass the raw field value into the parser/helper that owns the grammar. Preserve raw submitted text when it is stored or rendered as user-authored content. Do not apply compatibility normalization blindly to natural-language content, user display names, identifiers, or localized public copy, because it can change meaning, identity, or stylistic distinctions across writing systems. Regexes that run after normalization should prefer canonical grammar, while intentional non-ASCII semantic tokens should remain direct visible literals when they are part of the accepted language.

When an approved migration plan changes command names, feature names, privileged intents, Tortoise schema, stored worksheet ID semantics, Google Sheets worksheet layout, or worksheet columns, update the corresponding tests, documentation, and manual validation checklist in the same change set.

## Build, Test, and Development Commands

Read `docs/project_setup.md` before changing project setup, dependencies, general validation commands, CI, deployment, or local configuration. Read `docs/agent_harness.md` before changing `.codex/`, `.agents/`, managed Codex sandbox commands, or agent harness behavior. The commands below are Local/CI command forms; in managed Codex sandboxes, use the repo-local cache-prefixed commands in `docs/agent_harness.md` instead of these bare forms.

- `uv sync`: install runtime and developer dependencies from `pyproject.toml` and `uv.lock`.
- `uv lock --check`: verify that `uv.lock` is consistent with `pyproject.toml`.
- `uv run python main.py`: run the bot locally after setting required environment variables.
- `pre-commit install`: enable Ruff hooks for commits.
- `pre-commit run --all-files`: run all configured hooks; this may modify files because Ruff lint is configured with `--fix` and Ruff format writes formatting changes.
- `uv run ruff check --no-fix .`: lint with Ruff without modifying files.
- `uv run ruff format --check .`: check Ruff formatting.
- `uv run pytest`: run the automated test suite.

CI is configured in `.github/workflows/ci.yml` and runs `uv lock --check`, `uv sync --locked`, Ruff lint with `--no-fix`, Ruff format check, pytest with coverage over `bot`, `cogs`, `components`, `models`, and `utils`, and `compileall`. Deployment is configured by `.github/workflows/deploy.yml`, `.profile`, and `Procfile` for Heroku (`worker: python main.py`).

In managed Codex sandboxes, use the repo-local cache-prefixed command variants documented in `docs/agent_harness.md`; bare `uv run ...` can use an unwritable host uv cache, `pre-commit run --all-files` may rewrite files, and Ruff repair commands that intentionally modify files should use the managed-sandbox forms in `docs/agent_harness.md`.

## Coding Style & Naming Conventions

Use Ruff formatting with 88-character lines and Ruff settings from `pyproject.toml`. Ruff enables all lint rule families except `D` and `COM812`; keep imports sorted by Ruff/isort. Use Google-style docstrings for new public modules, classes, and functions where they clarify behavior. Name cogs and feature modules in snake_case, for example `team_register.py`, and keep matching managers and structs in `utils/*_manager.py` and `utils/*_structs.py`. Prefer async APIs for Discord, database, and Google Sheets work.

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
