from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, override

from discord import Interaction, Member, Message, app_commands

from bot import config
from cogs.base.feature_channel_base import FeatureChannelBase
from components.ui_team_register import (
    TeamRegisterView,
    build_current_settings_embed,
    build_summary_embed,
)
from models.feature_channel import FeatureChannel
from utils.key_async_lock import KeyAsyncLock
from utils.structs_base import UserInfo
from utils.team_register_manager import TeamRegisterManager
from utils.team_register_structs import ClassifiedTeams, TeamParser

if TYPE_CHECKING:
    from bot import Rhoboto


class TeamRegister(
    FeatureChannelBase[TeamRegisterManager, ClassifiedTeams], group_name="team_register"
):

    feature_name = "team_register"
    lock = KeyAsyncLock()

    ManagerType = TeamRegisterManager

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

        manager = TeamRegisterManager(
            feature_channel, config.GOOGLE_SERVICE_ACCOUNT_PATH
        )

        team_register_config = await manager.get_sheet_config_or_none()
        if team_register_config is None:
            content = (
                "Team Register is not yet configured for this channel. "
                "Click below to set up."
            )
            embed = None
            view = TeamRegisterView(team_register_manager=manager)
        else:
            metadata = await manager.fetch_google_sheets_metadata()
            roles = list(interaction.guild.roles) if interaction.guild else []
            encore_role_ids = team_register_config.encore_role_ids
            embed = build_current_settings_embed(
                sheet_url=team_register_config.sheet_url,
                metadata=metadata,
                encore_role_ids=encore_role_ids,
                color=config.DEFAULT_EMBED_COLOR,
            )
            view = TeamRegisterView(
                team_register_manager=manager,
                has_existing_settings=True,
                sheet_url=team_register_config.sheet_url,
                team_worksheet_titles=[ws.title for ws in metadata.team_worksheets],
                summary_worksheet_title=metadata.summary_worksheet.title,
                roles=roles,
                encore_role_ids=encore_role_ids,
            )

        if embed is None:
            await interaction.followup.send(content=content, view=view, ephemeral=True)
        else:
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    @override
    async def process_upsert_from_message(
        self, message: Message
    ) -> ClassifiedTeams | None:
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
        teams = TeamParser.parse_lines(user_info, lines=message.content.splitlines())
        if not teams:
            return None

        self.logger.info(
            "Parsed teams in Guild: `%s` Channel: `%s` (Feature: `%s`): `%s` (%s)",
            message.guild.id,
            message.channel.id,
            self.feature_name,
            message.author.display_name,
            ", ".join(
                f"{t.leader_skill_value}/{t.internal_skill_value}/{t.team_power}"
                for t in teams
            ),
        )

        feature_channel = await FeatureChannel.get_or_none(
            guild_id=message.guild.id,
            channel_id=message.channel.id,
            feature_name=self.feature_name,
        )
        if not feature_channel:
            return None

        manager = TeamRegisterManager(
            feature_channel, config.GOOGLE_SERVICE_ACCOUNT_PATH
        )

        team_register_config = await manager.get_sheet_config_or_none()
        if team_register_config is None:
            return None

        if self.bot.user is not None:
            await message.add_reaction(config.PROCESSING_EMOJI)

        classified_teams = TeamParser.classify_teams(teams)
        team_tuple = classified_teams.as_tuple()

        async with self.lock(message.channel.id):
            metadata = await manager.fetch_google_sheets_metadata()
            manager.log_missing_worksheet_warnings(metadata)

            metadata = await manager.ensure_worksheets_and_upsert_sheet_config(
                metadata, count=len(team_tuple)
            )

            await asyncio.gather(
                manager.upsert_user_teams(user_info, *team_tuple, metadata=metadata),
                manager.upsert_user_summary(
                    user_info,
                    message.author.roles if isinstance(message.author, Member) else [],
                    *team_tuple,
                    metadata=metadata,
                ),
            )

        if self.bot.user is not None:
            await message.remove_reaction("⌛", self.bot.user)
            await message.remove_reaction(config.PROCESSING_EMOJI, self.bot.user)
            await message.add_reaction("✅")

        return classified_teams

    @app_commands.command(
        name="summary",
        description=(
            "Show and refresh team summary with effective value, user info, and "
            "roles of encore type."
        ),
    )
    @app_commands.check(
        FeatureChannelBase.feature_enabled_app_command_predicate(feature_name)
    )
    async def summary(self, interaction: Interaction) -> None:
        if interaction.channel is None or interaction.guild is None:
            msg = (
                "Interaction channel or guild is None. "
                "Cannot proceed with setup message."
            )
            raise ValueError(msg)

        await interaction.response.defer(ephemeral=True)

        guild_id = interaction.guild.id
        channel_id = interaction.channel.id
        feature_channel = await FeatureChannel.get(
            guild_id=guild_id,
            channel_id=channel_id,
            feature_name=self.feature_name,
        )

        manager = TeamRegisterManager(
            feature_channel, config.GOOGLE_SERVICE_ACCOUNT_PATH
        )

        team_register_config = await manager.get_sheet_config_or_none()
        if team_register_config is None:
            await interaction.followup.send(
                content="Team Register is not configured for this channel.",
                ephemeral=True,
            )
            return

        async with self.lock(interaction.channel.id):
            metadata = await manager.fetch_google_sheets_metadata()
            manager.log_missing_worksheet_warnings(metadata)

            metadata = await manager.ensure_worksheets_and_upsert_sheet_config(
                metadata,
                count=0,  # No teams to process, just refresh summary
            )

            summary_df = await manager.refresh_summary_worksheet(
                metadata, member_by_names={m.name: m for m in interaction.guild.members}
            )

        if summary_df is None:
            await interaction.followup.send(
                content="No summary worksheet found or no data to display.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(embed=build_summary_embed(summary_df))

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
        name="help",
        description="Show the all language how to register your data for this feature.",
    )
    @app_commands.check(
        FeatureChannelBase.feature_enabled_app_command_predicate(feature_name)
    )
    async def help(self, interaction: Interaction) -> None:
        await self._help_callback(interaction)

    help_text_en = """### 📋 How to Register Your Teams

Each line represents a team. The format is `LeaderSkill/InternalSkill/TeamPower`, and you may add notes at the end of each line.

Example:
```
150/740/33.4 This is the main team
140/680/35.3 No HP check
150/700/39 Encore, any other notes
```
Order does not matter. {bot} will automatically determine:
- The team with the highest effective skill value is the "Main Team"
- Among the rest, the one with the highest power (not less than the main team) is the "Encore Team"
- Others are "Backup Teams"
- As long as a line contains the format `xxx/xxx/xx.x`, it will be recognized, so adding labels at the beginning of the line is also fine.

To delete your team data, please use the slash command: `/team delete`.
To update, simply submit again; your previous team registrations will be removed or completely overwritten.
Japanese:

After registration, {bot} will automatically process your teams and record the results in [Google Sheets]({sheet_url}) for you to view and confirm.
"""

    help_text_ja = """## 📋 編成入力の使い方

1行ごとに1つの編成を入力します。フォーマットは `リーダースキル/内部スキル/編成戦力` で、行末に備考を追加しても構いません。

例：
```
150/740/33.4 これは内部編成
140/680/35.3 HP判定なし
150/700/39 アンコール、その他備考
```
順番は問いません。{bot}が自動で判定します：
- 実効値が最も高い編成が「内部編成」となります
- 残りの中で総合が内部編成以上かつ最大のものが「アンコ編成」となります
- その他は「その他編成」となります
- 1行に `xxx/xxx/xx.x` の形式が含まれていれば認識されるため、行頭にラベルを付けても問題ありません。

編成を削除したい場合は、スラッシュコマンド `/編成 削除` をご利用ください。
更新する場合は、再度入力するだけで、以前の編成データは削除されるか、すべて完全に上書きされます

登録後、{bot}が編成データを自動で処理し、結果を [Google Sheets]({sheet_url}) に記録しますので、確認・閲覧できます。
"""

    help_text_zh_tw = """## 📋 隊伍登記格式說明

每行對應一個編成，格式為 `隊長技能/內部技能/隊伍戰力`，行尾可加備註。

範例：
```
150/740/33.4 這是內部編成
140/680/35.3 無血量判定
150/700/39 安可，其他任意備註
```
順序不拘，{bot} 會自動判斷：
- 實效值最高為「內部編成」
- 其餘中綜合力最大且不小於內部編成的為「安可編成」
- 其餘為「其他編成」
- 只要一行當中包含 `xxx/xxx/xx.x` 的格式就會被識別，因此添加標籤在行頭也沒問題。

如需刪除隊伍編成，請輸入 slash command: `/編成 刪除`。
更新時，請直接重新提交即可，舊的隊伍編成會消除或所有的都完全覆蓋。


登記後，{bot} 會自動處理並將結果記錄在 [Google Sheets]({sheet_url}) ，提供查看與確認。
"""


async def setup(bot: Rhoboto) -> None:
    await bot.add_cog(TeamRegister(bot))
