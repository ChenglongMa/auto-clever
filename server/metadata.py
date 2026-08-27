from __future__ import annotations

import asyncio
import html
import os
import re
from dataclasses import dataclass, replace
from datetime import date
from difflib import SequenceMatcher
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from server.clever import build_form_url, list_missing_required_fields
from server.identifiers import normalise_doi
from server.scholar import Publication, ScholarBlockedError

CROSSREF_API_URL = "https://api.crossref.org/works"
DATACITE_API_URL = "https://api.datacite.org/dois"
EXTERNAL_TIMEOUT = httpx.Timeout(20.0, connect=10.0)

CROSSREF_TYPE_MAP = {
    "book": "Book",
    "book-chapter": "Book chapter",
    "dataset": "Data set",
    "dissertation": "Thesis",
    "edited-book": "Edited book",
    "journal-article": "Journal article",
    "monograph": "Book",
    "posted-content": "Preprint",
    "proceedings": "Conference proceedings",
    "proceedings-article": "Refereed Conference Paper",
    "report": "Report",
    "report-series": "Report",
}

DATACITE_TYPE_MAP = {
    "Audiovisual": "Film / video",
    "Book": "Book",
    "BookChapter": "Book chapter",
    "ConferencePaper": "Refereed Conference Paper",
    "ConferenceProceeding": "Conference proceedings",
    "DataPaper": "Journal article",
    "Dataset": "Data set",
    "Dissertation": "Thesis",
    "Image": "Creative work",
    "InteractiveResource": "Interactive software / system / platform / plug-in",
    "JournalArticle": "Journal article",
    "Preprint": "Preprint",
    "Report": "Report",
    "Software": "Source Code",
}


@dataclass(frozen=True, slots=True)
class WorkMetadata:
    source: str
    title: str
    authors: tuple[str, ...] = ()
    published_date: str = ""
    publication_year: int | None = None
    container_title: str = ""
    publisher: str = ""
    volume: str = ""
    issue: str = ""
    pages: str = ""
    doi: str = ""
    isbn: str = ""
    url: str = ""
    raw_type: str = ""
    output_type: str = ""
    citation: str = ""


@dataclass(frozen=True, slots=True)
class PreparationResult:
    form_url: str
    fields: dict[str, str]
    field_sources: dict[str, str]
    missing_fields: tuple[str, ...]
    warnings: tuple[str, ...]
    metadata: WorkMetadata
    match_source: str
    confidence: float | None


def _clean_text(value: object) -> str:
    if value is None:
        return ""
    raw = str(value)
    text = (
        BeautifulSoup(raw, "html.parser").get_text(" ", strip=True)
        if "<" in raw and ">" in raw
        else html.unescape(raw)
    )
    return " ".join(text.replace("\xa0", " ").split())


def _first_text(value: object) -> str:
    if isinstance(value, list):
        return _clean_text(value[0]) if value else ""
    return _clean_text(value)


def _normalise_doi(value: object) -> str:
    return normalise_doi(value)


def _safe_external_url(value: object) -> str:
    candidate = _clean_text(value)
    if not candidate:
        return ""
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return ""
    return candidate if parsed.scheme == "https" and parsed.netloc else ""


def _parse_iso_date(value: object) -> tuple[str, int | None]:
    candidate = _clean_text(value).replace("/", "-")
    year_match = re.search(r"\b(?:19|20)\d{2}\b", candidate)
    year = int(year_match.group()) if year_match else None
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", candidate):
        return "", year
    try:
        return date.fromisoformat(candidate).isoformat(), year
    except ValueError:
        return "", year


def _date_from_crossref(item: dict[str, object]) -> tuple[str, int | None]:
    fallback_year: int | None = None
    for key in ("published", "published-online", "published-print", "issued"):
        date_value = item.get(key)
        if not isinstance(date_value, dict):
            continue
        parts_collection = date_value.get("date-parts")
        if not isinstance(parts_collection, list) or not parts_collection:
            continue
        parts = parts_collection[0]
        if not isinstance(parts, list) or not parts:
            continue
        try:
            year = int(parts[0])
        except (TypeError, ValueError):
            continue
        fallback_year = fallback_year or year
        if len(parts) < 3:
            continue
        try:
            exact = date(year, int(parts[1]), int(parts[2])).isoformat()
        except (TypeError, ValueError):
            continue
        return exact, year
    return "", fallback_year


