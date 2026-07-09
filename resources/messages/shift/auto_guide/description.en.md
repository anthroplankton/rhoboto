Send **Start-End** (JST, 30-hour clock) for each time range in this channel. Multiple time ranges can be included in one message, and notes can be added to each range.

‼️ To update, **send a new message with every shift time you want to keep**. Previous registrations are overwritten, and editing an old message does not update registration.

✅ ⇒ Results are recorded in [Google Sheets]({{ sheet_url }}) for review.
⚠️ ⇒ An error may have occurred.
{% if event_date %}
### Recruitment Time: {{ event_date.month_name }} {{ event_date.day }} ({{ event_date.weekday }})【{{ recruitment_time_range }}】
{% else %}
### Recruitment Time【{{ recruitment_time_range }}】
{% endif %}
{% if submission_deadline %}
- Submission deadline: {{ submission_deadline.day }} ({{ submission_deadline.weekday }}) {{ submission_deadline.hour }}:00
{% endif %}
{% if draft_shift_proposal %}
- Draft shift proposal: {{ draft_shift_proposal.day }} ({{ draft_shift_proposal.weekday }}) {{ draft_shift_proposal.hour }}:00
{% endif %}
{% if final_shift_notice %}
- Final shift notice: {{ final_shift_notice.day }} ({{ final_shift_notice.weekday }}) {{ final_shift_notice.hour }}:00
{% endif %}
