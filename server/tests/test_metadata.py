from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from server.metadata import (
    WorkMetadata,
    candidate_score,
    parse_crossref_item,
    parse_datacite_item,
    parse_scholar_detail_page,
    prepare_publication,
)
from server.scholar import Publication

FIXTURE = Path(__file__).parent / "fixtures" / "publication_detail.html"
PUBLICATION = Publication(
    id="iAtNdR8AAAAJ:4DMP91E08xMC",
    title="Computational methods for improving the observability of platform-based advertising",
    year=2024,
    authors="D Angus, L Hayden, AK Obeid",
    venue="Journal of advertising 53 (5), 661-680, 2024",
    detail_url=(
        "https://scholar.google.com/citations?view_op=view_citation&"
        "user=iAtNdR8AAAAJ&citation_for_view=iAtNdR8AAAAJ:4DMP91E08xMC"
    ),
)

CROSSREF_ITEM = {
    "DOI": "10.1080/00913367.2024.2394156",
    "title": [
        "Computational Methods for Improving the Observability of Platform-Based Advertising"
    ],
    "author": [
        {"given": "Daniel", "family": "Angus"},
        {"given": "Lauren", "family": "Hayden"},
        {"given": "Abdul Karim", "family": "Obeid"},
    ],
    "published": {"date-parts": [[2024, 9, 11]]},
    "container-title": ["Journal of Advertising"],
    "publisher": "Informa UK Limited",
    "type": "journal-article",
    "URL": "https://doi.org/10.1080/00913367.2024.2394156",
    "volume": "53",
    "issue": "5",
    "page": "661-680",
}


def test_parse_scholar_detail_page() -> None:
    metadata = parse_scholar_detail_page(
        FIXTURE.read_text(encoding="utf-8"),
        PUBLICATION,
    )

    assert metadata.title == PUBLICATION.title
    assert metadata.authors == (
        "Daniel Angus",
        "Lauren Hayden",
        "Abdul Karim Obeid",
    )
    assert metadata.published_date == "2024-10-19"
    assert metadata.output_type == "Journal article"
    assert metadata.doi == "10.1080/00913367.2024.2394156"


def test_crossref_candidate_is_parsed_and_conservatively_matched() -> None:
    reference = parse_scholar_detail_page(
        FIXTURE.read_text(encoding="utf-8"),
        PUBLICATION,
    )
    candidate = parse_crossref_item(CROSSREF_ITEM)

    assert candidate.output_type == "Journal article"
    assert candidate.published_date == "2024-09-11"
    assert candidate_score(reference, candidate) is not None

    unrelated = WorkMetadata(
        source="crossref",
        title="A completely different publication",
        publication_year=2024,
    )
    wrong_year = WorkMetadata(
        source="crossref",
        title=reference.title,
        publication_year=2018,
    )
    assert candidate_score(reference, unrelated) is None
    assert candidate_score(reference, wrong_year) is None


def test_parse_datacite_dataset() -> None:
    metadata = parse_datacite_item(
        {
            "id": "10.1234/example",
            "attributes": {
                "doi": "10.1234/example",
                "titles": [{"title": "A Research Dataset"}],
                "creators": [
                    {"givenName": "Ada", "familyName": "Lovelace"},
                ],
                "publicationYear": 2025,
                "published": "2025-03-14",
                "publisher": {"name": "Example Repository"},
                "types": {"resourceTypeGeneral": "Dataset"},
                "url": "https://example.org/dataset",
            },
        }
    )

    assert metadata.output_type == "Data set"
    assert metadata.authors == ("Ada Lovelace",)
    assert metadata.published_date == "2025-03-14"
    assert metadata.doi == "10.1234/example"


@pytest.mark.asyncio
async def test_prepare_publication_builds_prefilled_form_from_best_match() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "scholar.google.com":
            return httpx.Response(
                200,
                text=FIXTURE.read_text(encoding="utf-8"),
            )
        if request.url.host == "api.crossref.org":
            return httpx.Response(
                200,
                json={"message": {"items": [CROSSREF_ITEM]}},
            )
        if request.url.host == "api.datacite.org":
            return httpx.Response(200, json={"data": []})
        raise AssertionError(f"Unexpected request: {request.url}")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
        result = await prepare_publication(PUBLICATION, client)

    assert result.match_source == "crossref"
    assert result.confidence is not None
    assert result.fields["Output_type1"] == "Journal article"
    assert result.fields["Date_published"] == "11-09-2024"
    assert result.fields["Unique_Identifier1"] == "10.1080/00913367.2024.2394156"
    assert result.field_sources["Title"] == "crossref"
    assert "Member(s) involved" in result.missing_fields

    query = parse_qs(urlparse(result.form_url).query)
    assert query["Title"] == [CROSSREF_ITEM["title"][0]]
    assert query["Date_published"] == ["11-09-2024"]
    assert query["Output_type1"] == ["Journal article"]


@pytest.mark.asyncio
async def test_prepare_publication_degrades_to_scholar_when_providers_fail() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "scholar.google.com":
            return httpx.Response(
                200,
                text=FIXTURE.read_text(encoding="utf-8"),
            )
        return httpx.Response(503, text="Temporarily unavailable")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
        result = await prepare_publication(PUBLICATION, client)

    assert result.match_source == "google_scholar"
    assert result.confidence is None
    assert result.fields["Title"] == PUBLICATION.title
    assert result.fields["Date_published"] == "19-10-2024"
    assert result.fields["Unique_Identifier1"] == "10.1080/00913367.2024.2394156"
    assert any("Crossref" in warning for warning in result.warnings)
    assert any("DataCite" in warning for warning in result.warnings)
