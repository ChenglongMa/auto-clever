from fastapi.testclient import TestClient

from server import main
from server.metadata import PreparationResult, WorkMetadata
from server.public_library import PublicLibraryComparison, PublicLibraryMatch
from server.scholar import Publication, ScholarProfile


def test_health_endpoint() -> None:
    response = TestClient(main.app).get("/healthz")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_publications_rejects_reversed_year_range() -> None:
    response = TestClient(main.app).get(
        "/api/publications",
        params={
            "scholar_id": "iAtNdR8AAAAJ",
            "from_year": 2026,
            "to_year": 2025,
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "from_year must not exceed to_year"


def test_prepare_endpoint_allows_frontend_cors_preflight() -> None:
    response = TestClient(main.app).options(
        "/api/publications/prepare",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert "POST" in response.headers["access-control-allow-methods"]


def test_prepare_publication_endpoint(monkeypatch) -> None:
    publication = Publication(
        id="test123:paper",
        title="A prepared paper",
        year=2025,
        authors="A Author",
        venue="Journal of Tests",
        detail_url="https://scholar.google.com/citations?citation_for_view=test123:paper",
    )

    async def fake_get_profile(scholar_id: str) -> ScholarProfile:
        return ScholarProfile(
            scholar_id=scholar_id,
            name="Test Researcher",
            publications=(publication,),
        )

    async def fake_prepare_publication(item: Publication) -> PreparationResult:
        assert item == publication
        metadata = WorkMetadata(
            source="crossref",
            title=item.title,
            authors=("A Author",),
            published_date="2025-05-20",
            publication_year=2025,
            doi="10.1234/test",
            url="https://doi.org/10.1234/test",
            output_type="Journal article",
            citation="A Author. (2025). A prepared paper. https://doi.org/10.1234/test",
        )
        return PreparationResult(
            form_url="https://example.test/form?Title=A+prepared+paper",
            fields={"Title": item.title},
            field_sources={"Title": "crossref"},
            missing_fields=("Member(s) involved",),
            warnings=(),
            metadata=metadata,
            match_source="crossref",
            confidence=1.0,
        )

    monkeypatch.setattr(main, "get_profile", fake_get_profile)
    monkeypatch.setattr(main, "prepare_publication", fake_prepare_publication)

    response = TestClient(main.app).post(
        "/api/publications/prepare",
        json={"scholar_id": "test123", "publication_id": publication.id},
    )

    assert response.status_code == 200
    assert response.json()["fields"] == {"Title": "A prepared paper"}
    assert response.json()["metadata"]["doi"] == "10.1234/test"


def test_publications_marks_library_matches_and_sorts_within_year(monkeypatch) -> None:
    publications = (
        Publication(
            id="same-year-no-match",
            title="Another 2025 Paper",
            year=2025,
            authors="A Author",
            venue="Tests",
            detail_url="https://scholar.google.com/citations?citation_for_view=no-match",
        ),
        Publication(
            id="older-match",
            title="A 2024 Paper",
            year=2024,
            authors="A Author",
            venue="Tests",
            detail_url="https://scholar.google.com/citations?citation_for_view=older-match",
        ),
        Publication(
            id="same-year-possible-match",
            title="An uncertain 2025 Paper",
            year=2025,
            authors="A Author",
            venue="Tests",
            detail_url="https://scholar.google.com/citations?citation_for_view=possible",
        ),
        Publication(
            id="same-year-match",
            title="A 2025 Paper",
            year=2025,
            authors="A Author",
            venue="Tests",
            detail_url="https://scholar.google.com/citations?citation_for_view=match",
        ),
    )

    async def fake_get_profile(scholar_id: str) -> ScholarProfile:
        return ScholarProfile(scholar_id, "Test Researcher", publications)

    async def fake_compare_publications(
        profile_name: str, items: list[Publication]
    ) -> PublicLibraryComparison:
        assert profile_name == "Test Researcher"
        assert {item.id for item in items} == {
            "same-year-no-match",
            "same-year-possible-match",
            "older-match",
            "same-year-match",
        }
        return PublicLibraryComparison(
            {
                "same-year-no-match": PublicLibraryMatch(),
                "same-year-possible-match": PublicLibraryMatch("possible", "title", 0.9),
                "older-match": PublicLibraryMatch("likely", "doi", 1.0),
                "same-year-match": PublicLibraryMatch("likely", "doi", 1.0),
            }
        )

    monkeypatch.setattr(main, "get_profile", fake_get_profile)
    monkeypatch.setattr(main, "compare_publications", fake_compare_publications)

    response = TestClient(main.app).get(
        "/api/publications",
        params={"scholar_id": "test123", "from_year": 2024, "to_year": 2025},
    )

    assert response.status_code == 200
    payload = response.json()
    assert [item["id"] for item in payload["publications"]] == [
        "same-year-no-match",
        "same-year-possible-match",
        "same-year-match",
        "older-match",
    ]
    assert payload["publications"][2]["public_library_match"]["match_type"] == "doi"
