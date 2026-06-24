### 📋 How to Register Your Shifts

You can enter one or more time ranges anywhere in your message. The format is `start-end` (24-hour, e.g. `15-18`).
You may add notes before or after the time ranges; the {bot} will extract all valid ranges from your message.

**Examples:**
```
15-18 18-20 consecutive not allowed
20-22
16-17 encore not allowed 19-21
```
All `start-end` patterns (e.g. `15-18`, `18-20`, `20-22`, `16-17`, `19-21`) will be registered as your shifts, regardless of line breaks or notes.
- You can write multiple ranges in one line or across several lines.
- Add any special requests (e.g. "consecutive not allowed", "encore not allowed") after the time range.
- All shift times are recognized in **Japan Standard Time (JST)**.

To delete your shift registration, use the slash command: `/shift delete`.
To update, simply submit again; your previous shifts will be completely overwritten.

After registration, {bot} will automatically process your shifts and record the results in [Google Sheets]({sheet_url}) for you to view and confirm.
