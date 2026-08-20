# AI/pricing pipeline audit — ebay_deal_alert.py

Read-only investigation. Findings ordered by severity, each with exact file+line, a
concrete failure scenario, and severity.

---

## HIGH

### 1. Watch clamp uses a STALE `title` from PASS 1, not the current candidate

- `ebay_deal_alert.py:3689` — `band = watch_price_band(title)`
- `title` is assigned only once in `run()`, at `ebay_deal_alert.py:3184`
  (`title = listing.get("title", "")`), inside the PASS 1 listing loop. Python
  loop variables leak into the enclosing function scope, and the PASS 3 loop
  (`for candidate in review_candidates:`, line 3523) never reassigns `title`.
  So `watch_price_band(title)` sees the title of the **last listing processed in
  PASS 1**, not `listing["title"]` of the candidate being priced.

Failure scenario: a `watches` candidate whose real title is `"Movado Bold
36mm"` — the exact class the clamp exists to stop (the Movado $595–795
overestimate incident). But the last PASS 1 listing was, say, `"Barbour Waxed
Coat XL"` (no watch brand), so `watch_price_band("barbour waxed coat xl")`
returns `None`, the `if band is not None` guard is false, and **no ceiling is
applied** — the AI's inflated resale guess flows through to a "Steal". The
mirror failure: the last PASS 1 listing was a *different* watch brand, so the
candidate gets clamped to the **wrong brand's** ceiling (e.g. a Seiko candidate
clamped to the Rolex band's high). Correct fix is `watch_price_band(listing.get("title", ""))`.

---

## MEDIUM

### 2. AI numeric fields stored with zero validation; a string resale estimate crashes the whole run in the watch clamp

- `ebay_deal_alert.py:3678-3679` — `result["estimated_resale_value"] = ai_result.get("estimated_resale_value")` stores the raw model value, no type/coercion/range check anywhere.
- `ebay_deal_alert.py:3682` guards only `is not None`, then `:3693` calls `clamp_watch_resale_estimate(original, band)`.
- `ebay_deal_alert.py:1342` — `clamp_watch_resale_estimate` returns `min(high, estimate)`, where `high` is an `int` from the band tuple.

If the model returns `estimated_resale_value` as a string — even a clean numeric
one — then `min(high, "1200")` (or `min(high, "$1,200")`) raises
`TypeError: '<' not supported between instances of 'str' and 'int'`. There is no
`try/except` around the PASS 3 candidate body (the only handler in the loop is
around `send_alert`, line 3886), so the exception propagates out of `run()` and
kills the entire run. This is a trust-boundary gap: the model is the rolling
`gemini-flash-lite-latest` alias (see comment at 1697–1702), so its output
format is not pinned.

Lower-severity but same root cause: for non-watch categories a string
`estimated_resale_value` makes `compute_deal_rating` return `(None, None)`
(line 1877) — a graceful silent downgrade to "no rating", not a crash.

### 3. Negative resale (or negative price) is not guarded and fabricates a positive "Steal"

- `ebay_deal_alert.py:1878` — the `if not price or not estimated_resale_value` guard only rejects falsy (0 / None / ""). `float("-100")` is `-100.0`, which is truthy, so a negative value passes straight through.
- `ebay_deal_alert.py:1881` — `discount_pct = (estimated_resale_value - price) / estimated_resale_value`.

Concrete: `price = 50`, `estimated_resale_value = -100` →
`(-100 - 50) / -100 = 1.5` → `discount_pct >= 0.70` → **"Steal"**, logged as
`150%`. A negative resale estimate (a nonsense value) is turned into the
strongest positive rating in the system, with no validation anywhere between the
JSON field and the gate. `price` is internally computed and can't realistically
be negative, but `estimated_resale_value` is raw model output.

### 4. Absurd-high resale value bypasses the only sanity net for non-watch categories