def _split_scholar_authors(value: str) -> tuple[str, ...]:
    return tuple(
        part.strip()
        for part in value.split(",")
        if part.strip() and part.strip() != "..."
    )


def parse_scholar_detail_page(
    document: str,
    publication: Publication,
) -> WorkMetadata:
    lower_document = document.casefold()
    if "/sorry/" in lower_document or "not a robot" in lower_document:
        raise ScholarBlockedError("Google Scholar requested human verification.")

    soup = BeautifulSoup(document, "html.parser")
    title_element = soup.select_one("#gsc_oci_title")
    title = (
        _clean_text(title_element.get_text(" ", strip=True))
        if title_element
        else publication.title
    )
    details: dict[str, str] = {}
    for row in soup.select(".gs_scl"):
        label = row.select_one(".gsc_oci_field")
        value = row.select_one(".gsc_oci_value")
        if label is None or value is None:
            continue
        details[_clean_text(label.get_text(" ", strip=True)).casefold()] = _clean_text(
            value.get_text(" ", strip=True)
        )

    author_text = details.get("authors", publication.authors)
    published_date, year = _parse_iso_date(details.get("publication date", ""))
    container_title = next(
        (
            details[name]
            for name in ("journal", "conference", "book")
            if details.get(name)
        ),
        publication.venue,
    )
    raw_type = ""
    if details.get("journal"):
        raw_type = "journal-article"
    elif details.get("conference"):
        raw_type = "proceedings-article"

    title_link = title_element.select_one("a[href]") if title_element else None
    external_url = _safe_external_url(title_link.get("href")) if title_link else ""
    doi = _normalise_doi(" ".join(details.values()) + " " + external_url)
    output_type = CROSSREF_TYPE_MAP.get(raw_type, "")

    metadata = WorkMetadata(
        source="google_scholar",
        title=title,
        authors=_split_scholar_authors(author_text),
        published_date=published_date,
        publication_year=year or publication.year,
        container_title=container_title,
        publisher=details.get("publisher", ""),
        volume=details.get("volume", ""),
        issue=details.get("issue", ""),
        pages=details.get("pages", ""),
        doi=doi,
        url=(
            f"https://doi.org/{doi}" if doi else external_url or publication.detail_url
        ),
        raw_type=raw_type,
        output_type=output_type,
    )
    return replace(metadata, citation=format_citation(metadata))


def parse_crossref_item(item: dict[str, object]) -> WorkMetadata:
    published_date, publication_year = _date_from_crossref(item)
    authors: list[str] = []
    author_values = item.get("author")
    if isinstance(author_values, list):
        for author in author_values:
            if not isinstance(author, dict):
                continue
            name = " ".join(
                part
                for part in (
                    _clean_text(author.get("given")),
                    _clean_text(author.get("family")),
                )
                if part
            )
            if name:
                authors.append(name)

    doi = _normalise_doi(item.get("DOI"))
    isbn_values = item.get("ISBN")
    isbn = _first_text(isbn_values)
    raw_type = _clean_text(item.get("type"))
    metadata = WorkMetadata(
        source="crossref",
        title=_first_text(item.get("title")),
        authors=tuple(authors),
        published_date=published_date,
        publication_year=publication_year,
        container_title=_first_text(item.get("container-title")),
        publisher=_clean_text(item.get("publisher")),
        volume=_clean_text(item.get("volume")),
        issue=_clean_text(item.get("issue")),
        pages=_clean_text(item.get("page")),
        doi=doi,
        isbn=isbn,
        url=(f"https://doi.org/{doi}" if doi else _safe_external_url(item.get("URL"))),
        raw_type=raw_type,
        output_type=CROSSREF_TYPE_MAP.get(raw_type, ""),
    )
    return replace(metadata, citation=format_citation(metadata))


