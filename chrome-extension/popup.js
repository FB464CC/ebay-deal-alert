const targetsNode = document.getElementById("targets");
const statusNode = document.getElementById("status");

const syncGet = (keys) => new Promise((resolve) => chrome.storage.sync.get(keys, resolve));
const localGet = (keys) => new Promise((resolve) => chrome.storage.local.get(keys, resolve));

async function requestUrlPermission(rawUrl, options) {
  const url = new URL(DealScoutUrls.normalizeUrl(rawUrl, options));
  const origin = `${url.protocol}//${url.host}/*`;
  const alreadyGranted = await chrome.permissions.contains({ origins: [origin] });
  return alreadyGranted || chrome.permissions.request({ origins: [origin] });
}

async function renderTargets() {
  const { watchTargets = [] } = await syncGet(["watchTargets"]);
  targetsNode.replaceChildren();
  watchTargets.forEach((target, index) => {
    const box = document.createElement("div"); box.className = "target";
    const remove = document.createElement("button"); remove.className = "danger"; remove.textContent = "Remove";
    remove.addEventListener("click", async () => {
      watchTargets.splice(index, 1);
      try {
        await chrome.storage.sync.set({ watchTargets });
        await renderTargets();
      } catch (error) {
        statusNode.textContent = `Failed to remove target: ${error.message}`;
      }
    });
    const label = document.createElement("label");
    const toggle = document.createElement("input"); toggle.type = "checkbox"; toggle.checked = target.enabled;
    toggle.addEventListener("change", async () => {
      watchTargets[index].enabled = toggle.checked;
      try {
        await chrome.storage.sync.set({ watchTargets });
      } catch (error) {
        toggle.checked = !toggle.checked;
        watchTargets[index].enabled = toggle.checked;
        statusNode.textContent = `Failed to save toggle: ${error.message}`;
      }
    });
    label.append(toggle, document.createTextNode(target.label));
    const meta = document.createElement("div"); meta.className = "meta";
    meta.textContent = `${target.platform} · ${target.parser}\n${target.searchUrl}`;
    box.append(remove, label, meta); targetsNode.append(box);
  });
}

function showStatus(status) {
  if (!status?.startedAt) return;
  const rows = Object.entries(status.targets || {}).map(([key, result]) => {
    const label = key.replace(/^\d+:/, "");
    return result.ok ? `${label}: found ${result.found}, accepted ${result.accepted}, dropped ${result.dropped}` : `${label}: ERROR — ${result.error}`;
  });
  statusNode.textContent = `${status.running ? "Running" : "Last scan"}: ${new Date(status.startedAt).toLocaleString()}${status.error ? ` — ${status.error}` : ""}${rows.length ? `\n${rows.join("\n")}` : ""}`;
  statusNode.style.whiteSpace = "pre-line";
}

document.getElementById("scanNow").addEventListener("click", async () => {
  statusNode.textContent = "Scanning…";
  try {
    showStatus(await chrome.runtime.sendMessage({ type: "scan-now" }));
  } catch (error) {
    statusNode.textContent = `Scan failed: ${error.message}`;
  }
});

document.getElementById("targetForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const searchUrl = DealScoutUrls.normalizeUrl(document.getElementById("searchUrl").value.trim());
    if (!await requestUrlPermission(searchUrl)) return void (statusNode.textContent = "Site permission is required to scan that target.");
    const stored = await syncGet(["watchTargets"]);
    const watchTargets = Array.isArray(stored.watchTargets) ? stored.watchTargets : [];
    watchTargets.push({
      label: document.getElementById("label").value.trim(), platform: document.getElementById("platform").value.trim(),
      searchUrl, parser: document.getElementById("parser").value, enabled: true
    });
    await chrome.storage.sync.set({ watchTargets }); event.target.reset(); await renderTargets();
  } catch (error) {
    statusNode.textContent = error.message;
  }
});

document.getElementById("setupForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const ingestEndpoint = DealScoutUrls.normalizeUrl(document.getElementById("endpoint").value.trim(), { requireHttps: true });
    if (!await requestUrlPermission(ingestEndpoint, { requireHttps: true })) return void (statusNode.textContent = "Endpoint permission is required to send listings.");
    await chrome.storage.local.set({ ingestEndpoint, scoutSecret: document.getElementById("secret").value });
    statusNode.textContent = "Setup saved locally.";
  } catch (error) {
    statusNode.textContent = error.message;
  }
});

Promise.allSettled([renderTargets(), localGet(["ingestEndpoint", "scoutSecret"]), chrome.runtime.sendMessage({ type: "get-status" })])
  .then(([, setupResult, statusResult]) => {
    const setup = setupResult.status === "fulfilled" ? setupResult.value : {};
    document.getElementById("endpoint").value = setup.ingestEndpoint || "";
    document.getElementById("secret").value = setup.scoutSecret || "";
    if (statusResult.status === "fulfilled") {
      showStatus(statusResult.value);
    } else {
      statusNode.textContent = `Status unavailable: ${statusResult.reason?.message || statusResult.reason}`;
    }
  });
