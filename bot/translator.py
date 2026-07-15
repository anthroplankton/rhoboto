# ruff: noqa: RUF001
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
            "room_number": {
                "ja": "部屋番号",
                "zh-TW": "房號",
            },
            "Configure and update the current room number.": {
                "ja": "現在の部屋番号を設定・更新します。",
                "zh-TW": "設定及更新目前房號。",
            },
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
            "post_schedule_image": {
                "ja": "現行シフト画像投稿",
                "zh-TW": "發布班表圖片",
            },
            "Post the current Final Schedule as an image.": {
                "ja": "現行シフトを画像として投稿します。",
                "zh-TW": "將現行班表發布為圖片。",
            },
            "schedule_status": {
                "ja": "シフト状態",
                "zh-TW": "班表狀態",
            },
            "channel": {
                "ja": "投稿先",
                "zh-TW": "頻道",
            },
            "final_schedule_range": {
                "ja": "現行シフト範囲",
                "zh-TW": "班表範圍",
            },
            "Schedule status used in the attachment filename.": {
                "ja": "添付ファイル名に使用するシフト状態です。",
                "zh-TW": "用於附件檔名的班表狀態。",
            },
            "Destination channel; defaults to the current channel.": {
                "ja": "投稿先チャンネル。省略時は現在のチャンネルです。",
                "zh-TW": "發布目的頻道；預設為目前頻道。",
            },
            "Optional Final Schedule rectangle, for example A1:J30.": {
                "ja": "任意の現行シフト範囲（例：A1:J30）。",
                "zh-TW": "選填的現行班表矩形範圍，例如 A1:J30。",
            },
            "Tentative": {
                "ja": "仮",
                "zh-TW": "暫定",
            },
            "Confirmed": {
                "ja": "確定",
                "zh-TW": "確定",
            },
        }
        return translations.get(string.message, {}).get(locale.value, string.message)
