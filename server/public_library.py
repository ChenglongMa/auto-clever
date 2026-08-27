from __future__ import annotations

import asyncio
import html
import re
import time
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from urllib.parse import parse_qs, urlparse

import httpx
from bs4 import BeautifulSoup

from server.identifiers import normalise_doi
from server.metadata import WorkMetadata, candidate_score, search_crossref
from server.scholar import Publication

PUBLICATIONS_LIBRARY_URL = "https://www.admscentre.org.au/publications-library/"
LIBRARY_TIMEOUT = httpx.Timeout(12.0, connect=8.0)
CACHE_TTL_SECONDS = 60 * 60
MAX_AUTHOR_CODES = 4
MAX_PAGES_PER_AUTHOR = 20
MAX_CROSSREF_REQUESTS = 4


@dataclass(frozen=True, slots=True)
class LibraryEntry:
    title: str
    authors: tuple[str, ...]
    year: int | None
    doi: str


@dataclass(frozen=True, slots=True)
class PublicLibraryMatch:
    status: str = "none"
    match_type: str = ""
    confidence: float | None = None
    title: str = ""
    year: int | None = None


@dataclass(frozen=True, slots=True)
class PublicLibraryComparison:
    matches: dict[str, PublicLibraryMatch]
    warning: str = ""


@dataclass(slots=True)
class _EntriesCacheEntry:
    entries: tuple[LibraryEntry, ...]
    warning: str
    expires_at: float


_entries_cache: dict[str, _EntriesCacheEntry] = {}
_cache_lock = asyncio.Lock()


def _clean_text(value: object) -> str:
    return " ".join(html.unescape(str(value or "")).replace("\xa0", " ").split())


