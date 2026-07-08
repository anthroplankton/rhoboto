-# - **開始-結束**（JST、30 小時制）請送出想登記的時段。一則訊息可以包含多個時段。
-# - ‼️ 更新時，**請用新訊息送出所有想保留的時段**；先前登記會被覆蓋，只編輯舊訊息不會更新。
-# - {{ bot }} 會自動登記。✅ ⇒ 結果會記錄到 [Google Sheets]({{ sheet_url }})，可供確認。⚠️ ⇒ 可能發生錯誤。

{% if day_number and event_date %}
-# ### 第{{ day_number }}天【{{ event_date.month }}月{{ event_date.day }}日（{{ event_date.weekday }}）】班表募集時段（JST）【{{ recruitment_time_range }}】
{% elif day_number %}
-# ### 第{{ day_number }}天 班表募集時段（JST）【{{ recruitment_time_range }}】
{% elif event_date %}
-# ### 【{{ event_date.month }}月{{ event_date.day }}日（{{ event_date.weekday }}）】班表募集時段（JST）【{{ recruitment_time_range }}】
{% else %}
-# ### 班表募集時段（JST）【{{ recruitment_time_range }}】
{% endif %}
{% if submission_deadline %}
-# - 募集截止：{{ submission_deadline.day }}日（{{ submission_deadline.weekday }}）{{ submission_deadline.hour }}時
{% endif %}
{% if draft_shift_proposal %}
-# - 暫定班表：{{ draft_shift_proposal.day }}日（{{ draft_shift_proposal.weekday }}）{{ draft_shift_proposal.hour }}時
{% endif %}
{% if final_shift_notice %}
-# - 確定班表：{{ final_shift_notice.day }}日（{{ final_shift_notice.weekday }}）{{ final_shift_notice.hour }}時
{% endif %}
