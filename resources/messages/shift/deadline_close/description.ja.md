ご提出くださった皆さま、ありがとうございました！
定刻となりましたので、シフト募集を締め切らせていただきます。
-# 結果は [Google Sheets](https://example.com) で確認できます。
{% if draft_shift_proposal or final_shift_notice %}

{% endif %}{% if draft_shift_proposal %}- 仮シフト提示：　{{ draft_shift_proposal.day }}日（{{ draft_shift_proposal.weekday }}）{{ draft_shift_proposal.hour }}時
{% endif %}{% if final_shift_notice %}- 確定シフト提示：{{ final_shift_notice.day }}日（{{ final_shift_notice.weekday }}）{{ final_shift_notice.hour }}時
{% endif %}
