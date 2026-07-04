from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from jinja2 import (
    Environment,
    FileSystemLoader,
    StrictUndefined,
    TemplateNotFound,
    select_autoescape,
)

TEMPLATE_ROOT = Path(__file__).resolve().parents[1] / "resources" / "messages"


class MessageTemplateNotFoundError(FileNotFoundError):
    def __init__(self, key: str, locale: str, path: Path | None) -> None:
        self.key = key
        self.locale = locale
        self.path = path
        msg = f"Message template `{key}` for locale `{locale}` was not found."
        super().__init__(msg)


def locale_to_template_code(locale: str) -> str:
    """Map a Discord locale value to a message template locale code."""
    if locale.startswith("zh"):
        return "zh_tw"
    if locale.startswith("ja"):
        return "ja"
    return "en"


def get_message_template_name(key: str, locale: str) -> str:
    """Return the loader-relative template name for a key and locale code."""
    return Path(*key.split(".")).with_suffix(f".{locale}.md").as_posix()


def get_message_template_path(key: str, locale: str) -> Path:
    """Return the Markdown template path for a key and locale code."""
    return TEMPLATE_ROOT / get_message_template_name(key, locale)


@lru_cache
def get_template_environment() -> Environment:
    """Return the Jinja environment for Discord message templates."""
    return Environment(
        loader=FileSystemLoader(TEMPLATE_ROOT),
        undefined=StrictUndefined,
        autoescape=select_autoescape(
            enabled_extensions=("html", "htm", "xml"),
            disabled_extensions=("md",),
            default_for_string=False,
            default=False,
        ),
        keep_trailing_newline=True,
    )


@lru_cache
def load_message_template(key: str, locale: str) -> str:
    """Load a message template from resources/messages."""
    path = get_message_template_path(key, locale)
    if not path.exists():
        raise MessageTemplateNotFoundError(key, locale, path)
    return path.read_text(encoding="utf-8")


def render_message_template(key: str, locale: str, **values: object) -> str:
    """Render a message template with Jinja placeholders."""
    try:
        template = get_template_environment().get_template(
            get_message_template_name(key, locale)
        )
    except TemplateNotFound as exc:
        raise MessageTemplateNotFoundError(
            key,
            locale,
            get_message_template_path(key, locale),
        ) from exc
    return template.render(**values)
