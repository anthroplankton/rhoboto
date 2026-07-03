# Manual Integration Validation

Use this runbook to manually validate Rhoboto against a development Discord
guild and a disposable Google spreadsheet. Do not use production channels,
production sheets, or real user data.

## Prerequisites

- A Discord development guild with at least two text channels:
  - one channel for `team_register`
  - one channel for `shift_register`
- A bot installation in that guild with slash commands synced.
- One administrator test user with both `administrator` and `manage_channels`.
- One non-admin test user for permission checks.
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
| Black format | `uv run black --check --workers 1 main.py bot cogs components models utils` |  |  |
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
| Invalid Sheet link | Submit Team or Shift settings with a malformed or inaccessible Sheet URL. | The bot returns a safe ephemeral Google Sheets error and does not show a success settings embed. |  |  |
| Missing sharing permission | Submit settings for a disposable spreadsheet that is not shared with the service account. | The bot asks to check sharing or service account access and does not expose credential paths, service account JSON, raw traceback, or private Sheet details. |  |  |
| Missing worksheet | Configure settings, delete or rename one configured worksheet in the disposable spreadsheet, then run the feature settings or summary/delete command. | The bot reports a safe Google Sheets error or shows the worksheet as not found without exposing raw Google API details. |  |  |
| Message write failure | With an invalid or inaccessible configured sheet, send a Team or Shift registration message. | The processing reaction is removed when present, both `⚠️` and `🛠️` appear, and no raw Google error is posted to the channel. |  |  |

## Team Register

Use the team test channel.

| Scenario | Steps | Pass Criteria | Result | Notes |
| --- | --- | --- | --- | --- |
| Enable feature | Run `/team_register enable`. | Feature is enabled and setup prompt appears. |  |  |
| Create settings | Open settings, enter test sheet URL, team worksheet titles, and summary worksheet title. | Bot saves settings and creates or finds worksheets. |  |  |
| Settings embed | Run `/team_register settings`. | Embed shows worksheet titles together with worksheet IDs and the Google Sheet link. |  |  |
| Settings callback guard | Remove permissions from the admin test user after opening the Team Register settings modal, then submit it. | The bot returns an ephemeral permission error and does not save settings. |  |  |
| Encore role callback guard | As the non-admin user, use an existing encore role select menu. | The bot returns an ephemeral permission error and does not update encore roles. |  |  |
| Help text | Run `/team help` and `/team_register help`. | Help content renders from templates and includes the bot mention and Sheet link. |  |  |
| Team submission | Send lines in order: `150/740/33.4 main`, `150/700/39 encore`, and `140/680/35.3 backup`. | Processing reaction is removed, check reaction is added, and Main, Encore, and Backup worksheets update in message order. |  |  |
| Team overwrite update | After registering three teams, send only `150/740/33.4 updated main` as a new message. | The Main worksheet updates and the user's old Encore and Backup rows are cleared. |  |  |
| Full-width Team submission | Send `150／740／33.4 main`. | Processing reaction is removed, check reaction is added, and team worksheets update. |  |  |
| Invalid Team attempt | Send `160//600/33`, `160,600,33`, or `160 600 33`. | No worksheet write occurs and the confused reaction appears. |  |  |
| Team ordinary text | Send ordinary announcement text with no team-like numbers. | No worksheet write occurs and no reaction appears. |  |  |
| Team context menu invalid attempt | Use the `team_register upsert` context menu on `160//600/33`. | Confused reaction appears on the selected message and the context menu returns the existing failed-upsert follow-up. |  |  |
| Summary refresh | Run `/team_register summary`. | Summary worksheet and summary embed match the submitted teams and encore roles. |  |  |
| Delete own data | Run `/team delete`. | The current user's team rows and summary row are removed or blanked as expected. |  |  |

## Shift Register

Use the shift test channel.

