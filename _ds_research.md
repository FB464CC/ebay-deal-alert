# DeepSeek / scraper research — value-for-effort ranking

Context: one AI vision check per listing today — `check_photos_with_gemini()` → `_call_photo_check()` → `_call_deepseek_json()`/`_call_gemini_json()`. Returns JSON incl. `price_confidence`, `estimated_resale_value`, `damage_found`, `weird_logo_found`, `looks_good`, `fabric_from_tag`, `liquidity`, `summary`. config.json currently sets `AI_PHOTO_PROVIDER="gemini"` (Gemini primary, DeepSeek fallback), `DEEPSEEK_MODEL="deepseek-v4-flash-vision-exp"`. Budget: `GEMINI_CALL_LIMIT=3`/run, `MAX_ALERTS_PER_RUN=8`.

## (1) DeepSeek for alert QUALITY / rating accuracy

Ranked by value-for-effort. All are text-only (no image payload → ~$0.0001–0.001/call, i.e. effectively free at the ≤8 alerts/day + few borderline candidates this bot actually produces).

1. **Text-only "is this the real, complete, adult-sized item?" sanity pass right before `send_alert()`.** Highest value: kills the residual junk class the single vision check misses — accessory/parts (Rolex crystal, watch strap), jacket-only, wrong gender/size when the title gives no hint. Cost ~$0.0001 (no images), fires only on candidates that already cleared the gate. Complexity **small**. Hook: PASS 3 loop in `run()`, immediately before `send_alert()` (~line 4325), mirroring the existing gender/pet re-checks at 4018–4051 (which already pass the AI `summary` through `brand_in`/regex). Returns `{is_what_searched, is_part_or_accessory, likely_gender}`; suppress on mismatch.

2. **Second-opinion/ensemble on borderline price confidence.** When `price_confidence` is "low"/"medium" AND no Grailed comp override fires (the 4167–4203 `comp_overrides_weak_ai` path), send a text-only DeepSeek re-estimate (title + description + the primary's `visible_brand_evidence`) and take the more conservative (lower) of the two resale values. Directly targets the measured fact in the code: 56 of 97 alerts rested on a medium-confidence guess. Cost <$0.001, only on borderline candidates. Complexity **small**. Hook: PASS 3 in `run()`, after `check_photos_with_gemini()` returns (~4007) and before `compute_deal_rating()` (4146).

3. **DeepSeek description→structured extraction as a pre-vision filter.** Poshmark/ShopGoodwill carry full descriptions for free (`make_listing()`); a text-only call extracting `{size, fabric, disclosed_flaws, brand_claim}` hard-passes obvious disclosed damage or wrong size BEFORE spending a vision slot. Lower priority than 1–2 because the description is already fed into the vision prompt (2013–2026) — this only adds early-out on disclosed flaws, not new evidence. Cost ~$0.0001. Complexity **medium** (new fields + plumbing). Hook: `score_listing()` (1723) or a new pre-AI pass in the PASS 3 loop.

## (2) DeepSeek for building/maintaining `platforms.py` scrapers

Framing correction: `platforms.py` has **no HTML selectors** — all four adapters are JSON endpoints (Grailed/Algolia, Poshmark frontend JSON, ShopGoodwill POST API, Vinted catalog API). The real silent-breakage mode is **JSON shape drift** — renamed keys, dict→scalar changes — not selector rot. Evidence already in-repo: the `cover_shot` vs `covershot` field-name bug, and the `_dget()` docstring's scalar-AttributeError case.

1. **Zero-result / count-drop anomaly detector (no DeepSeek needed — do this first).** `prefetch_marketplaces()` (3114) already computes per-platform counts; persist them per (platform, search) and flag when a historically-nonzero pair returns 0 or drops >10× vs its own 7-day baseline. DeepSeek's only role is a maintenance copilot: when flagged, paste the raw response body (captured in `get_json()`, 90) and let DeepSeek return the corrected field path or "endpoint now requires X." Cost ≈ $0 (fires only on anomaly). Complexity **small** for the detector, **small** for the copilot. Hook: `prefetch_marketplaces()` (3114) + `get_json()` (90).

2. **DeepSeek as a fallback JSON extractor when the hand-written path returns None.** When `_dig`/`_dget` yields None for a critical field (image_url, price), send the raw JSON blob to DeepSeek text and ask for the normalized listing shape (`title, price, url, image_url, extra_images, size, description`). Only fires on shape-drift, so near-zero cost. Complexity **medium** (shared helper + `_sane_ai_price`-style sanitization of returned values). Hook: one helper called from `search_poshmark()` / `search_vinted()` / `search_shopgoodwill()` / `_grailed_hit_to_listing()`.

Prioritize 1 over 2 — the detector is ~10 lines of stats and finds breakage regardless of how the shape changed; the DeepSeek extractor is the repair path.

## (3) Facebook Marketplace via Apify — feasibility

- **Viable, no login/cookies:** yes, via prebuilt Apify actors (All-in-One Facebook Scraper, `scrapelabsapi/facebook-marketplace-scraper`, Curious Coder's FB scraper). Public marketplace results scrape without an account. Caveat: FB exposes no public JSON endpoint, so it does **not** fit the current `platforms.py` adapter model (all-JSON, no proxy, plain `requests`) — it would be a new, out-of-band fetch path.
- **Cost per 1,000 listings:** actor fee ~$1.50–$4.00, **plus** residential proxy (the dominant hidden cost): $0.10/result surcharge on some actors, or ~$7–8/GB residential bandwidth — image-heavy scrapes can exceed the per-result fee. Realistic all-in **~$5–15/1,000 with proxies**.
- **Reliability:** moderate-to-low. FB is Cloudflare/Akamai + login-walled on most browsing paths; actors return partial, geo-limited results and listings can be stale (marketplace items sell in minutes — the exact freshness this bot competes on). ~95% is request success, not coverage completeness.
- **Account-ban / ToS risk:** scraping FB violates FB ToS regardless of method, but through Apify the account/ban risk lands on Apify's proxy/actor infra, not your personal FB account — so **personal-account-ban risk is low**; residual ToS/legal exposure and possible actor degradation remain.
- **Verdict:** cheap to pilot (<$10 for a few thousand listings) but expect partial/stale data — treat as a supplementary feed, not a primary source. Skip if you need beat-the-clock freshness.

## Sources
- [All-in-One Facebook Scraper (Apify)](https://apify.com/get-leads/all-in-one-facebook-scraper)
- [Facebook Marketplace Scraper (ScrapeLabs API)](https://apify.com/scrapelabsapi/facebook-marketplace-scraper)
- [Apify Pricing 2026: Compute Units & Real Cost](https://scrapewise.ai/blogs/apify-pricing-compute-units-cost-2026)
- [Apify Pay Per Event (PPE) Pricing Explained (2026)](https://use-apify.com/docs/what-is-apify/apify-pay-per-event)
