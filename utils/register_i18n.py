from __future__ import annotations

from typing import Final

DEFAULT_LOCALE: Final = "en"

_FEATURE_LABELS: Final[dict[str, dict[str, str]]] = {
    "team_register": {
        "en": "Team Register",
        "ja": "編成登録",
        "zh_tw": "隊伍編成登記",
    },
    "shift_register": {
        "en": "Shift Register",
        "ja": "シフト登録",
        "zh_tw": "班表登記",
    },
}

_TEXT_TEMPLATES: Final[dict[str, dict[str, str]]] = {
    "feature_label": {
        "en": "{feature_label}",
        "ja": "{feature_label}",
        "zh_tw": "{feature_label}",
    },
    "missing_config": {
        "en": "⚠️ {feature_label} is not configured for this channel.",
        "ja": "⚠️ このチャンネルでは{feature_label}が設定されていません。",
        "zh_tw": "⚠️ 此頻道尚未設定{feature_label}。",
    },
    "delete_success": {
        "en": (
            "✅ Your data for {feature_label} has been deleted from Google Sheets. "
            "If you also want to remove your original registration message from "
            "Discord, you'll need to delete it yourself."
        ),
        "ja": (
            "✅ Google Sheets 上の{feature_label}のデータを正常に削除しました。"
            "Discord 上の元の登録メッセージも削除したい場合は、"
            "ご自身で削除してください。"
        ),
        "zh_tw": (
            "✅ 已成功刪除您在 Google Sheets 中的{feature_label}資料。"
            "若也想移除 Discord 上的原始登記訊息，"  # noqa: RUF001
            "請記得自行刪除。"
        ),
    },
    "delete_confirm_prompt": {
        "en": (
            "‼️ Are you sure you want to delete your data for "
            "{feature_label} in this channel? This will only delete the data from "
            "Google Sheets."
        ),
        "ja": (
            "‼️ このチャンネルの{feature_label}のデータを削除してもよろしいですか？"  # noqa: RUF001
            "削除されるのは Google Sheets 上のデータのみです。"
        ),
        "zh_tw": (
            "‼️ 確定要刪除您在此頻道的{feature_label}資料嗎？"  # noqa: RUF001
            "這只會刪除 Google Sheets 中的資料。"
        ),
    },
    "delete_in_progress": {
        "en": "{processing_emoji} Deleting your data...",
        "ja": "{processing_emoji} データを削除しています...",
        "zh_tw": "{processing_emoji} 正在刪除您的資料...",
    },
    "delete_cancelled": {
        "en": "✖️ Delete cancelled.",
        "ja": "✖️ 削除をキャンセルしました。",
        "zh_tw": "✖️ 已取消刪除。",
    },
    "delete_timeout": {
        "en": "✖️ No response received. Delete cancelled.",
        "ja": "✖️ 応答がありませんでした。削除をキャンセルしました。",
        "zh_tw": "✖️ 未收到回應，已取消刪除。",  # noqa: RUF001
    },
    "delete_unauthorized": {
        "en": (
            "⚠️ Only the user who started this delete request can use these buttons."
        ),
        "ja": (
            "⚠️ この削除リクエストを開始したユーザーだけがこのボタンを使用できます。"
        ),
        "zh_tw": "⚠️ 只有發起此刪除請求的使用者可以操作這些按鈕。",
    },
    "delete_confirm_button": {
        "en": "Confirm",
        "ja": "確認",
        "zh_tw": "確認",
    },
    "delete_cancel_button": {
        "en": "Cancel",
        "ja": "キャンセル",
        "zh_tw": "取消",
    },
    "not_enabled": {
        "en": "⚠️ {feature_label} is not enabled in this channel.",
        "ja": "⚠️ このチャンネルでは{feature_label}が有効になっていません。",
        "zh_tw": "⚠️ 此頻道尚未啟用{feature_label}。",
    },
}


def register_user_text(
    feature_name: str,
    locale: str,
    key: str,
    *,
    fallback_display_name: str,
    **values: object,
) -> str:
    """Return localized general user-flow copy for Team/Shift Register."""
    locale_code = _locale_code(locale)
    feature_label = _feature_label(
        feature_name,
        locale_code,
        fallback_display_name=fallback_display_name,
    )
    template = _template_for(key, locale_code)
    return template.format(feature_label=feature_label, **values)


def _locale_code(locale: str) -> str:
    if locale.startswith("zh"):
        return "zh_tw"
    if locale.startswith("ja"):
        return "ja"
    return DEFAULT_LOCALE


def _feature_label(
    feature_name: str,
    locale: str,
    *,
    fallback_display_name: str,
) -> str:
    labels = _FEATURE_LABELS.get(feature_name)
    if labels is None:
        return fallback_display_name
    return labels.get(locale) or labels[DEFAULT_LOCALE]


def _template_for(key: str, locale: str) -> str:
    templates = _TEXT_TEMPLATES.get(key)
    if templates is None:
        templates = _TEXT_TEMPLATES["feature_label"]
    return templates.get(locale) or templates[DEFAULT_LOCALE]
