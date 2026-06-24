from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace


class FakeDiscordResponse:
    def __init__(self) -> None:
        self.deferred: list[bool] = []
        self.messages: list[tuple[str | None, dict[str, object]]] = []

    async def defer(self, *, ephemeral: bool = False) -> None:
        self.deferred.append(ephemeral)

    async def send_message(self, content: str | None = None, **kwargs: object) -> None:
        self.messages.append((content, kwargs))


class FakeDiscordFollowup:
    def __init__(self) -> None:
        self.messages: list[tuple[str | None, dict[str, object]]] = []

    async def send(self, content: str | None = None, **kwargs: object) -> None:
        self.messages.append((content, kwargs))


class FakeInteraction:
    def __init__(self, *, locale: str = "en-US") -> None:
        self.channel = SimpleNamespace(id=222)
        self.guild = SimpleNamespace(id=111)
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
