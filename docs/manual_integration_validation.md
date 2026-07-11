# Manual Integration Validation

Use this runbook to manually validate Rhoboto against a development Discord
guild and a disposable Google spreadsheet. Do not use production channels,
production sheets, or real user data.

## Prerequisites

- A Discord development guild with at least two text channels:
  - one channel for `team_register`
  - one channel for `shift_register`
- A bot installation in that guild using the documented invite permissions,
  with slash commands synced and the required privileged intents enabled.
- One administrator test user with both `administrator` and `manage_channels`.
- One non-admin test user for permission checks.
- If validating role assignment, the bot role is above the test target roles,
  and the test target roles do not grant high-risk permissions.
- A disposable Google spreadsheet shared with the service account email.
- Local environment variables configured in `.env` or the shell:
  - `DISCORD_TOKEN`
  - `DATABASE_URL`
  - `GOOGLE_SERVICE_ACCOUNT_PATH`

Never commit `.env`, service account JSON files, local databases, logs, or
spreadsheet exports.

## Preflight

Record the result for each command before starting manual Discord checks.

| Check | Command | Result | Notes |
| --- | --- | --- | --- |
| Install locked deps | `uv sync --locked` |  |  |
| Tests | `uv run pytest` |  |  |
| Ruff lint | `uv run ruff check --no-fix .` |  |  |
| Ruff format | `uv run ruff format --check .` |  |  |
| Compile | `uv run python -m compileall -q main.py bot cogs components models utils` |  |  |

Start the bot locally:

```shell
uv run python main.py
```

Pass criteria:

- Bot logs show successful startup.
- Slash commands are visible in the development guild.
- No secrets or local runtime files are staged in git.

## Discord Feature Checks

| Scenario | Steps | Pass Criteria | Result | Notes |
| --- | --- | --- | --- | --- |
| Bot status | Open the dev guild and confirm the bot is online. | Bot appears online and responds to slash commands. |  |  |
| Feature list | Run `/features` in each test channel. | Embed lists configured features or states that none exist. |  |  |
| Admin guard | As the non-admin user, try a settings command. | Discord denies access or the bot returns the permission error. |  |  |
| Callback guard | As the non-admin user, click a previously visible settings button. | The bot returns an ephemeral permission error and does not open a modal. |  |  |

## Google Sheets Failure Checks

Run these checks with disposable spreadsheets only. Do not paste production
sheet links or service account details into notes.

| Scenario | Steps | Pass Criteria | Result | Notes |
| --- | --- | --- | --- | --- |
| Invalid Sheet link | Submit Team or Shift settings with a malformed or inaccessible Sheet URL. | The bot returns safe ephemeral Sheet access/link guidance and does not show a success settings embed. |  |  |
| Missing sharing permission | Submit settings for a disposable spreadsheet that is not shared with the configured Google identity. | The bot asks to check sheet sharing, sheet settings, or the saved Sheet link and does not expose credential paths, credential contents, raw traceback, or private Sheet details. |  |  |
| Missing worksheet | Configure settings, delete or rename one configured worksheet in the disposable spreadsheet, then run the feature settings or summary/delete command. | The bot reports safe worksheet/storage guidance or shows the worksheet as not found without exposing raw Google API details. |  |  |
| Message write failure | With an invalid or inaccessible configured sheet, send a Team or Shift registration message. | The processing reaction is removed when present, both `⚠️` and `🛠️` appear, and no raw Google error is posted to the channel. |  |  |

## Storage Error Handling

Run these checks in a development guild with a disposable Google spreadsheet.
Do not use production Discord channels, production sheets, or real user data.