def _normalise_words(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    return " ".join(
        "".join(character if character.isalnum() else " " for character in decomposed)
        .casefold()
        .split()
    )


def _name_tokens(value: str) -> frozenset[str]:
    return frozenset(_normalise_words(value).split())


def author_codes(document: str, profile_name: str) -> tuple[str, ...]:
    target = _name_tokens(profile_name)
    if not target:
        return ()

    codes: list[str] = []
    soup = BeautifulSoup(document, "html.parser")
    for item in soup.select("li[data-author-id]"):
        label = re.sub(r"\s*\(\d+\)\s*$", "", item.get_text(" ", strip=True))
        if _name_tokens(label) == target:
            code = _clean_text(item.get("data-author-id"))
            if code and code not in codes:
                codes.append(code)
    return tuple(codes)


def _read_bibtex_value(document: str, start: int) -> tuple[str, int]:
    if start >= len(document):
        return "", start
    delimiter = document[start]
    if delimiter not in {'{', '"'}:
        return "", start

    result: list[str] = []
    depth = 1 if delimiter == "{" else 0
    position = start + 1
    while position < len(document):
        character = document[position]
        if character == "\\" and position + 1 < len(document):
            result.extend((character, document[position + 1]))
            position += 2
            continue
        if delimiter == "{" and character == "{":
            depth += 1
        elif delimiter == "{" and character == "}":
            depth -= 1
            if depth == 0:
                return "".join(result), position + 1
        elif delimiter == '"' and character == '"':
            return "".join(result), position + 1
        result.append(character)
        position += 1
    return "", position


def parse_bibtex_entry(document: str) -> LibraryEntry | None:
    fields: dict[str, str] = {}
    for match in re.finditer(r"(?im)^\s*([a-z][a-z-]*)\s*=\s*", document):
        value, _ = _read_bibtex_value(document, match.end())
        if value:
            fields[match.group(1).casefold()] = value

    title = _clean_text(fields.get("title", "").replace("{", "").replace("}", ""))
    if not title:
        return None
    authors = tuple(
        _clean_text(author)
        for author in re.split(r"\s+and\s+", fields.get("author", ""), flags=re.I)
        if _clean_text(author)
    )
    year_match = re.search(
        r"\b(?:19|20)\d{2}\b", fields.get("year") or fields.get("date", "")
    )
    return LibraryEntry(
        title=title,
        authors=authors,
        year=int(year_match.group()) if year_match else None,
        doi=normalise_doi(fields.get("doi") or fields.get("url")),
    )


def parse_library_entries(document: str) -> tuple[LibraryEntry, ...]:
    entries: list[LibraryEntry] = []
    seen: set[tuple[str, str, int | None]] = set()
    soup = BeautifulSoup(document, "html.parser")
    for element in soup.select("pre"):
        entry = parse_bibtex_entry(element.get_text("\n", strip=True))
        if entry is None:
            continue
        key = (entry.doi, _normalise_words(entry.title), entry.year)
        if key not in seen:
            entries.append(entry)
            seen.add(key)
    return tuple(entries)


def _page_count(document: str, author_code: str) -> tuple[int, bool]:
    highest = 1
    for link in BeautifulSoup(document, "html.parser").select("a[href]"):
        query = parse_qs(urlparse(link["href"]).query)
        if author_code not in query.get("auth", []):
            continue
        for value in query.get("limit", []):
            try:
                highest = max(highest, int(value))
            except ValueError:
                continue
    return min(highest, MAX_PAGES_PER_AUTHOR), highest > MAX_PAGES_PER_AUTHOR


async def _fetch_author_entries(
    client: httpx.AsyncClient,
    author_code: str,
) -> tuple[tuple[LibraryEntry, ...], bool]:
    response = await client.get(PUBLICATIONS_LIBRARY_URL, params={"auth": author_code})
    response.raise_for_status()
    documents = [response.text]
    page_count, truncated = _page_count(response.text, author_code)
    responses = await asyncio.gather(
        *(
            client.get(
                PUBLICATIONS_LIBRARY_URL,
                params={"auth": author_code, "limit": page},
            )
            for page in range(2, page_count + 1)
        ),
        return_exceptions=True,
    )
    for page in responses:
        if isinstance(page, Exception):
            truncated = True
            continue
        try:
            page.raise_for_status()
        except httpx.HTTPError:
            truncated = True
            continue
        documents.append(page.text)

    entries: list[LibraryEntry] = []
    seen: set[tuple[str, str, int | None]] = set()
    for document in documents:
        for entry in parse_library_entries(document):
            key = (entry.doi, _normalise_words(entry.title), entry.year)
            if key not in seen:
                entries.append(entry)
                seen.add(key)
    return tuple(entries), truncated


async def _fetch_library_entries(
    profile_name: str,
    client: httpx.AsyncClient,
) -> tuple[tuple[LibraryEntry, ...], str]:
    response = await client.get(PUBLICATIONS_LIBRARY_URL)
    response.raise_for_status()
    codes = author_codes(response.text, profile_name)
    if not codes:
        return (), ""
    if len(codes) > MAX_AUTHOR_CODES:
        return (), "Public-library comparison is unavailable for this ambiguous author."

    results = await asyncio.gather(
        *(_fetch_author_entries(client, code) for code in codes),
        return_exceptions=True,
    )
    entries: list[LibraryEntry] = []
    incomplete = False
    for result in results:
        if isinstance(result, Exception):
            incomplete = True
            continue
        author_entries, truncated = result
        entries.extend(author_entries)
        incomplete = incomplete or truncated

    if not entries and incomplete:
        return (), "Public-library comparison is temporarily unavailable."
    if incomplete:
        return tuple(entries), "Public-library comparison may be incomplete."
    return tuple(entries), ""


async def _library_entries(
    profile_name: str,
    client: httpx.AsyncClient | None,
) -> tuple[tuple[LibraryEntry, ...], str]:
    if client is not None:
        return await _fetch_library_entries(profile_name, client)

    key = _normalise_words(profile_name)
    now = time.monotonic()
    async with _cache_lock:
        cached = _entries_cache.get(key)
        if cached and cached.expires_at > now:
            return cached.entries, cached.warning

    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=LIBRARY_TIMEOUT,
        headers={"User-Agent": "auto-clever/0.1 (https://chenglongma.com/auto-clever)"},
    ) as request_client:
        try:
            entries, warning = await _fetch_library_entries(profile_name, request_client)
        except httpx.HTTPError:
            return (), "Public-library comparison is temporarily unavailable."

    async with _cache_lock:
        _entries_cache[key] = _EntriesCacheEntry(
            entries=entries,
            warning=warning,
            expires_at=time.monotonic() + CACHE_TTL_SECONDS,
        )
    return entries, warning


def _title_score(left: str, right: str) -> float:
    left_words = _normalise_words(left)
    right_words = _normalise_words(right)
    if not left_words or not right_words:
        return 0.0
    sequence_score = SequenceMatcher(None, left_words, right_words).ratio()
    left_tokens = set(left_words.split())
    right_tokens = set(right_words.split())
    token_score = len(left_tokens & right_tokens) / len(left_tokens | right_tokens)
    return max(sequence_score, token_score)


