# Announcement Language Settings Design

## Purpose

Announcement language settings let guild administrators choose which languages
Rhoboto uses for public announcement-style messages, and in what order those
messages are posted.

This setting is separate from Discord command localization. Slash command names
and descriptions continue to use `bot/translator.py`, `locale_str`, and
Discord's per-user locale handling.

## Behavior

- The setting is guild-level and stored in `GuildLanguageSettings`.
- The default announcement language order is `["ja"]`.
- Supported announcement languages are `ja`, `zh_tw`, and `en`.
- Each selected language posts one public announcement message.
- Messages are posted in the saved language order.
- Missing settings rows resolve to the default without creating a row.
- Empty, duplicate, unsupported, or otherwise invalid saved values normalize
  safely before use.

This feature currently applies only to public announcement messages from:

- `/team_register announce_guide`
- `/shift_register announce_guide`
- `/shift_register announce_timeline`

It does not affect management UI text, ephemeral setup or status responses,
delete success messages, Google Sheets errors, command names, or command
descriptions.

## Settings UI

Administrators configure the setting with:

```text
/language settings announcement
```

The command and every settings-panel callback require both `administrator` and
`manage_channels`.

The settings panel edits a draft:

- `Add Language` appends a language to the draft and defines announcement order.
- `Remove Language` removes a language, but the last language cannot be removed.
- `Reset to Default` replaces the draft with `["ja"]`.
- `Save` persists the draft.
- `Cancel` discards the draft.
- Timeout discards the draft and disables the panel.

Only `Save` writes to `GuildLanguageSettings`.

## Future Scope

The `/language settings` command group is intentionally shaped for future
language settings such as administrator UI language. No administrator UI
language setting is implemented in this feature.
