---
name: rhoboto-agent-branch-finalizer
description: Use when a completed Rhoboto isolated Superpowers SDD `agent/*` branch needs finalization advice, a handoff package, approved staging and local final commits, or a decision to keep the branch as-is. Do not use for normal commit advice, pushing, pull requests, merge workflows, branch deletion, worktree removal, or non-Rhoboto repositories.
---

# Rhoboto Agent Branch Finalizer

## Overview

Use this skill after isolated agent-branch SDD work is complete and the user
wants a safe path from local checkpoint commits to reviewed final project
commits. Treat checkpoint commits as review material, not final history. This
skill creates approved local final commits; it does not integrate by merging
branches.

## Non-Goals

This skill does not push, open pull requests, merge branches, delete branches,
remove worktrees, or run the default Superpowers finishing menu without
Rhoboto's handoff and explicit-approval gate. Route normal commit advice to
`$rhoboto-commit-advisor`. It does not cherry-pick into a target branch unless
the approved strategy explicitly names cherry-pick. Reference
`docs/project_setup.md` for general validation and `docs/agent_harness.md` for
sandbox, Superpowers/SDD, worktree, and handoff rules. Do not duplicate their
command lists or execution runbooks here.

## Entry Rules

- Read `AGENTS.md` first. Repository git rules override this skill.
- Require a source branch. If the target branch is not specified, default to the
  current canonical checkout branch. Do not infer a canonical target branch from
  an isolated source worktree; when in doubt, ask for the target branch.
- Verify the source and target are distinct branches before proposing execution.
- Do not execute against `main`, `master`, or `develop` unless the execution
  approval explicitly names that branch as the target.
- Accept `agent/*` source branches by default. For a non-`agent/*` source,
  analyze and warn only unless the user explicitly overrides the guard.
- Confirm whether the source branch came from isolated agent-branch SDD. If not,
  explain that this skill may be the wrong tool.
- Do not assume that approval of a handoff package is approval to stage, commit,
  rebase, squash, cherry-pick, merge, push, delete, or remove anything.
- Do not pull, fetch, or otherwise update the target branch unless the user
  explicitly approves that exact operation.
- For untracked files, inspect filenames and ignore status before reading
  contents. Do not open secret-like, database, log, export, credential, or
  private-data files just to classify them.

## Handoff Package

Before any git operation, provide a handoff package:

- source branch and target branch,
- merge base or chosen base commit used for the source-to-target diff,
- changed files and diff stat,
- concise behavior summary,
- validation evidence already run,
- Superpowers task/final review status if provided or discoverable,
- unresolved risks or missing validation,
- proposed final commit grouping,
- source-branch cleanup verdict: needed or not needed, with the reason,
- proposed integration strategy.

If Superpowers final review already exists, cite it. If not, say that final
review was not found and recommend requesting review when the change is broad or
risky. Do not automatically launch another review unless the user asks.

Use the target/source merge base for the handoff diff unless the user specifies
a different base. Do not use `HEAD~1` to summarize a multi-commit agent branch.

## Strategy Selection

Always check whether the source `agent/*` branch should be cleaned before
finalizing into the target branch. The supported strategies are:

1. **Create clean final commit(s) on the target branch.** This is the default:
   use one squash commit or a small number of logical commits from the approved
   agent-branch diff. Treat more than four proposed final commits as a warning
   sign and stop to ask whether the branch should be split.
2. **Clean the source branch first, then finalize.** This is the exception:
   recommend it only when source cleanup has concrete value for review,
   conflict resolution, or preserving logical commits.
3. **Keep the branch as-is.** Do not finalize, delete, or clean up anything;
   report what remains and stop.

Prefer applying the approved source diff to the target worktree and creating
local final commits from approved files or hunks. Choose the least
history-mutating method that preserves the target branch's existing worktree
state.

When recommending strategy 1, explicitly say that source-branch cleanup is not
needed and why. Only recommend strategy 2 when source cleanup has concrete
value, such as safer conflict resolution, checkpoint commits that already map to
logical final commits, or an explicit user desire to preserve multiple commits
derived from the agent branch.

Source-branch cleanup requires separate explicit approval before any rebase,
fixup, squash, amend, reset, or other source-history rewrite command is run.

Do not rewrite, rebase, fix up, squash, or otherwise clean the `agent/*` branch
just because checkpoint commits are messy. If clean commits can be created
directly on the target branch, recommend that simpler path.

Keeping the branch as-is is always valid, but it is a deliberate strategy, not
an implicit fallback.

## Execution Approval

Execution approval must name the source branch, target branch, strategy, final
commit grouping, and explicitly allow staging and local commit creation. If the
strategy uses `cherry-pick`, the approval must name that operation.

A user saying "looks good", "方案可以", or similar after the handoff package is
review approval only, not execution approval. Ask for explicit execution
approval.

For non-trivial staging surfaces, existing staged files, unrelated dirty
changes, many untracked files, or partial-hunk staging, use a two-step gate:
stage only approved files or hunks, show `git status --short`,
`git diff --staged --stat`, and focused staged diffs, then ask for final commit
approval before creating commits.

If the target worktree is dirty, analyze and hand off only unless all dirty
changes are part of the approved integration surface or the user explicitly
approves how to handle existing staged, unstaged, and untracked changes. Treat
the staged index as user-owned state; do not unstage, restage, or overwrite it
without explicit approval.

## Validation

Do not duplicate validation commands in this skill. Use `docs/project_setup.md`
for the general validation contract and `docs/agent_harness.md` for managed
Codex sandbox command variants and Superpowers execution rules.

List validation already run on the source branch. If validation is missing,
recommend the narrowest validation that proves the change. Do not run validation
unless the user approves it.

Do not create final commits until relevant validation has passed or the user
explicitly confirms that validation should be skipped.

## Execution Rules

- Restate the approved plan before staging.
- Stage only approved files or hunks.
- Run `git status --short`, `git diff --staged --stat`, and
  `git diff --staged --check` before each final commit.
- Verify the staged diff contains only approved files and intended hunks. For
  non-trivial commits, inspect focused staged diffs before committing.
- Create only the approved local final commit(s), then report commit hash(es).
- Never push.
- Never open a PR.
- Never merge, delete branches, remove worktrees, or clean up source history
  without separate explicit approval naming that operation.
- Never run `git reset --hard`, `git clean`, or checkout/restore commands that
  overwrite local changes.

## Common Mistakes

- Treating checkpoint commits as final project history.
- Running `superpowers:finishing-a-development-branch` options without applying
  Rhoboto's handoff gate.
- Rewriting the source branch when a clean target-branch commit is simpler.
- Mixing unrelated dirty worktree changes into final commits.
- Duplicating validation or sandbox command lists already owned by docs.
- Treating destructive commands as a shortcut for making the target branch clean.
