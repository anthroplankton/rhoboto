from __future__ import annotations

import pytest

from utils.message_templates import locale_to_template_code, render_message_template


@pytest.mark.parametrize(
    ("locale", "expected"),
    [
        ("en-US", "en"),
        ("ja", "ja"),
        ("zh-TW", "zh_tw"),
        ("zh-CN", "zh_tw"),
    ],
)
def test_locale_to_template_code(locale: str, expected: str) -> None:
    assert locale_to_template_code(locale) == expected


def test_render_message_template_injects_values() -> None:
    content = render_message_template(
        "shift.info",
        "en",
        bot="@Rhoboto",
        day_number=1,
        month_name="August",
        month=8,
        day=15,
        deadline_day=12,
        deadline_hour=21,
        draft_day=13,
        draft_hour=20,
        final_day=14,
        final_hour=18,
        sheet_url="https://sheet.example",
    )

    assert "@Rhoboto" in content
    assert "https://sheet.example" in content
    assert "August 15" in content
