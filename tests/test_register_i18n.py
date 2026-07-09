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
        == "Team Register is not configured for this channel."
    )
    assert (
        register_user_text(
            "team_register",
            "ja",
            "missing_config",
            fallback_display_name="Team Register",
        )
        == "このチャンネルでは編成登録が設定されていません。"
    )
    assert (
        register_user_text(
            "shift_register",
            "zh-TW",
            "missing_config",
            fallback_display_name="Shift Register",
        )
        == "此頻道尚未設定班表登記。"
    )


def test_register_user_text_formats_delete_success() -> None:
    assert (
        register_user_text(
            "team_register",
            "en-US",
            "delete_success",
            fallback_display_name="Team Register",
        )
        == "✅ Your data for Team Register has been deleted successfully."
    )
    assert (
        register_user_text(
            "team_register",
            "ja",
            "delete_success",
            fallback_display_name="Team Register",
        )
        == "✅ 編成登録の入力データを正常に削除しました。"
    )
    assert (
        register_user_text(
            "shift_register",
            "zh-TW",
            "delete_success",
            fallback_display_name="Shift Register",
        )
        == "✅ 已成功刪除您的班表登記資料。"
    )


def test_register_user_text_formats_not_enabled() -> None:
    assert (
        register_user_text(
            "team_register",
            "en-US",
            "not_enabled",
            fallback_display_name="Team Register",
        )
        == "Team Register is not enabled in this channel."
    )
    assert (
        register_user_text(
            "team_register",
            "ja",
            "not_enabled",
            fallback_display_name="Team Register",
        )
        == "このチャンネルでは編成登録が有効になっていません。"
    )
    assert (
        register_user_text(
            "shift_register",
            "zh-TW",
            "not_enabled",
            fallback_display_name="Shift Register",
        )
        == "此頻道尚未啟用班表登記。"
    )


def test_register_user_text_falls_back_for_unknown_locale_and_feature() -> None:
    assert (
        register_user_text(
            "team_register",
            "fr",
            "missing_config",
            fallback_display_name="Team Register",
        )
        == "Team Register is not configured for this channel."
    )
    assert (
        register_user_text(
            "custom_register",
            "zh-TW",
            "missing_config",
            fallback_display_name="Custom Register",
        )
        == "此頻道尚未設定Custom Register。"
    )
