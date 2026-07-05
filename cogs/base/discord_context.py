from __future__ import annotations

from typing import Protocol, TypeVar, cast

from discord.ext import commands


class ChannelWithId(Protocol):
    id: int


class GuildWithId(Protocol):
    id: int


TGuild = TypeVar("TGuild", bound=GuildWithId)
TChannel = TypeVar("TChannel", bound=ChannelWithId)


class MaybeGuildSource(Protocol[TGuild]):
    @property
    def guild(self) -> TGuild | None: ...


class GuildSource(Protocol[TGuild]):
    @property
    def guild(self) -> TGuild: ...


class MaybeGuildChannelSource(
    MaybeGuildSource[TGuild],
    Protocol[TGuild, TChannel],
):
    @property
    def channel(self) -> TChannel | None: ...


class GuildChannelSource(GuildSource[TGuild], Protocol[TGuild, TChannel]):
    @property
    def channel(self) -> TChannel: ...


def _source_label(source: object) -> str:
    if isinstance(source, commands.Context):
        return "Context"

    class_name = source.__class__.__name__
    if class_name.endswith("Interaction"):
        return "Interaction"
    if class_name.endswith("Context"):
        return "Context"
    return class_name


def require_guild_source[TSourceGuild: GuildWithId](
    source: MaybeGuildSource[TSourceGuild],
    *,
    action: str,
) -> GuildSource[TSourceGuild]:
    if source.guild is None:
        msg = f"{_source_label(source)} guild is None. Cannot {action}."
        raise ValueError(msg)

    return cast("GuildSource[TSourceGuild]", source)


def require_guild_channel_source[
    TSourceGuild: GuildWithId,
    TSourceChannel: ChannelWithId,
](
    source: MaybeGuildChannelSource[TSourceGuild, TSourceChannel],
    *,
    action: str,
) -> GuildChannelSource[TSourceGuild, TSourceChannel]:
    if source.guild is None or source.channel is None:
        msg = f"{_source_label(source)} guild or channel is None. Cannot {action}."
        raise ValueError(msg)

    return cast("GuildChannelSource[TSourceGuild, TSourceChannel]", source)
