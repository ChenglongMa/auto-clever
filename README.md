# Auto Clever

Small internal helper for preparing research outputs one at a time for the
ADM+S Clever Research Outputs form. It was inspired by
[clever_gs](https://github.com/ADMSCentre/clever_gs), but has its own query and
preparation implementation.

Search results retain every Google Scholar record. When the same output is likely
already in the ADM+S public library, the page shows an advisory badge; it never
removes the record or submits anything automatically. DOI equality is preferred,
with title, year, and author evidence used only when a DOI cannot be confirmed.

Start the API locally:

```sh
uv sync --dev
uv run uvicorn server.main:app --reload --port 8001
```

Serve `web/` with a local static HTTP server, then open it in a browser.
