# Project Setup and Harness Contract

This document records the project-level setup contract for Rhoboto. Runtime
architecture risks live in `docs/runtime_architecture_review.md`; this file is
for tooling, configuration, deployment, and agent workflow surfaces.

## Toolchain

- Python is pinned by `.python-version`; `pyproject.toml` requires Python
  `>=3.13,<3.14`.
- Use `uv sync` for local runtime and developer dependencies from
  `pyproject.toml` and `uv.lock`.
- The project is not packaged as an installable wheel; `[tool.uv]` sets
  `package = false`.
- Formatting and linting use both Black and Ruff. Black is the compatibility
  formatter of record for pre-commit, while Ruff remains the linter, import
  sorter, and format checker configured in `pyproject.toml`.
- The documented Black check uses `--workers 1` for deterministic local and CI
  behavior.

## Validation Contract

Run these checks before review when touching project setup, dependencies,
startup configuration, or shared runtime paths:

```shell
uv lock --check
uv run ruff check --no-fix .
uv run ruff format --check .
uv run black --check --workers 1 main.py bot cogs components models utils
uv run pytest --cov=bot --cov=cogs --cov=components --cov=models --cov=utils --cov-report=term-missing --cov-fail-under=35
uv run python -m compileall -q main.py bot cogs components models utils
```

Use `git diff --check` before handing off changes. `pre-commit run --all-files`
is useful before committing, but it may modify files because Black formats code
and Ruff is configured with `--fix`.

CI in `.github/workflows/ci.yml` mirrors this contract with locked dependency
installation, Ruff lint, Ruff format, Black format, pytest coverage, and
`compileall`.

## Local Configuration

Copy `.env.example` into a private `.env` or set equivalent shell variables.
Required and common settings:

- `DISCORD_TOKEN`: required for bot startup.
- `DATABASE_URL`: defaults to `sqlite://data/db.sqlite3`.
- `GOOGLE_SERVICE_ACCOUNT_PATH`: defaults to `secrets/service_account.json`.
- `LOG_TO_FILE`, `LOG_DIR`, `LOG_FILENAME`, and `LOG_LEVEL`: configure logging.

Never commit `.env`, service account JSON files, local databases, logs,
spreadsheet exports, or private identifiers.

## Heroku Deployment

Deployment is Heroku-oriented:

- `Procfile` runs the worker dyno with `python main.py`.
- `.github/workflows/deploy.yml` deploys `main` to Heroku through GitHub
  Actions using `HEROKU_API_KEY`, `HEROKU_APP_NAME`, and `HEROKU_EMAIL`
  repository secrets.
- `.profile` materializes the path in `GOOGLE_SERVICE_ACCOUNT_PATH`, defaulting
  to `secrets/service_account.json`, only when the Heroku config var
  `GOOGLE_CREDENTIALS` is non-empty.

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

## Agent Harness

- `AGENTS.md` is the canonical Codex-facing repository guidance surface.
- `.github/copilot-instructions.md` is a compatibility pointer back to
  `AGENTS.md`; do not duplicate command lists there.
- `.codex/config.toml` is intentionally tracked for repo-local Codex defaults.
  Keep personal tokens, private paths, and private conversation context out of
  that file.
- In managed Codex sandboxes, keep the CI commands above as the project
  contract but run uv and Black through repo-local caches to avoid host cache
  permission or lock failures:

  ```shell
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

  For Black, use the process exit code rather than the last output line: exit
  `0` is clean, `1` means files would reformat, `123` is an internal error,
  and timeout exit `124` is inconclusive even if the output includes `All
  done`. In this sandbox, Black 25.1.0 can print a clean multi-file summary and
  then fail to terminate before the timeout. The sandbox command therefore runs
  Black one file at a time inside a single `uv run` shell, which preserves the
  project environment while avoiding the multi-file timeout path.
- `.planning/` and `docs/superpowers/` are ignored local agent working memory.
  Records there should capture reusable engineering facts, neutral decisions,
  and validation evidence, not secrets, raw environment values, private
  identifiers, or private user/agent conversation context. Do not commit
  Superpowers artifacts directly. When a Superpowers spec or plan contains
  durable decisions, summarize and promote them into the appropriate tracked
  documentation for their audience and scope: `AGENTS.md` for agent operating
  rules, `docs/project_setup.md` for setup or harness contracts, relevant
  feature design docs or implementation plans for feature behavior and rollout
  decisions, and validation runbooks such as
  `docs/manual_integration_validation.md` for reusable manual checks.
