from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import urlencode

CLEVER_FORM_URL = (
    "https://creatorapp.zohopublic.com.au/admscentre/adms-cle-v-er/"
    "form-perma/Research_outputs/"
    "KJW9qMxRAwk5ebQ17EmWeeazRYXE6bC6CW2dOU2NzvDpN1fAWzy1XdGNzmE3fUa7YCNvhV2YHt3uGahexN66XZNZKjttAsnXUmCN"
)

PREFILLABLE_FIELDS = frozenset(
    {
        "Output_type1",
        "Date_published",
        "Title",
        "Citation1",
        "Unique_Identifier1",
        "Output_URL",
    }
)

REQUIRED_FIELD_LABELS = {
    "Output_type1": "Output type",
    "Date_published": "Date published",
    "Citation1": "Citation",
    "Unique_Identifier1": "DOI, ISBN, or Unique Identifier",
    "Has_the_ARC_been_acknowledged": "Has the ARC been appropriately acknowledged?",
    "Has_the_output_been_made_openly_accessible": (
        "Has the output been made openly accessible?"
    ),
    "Author_s": "Member(s) involved",
    "Which_nodes_were_involved": "Node(s) involved",
    "Associated_project_s": "Associated Research Projects",
    "Was_this_added_by_RMIT_admin": "Was this added by RMIT admin?",
}


def build_form_url(fields: Mapping[str, str]) -> str:
    safe_fields = {
        name: value.strip()
        for name, value in fields.items()
        if name in PREFILLABLE_FIELDS and value.strip()
    }
    if not safe_fields:
        return CLEVER_FORM_URL
    return f"{CLEVER_FORM_URL}?{urlencode(safe_fields)}"


def list_missing_required_fields(fields: Mapping[str, str]) -> list[str]:
    return [
        label
        for name, label in REQUIRED_FIELD_LABELS.items()
        if not fields.get(name, "").strip()
    ]
