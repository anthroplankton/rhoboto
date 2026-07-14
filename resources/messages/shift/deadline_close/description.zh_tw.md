感謝大家登記班表！
募集截止時間已到，班表登記到此結束。
-# 可在 [Google Sheets](https://example.com) 確認結果。
{% if draft_shift_proposal or final_shift_notice %}

{% endif %}{% if draft_shift_proposal %}- 暫定班表公布：{{ draft_shift_proposal.day }}日（{{ draft_shift_proposal.weekday }}）{{ draft_shift_proposal.hour }}時
{% endif %}{% if final_shift_notice %}- 確定班表公布：{{ final_shift_notice.day }}日（{{ final_shift_notice.weekday }}）{{ final_shift_notice.hour }}時
{% endif %}
