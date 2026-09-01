importScripts("url-utils.js");

const ALARM_NAME = "deal-scout-scan";
const TARGETS_VERSION = 6;
const RETRY_QUEUE_KEY = "scoutRetryQueue";
const FAILURE_STATE_KEY = "scoutIngestFailures";
const MAX_INGEST_ATTEMPTS = 5;
const DEFAULT_RETRY_DELAY_MS = 10 * 60 * 1000;
const MAX_FAILURE_RECORDS = 100;
// chrome.storage.local has a 10MB quota (only "storage" permission, no
// unlimitedStorage) - neither of these grew a bound before. Oldest-first
// eviction once past the cap; simple array-length cap, not a byte-measuring
// system.
const MAX_RETRY_QUEUE_BATCHES = 300;
const MAX_ABANDONED_KEYS = 2000;

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

// Stable per-default id, independent of array order/text tweaks - the
// migration merge below matches on this, never on array position, so
// reordering/rewording GOLF_QUERIES can't misattribute a user's overrides.
const slugify = (value) => String(value).toLowerCase().trim().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");

const DEFAULT_TARGETS = GOLF_QUERIES.map((query) => ({
  id: `facebook-golf-${slugify(query)}`,
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
const localSet = (values) => new Promise((resolve, reject) => {
  chrome.storage.local.set(values, () => {
    if (chrome.runtime.lastError) reject(new Error(chrome.runtime.lastError.message));
    else resolve();
  });
});

// Merge shipped defaults into the user's live target collection instead of
// replacing it. `defaultsVersion` (schema version of DEFAULT_TARGETS) is
// stored separately from `watchTargets` (the user's actual, editable data)
// precisely so this same merge runs correctly on every future bump too.
//
// For each shipped default: if the user already has a record with that
// default's id, update its fields but keep the user's own enabled/disabled
// choice. If the id is new (never shipped before), add it. If the id was
// shipped in a prior version but the user's collection no longer has it,
// that means the user deleted it - leave it deleted, don't resurrect it.
// Any record that isn't a recognized default id (user-created) is left
// completely untouched.
//
// `defaultsSeenIds` is what lets us tell "new default" apart from "user
// deleted this one" - it's the full set of default ids that have ever been
// shipped, refreshed on every merge.
function mergeDefaultTargets(existing, previouslySeenIds) {
  const byId = new Map();
  // One-time bootstrap for installs upgrading from before defaults had
  // stable ids: match by label (the only stable identity those records
  // have) so an already-customized legacy default gets upgraded in place
  // instead of duplicated.
  const byLegacyLabel = new Map();
  for (const record of existing) {
    if (!record || typeof record !== "object") continue;
    if (typeof record.id === "string") byId.set(record.id, record);
    else if (typeof record.label === "string") byLegacyLabel.set(record.label, record);
  }

  const merged = existing.slice();
  for (const def of DEFAULT_TARGETS) {
    const current = byId.get(def.id) || byLegacyLabel.get(def.label);
    if (current) {
      merged[merged.indexOf(current)] = { ...def, enabled: current.enabled };
    } else if (!previouslySeenIds.has(def.id)) {
      merged.push({ ...def });
    }
  }
  return merged;
}

async function initialize() {
  const { watchTargets, defaultsVersion, defaultsSeenIds } = await syncGet([
    "watchTargets", "defaultsVersion", "defaultsSeenIds"
  ]);
  const seenIds = DEFAULT_TARGETS.map((target) => target.id);
  if (!Array.isArray(watchTargets)) {
    // First run: nothing to merge against, seed straight from defaults.
    await chrome.storage.sync.set({ watchTargets: DEFAULT_TARGETS, defaultsVersion: TARGETS_VERSION, defaultsSeenIds: seenIds });
  } else if ((defaultsVersion || 0) < TARGETS_VERSION) {
    const merged = mergeDefaultTargets(watchTargets, new Set(Array.isArray(defaultsSeenIds) ? defaultsSeenIds : []));
    await chrome.storage.sync.set({ watchTargets: merged, defaultsVersion: TARGETS_VERSION, defaultsSeenIds: seenIds });
  }
  await chrome.alarms.create(ALARM_NAME, { periodInMinutes: 10 });
  await updateUnsentBadge();
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

// A parser that returns [] because the query genuinely has no hits looks
// identical to one that returned [] because Facebook bounced the tab to a
// login/checkpoint/consent wall - unless we also look at where the tab
// actually ended up. Lightweight post-load URL check, not a full DOM signal.
const FACEBOOK_BLOCKED_PATH_RE = /\/(login|checkpoint|recover|two_step_verification|confirmemail|consent)(?:[/?]|$)/i;

async function scanTarget(target) {
  if (!target || typeof target !== "object" || typeof target.searchUrl !== "string") throw new Error("Invalid watch target");
  const targetUrl = DealScoutUrls.normalizeUrl(target.searchUrl);
  const isFacebookParser = target.parser !== "generic-og";
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
    if (isFacebookParser) {
      const finalTab = await chrome.tabs.get(tab.id).catch(() => null);
      let finalPath = "";
      try { finalPath = new URL(finalTab?.url || "").pathname; } catch (_error) { /* leave blank */ }
      if (FACEBOOK_BLOCKED_PATH_RE.test(finalPath)) {
        throw new Error(`Facebook redirected to a login/checkpoint page (${finalPath}) - session likely logged out`);
      }
    }
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

function retryDelayMs(response, body) {
  const retryAfter = response.headers?.get?.("Retry-After");
  if (retryAfter) {
    const seconds = Number(retryAfter);
    if (Number.isFinite(seconds) && seconds >= 0) return seconds * 1000;
    const dateDelay = Date.parse(retryAfter) - Date.now();
    if (Number.isFinite(dateDelay) && dateDelay > 0) return dateDelay;
  }
  const bodySeconds = Number(body?.retryAfterSeconds);
  return Number.isFinite(bodySeconds) && bodySeconds >= 0
    ? bodySeconds * 1000
    : DEFAULT_RETRY_DELAY_MS;
}

const POST_TIMEOUT_MS = 20000;

async function postListings(endpointUrl, secret, listings) {
  if (!listings.length) return { accepted: 0, dropped: 0 };
  const endpoint = DealScoutUrls.normalizeUrl(endpointUrl, { requireHttps: true });
  // A stalled fetch (retry POST or live-scan POST - this is shared by both)
  // must not be able to hang indefinitely and block everything behind it.
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), POST_TIMEOUT_MS);
  let response;
  try {
    response = await fetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json", "x-scout-secret": secret },
      body: JSON.stringify({ listings }),
      signal: controller.signal
    });
  } catch (error) {
    if (error.name === "AbortError") throw new Error(`Ingest request timed out after ${POST_TIMEOUT_MS}ms`);
    throw error;
  } finally {
    clearTimeout(timer);
  }
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(body.error || `Ingest returned HTTP ${response.status}`);
    error.status = response.status;
    error.retryAfterMs = retryDelayMs(response, body);
    throw error;
  }
  return body;
}

