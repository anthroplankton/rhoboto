---
name: rhoboto-commit-advisor
description: Use when the user asks for Rhoboto commit advice, commit grouping, Conventional Commit-style messages, staging commands, pre-commit validation choices, or explicit local commit execution in the Rhoboto repository. Do not use for pushing, pull request creation, branch integration, squash/rebase/cherry-pick workflows, or agent-branch finalization.
---

# Rhoboto Commit Advisor

## Overview

Use this skill to turn the current Rhoboto git state into focused commit advice.
Default to advice only: do not stage, commit, or push unless the user explicitly
confirms local commit execution after reviewing the staging plan.

## Non-Goals

This skill does not push, open pull requests, merge, rebase, squash branch
history, cherry-pick into a target branch, delete branches or worktrees, or
perform final `agent/*` branch finalization. Route final `agent/*` branch
finalization to `$rhoboto-agent-branch-finalizer` or `docs/agent_harness.md`.

## Workflow

1. Classify the request.
   - Treat requests like "how should I commit this", "suggest a commit",
     "commit message?", or "建議 commit" as commit advice requests.
   - Treat requests for validation, pre-commit checks, or "what should I run
     before committing?" as validation requests.
   - Treat requests like "commit this", "help me commit", "請 commit", or
     "幫我 commit" as execution intent, not execution permission: do not stage
     or commit until the proposed staging plan is shown and the user explicitly
     confirms commit execution.
   - If execution intent is present and relevant validation has not been run,
     recommend the narrowest relevant pre-commit validation before committing.
     Do not create the commit until validation has passed or the user explicitly
     confirms that validation should be skipped.
   - If validation is not visible in the current conversation, command output,
     repository state, or user-provided evidence, treat it as not run.
   - If the request is unclear, provide commit advice only.

2. Re-establish Rhoboto context before advising.
   - Work from the Rhoboto repository root. If the current directory is not a
     Rhoboto checkout and the user did not clearly mean Rhoboto, ask a concise
     clarification.
   - Read `AGENTS.md` first for repository rules and git boundaries.
   - Inspect current state with `git status --short --branch`.
   - Inspect unstaged, staged, and untracked changes with focused `git diff`,
     `git diff --staged`, `git diff --stat`, and file reads as needed.
   - If files are already staged, treat the index as user-owned state. Report
     staged and unstaged changes separately, and do not unstage, restage, or
     overwrite staged content without explicit approval.
   - For untracked files, inspect filenames and ignore status before reading
     contents. Do not open secret-like, database, log, export, credential, or
     private-data files just to classify them.
   - Inspect recent convention with `git log --oneline -n 12`.
   - Use `docs/project_setup.md` when validation advice or validation
     execution needs the project validation contract.
   - Use `docs/agent_harness.md` when validation or the diff involves managed
     Codex sandbox behavior, `.codex/`, `.agents/`, Superpowers/SDD, agent
     worktrees, or handoff guidance.

3. Protect repository rules.
   - Do not push.
   - Do not commit unless the user explicitly confirmed commit execution.
   - Do not stage secrets, `.env`, service account JSON files, runtime
     databases, logs, or unrelated local artifacts.
   - Preserve user changes. Do not revert unrelated work.
   - Flag migration-sensitive changes and require approval before including
     them in a commit plan. This includes Discord command names, privileged
     intents, database schema, Google Sheets worksheet layouts or columns, and
     similar compatibility-sensitive surfaces.

4. Group changes into focused commits.
   - Prefer multiple focused commits when changes are separable by purpose,
     risk, or review surface.
   - For each suggested commit, list the exact files to stage.
   - If all changes share one coherent purpose, recommend one commit.
   - Call out files that appear unrelated, generated, secret-like, or unsafe to
     include.

5. Choose validation only when needed.
   - For commit advice, report validation already run and whether validation is
     still recommended, but do not treat validation as implied work.
   - Before explicit commit execution, require relevant validation to have
     passed unless the user explicitly confirms that validation should be
     skipped.
   - For docs-only changes, `git diff --check` is usually the narrowest useful
     pre-commit validation.
   - Use `docs/project_setup.md` for general validation guidance.
   - Use `docs/agent_harness.md` for managed Codex sandbox command variants
     and diagnostic fallbacks.
   - Do not duplicate Ruff, pytest, lockfile, or sandbox command details in this
     skill.
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
   - Do not convert a failing validation result into a commit plan unless the
     user explicitly asks to commit despite the known failure.

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
   - After presenting the commit plan, ask one next-step question only: if
     relevant validation has not been run, ask to run the narrowest relevant
     pre-commit validation; if validation has passed, ask whether to create the
     commit using the proposed staging plan; if the user explicitly skipped
     validation, stop after reporting that validation was skipped and wait for
     separate explicit commit confirmation.
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

For commit execution, restate the selected commit plan before staging. Stage
only approved files, then run `git status --short`,
`git diff --staged --stat`, and `git diff --staged --check`. Verify the staged
diff contains only approved files and intended hunks; for non-trivial staging
surfaces, inspect focused `git diff --staged -- <paths>`. Create the commit only
after that verification, then report the commit hash. Never push.
