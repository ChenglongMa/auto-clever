from __future__ import annotations

import asyncio
import hashlib
import html
import re
import time
from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

SCHOLAR_BASE_URL = "https://scholar.google.com/citations"
SCHOLAR_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{6,32}$")
MAX_PAGES = 5
PAGE_SIZE = 100
CACHE_TTL_SECONDS = 60 * 60


class ScholarError(RuntimeError):
    """Base error for Scholar lookup failures."""


class InvalidScholarIdError(ScholarError):
    pass


class ScholarProfileNotFoundError(ScholarError):
    pass


class ScholarBlockedError(ScholarError):
    pass


@dataclass(frozen=True, slots=True)
class Publication:
    id: str
    title: str
    year: int | None
    authors: str
    venue: str
    detail_url: str
    duplicate_count: int = 1


@dataclass(frozen=True, slots=True)
class ScholarProfile:
    scholar_id: str
    name: str
    publications: tuple[Publication, ...]


@dataclass(slots=True)
class _CacheEntry:
    profile: ScholarProfile
    expires_at: float


_profile_cache: dict[str, _CacheEntry] = {}
_cache_lock = asyncio.Lock()


def extract_scholar_id(value: str) -> str:
    candidate = value.strip()
    if not candidate:
        raise InvalidScholarIdError("Google Scholar ID is required.")

    if "://" in candidate:
        parsed = urlparse(candidate)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
            "scholar.google.com",
            "scholar.google.com.au",
        }:
            raise InvalidScholarIdError("Use a Google Scholar profile URL.")
        candidate = parse_qs(parsed.query).get("user", [""])[0]

    if not SCHOLAR_ID_PATTERN.fullmatch(candidate):
        raise InvalidScholarIdError("Google Scholar ID has an invalid format.")
    return candidate


def _clean_text(value: str) -> str:
    return " ".join(html.unescape(value).replace("\xa0", " ").split())


def _parse_year(value: str) -> int | None:
    match = re.search(r"\b(?:19|20)\d{2}\b", value)
    return int(match.group()) if match else None


def _fallback_publication_id(title: str, year: int | None, authors: str) -> str:
    value = f"{title.casefold()}\0{year or ''}\0{authors.casefold()}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]


def parse_profile_page(document: str, scholar_id: str) -> tuple[str, list[Publication]]:
    soup = BeautifulSoup(document, "html.parser")

    lower_document = document.casefold()
    if "/sorry/" in lower_document or "not a robot" in lower_document:
        raise ScholarBlockedError("Google Scholar requested human verification.")

    name_element = soup.select_one("#gsc_prf_in")
    if name_element is None:
        raise ScholarProfileNotFoundError("Google Scholar profile was not found.")

    profile_name = _clean_text(name_element.get_text(" ", strip=True))
    publications: list[Publication] = []

    for row in soup.select("tr.gsc_a_tr"):
        title_element = row.select_one("a.gsc_a_at")
        if title_element is None:
            continue

        title = _clean_text(title_element.get_text(" ", strip=True))
        if not title:
            continue

        gray_fields = row.select("td.gsc_a_t div.gs_gray")
        authors = (
            _clean_text(gray_fields[0].get_text(" ", strip=True)) if gray_fields else ""
        )
        venue = (
            _clean_text(gray_fields[1].get_text(" ", strip=True))
            if len(gray_fields) > 1
            else ""
        )
        year_element = row.select_one("td.gsc_a_y span")
        year = (
            _parse_year(year_element.get_text(" ", strip=True))
            if year_element
            else None
        )

        detail_url = urljoin(SCHOLAR_BASE_URL, title_element.get("href", ""))
        detail_query = parse_qs(urlparse(detail_url).query)
        publication_id = detail_query.get("citation_for_view", [""])[0]
        if not publication_id:
            publication_id = _fallback_publication_id(title, year, authors)

        publications.append(
            Publication(
                id=publication_id,
                title=title,
                year=year,
                authors=authors,
                venue=venue,
                detail_url=detail_url,
            )
        )

    return profile_name, publications


def _normalise_title(value: str) -> str:
    return " ".join(re.findall(r"[\w]+", value.casefold(), flags=re.UNICODE))


def filter_and_deduplicate(
    publications: list[Publication] | tuple[Publication, ...],
    from_year: int,
    to_year: int,
) -> list[Publication]:
    selected = [
        publication
        for publication in publications
        if publication.year is not None and from_year <= publication.year <= to_year
    ]

    grouped: dict[tuple[str, int], Publication] = {}
    counts: dict[tuple[str, int], int] = {}

    for publication in selected:
        key = (_normalise_title(publication.title), publication.year or 0)
        counts[key] = counts.get(key, 0) + 1
        current = grouped.get(key)
        if current is None:
            grouped[key] = publication
            continue

        current_detail = len(current.authors) + len(current.venue)
        candidate_detail = len(publication.authors) + len(publication.venue)
        if candidate_detail > current_detail:
            grouped[key] = publication

    deduplicated = [
        Publication(
            id=publication.id,
            title=publication.title,
            year=publication.year,
            authors=publication.authors,
            venue=publication.venue,
            detail_url=publication.detail_url,
            duplicate_count=counts[key],
        )
        for key, publication in grouped.items()
    ]
    return sorted(
        deduplicated, key=lambda item: (-(item.year or 0), item.title.casefold())
    )


async def _fetch_profile(scholar_id: str) -> ScholarProfile:
    headers = {
        "Accept-Language": "en-AU,en;q=0.9",
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ),
    }
    publications_by_id: dict[str, Publication] = {}
    profile_name = ""

    timeout = httpx.Timeout(20.0, connect=10.0)
    async with httpx.AsyncClient(
        headers=headers, follow_redirects=True, timeout=timeout
    ) as client:
        for page_index in range(MAX_PAGES):
            params = {
                "user": scholar_id,
                "hl": "en",
                "cstart": page_index * PAGE_SIZE,
                "pagesize": PAGE_SIZE,
            }
            response = await client.get(f"{SCHOLAR_BASE_URL}?{urlencode(params)}")
            if response.status_code == 429:
                raise ScholarBlockedError("Google Scholar rate limited the request.")
            response.raise_for_status()

            page_name, page_publications = parse_profile_page(response.text, scholar_id)
            profile_name = profile_name or page_name
            for publication in page_publications:
                publications_by_id[publication.id] = publication

            if len(page_publications) < PAGE_SIZE:
                break

    return ScholarProfile(
        scholar_id=scholar_id,
        name=profile_name,
        publications=tuple(publications_by_id.values()),
    )


async def get_profile(scholar_id: str) -> ScholarProfile:
    now = time.monotonic()
    cached = _profile_cache.get(scholar_id)
    if cached is not None and cached.expires_at > now:
        return cached.profile

    async with _cache_lock:
        cached = _profile_cache.get(scholar_id)
        if cached is not None and cached.expires_at > time.monotonic():
            return cached.profile

        profile = await _fetch_profile(scholar_id)
        _profile_cache[scholar_id] = _CacheEntry(
            profile=profile,
            expires_at=time.monotonic() + CACHE_TTL_SECONDS,
        )
        return profile