const retryListingKey = (listing) => {
  if (!listing || typeof listing.platform !== "string" || typeof listing.itemId !== "string") return null;
  return `${listing.platform.trim().toLowerCase()}:${listing.itemId.trim()}`;
};

function normalizeFailureState(value) {
  const state = value && typeof value === "object" ? value : {};
  return {
    records: Array.isArray(state.records) ? state.records.slice(-MAX_FAILURE_RECORDS) : [],
    abandonedKeys: Array.isArray(state.abandonedKeys) ? state.abandonedKeys.filter((key) => typeof key === "string") : [],
    abandonedCount: Number.isInteger(state.abandonedCount) && state.abandonedCount >= 0 ? state.abandonedCount : 0,
    pendingCount: 0,
    unsentCount: 0,
    updatedAt: state.updatedAt || null
  };
}

async function readRetryState() {
  const stored = await localGet([RETRY_QUEUE_KEY, FAILURE_STATE_KEY]);
  return {
    queue: Array.isArray(stored[RETRY_QUEUE_KEY])
      ? stored[RETRY_QUEUE_KEY].filter((entry) => entry && Array.isArray(entry.listings) && entry.listings.length)
      : [],
    failures: normalizeFailureState(stored[FAILURE_STATE_KEY])
  };
}

function appendFailureRecord(failures, record) {
  failures.records.push({ at: new Date().toISOString(), ...record });
  failures.records = failures.records.slice(-MAX_FAILURE_RECORDS);
}

