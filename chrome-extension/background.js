const ALARM_NAME = "deal-scout-scan";
const DEFAULT_TARGETS = [{
  label: "Golf club sets — Columbia, SC",
  platform: "facebook",
  searchUrl: "https://www.facebook.com/marketplace/columbiasc/search?query=golf%20club%20set",
  parser: "facebook-json",
  enabled: true
}];

let lastRunStatus = { startedAt: null, finishedAt: null, running: false, targets: {} };

const sleep = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));
const syncGet = (keys) => new Promise((resolve) => chrome.storage.sync.get(keys, resolve));
const localGet = (keys) => new Promise((resolve) => chrome.storage.local.get(keys, resolve));

async function initialize() {
  const { watchTargets } = await syncGet(["watchTargets"]);
  if (!Array.isArray(watchTargets)) await chrome.storage.sync.set({ watchTargets: DEFAULT_TARGETS });
  await chrome.alarms.create(ALARM_NAME, { periodInMinutes: 10 });
}

chrome.runtime.onInstalled.addListener(initialize);
chrome.runtime.onStartup.addListener(initialize);

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
  let tab;
  try {
    tab = await chrome.tabs.create({ url: target.searchUrl, active: false });
    await waitForTab(tab.id);
    await sleep(2500);
    const file = target.parser === "generic-og"
      ? "content-scripts/generic-og-parser.js"
      : "content-scripts/facebook-parser.js";
    const results = await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: [file] });
    const extracted = Array.isArray(results?.[0]?.result) ? results[0].result : [];
    return extracted.map((listing) => ({ ...listing, platform: target.platform }));
  } finally {
    if (tab?.id) await chrome.tabs.remove(tab.id).catch(() => {});
  }
}

async function postListings(endpointUrl, secret, listings) {
  if (!listings.length) return { accepted: 0, dropped: 0 };
  const response = await fetch(endpointUrl, {
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
  const [{ watchTargets = DEFAULT_TARGETS }, { ingestEndpoint = "", scoutSecret = "" }] = await Promise.all([
    syncGet(["watchTargets"]), localGet(["ingestEndpoint", "scoutSecret"])
  ]);
  if (!ingestEndpoint || !scoutSecret) {
    lastRunStatus.running = false;
    lastRunStatus.finishedAt = new Date().toISOString();
    lastRunStatus.error = "Configure the ingest endpoint and secret in the popup.";
    return lastRunStatus;
  }

  for (const [index, target] of watchTargets.entries()) {
    if (!target.enabled) continue;
    const key = `${index}:${target.label}`;
    try {
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
