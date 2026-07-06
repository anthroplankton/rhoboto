# Project Setup

This document owns Rhoboto's general project setup, validation, local
configuration, and deployment contract. Read it before changing dependencies,
general validation commands, CI, deployment, or local configuration.

For Codex, agent harness, managed sandbox command variants, and detailed
Superpowers/SDD execution guidance, read `docs/agent_harness.md`.

Keep durable guidance split by audience and scope:

- `AGENTS.md` owns repository-wide agent rules, safety boundaries, and workflow
  routing.
- `docs/project_setup.md` owns concrete setup, general validation, local
  configuration, and deployment contracts.
- `docs/agent_harness.md` owns Codex, agent harness, managed sandbox command
  variants, and detailed Superpowers/SDD execution guidance.
- `docs/manual_integration_validation.md` owns reusable manual Discord and
  Google Sheets validation checks.
- `docs/runtime_architecture_review.md` owns runtime architecture risks and
  backlog-level observations.
- Feature design docs and implementation plans own feature behavior,
  compatibility, rollout, and migration decisions.

Do not use this file as a generic bucket for feature notes, raw plans, or local
agent memory. Rewrite durable decisions into the tracked document whose audience
matches the decision.

## Toolchain

- Python is pinned by `.python-version`; `pyproject.toml` requires Python
  `>=3.13,<3.14`.
- Use `uv sync` for local runtime and developer dependencies from
  `pyproject.toml` and `uv.lock`; use `uv sync --locked` when validating the
  locked dependency state.
- The project is not packaged as an installable wheel; `[tool.uv]` sets
  `package = false`.
- Formatting and linting use both Black and Ruff. Black is the compatibility
  formatter of record for pre-commit, while Ruff remains the linter, import
  sorter, and format checker configured in `pyproject.toml`.
- The documented Black check uses `--workers 1` for deterministic local and CI
  behavior.

## Validation Contract

Use the narrowest validation that proves the change. For documentation-only
changes, `git diff --check` is usually enough. For dependency, setup, startup,
CI, deployment, or shared runtime changes, run the relevant CI-style checks:

```shell
uv lock --check
uv sync --locked
uv run ruff check --no-fix .
uv run ruff format --check .
uv run black --check --workers 1 main.py bot cogs components models utils
uv run pytest --cov=bot --cov=cogs --cov=components --cov=models --cov=utils --cov-report=term-missing --cov-fail-under=35
uv run python -m compileall -q main.py bot cogs components models utils
```

Use `git diff --check` before handing off changes. `pre-commit run --all-files`
is useful before committing, but it is not a read-only validation command:
Black formats code and Ruff is configured with `--fix`.

CI in `.github/workflows/ci.yml` mirrors this contract with locked dependency
installation, Ruff lint, Ruff format, Black format, pytest coverage, and
`compileall`. Deployment verification in `.github/workflows/deploy.yml` also
checks the lockfile, installs locked dependencies, compiles Python modules,
checks runtime imports, and then syncs without default dependency groups before
deploying to Heroku.

In managed Codex sandboxes, use the repo-local cache-prefixed command variants
in `docs/agent_harness.md` instead of the bare commands above.

## Local Configuration and Private Data

Use `.env.example` as the editable sample. Keep the real `.env` private, and do
not read or print real credential values while validating setup.

Runtime configuration is loaded by `bot/config.py`:

- `DISCORD_TOKEN`: required for bot startup.
- `COMMAND_PREFIX`: defaults to `$`.
- `BOT_ENV`: defaults to `dev`; `LOG_LEVEL` defaults to `DEBUG` in `dev` and
  `INFO` otherwise.
- `DATABASE_URL`: defaults to `sqlite://data/db.sqlite3`.
- `GOOGLE_SERVICE_ACCOUNT_PATH`: defaults to `secrets/service_account.json`.
- `LOG_TO_FILE`: defaults to `False`.
- `USE_RICH_LOGGING`: defaults to `True`.
- `LOG_DIR`: defaults to `data/logs`.
- `LOG_FILENAME`: defaults to `rhoboto.log`.

Do not read, print, copy, edit, stage, or commit `.env`, service account JSON
files, tokens, or credential material. When setup diagnostics require checking
credentials, verify only existence, expected paths, ignore status, staging
status, or configuration key names without exposing values.

Do not edit, stage, or commit local databases, logs, spreadsheet exports, or
private identifiers. If diagnostics require runtime/private data, inspect only
minimum metadata, schema, counts, or sanitized excerpts, and keep production
Discord channels, production sheets, and real user data out of validation notes.

## Heroku Deployment

Deployment is Heroku-oriented:

- `Procfile` runs the worker dyno with `python main.py`.
- `.github/workflows/deploy.yml` deploys `main` to Heroku through GitHub
  Actions using `HEROKU_API_KEY`, `HEROKU_APP_NAME`, and `HEROKU_EMAIL`
  repository secrets.
- `.profile` materializes the path in `GOOGLE_SERVICE_ACCOUNT_PATH`, defaulting
  to `secrets/service_account.json`, only when the Heroku config var
  `GOOGLE_CREDENTIALS` is non-empty, then sets the file mode to `600`.

Set `DISCORD_TOKEN`, `DATABASE_URL`, and `GOOGLE_SERVICE_ACCOUNT_PATH`
explicitly for deployment. If `GOOGLE_SERVICE_ACCOUNT_PATH` uses the default
`secrets/service_account.json`, also set `GOOGLE_CREDENTIALS` to the service
account JSON content. Do not store that JSON in git.

Before production deploy, confirm the Heroku app config vars include:

- `DISCORD_TOKEN`: production Discord bot token.
- `DATABASE_URL`: durable production database URL, not local SQLite.
- `BOT_ENV=production`.
- `GOOGLE_SERVICE_ACCOUNT_PATH=secrets/service_account.json`.
- `GOOGLE_CREDENTIALS`: complete production service account JSON.

GitHub Actions deploy secrets are only used to deploy to Heroku; they do not
automatically become Heroku runtime config vars.

## Common Pitfalls

- Do not update CI, deployment, dependency, or validation behavior without
  syncing this document and the shorter command summary in `AGENTS.md`.
- Do not duplicate detailed validation command variants in `AGENTS.md`,
  `.github/copilot-instructions.md`, or skills. This file owns general setup and
  validation commands; `docs/agent_harness.md` owns managed sandbox variants.
- Do not treat `pre-commit run --all-files` as a read-only check. It may rewrite
  Python files.
- Do not propose credential-path migrations casually. Any change to the service
  account path must cover `bot/config.py`, `.env.example`, `.profile`,
  `.gitignore`, deployment configuration, docs, and validation.
