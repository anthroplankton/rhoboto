### 📋 How to Register Your Teams

Each line represents a team. The format is `LeaderSkill/InternalSkill/TeamPower`, and you may add notes at the end of each line.

Example:
```
150/740/33.4 This is the main team
140/680/35.3 No HP check
150/700/39 Encore, any other notes
```
Order does not matter. {bot} will automatically determine:
- The team with the highest effective skill value is the "Main Team"
- Among the rest, the one with the highest power (not less than the main team) is the "Encore Team"
- Others are "Backup Teams"
- As long as a line contains the format `xxx/xxx/xx.x`, it will be recognized, so adding labels at the beginning of the line is also fine.

To delete your team data, please use the slash command: `/team delete`.
To update, simply submit again; your previous team registrations will be removed or completely overwritten.
Japanese:

After registration, {bot} will automatically process your teams and record the results in [Google Sheets]({sheet_url}) for you to view and confirm.
