from pathlib import Path

import pytest

from server.scholar import (
    InvalidScholarIdError,
    extract_scholar_id,
    filter_and_deduplicate,
    parse_profile_page,
)

FIXTURE = Path(__file__).parent / "fixtures" / "profile.html"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("iAtNdR8AAAAJ", "iAtNdR8AAAAJ"),
        (
            "https://scholar.google.com/citations?hl=en&user=iAtNdR8AAAAJ",
            "iAtNdR8AAAAJ",
        ),
        (
            "https://scholar.google.com.au/citations?user=iAtNdR8AAAAJ",
            "iAtNdR8AAAAJ",
        ),
    ],
)
def test_extract_scholar_id(value: str, expected: str) -> None:
    assert extract_scholar_id(value) == expected


@pytest.mark.parametrize(
    "value",
    ["", "not an id", "https://example.com/?user=iAtNdR8AAAAJ"],
)
def test_rejects_invalid_scholar_id(value: str) -> None:
    with pytest.raises(InvalidScholarIdError):
        extract_scholar_id(value)


def test_parses_filters_and_deduplicates_profile() -> None:
    profile_name, publications = parse_profile_page(
        FIXTURE.read_text(encoding="utf-8"),
        "test123",
    )

    assert profile_name == "Test Researcher"
    assert len(publications) == 4
    assert publications[0].title == "First & Best Paper"

    filtered = filter_and_deduplicate(publications, 2025, 2026)

    assert len(filtered) == 1
    assert filtered[0].duplicate_count == 2
    assert filtered[0].authors == "A Author, B Author, C Author"
    assert filtered[0].venue == "Journal of Tests, 1(2), 3–10"
