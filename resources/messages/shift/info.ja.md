{% if day_number and event_date %}
## 🗓️ {{ day_number }}日目【{{ event_date.month }}月{{ event_date.day }}日（{{ event_date.weekday }}）】シフト登録のお知らせ
{% elif day_number %}
## 🗓️ {{ day_number }}日目 シフト登録のお知らせ
{% elif event_date %}
## 🗓️【{{ event_date.month }}月{{ event_date.day }}日（{{ event_date.weekday }}）】シフト登録のお知らせ
{% else %}
## 🗓️ シフト登録のお知らせ
{% endif %}

表示時刻はすべて **日本標準時（JST）** です。
### 募集時間【{{ recruitment_time_range }}】

{% if submission_deadline %}
- 募集締切：　　　{{ submission_deadline.day }}日（{{ submission_deadline.weekday }}）{{ submission_deadline.hour }}時
{% endif %}
{% if draft_shift_proposal %}
- 仮シフト提示：　{{ draft_shift_proposal.day }}日（{{ draft_shift_proposal.weekday }}）{{ draft_shift_proposal.hour }}時
{% endif %}
{% if final_shift_notice %}
- 確定シフト提示：{{ final_shift_notice.day }}日（{{ final_shift_notice.weekday }}）{{ final_shift_notice.hour }}時
{% endif %}
