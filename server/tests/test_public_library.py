import httpx
import pytest

from server.identifiers import normalise_doi
from server.public_library import (
    author_codes,
    compare_publications,
    parse_bibtex_entry,
)
from server.scholar import Publication


def test_normalise_doi_accepts_library_prefixes_and_urls() -> None:
    assert normalise_doi("doi:10.1145/3477495.3531792") == "10.1145/3477495.3531792"
    assert (
        normalise_doi("doi:https://doi.org/10.1145/3589334.3645354")
        == "10.1145/3589334.3645354"
    )


def test_author_codes_accepts_directory_name_order() -> None:
    document = """
    <li data-author-id="10">Angus, Daniel (107)</li>
    <li data-author-id="11">Angus, Angela (4)</li>
    """

    assert author_codes(document, "Daniel Angus") == ("10",)


def test_parse_bibtex_entry_preserves_nested_title_and_normalises_doi() -> None:
    entry = parse_bibtex_entry(
        """
        @article{example,
          title = {{Computational {Methods}} for Testing},
          author = {Angus, Daniel and Hayden, Lauren},
          doi = {doi:https://doi.org/10.1145/3589334.3645354},
          date = {2024-10-19},
        }
        """
    )

    assert entry is not None
    assert entry.title == "Computational Methods for Testing"
    assert entry.authors == ("Angus, Daniel", "Hayden, Lauren")
    assert entry.year == 2024
    assert entry.doi == "10.1145/3589334.3645354"


@pytest.mark.asyncio
async def test_compare_publications_prefers_confirmed_doi_match() -> None:
    publication = Publication(
        id="paper-1",
        title="Computational Methods for Testing",
        year=2024,
        authors="D Angus, L Hayden",
        venue="Journal of Tests",
        detail_url="https://scholar.google.com/citations?citation_for_view=paper-1",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.admscentre.org.au":
            if "auth" not in request.url.params:
                return httpx.Response(
                    200,
                    text='<li data-author-id="10">Angus, Daniel (107)</li>',
                )
            return httpx.Response(
                200,
                text="""
                <pre>@article{example,
                  title = {Computational Methods for Testing},
                  author = {Angus, Daniel and Hayden, Lauren},
                  doi = {doi:10.1145/3477495.3531792},
                  year = {2024},
                }</pre>
                """,
            )
        if request.url.host == "api.crossref.org":
            return httpx.Response(
                200,
                json={
                    "message": {
                        "items": [
                            {
                                "DOI": "10.1145/3477495.3531792",
                                "title": ["Computational Methods for Testing"],
                                "author": [
                                    {"given": "Daniel", "family": "Angus"},
                                    {"given": "Lauren", "family": "Hayden"},
                                ],
                                "published": {"date-parts": [[2024]]},
                            }
                        ]
                    }
                },
            )
        raise AssertionError(f"Unexpected request: {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        comparison = await compare_publications("Daniel Angus", (publication,), client)

    match = comparison.matches[publication.id]
    assert comparison.warning == ""
    assert match.status == "likely"
    assert match.match_type == "doi"
    assert match.confidence == 1.0
