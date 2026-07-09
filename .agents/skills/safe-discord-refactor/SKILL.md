---
name: safe-discord-refactor
description: Use this skill when refactoring rhoboto code while preserving behavior. Especially relevant for discord.py cogs, FeatureChannelBase, Google Sheets managers, Tortoise ORM access, parser logic, and UI components. Do not use for new features.
---

You are performing a safe refactor in rhoboto.

Rules:

1. Preserve existing behavior exactly unless the user explicitly requests a behavior change.
2. Do not rename slash commands, context menus, feature names, database fields, worksheet columns, worksheet titles, or public command text without an explicit migration plan.
3. Prefer small, reviewable patches.
4. Add or update tests before refactoring pure logic.
5. Do not touch secrets, deployment settings, tokens, or service account files.
6. Do not change Discord privileged intents unless explicitly requested.
7. Do not change Tortoise ORM schema or Google Sheets data layout as part of a refactor.

Workflow:

1. Inspect current git status and relevant files.
2. Identify the exact behavior to preserve.
3. Locate existing tests. If missing, propose focused tests first.
4. Make the smallest viable refactor.
5. Run formatting, linting, and tests if available.
6. Summarize changed files, validation, and remaining risk.

Output format:

- Refactor target
- Behavior preserved
- Files changed
- Tests added or run
- Validation result
- Remaining risk