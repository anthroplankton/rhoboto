from __future__ import annotations

import calendar
from typing import TYPE_CHECKING, override

from discord import app_commands

from bot import config
from cogs.base.feature_channel_base import FeatureChannelBase
from components.ui_shift_register import (
    ShiftRegisterView,
    build_current_settings_embed,
)
from models.feature_channel import FeatureChannel
from utils.key_async_lock import KeyAsyncLock
from utils.shift_register_manager import ShiftRegisterManager
from utils.shift_register_structs import Period, Shift, ShiftParser
from utils.structs_base import UserInfo

if TYPE_CHECKING:
    from discord import Interaction, Message

    from bot import Rhoboto


class ShiftRegister(
    FeatureChannelBase[ShiftRegisterManager, Shift | list[Period]],
    group_name="shift_register",
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
    async def process_upsert_from_message(
        self, message: Message
    ) -> Shift | list[Period] | None:
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
        shift, periods = ShiftParser.parse_lines(
            user_info, message.content.splitlines()
        )
        if not periods:
            return None

        self.logger.info(
            "Parsed shift in Guild: `%s` Channel: `%s` (Feature: `%s`): `%s` (%r)",
            message.guild.id,
            message.channel.id,
            self.feature_name,
            message.author.display_name,
            shift,
        )

        if not shift:
            await message.add_reaction(config.CONFUSED_EMOJI)
            return periods

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

    @app_commands.command(
        name="settings",
        description="Show and edit current feature settings for this channel.",
    )
    @app_commands.check(
        FeatureChannelBase.feature_enabled_app_command_predicate(feature_name)
    )
    async def settings(self, interaction: Interaction) -> None:
        """Slash command to show and edit current feature settings."""
        await interaction.response.defer(ephemeral=True)
        await self.setup_after_enable(interaction)

    @app_commands.command(
        name="info",
    )
    @app_commands.check(
        FeatureChannelBase.feature_enabled_app_command_predicate(feature_name)
    )
    async def info(
        self,
        interaction: Interaction,
        day_number: int,
        month: int,
        day: int,
        deadline_hour: int,
        draft_hour: int,
        final_hour: int,
    ) -> None:
        month_name = calendar.month_name[month]
        info_text = {
            "en": self.info_text_en,
            "zh_tw": self.info_text_zh_tw,
            "ja": self.info_text_ja,
        }
        await interaction.response.send_message(
            "\n".join(
                text.format(
                    day_number=day_number,
                    month_name=month_name,
                    month=month,
                    day=day,
                    deadline_hour=deadline_hour,
                    draft_hour=draft_hour,
                    final_hour=final_hour,
                )
                for text in info_text.values()
            ),
            ephemeral=False,
        )

    info_text_en = """🐧 **Day {day_number} ({month_name} {day}) Shift Registration Info** 🐧

Shift Entry Time Slot: 【4-28 (JST)】
- We don't have standby slots.
- If you have requests such as "no consecutive shifts," "no skipping," or "no encore," please include them together. (If you do not specify "up to X consecutive hours," all submitted time slots may be adopted. Please be aware.)
- After the entry deadline, automatic processing will stop. If you wish to make changes, or if you haven’t submitted your shift yet, please feel free to mention me in the channel before the shift for the day starts. Additional submissions are always welcome.

Entry Deadline ⇒ {day}th, {deadline_hour}:00 (JST)
Draft Shift ⇒ {day}th, {draft_hour}:00 (JST)
Final Shift ⇒ {day}th, {final_hour}:00 (JST)
"""

    info_text_ja = """🐧 **{day_number}日目（{month}月{day}日）シフト登録のお知らせ** 🐧

募集時間帯【4-28 (JST)】
- 待機枠は設けません。
- 連続、飛び、アンコ不可などの要望がありましたら併せてご記入ください（「連続〇時間まで」の記載がない場合、提出していただいた時間全てを採用させていただく場合がございます。ご注意ください。）
- 募集〆切後は自動処理を停止いたします。当日シフトが始まる前まで、修正をご希望の場合や、まだ提出されていない方も、どうぞご遠慮なくチャンネルで私にメンションしてご連絡ください。追加提出も歓迎いたします。

募集〆 ⇒ {day}日{deadline_hour}時 (JST)
仮シフト ⇒ {day}日{draft_hour}時 (JST)
確定シフト ⇒ {day}日{final_hour}時 (JST)
"""

    info_text_zh_tw = """- 不設待機時段
- 如果「不可連續、跳班、安可」，請一併填寫（若未註明「連續〇小時為限」，則提交的所有時段可能都會採用，請特別注意）
- 募集截止後將停止自動解析。在當日班表開始前，如需修改請在頻道 tag 我訊息，想要再提出班表也沒問題。
"""

    @app_commands.command(
        name="help",
        description="Show the all language how to register your data for this feature.",
    )
    @app_commands.check(
        FeatureChannelBase.feature_enabled_app_command_predicate(feature_name)
    )
    async def help(self, interaction: Interaction) -> None:
        await self._help_callback(interaction)

    help_text_en = """### 📋 How to Register Your Shifts

You can enter one or more time ranges anywhere in your message. The format is `start-end` (24-hour, e.g. `15-18`).
You may add notes before or after the time ranges; the {bot} will extract all valid ranges from your message.

**Examples:**
```
15-18 18-20 consecutive not allowed
20-22
16-17 encore not allowed 19-21
```
All `start-end` patterns (e.g. `15-18`, `18-20`, `20-22`, `16-17`, `19-21`) will be registered as your shifts, regardless of line breaks or notes.
- You can write multiple ranges in one line or across several lines.
- Add any special requests (e.g. "consecutive not allowed", "encore not allowed") after the time range.
- All shift times are recognized in **Japan Standard Time (JST)**.

To delete your shift registration, use the slash command: `/shift delete`.
To update, simply submit again; your previous shifts will be completely overwritten.

After registration, {bot} will automatically process your shifts and record the results in [Google Sheets]({sheet_url}) for you to view and confirm.
"""

    help_text_ja = """## 📋 シフト登録の使い方

メッセージ内のどこに書いても、`開始-終了`（30時間表記、例：`15-18`）の形式で書かれた全ての時間帯が登録されます。
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
- 登録時刻の認識はすべて**日本標準時（JST）**で行われます。

シフトを削除したい場合は、スラッシュコマンド `/シフト 削除` をご利用ください。
更新する場合は、もう一度提出してください。以前の班表はすべて上書きされます。

登録後、{bot}が自動で処理し、結果を [Google Sheets]({sheet_url}) に記録しますので、確認・閲覧できます。
"""

    help_text_zh_tw = """## 📋 班表登記格式說明


訊息中只要有 `開始-結束`（30小時制，例如 `15-18`）的格式，無論一行有幾個區間、前後有無備註，{bot} 都會自動登記。

範例：
```
15-18 18-20 不可連續
20-22
16-17 不可安可 19-21
```
上述所有 `15-18`、`18-20`、`20-22`、`16-17`、`19-21` 都會被自動登記。
- 一行可輸入多個區間，也可加上備註文字。
- 有特殊需求（如「不可連續」「不可安可」）請寫在時段後方。
- 登記時段的解析統一為**日本標準時區 (JST)**。

若要刪除自己的班表，請使用 slash command `/班表 刪除`。
更新時，請直接重新提交即可，會完全覆蓋舊的班表。

登記後，{bot} 會自動處理並將結果記錄在 [Google Sheets]({sheet_url})，提供查看與確認。
"""


async def setup(bot: Rhoboto) -> None:
    await bot.add_cog(ShiftRegister(bot))
