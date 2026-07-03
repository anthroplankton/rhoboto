# Project Setup and Harness Contract

This document owns Rhoboto's detailed setup, validation, deployment, and agent
harness contract. Read it before changing dependencies, validation commands, CI,
deployment, `.codex/`, `.agents/`, or any agent harness behavior.

Keep durable guidance split by audience and scope:

- `AGENTS.md` owns repository-wide agent rules, safety boundaries, and workflow
  routing.
- `docs/project_setup.md` owns concrete setup, validation, deployment, and
  sandbox command contracts.
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

## Agent Harness and Guidance Ownership

- `AGENTS.md` is the canonical Codex-facing repository guidance surface.
- `.github/copilot-instructions.md` is a compatibility pointer back to
  `AGENTS.md`; do not duplicate command lists there.
- `.codex/config.toml` is intentionally tracked for repo-local Codex defaults.
  Keep personal tokens, machine-specific paths, and private conversation
  context out of that file.
- `.agents/skills/` owns reusable workflow procedures. Skills should reference
  this document for concrete setup and validation commands instead of
  duplicating command lists that can drift.
- `.planning/`, `.superpowers/`, `docs/superpowers/`, subagent ledgers, reports,
  and temporary plans are local agent working memory rather than project
  documentation. Do not stage or commit them directly. Verify ignore and staging
  status before handoff, especially when introducing a new local-memory path.
- Agent memory should capture reusable engineering facts, neutral decisions,
  and validation evidence, not secrets, raw environment values, private
  identifiers, local absolute paths, usernames, or private user/agent
  conversation context.

When a local agent artifact contains durable decisions, promote only the
reusable, neutral content into the tracked documentation surface that matches
its audience and scope. Do not copy raw plans, private context, or
machine-local paths into tracked docs.

## Managed Codex Sandbox Commands

In managed Codex sandboxes, keep the CI commands above as the project contract
but run uv and Black through repo-local caches to avoid host cache permission or
lock failures. Select the commands that match the change scope:

```shell
env UV_CACHE_DIR=.cache/uv uv lock --check
env UV_CACHE_DIR=.cache/uv uv sync --locked
env UV_CACHE_DIR=.cache/uv uv run ruff check --no-fix .
env UV_CACHE_DIR=.cache/uv uv run ruff format --check .
timeout 60s env UV_CACHE_DIR=.cache/uv BLACK_CACHE_DIR=.cache/black uv run bash -lc '
files=$(rg --files main.py bot cogs components models utils -g "*.py") || exit $?
if [ -z "$files" ]; then
  printf "black check failed: no Python files matched\n" >&2
  exit 1
fi
count=0
while IFS= read -r f; do
  black --check --quiet --workers 1 "$f" || exit $?
  count=$((count + 1))
done <<< "$files"
printf "checked %s files\n" "$count"
'
env UV_CACHE_DIR=.cache/uv uv run pytest --cov=bot --cov=cogs --cov=components --cov=models --cov=utils --cov-report=term-missing --cov-fail-under=35
env UV_CACHE_DIR=.cache/uv uv run python -m compileall -q main.py bot cogs components models utils
```

For Black, use the process exit code rather than the last output line: exit `0`
is clean, `1` means files would reformat, `123` is an internal error, and
timeout exit `124` is inconclusive even if the output includes `All done`. In
this sandbox, Black 25.1.0 can print a clean multi-file summary and then fail to
terminate before the timeout. The sandbox command therefore runs Black one file
at a time inside a single `uv run` shell, which preserves the project
environment while avoiding the multi-file timeout path.

If a validation command fails because of sandbox cache permissions, report that
as an environment issue separate from repository failures. Do not replace the
canonical command with direct `.venv` execution except as a clearly labeled
diagnostic fallback.

## Common Pitfalls

- Do not update CI, deployment, dependency, or validation behavior without
  syncing this document and the shorter command summary in `AGENTS.md`.
- Do not duplicate detailed validation command variants in `AGENTS.md`,
  `.github/copilot-instructions.md`, or skills. This file owns the concrete
  setup and sandbox command contract.
- Do not treat `pre-commit run --all-files` as a read-only check. It may rewrite
  Python files.
- Do not treat Black stdout as proof of success in the managed sandbox. Trust
  the exit code, and treat timeout `124` as inconclusive.
- Do not assume a local agent memory path is ignored just because it is intended
  to be local. Check `.gitignore` and `git status --short` before handoff.
- Do not promote `.planning/`, `.superpowers/`, `docs/superpowers/`, subagent
  ledgers, or reports directly into tracked docs. Rewrite durable content for
  the correct audience and remove private context first.
- Do not record local absolute paths, usernames, machine-specific directories,
  private identifiers, credential values, service account contents, or private
  conversation context in tracked documentation.
- Do not propose credential-path migrations casually. Any change to the service
  account path must cover `bot/config.py`, `.env.example`, `.profile`,
  `.gitignore`, deployment configuration, docs, and validation.