function countPendingListings(queue) {
  const keys = new Set();
  let unkeyed = 0;
  for (const entry of queue) {
    for (const listing of entry.listings) {
      const key = retryListingKey(listing);
      if (key) keys.add(key);
      else unkeyed += 1;
    }
  }
  return keys.size + unkeyed;
}

async function setUnsentBadge(unsentCount, hasAbandoned) {
  const badgeText = unsentCount > 999 ? "999+" : (unsentCount ? String(unsentCount) : "");
  await Promise.all([
    chrome.action.setBadgeText({ text: badgeText }),
    chrome.action.setBadgeBackgroundColor({ color: hasAbandoned ? "#b3261e" : "#b06000" }),
    chrome.action.setTitle({
      title: unsentCount
        ? `Deal Scout: ${unsentCount} listing${unsentCount === 1 ? "" : "s"} unsent`
        : "Deal Scout"
    })
  ]);
}

async function writeRetryState(queue, failures) {
  // Drop oldest batches/keys past the cap rather than growing forever.
  if (queue.length > MAX_RETRY_QUEUE_BATCHES) queue = queue.slice(queue.length - MAX_RETRY_QUEUE_BATCHES);
  if (failures.abandonedKeys.length > MAX_ABANDONED_KEYS) {
    failures.abandonedKeys = failures.abandonedKeys.slice(failures.abandonedKeys.length - MAX_ABANDONED_KEYS);
  }
  failures.pendingCount = countPendingListings(queue);
  failures.unsentCount = failures.pendingCount + failures.abandonedCount;
  failures.updatedAt = new Date().toISOString();
  await localSet({ [RETRY_QUEUE_KEY]: queue, [FAILURE_STATE_KEY]: failures });
  await setUnsentBadge(failures.unsentCount, failures.abandonedCount > 0);
}

async function updateUnsentBadge() {
  const { queue, failures } = await readRetryState();
  await writeRetryState(queue, failures);
}

