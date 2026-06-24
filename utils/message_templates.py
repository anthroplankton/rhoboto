from __future__ import annotations

from functools import lru_cache
from pathlib import Path

TEMPLATE_ROOT = Path(__file__).resolve().parents[1] / "resources" / "messages"


def locale_to_template_code(locale: str) -> str:
    """Map a Discord locale value to a message template locale code."""
    if locale.startswith("zh"):
        return "zh_tw"
    if locale.startswith("ja"):
        return "ja"
    return "en"


@lru_cache
def load_message_template(key: str, locale: str) -> str:
    """Load a message template from resources/messages."""
    path = TEMPLATE_ROOT.joinpath(*key.split(".")).with_suffix(f".{locale}.md")
    return path.read_text(encoding="utf-8")


def render_message_template(key: str, locale: str, **values: object) -> str:
    """Render a message template with Python format placeholders."""
    return load_message_template(key, locale).format(**values)
