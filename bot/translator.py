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
            "guide": {
                "ja": "使い方",
                "zh-TW": "說明",
            },
            "Show how to register your teams.": {
                "ja": "編成登録の使い方を表示します。",
                "zh-TW": "顯示隊伍編成登記說明。",
            },
            "Show how to register your shifts.": {
                "ja": "シフト登録の使い方を表示します。",
                "zh-TW": "顯示班表登記說明。",
            },
            "Delete your team registration in this channel.": {
                "ja": "このチャンネルの編成登録を削除します。",
                "zh-TW": "刪除您在此頻道的隊伍編成登記。",
            },
            "Delete your shift registration in this channel.": {
                "ja": "このチャンネルのシフト登録を削除します。",
                "zh-TW": "刪除您在此頻道的班表登記。",
            },
        }
        return translations.get(string.message, {}).get(locale.value, string.message)
