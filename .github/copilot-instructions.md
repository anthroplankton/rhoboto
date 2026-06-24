# GitHub Copilot Instructions

This file is kept for GitHub Copilot compatibility. The canonical repository
instructions live in `../AGENTS.md`; follow that file first if guidance differs.

Key reminders:

- Preserve existing project conventions around `FeatureChannelBase`, manager-based
  Google Sheets access, Modal/View setup flows, and centralized cog error
  handling.
- Do not push. Do not commit unless explicitly asked.
- Do not edit secrets, `.env`, service account JSON files, local databases, or
  logs.
- Do not change Discord command names, privileged intents, database schema, or
  Google Sheets column layout without an explicit migration plan.
- For validation, run `uv run ruff check .` and
  `uv run ruff format --check .`; run the test suite once tests exist.
