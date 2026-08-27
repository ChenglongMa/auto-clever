from urllib.parse import parse_qs, urlparse

from server.clever import CLEVER_FORM_URL, build_form_url, list_missing_required_fields


def test_build_form_url_only_includes_supported_non_empty_fields() -> None:
    form_url = build_form_url(
        {
            "Title": "Research & society / 研究",
            "Date_published": "19-10-2024",
            "Output_URL": "",
            "Author_s": "Must not be prefilled by display name",
        }
    )

    parsed = urlparse(form_url)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == CLEVER_FORM_URL
    assert parse_qs(parsed.query) == {
        "Title": ["Research & society / 研究"],
        "Date_published": ["19-10-2024"],
    }


def test_empty_fields_return_plain_form_and_required_manual_fields() -> None:
    assert build_form_url({}) == CLEVER_FORM_URL

    missing = list_missing_required_fields({"Citation1": "A citation"})

    assert "Citation" not in missing
    assert "Output type" in missing
    assert "Member(s) involved" in missing
    assert "Associated Research Projects" in missing
