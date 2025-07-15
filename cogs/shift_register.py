from __future__ import annotations

from typing import TYPE_CHECKING, override

from bot import config
from cogs.base.feature_channel_base import FeatureChannelBase
from components.ui_shift_register import (
    ShiftRegisterView,
    build_current_settings_embed,
)
from models.feature_channel import FeatureChannel
from utils.key_async_lock import KeyAsyncLock
from utils.shift_register_manager import ShiftRegisterManager
from utils.shift_register_structs import Shift, ShiftParser
from utils.structs_base import UserInfo

if TYPE_CHECKING:
    from discord import Interaction, Message

    from bot import Rhoboto


class ShiftRegister(
    FeatureChannelBase[ShiftRegisterManager, Shift], group_name="shift_register"
):

    feature_name = "shift_register"
    lock = KeyAsyncLock()

    ManagerType = ShiftRegisterManager

    async def setup_after_enable(self, interaction: Interaction) -> None:
        if interaction.channel is None or interaction.guild is None:
            msg = (
                "Interaction channel or guild is None. "
                "Cannot proceed with setup message."
            )
            raise ValueError(msg)
        guild_id = interaction.guild.id
        channel_id = interaction.channel.id
        feature_channel = await FeatureChannel.get(
            guild_id=guild_id,
            channel_id=channel_id,
            feature_name=self.feature_name,
        )

        manager = ShiftRegisterManager(
            feature_channel, config.GOOGLE_SERVICE_ACCOUNT_PATH
        )

        shift_register_config = await manager.get_sheet_config_or_none()
        if shift_register_config is None:
            content = (
                "Shift Register is not yet configured for this channel. "
                "Click below to set up."
            )
            embed = None
            view = ShiftRegisterView(shift_register_manager=manager)
        else:
            metadata = await manager.fetch_google_sheets_metadata()
            embed = build_current_settings_embed(
                sheet_url=shift_register_config.sheet_url,
                metadata=metadata,
                final_schedule_anchor_cell=shift_register_config.final_schedule_anchor_cell,
                color=config.DEFAULT_EMBED_COLOR,
            )
            view = ShiftRegisterView(
                shift_register_manager=manager,
                has_existing_settings=True,
                sheet_url=shift_register_config.sheet_url,
                entry_worksheet_title=metadata.entry_worksheets.title,
                draft_worksheet_title=metadata.draft_worksheet.title,
                final_schedule_worksheet_title=metadata.final_schedule_worksheet.title,
                final_schedule_anchor_cell=shift_register_config.final_schedule_anchor_cell,
            )

        if embed is None:
            await interaction.followup.send(content=content, view=view, ephemeral=True)
        else:
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    @override
    async def process_upsert_from_message(self, message: Message) -> Shift | None:
        """
        Listen for messages to provide a button for shift register setup/edit.
        This is used in channels where the feature is enabled.
        """
        if (
            message.author.bot
            or not message.guild
            or not message.channel
            or not await self.is_enabled(message.guild.id, message.channel.id)
        ):
            return None

        self.logger.debug(
            "Received message in Guild: `%s` Channel: `%s` (Feature: `%s`): %r",
            message.guild.id,
            message.channel.id,
            self.feature_name,
            message.content,
        )

        user_info = UserInfo(
            username=message.author.name,
            display_name=message.author.display_name,
        )
        shift = ShiftParser.parse_lines(user_info, message.content.splitlines())
        if not shift:
            return None

        self.logger.info(
            "Parsed shift in Guild: `%s` Channel: `%s` (Feature: `%s`): `%s` (%r)",
            message.guild.id,
            message.channel.id,
            self.feature_name,
            message.author.display_name,
            shift,
        )

        feature_channel = await FeatureChannel.get_or_none(
            guild_id=message.guild.id,
            channel_id=message.channel.id,
            feature_name=self.feature_name,
        )
        if not feature_channel:
            return None

        manager = ShiftRegisterManager(
            feature_channel, config.GOOGLE_SERVICE_ACCOUNT_PATH
        )

        shift_register_config = await manager.get_sheet_config_or_none()
        if shift_register_config is None:
            return None

        if self.bot.user is not None:
            await message.add_reaction(config.PROCESSING_EMOJI)

        async with self.lock(message.channel.id):
            metadata = await manager.fetch_google_sheets_metadata()
            manager.log_missing_worksheet_warnings(metadata)

            metadata = await manager.ensure_worksheets_and_upsert_sheet_config(metadata)

            await manager.upsert_or_delete_user_shift(
                user_info, shift, metadata=metadata
            )

        if self.bot.user is not None:
            await message.remove_reaction("⌛", self.bot.user)
            await message.remove_reaction(config.PROCESSING_EMOJI, self.bot.user)
            await message.add_reaction("✅")

        return shift

    help_text_en = """### 📋 How to Register Your Shifts

You can enter one or more time ranges anywhere in your message. The format is `start-end` (24-hour, e.g. `15-18`).
You may add notes before or after the time ranges; the bot will extract all valid ranges from your message.

**Examples:**
```
15-18 18-20 consecutive not allowed
20-22
16-17 encore not allowed 19-21
```
All `start-end` patterns (e.g. `15-18`, `18-20`, `20-22`, `16-17`, `19-21`) will be registered as your shifts, regardless of line breaks or notes.
- You can write multiple ranges in one line or across several lines.
- Add any special requests (e.g. "consecutive not allowed", "encore not allowed") after the time range.
- To delete your shift registration, use the slash command: `/shift delete`.
- After processing, your shifts will be shown in [Google Sheets]({}) for your review.
"""

    help_text_ja = """## 📋 シフト登録の使い方

メッセージ内のどこに書いても、`開始-終了`（24時間表記、例：`15-18`）の形式で書かれた全ての時間帯が登録されます。
行頭・行末・1行に複数区間・備考付き、すべてOKです。

**例：**
```
15-18 18-20 連続不可
20-22
16-17 アンコ不可 19-21
```
この例では `15-18`、`18-20`、`20-22`、`16-17`、`19-21` の全てが登録されます。
- 1行に複数区間を書いてもOKです。
- 「連続不可」「アンコ不可」などの希望は時間帯の後ろに記載してください。
- シフトを削除したい場合は、スラッシュコマンド `/shift delete` をご利用ください。
- 登録内容は [Google Sheets]({}) で確認・閲覧できます。
"""

    help_text_zh_tw = """## 📋 班表登記格式說明


訊息中只要有 `開始-結束`（24小時制，例如 `15-18`）的格式，無論一行有幾個區間、前後有無備註，Bot 都會自動登記。

範例：
```
15-18 18-20 連續不可
20-22
16-17 安可不可 19-21
```
上述所有 `15-18`、`18-20`、`20-22`、`16-17`、`19-21` 都會被自動登記。
- 一行可輸入多個區間，也可加上備註文字。
- 有特殊需求（如「連續不可」「安可不可」）請寫在時段後方。
- 若要刪除自己的班表，請輸入 `/shift delete`。
- 登記後結果會顯示在 [Google Sheets]({}) ，提供查看與確認。
"""


async def setup(bot: Rhoboto) -> None:
    await bot.add_cog(ShiftRegister(bot))