function retryBatchId() {
  return globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

async function persistFailedListings(listings, error) {
  if (!listings.length) return 0;
  const { queue, failures } = await readRetryState();
  const queuedKeys = new Set(queue.flatMap((entry) => entry.listings.map(retryListingKey).filter(Boolean)));
  const abandonedKeys = new Set(failures.abandonedKeys);
  const retryListings = listings.filter((listing) => {
    const key = retryListingKey(listing);
    if (!key) return true;
    if (queuedKeys.has(key) || abandonedKeys.has(key)) return false;
    queuedKeys.add(key);
    return true;
  });
  const batchId = retryBatchId();
  if (retryListings.length) {
    queue.push({
      id: batchId,
      listings: retryListings,
      attempts: 1,
      createdAt: new Date().toISOString(),
      nextAttemptAt: new Date(Date.now() + (error.retryAfterMs || DEFAULT_RETRY_DELAY_MS)).toISOString(),
      lastError: String(error.message || error).slice(0, 500),
      lastStatus: error.status || null
    });
  }
  appendFailureRecord(failures, {
    batchId,
    attempt: 1,
    status: error.status || null,
    listingCount: listings.length,
    queuedCount: retryListings.length,
    outcome: retryListings.length ? "queued" : "already-pending-or-abandoned",
    error: String(error.message || error).slice(0, 500)
  });
  await writeRetryState(queue, failures);
  return retryListings.length;
}

async function retryPendingListings(endpointUrl, secret) {
  const { queue, failures } = await readRetryState();
  const retained = [];
  const summary = { attempted: 0, delivered: 0, pending: 0, abandoned: 0 };
  const now = Date.now();

  for (const entry of queue) {
    const nextAttemptAt = Date.parse(entry.nextAttemptAt || 0);
    if (Number.isFinite(nextAttemptAt) && nextAttemptAt > now) {
      retained.push(entry);
      continue;
    }
    summary.attempted += entry.listings.length;
    try {
      await postListings(endpointUrl, secret, entry.listings);
      summary.delivered += entry.listings.length;
      appendFailureRecord(failures, {
        batchId: entry.id,
        attempt: entry.attempts + 1,
        listingCount: entry.listings.length,
        outcome: "delivered"
      });
    } catch (error) {
      const attempts = (Number.isInteger(entry.attempts) ? entry.attempts : 1) + 1;
      if (attempts >= MAX_INGEST_ATTEMPTS) {
        const abandonedKeys = new Set(failures.abandonedKeys);
        let newlyAbandoned = 0;
        for (const listing of entry.listings) {
          const key = retryListingKey(listing);
          if (key && !abandonedKeys.has(key)) {
            abandonedKeys.add(key);
            newlyAbandoned += 1;
          } else if (!key) newlyAbandoned += 1;
        }
        failures.abandonedKeys = [...abandonedKeys];
        failures.abandonedCount += newlyAbandoned;
        summary.abandoned += newlyAbandoned;
        appendFailureRecord(failures, {
          batchId: entry.id,
          attempt: attempts,
          status: error.status || null,
          listingCount: entry.listings.length,
          outcome: "abandoned",
          error: String(error.message || error).slice(0, 500)
        });
      } else {
        retained.push({
          ...entry,
          attempts,
          nextAttemptAt: new Date(Date.now() + (error.retryAfterMs || DEFAULT_RETRY_DELAY_MS)).toISOString(),
          lastError: String(error.message || error).slice(0, 500),
          lastStatus: error.status || null
        });
        appendFailureRecord(failures, {
          batchId: entry.id,
          attempt: attempts,
          status: error.status || null,
          listingCount: entry.listings.length,
          outcome: "retry-scheduled",
          error: String(error.message || error).slice(0, 500)
        });
      }
    }
  }
  await writeRetryState(retained, failures);
  summary.pending = failures.pendingCount;
  return summary;
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

  try {
    lastRunStatus.retry = await retryPendingListings(ingestEndpoint, scoutSecret);
  } catch (error) {
    lastRunStatus.retry = { ok: false, error: error.message };
    console.error("Deal Scout retry processing failed", error);
  }

  for (const [index, target] of watchTargets.entries()) {
    if (!target || typeof target !== "object") {
      lastRunStatus.targets[`${index}:Invalid target`] = { ok: false, error: "Stored watch target is invalid" };
      continue;
    }
    if (!target.enabled) continue;
    const key = `${index}:${target.label || "Unnamed target"}`;
    let listings = [];
    try {
      // A short gap between targets keeps consecutive create/remove pairs from
      // landing inside the same transient tab-strip state.
      if (index > 0) await sleep(1500);
      listings = await scanTarget(target);
      const ingest = await postListings(ingestEndpoint, scoutSecret, listings);
      lastRunStatus.targets[key] = { ok: true, found: listings.length, accepted: ingest.accepted || 0, dropped: ingest.dropped || 0 };
      console.info("Deal Scout target complete", target.label, lastRunStatus.targets[key]);
    } catch (error) {
      let queued = 0;
      let persistenceError = null;
      if (listings.length) {
        try {
          queued = await persistFailedListings(listings, error);
        } catch (storageError) {
          persistenceError = storageError.message;
          console.error("Deal Scout could not persist failed listings", target.label, storageError);
        }
      }
      lastRunStatus.targets[key] = {
        ok: false,
        error: error.message,
        found: listings.length,
        queued,
        ...(persistenceError ? { persistenceError } : {})
      };
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
