---
name: rhoboto-commit-advisor
description: Use when the user asks for Rhoboto commit advice, commit grouping, Conventional Commit-style messages, staging commands, pre-commit validation choices, or explicit commit execution in /home/dongj/projects/rhoboto.
---

# Rhoboto Commit Advisor

## Overview

Use this skill to turn the current Rhoboto git state into focused commit advice.
Default to advice only: do not stage, commit, or push unless the user explicitly
asks Codex to commit.

## Workflow

1. Confirm the mode.
   - Treat requests like "how should I commit this", "suggest a commit", or
     "commit message?" as advice mode.
   - Treat requests like "commit this", "help me commit", or "幫我 commit" as
     execution mode, but never push.
   - If execution mode is unclear, stay in advice mode.

2. Re-establish Rhoboto context before advising.
   - Work in `/home/dongj/projects/rhoboto`. If the current directory differs
     and the user did not clearly mean Rhoboto, ask a concise clarification.
   - Read `AGENTS.md` and `docs/project_setup.md` when present.
   - Inspect current state with `git status --short --branch`.
   - Inspect unstaged, staged, and untracked changes with focused `git diff`,
     `git diff --staged`, `git diff --stat`, and file reads as needed.
   - Inspect recent convention with `git log --oneline -n 12`.

3. Protect repository rules.
   - Do not push.
   - Do not commit unless the user explicitly asked for execution mode.
   - Do not stage secrets, `.env`, service account JSON files, runtime
     databases, logs, or unrelated local artifacts.
   - Preserve user changes. Do not revert unrelated work.
   - Do not change Discord command names, privileged intents, database schema,
     or Google Sheets column layout as part of commit preparation unless the
     user separately approved that migration work.

4. Group changes into focused commits.
   - Prefer multiple focused commits when changes are separable by purpose,
     risk, or review surface.
   - For each suggested commit, list the exact files to stage.
   - If all changes share one coherent purpose, recommend one commit.
   - Call out files that appear unrelated, generated, secret-like, or unsafe to
     include.

5. Choose validation from the change type.
   - Run `git diff --check` before final commit advice unless it is impossible,
     irrelevant, or explicitly skipped; state the reason when skipped.
   - Docs-only changes: usually run `git diff --check`; add Markdown-specific
     checks only when the repo already has them.
   - For Codex sandbox sessions, follow the current validation command guidance
     in `docs/project_setup.md`; do not duplicate or invent Black, Ruff, or
     pytest command details in this skill when that document is available.
   - Python formatting or lint-sensitive changes: choose the relevant checks
     from `docs/project_setup.md` when scope and time justify it.
   - For Black, trust the command exit status over output text: exit 0 means
     clean, exit 1 means files would reformat, exit 123 means internal error,
     and exit 124 from `timeout` is inconclusive even if the output includes
     "All done".
   - Do not start overlapping or repeated Black processes. If sandbox Black
     validation times out, follow `docs/project_setup.md` and report the result
     as environment-inconclusive rather than inventing a fallback.
   - Do not use unscoped `black .`; use the current command documented in
     `docs/project_setup.md` or an explicit changed-file list.
   - Do not replace the canonical Black check with `.venv/bin/black`; direct
     `.venv` commands are diagnostic fallbacks only when `uv run` has an
     environment/cache failure.
   - Behavior changes: prefer focused tests for the affected path first, then
     recommend the current full test command from `docs/project_setup.md` when
     shared paths are affected.
   - Dependency or lockfile changes: include the current lockfile check from
     `docs/project_setup.md`.
   - If a command fails because of the sandbox or cache permissions, report
     that separately from project failures and suggest the closest useful next
     command.

6. Handle validation failures rigorously.
   - If checks fail because of the current changes, do not recommend committing
     yet; summarize the failure and the next fix or verification step.
   - If checks expose pre-existing repo-wide debt, clearly separate it from
     current-change issues. A scoped commit may still be recommended when the
     proposed commit contents are clean and the residual risk is explicit.
   - If validation was not run, say so and explain why.

7. Write commit messages in Rhoboto's existing style.
   - Use English Conventional Commit-style messages, matching recent history:
     `feat: ...`, `fix(scope): ...`, `docs(scope): ...`, `chore(scope): ...`,
     `ci: ...`, or `i18n: ...`.
   - Use a scope when it clarifies the affected area, such as `discord`, `db`,
     `models`, `agents`, `deps`, or a feature name.
   - Keep the subject concise, imperative, and specific.
   - Do not invent issue numbers or external references.

8. Present high-signal output.
   - Follow the user's language for explanations.
   - Put the recommended commit plan first.
   - Include exact commands, using `git add -- <files>` rather than `git add .`
     unless every changed file is intentionally included.
   - Include validation already run and remaining checks.
   - Keep advice concise unless the diff is complex.

## Output Shape

Use this structure when advising:

```text
<Recommended commit plan in the user's language>:

1. <type(scope): subject>
   Files: <paths>
   Why: <brief reason>
   Commands:
   git add -- <paths>
   git commit -m "<type(scope): subject>"

Validation:
- Ran: <commands and result>
- Not run: <commands and reason>

Notes:
- <unsafe/unrelated/pre-existing-debt notes, if any>
```

In execution mode, restate the selected commit plan before staging. Stage only
the listed files, run `git status --short` after staging, create the commit, and
report the resulting commit hash. Never push.
