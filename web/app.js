(() => {
  "use strict";

  const config = window.AUTO_CLEVER_CONFIG ?? {};
  const apiBaseUrl = String(config.apiBaseUrl ?? "").replace(/\/+$/, "");
  const requestTimeoutMs = Number(config.requestTimeoutMs) || 30000;
  const currentYear = new Date().getFullYear();
  let activeScholarId = "";

  const elements = {
    form: document.querySelector("#lookup-form"),
    scholarInput: document.querySelector("#scholar-input"),
    fromYear: document.querySelector("#from-year"),
    toYear: document.querySelector("#to-year"),
    searchButton: document.querySelector("#search-button"),
    status: document.querySelector("#query-status"),
    statusMessage: document.querySelector("#status-message"),
    results: document.querySelector("#results"),
    profileSummary: document.querySelector("#profile-summary"),
    resultCount: document.querySelector("#result-count"),
    emptyState: document.querySelector("#empty-state"),
    publicationList: document.querySelector("#publication-list"),
    publicationTemplate: document.querySelector("#publication-template"),
  };

  class ApiError extends Error {
    constructor(message, status) {
      super(message);
      this.name = "ApiError";
      this.status = status;
    }
  }

  initialiseForm();
  elements.form.addEventListener("submit", handleSubmit);

  function initialiseForm() {
    const query = new URLSearchParams(window.location.search);
    elements.scholarInput.value = query.get("scholar_id") ?? "";
    elements.fromYear.value = query.get("from_year") ?? String(currentYear - 5);
    elements.toYear.value = query.get("to_year") ?? String(currentYear);
  }

  async function handleSubmit(event) {
    event.preventDefault();
    clearStatus();

    const request = validateForm();
    if (!request) {
      return;
    }

    setLoading(true);
    elements.results.hidden = true;
    setStatus("loading", "Searching Scholar…");

    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), requestTimeoutMs);

    try {
      const query = new URLSearchParams({
        scholar_id: request.scholarId,
        from_year: String(request.fromYear),
        to_year: String(request.toYear),
      });
      const response = await fetch(`${apiBaseUrl}/api/publications?${query}`, {
        headers: { Accept: "application/json" },
        signal: controller.signal,
      });
      const payload = await readJson(response);

      if (!response.ok) {
        throw new ApiError(payload?.detail || "The search service returned an error.", response.status);
      }

      const publicLibraryWarning = renderResults(payload);
      rememberSuccessfulQuery(request);
      setStatus(
        "success",
        `Found ${payload.count} publications.${publicLibraryWarning ? ` ${publicLibraryWarning}` : ""}`,
      );
    } catch (error) {
      renderError(error);
    } finally {
      window.clearTimeout(timeout);
      setLoading(false);
    }
  }

  function validateForm() {
    const scholarId = elements.scholarInput.value.trim();
    const fromYear = Number(elements.fromYear.value);
    const toYear = Number(elements.toYear.value);

    if (!scholarId) {
      setStatus("error", "Enter a Google Scholar ID or profile URL.");
      elements.scholarInput.focus();
      return null;
    }

    if (!Number.isInteger(fromYear) || fromYear < 1900 || fromYear > 2100) {
      setStatus("error", "Enter a from year between 1900 and 2100.");
      elements.fromYear.focus();
      return null;
    }

    if (!Number.isInteger(toYear) || toYear < 1900 || toYear > 2100) {
      setStatus("error", "Enter a to year between 1900 and 2100.");
      elements.toYear.focus();
      return null;
    }

    if (fromYear > toYear) {
      setStatus("error", "The from year cannot be later than the to year.");
      elements.fromYear.focus();
      return null;
    }

    return { scholarId, fromYear, toYear };
  }

  function renderResults(payload) {
    const publications = Array.isArray(payload.publications) ? payload.publications : [];
    activeScholarId = String(payload.scholar_id ?? "");
    elements.publicationList.replaceChildren();
    elements.profileSummary.textContent = `${payload.profile_name} · Scholar ID: ${payload.scholar_id}`;
    elements.resultCount.textContent = String(publications.length);
    elements.resultCount.setAttribute("aria-label", `${publications.length} publications`);

    for (const publication of publications) {
      const fragment = elements.publicationTemplate.content.cloneNode(true);
      const item = fragment.querySelector(".publication-item");
      const yearBadge = fragment.querySelector(".year-badge");
      const duplicateBadge = fragment.querySelector(".duplicate-badge");
      const libraryMatchBadge = fragment.querySelector(".library-match-badge");
      const title = fragment.querySelector(".publication-title");
      const authors = fragment.querySelector(".publication-authors");
      const venue = fragment.querySelector(".publication-venue");
      const detailLink = fragment.querySelector(".detail-link");
      const prepareButton = fragment.querySelector(".prepare-button");
      const prepareLabel = fragment.querySelector(".prepare-label");
      const openedBadge = fragment.querySelector(".opened-badge");
      const feedback = fragment.querySelector(".publication-feedback");
      const feedbackMessage = fragment.querySelector(".feedback-message");
      const manualFields = fragment.querySelector(".manual-fields");
      const fallbackLink = fragment.querySelector(".fallback-link");

      item.dataset.publicationId = String(publication.id ?? "");
      yearBadge.textContent = String(publication.year ?? "Year unknown");
      title.textContent = publication.title || "Untitled publication";
      setOptionalText(authors, publication.authors);
      setOptionalText(venue, publication.venue);

      if (Number(publication.duplicate_count) > 1) {
        duplicateBadge.hidden = false;
        duplicateBadge.textContent = `${publication.duplicate_count} duplicate records merged`;
      }
      renderPublicLibraryMatch(publication.public_library_match, libraryMatchBadge);

      const detailUrl = getScholarDetailUrl(publication.detail_url);
      if (detailUrl) {
        detailLink.href = detailUrl;
      } else {
        detailLink.hidden = true;
      }

      const prepareUi = {
        button: prepareButton,
        label: prepareLabel,
        openedBadge,
        feedback,
        feedbackMessage,
        manualFields,
        fallbackLink,
      };
      if (wasOpened(activeScholarId, publication.id)) {
        showOpenedState(prepareUi);
      }
      prepareButton.addEventListener("click", () =>
        handlePrepare(publication, prepareUi),
      );

      elements.publicationList.append(fragment);
    }

    elements.emptyState.hidden = publications.length !== 0;
    elements.results.hidden = false;
    elements.results.scrollIntoView({ behavior: "smooth", block: "start" });
    return String(payload.public_library_warning ?? "").trim();
  }

  async function handlePrepare(publication, ui) {
    const scholarId = activeScholarId;
    resetPreparationFeedback(ui);
    setPrepareLoading(ui, true);
    const cleverTab = openPreparingTab();
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), requestTimeoutMs);

    try {
      const response = await fetch(`${apiBaseUrl}/api/publications/prepare`, {
        method: "POST",
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          scholar_id: scholarId,
          publication_id: publication.id,
        }),
        signal: controller.signal,
      });
      const payload = await readJson(response);
      if (!response.ok) {
        throw new ApiError(payload?.detail || "This publication could not be prepared.", response.status);
      }

      const formUrl = getCleverFormUrl(payload?.form_url);
      if (!formUrl) {
        throw new Error("The server returned an invalid Clever URL.");
      }

      markOpened(scholarId, publication.id);
      showOpenedState(ui);
      renderPreparationResult(ui, payload, Boolean(cleverTab));

      if (cleverTab) {
        cleverTab.location.replace(formUrl);
      } else {
        ui.fallbackLink.href = formUrl;
        ui.fallbackLink.hidden = false;
      }
    } catch (error) {
      if (cleverTab && !cleverTab.closed) {
        cleverTab.close();
      }
      renderPreparationError(ui, error);
    } finally {
      window.clearTimeout(timeout);
      setPrepareLoading(ui, false);
    }
  }

  function openPreparingTab() {
    const tab = window.open("", "_blank");
    if (!tab) {
      return null;
    }
    tab.opener = null;
    tab.document.title = "Preparing…";
    tab.document.body.textContent = "Preparing publication…";
    return tab;
  }

  function getCleverFormUrl(value) {
    try {
      const url = new URL(String(value));
      if (
        url.protocol === "https:" &&
        url.hostname === "creatorapp.zohopublic.com.au" &&
        url.pathname.includes("/form-perma/Research_outputs/")
      ) {
        return url.href;
      }
    } catch {
      return null;
    }
    return null;
  }

  function renderPreparationResult(ui, payload, tabWasOpened) {
    const missingFields = Array.isArray(payload.missing_fields)
      ? payload.missing_fields
      : [];
    const warnings = Array.isArray(payload.warnings) ? payload.warnings : [];

    ui.feedback.hidden = false;
    ui.feedback.dataset.state = warnings.length ? "warning" : "success";
    if (!tabWasOpened) {
      ui.feedbackMessage.textContent = "Pop-up blocked. Use the link below.";
    } else if (warnings.length) {
      ui.feedbackMessage.textContent = "Opened with limited metadata.";
    } else {
      ui.feedbackMessage.textContent = "Opened for review.";
    }

    if (missingFields.length) {
      ui.manualFields.hidden = false;
      ui.manualFields.querySelector("summary").textContent = `${missingFields.length} fields to complete`;
      const list = ui.manualFields.querySelector("ul");
      list.replaceChildren();
      for (const field of missingFields) {
        const item = document.createElement("li");
        item.textContent = String(field);
        list.append(item);
      }
    }
  }

  function renderPreparationError(ui, error) {
    ui.feedback.hidden = false;
    ui.feedback.dataset.state = "error";
    if (error instanceof DOMException && error.name === "AbortError") {
      ui.feedbackMessage.textContent = "Timed out. Try again.";
    } else if (error instanceof ApiError) {
      ui.feedbackMessage.textContent = error.message;
    } else {
      ui.feedbackMessage.textContent = "Couldn’t prepare this record. Try again.";
    }
  }

  function resetPreparationFeedback(ui) {
    ui.feedback.hidden = true;
    ui.feedback.removeAttribute("data-state");
    ui.feedbackMessage.textContent = "";
    ui.manualFields.hidden = true;
    ui.manualFields.open = false;
    ui.manualFields.querySelector("ul").replaceChildren();
    ui.fallbackLink.hidden = true;
    ui.fallbackLink.removeAttribute("href");
  }

  function setPrepareLoading(ui, isLoading) {
    ui.button.disabled = isLoading;
    ui.button.dataset.loading = String(isLoading);
    ui.button.setAttribute("aria-busy", String(isLoading));
    if (isLoading) {
      ui.label.textContent = "Preparing…";
    } else {
      ui.label.textContent = ui.openedBadge.hidden ? "Open in Clever" : "Open again";
    }
  }

  function showOpenedState(ui) {
    ui.openedBadge.hidden = false;
    ui.label.textContent = "Open again";
  }

  function openedStorageKey(scholarId, publicationId) {
    return `auto-clever:opened:${scholarId}:${publicationId}`;
  }

  function wasOpened(scholarId, publicationId) {
    try {
      return Boolean(window.localStorage.getItem(openedStorageKey(scholarId, publicationId)));
    } catch {
      return false;
    }
  }

  function markOpened(scholarId, publicationId) {
    try {
      window.localStorage.setItem(
        openedStorageKey(scholarId, publicationId),
        new Date().toISOString(),
      );
    } catch {
      // Opening the form must still work when storage is unavailable.
    }
  }

  function setOptionalText(element, value) {
    const text = String(value ?? "").trim();
    element.textContent = text;
    element.hidden = !text;
  }

  function renderPublicLibraryMatch(match, badge) {
    const status = String(match?.status ?? "");
    const matchType = String(match?.match_type ?? "");
    if (status === "likely") {
      badge.hidden = false;
      badge.textContent = matchType === "doi"
        ? "DOI match"
        : "Title match";
    } else if (status === "possible") {
      badge.hidden = false;
      badge.textContent = "Possible match";
    }
  }

  function getScholarDetailUrl(value) {
    try {
      const url = new URL(String(value));
      if (
        url.protocol === "https:" &&
        ["scholar.google.com", "scholar.google.com.au"].includes(url.hostname)
      ) {
        return url.href;
      }
    } catch {
      return null;
    }
    return null;
  }

  function rememberSuccessfulQuery(request) {
    const url = new URL(window.location.href);
    url.searchParams.set("scholar_id", request.scholarId);
    url.searchParams.set("from_year", String(request.fromYear));
    url.searchParams.set("to_year", String(request.toYear));
    window.history.replaceState(null, "", url);
  }

  function setLoading(isLoading) {
    elements.searchButton.disabled = isLoading;
    elements.searchButton.dataset.loading = String(isLoading);
    elements.searchButton.setAttribute("aria-busy", String(isLoading));
  }

  function setStatus(type, message) {
    elements.status.hidden = false;
    elements.status.dataset.state = type;
    elements.statusMessage.textContent = message;
  }

  function clearStatus() {
    elements.status.hidden = true;
    elements.status.removeAttribute("data-state");
    elements.statusMessage.textContent = "";
  }

  function renderError(error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      setStatus("error", "The search timed out. Try again shortly or narrow the year range.");
      return;
    }

    if (error instanceof ApiError) {
      const messages = {
        404: "This Scholar profile was not found. Check the ID and make sure the profile is public.",
        422: "The Scholar ID or year range is not valid. Check the values and try again.",
        502: "Google Scholar cannot be reached right now. Try again shortly.",
        503: "Google Scholar requested human verification or temporarily limited access. Try again later.",
      };
      setStatus("error", messages[error.status] || error.message);
      return;
    }

    setStatus("error", "The search service cannot be reached. Check that the server is running and try again.");
  }

  async function readJson(response) {
    try {
      return await response.json();
    } catch {
      return null;
    }
  }
})();
