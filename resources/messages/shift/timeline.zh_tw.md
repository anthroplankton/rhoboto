{% if day_number and event_date %}
## 🗓️ 第{{ day_number }}天｜{{ event_date.month }}月{{ event_date.day }}日（{{ event_date.weekday }}）班表登記公告
{% elif day_number %}
## 🗓️ 第{{ day_number }}天｜班表登記公告
{% elif event_date %}
## 🗓️ {{ event_date.month }}月{{ event_date.day }}日（{{ event_date.weekday }}）班表登記公告
{% else %}
## 🗓️ 班表登記公告
{% endif %}

所有時間皆為 **日本標準時（JST）**。
{% if event_date %}
### 募集時段：{{ event_date.month }}月{{ event_date.day }}日（{{ event_date.weekday }}）【{{ recruitment_time_range }}】
{% else %}
### 募集時段【{{ recruitment_time_range }}】
{% endif %}
{% if submission_deadline %}
- 募集截止：　　{{ submission_deadline.day }}日（{{ submission_deadline.weekday }}）{{ submission_deadline.hour }}時
{% endif %}
{% if draft_shift_proposal %}
- 暫定班表公布：{{ draft_shift_proposal.day }}日（{{ draft_shift_proposal.weekday }}）{{ draft_shift_proposal.hour }}時
{% endif %}
{% if final_shift_notice %}
- 確定班表公布：{{ final_shift_notice.day }}日（{{ final_shift_notice.weekday }}）{{ final_shift_notice.hour }}時
{% endif %}
