---
name: discord-bot-feature-plan
description: Use this skill before adding or changing a Discord bot feature, cog, slash command, context menu, Discord permission, Google Sheets workflow, or Tortoise ORM model in rhoboto. Plan first; do not edit files until a file-level implementation plan is approved.
---

You are planning a feature change for rhoboto, a personal Discord bot using discord.py 2.x, modular cogs, Tortoise ORM, and Google Sheets.

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
5. Produce a file-level plan and wait for approval unless the user explicitly asks you to implement immediately.

Output format:

- Goal
- Existing behavior
- Proposed behavior
- Affected files
- Risk areas
- Test plan
- Manual Discord UI checklist
- Implementation steps
- What will not be touched