def parse_datacite_item(item: dict[str, object]) -> WorkMetadata:
    attributes = item.get("attributes")
    if not isinstance(attributes, dict):
        attributes = {}

    titles = attributes.get("titles")
    title = ""
    if isinstance(titles, list) and titles and isinstance(titles[0], dict):
        title = _clean_text(titles[0].get("title"))

    authors: list[str] = []
    creators = attributes.get("creators")
    if isinstance(creators, list):
        for creator in creators:
            if not isinstance(creator, dict):
                continue
            name = " ".join(
                part
                for part in (
                    _clean_text(creator.get("givenName")),
                    _clean_text(creator.get("familyName")),
                )
                if part
            )
            name = name or _clean_text(creator.get("name"))
            if name:
                authors.append(name)

    published_date, date_year = _parse_iso_date(attributes.get("published"))
    try:
        publication_year = int(attributes.get("publicationYear") or date_year)
    except (TypeError, ValueError):
        publication_year = date_year

    types = attributes.get("types")
    if not isinstance(types, dict):
        types = {}
    raw_type = _clean_text(types.get("resourceTypeGeneral"))

    publisher_value = attributes.get("publisher")
    publisher = (
        _clean_text(publisher_value.get("name"))
        if isinstance(publisher_value, dict)
        else _clean_text(publisher_value)
    )
    container = attributes.get("container")
    container_title = (
        _clean_text(container.get("title")) if isinstance(container, dict) else ""
    )

    doi = _normalise_doi(attributes.get("doi") or item.get("id"))
    isbn = ""
    alternate_ids = attributes.get("alternateIdentifiers")
    if isinstance(alternate_ids, list):
        for identifier in alternate_ids:
            if not isinstance(identifier, dict):
                continue
            if (
                _clean_text(identifier.get("alternateIdentifierType")).casefold()
                == "isbn"
            ):
                isbn = _clean_text(identifier.get("alternateIdentifier"))
                break

    metadata = WorkMetadata(
        source="datacite",
        title=title,
        authors=tuple(authors),
        published_date=published_date,
        publication_year=publication_year,
        container_title=container_title,
        publisher=publisher,
        doi=doi,
        isbn=isbn,
        url=(
            f"https://doi.org/{doi}"
            if doi
            else _safe_external_url(attributes.get("url"))
        ),
        raw_type=raw_type,
        output_type=DATACITE_TYPE_MAP.get(raw_type, ""),
    )
    return replace(metadata, citation=format_citation(metadata))


def format_citation(metadata: WorkMetadata) -> str:
    pieces: list[str] = []
    if metadata.authors:
        author_text = ", ".join(metadata.authors)
        pieces.append(author_text.rstrip(". ") + ".")
    if metadata.publication_year:
        pieces.append(f"({metadata.publication_year}).")
    if metadata.title:
        pieces.append(metadata.title.rstrip(". ") + ".")

    publication = metadata.container_title
    if metadata.volume:
        publication += (", " if publication else "") + metadata.volume
        if metadata.issue:
            publication += f"({metadata.issue})"
    elif metadata.issue:
        publication += (", " if publication else "") + metadata.issue
    if metadata.pages:
        publication += (", " if publication else "") + metadata.pages
    if publication:
        pieces.append(publication.rstrip(". ") + ".")
    elif metadata.publisher:
        pieces.append(metadata.publisher.rstrip(". ") + ".")

    if metadata.doi:
        pieces.append(f"https://doi.org/{metadata.doi}")
    elif metadata.url:
        pieces.append(metadata.url)
    return " ".join(pieces)


def _normalise_title(value: str) -> str:
    return " ".join(re.findall(r"[\w]+", value.casefold(), flags=re.UNICODE))


def _surname_set(authors: tuple[str, ...]) -> set[str]:
    surnames: set[str] = set()
    for author in authors:
        cleaned = _normalise_title(author)
        if not cleaned:
            continue
        surnames.add(cleaned.split()[-1])
    return surnames


