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
        "en": "{feature_label} is not configured for this channel.",
        "ja": "このチャンネルでは{feature_label}が設定されていません。",
        "zh_tw": "此頻道尚未設定{feature_label}。",
    },
    "delete_success": {
        "en": "✅ Your data for {feature_label} has been deleted successfully.",
        "ja": "✅ {feature_label}の入力データを正常に削除しました。",
        "zh_tw": "✅ 已成功刪除您的{feature_label}資料。",
    },
    "not_enabled": {
        "en": "{feature_label} is not enabled in this channel.",
        "ja": "このチャンネルでは{feature_label}が有効になっていません。",
        "zh_tw": "此頻道尚未啟用{feature_label}。",
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
