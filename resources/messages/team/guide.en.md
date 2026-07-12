## 📋 How to Register Your Teams

Enter one team per line in the format `LeaderSkill/InternalSkill/TeamPower`.
Labels or notes may also be added before or after each team's values.
Send the message in the team registration channel.

**The formations you enter will be registered from top to bottom. If even one entry has an invalid format, the entire message cannot be registered.**

- 1st team → Main Team
- 2nd team → Encore Team, if you have one
- 3rd and later teams → Backup Teams

Example:
```text
150/740/33.4 optional note
150/700/39 optional note
140/680/35.3 optional note
```
- To delete your team registration, use the slash command `/team delete`.
### ⚠️ Updating Your Teams
> -# To update, send a new message with **all of your latest teams**. Include every team you want to keep.
> -# {{ bot }} treats that message as the latest data and **overwrites all previous team registrations**. Teams not included in the new message will be cleared.
> -# Editing an old message does not update your registration.

After submission, {{ bot }} will process the formations automatically. If the message receives a ✅, the results have been recorded in [Google Sheets]({{ sheet_url }}) for you to view and confirm. If it receives ⚠️, the registration may not have completed successfully.
