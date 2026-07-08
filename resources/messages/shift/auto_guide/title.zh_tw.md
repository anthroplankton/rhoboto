{% if day_number and event_date %}
第{{ day_number }}天【{{ event_date.month }}月{{ event_date.day }}日（{{ event_date.weekday }}）】班表登記自動受理中 🙇
{% elif day_number %}
第{{ day_number }}天 班表登記自動受理中 🙇
{% elif event_date %}
【{{ event_date.month }}月{{ event_date.day }}日（{{ event_date.weekday }}）】班表登記自動受理中 🙇
{% else %}
班表登記自動受理中 🙇
{% endif %}
