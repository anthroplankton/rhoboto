from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from jinja2 import UndefinedError

from utils import message_templates
from utils.message_templates import (
    MessageTemplateNotFoundError,
    get_message_template_name,
    get_message_template_path,
    load_message_template,
    locale_to_template_code,
    render_message_template,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture(autouse=True)
def clear_message_template_caches() -> Iterator[None]:
    load_message_template.cache_clear()
    message_templates.get_template_environment.cache_clear()
    yield
    load_message_template.cache_clear()
    message_templates.get_template_environment.cache_clear()


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


def test_get_message_template_name_and_path_use_key_segments() -> None:
    assert get_message_template_name("shift.timeline", "en") == ("shift/timeline.en.md")
    assert get_message_template_path("team.guide", "zh_tw") == (
        message_templates.TEMPLATE_ROOT / "team" / "guide.zh_tw.md"
    )


def test_render_message_template_injects_values() -> None:
    content = render_message_template(
        "shift.timeline",
        "en",
        day_number=2,
        event_date=SimpleNamespace(month=8, month_name="August", day=12, weekday="Wed"),
        recruitment_time_range="4-28",
        submission_deadline=SimpleNamespace(day=12, weekday="Wed", hour=21),
        draft_shift_proposal=SimpleNamespace(day=13, weekday="Thu", hour=20),
        final_shift_notice=SimpleNamespace(day=14, weekday="Fri", hour=18),
    )

    assert "Day 2" in content
    assert "Recruitment Time: August 12 (Wed)【4-28】" in content
    assert "Submission deadline: 12 (Wed) 21:00" in content
    assert "Draft shift proposal: 13 (Thu) 20:00" in content
    assert "Final shift notice: 14 (Fri) 18:00" in content


def test_load_message_template_raises_typed_missing_template_error() -> None:
    with pytest.raises(MessageTemplateNotFoundError) as exc_info:
        load_message_template("missing.template", "ja")

    assert exc_info.value.key == "missing.template"
    assert exc_info.value.locale == "ja"


def test_render_message_template_renders_jinja_conditionals(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    template_dir = tmp_path / "demo"
    template_dir.mkdir()
    (template_dir / "sample.en.md").write_text(
        "Hello {{ name }}{% if note %}: {{ note }}{% endif %}",
        encoding="utf-8",
    )
    monkeypatch.setattr(message_templates, "TEMPLATE_ROOT", tmp_path)
    message_templates.load_message_template.cache_clear()
    message_templates.get_template_environment.cache_clear()

    content = render_message_template("demo.sample", "en", name="Rho", note=None)

    assert content == "Hello Rho"


def test_render_message_template_raises_for_missing_jinja_values(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    template_dir = tmp_path / "demo"
    template_dir.mkdir()
    (template_dir / "strict.en.md").write_text(
        "{{ present }} {{ missing }}",
        encoding="utf-8",
    )
    monkeypatch.setattr(message_templates, "TEMPLATE_ROOT", tmp_path)
    message_templates.load_message_template.cache_clear()
    message_templates.get_template_environment.cache_clear()

    with pytest.raises(UndefinedError):
        render_message_template("demo.strict", "en", present="ok")


def test_render_message_template_does_not_autoescape_markdown(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    template_dir = tmp_path / "demo"
    template_dir.mkdir()
    (template_dir / "markdown.en.md").write_text(
        "{{ mention }} [Sheet]({{ sheet_url }})",
        encoding="utf-8",
    )
    monkeypatch.setattr(message_templates, "TEMPLATE_ROOT", tmp_path)
    message_templates.load_message_template.cache_clear()
    message_templates.get_template_environment.cache_clear()

    content = render_message_template(
        "demo.markdown",
        "en",
        mention="<@123>",
        sheet_url="https://example.test/sheet?a=1&b=2",
    )

    assert content == "<@123> [Sheet](https://example.test/sheet?a=1&b=2)"


@pytest.mark.parametrize(
    ("locale", "expected_title", "expected_deadline"),
    [
        (
            "ja",
            "## 🗓️ 1日目｜7月4日（土）シフト登録のお知らせ",  # noqa: RUF001
            "- 募集締切：　　　20日（金）21時",  # noqa: RUF001
        ),
        (
            "zh_tw",
            "## 🗓️ 第1天｜7月4日（六）班表登記公告",  # noqa: RUF001
            "- 募集截止：　　20日（五）21時",  # noqa: RUF001
        ),
        (
            "en",
            "## 🗓️ Day 1 | Jul 4 (Sat) Shift Registration Announcement",
            "- Submission deadline: 20 (Fri) 21:00",
        ),
    ],
)
def test_shift_timeline_templates_render_announcement_values(
    locale: str,
    expected_title: str,
    expected_deadline: str,
) -> None:
    event_date = SimpleNamespace(
        month=7,
        month_name="Jul",
        day=4,
        weekday={"ja": "土", "zh_tw": "六", "en": "Sat"}[locale],
    )
    submission_deadline = SimpleNamespace(
        day=20,
        weekday={"ja": "金", "zh_tw": "五", "en": "Fri"}[locale],
        hour=21,
    )
    content = render_message_template(
        "shift.timeline",
        locale,
        day_number=1,
        event_date=event_date,
        recruitment_time_range="4-10・14-20・24-28",
        submission_deadline=submission_deadline,
        draft_shift_proposal=None,
        final_shift_notice=None,
    )

    assert expected_title in content
    assert "4-10・14-20・24-28" in content
    assert expected_deadline in content
    assert "Google Sheets" not in content
    assert "注意事項" not in content
    assert "Draft shift proposal" not in content
    assert "{%" not in content


@pytest.mark.parametrize("key", ["shift.guide", "team.guide"])
@pytest.mark.parametrize("locale", ["ja", "zh_tw", "en"])
def test_guide_templates_render_jinja_values(key: str, locale: str) -> None:
    content = render_message_template(
        key,
        locale,
        bot="@Rhoboto",
        sheet_url="https://docs.google.com/spreadsheets/d/example",
    )

    assert "@Rhoboto" in content
    assert "https://docs.google.com/spreadsheets/d/example" in content
    assert "{bot}" not in content
    assert "{sheet_url}" not in content


@pytest.mark.parametrize("feature", ["team", "shift"])
@pytest.mark.parametrize("part", ["title", "description", "footer"])
@pytest.mark.parametrize("locale", ["ja", "zh_tw", "en"])
def test_auto_guide_runtime_templates_render(
    feature: str,
    part: str,
    locale: str,
) -> None:
    content = render_message_template(
        f"{feature}.auto_guide.{part}",
        locale,
        bot="@Rhoboto",
        sheet_url="https://docs.google.com/spreadsheets/d/example#gid=123",
        day_number=2,
        event_date=SimpleNamespace(
            month=8,
            month_name="Aug",
            day=12,
            weekday={"ja": "水", "zh_tw": "三", "en": "Wed"}[locale],
        ),
        recruitment_time_range="4-28",
        submission_deadline=SimpleNamespace(
            day=12,
            weekday={"ja": "水", "zh_tw": "三", "en": "Wed"}[locale],
            hour=21,
        ),
        draft_shift_proposal=SimpleNamespace(
            day=13,
            weekday={"ja": "木", "zh_tw": "四", "en": "Thu"}[locale],
            hour=20,
        ),
        final_shift_notice=SimpleNamespace(
            day=14,
            weekday={"ja": "金", "zh_tw": "五", "en": "Fri"}[locale],
            hour=18,
        ),
    )

    assert content.strip()
    for token in ("{{", "}}", "{%", "%}"):
        assert token not in content


@pytest.mark.parametrize(
    ("locale", "expected"),
    [
        (
            "ja",
            (
                "メッセージに ✅ が付けば、結果は "
                "[Google Sheets](https://docs.google.com/spreadsheets/d/example) "
                "に記録され、確認できます。⚠️ が付いた場合は、登録が正常に完了していない"
                "可能性があります。"
            ),
        ),
        (
            "zh_tw",
            (
                "若訊息上出現 ✅，代表結果已記錄到 "  # noqa: RUF001
                "[Google Sheets](https://docs.google.com/spreadsheets/d/example)"
                "，可供查看與確認。若出現 ⚠️，代表登記可能未正常完成。"  # noqa: RUF001
            ),
        ),
        (
            "en",
            (
                "If the message receives ✅, the result has been recorded in "
                "[Google Sheets](https://docs.google.com/spreadsheets/d/example) "
                "for you to view and confirm. If it receives ⚠️, "
                "the registration may not have completed successfully."
            ),
        ),
    ],
)
def test_team_guide_describes_registration_reactions(
    locale: str,
    expected: str,
) -> None:
    content = render_message_template(
        "team.guide",
        locale,
        bot="@Rhoboto",
        sheet_url="https://docs.google.com/spreadsheets/d/example",
    )

    assert expected in content
