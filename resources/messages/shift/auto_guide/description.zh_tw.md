**開始-結束**（JST、30 小時制）請送出想登記的時段。一則訊息可以包含多個時段，也可以在每個時段旁加上備註。

‼️ 更新時，**請用包含所有想保留時段的新訊息送出**；先前登記會被覆蓋，只編輯舊訊息不會更新。

✅ ⇒ 結果會記錄到 [Google Sheets]({{ sheet_url }})，可供確認。
⚠️ ⇒ 可能發生錯誤。

### 募集時段（JST）【{{ recruitment_time_range }}】
{% if submission_deadline %}
- 募集截止：{{ submission_deadline.day }}日（{{ submission_deadline.weekday }}）{{ submission_deadline.hour }}時
{% endif %}
{% if draft_shift_proposal %}
- 暫定班表：{{ draft_shift_proposal.day }}日（{{ draft_shift_proposal.weekday }}）{{ draft_shift_proposal.hour }}時
{% endif %}
{% if final_shift_notice %}
- 確定班表：{{ final_shift_notice.day }}日（{{ final_shift_notice.weekday }}）{{ final_shift_notice.hour }}時
{% endif %}
