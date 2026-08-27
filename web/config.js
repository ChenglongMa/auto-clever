const localHosts = new Set(["localhost", "127.0.0.1"]);

window.AUTO_CLEVER_CONFIG = Object.freeze({
  apiBaseUrl: localHosts.has(window.location.hostname)
    ? "http://localhost:8001"
    : "https://auto-clever-api.handbooks.cc",
  requestTimeoutMs: 30000,
});