def candidate_score(reference: WorkMetadata, candidate: WorkMetadata) -> float | None:
    reference_title = _normalise_title(reference.title)
    candidate_title = _normalise_title(candidate.title)
    if not reference_title or not candidate_title:
        return None

    title_score = SequenceMatcher(None, reference_title, candidate_title).ratio()
    reference_words = set(reference_title.split())
    candidate_words = set(candidate_title.split())
    token_score = len(reference_words & candidate_words) / max(
        len(reference_words | candidate_words), 1
    )
    title_score = max(title_score, token_score)

    if title_score < 0.88:
        return None
    if min(len(reference_title), len(candidate_title)) < 20 and title_score < 0.95:
        return None

    year_score = 0.5
    if reference.publication_year and candidate.publication_year:
        year_difference = abs(reference.publication_year - candidate.publication_year)
        if year_difference > 1:
            return None
        year_score = 1.0 if year_difference == 0 else 0.6

    reference_authors = _surname_set(reference.authors)
    candidate_authors = _surname_set(candidate.authors)
    author_score = 0.5
    if reference_authors and candidate_authors:
        author_score = len(reference_authors & candidate_authors) / min(
            len(reference_authors), len(candidate_authors)
        )

    score = 0.82 * title_score + 0.12 * year_score + 0.06 * author_score
    return round(score, 4) if score >= 0.85 else None


async def fetch_scholar_details(
    client: httpx.AsyncClient,
    publication: Publication,
) -> WorkMetadata:
    response = await client.get(
        publication.detail_url,
        headers={
            "Accept-Language": "en-AU,en;q=0.9",
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
        },
    )
    if response.status_code == 429:
        raise ScholarBlockedError("Google Scholar rate limited the request.")
    response.raise_for_status()
    return parse_scholar_detail_page(response.text, publication)


async def search_crossref(
    client: httpx.AsyncClient,
    publication: Publication,
) -> list[WorkMetadata]:
    params = {
        "query.title": publication.title,
        "rows": "5",
        "select": (
            "DOI,title,author,published,published-online,published-print,issued,"
            "container-title,publisher,type,URL,volume,issue,page,ISBN"
        ),
    }
    first_author = publication.authors.split(",", 1)[0].strip()
    if first_author and first_author != "...":
        params["query.author"] = first_author
    mailto = os.getenv("CROSSREF_MAILTO", "").strip()
    if mailto:
        params["mailto"] = mailto

    response = await client.get(
        CROSSREF_API_URL,
        params=params,
        headers={"User-Agent": "auto-clever/0.1 (https://chenglongma.com/auto-clever)"},
    )
    response.raise_for_status()
    payload = response.json()
    message = payload.get("message", {}) if isinstance(payload, dict) else {}
    items = message.get("items", []) if isinstance(message, dict) else []
    return [parse_crossref_item(item) for item in items if isinstance(item, dict)]


async def search_datacite(
    client: httpx.AsyncClient,
    publication: Publication,
) -> list[WorkMetadata]:
    escaped_title = publication.title.replace("\\", "\\\\").replace('"', '\\"')
    response = await client.get(
        DATACITE_API_URL,
        params={
            "query": f'titles.title:"{escaped_title}"',
            "page[size]": "5",
        },
        headers={"User-Agent": "auto-clever/0.1 (https://chenglongma.com/auto-clever)"},
    )
    response.raise_for_status()
    payload = response.json()
    items = payload.get("data", []) if isinstance(payload, dict) else []
    return [parse_datacite_item(item) for item in items if isinstance(item, dict)]


def _base_scholar_metadata(publication: Publication) -> WorkMetadata:
    metadata = WorkMetadata(
        source="google_scholar",
        title=publication.title,
        authors=_split_scholar_authors(publication.authors),
        publication_year=publication.year,
        container_title=publication.venue,
        url=publication.detail_url,
    )
    return replace(metadata, citation=format_citation(metadata))


