from bot import config
from utils.register_i18n import register_user_text


def test_register_user_text_localizes_known_feature_labels() -> None:
    assert (
        register_user_text(
            "team_register",
            "en-US",
            "feature_label",
            fallback_display_name="Team Register",
        )
        == "Team Register"
    )
    assert (
        register_user_text(
            "team_register",
            "ja",
            "feature_label",
            fallback_display_name="Team Register",
        )
        == "編成登録"
    )
    assert (
        register_user_text(
            "shift_register",
            "zh-TW",
            "feature_label",
            fallback_display_name="Shift Register",
        )
        == "班表登記"
    )


def test_register_user_text_uses_complete_templates_by_locale() -> None:
    assert (
        register_user_text(
            "team_register",
            "en-US",
            "missing_config",
            fallback_display_name="Team Register",
        )
        == "⚠️ Team Register is not configured for this channel."
    )
    assert (
        register_user_text(
            "team_register",
            "ja",
            "missing_config",
            fallback_display_name="Team Register",
        )
        == "⚠️ このチャンネルでは編成登録が設定されていません。"
    )
    assert (
        register_user_text(
            "shift_register",
            "zh-TW",
            "missing_config",
            fallback_display_name="Shift Register",
        )
        == "⚠️ 此頻道尚未設定班表登記。"
    )


def test_register_user_text_formats_delete_success() -> None:
    assert (
        register_user_text(
            "team_register",
            "en-US",
            "delete_success",
            fallback_display_name="Team Register",
        )
        == "✅ Your data for Team Register has been deleted from Google Sheets. If "
        "you also want to remove your original registration message from Discord, "
        "you'll need to delete it yourself."
    )
    assert register_user_text(
        "team_register",
        "ja",
        "delete_success",
        fallback_display_name="Team Register",
    ) == (
        "✅ Google Sheets 上の編成登録の入力データを正常に削除しました。"
        "Discord 上の元の登録メッセージも削除したい場合は、"
        "ご自身で削除してください。"
    )
    assert register_user_text(
        "shift_register",
        "zh-TW",
        "delete_success",
        fallback_display_name="Shift Register",
    ) == (
        "✅ 已成功刪除您在 Google Sheets 中的班表登記資料。"
        "若也想移除 Discord 上的原始登記訊息，"  # noqa: RUF001
        "請記得自行刪除。"
    )


def test_register_user_text_formats_not_enabled() -> None:
    assert (
        register_user_text(
            "team_register",
            "en-US",
            "not_enabled",
            fallback_display_name="Team Register",
        )
        == "⚠️ Team Register is not enabled in this channel."
    )
    assert (
        register_user_text(
            "team_register",
            "ja",
            "not_enabled",
            fallback_display_name="Team Register",
        )
        == "⚠️ このチャンネルでは編成登録が有効になっていません。"
    )
    assert (
        register_user_text(
            "shift_register",
            "zh-TW",
            "not_enabled",
            fallback_display_name="Shift Register",
        )
        == "⚠️ 此頻道尚未啟用班表登記。"
    )


def test_register_user_text_falls_back_for_unknown_locale_and_feature() -> None:
    assert (
        register_user_text(
            "team_register",
            "fr",
            "missing_config",
            fallback_display_name="Team Register",
        )
        == "⚠️ Team Register is not configured for this channel."
    )
    assert (
        register_user_text(
            "custom_register",
            "zh-TW",
            "missing_config",
            fallback_display_name="Custom Register",
        )
        == "⚠️ 此頻道尚未設定Custom Register。"
    )


def test_register_user_text_formats_delete_confirmation_copy() -> None:
    assert register_user_text(
        "team_register",
        "en-US",
        "delete_confirm_prompt",
        fallback_display_name="Team Register",
    ) == (
        "‼️ Are you sure you want to delete your data for Team Register in this "
        "channel? This will only delete the data from Google Sheets."
    )
    assert register_user_text(
        "team_register",
        "ja",
        "delete_confirm_prompt",
        fallback_display_name="Team Register",
    ) == (
        "‼️ このチャンネルの編成登録の入力データを削除してもよろしいですか？"  # noqa: RUF001
        "削除されるのは Google Sheets 上のデータのみです。"
    )
    assert (
        register_user_text(
            "shift_register",
            "zh-TW",
            "delete_confirm_prompt",
            fallback_display_name="Shift Register",
        )
        == "‼️ 確定要刪除您在此頻道的班表登記資料嗎？這只會刪除 Google Sheets 中的資料。"  # noqa: RUF001
    )


def test_register_user_text_formats_delete_confirmation_status_copy() -> None:
    assert (
        register_user_text(
            "team_register",
            "en-US",
            "delete_in_progress",
            fallback_display_name="Team Register",
            processing_emoji=config.PROCESSING_EMOJI,
        )
        == f"{config.PROCESSING_EMOJI} Deleting your data..."
    )
    assert (
        register_user_text(
            "team_register",
            "ja",
            "delete_cancelled",
            fallback_display_name="Team Register",
        )
        == "✖️ 削除をキャンセルしました。"
    )
    assert (
        register_user_text(
            "shift_register",
            "zh-TW",
            "delete_timeout",
            fallback_display_name="Shift Register",
        )
        == "✖️ 未收到回應，已取消刪除。"  # noqa: RUF001
    )
    assert register_user_text(
        "team_register",
        "en-US",
        "delete_unauthorized",
        fallback_display_name="Team Register",
    ) == ("⚠️ Only the user who started this delete request can use these buttons.")


def test_register_user_text_formats_delete_confirmation_button_labels() -> None:
    assert (
        register_user_text(
            "team_register",
            "en-US",
            "delete_confirm_button",
            fallback_display_name="Team Register",
        )
        == "Confirm"
    )
    assert (
        register_user_text(
            "team_register",
            "ja",
            "delete_cancel_button",
            fallback_display_name="Team Register",
        )
        == "キャンセル"
    )
    assert (
        register_user_text(
            "shift_register",
            "zh-TW",
            "delete_cancel_button",
            fallback_display_name="Shift Register",
        )
        == "取消"
    )
