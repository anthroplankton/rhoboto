from __future__ import annotations

import re
from urllib.parse import urlparse, urlunparse


def extract_google_sheet_id(sheet_url: str) -> str:
    """Return the spreadsheet ID from a canonical Google Sheets URL."""
    parsed = urlparse(sheet_url)
    match = re.fullmatch(
        r"/spreadsheets/d/([A-Za-z0-9_-]+)(?:/.*)?",
        parsed.path,
    )
    if parsed.scheme != "https" or parsed.netloc != "docs.google.com" or match is None:
        msg = "Invalid Google Sheet URL."
        raise ValueError(msg)
    return match.group(1)


def normalize_google_sheet_url(sheet_url: str) -> str:
    """Return a spreadsheet URL without query parameters or fragments."""
    parsed = urlparse(sheet_url)
    return urlunparse(parsed._replace(query="", fragment=""))


def google_sheet_url_with_gid(sheet_url: str, worksheet_id: int | None) -> str:
    """Return a normalized spreadsheet URL that opens the given worksheet."""
    normalized = normalize_google_sheet_url(sheet_url)
    if worksheet_id is None:
        return normalized
    parsed = urlparse(normalized)
    gid = f"gid={worksheet_id}"
    return urlunparse(parsed._replace(query=gid, fragment=gid))
