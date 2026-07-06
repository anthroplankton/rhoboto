# Agent Harness and Sandbox Contract

This document owns Rhoboto's Codex and agent harness contract, managed Codex
sandbox command variants, local agent working-memory rules, and detailed
Superpowers/SDD execution guidance. Read it before changing `.codex/`,
`.agents/`, managed sandbox validation commands, or agent harness behavior.

For normal project setup, general validation commands, local configuration, and
deployment, read `docs/project_setup.md`.

## Guidance Ownership

- `AGENTS.md` is the canonical Codex-facing repository guidance surface.
- `.github/copilot-instructions.md` is a compatibility pointer back to
  `AGENTS.md`; do not duplicate command lists there.
- `.codex/config.toml` is intentionally tracked for repo-local Codex defaults.
  Keep personal tokens, machine-specific paths, and private conversation
  context out of that file.
- `.agents/skills/` owns reusable workflow procedures. Skills should reference
  `AGENTS.md` for hard repository rules, this document for agent harness and
  managed sandbox behavior, and `docs/project_setup.md` for general setup and
  validation commands.
- `.planning/`, `.superpowers/`, `docs/superpowers/`, project-local agent
  worktrees, subagent ledgers, reports, and temporary plans are local agent
  working memory rather than project documentation. Do not stage or commit them
  directly. Verify ignore and staging status before handoff, especially when
  introducing a new local-memory path.
- When using `$planning-with-files` in Rhoboto, prefer a named
  `.planning/YYYY-MM-DD-<slug>/` planning session instead of legacy root planning
  files. Only use root planning files with explicit user approval, and verify
  ignore and staging status before handoff.
- Agent memory should capture reusable engineering facts, neutral decisions,
  and validation evidence, not secrets, raw environment values, private
  identifiers, local absolute paths, usernames, or private user/agent
  conversation context.

When a local agent artifact contains durable decisions, promote only the
reusable, neutral content into the tracked documentation surface that matches
its audience and scope. Do not copy raw plans, private context, or
machine-local paths into tracked docs.

## Superpowers And SDD Execution

Before substantial Superpowers execution, if the user has not already selected
an execution mode, classify the task and ask the user to choose one Rhoboto
execution mode:

1. Inline execution mode: execute the written plan in this session using
   Superpowers executing-plans. Track tasks with the available task-tracking
   tool, run the plan step by step, and pause at natural checkpoints with scoped
   diffs, validation results, concerns, and next-step summaries. In the
   canonical checkout, checkpoints are review handoffs, not git commits; do not
   stage, commit, or push unless separately approved.
2. Canonical SDD mode: use Subagent-Driven Development in the canonical
   checkout or through patches/reports. Dispatch fresh subagents per task and
   use the Superpowers review loop, but subagents must not stage, commit, or
   push. Review checkpoints use task briefs, implementation reports, scoped
   diffs, validation results, and remaining-risk notes.
3. Isolated agent-branch SDD mode: use an isolated worktree on a disposable
   `agent/*` branch. Local checkpoint commits are allowed only on that branch as
   Superpowers-style review checkpoints, not final project history. Do not push,
   merge, rebase, cherry-pick into the target branch, delete the branch, or
   remove the worktree without separate explicit approval.

Recommend Inline execution mode for written plans that are tightly coupled,
small enough to execute in one session, or not worth subagent overhead.
Recommend Canonical SDD mode for approved plans with independent tasks when
Rhoboto's no-commit policy should remain in force. Recommend Isolated
agent-branch SDD mode for broad, risky, cross-cutting work that benefits from
isolation and commit-range review.

Execution mode selection does not replace Superpowers skill routing. Agents
must still invoke every applicable Superpowers skill and required sub-skill for
the chosen workflow. If no approved implementation plan exists, use the
appropriate planning or design skill before execution. If Inline execution mode
is chosen, use `superpowers:executing-plans`. If either SDD mode is chosen, use
`superpowers:subagent-driven-development`; reviewer checkpoints must follow
`superpowers:requesting-code-review`, and subagents should follow
`superpowers:test-driven-development`.

