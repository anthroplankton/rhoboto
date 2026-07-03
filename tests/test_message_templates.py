from __future__ import annotations

import pytest

from utils.message_templates import (
    MessageTemplateNotFoundError,
    load_message_template,
    locale_to_template_code,
    render_message_template,
)


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
        title="🐧 **Day 2 (August 12) Shift Registration Announcement** 🐧",
        recruitment_time_range="4-28",
        submission_deadline_line="Submission deadline ⇒ August 12, 21:00",
        draft_shift_proposal_line="Draft shift proposal ⇒ August 13, 20:00",
        final_shift_notice_line="Final shift notice ⇒ August 14, 18:00",
        deadline_processing_note=(
            "After the submission deadline, @Rhoboto treats registration "
            "processing as closed."
        ),
    )

    assert "Day 2" in content
    assert "Recruitment time range: 【4-28】" in content
    assert "Submission deadline" in content
    assert "@Rhoboto" in content


def test_load_message_template_raises_typed_missing_template_error() -> None:
    with pytest.raises(MessageTemplateNotFoundError) as exc_info:
        load_message_template("missing.template", "ja")

    assert exc_info.value.key == "missing.template"
    assert exc_info.value.locale == "ja"


@pytest.mark.parametrize("locale", ["ja", "zh_tw", "en"])
def test_shift_info_templates_render_required_values(locale: str) -> None:
    content = render_message_template(
        "shift.info",
        locale,
        title="Shift title Day 2",
        recruitment_time_range="4-28",
        submission_deadline_line="Submission deadline ⇒ August 12, 21:00",
        draft_shift_proposal_line="Draft shift proposal ⇒ August 13, 20:00",
        final_shift_notice_line="Final shift notice ⇒ August 14, 18:00",
        deadline_processing_note=(
            "After the submission deadline, @Rhoboto treats registration "
            "processing as closed."
        ),
    )

    assert "Shift title Day 2" in content
    assert "4-28" in content
    assert "Submission deadline" in content
    assert "@Rhoboto" in content


@pytest.mark.parametrize("locale", ["ja", "zh_tw", "en"])
def test_shift_deadline_processing_note_templates_render(locale: str) -> None:
    content = render_message_template(
        "shift.info_deadline_processing_note",
        locale,
        bot="@Rhoboto",
    )

    assert "@Rhoboto" in content
