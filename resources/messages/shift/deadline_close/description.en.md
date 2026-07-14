Thank you, everyone, for your submissions!
The submission deadline has been reached, so shift registration is now closed.
-# Results can be checked in [Google Sheets](https://example.com).
{% if draft_shift_proposal or final_shift_notice %}

{% endif %}{% if draft_shift_proposal %}- Draft shift proposal: {{ draft_shift_proposal.day }} ({{ draft_shift_proposal.weekday }}) {{ draft_shift_proposal.hour }}:00
{% endif %}{% if final_shift_notice %}- Final shift notice: {{ final_shift_notice.day }} ({{ final_shift_notice.weekday }}) {{ final_shift_notice.hour }}:00
{% endif %}