| Scenario | Steps | Pass Criteria | Result | Notes |
| --- | --- | --- | --- | --- |
| Google Sheets access denied | Remove sharing from the disposable spreadsheet, then run Team or Shift settings, summary, or delete flow that reads the configured sheet. | The bot returns safe access guidance that asks the user to check sheet sharing, sheet settings, or the saved Sheet link. The response does not mention credential internals. |  |  |
| Invalid saved Sheet URL | In the development guild, use the settings modal to save a clearly invalid disposable Sheet URL for a Team or Shift feature. If the UI blocks malformed input, use an approved development-only failure injection instead, then run a settings or command flow that reloads the sheet. | The bot gives invalid link guidance and does not show a success settings embed or raw Google API error. |  |  |
| Missing configured worksheet | Remove or rename a configured worksheet in the disposable spreadsheet, then run a feature command that uses it. | The bot reports missing worksheet guidance that is actionable without exposing worksheet internals beyond configured display metadata. |  |  |
| Malformed worksheet | Change disposable worksheet headers or required columns so the sheet no longer matches the expected Team or Shift layout, then trigger a read or write. | The bot returns a safe malformed-sheet response and does not post a raw traceback, credential path, or private Sheet data. |  |  |
| Database unavailable | Temporarily run the bot with an invalid local database path or inject a database failure in a development-only run, then submit a settings modal or command that reads or writes feature state. | The interaction returns a visible safe failure response instead of going silent or timing out without explanation. |  |  |
| Listener storage failure | While storage is failing, submit Team and Shift listener messages that would normally write to storage. | Any processing reaction is cleaned up. Failure and repair reactions appear as defined by the feature, and no raw storage exception is posted publicly. |  |  |
| Team summary partial success | In a development guild with a disposable spreadsheet, first confirm Team source worksheet writes succeed. Before summary refresh, remove or rename the summary worksheet, or use approved development-only failure injection if a non-sheet mutation is needed, then submit a Team registration or run a Team summary refresh flow. | The bot reports partial success behavior: the source write is acknowledged, the summary refresh failure is visible, and the response includes repair guidance. |  |  |
| Reference IDs | Trigger at least one Google Sheets storage failure and one database storage failure through interaction flows, then compare the Discord UI with the bot logs. For listener-only failures, compare the reactions with the bot logs. | Interaction responses and logs include matching reference IDs. Listener-only failures log reference IDs and use only the defined reactions publicly. Logs include enough operation context to locate the failure without exposing private data. |  |  |
| Sensitive data guard | Review the Discord responses and relevant logs from the storage failure checks. | UI and logs do not expose credential contents, credential file contents, private Sheet cell data, raw tokens, or private identifiers. |  |  |

## Team Register

Use the team test channel.

| Scenario | Steps | Pass Criteria | Result | Notes |
| --- | --- | --- | --- | --- |
| Enable feature | Run `/team_register enable`. | Feature is enabled and setup prompt appears. |  |  |
| Pre-setup Team listener attempt | Before creating settings, send `150/740/33.4 main` and `160//600/33`. | The bot does not add reactions, post a reply, or write worksheet data. |  |  |
| Pre-setup Team context menu upsert | Before creating settings, use the `team_register upsert` context menu on `150/740/33.4 main`. | The context menu response is ephemeral and says Team Register is not configured for this channel. No public message is posted. |  |  |
| Create settings | Open settings, enter test sheet URL, team worksheet titles, and summary worksheet title. | Bot saves settings and creates or finds worksheets. |  |  |
| Settings embed | Run `/team_register settings`. | Embed shows worksheet titles together with worksheet IDs and the Google Sheet link. |  |  |
| Settings callback guard | Remove permissions from the admin test user after opening the Team Register settings modal, then submit it. | The bot returns an ephemeral permission error and does not save settings. |  |  |
| Encore role callback guard | As the non-admin user, use an existing encore role select menu. | The bot returns an ephemeral permission error and does not update encore roles. |  |  |
| Guide text | Run `/team guide` and `/team_register announce_guide`. | Guide content renders from templates and includes the bot mention and Sheet link. |  |  |
| Team submission | Send lines in order: `150/740/33.4 main`, `150/700/39 encore`, and `140/680/35.3 backup`. | Processing reaction is removed, the `✅` reaction is added, and Main, Encore, and Backup worksheets update in message order. |  |  |
| Team overwrite update | After registering three teams, send only `150/740/33.4 updated main` as a new message. | The Main worksheet updates and the user's old Encore and Backup rows are cleared. |  |  |
| Full-width Team submission | Send `150／740／33.4 main`. | Processing reaction is removed, the `✅` reaction is added, and team worksheets update. |  |  |
| Invalid Team attempt | Send `160//600/33`, `160,600,33`, or `160 600 33`. | No worksheet write occurs, and `⚠️` then the confused reaction appear. |  |  |
| Team ordinary text | Send ordinary announcement text with no team-like numbers. | No worksheet write occurs and no reaction appears. |  |  |
| Team context menu invalid attempt | Use the `team_register upsert` context menu on `160//600/33`. | `⚠️` then the confused reaction appear on the selected message, and the context menu returns an ephemeral failed-upsert follow-up. |  |  |
| Summary refresh | Run `/team_register summary`. | Summary worksheet and summary embed match the submitted teams and encore roles. |  |  |
| Delete own data confirmation | Run `/team delete`, confirm the localized `‼️` prompt appears, click Cancel, and verify the current user's Team rows and summary row remain. Run `/team delete` again and click Confirm. | Cancel shows `✖️` cancellation copy and leaves data unchanged. Confirm shows processing copy with `config.PROCESSING_EMOJI`, then the current user's Team rows and summary row are removed or blanked as expected. |  |  |

