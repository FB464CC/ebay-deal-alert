# Deal Alert Engine — Build Spec

**Purpose:** Stop manually checking eBay/Poshmark/Depop throughout the day. Run the six-check verdict framework and Wardrobe OS gap check automatically against new listings. Only alert on BUY or FLIP-ONLY, not raw keyword matches.

**Hand this whole file to Claude Code as the project brief.**

---

## 1. Platform feasibility (confirmed August 2026)

| Platform | Official API | Alerts | Verdict |
|---|---|---|---|
| eBay | Yes — Browse API + Notification API, OAuth app token | Real-time via polling (10-60 sec) or push subscriptions | **Build on this** |
| Poshmark | None. No public dev API. | No RSS feed for search results (confirmed) | Scraping only — Phase 2 |
| Depop | None. No public dev API. | No RSS feed for search results (confirmed) | Scraping only — Phase 2 |

eBay's native saved-search alerts are batched and slow (often minutes to hours). Polling the Browse API directly at a fixed interval beats native alerts on speed and lets you attach custom scoring logic native alerts can't do.

Poshmark/Depop scraping is technically possible (unofficial libraries exist, e.g. cookie-session based clients) but fragile — breaks on redesigns, requires session cookie refresh, and is against both platforms' ToS. Recommend building and proving out the eBay engine first, then deciding if the maintenance burden on Poshmark/Depop is worth it.

---

## 2. Architecture

```
┌─────────────────────────────────────────────┐
│  Scheduler (cron / GitHub Actions / systemd) │
│  runs every 2-5 min                          │
└──────────────────┬────────────────────────────┘
                    │
         ┌──────────▼──────────┐
         │  eBay Browse API     │  ← your 13 saved searches,
         │  search per query    │     converted to API params
         └──────────┬──────────┘
                    │ new listings only (dedupe by item ID)
         ┌──────────▼──────────┐
         │  Scoring Engine       │  ← six-check framework as code
         │  (brand/fabric/fit/   │
         │   condition/gap/flip) │
         └──────────┬──────────┘
                    │
         ┌──────────▼──────────┐
         │  Wardrobe OS API      │  ← gap_report call, live —
         │  gap cross-check      │     never hardcode gaps
         └──────────┬──────────┘
                    │ verdict: BUY / FLIP-ONLY / PASS
                    │ (PASS results are logged, not sent)
         ┌──────────▼──────────┐
         │  Alert dispatch       │  ← push notification
         │  (ntfy.sh / Telegram) │
         └──────────────────────┘
```

---

## 3. Scoring engine — six-check as code

Port the CareerOS framework directly into scoring functions. This is the actual value-add over any off-the-shelf alert tool.

1. **Brand tier** — lookup table (grab-on-sight / standard / pass), plus corporate-logo keyword blocklist (Tournament, Championship, Club, Resort, Hotel, Bank, Capital, Financial, Wealth, Partners, Group, Associates, Invitational, Foundation, Corporate, Insurance, Open, Classic). Title/description regex match.
2. **Fabric** — keyword match against listing title + description (merino, cashmere, poly, wool, silk, cotton). Note: this only screens obvious cases. Anything ambiguous should route to a "needs photo review" queue rather than auto-pass or auto-fail, since fabric tag verification can't be automated from a listing title.
3. **Fit** — parse any pit-to-pit measurement mentioned in description against the 23.5" cap. If absent, flag explicitly rather than guessing.
4. **Condition** — keyword flags for "moth," "hole," "stain," "pilling," "repair" in description. Hard-fail on moth/hole keywords per your no-exceptions rule.
5. **Gap check** — call Wardrobe OS `gap_report` action live on every run (cache for the polling interval, don't hit it per-listing). Cross-reference item type + color family against open gaps and against the "filled this cycle" list.
6. **Flip potential** — if brand/fabric/condition pass but fit fails, tag as FLIP-ONLY and skip resale estimate automation (that needs a real eBay sold-comps lookup, separate from this engine).

**Verdicts below full BUY confidence still get logged to a review queue, not silently dropped** — this preserves your manual judgment call on borderline items instead of the bot deciding for you.

---

## 4. Alert delivery — pick one to start

| Option | Setup effort | Cost | Notes |
|---|---|---|---|
| **ntfy.sh** | Lowest — no account needed, one HTTP POST | Free | Recommended starting point. Phone app + topic subscription. |
| Telegram bot | Low — BotFather token, one-time | Free | Better if you want to reply/interact with alerts (e.g. "snooze," "mark seen") |
| Pushover | Low | $5 one-time | More polished mobile UX than ntfy |
| Email/SMS | Low | Free/cheap | Slower to notice than push |

Recommend **ntfy.sh** for v1 — fastest to stand up, revisit if you want interactivity.

---

## 5. Hosting/scheduling — the engine has to run continuously

Your laptop being closed kills a cron job. Options:

| Option | Cost | Notes |
|---|---|---|
| **GitHub Actions scheduled workflow** | Free (public repo) | Simplest — cron syntax, runs in the cloud, no server to maintain. Minimum interval ~5 min on free tier. |
| Small VPS (Oracle free tier, Fly.io, Railway) | Free-$5/mo | True continuous polling at any interval, more setup |
| Raspberry Pi at home | Free if you have one | Runs locally, no cloud dependency |

Recommend **GitHub Actions** for v1 — zero infrastructure, good enough interval for eBay's listing pace.

---

## 6. Build phases for Claude Code

**Phase 1 (build first):**
- eBay Browse API auth (client credentials OAuth)
- Port your 13 saved searches into API query params
- Dedupe logic (track seen item IDs, e.g. in a small SQLite file or the Wardrobe OS sheet itself as a new tab)
- Six-check scoring engine (steps 1-4 above, keyword/regex based)
- Wardrobe OS `gap_report` integration
- ntfy.sh alert dispatch
- GitHub Actions cron wrapper

**Phase 2 (only if Phase 1 proves useful):**
- Poshmark/Depop scraping (unofficial, session-based)
- Resale comp lookup for flip-only verdicts (eBay sold listings search)
- Telegram bot for interactive alerts instead of one-way push

**Explicitly not v1:** full computer-vision fabric/pit-to-pit verification from photos. Keep the "needs photo review" queue for anything the text can't confirm — this matches your existing rule that fabric tag photos and pit-to-pit measurements can't be trusted from listing titles alone.

---

## 7. Open decisions before Claude Code starts building

- Alert channel: ntfy.sh vs Telegram vs Pushover
- Hosting: GitHub Actions vs VPS vs local
- Poll interval: 2 min vs 5 min (tighter interval on GitHub Actions free tier costs more Action minutes)
- Where to store "seen item IDs" — new Wardrobe OS sheet tab (keeps everything in one place) vs standalone SQLite file (simpler, but disconnected from the sheet)