When a Superpowers workflow calls for `superpowers:using-git-worktrees`, first
apply Rhoboto's selected execution mode: canonical modes must not create a
worktree unless the user separately approves one; isolated agent-branch SDD mode
must use the worktree setup flow.

Do not infer isolated agent-branch SDD mode from a generic request to use
Superpowers or SDD. It requires explicit user choice or an approved plan.

Subagent-Driven Development is optional, not a default requirement for every
plan. Use it only when the user, an approved plan, or the task shape calls for
independent task-level agent work.

In Canonical SDD mode, Rhoboto remains report/diff-based rather than
commit-based: implementer subagents edit only assigned files in the canonical
checkout or provide patches, and reviewers work from task briefs,
implementation reports, scoped diffs, and validation results. Subagent task
status belongs in ignored local ledgers and reports, not per-task commits.

In Isolated agent-branch SDD mode, work must happen in an isolated worktree on a
disposable `agent/*` branch. Implementer subagents may create local checkpoint
commits on that branch only. These commits must not be pushed, merged, rebased
into the target branch, cherry-picked into the target branch, or treated as
final project history without explicit user approval. Durable handoff status
still belongs in ignored reports, and final project history remains gated by
`AGENTS.md`.

For all modes, repository rules override generic Superpowers prompts. Agents
and subagents must stay inside assigned scope, preserve unrelated behavior, and
report changed files, validation, concerns, and remaining risk after each task.

Before creating final project commits on the user's work branch, provide a
handoff package containing changed files, a concise behavior summary,
validation commands and results, unresolved findings or risks, proposed final
commit grouping, and the exact proposed integration strategy.

Stop after the handoff package. Do not create final project commits, merge,
rebase, cherry-pick into the target branch, push, open a pull request, delete an
agent branch, or remove a worktree until the user explicitly approves that next
operation.

## Managed Codex Sandbox Commands

In managed Codex sandboxes, keep the general validation commands in
`docs/project_setup.md` as the project contract, but run uv through repo-local
caches to avoid host cache permission or lock failures. Bare `uv run ...`
commands must not be used in managed Codex sandboxes because the host uv cache
may be unwritable. Select the commands that match the change scope:

```shell
env UV_CACHE_DIR=.cache/uv uv lock --check
env UV_CACHE_DIR=.cache/uv uv sync --locked
env UV_CACHE_DIR=.cache/uv uv run ruff check --no-fix .
env UV_CACHE_DIR=.cache/uv uv run ruff format --check .
env UV_CACHE_DIR=.cache/uv uv run pytest --cov=bot --cov=cogs --cov=components --cov=models --cov=utils --cov-report=term-missing --cov-fail-under=35
env UV_CACHE_DIR=.cache/uv uv run python -m compileall -q main.py bot cogs components models utils
```

When intentionally repairing Ruff lint or format failures, use the same
repo-local uv cache prefix with commands that may modify files:

```shell
env UV_CACHE_DIR=.cache/uv uv run ruff check --fix .
env UV_CACHE_DIR=.cache/uv uv run ruff format .
```

If a validation command fails because of sandbox cache permissions, report that
as an environment issue separate from repository failures. Do not replace the
canonical command with direct `.venv` execution except as a clearly labeled
diagnostic fallback.

## Common Pitfalls

- Do not duplicate managed sandbox command variants in `AGENTS.md`,
  `.github/copilot-instructions.md`, or skills. This file owns the concrete
  sandbox command contract.
- Do not use bare `uv run ...` commands in managed Codex sandboxes; use the
  repo-local cache-prefixed commands above.
- Do not assume a local agent memory path is ignored just because it is intended
  to be local. Check `.gitignore` and `git status --short` before handoff.
- Do not promote `.planning/`, `.superpowers/`, `docs/superpowers/`, subagent
  ledgers, or reports directly into tracked docs. Rewrite durable content for
  the correct audience and remove private context first.
- Do not record local absolute paths, usernames, machine-specific directories,
  private identifiers, credential values, service account contents, or private
  conversation context in tracked documentation.