## Shift Register

Use the shift test channel.

| Scenario | Steps | Pass Criteria | Result | Notes |
| --- | --- | --- | --- | --- |
| Enable feature | Run `/shift_register enable`. | Feature is enabled and setup prompt appears. |  |  |
| Pre-setup Shift listener attempt | Before creating settings, send `4-8` and `18:00-20:00`. | The bot does not add reactions, post a reply, or write worksheet data. |  |  |
| Pre-setup Shift context menu upsert | Before creating settings, use the `shift_register upsert` context menu on `4-8`. | The context menu response is ephemeral and says Shift Register is not configured for this channel. No public message is posted. |  |  |
| Create settings | Open settings, enter test sheet URL, entry/draft/final worksheet titles, and final schedule anchor cell. | Bot saves settings and creates or finds all worksheets, then opens the optional Team Source flow. |  |  |
| Timeline settings | In the Shift Register settings panel, click `Edit Shift Timeline`; enter day number, event date, submission deadline, draft shift proposal, and final shift notice. | Bot saves the timeline and the refreshed settings embed shows the saved values in JST. |  |  |
| Recruitment range settings | In the Shift Register settings panel, click `Edit Recruitment Time Range`; enter `4-12, 20-28`. Then reopen the modal. | Bot saves the normalized recruitment range and the modal is prefilled from the saved DB value. |  |  |
| Unset Team source | Use `Set Later` after initial Sheet setup, then reopen Shift settings. | Team Source says no source is selected; Shift registration still updates availability without Team references. |  |  |
| Initial Team source candidates | Complete initial Sheet setup with zero, one, and multiple configured Team Registers in separate checks. | Zero shows no selector and only `Set Later`; one preselects its channel in native ChannelSelect; multiple leaves ChannelSelect unselected. No case persists a source before `Apply & Repair`. |  |  |
| Settings embed with selected Team source | Choose a Team source and apply it, then run `/shift_register settings`. | Embed shows worksheet titles/IDs, Sheet link, selected Team Source channel and Team Register Google Sheet link, final schedule anchor cell, Shift Timeline, and Recruitment Time Range. The Team Source does not repeat its landing worksheet title or ID. |  |  |
| Team source landing links | Open the Team guide, Shift settings Team Source link, and Shift guide. | The Team guide and Shift settings open the same Team landing worksheet (currently Team Summary); the Shift guide opens Shift Entry. |  |  |
| No configured Team Register | Open `Edit Team Source` with no Team Register configuration in the guild. | The bot shows that no Team Register is configured and provides only `Back to Settings`. |  |  |
| Select Team source | Open `Edit Team Source`, choose one configured Team Register channel, then press `Apply & Repair`. | The refreshed settings panel shows the selected channel, retains `Edit Team Source` and the current Latest Guide control, and subsequent Shift formulas use only that source. |  |  |
| Back from Team source | Open `Edit Team Source` and press `Back to Settings` without applying. | The settings panel returns with no database or Google Sheets change. |  |  |
| Team source callback guard | Open `Edit Team Source`, select a channel, remove `administrator` or `manage_channels`, then press `Apply & Repair`. | The bot returns an ephemeral permission error and does not save the selection or write Shift Entry. |  |  |
| Invalid Team source selection | Select a channel without configured Team Register settings and apply. | The bot shows `⚠️`; the saved source and all existing Shift Entry formulas remain unchanged. |  |  |
| Team Summary rename repair | Rename the selected Team Summary worksheet, reopen `Edit Team Source`, keep the same channel, and press `Apply & Repair`. | Every populated Shift Entry C anchor uses the current worksheet title; A:B, D:E, F:AJ, AK+, row order, formatting, validation, and notes remain unchanged. |  |  |
| Team source repair no-op | Open `Edit Team Source` and apply the current source without changing the source or worksheet title. | The operation succeeds without rewriting already-current formulas. |  |  |
| Team source repair partial success | After validating a selectable Team source, use approved development-only failure injection for the Shift Entry write and apply it. Restore Sheet access and apply again. | The first attempt reports `⚠️🛠️`, retains the saved source, and does not clear formulas. The retry repairs the formulas. |  |  |
| Team source open is read-only | Open and close `Edit Team Source` without applying. | No database value or Google Sheets cell changes. |  |  |
| Invalid Team source | With one Team Register, remove a configured Team/Summary worksheet or alter the required Summary header, then reopen Shift settings and submit a Shift. | Settings reports an invalid Team source; Shift availability still updates, stale `C` anchors are cleared, and no guessed Team columns are used. |  |  |
| Temporarily unreadable Team source | With a previously working Team source, use approved development-only failure injection for a transient Summary read failure, then submit a Shift. | Shift availability still updates, the existing `C` formula is preserved, settings reports that the source cannot currently be read, and logs contain no private Sheet data. |  |  |
| Settings callback guard | Remove permissions from the admin test user after opening the Shift Register settings modal, then submit it. | The bot returns an ephemeral permission error and does not save settings. |  |  |
| Timeline validation error | Open `Edit Shift Timeline`, enter `0` for day number or `8/12 24` for a milestone, and submit. | Bot sends an ephemeral validation error with `Edit Again`; clicking it reopens the modal with the submitted values. |  |  |
| Recruitment range validation error | Open `Edit Recruitment Time Range`, enter `28-4`, and submit. | Bot sends an ephemeral validation error with `Edit Again`; no setting is saved. |  |  |
| Timeline announcement | Run `/shift_register announce_timeline` with no parameters after saving timeline and recruitment range. | Public timeline announcement is posted in the configured announcement languages. It includes the saved day/date when present, recruitment range, milestone lines, and bot mention when a submission deadline is set. |  |  |
| Guide text | Run `/shift guide` and `/shift_register announce_guide`. | Guide content renders from templates and includes the bot mention and Sheet link. |  |  |
| Shift Entry layout | Confirm row 1 is the count row and row 2 is `username`, `display_name`, `Main ISV`, `Encore ISV`, `Team Info`, `0-1` through `29-30`, then `original_message`. | Bot-owned columns end at `AJ`; hours are `F:AI`; administrator-owned columns begin at `AK`. |  |  |
| Shift Entry count formulas | Submit and update several users with overlapping and different hours. Inspect `F1` through `AI1`. | Each cell uses the corresponding `COUNTIF(<column>$3:<column>, 1)` formula and updates as availability changes. |  |  |
| Team formula access | On the first Shift submission after linking the Team source, inspect the `IMPORTRANGE` result and select **Allow access** when prompted. | The cross-spreadsheet connection is granted once and `C:E` populate; no helper worksheet or helper cell exists. |  |  |
| Team display: no registered team | Submit a Shift for a user who has not registered a Team. | Both ISV fields are blank and `Team Info` is `No team yet`. |  |  |
| Team display: role and Encore Team | Give a user at least one configured Encore role and a second registered Team, then submit a Shift. | `Main ISV` shows Main, `Encore ISV` shows the second Team, and `Team Info` contains only `<roles>`. |  |  |
| Team display: role with Main fallback | Give a user at least one configured Encore role but only a Main Team, then submit a Shift. | Both ISV columns show Main and `Team Info` is `<roles>｜Main fallback`. |  |  |
| Team display: Encore Team without role | Register a second Team for a user with no configured Encore role, then submit a Shift. | Main and Encore ISV values are shown and `Team Info` is `No role`; the display does not imply scheduler eligibility. |  |  |
| Team display: no role or Encore Team | Leave a user with only Main and no configured Encore role, then submit a Shift. | Main ISV is shown; Encore ISV and `Team Info` are blank. |  |  |
| Formula no-op | Submit the same user twice without changing the Team source or Summary structure. | The existing `C` formula text remains identical and does not visibly reload solely because of the Shift update. |  |  |
| Team source change repair | Rename configured Team/Summary worksheets or change the unique Team source, then submit one Shift. | Only stale participant `C` anchors are repaired to the new source; unchanged formulas are not rewritten. |  |  |
| Same-row update and manual-cell preservation | Add text, a formula, formatting, validation, and a note in `AK+` on a participant row. Update that participant's Shift. | The username stays on the same physical row and all `AK+` content/metadata remains attached and unchanged. |  |  |
| First blank row reuse | Leave a blank username row between two participants and put a prepared manual value in `AK+`; register a new user. | The new user uses the first blank username row and the prepared `AK+` value is preserved. |  |  |
| Filter view | Create a Google Sheets Filter view and sort participants by `Main ISV` or `Encore ISV`; then update Team Summary data. | Formula values refresh automatically. The administrator can reapply or adjust the Filter view; the bot does not reorder rows. |  |  |
| Generate Draft threshold validation | Invoke `/shift_register generate_draft` without the required threshold, with text, and with a negative number; then use `35`. | Discord blocks the first three submissions. The valid command reports Runner, `安可綜合力閾值：35`, `募集時間【...】`, then the overwrite warning. |  |  |
| Generate Draft Team scheduling | Use participants covering role/no-role, Encore Team/Main fallback, Power below/equal/above `35`, and missing Team data. | Only role-bearing participants strictly above the applicable Power threshold enter Encore; Honso/standby use Main ISV and no-team participants rank last. |  |  |
| Generate Draft continuity | Use adjacent hours where Encore, Honso, and standby candidates remain available, then introduce a higher-ISV candidate. | The approved cross-role priority applies, the lowest selected Main ISV is standby each hour, and Honso columns stay stable when role decisions permit. |  |  |
| Generate Draft continuous time axis | Configure `4-12, 20-28`, leave a development-only stale/manual `1` in a middle Shift Entry hour, then generate the Draft. | Draft contains every row from `4-5` through `27-28`; `12-13` through `19-20` remain visible and empty because out-of-range availability is ignored, while Discord's assigned/unassigned sections omit those gap hours. Position and longest-run continuity reset across the gap while accumulated load remains. |  |  |
| Generate Draft Team Source fallback | Test unset, invalid, and temporarily unreadable Team Source states. | Draft still generates with Encore empty; the shared Japanese Discord/Notes warning shows `⚠️` for unset or `⚠️🛠️` for unavailable data. |  |  |
| Generate Draft reply ordering and unregistered warning | Use an available Team Source with assigned and unassigned candidates that lack Main ISV, including one current guild member and one unmatched username. | The reply shows `⚠️ 編成未登録：...` with the same mention/canonical-name formatting as schedule rows, then `募集時間【...】` immediately above `已排入`. It includes every affected Draft candidate, not only assigned candidates. The attachment explanation is the final line. With unavailable Team Source, the unregistered line is omitted. |  |  |
| Generate Draft dynamic Notes | Include duplicate and reserved-suffix display names, generate the Draft, then rearrange canonical participant cells manually within `C:G`. | One formula spills an eight-column table across `A:H` below the schedule. The order is `メモ`, recruitment time, optional warnings, one blank row, headers and participants, another blank row, then both Japanese legends. Team values use `実効値/総合力`, missing Main ISV shows `未登録`, duplicate names resolve exactly, and values update automatically. |  |  |
| Generate Draft Notes workload order | Give participants different total hours, longest runs, and Encore hours, including a canonical-name tie-break case. | Both Sheet Notes and `shift-draft-notes.txt` sort by total hours descending, longest run descending, Encore hours descending, then canonical name ascending. |  |  |
| Generate Draft Notes snapshot | Generate a Draft containing Japanese text and an original message with an ` ⏎  ` separator, then manually rearrange `C:G`. | The ephemeral reply attaches UTF-8 `shift-draft-notes.txt`; it contains the generation-time input snapshot with the same semantic content and participant order as initial Sheet Notes. Each participant is one labeled narrative line separated by `｜`, absent optional Team segments are omitted, missing Main Team data is `内部編成 未登録`, and blank lines remain before participants and legends. After rearrangement, Sheet Notes update while the attachment stays fixed. |  |  |
| Generate Draft canonical names | Register duplicate display names and a display name already ending in `⟨@valid_name⟩`. | Duplicate/reserved names receive exactly one real username suffix, remain distinguishable, and Notes resolve each exact canonical name. |  |  |
| Generate Draft atomic overwrite | Generate a long Draft, then a shorter Draft, with approved write-failure injection between checks. Put sentinel values in `I+`. | Shorter generation clears stale `A:G` values and stale Notes-column `H` values from the new anchor downward while preserving `I+`; the injected failed batch leaves the previous Draft intact. |  |  |
| Malformed legacy Entry rejection | Restore a disposable row-1 header or old `4-5` through `27-28` layout and submit a Shift. | The bot reports a safe malformed-sheet failure and performs no worksheet mutation. |  |  |
| Shift submission | With recruitment range `4-12, 20-28`, send `4-8` or `20-28`. | Processing reaction is removed, the `✅` reaction is added, and entry worksheet updates. |  |  |
| Out-of-range shift | With recruitment range `4-12, 20-28`, send `12-20` or `0-30`. | No worksheet write occurs, and `⚠️` then the confused reaction appear. |  |  |
| Invalid shift | Send a message with a range-like invalid attempt. | No worksheet write occurs, and `⚠️` then the confused reaction appear. |  |  |
| Invalid Shift time attempt | Send `18:00-20:00`, `18點到20點`, `18點到`, or `到20點`. | No worksheet write occurs, and `⚠️` then the confused reaction appear. |  |  |
| Shift ordinary text | Send `20:00` or `20點前`. | No worksheet write occurs and no reaction appears. |  |  |
| Shift context menu invalid attempt | Use the `shift_register upsert` context menu on `18:00-20:00`. | `⚠️` then the confused reaction appear on the selected message, and the context menu returns an ephemeral failed-upsert follow-up. |  |  |
| Delete own data confirmation | Run `/shift delete`, confirm the localized `‼️` prompt appears, click Cancel, and verify the current user's Shift entry row remains. Run `/shift delete` again and click Confirm. | Cancel shows `✖️` cancellation copy and leaves data unchanged. Confirm shows processing copy with `config.PROCESSING_EMOJI`, then physically deletes the current user's whole row so every later row's values, formulas, formatting, validation, and notes move together. |  |  |