- `ebay_deal_alert.py:2293-2302` — the MARKET SATURATION guard fires only when `deal_rating != "Steal"`. A hallucinated high resale produces a "Steal", and "Steal" is explicitly exempt, so an oversupplied item with no comps (`search_total_listings >= 500`) still alerts.
- There is no resale↔retail ratio check or any upper bound on `estimated_resale_value` outside `watches`. A $9.99 belt guessed at a $500 resale → 98% "Steal" with no ceiling, no comp, and the saturation gate skipped.

The watches category is protected (ceiling band), and Great Deal is protected (saturation gate); Steal on any other category is not.

---

## LOW

### 5. Prompt asks for four fields the code never reads

`ebay_deal_alert.py:1748-1752` defines this output shape:

> `{"damage_found": bool, "damage_desc": string, "weird_logo_found": bool, "logo_desc": string, "looks_good": bool, "summary": string, "visible_brand_evidence": string, "peter_millar_back_crown_visible": bool|null, "pricing_basis": string, ...}`

and `:1756`: `"Reason from visible_brand_evidence and pricing_basis to the price estimate."`

A whole-file search shows `damage_desc`, `logo_desc`, `visible_brand_evidence`,
and `pricing_basis` appear **only in the prompt** — never consumed in PASS 3,
`send_alert`, `append_alert_log`, or the web UI. The damage/logo suppression at
3645-3658 reports `summary` instead of the two `*_desc` fields, and the two
reasoning fields (`visible_brand_evidence`, `pricing_basis`) that the model is
told to anchor its price on are never surfaced anywhere. Wasted output tokens
and dead reasoning; not a correctness bug.

### 6. Sold-comps override: cannot overwrite high-confidence, but has two soft spots

- `ebay_deal_alert.py:3728-3738`. The override correctly refuses to touch a
  high-confidence AI number (`comp_overrides_weak_ai` requires `price_confidence
  in ("medium", "low")`), so it can't *worsen* a strong estimate.
- (a) `ebay_deal_alert.py:3728` — `ai_confidence = (result.get("price_confidence") or "").lower()`. A **missing/null** `price_confidence` becomes `""`, which is not in `("medium","low")`, so real comps never rescue an AI number whose confidence the model declined to state. A null-confidence guess (no evidence of high confidence) is treated like high confidence for override purposes.
- (b) The median is a **brand+garment-query** median (`comp_count` is the only relevance proxy; nothing in this file checks that the sold comps match the candidate's model/size/condition). So when it does override a medium-confidence photo-based estimate at `comp_count >= 5`, it can replace a photo-derived number for *this specific item* with a median drawn from potentially mismatched sales. The 5-sample floor mitigates but does not eliminate "irrelevant median."

---

## Confirmed correct (no finding)

- **`compute_deal_rating` division by zero**: guarded — `estimated_resale_value` of `0`/`None`/`""` returns `(None, None)` both before (`:1871`) and after (`:1878`) the `float()` coercion. No div-by-zero path.
- **Sign convention is consistent**: positive `discount_pct` = margin (resale > price), negative = overpriced. Every gate consumer checks `discount_pct <= 0` to block (lines 2015, 2044, 2076, 2093, 2230, 2265) and the suit retail path checks `>= 0` (2177). The unclamped negative-magnitude change (1881-1891) is safe: consumers only test sign or a `>=` threshold.
- **`clamp_watch_resale_estimate` is ceiling-only**: `return min(high, estimate)` (1342), never raises. No-band-entry interaction is handled — `watch_price_band` returns `None` and the caller guards `if band is not None` (3690), so no crash on a missing band (the no-band case simply applies no ceiling, which is intended; see finding #1 for the separate stale-title bug).
- **`_call_gemini_json` error handling** (1810-1814) catches `RequestException`, `KeyError`, `IndexError`, `JSONDecodeError` — network/timeout/HTTP-status/empty-candidates/malformed-JSON all degrade to `None` instead of crashing.
