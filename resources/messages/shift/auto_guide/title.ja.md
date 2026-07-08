{% if day_number and event_date %}
{{ day_number }}日目【{{ event_date.month }}月{{ event_date.day }}日（{{ event_date.weekday }}）】シフト登録を自動で受け付けています 🙇
{% elif day_number %}
{{ day_number }}日目 シフト登録を自動で受け付けています 🙇
{% elif event_date %}
【{{ event_date.month }}月{{ event_date.day }}日（{{ event_date.weekday }}）】シフト登録を自動で受け付けています 🙇
{% else %}
シフト登録を自動で受け付けています 🙇
{% endif %}
