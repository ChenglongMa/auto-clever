from __future__ import annotations

import html
import re
from urllib.parse import unquote

DOI_PATTERN = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)


def normalise_doi(value: object) -> str:
    """Return a DOI from a citation, DOI label, or DOI URL."""
    if value is None:
        return ""
    candidate = unquote(html.unescape(str(value))).replace("\u200b", "")
    match = DOI_PATTERN.search(candidate)
    return match.group(0).rstrip(".,;:)").lower() if match else ""
