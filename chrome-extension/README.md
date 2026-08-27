# Deal Scout Chrome extension

Deal Scout replaces the unreliable Facebook proxy browser with your real, logged-in Chrome session. It opens each enabled saved search in an inactive tab about every 10 minutes, extracts listing candidates, sends them to `/api/scout-ingest`, and closes the tab. The Python bot later consumes those candidates through its existing scoring, AI-check, dedupe, and alert pipeline.

This is not 24/7 coverage. Alarm scans run only while Chrome is open (and Chrome may throttle alarms); **Scan Now** runs only when clicked. GitHub Actions continues to run independently, but it can consume only candidates that the extension has already committed to the queue.

## Install and configure

1. Generate a long random secret. Set it as `SCOUT_INGEST_SECRET` in the Vercel project alongside the existing `GITHUB_TOKEN` and `GITHUB_REPO` variables, then redeploy. The Python bot does not need this secret because it reads the committed queue file directly. Do not put the secret in this repository or in a GitHub Actions secret.
2. Open `chrome://extensions`, enable **Developer mode**, choose **Load unpacked**, and select this `chrome-extension` directory.
3. Open Deal Scout's popup. Enter the deployed URL, such as `https://YOUR-PROJECT.vercel.app/api/scout-ingest`, and the same secret; click **Save setup** and approve the endpoint permission prompt.
4. Sign in to Facebook and confirm Marketplace is located around Columbia, South Carolina. The shipped target, **Golf club sets — Columbia, SC**, mirrors the enabled `golf club set -junior ...` saved search in `config.json` and is enabled by default.
5. Click **Scan Now**. The popup should show the target, the number found, accepted, and dropped. The background service worker console (`chrome://extensions` → Deal Scout → **service worker**) contains detailed per-target errors.

## Adding categories and sites

Cars or furniture on Facebook are configuration only: perform the desired Marketplace search in Facebook, copy the results URL, add it in the popup, select `facebook`, and use **Facebook JSON**. Add a matching enabled saved search to `config.json` so the Python bot can associate the title with the right price/category rules.

The manifest grants Facebook access initially. Adding a different HTTPS site through the popup requests that hostname at runtime from `optional_host_permissions`; no manifest edit is normally necessary. Chrome Web Store policy or a non-HTTP browser page may require an explicit manifest permission.

`generic-og` is intentionally minimal: it returns the loaded page's canonical URL and OG fields rather than trying to understand a site's search-result schema. It is useful when a target URL itself represents one listing and exposes a numeric `product:price:amount`. A true multi-result site needs a new parser file under `content-scripts/`, a parser option in `popup.html`, and a dispatch entry in `background.js`; that is code, not configuration. Facebook needs its dedicated parser because its useful data is in embedded `application/json` blocks rather than dependable server-rendered OG tags.

## Manual test checklist

- With Chrome logged into Facebook and Marketplace set to Columbia, open the default target manually and verify visible listings are local.
- Save the Vercel endpoint and secret; approve the permission prompt.
- Click **Scan Now** and verify an inactive Facebook tab opens, finishes loading, and closes.
- Confirm the popup reports `ok`, with found/accepted counts; inspect the service-worker console if it reports an error.
- Confirm Vercel returns HTTP 200 and `scout_queue.jsonl` gains JSONL rows with `discoveredAt`; a wrong secret should return HTTP 401.
- Run the Python bot once and verify Scout rows enter ordinary scoring logs, then the queue becomes empty and the workflow state commit includes that clearing change.
- Disable the target and scan again; verify no Facebook tab opens for it.
- Add one deliberately invalid target and one valid target; verify the valid target still completes independently.
