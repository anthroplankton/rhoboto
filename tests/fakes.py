from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pandas as pd

MISSING_CONTENT: object = object()


class FakeDiscordResponse:
    def __init__(self) -> None:
        self.deferred: list[bool] = []
        self.messages: list[tuple[str | None, dict[str, object]]] = []
        self.modals: list[object] = []
        self.edits: list[tuple[object, dict[str, object]]] = []

    async def defer(self, *, ephemeral: bool = False) -> None:
        self.deferred.append(ephemeral)

    async def send_message(self, content: str | None = None, **kwargs: object) -> None:
        self.messages.append((content, kwargs))

    async def send_modal(self, modal: object) -> None:
        self.modals.append(modal)

    async def edit_message(
        self,
        content: object = MISSING_CONTENT,
        **kwargs: object,
    ) -> None:
        self.edits.append((content, kwargs))


class FakeDiscordFollowup:
    def __init__(self) -> None:
        self.messages: list[tuple[str | None, dict[str, object]]] = []

    async def send(self, content: str | None = None, **kwargs: object) -> None:
        self.messages.append((content, kwargs))


@dataclass(frozen=True)
class FakePermissions:
    administrator: bool = True
    manage_channels: bool = True


class FakeGuild:
    def __init__(
        self, *, guild_id: int = 111, roles: list[object] | None = None
    ) -> None:
        self.id = guild_id
        self.roles = roles or []

    def get_role(self, role_id: int) -> object | None:
        return next((role for role in self.roles if role.id == role_id), None)


class FakeInteraction:
    def __init__(
        self,
        *,
        locale: str = "en-US",
        administrator: bool = True,
        manage_channels: bool = True,
        guild: object | None = None,
        roles: list[object] | None = None,
    ) -> None:
        self.channel = SimpleNamespace(id=222)
        self.guild = guild if guild is not None else FakeGuild(roles=roles)
        self.user = SimpleNamespace(
            name="alice",
            display_name="Alice",
            guild_permissions=FakePermissions(
                administrator=administrator,
                manage_channels=manage_channels,
            ),
        )
        self.locale = SimpleNamespace(value=locale)
        self.response = FakeDiscordResponse()
        self.followup = FakeDiscordFollowup()


class ConfiguredManager:
    def __init__(self, feature_channel: object, service_account_path: str) -> None:
        self.feature_channel = feature_channel
        self.service_account_path = service_account_path

    async def get_sheet_config_or_none(self) -> SimpleNamespace:
        return SimpleNamespace(sheet_url="https://sheet.example")


class MissingConfigManager(ConfiguredManager):
    async def get_sheet_config_or_none(self) -> None:
        return None


@dataclass(frozen=True)
class FakeRole:
    id: int
    name: str
    position: int
    managed: bool = False
    default: bool = False

    def is_default(self) -> bool:
        return self.default


class FakeWorksheet:
    def __init__(
        self,
        *,
        title: str = "Worksheet",
        worksheet_id: int = 1,
        frame: pd.DataFrame | None = None,
    ) -> None:
        self.title = title
        self.id = worksheet_id
        self.frame = frame.copy() if frame is not None else pd.DataFrame()
        self.updated_frames: list[pd.DataFrame] = []

    async def to_frame(self) -> pd.DataFrame:
        return self.frame.copy()

    async def update_from_dataframe(self, dataframe: pd.DataFrame) -> None:
        updated = dataframe.copy()
        self.updated_frames.append(updated)
        self.frame = updated
