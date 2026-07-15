from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from enum import Enum, auto
from typing import TYPE_CHECKING, Protocol, override

from discord import Interaction, Message, app_commands
from discord.ext import commands

from bot import config
from cogs.base.discord_context import require_guild_channel_source
from cogs.base.feature_channel_base import FeatureChannelBase
from utils.reactions import add_reaction_if_possible
from utils.storage_errors import classify_storage_exception, generate_error_reference
from utils.structs_base import SubmissionParseResult, UserInfo

if TYPE_CHECKING:
    from bot import Rhoboto
    from cogs.base.discord_context import GuildChannelSource
    from models.feature_channel import FeatureChannel


INTERNAL_FAILURE_REACTIONS = (config.WARNING_EMOJI, "🚧")


class MessageFeatureChannelContext(Protocol):
    """Smallest context capability required by message orchestration."""

    guild_id: int
    channel_id: int


class MessageSubmissionParser[SubmissionT](Protocol):
    """Parser contract shared by message-driven feature channels."""

    @classmethod
    def parse_submission(
        cls,
        user_info: UserInfo,
        lines: list[str],
    ) -> SubmissionParseResult[SubmissionT]: ...


class MessageParseStatus(Enum):
    IGNORED = auto()
    INVALID = auto()
    PARSED = auto()


@dataclass(frozen=True)
class MessageParseResult[SubmissionT]:
    status: MessageParseStatus
    submission: SubmissionT | None
    user_info: UserInfo | None

    @classmethod
    def ignored(cls) -> MessageParseResult[SubmissionT]:
        return cls(
            status=MessageParseStatus.IGNORED,
            submission=None,
            user_info=None,
        )

    @classmethod
    def invalid(
        cls,
        *,
        user_info: UserInfo,
    ) -> MessageParseResult[SubmissionT]:
        return cls(
            status=MessageParseStatus.INVALID,
            submission=None,
            user_info=user_info,
        )

    @classmethod
    def parsed(
        cls,
        submission: SubmissionT,
        *,
        user_info: UserInfo,
    ) -> MessageParseResult[SubmissionT]:
        return cls(
            status=MessageParseStatus.PARSED,
            submission=submission,
            user_info=user_info,
        )


class MessageUpsertStatus(Enum):
    IGNORED = auto()
    INVALID = auto()
    MISSING_CONFIG = auto()
    PROCESSED = auto()


@dataclass(frozen=True)
class MessageUpsertOutcome[UpsertResultT]:
    status: MessageUpsertStatus
    result: UpsertResultT | None = None

    @classmethod
    def ignored(cls) -> MessageUpsertOutcome[UpsertResultT]:
        return cls(status=MessageUpsertStatus.IGNORED)

    @classmethod
    def invalid(cls) -> MessageUpsertOutcome[UpsertResultT]:
        return cls(status=MessageUpsertStatus.INVALID)

    @classmethod
    def missing_config(cls) -> MessageUpsertOutcome[UpsertResultT]:
        return cls(status=MessageUpsertStatus.MISSING_CONFIG)

    @classmethod
    def processed(
        cls,
        result: UpsertResultT | None,
    ) -> MessageUpsertOutcome[UpsertResultT]:
        return cls(status=MessageUpsertStatus.PROCESSED, result=result)