def _merge_metadata(
    scholar: WorkMetadata,
    candidate: WorkMetadata,
) -> tuple[WorkMetadata, dict[str, str]]:
    def choose(attribute: str) -> tuple[object, str]:
        candidate_value = getattr(candidate, attribute)
        if candidate_value:
            return candidate_value, candidate.source
        return getattr(scholar, attribute), scholar.source

    title, title_source = choose("title")
    authors, _ = choose("authors")
    published_date, date_source = choose("published_date")
    publication_year, _ = choose("publication_year")
    container_title, _ = choose("container_title")
    publisher, _ = choose("publisher")
    volume, _ = choose("volume")
    issue, _ = choose("issue")
    pages, _ = choose("pages")
    doi, identifier_source = choose("doi")
    isbn, isbn_source = choose("isbn")
    url, url_source = choose("url")
    raw_type, _ = choose("raw_type")
    output_type, type_source = choose("output_type")

    merged = WorkMetadata(
        source=candidate.source,
        title=str(title),
        authors=tuple(authors),
        published_date=str(published_date),
        publication_year=int(publication_year) if publication_year else None,
        container_title=str(container_title),
        publisher=str(publisher),
        volume=str(volume),
        issue=str(issue),
        pages=str(pages),
        doi=str(doi),
        isbn=str(isbn),
        url=str(url),
        raw_type=str(raw_type),
        output_type=str(output_type),
    )
    merged = replace(merged, citation=format_citation(merged))
    sources = {
        "title": title_source,
        "published_date": date_source,
        "citation": candidate.source,
        "identifier": identifier_source if doi else isbn_source,
        "url": url_source,
        "output_type": type_source,
    }
    return merged, sources


def _fields_from_metadata(
    metadata: WorkMetadata,
    sources: dict[str, str],
) -> tuple[dict[str, str], dict[str, str]]:
    date_value = ""
    if metadata.published_date:
        parsed_date = date.fromisoformat(metadata.published_date)
        date_value = parsed_date.strftime("%d-%m-%Y")

    identifier = metadata.doi or metadata.isbn
    if not identifier and metadata.url:
        identifier = metadata.url

    candidates = {
        "Output_type1": (
            metadata.output_type,
            sources.get("output_type", metadata.source),
        ),
        "Date_published": (date_value, sources.get("published_date", metadata.source)),
        "Title": (metadata.title, sources.get("title", metadata.source)),
        "Citation1": (metadata.citation, sources.get("citation", metadata.source)),
        "Unique_Identifier1": (
            identifier,
            sources.get("identifier") or sources.get("url", metadata.source),
        ),
        "Output_URL": (metadata.url, sources.get("url", metadata.source)),
    }
    fields = {name: value for name, (value, _) in candidates.items() if value}
    field_sources = {
        name: source for name, (value, source) in candidates.items() if value and source
    }
    return fields, field_sources


async def prepare_publication(
    publication: Publication,
    client: httpx.AsyncClient | None = None,
) -> PreparationResult:
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(follow_redirects=True, timeout=EXTERNAL_TIMEOUT)

    warnings: list[str] = []
    try:
        results = await asyncio.gather(
            fetch_scholar_details(client, publication),
            search_crossref(client, publication),
            search_datacite(client, publication),
            return_exceptions=True,
        )
    finally:
        if owns_client:
            await client.aclose()

    scholar = _base_scholar_metadata(publication)
    if isinstance(results[0], WorkMetadata):
        scholar = results[0]
    else:
        warnings.append(
            "Scholar details could not be loaded; profile-list metadata was used."
        )

    candidates: list[WorkMetadata] = []
    for provider, result in zip(("Crossref", "DataCite"), results[1:], strict=True):
        if isinstance(result, list):
            candidates.extend(item for item in result if isinstance(item, WorkMetadata))
        else:
            warnings.append(
                f"{provider} could not be reached; its fields were left unavailable."
            )

    scored = [
        (score, candidate)
        for candidate in candidates
        if (score := candidate_score(scholar, candidate)) is not None
    ]
    scored.sort(key=lambda item: (item[0], item[1].source == "crossref"), reverse=True)

    if scored:
        confidence, candidate = scored[0]
        metadata, attribute_sources = _merge_metadata(scholar, candidate)
        match_source = candidate.source
    else:
        confidence = None
        metadata = scholar
        attribute_sources = {
            "title": scholar.source,
            "published_date": scholar.source,
            "citation": scholar.source,
            "identifier": scholar.source,
            "url": scholar.source,
            "output_type": scholar.source,
        }
        match_source = scholar.source
        warnings.append(
            "No sufficiently close Crossref or DataCite match was found; Scholar metadata was used."
        )

    fields, field_sources = _fields_from_metadata(metadata, attribute_sources)
    return PreparationResult(
        form_url=build_form_url(fields),
        fields=fields,
        field_sources=field_sources,
        missing_fields=tuple(list_missing_required_fields(fields)),
        warnings=tuple(warnings),
        metadata=metadata,
        match_source=match_source,
        confidence=confidence,
    )