def _surname_set(authors: tuple[str, ...]) -> set[str]:
    surnames: set[str] = set()
    for author in authors:
        words = _normalise_words(author).split()
        if not words:
            continue
        surnames.add(words[0] if "," in author else words[-1])
    return surnames


def _scholar_authors(publication: Publication) -> tuple[str, ...]:
    return tuple(
        author.strip()
        for author in publication.authors.split(",")
        if author.strip() and author.strip() != "..."
    )


def _provisional_match(
    publication: Publication,
    entries: tuple[LibraryEntry, ...],
) -> PublicLibraryMatch:
    scholar_surnames = _surname_set(_scholar_authors(publication))
    best: tuple[float, LibraryEntry, bool, bool] | None = None
    for entry in entries:
        title_score = _title_score(publication.title, entry.title)
        if title_score < 0.9:
            continue
        same_year = publication.year is not None and entry.year == publication.year
        shares_author = bool(scholar_surnames & _surname_set(entry.authors))
        ranking = title_score + (0.04 if same_year else 0) + (0.03 if shares_author else 0)
        candidate = (ranking, entry, same_year, shares_author)
        if best is None or candidate[0] > best[0]:
            best = candidate

    if best is None:
        return PublicLibraryMatch()
    ranking, entry, same_year, shares_author = best
    title_score = min(ranking, 1.0)
    if _title_score(publication.title, entry.title) >= 0.995 and same_year and shares_author:
        return PublicLibraryMatch("likely", "title_year_author", title_score, entry.title, entry.year)
    if _title_score(publication.title, entry.title) >= 0.9 and (same_year or shares_author):
        return PublicLibraryMatch("possible", "title", title_score, entry.title, entry.year)
    return PublicLibraryMatch()


async def _doi_match(
    client: httpx.AsyncClient,
    publication: Publication,
    entries: tuple[LibraryEntry, ...],
    semaphore: asyncio.Semaphore,
) -> PublicLibraryMatch | None:
    dois = {entry.doi: entry for entry in entries if entry.doi}
    if not dois:
        return None
    reference = WorkMetadata(
        source="google_scholar",
        title=publication.title,
        authors=_scholar_authors(publication),
        publication_year=publication.year,
    )
    try:
        async with semaphore:
            candidates = await search_crossref(client, publication)
    except httpx.HTTPError:
        return None
    for candidate in candidates:
        entry = dois.get(candidate.doi)
        if entry and candidate_score(reference, candidate) is not None:
            return PublicLibraryMatch("likely", "doi", 1.0, entry.title, entry.year)
    return None


async def compare_publications(
    profile_name: str,
    publications: list[Publication] | tuple[Publication, ...],
    client: httpx.AsyncClient | None = None,
) -> PublicLibraryComparison:
    items = tuple(publications)
    default_matches = {item.id: PublicLibraryMatch() for item in items}
    if not items:
        return PublicLibraryComparison(default_matches)

    entries, warning = await _library_entries(profile_name, client)
    if not entries:
        return PublicLibraryComparison(default_matches, warning)

    candidates = {
        item.id: tuple(
            entry for entry in entries if _title_score(item.title, entry.title) >= 0.82
        )
        for item in items
    }
    matches = {
        item.id: _provisional_match(item, candidates[item.id]) for item in items
    }
    request_items = [item for item in items if any(entry.doi for entry in candidates[item.id])]
    if not request_items:
        return PublicLibraryComparison(matches, warning)

    owns_client = client is None
    request_client = client or httpx.AsyncClient(
        follow_redirects=True,
        timeout=LIBRARY_TIMEOUT,
        headers={"User-Agent": "auto-clever/0.1 (https://chenglongma.com/auto-clever)"},
    )
    try:
        semaphore = asyncio.Semaphore(MAX_CROSSREF_REQUESTS)
        doi_matches = await asyncio.gather(
            *(
                _doi_match(request_client, item, candidates[item.id], semaphore)
                for item in request_items
            )
        )
    finally:
        if owns_client:
            await request_client.aclose()
    for item, doi_match in zip(request_items, doi_matches, strict=True):
        if doi_match is not None:
            matches[item.id] = doi_match
    return PublicLibraryComparison(matches, warning)
