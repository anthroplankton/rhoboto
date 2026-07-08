{% if day_number and event_date %}
Day {{ day_number }}【{{ event_date.month_name }} {{ event_date.day }} ({{ event_date.weekday }})】Shift registration is open and processed automatically 🙇
{% elif day_number %}
Day {{ day_number }} Shift Registration is open and processed automatically 🙇
{% elif event_date %}
【{{ event_date.month_name }} {{ event_date.day }} ({{ event_date.weekday }})】Shift registration is open and processed automatically 🙇
{% else %}
Shift registration is open and processed automatically 🙇
{% endif %}
