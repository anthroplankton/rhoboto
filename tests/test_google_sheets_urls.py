import pytest

from utils import google_sheets_urls as google_sheets_urls_module
from utils.google_sheets_urls import (
    google_sheet_url_with_gid,
    normalize_google_sheet_url,
)


def test_extract_google_sheet_id_uses_canonical_url_path() -> None:
    sheet_url = (
        "https://docs.google.com/spreadsheets/d/abcDEF_123-xyz/edit?usp=sharing#gid=42"
    )

    result = google_sheets_urls_module.extract_google_sheet_id(sheet_url)

    assert result == "abcDEF_123-xyz"


@pytest.mark.parametrize(
    "sheet_url",
    [
        "http://docs.google.com/spreadsheets/d/abc/edit",
        "https://example.com/spreadsheets/d/abc/edit",
        "https://docs.google.com.evil.example/spreadsheets/d/abc/edit",
        "https://docs.google.com/document/d/abc/edit",
        "https://docs.google.com/spreadsheets/d/",
        "https://docs.google.com/spreadsheets/d/abc%2Fdef/edit",
    ],
)
def test_extract_google_sheet_id_rejects_non_google_sheet_urls(
    sheet_url: str,
) -> None:
    with pytest.raises(ValueError, match="Google Sheet URL"):
        google_sheets_urls_module.extract_google_sheet_id(sheet_url)


def test_normalize_google_sheet_url_removes_gid_query_and_fragment() -> None:
    sheet_url = "https://docs.google.com/spreadsheets/d/abc/edit?gid=111#gid=222"

    result = normalize_google_sheet_url(sheet_url)

    assert result == "https://docs.google.com/spreadsheets/d/abc/edit"


def test_normalize_google_sheet_url_removes_sharing_query() -> None:
    sheet_url = "https://docs.google.com/spreadsheets/d/abc/edit?usp=sharing"

    result = normalize_google_sheet_url(sheet_url)

    assert result == "https://docs.google.com/spreadsheets/d/abc/edit"


def test_google_sheet_url_with_gid_composes_query_and_fragment() -> None:
    sheet_url = "https://docs.google.com/spreadsheets/d/abc/edit?usp=sharing#gid=111"

    result = google_sheet_url_with_gid(sheet_url, 222)

    assert result == "https://docs.google.com/spreadsheets/d/abc/edit?gid=222#gid=222"


def test_google_sheet_url_with_gid_returns_base_url_without_worksheet_id() -> None:
    sheet_url = "https://docs.google.com/spreadsheets/d/abc/edit?usp=sharing#gid=111"

    result = google_sheet_url_with_gid(sheet_url, None)

    assert result == "https://docs.google.com/spreadsheets/d/abc/edit"
