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


@pytest.mark.parametrize(
    ("locale", "weekday", "expected_event", "expected_milestone"),
    [
        ("ja", "土", "8月4日（土）", "08日（土）09時"),  # noqa: RUF001
        ("zh_tw", "六", "8月4日（六）", "08日（六）09時"),  # noqa: RUF001
        ("en", "Sat", "Aug 4 (Sat)", "08 (Sat) 09:00"),
    ],
)
@pytest.mark.parametrize(
    "template_key",
    ["shift.timeline", "shift.auto_guide.description", "shift.auto_guide.plain"],
)
def test_shift_templates_pad_only_milestone_day_and_hour(
    template_key: str,
    locale: str,
    weekday: str,
    expected_event: str,
    expected_milestone: str,
) -> None:
    content = render_message_template(
        template_key,
        locale,
        bot="@Rhoboto",
        sheet_url="https://docs.google.com/spreadsheets/d/example#gid=123",
        day_number=1,
        event_date=SimpleNamespace(
            month=8,
            month_name="Aug",
            day=4,
            weekday=weekday,
        ),
        recruitment_time_range="4-28",
        submission_deadline=SimpleNamespace(
            day="08",
            weekday=weekday,
            hour="09",
        ),
        draft_shift_proposal=None,
        final_shift_notice=None,
    )

    assert expected_event in content
    assert expected_milestone in content


@pytest.mark.parametrize("key", ["shift.guide", "team.guide"])
@pytest.mark.parametrize("locale", ["ja", "zh_tw", "en"])
def test_guide_templates_render_jinja_values(key: str, locale: str) -> None:
    content = render_message_template(
        key,
        locale,
        bot="@Rhoboto",
        sheet_url="https://docs.google.com/spreadsheets/d/example",
        team_source_channel_id=123,
    )

    assert "@Rhoboto" in content
    assert "https://docs.google.com/spreadsheets/d/example" in content
    assert "{bot}" not in content
    assert "{sheet_url}" not in content


@pytest.mark.parametrize(
    ("locale", "expected_mention", "expected_fallback"),
    [
        (
            "ja",
            "シフトを提出する前に、編成は <#123> へご提出ください。",
            "シフトを提出する前に、編成は登録用のチャンネルへご提出ください。",
        ),
        (
            "zh_tw",
            "提交班表前，請先將編成提交至 <#123>。",  # noqa: RUF001
            "提交班表前，請先將編成提交至編成登記用頻道。",  # noqa: RUF001
        ),
        (
            "en",
            "Before submitting your shifts, please submit your teams in <#123>.",
            "Before submitting your shifts, please submit your teams in the team "
            "registration channel.",
        ),
    ],
)
def test_shift_guide_team_source_channel_copy(
    locale: str,
    expected_mention: str,
    expected_fallback: str,
) -> None:
    values = {
        "bot": "@Rhoboto",
        "sheet_url": "https://docs.google.com/spreadsheets/d/example",
    }

    mention_content = render_message_template(
        "shift.guide",
        locale,
        team_source_channel_id=123,
        **values,
    )
    fallback_content = render_message_template(
        "shift.guide",
        locale,
        team_source_channel_id=None,
        **values,
    )

    assert expected_mention in mention_content
    assert expected_fallback not in mention_content
    assert expected_fallback in fallback_content
    assert "<#" not in fallback_content


