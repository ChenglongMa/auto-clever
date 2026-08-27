from __future__ import annotations

import os
from datetime import UTC, datetime

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from server.metadata import prepare_publication
from server.public_library import (
    PublicLibraryComparison,
    PublicLibraryMatch,
    compare_publications,
)
from server.scholar import (
    InvalidScholarIdError,
    ScholarBlockedError,
    ScholarProfileNotFoundError,
    extract_scholar_id,
    filter_and_deduplicate,
    get_profile,
)


class PublicLibraryMatchResponse(BaseModel):
    status: str
    match_type: str
    confidence: float | None
    title: str
    year: int | None


class PublicationResponse(BaseModel):
    id: str
    title: str
    year: int
    authors: str
    venue: str
    detail_url: str
    duplicate_count: int
    public_library_match: PublicLibraryMatchResponse


def _public_library_match_response(
    match: PublicLibraryMatch,
) -> PublicLibraryMatchResponse:
    return PublicLibraryMatchResponse(
        status=match.status,
        match_type=match.match_type,
        confidence=match.confidence,
        title=match.title,
        year=match.year,
    )


class PublicationsResponse(BaseModel):
    scholar_id: str
    profile_name: str
    count: int
    publications: list[PublicationResponse]
    public_library_warning: str = ""


class PreparePublicationRequest(BaseModel):
    scholar_id: str = Field(min_length=1, max_length=300)
    publication_id: str = Field(min_length=1, max_length=300)


class PreparedMetadataResponse(BaseModel):
    source: str
    title: str
    authors: list[str]
    published_date: str
    publication_year: int | None
    container_title: str
    publisher: str
    volume: str
    issue: str
    pages: str
    doi: str
    isbn: str
    url: str
    raw_type: str
    output_type: str
    citation: str


class PreparePublicationResponse(BaseModel):
    form_url: str
    fields: dict[str, str]
    field_sources: dict[str, str]
    missing_fields: list[str]
    warnings: list[str]
    metadata: PreparedMetadataResponse
    match_source: str
    confidence: float | None


app = FastAPI(title="Auto Clever API", version="0.1.0")

allowed_origins = [
    origin.strip()
    for origin in os.getenv(
        "ALLOWED_ORIGINS",
        "https://chenglongma.com,http://localhost:8000,http://localhost:5173",
    ).split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


@app.get("/healthz")
async def health() -> dict[str, str]:
    return {"status": "ok", "time": datetime.now(UTC).isoformat()}


@app.get("/api/publications", response_model=PublicationsResponse)
async def publications(
    scholar_id: str = Query(min_length=1, max_length=300),
    from_year: int = Query(ge=1900, le=2100),
    to_year: int = Query(ge=1900, le=2100),
) -> PublicationsResponse:
    if from_year > to_year:
        raise HTTPException(status_code=422, detail="from_year must not exceed to_year")

    try:
        parsed_id = extract_scholar_id(scholar_id)
        profile = await get_profile(parsed_id)
    except InvalidScholarIdError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except ScholarProfileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ScholarBlockedError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except httpx.HTTPError as error:
        raise HTTPException(
            status_code=502, detail="Google Scholar request failed."
        ) from error

    filtered = filter_and_deduplicate(profile.publications, from_year, to_year)
    try:
        comparison = await compare_publications(profile.name, filtered)
    except Exception:
        comparison = PublicLibraryComparison(
            {item.id: PublicLibraryMatch() for item in filtered},
            "Public-library comparison is temporarily unavailable.",
        )

    match_order = {"none": 0, "possible": 1, "likely": 2}
    ordered = sorted(
        (item for item in filtered if item.year is not None),
        key=lambda item: (
            -(item.year or 0),
            match_order.get(
                comparison.matches.get(item.id, PublicLibraryMatch()).status, 0
            ),
            -(
                comparison.matches.get(item.id, PublicLibraryMatch()).confidence
                or 0
            ),
            item.title.casefold(),
        ),
    )
    items = [
        PublicationResponse(
            id=item.id,
            title=item.title,
            year=item.year,
            authors=item.authors,
            venue=item.venue,
            detail_url=item.detail_url,
            duplicate_count=item.duplicate_count,
            public_library_match=_public_library_match_response(
                comparison.matches.get(item.id, PublicLibraryMatch())
            ),
        )
        for item in ordered
    ]
    return PublicationsResponse(
        scholar_id=profile.scholar_id,
        profile_name=profile.name,
        count=len(items),
        publications=items,
        public_library_warning=comparison.warning,
    )


@app.post("/api/publications/prepare", response_model=PreparePublicationResponse)
async def prepare(request: PreparePublicationRequest) -> PreparePublicationResponse:
    try:
        parsed_id = extract_scholar_id(request.scholar_id)
        profile = await get_profile(parsed_id)
    except InvalidScholarIdError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except ScholarProfileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ScholarBlockedError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except httpx.HTTPError as error:
        raise HTTPException(
            status_code=502, detail="Google Scholar request failed."
        ) from error

    publication = next(
        (item for item in profile.publications if item.id == request.publication_id),
        None,
    )
    if publication is None:
        raise HTTPException(
            status_code=404,
            detail="The publication was not found in this Scholar profile.",
        )

    result = await prepare_publication(publication)
    metadata = result.metadata
    return PreparePublicationResponse(
        form_url=result.form_url,
        fields=result.fields,
        field_sources=result.field_sources,
        missing_fields=list(result.missing_fields),
        warnings=list(result.warnings),
        metadata=PreparedMetadataResponse(
            source=metadata.source,
            title=metadata.title,
            authors=list(metadata.authors),
            published_date=metadata.published_date,
            publication_year=metadata.publication_year,
            container_title=metadata.container_title,
            publisher=metadata.publisher,
            volume=metadata.volume,
            issue=metadata.issue,
            pages=metadata.pages,
            doi=metadata.doi,
            isbn=metadata.isbn,
            url=metadata.url,
            raw_type=metadata.raw_type,
            output_type=metadata.output_type,
            citation=metadata.citation,
        ),
        match_source=result.match_source,
        confidence=result.confidence,
    )
