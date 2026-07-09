---
name: discord-bot-feature-plan
description: Use when a Rhoboto request may add or change Discord bot feature behavior, cogs, slash commands, context menus, Discord permissions, settings UI, Google Sheets workflows, Tortoise ORM models or schema, or localized user-facing text.
---

# Rhoboto Discord Feature Plan

Use this skill as Rhoboto's feature-change safety overlay. It complements, not
replaces, the user's normal workflow: Superpowers brainstorming -> spec ->
implementation plan -> execution, or occasional planning-with-files for
persistent investigation.

Do not create a competing workflow. If Superpowers brainstorming, specs, or
implementation plans are active, feed this skill's Rhoboto-specific findings
into that flow. If planning-with-files is active, keep working notes there but
do not treat `.planning/` as tracked project documentation. Repository routing
in `AGENTS.md` and execution gates in `docs/agent_harness.md` override this
skill.

Before editing files:

1. Inspect the relevant existing files.
2. Identify whether the change affects:
   - Discord cogs or app_commands
   - context menus
   - privileged intents
   - permissions
   - FeatureChannelBase behavior
   - Tortoise ORM models or schema
   - Google Sheets worksheet titles, IDs, columns, or update behavior
   - user-facing EN / JA / ZH-TW text
3. Separate pure business logic from Discord API, Google Sheets API, and database code.
4. Propose tests before implementation whenever pure logic can be tested.
5. Produce or review a file-level implementation plan.
6. Stop for approval before implementation unless the user has already approved
   the exact file-level plan or is explicitly asking to execute an already
   approved plan.

Output format:

- Workflow context
- Goal
- Existing behavior
- Proposed behavior
- Affected files
- Risk areas
- Test plan
- Manual Discord UI checklist
- Implementation steps
- What will not be touched
- Approval status