## Announcement Languages

Run these checks after Team Register and Shift Register settings exist in the
development guild.

| Scenario | Steps | Pass Criteria | Result | Notes |
| --- | --- | --- | --- | --- |
| Open settings | As the administrator user, run `/language settings announcement`. | An ephemeral language settings panel appears and shows the saved language order. |  |  |
| Command guard | As the non-admin user, run `/language settings announcement`. | Discord denies access or the bot returns the permission error. |  |  |
| Callback guard | As the administrator user, open the language settings panel, then remove `administrator` or `manage_channels` before pressing Save. | The bot returns an ephemeral permission error and does not save changes. |  |  |
| Save ordered languages | Add languages so the draft order is Japanese, Traditional Chinese, then English, and press Save. | The saved panel shows Japanese, Traditional Chinese, then English in that order. |  |  |
| Reopen saved order | Run `/language settings announcement` again. | The panel shows the same saved language order. |  |  |
| Cancel discards draft | Add or remove a language, then press Cancel without saving. Reopen the panel. | The saved language order is unchanged. |  |  |
| Timeout discards draft | Add or remove a language, wait for the panel to time out, then reopen the panel. | The saved language order is unchanged and the timed-out panel is disabled. |  |  |
| Team guide announcements | Run `/team_register announce_guide`. | The channel receives one public guide message per saved language, in saved order, and each message includes the bot mention and Sheet link. |  |  |
| Shift guide announcements | Run `/shift_register announce_guide`. | The channel receives one public guide message per saved language, in saved order, and each message includes the bot mention and Sheet link. |  |  |
| Shift timeline announcements | Run `/shift_register announce_timeline` after saving Shift Timeline and Recruitment Time Range settings. | The channel receives one public timeline message per saved language, in saved order. The messages include the saved day/date when present, recruitment range, milestone lines, and bot mention when a submission deadline is set. |  |  |

