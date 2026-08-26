# eBay Deal Alert Engine

This project polls the eBay Browse API for the saved searches in `ebay_deal_alert.py`, dedupes already-seen item IDs in `seen_items.db`, scores new listings with the Phase 1 six-check framework, checks Wardrobe OS gap data when available, and sends matching alerts through ntfy.

GitHub Actions runs the poller every 5 minutes and commits `seen_items.db` back to the repository when new item IDs are recorded. That commit is required because scheduled GitHub Actions runners are ephemeral.

## One-Time GitHub Setup

Create these repository secrets in GitHub under `Settings > Secrets and variables > Actions`:

- `EBAY_CLIENT_ID`
- `EBAY_CLIENT_SECRET`
- `WARDROBE_OS_URL`
- `WARDROBE_OS_SECRET`
- `NTFY_TOPIC`

The workflow already requests `contents: write` permission so it can commit `seen_items.db` updates using `GITHUB_TOKEN`.

## Phone Alerts With ntfy

1. Install the ntfy app from the iOS App Store or Google Play.
2. Open the app and subscribe to the exact topic stored in your `NTFY_TOPIC` GitHub secret.
3. Leave notifications enabled for the app.

Alerts are sent to `https://ntfy.sh/<NTFY_TOPIC>`.

## AI Cost Guard

Paid AI calls are guarded by a persistent monthly ledger in `seen_items.db`.
The default ceiling is $18/month, leaving headroom below the $20 target. Each
DeepSeek vision or text request reserves a conservative configured amount
before the HTTP call; when the ceiling is reached, paid calls stop and the bot
continues with free-provider results. The relevant settings are
`AI_PAID_MONTHLY_BUDGET_USD`, `AI_PAID_VISION_RESERVATION_USD`, and
`AI_PAID_TEXT_RESERVATION_USD` in `config.json`.

## Test Locally

Install dependencies:

```bash
pip install -r requirements.txt
```

Set the required environment variables:

```bash
export EBAY_CLIENT_ID="your-ebay-client-id"
export EBAY_CLIENT_SECRET="your-ebay-client-secret"
export WARDROBE_OS_URL="your-wardrobe-os-url"
export WARDROBE_OS_SECRET="your-wardrobe-os-secret"
export NTFY_TOPIC="your-ntfy-topic"
```

On PowerShell:

```powershell
$env:EBAY_CLIENT_ID = "your-ebay-client-id"
$env:EBAY_CLIENT_SECRET = "your-ebay-client-secret"
$env:WARDROBE_OS_URL = "your-wardrobe-os-url"
$env:WARDROBE_OS_SECRET = "your-wardrobe-os-secret"
$env:NTFY_TOPIC = "your-ntfy-topic"
```

Run the poller:

```bash
python ebay_deal_alert.py
```

Check syntax without running API calls:

```bash
python -m py_compile ebay_deal_alert.py
```

## Manual GitHub Actions Run

In GitHub, open the repository's `Actions` tab, select `Poll eBay Deals`, choose `Run workflow`, and start a manual run from the default branch.
