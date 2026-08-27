from pathlib import Path

from bs4 import BeautifulSoup

WEB_ROOT = Path(__file__).resolve().parents[1]


def _document() -> BeautifulSoup:
    return BeautifulSoup((WEB_ROOT / "index.html").read_text(), "html.parser")


def test_page_has_required_landmarks_and_controls() -> None:
    document = _document()

    assert document.html["lang"] == "en"
    assert document.select_one("main") is not None
    assert document.select_one("#lookup-form") is not None
    assert document.select_one("#scholar-input") is not None
    assert document.select_one("#from-year") is not None
    assert document.select_one("#to-year") is not None
    assert document.select_one("#query-status[aria-live]") is not None
    assert document.select_one("#publication-template") is not None


def test_form_controls_have_explicit_labels() -> None:
    document = _document()

    for control in document.select("#lookup-form input[id]"):
        assert document.select_one(f'label[for="{control["id"]}"]') is not None


def test_local_assets_use_relative_existing_paths() -> None:
    document = _document()
    references = [
        element.get(attribute)
        for selector, attribute in (("link[href]", "href"), ("script[src]", "src"))
        for element in document.select(selector)
    ]

    assert references == ["./styles.css", "./config.js", "./app.js"]
    for reference in references:
        assert not reference.startswith("/")
        assert (WEB_ROOT / reference.removeprefix("./")).is_file()


def test_untrusted_api_values_are_not_rendered_as_html() -> None:
    script = (WEB_ROOT / "app.js").read_text()

    assert ".innerHTML" not in script
    assert "textContent" in script


def test_prepare_button_and_review_feedback_are_available() -> None:
    document = _document()
    button = document.select_one("#publication-template .prepare-button")

    assert button is not None
    assert not button.has_attr("disabled")
    assert "Open in Clever" in str(button)
    assert document.select_one("#publication-template .library-match-badge") is not None
    assert document.select_one("#publication-template .publication-feedback") is not None
    assert document.select_one("#publication-template .manual-fields") is not None


def test_prepare_flow_opens_a_tab_and_records_only_opened_state() -> None:
    script = (WEB_ROOT / "app.js").read_text()

    assert 'window.open("", "_blank")' in script
    assert 'method: "POST"' in script
    assert "/api/publications/prepare" in script
    assert "window.localStorage.setItem" in script
    assert "Opened" in (WEB_ROOT / "index.html").read_text()
    assert "Submitted" not in (WEB_ROOT / "index.html").read_text()
