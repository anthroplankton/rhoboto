## 📋 How to Register Your Shifts

Enter the time ranges you want to register in the format `Start-End` (30-hour clock).
Notes can be added to each range. Send one message with all of your time ranges in the shift registration channel.

**All time ranges you enter will be registered. If even one entry has an invalid format, the entire message cannot be registered.**

Example:
```text
15-20 up to 3h consecutive
20-22
16-17 no encore
```
This example registers `15-20`, `20-22`, and `16-17`.

- Times are handled in **Japan Standard Time (JST)**.
- To delete your shift registration, use the slash command `/shift delete`.
{% if team_source_channel_id %}
- Before submitting your shifts, please submit your teams in <#{{ team_source_channel_id }}>.
{% else %}
- Before submitting your shifts, please submit your teams in the team registration channel.
{% endif %}
### ⚠️ Updating Your Shifts
> -# To update, send a new message with **all of your latest shift registrations**. Include every time range you want to keep.
> -# {{ bot }} treats that message as the latest data and **overwrites all previous shift registrations**. Time ranges not included in the new message will be cleared.
> -# Editing an old message does not update your registration.

After submission, {{ bot }} will process the shifts automatically. If the message receives a ✅, the results have been recorded in [Google Sheets]({{ sheet_url }}) for you to view and confirm. If it receives ⚠️, the registration may not have completed successfully.