class MessageUpsertFeatureChannelBase[
    MessageContextT: MessageFeatureChannelContext,
    ConfiguredContextT,
    SubmissionT,
    UpsertResultT,
](FeatureChannelBase):
    """Shared message parsing, gating, and typed upsert orchestration."""

    ParserType: type[MessageSubmissionParser[SubmissionT]]
    context_menu_name: str | None = None

    @override
    def __init__(self, bot: Rhoboto) -> None:
        super().__init__(bot)
        self.context_menu = app_commands.ContextMenu(
            name=self.context_menu_name or f"{self.feature_display_name} Upsert",
            callback=self.upsert_from_content_menu,
        )
        self.context_menu.add_check(
            self.feature_enabled_app_command_predicate(
                self.feature_name,
                self.feature_display_name,
            )
        )
        self.context_menu.error(self.cog_app_command_error)
        bot.tree.add_command(self.context_menu)

    @abstractmethod
    def _build_message_context(
        self,
        membership: FeatureChannel,
    ) -> MessageContextT: ...

    @abstractmethod
    async def _get_configured_message_context(
        self,
        context: MessageContextT,
    ) -> ConfiguredContextT | None: ...

    @abstractmethod
    async def _process_configured_message_submission(
        self,
        message: Message,
        context: ConfiguredContextT,
        submission: SubmissionT,
        user_info: UserInfo,
    ) -> UpsertResultT | None: ...

    @abstractmethod
    async def _process_enabled_message(
        self,
        message: Message,
        context: MessageContextT,
    ) -> None: ...

    @abstractmethod
    async def _process_context_menu_message(
        self,
        interaction: Interaction,
        message: Message,
        source: GuildChannelSource,
    ) -> None: ...

    async def _process_feature_channel_message_with_outcome(
        self,
        message: Message,
        context: MessageContextT,
    ) -> MessageUpsertOutcome[UpsertResultT]:
        self._log_received_message(message)

        parse_result = self._parse_message_submission(message)
        if parse_result.status is MessageParseStatus.IGNORED:
            return MessageUpsertOutcome.ignored()

        configured_context = await self._get_configured_message_context(context)
        if configured_context is None:
            self.logger.debug(
                "Feature `%s` in Guild: `%s` Channel: `%s` has no feature config; "
                "ignoring parsed message.",
                self.feature_name,
                context.guild_id,
                context.channel_id,
            )
            return MessageUpsertOutcome.missing_config()

        if parse_result.status is MessageParseStatus.INVALID:
            await self._add_invalid_message_reactions(message)
            return MessageUpsertOutcome.invalid()

        if parse_result.submission is None or parse_result.user_info is None:
            message_text = "Parsed message result is missing submission or user info."
            raise ValueError(message_text)

        result = await self._process_configured_message_submission(
            message,
            configured_context,
            parse_result.submission,
            parse_result.user_info,
        )
        return MessageUpsertOutcome.processed(result)

    async def _get_message_feature_channel_context_or_none(
        self,
        message: Message,
    ) -> MessageContextT | None:
        if message.author.bot or message.guild is None or message.channel is None:
            return None
        membership = await self._get_enabled_feature_channel_or_none(
            message.guild.id,
            message.channel.id,
            self.feature_name,
        )
        if membership is None:
            return None
        return self._build_message_context(membership)

    def _parse_message_submission(
        self,
        message: Message,
    ) -> MessageParseResult[SubmissionT]:
        user_info = self._message_user_info(message)
        parse_result = self.ParserType.parse_submission(
            user_info,
            message.content.splitlines(),
        )
        if parse_result.invalid_attempts:
            return MessageParseResult.invalid(user_info=user_info)
        if parse_result.submission is None:
            return MessageParseResult.ignored()
        return MessageParseResult.parsed(
            parse_result.submission,
            user_info=user_info,
        )

    @staticmethod
    def _message_user_info(message: Message) -> UserInfo:
        return UserInfo(
            username=message.author.name,
            display_name=message.author.display_name,
        )

    def _log_received_message(self, message: Message) -> None:
        if message.guild is None or message.channel is None:
            return
        self.logger.debug(
            (
                "Received feature message. operation=message_receive feature=%s "
                "guild=%s channel=%s message=%s lines=%s characters=%s"
            ),
            self.feature_name,
            message.guild.id,
            message.channel.id,
            message.id,
            len(message.content.splitlines()),
            len(message.content),
        )

    async def _add_invalid_message_reactions(self, message: Message) -> None:
        await add_reaction_if_possible(
            message,
            config.WARNING_EMOJI,
            log=self.logger,
        )
        await add_reaction_if_possible(
            message,
            config.CONFUSED_EMOJI,
            log=self.logger,
        )

    @app_commands.default_permissions(administrator=True, manage_channels=True)
    async def upsert_from_content_menu(
        self,
        interaction: Interaction,
        message: Message,
    ) -> None:
        """Upsert feature data from a selected Discord message."""
        source = require_guild_channel_source(
            interaction,
            action="upsert feature data from context menu",
        )
        await interaction.response.defer(ephemeral=True)
        await self._process_context_menu_message(interaction, message, source)

    @commands.Cog.listener()
    async def on_message(self, message: Message) -> None:
        """Gate enabled feature messages before feature-specific processing."""
        if message.author.bot or message.guild is None or message.channel is None:
            return

        try:
            context = await self._get_message_feature_channel_context_or_none(message)
        except Exception as exc:
            error = classify_storage_exception(exc)
            if error is None:
                raise
            reference = generate_error_reference()
            self.logger.warning(
                (
                    "Message listener storage lookup failed before filtering. "
                    "reference=%s operation=%s feature=%s guild=%s channel=%s "
                    "message=%s kind=%s hint=%s"
                ),
                reference,
                "message_lookup",
                self.feature_name,
                message.guild.id,
                message.channel.id,
                message.id,
                error.kind.value,
                error.log_hint,
            )
            return
        if context is None:
            return
        await self._process_enabled_message(message, context)