## Latest Guide Message

Run these checks in a development channel with Team Register and Shift Register
settings available.

Schema note: this feature adds the Tortoise-managed
`feature_channel_message_state` table for bot-managed `auto_guide` and
`manual_guide` message state. Fresh databases create it from the current models
through `generate_schemas()`. Existing deployments must apply the usual schema
rollout before enabling the feature because `generate_schemas()` is not a safe
production migration mechanism; back up the database and verify the table exists
first.

| Scenario | Steps | Pass Criteria | Result | Notes |
| --- | --- | --- | --- | --- |
| Enable latest guide | Open `/team_register settings`, enable Team Register Latest Guide, and save. | A short guide message is sent. |  |  |
| Latest Guide buttons | Enable Latest Guide for Team and Shift in a development guild. Configure announcement languages with each of `en`, `zh_tw`, and `ja` first in separate checks. | The latest guide shows buttons in order: `🗑️` Delete, `⤴️` Full Guide when replying to a manual guide, and `👀 Google Sheets`. Delete and Full Guide labels follow the first announcement language. Google Sheets remains `Google Sheets`. |  |  |
| Latest Guide delete button | Click the `🗑️` Delete button from Team and Shift latest guides. Test Confirm, Cancel, timeout, and another user clicking Confirm. | The flow matches `/team delete` and `/shift delete`: confirmation is ephemeral, Cancel/timeout apply no changes, only the requester can confirm, and Confirm deletes only the requester's registration data. |  |  |
| Latest Guide Full Guide link | Post a manual guide announcement, enable or refresh Latest Guide, then click `⤴️` Full Guide on desktop and mobile Discord clients. | The link opens the replied manual guide message in the Discord client. If it opens an external browser unexpectedly, capture the client/platform and revisit whether to keep this button. |  |  |
| Latest Guide Google Sheets link | Click `👀 Google Sheets` on Team and Shift latest guides. | The link opens the configured Google Sheets URL with the expected worksheet gid. |  |  |
| Latest Guide delete after restart | Enable Latest Guide, restart the bot, then click `🗑️` Delete on an existing latest guide message. | The persistent Delete button still starts the existing ephemeral delete confirmation flow after restart. |  |  |
| Reply to full guide | Run `/team_register announce_guide`, then send a non-bot message. | The short guide replies to the latest full guide and shows the footer. |  |  |
| Missing full guide anchor | Delete the full guide anchor message, then send a non-bot message. | The short guide falls back to a normal message and has no footer. |  |  |
| Refresh after messages | Send three ordinary non-bot messages. | The guide refreshes after each message, and the previous guide is deleted when bot permissions allow it. |  |  |
| Disable latest guide | Disable Latest Guide from `/team_register settings`. | The previous short guide is deleted, or the administrator receives the delete-permission warning. |  |  |
| Soft disable feature | Run the feature's `/disable` command. | Latest Guide is disabled, and the same delete warning appears if the bot cannot delete the old guide. |  |  |
| Hard clear feature | Run `/disable_and_clear` and confirm. | Feature settings are cleared, and the hard clear delete warning appears if the bot cannot delete the old guide. |  |  |
| Team settings refresh | Edit Team sheet settings, then edit Encore roles. | Latest Guide refreshes after Team sheet changes and does not refresh after Encore role changes. |  |  |
| Shift settings refresh | Edit Shift sheet settings, timeline, and recruitment time range. | Each successful save refreshes Latest Guide. |  |  |
| Permission warnings | Remove send, reply, and delete permissions in a development channel. | Administrator warnings match the design, and registration still works. |  |  |

