# Storage Error Handling Design

## Goal

Define one storage error classification policy for Rhoboto so Discord-facing
flows and future non-interactive flows do not grow separate Google Sheets or
database error taxonomies.

## Version 1 Scope

The first implementation covers Discord user-triggered flows where the bot can
respond directly to the user:

- slash commands
- context menus
- buttons
- modals
- selects
- message listeners

These flows can report failures through ephemeral messages, followups, edited
settings views, public reactions, or registration feedback. The implementation
should keep feature disabled, missing config, invalid parser input, and
permission denied separate from storage failures.

## Storage Error Policy

Storage errors include expected Google Sheets, database availability/write, and
explicit malformed-sheet failures. They do not include arbitrary programming
errors.

Discord UI must not expose database, Tortoise, SQL, schema, credential, service
account, token, private key, raw Google API response, or private sheet data.

Discord UI should include a short runtime reference ID for classified storage
failures. Logs should include the same reference ID plus operation, feature,
guild, channel, message ID when available, and safe maintainer hints.

## Exception Boundary Rules

Storage `try` blocks should cover the smallest workflow segment that can raise
an expected storage failure and has the same user-facing recovery message. They
should not cover unrelated Discord delivery, such as normal success messages,
public announcement followups, or help followups. If Discord delivery itself
fails, that failure should bubble as a Discord/runtime failure instead of being
classified as database or Google Sheets storage failure.

The storage-error response helper is the exception: it should log the
reference ID and suppress a secondary delivery failure so the original storage
failure remains the actionable event.

Partial-success handling starts at the first operation that may mutate Google
Sheets or save storage state. Read-only metadata/config lookups can report a
normal storage failure. Once a workflow may have created worksheets, saved sheet
config, or written registration data, later classified storage failures should
use the partial-success response unless a more specific recovery message exists.

Shared decorators or wrappers should not merge flows that have different
response surfaces. Slash commands, context menus, message listeners, modals,
buttons, and selects may share classification and copy helpers, but each
boundary should still decide whether to send an interaction response, edit a
settings view, add reactions, stay silent, or mark partial success.

## Message Listener Policy

Message listeners must stay low-noise. Before a listener has established that a
channel has the feature enabled and that the message belongs to the feature
grammar, storage lookup failures should be logged without public reactions.

After a feature submission path is established, expected storage failures may
remove the processing reaction and add the configured failure/repair reactions.
Listeners should not catch arbitrary programming errors.

## Partial Success Policy

Google Sheets and the database are not transactional together. When a workflow
may have already changed worksheets or saved source data before a later storage
failure, the user-facing response must say that some changes may have happened
and the administrator should verify before retrying.

The first version does not rollback, delete worksheets, create pending setup
state, or persist error events.

## Future Non-Interactive Scope

Startup, shutdown, background jobs, scheduled tasks, deadline automation,
maintenance refreshes, cache refreshes, and Discord event handlers are outside
the first implementation. They do not have an interaction response surface, so
they need a separate design for:

- log policy
- owner alerting
- admin channel notification
- retry strategy
- pending/error state recording

Future non-interactive work should reuse `utils.storage_errors` classification
instead of introducing a second error taxonomy.

## Out Of Scope For Version 1

- startup failure alerting
- shutdown cleanup alerting
- background job alerting
- owner-only debug UI
- direct-message fallback
- database migrations
- Google Sheets worksheet layout changes
- rollback or cleanup of Google Sheets changes
- public announcement/help template changes
- full localization of administrator-only storage errors
