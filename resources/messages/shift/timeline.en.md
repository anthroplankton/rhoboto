{% if day_number and event_date %}
## 🗓️ Day {{ day_number }} | {{ event_date.month_name }} {{ event_date.day }} ({{ event_date.weekday }}) Shift Registration Announcement
{% elif day_number %}
## 🗓️ Day {{ day_number }} | Shift Registration Announcement
{% elif event_date %}
## 🗓️ {{ event_date.month_name }} {{ event_date.day }} ({{ event_date.weekday }}) Shift Registration Announcement
{% else %}
## 🗓️ Shift Registration Announcement
{% endif %}

All times are in **Japan Standard Time (JST)**.
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