## Feature Lifecycle

Run these checks for both feature channels.

| Scenario | Steps | Pass Criteria | Result | Notes |
| --- | --- | --- | --- | --- |
| Soft disable | Run the feature's `/disable` command. | Settings remain stored and message processing stops. |  |  |
| Re-enable | Run the feature's `/enable` command. | Existing settings are reused without re-entering the Sheet URL. |  |  |
| Hard clear | Run `/disable_and_clear` and confirm. | Feature settings are deleted; the next settings command shows setup state. |  |  |
| Hard-clear callback guard | Start `/disable_and_clear` as the admin test user, remove permissions before clicking Confirm, then confirm. | The bot returns an ephemeral permission error and does not clear settings. |  |  |

## Cleanup

- Stop the local bot process.
- Delete or archive the disposable spreadsheet.
- Remove test Discord messages if needed.
- Inspect git state:

```shell
git status --short --untracked-files=all
```

Pass criteria:

- No `.env`, service account JSON, local database, logs, or generated runtime
  artifacts are staged.
- Any retained screenshots or notes are scrubbed of secrets and private IDs.

## Validation Summary

| Field | Value |
| --- | --- |
| Date |  |
| Validator |  |
| Git commit or branch |  |
| Discord guild |  |
| Test spreadsheet |  |
| Overall result |  |
| Follow-up issues |  |
