請用 **開始-結束**（JST）的格式，將募集時段內所有想登記的時段整理在一則訊息，並傳送到這個頻道。每個時段旁也可以加上備註。

‼️ 更新時，**請用包含所有想保留時段的新訊息送出**；先前登記會被覆蓋，只編輯舊訊息不會更新。

✅ ⇒ 結果會記錄到 [Google Sheets]({{ sheet_url }})，可供確認。
⚠️ ⇒ 可能發生錯誤。
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
