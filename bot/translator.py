import discord
from discord import app_commands
from discord.app_commands import locale_str


class Translator(app_commands.Translator):
    async def translate(
        self,
        string: locale_str,
        locale: discord.Locale,
        _: app_commands.TranslationContext,
    ) -> str:
        translations = {
            "shift": {
                "ja": "シフト",
                "zh-TW": "班表",
            },
            "team": {
                "ja": "編成",
                "zh-TW": "編成",
            },
            "delete": {
                "ja": "削除",
                "zh-TW": "刪除",
            },
            "help": {
                "ja": "ヘルプ",
                "zh-TW": "幫助",
            },
            "Show how to register your teams.": {
                "ja": "編成の登録方法を表示します。",
                "zh-TW": "如何登記隊伍編成的說明。",
            },
            "Show how to register your shifts.": {
                "ja": "シフトの登録方法を表示します。",
                "zh-TW": "如何登記班表的說明。",
            },
            "Delete your registration data for this feature in this channel.": {
                "ja": "このチャンネルでこの機能の入力データを削除します。",
                "zh-TW": "刪除您在此頻道該功能的登記資料",
            },
        }
        return translations.get(string.message, {}).get(locale.value, string.message)
