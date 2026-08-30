importScripts("url-utils.js");

const ALARM_NAME = "deal-scout-scan";
const TARGETS_VERSION = 6;

// Verified live 2026-08-28: Facebook IGNORES the /marketplace/<city>/ slug and
// redirects to /marketplace/category/search/, resolving location from
// lat/long+radius (or the account default). Five city-slug targets would have
// been five identical Columbia searches. `radius` is in KILOMETERS - 200km
// (~124mi) from Columbia covers Charlotte, Charleston, Augusta and Greenville
// in ONE tab per query instead of one per city.
// Radius corrected 2026-08-30: 200km (124mi) reached Charlotte (85mi away),
// which the user explicitly does NOT want to drive for golf clubs - "all of
// the golf stuff is in charlotte, im in columbia". 65km (~40mi) keeps the
// actual Columbia metro (Lexington, Irmo, Chapin, West Columbia, Elgin,
// Batesburg-Leesville - every listing that was actually verified as good
// tonight was in this ring) and drops Charlotte/Charleston/Augusta/Greenville
// entirely. Widen this again only if the user explicitly says they'll drive
// further - don't re-guess it.
const GOLF_ORIGIN = { latitude: 34.0007, longitude: -81.0348, radius: 65 };
// These strings MUST each match a saved search in the bot's config.json
// (compared after exclusion-stripping). A query with no matching saved
// search makes every listing it finds defer forever with "did not match
// an enabled saved search" - confirmed live 2026-08-28 by "golf clubs bag"
// vs the config's "golf bag clubs". Bump TARGETS_VERSION when editing.
const GOLF_QUERIES = [
  "golf club set",
  "complete golf set",
  "mens golf clubs",
  "golf irons set",
  "golf bag clubs",
  // Poorly-titled listings are where local mispricing actually lives: a seller
  // who writes "TaylorMade M4" has already priced to market, while "golf clubs
  // and bag" is the guy clearing out a garage who never looked up what he has.
  // These deliberately match nothing specific - the value is in the vagueness.
  "golf clubs and bag",
  "mens golf set used",
  "full golf set",
  "golf clubs garage"
];

const DEFAULT_TARGETS = GOLF_QUERIES.map((query) => ({
  label: `${query} — Columbia +124mi`,
  platform: "facebook",
  // newest-first so a 10-min alarm sees fresh posts, not page-1 staleness
  searchUrl: "https://www.facebook.com/marketplace/category/search/"
    + `?query=${encodeURIComponent(query)}&maxPrice=350&sortBy=creation_time_descend`
    + `&latitude=${GOLF_ORIGIN.latitude}&longitude=${GOLF_ORIGIN.longitude}&radius=${GOLF_ORIGIN.radius}`,
  parser: "facebook-json",
  enabled: true
}));

let lastRunStatus = { startedAt: null, finishedAt: null, running: false, targets: {} };

const sleep = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));
const syncGet = (keys) => new Promise((resolve) => chrome.storage.sync.get(keys, resolve));
const localGet = (keys) => new Promise((resolve) => chrome.storage.local.get(keys, resolve));

async function initialize() {
  const { watchTargets, targetsVersion } = await syncGet(["watchTargets", "targetsVersion"]);
  // Seed on first run, and re-seed when DEFAULT_TARGETS itself changes -
  // without the version gate a shipped target list can never reach an
  // install that already has the old one in storage.sync.
  if (!Array.isArray(watchTargets) || (targetsVersion || 0) < TARGETS_VERSION) {
    await chrome.storage.sync.set({ watchTargets: DEFAULT_TARGETS, targetsVersion: TARGETS_VERSION });
  }
  await chrome.alarms.create(ALARM_NAME, { periodInMinutes: 10 });
}

chrome.runtime.onInstalled.addListener(initialize);
chrome.runtime.onStartup.addListener(initialize);

// Chrome refuses tab mutations while the tab strip is in a transient state and
// throws "Tabs cannot be edited right now (user may be dragging a tab)". It is
// transient by definition, and the odds of hitting it scale with how many tabs
// a scan opens - going from 5 targets to 9 made it show up on every run. Retry
// with backoff rather than failing the whole target over a timing collision.
const TAB_BUSY_RE = /cannot be edited right now|dragging a tab/i;

async function withTabRetry(action, attempts = 4) {
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    try {
      return await action();
    } catch (error) {
      const transient = TAB_BUSY_RE.test(error?.message || "");
      if (!transient || attempt === attempts - 1) throw error;
      await sleep(750 * (attempt + 1));
    }
  }
}

