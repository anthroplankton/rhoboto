from __future__ import annotations

# ruff: noqa: RUF001
import discord
import pytest
from discord.app_commands import locale_str

from bot.translator import Translator

TRANSLATIONS = [
    ("post_schedule_image", "現行シフト画像投稿", "發布班表圖片"),
    (
        "Post the current Final Schedule as an image.",
        "現行シフトを画像として投稿します。",
        "將現行班表發布為圖片。",
    ),
    ("schedule_status", "シフト状態", "班表狀態"),
    ("channel", "投稿先", "頻道"),
    ("final_schedule_range", "現行シフト範囲", "班表範圍"),
    (
        "Schedule status used in the attachment filename.",
        "添付ファイル名に使用するシフト状態です。",
        "用於附件檔名的班表狀態。",
    ),
    (
        "Destination channel; defaults to the current channel.",
        "投稿先チャンネル。省略時は現在のチャンネルです。",
        "發布目的頻道；預設為目前頻道。",
    ),
    (
        "Optional Final Schedule rectangle, for example A1:J30.",
        "任意の現行シフト範囲（例：A1:J30）。",
        "選填的現行班表矩形範圍，例如 A1:J30。",
    ),
    ("Tentative", "仮", "暫定"),
    ("Confirmed", "確定", "確定"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("key", "japanese", "chinese"), TRANSLATIONS)
async def test_schedule_image_command_translations(
    key: str,
    japanese: str,
    chinese: str,
) -> None:
    translator = Translator()

    assert (
        await translator.translate(locale_str(key), discord.Locale.japanese, None)
        == japanese
    )
    assert (
        await translator.translate(locale_str(key), discord.Locale.taiwan_chinese, None)
        == chinese
    )


@pytest.mark.asyncio
async def test_translator_falls_back_to_original_message() -> None:
    key = "unknown translation key"

    assert (
        await Translator().translate(
            locale_str(key),
            discord.Locale.japanese,
            None,
        )
        == key
    )
