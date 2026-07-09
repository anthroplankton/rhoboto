**開始-終了**（JST・30時間制）で、登録したい時間帯を送ってください。複数の時間帯を1つのメッセージにまとめられ、備考も各時間帯に添えられます。

‼️ 更新時は、**残したいシフトをすべて含めた新しいメッセージで送ってください**。以前の登録は上書きされ、編集だけでは更新されません。

✅ ⇒ 結果は [Google Sheets]({{ sheet_url }}) に記録され、確認できます。
⚠️ ⇒ エラーの可能性があります。
{% if event_date %}
### 募集時間：{{ event_date.month }}月{{ event_date.day }}日（{{ event_date.weekday }}）【{{ recruitment_time_range }}】
{% else %}
### 募集時間【{{ recruitment_time_range }}】
{% endif %}
{% if submission_deadline %}
- 募集締切：　　　{{ submission_deadline.day }}日（{{ submission_deadline.weekday }}）{{ submission_deadline.hour }}時
{% endif %}
{% if draft_shift_proposal %}
- 仮シフト提示：　{{ draft_shift_proposal.day }}日（{{ draft_shift_proposal.weekday }}）{{ draft_shift_proposal.hour }}時
{% endif %}
{% if final_shift_notice %}
- 確定シフト提示：{{ final_shift_notice.day }}日（{{ final_shift_notice.weekday }}）{{ final_shift_notice.hour }}時
{% endif %}
