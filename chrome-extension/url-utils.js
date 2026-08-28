(function (root) {
  function normalizeUrl(rawUrl, { requireHttps = false } = {}) {
    const url = new URL(rawUrl);
    const allowed = requireHttps ? url.protocol === "https:" : ["http:", "https:"].includes(url.protocol);
    if (!allowed || url.username || url.password) {
      throw new Error(requireHttps ? "An HTTPS URL without embedded credentials is required." : "An HTTP(S) URL without embedded credentials is required.");
    }
    return url.toString();
  }

  const api = { normalizeUrl };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.DealScoutUrls = api;
}(typeof globalThis !== "undefined" ? globalThis : this));
