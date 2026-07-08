-# - **start-end** (JST, 30-hour clock) for each time range you want to register. You can include multiple ranges in one message.
-# - ‼️ To update, **send a new message with every shift time you want to keep**. Previous registrations are overwritten, and editing an old message does not update registration.
-# - {{ bot }} processes it automatically. ✅ ⇒ Results are recorded in [Google Sheets]({{ sheet_url }}) for review. ⚠️ ⇒ An error may have occurred.

{% if day_number and event_date %}
-# ### Day {{ day_number }}【{{ event_date.month_name }} {{ event_date.day }} ({{ event_date.weekday }})】Shift recruitment time (JST)【{{ recruitment_time_range }}】
{% elif day_number %}
-# ### Day {{ day_number }} Shift recruitment time (JST)【{{ recruitment_time_range }}】
{% elif event_date %}
-# ### 【{{ event_date.month_name }} {{ event_date.day }} ({{ event_date.weekday }})】Shift recruitment time (JST)【{{ recruitment_time_range }}】
{% else %}
-# ### Shift recruitment time (JST)【{{ recruitment_time_range }}】
{% endif %}
{% if submission_deadline %}
-# - Submission deadline: {{ submission_deadline.day }} ({{ submission_deadline.weekday }}) {{ submission_deadline.hour }}:00
{% endif %}
{% if draft_shift_proposal %}
-# - Draft shift proposal: {{ draft_shift_proposal.day }} ({{ draft_shift_proposal.weekday }}) {{ draft_shift_proposal.hour }}:00
{% endif %}
{% if final_shift_notice %}
-# - Final shift notice: {{ final_shift_notice.day }} ({{ final_shift_notice.weekday }}) {{ final_shift_notice.hour }}:00
{% endif %}