@pytest.mark.parametrize(
    ("locale", "expected_full", "expected_auto"),
    [
        (
            "ja",
            (
                "募集時間の範囲内で登録したい時間帯を、`開始-終了`（JST）の形式で入力"  # noqa: RUF001
                "してください。時刻は30時間制の表記にも対応しています。"
            ),
            (
                "**開始-終了**（JST）で、募集時間の範囲内で登録したい時間帯をすべて"  # noqa: RUF001
                "1つのメッセージにまとめて、このチャンネルに送ってください。"
                "備考も各時間帯に添えられます。"
            ),
        ),
        (
            "zh_tw",
            (
                "請用 `開始-結束`（JST）的格式，輸入募集時段內想登記的時段。"  # noqa: RUF001
                "輸入時間也支援 30 小時制。"
            ),
            (
                "請用 **開始-結束**（JST）的格式，將募集時段內所有想登記的時段整理"  # noqa: RUF001
                "在一則訊息，並傳送到這個頻道。每個時段旁也可以加上備註。"  # noqa: RUF001
            ),
        ),
        (
            "en",
            (
                "Enter the time ranges you want to register within the recruitment "
                "time "
                "range in `Start-End` format (JST). The 30-hour clock notation is also "
                "supported."
            ),
            (
                "In **Start-End** format (JST), send all time ranges you want to "
                "register "
                "within the recruitment time range in one message in this channel. You "
                "can add a note to each time range."
            ),
        ),
    ],
)
def test_shift_guides_limit_entries_to_recruitment_time_range(
    locale: str,
    expected_full: str,
    expected_auto: str,
) -> None:
    values = {
        "bot": "@Rhoboto",
        "sheet_url": "https://docs.google.com/spreadsheets/d/example",
        "team_source_channel_id": None,
    }

    assert expected_full in render_message_template("shift.guide", locale, **values)
    for part in ("description", "plain"):
        assert expected_auto in render_message_template(
            f"shift.auto_guide.{part}",
            locale,
            recruitment_time_range="4-28",
            event_date=None,
            day_number=None,
            submission_deadline=None,
            draft_shift_proposal=None,
            final_shift_notice=None,
            **values,
        )


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
    ("locale", "expected_title", "expected_description", "expected_footer"),
    [
        (
            "ja",
            "2日目｜シフト登録の受付を自動で締め切りました 🙇",  # noqa: RUF001
            (
                "ご提出くださった皆さま、ありがとうございました！\n"  # noqa: RUF001
                "定刻となりましたので、シフト募集を締め切らせていただきます。\n"
                "-# 結果は [Google Sheets](https://example.com) で確認できます。\n\n"
                "- 仮シフト提示：　13日（木）20時\n"  # noqa: RUF001
                "- 確定シフト提示：14日（金）18時"  # noqa: RUF001
            ),
            "募集締切：12日（水）21時（JST）",  # noqa: RUF001
        ),
        (
            "zh_tw",
            "第2天｜班表登記已自動截止 🙇",  # noqa: RUF001
            (
                "感謝大家登記班表！\n"  # noqa: RUF001
                "募集截止時間已到，班表登記到此結束。\n"  # noqa: RUF001
                "-# 可在 [Google Sheets](https://example.com) 確認結果。\n\n"
                "- 暫定班表公布：13日（四）20時\n"  # noqa: RUF001
                "- 確定班表公布：14日（五）18時"  # noqa: RUF001
            ),
            "募集截止：12日（三）21時（JST）",  # noqa: RUF001
        ),
        (
            "en",
            "Day 2 | Shift registration has been automatically closed 🙇",
            (
                "Thank you, everyone, for your submissions!\n"
                "The submission deadline has been reached, so shift "
                "registration is now closed.\n"
                "-# Results can be checked in [Google Sheets](https://example.com).\n\n"
                "- Draft shift proposal: 13 (Thu) 20:00\n"
                "- Final shift notice: 14 (Fri) 18:00"
            ),
            "Submission deadline: 12 (Wed) 21:00 JST",
        ),
    ],
)
def test_shift_deadline_close_templates_render_exact_copy(
    locale: str,
    expected_title: str,
    expected_description: str,
    expected_footer: str,
) -> None:
    values = {
        "day_number": 2,
        "submission_deadline": SimpleNamespace(
            day=12,
            weekday={"ja": "水", "zh_tw": "三", "en": "Wed"}[locale],
            hour=21,
        ),
        "draft_shift_proposal": SimpleNamespace(
            day=13,
            weekday={"ja": "木", "zh_tw": "四", "en": "Thu"}[locale],
            hour=20,
        ),
        "final_shift_notice": SimpleNamespace(
            day=14,
            weekday={"ja": "金", "zh_tw": "五", "en": "Fri"}[locale],
            hour=18,
        ),
    }

    for part, expected in (
        ("title", expected_title),
        ("description", expected_description),
        ("footer", expected_footer),
    ):
        assert (
            render_message_template(
                f"shift.deadline_close.{part}", locale, **values
            ).rstrip("\n")
            == expected
        )