function waitForTab(tabId, timeoutMs = 30000) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => finish(new Error("Timed out waiting for page load")), timeoutMs);
    const listener = (changedId, changeInfo) => {
      if (changedId === tabId && changeInfo.status === "complete") finish();
    };
    function finish(error) {
      clearTimeout(timer);
      chrome.tabs.onUpdated.removeListener(listener);
      error ? reject(error) : resolve();
    }
    chrome.tabs.onUpdated.addListener(listener);
    chrome.tabs.get(tabId, (tab) => {
      if (chrome.runtime.lastError) finish(new Error(chrome.runtime.lastError.message));
      else if (tab.status === "complete") finish();
    });
  });
}

async function scanTarget(target) {
  if (!target || typeof target !== "object" || typeof target.searchUrl !== "string") throw new Error("Invalid watch target");
  const targetUrl = DealScoutUrls.normalizeUrl(target.searchUrl);
  let tab;
  let createdWindowId = null;
  try {
    // chrome.tabs.create with no windowId targets the "current" window, and
    // an alarm-driven service worker often has none (all windows closed, or
    // only a devtools/app window open) - which throws "No current window"
    // and silently fails every scheduled scan. Pick a window explicitly.
    const windows = await chrome.windows.getAll({ windowTypes: ["normal"] });
    let windowId = windows[0]?.id;
    if (windowId === undefined) {
      const created = await chrome.windows.create({ focused: false, state: "minimized" });
      windowId = createdWindowId = created.id;
    }
    tab = await withTabRetry(() => chrome.tabs.create({ url: targetUrl, active: false, windowId }));
    await waitForTab(tab.id);
    await sleep(2500);
    const file = target.parser === "generic-og"
      ? "content-scripts/generic-og-parser.js"
      : "content-scripts/facebook-parser.js";
    const results = await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: [file] });
    const extracted = Array.isArray(results?.[0]?.result) ? results[0].result : [];
    return extracted.map((listing) => ({ ...listing, platform: target.platform }));
  } finally {
    if (tab?.id) await withTabRetry(() => chrome.tabs.remove(tab.id)).catch(() => {});
    // only tear down a window we opened ourselves - never the user's
    if (createdWindowId !== null) await chrome.windows.remove(createdWindowId).catch(() => {});
  }
}

async function postListings(endpointUrl, secret, listings) {
  if (!listings.length) return { accepted: 0, dropped: 0 };
  const endpoint = DealScoutUrls.normalizeUrl(endpointUrl, { requireHttps: true });
  const response = await fetch(endpoint, {
    method: "POST",
    headers: { "Content-Type": "application/json", "x-scout-secret": secret },
    body: JSON.stringify({ listings })
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.error || `Ingest returned HTTP ${response.status}`);
  return body;
}

async function runScan() {
  if (lastRunStatus.running) return lastRunStatus;
  lastRunStatus = { startedAt: new Date().toISOString(), finishedAt: null, running: true, targets: {} };
  const [{ watchTargets: storedTargets }, { ingestEndpoint = "", scoutSecret = "" }] = await Promise.all([
    syncGet(["watchTargets"]), localGet(["ingestEndpoint", "scoutSecret"])
  ]);
  const watchTargets = Array.isArray(storedTargets) ? storedTargets : DEFAULT_TARGETS;
  if (!ingestEndpoint || !scoutSecret) {
    lastRunStatus.running = false;
    lastRunStatus.finishedAt = new Date().toISOString();
    lastRunStatus.error = "Configure the ingest endpoint and secret in the popup.";
    return lastRunStatus;
  }

  for (const [index, target] of watchTargets.entries()) {
    if (!target || typeof target !== "object") {
      lastRunStatus.targets[`${index}:Invalid target`] = { ok: false, error: "Stored watch target is invalid" };
      continue;
    }
    if (!target.enabled) continue;
    const key = `${index}:${target.label || "Unnamed target"}`;
    try {
      // A short gap between targets keeps consecutive create/remove pairs from
      // landing inside the same transient tab-strip state.
      if (index > 0) await sleep(1500);
      const listings = await scanTarget(target);
      const ingest = await postListings(ingestEndpoint, scoutSecret, listings);
      lastRunStatus.targets[key] = { ok: true, found: listings.length, accepted: ingest.accepted || 0, dropped: ingest.dropped || 0 };
      console.info("Deal Scout target complete", target.label, lastRunStatus.targets[key]);
    } catch (error) {
      lastRunStatus.targets[key] = { ok: false, error: error.message };
      console.error("Deal Scout target failed", target.label, error);
    }
  }
  lastRunStatus.running = false;
  lastRunStatus.finishedAt = new Date().toISOString();
  return lastRunStatus;
}

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === ALARM_NAME) runScan().catch((error) => console.error("Deal Scout alarm failed", error));
});

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type === "scan-now") {
    runScan().then(sendResponse);
    return true;
  }
  if (message?.type === "get-status") sendResponse(lastRunStatus);
  return false;
});

initialize().catch((error) => console.error("Deal Scout initialization failed", error));
