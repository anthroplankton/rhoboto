## 📋 How to Register Your Shifts

Enter the time ranges you want to register in the format `Start-End` (30-hour clock).
Notes can be added to each range. Send one message with all of your time ranges in the shift registration channel.

**All valid time ranges are registered as your shift availability, but if any time-range entry has an invalid format, the message cannot be registered**

Example:
```text
15-20 up to 3h consecutive
20-22
16-17 no encore
```
This example registers `15-20`, `20-22`, and `16-17`.

- Times are handled in **Japan Standard Time (JST)**.
- To delete your shift registration, use the slash command `/shift delete`.
### ⚠️ Updating Your Shifts
> -# To update, send a new message with **all of your latest shift registrations**. Include every time range you want to keep.
> -# {{ bot }} treats that message as the latest data and **overwrites all previous shift registrations**. Time ranges not included in the new message will be cleared.
> -# Editing an old message does not update your registration.

After registration, {{ bot }} will automatically process your shifts. If the message receives ✅, the result has been recorded in [Google Sheets]({{ sheet_url }}) for you to view and confirm. If it receives ⚠️, the registration may not have completed successfully.