@pytest.mark.parametrize("locale", ["ja", "zh_tw", "en"])
@pytest.mark.parametrize("case_name", ["dayless", "draft_only", "final_only", "none"])
def test_shift_deadline_close_templates_guard_optional_rows(
    locale: str,
    case_name: str,
) -> None:
    day_number, draft, final = {
        "dayless": (None, True, True),
        "draft_only": (2, True, False),
        "final_only": (2, False, True),
        "none": (2, False, False),
    }[case_name]
    weekday = {
        "ja": ("水", "木", "金"),
        "zh_tw": ("三", "四", "五"),
        "en": ("Wed", "Thu", "Fri"),
    }[locale]
    values = {
        "day_number": day_number,
        "submission_deadline": SimpleNamespace(day=12, weekday=weekday[0], hour=21),
        "draft_shift_proposal": (
            SimpleNamespace(day=13, weekday=weekday[1], hour=20) if draft else None
        ),
        "final_shift_notice": (
            SimpleNamespace(day=14, weekday=weekday[2], hour=18) if final else None
        ),
    }

    title = render_message_template("shift.deadline_close.title", locale, **values)
    description = render_message_template(
        "shift.deadline_close.description", locale, **values
    )
    expected_title = {
        "ja": "2日目｜シフト登録の受付を自動で締め切りました 🙇",  # noqa: RUF001
        "zh_tw": "第2天｜班表登記已自動截止 🙇",  # noqa: RUF001
        "en": "Day 2 | Shift registration has been automatically closed 🙇",
    }[locale]
    if case_name == "dayless":
        expected_title = {
            "ja": "シフト登録の受付を自動で締め切りました 🙇",
            "zh_tw": "班表登記已自動截止 🙇",
            "en": "Shift registration has been automatically closed 🙇",
        }[locale]
    assert title.rstrip("\n") == expected_title
    draft_label = {
        "ja": "- 仮シフト提示",
        "zh_tw": "- 暫定班表公布",
        "en": "- Draft shift proposal",
    }[locale]
    final_label = {
        "ja": "- 確定シフト提示",
        "zh_tw": "- 確定班表公布",
        "en": "- Final shift notice",
    }[locale]
    assert (draft_label in description) is draft
    assert (final_label in description) is final
    if case_name == "none":
        assert "\n\n" not in description
        assert (
            description.rstrip("\n")
            == {
                "ja": (
                    "ご提出くださった皆さま、ありがとうございました！\n"  # noqa: RUF001
                    "定刻となりましたので、シフト募集を締め切らせていただきます。\n"
                    "-# 結果は [Google Sheets](https://example.com) で確認できます。"
                ),
                "zh_tw": (
                    "感謝大家登記班表！\n募集截止時間已到，班表登記到此結束。\n"  # noqa: RUF001
                    "-# 可在 [Google Sheets](https://example.com) 確認結果。"
                ),
                "en": (
                    "Thank you, everyone, for your submissions!\n"
                    "The submission deadline has been reached, so shift "
                    "registration is now closed.\n"
                    "-# Results can be checked in [Google Sheets](https://example.com)."
                ),
            }[locale]
        )


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
                "若訊息出現 ✅，結果就會記錄在 "  # noqa: RUF001
                "[Google Sheets](https://docs.google.com/spreadsheets/d/example)"
                " 中，可前往確認。若出現 ⚠️，表示登錄可能未正常完成。"  # noqa: RUF001
            ),
        ),
        (
            "en",
            (
                "If the message receives a ✅, the results have been recorded in "
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
