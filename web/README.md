# eBay Deal Alert Settings App

This is a plain static settings UI plus a Vercel serverless API for editing the repo root `config.json` from a phone.

Deploy it in Vercel with the project **Root Directory** set to `web/` and the framework preset set to **Other**.

Required Vercel environment variables:

- `GITHUB_TOKEN`: GitHub personal access token with permission to read and write this repo.
- `GITHUB_REPO`: Repository name in `owner/repo` format.
- `SETTINGS_PASSWORD`: Shared password required by the browser UI and API.

The optional Telegram listing-analysis webhook additionally requires
`TELEGRAM_BOT_TOKEN`, `DEEPSEEK_API_KEY`, `TELEGRAM_WEBHOOK_SECRET`, and
`TELEGRAM_ALLOWED_CHAT_ID`. The webhook secret must match the `secret_token`
used with Telegram's `setWebhook`; the allowed chat ID keeps paid analysis
restricted to the owner.

The API uses GitHub's Contents API to fetch the current `config.json`, re-fetches the file SHA before saving, and commits changes with the message `Update config via mobile settings app`.