| Scenario | Steps | Pass Criteria | Result | Notes |
| --- | --- | --- | --- | --- |
| Enable feature | Run `/shift_register enable`. | Feature is enabled and setup prompt appears. |  |  |
| Create settings | Open settings, enter test sheet URL, entry/draft/final worksheet titles, and final schedule anchor cell. | Bot saves settings and creates or finds all worksheets. |  |  |
| Timeline settings | In the Shift Register settings panel, click `Edit Shift Timeline`; enter day number, event date, submission deadline, draft shift proposal, and final shift notice. | Bot saves the timeline and the refreshed settings embed shows the saved values in JST. |  |  |
| Recruitment range settings | In the Shift Register settings panel, click `Edit Recruitment Time Range`; enter `4-12, 20-28`. Then reopen the modal. | Bot saves the normalized recruitment range and the modal is prefilled from the saved DB value. |  |  |
| Settings embed | Run `/shift_register settings`. | Embed shows worksheet titles, worksheet IDs, Sheet link, final schedule anchor cell, Shift Timeline, and Recruitment Time Range. |  |  |
| Settings callback guard | Remove permissions from the admin test user after opening the Shift Register settings modal, then submit it. | The bot returns an ephemeral permission error and does not save settings. |  |  |
| Timeline validation error | Open `Edit Shift Timeline`, enter `0` for day number or `8/12 24` for a milestone, and submit. | Bot sends an ephemeral validation error with `Edit Again`; clicking it reopens the modal with the submitted values. |  |  |
| Recruitment range validation error | Open `Edit Recruitment Time Range`, enter `28-4`, and submit. | Bot sends an ephemeral validation error with `Edit Again`; no setting is saved. |  |  |
| Info message | Run `/shift_register info` with no parameters after saving timeline and recruitment range. | Public info message is posted in the configured announcement languages. It includes the saved day/date when present, recruitment range, milestone lines, and bot mention when a submission deadline is set. |  |  |
| Help text | Run `/shift help` and `/shift_register help`. | Help content renders from templates and includes the bot mention and Sheet link. |  |  |
| Shift Entry header | Confirm the entry worksheet header is `username`, `display_name`, `0-1` through `29-30`, then `original_message`. | Valid shift writes use the `0-30` columns. Old `4-5` through `27-28` headers are not silently accepted. |  |  |
| Shift submission | With recruitment range `4-12, 20-28`, send `4-8` or `20-28`. | Processing reaction is removed, check reaction is added, and entry worksheet updates. |  |  |
| Out-of-range shift | With recruitment range `4-12, 20-28`, send `12-20` or `0-30`. | No worksheet write occurs and the confused reaction appears. |  |  |
| Invalid shift | Send a message with a range-like invalid attempt. | No worksheet write occurs and the confused reaction appears. |  |  |
| Invalid Shift time attempt | Send `18:00-20:00`, `18點到20點`, `18點到`, or `到20點`. | No worksheet write occurs and the confused reaction appears. |  |  |
| Shift ordinary text | Send `20:00` or `20點前`. | No worksheet write occurs and no reaction appears. |  |  |
| Shift context menu invalid attempt | Use the `shift_register upsert` context menu on `18:00-20:00`. | Confused reaction appears on the selected message and the context menu returns the existing failed-upsert follow-up. |  |  |
| Delete own data | Run `/shift delete`. | The current user's entry row is removed or blanked as expected. |  |  |

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
| Team help announcements | Run `/team_register help`. | The channel receives one public help message per saved language, in saved order, and each message includes the bot mention and Sheet link. |  |  |
| Shift help announcements | Run `/shift_register help`. | The channel receives one public help message per saved language, in saved order, and each message includes the bot mention and Sheet link. |  |  |
| Shift info announcements | Run `/shift_register info` after saving Shift Timeline and Recruitment Time Range settings. | The channel receives one public info message per saved language, in saved order. The messages include the saved day/date when present, recruitment range, milestone lines, and bot mention when a submission deadline is set. |  |  |

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
