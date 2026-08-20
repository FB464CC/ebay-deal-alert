# _rb2.md — review of peripheral paths in ebay_deal_alert.py

Read-only review. Findings ordered by severity.

---

## MEDIUM

### 1. Circuit breaker: semantically-corrupt state file aborts the run, and a far-future `blocked_until_ts` locks eBay out forever
`ebay_deal_alert.py:691` (`if now_ts < blocked_until_ts:`), call site `:2948`.

`_read_ebay_rate_limit_state()` (`:650`) only catches `OSError/ValueError/JSONDecodeError` and does no schema validation. Two distinct failures:

- **Wrong type → crash.** If `ebay_rate_limit_state.json` contains valid JSON with `"blocked_until_ts": "..."` (a string) or `"consecutive_429_streak": "3"`, then `now_ts < blocked_until_ts` raises `TypeError` (float < str) at `:691`. The call at `:2948` has no try/except, so the whole run() aborts — and this is *after* the token try/except at `:2879`, so `notify_bot_down()` is never reached either. Silent death, no down-notification.
- **Far-future timestamp → permanently stuck open.** There is no sanity clamp on `blocked_until_ts` (`:687-698`). A value far in the future (clock jump, manual edit, or a buggy prior write) makes the breaker return `False` on every run, forever. Because the breaker's only self-heal (`_clear_ebay_circuit_breaker_if_tripped()`, `:717`) fires only after a *successful search*, and no search runs while blocked, there is no path back out — permanent eBay silence.

(Truncated/corrupt *JSON* is safe: `JSONDecodeError` → `{}` → breaker treated healthy. The risk is specifically valid JSON with bad values.)

### 2. Token cache write failure aborts the run even though a valid token was just minted
`ebay_deal_alert.py:328-332` (`_write_cached_ebay_token`), call at `:359`.

`_write_cached_ebay_token()` has no try/except. It is called inside `get_ebay_token()`'s try block, whose `except` (`:361`) only catches `RequestException/KeyError/ValueError`. So:

- An `OSError` writing the cache (read-only FS, disk full, permission) propagates out of `get_ebay_token()`, aborting the run — after a token was already successfully obtained. The cache is pure optimization; its failure should be logged, not fatal.
- `int(expires_in)` (`:329`) raises `TypeError` if eBay returns `"expires_in": null` — `.get("expires_in", 7200)` (`:359`) only defaults when the key is *absent*, not when it's `null`. `TypeError` is also not in the `except` list, so the same uncaught abort.

Both are non-essential-failure crashes on the one function whose failure the spec flags as aborting the entire run.

### 3. `append_alert_log()` writes the `price` field with three different meanings
`ebay_deal_alert.py:2357-2360` (fallback), `:1544` (score_listing), `:1552-1606` (early returns), `:3269-3271` (near-miss).

The `price` field in `alerts_log.jsonl` is not one unit:

- **Raw item price** — for PASS records that come from `score_listing()`'s early returns (`:1552,1554,1556,1560,1568,1588,1596,1606`): gender/pet/obfuscated/pass-brand/logo/poly/condition hard-fails. Those dicts have no `price` key, so `append_alert_log()` falls back to `listing["price"]["value"]` (`:2359`) — item only, no shipping, no tax.
- **Item + shipping + 6% tax** — for REVIEW/alerted records and gate-blocked PASS records that come from `score_listing()`'s final return (`price` computed at `:1544`).
- **Item + shipping (no tax)** — for the inline "over max price" near-miss record (`:3269-3271`, `"price": total_price` where `total_price = item_price + shipping_cost` at `:3149`).

Concrete impact: the mobile app reads `price` directly. A PASS near-miss (the record class meant to help tune `max_price` too low) can show item-only price, understating real landed cost by shipping + tax — e.g. a $15 item + $10 shipping logged as `price: 15`. Any price sort/filter across the log mixes three units. `item_price`/`shipping_cost` are also written for most records (`:2373-2374`), so the app *can* reconstruct landed cost — but the `price` field itself is inconsistent. The weekly digest is unaffected (it never reads `price`).

### 4. Weekly digest headline counts suppressed PASS records as "alerts"
`ebay_deal_alert.py:2630-2645`, `:2698`.

`recent_records` is every log line in the last 7 days, and the log contains PASS/near-miss/suppressed records (see `:3349,3546,3585,3614,3630,3665,3825`). The headline `f"{len(recent_records)} alerts {window_label}"` therefore counts suppressed candidates as sent alerts. A week that found 50 candidates but actually pushed 3 alerts reads "50 alerts this week". The `rating_parts` line (`:2690-2694`) only counts records with a `deal_rating`, so the same message can say "50 alerts ... 2 Steals, 1 Great Deal" — internally inconsistent. Should count only sent-alert records (verdict != PASS, or presence of `deal_rating`/the alert send).

---

## LOW

### 5. `_trip_ebay_circuit_breaker()` exponentiation before the cap; streak type unchecked
`ebay_deal_alert.py:706` (`min(EBAY_BACKOFF_INITIAL_MINUTES * (2 ** (streak - 1)), EBAY_BACKOFF_MAX_MINUTES)`), `:705`.

- The `2 ** (streak - 1)` is evaluated *before* `min()` caps it. Python has no int overflow, but a corrupt huge `consecutive_429_streak` (e.g. `1e6`) computes a ~300k-digit number first — slow/hang on a value read straight from disk. Cap the exponent, not just the product.
- `streak = state.get("consecutive_429_streak", 0) + 1` (`:705`) raises `TypeError` if the stored value is a string; called inside the 429 except handler (`:3018/:3033`), so it would propagate out of the handler.

### 6. `prune_old_seen_entries()` VACUUM can collide with a concurrent 5-min run
`ebay_deal_alert.py:922` (`conn.execute("VACUUM")`), caller `:2877-2878`.

VACUUM rebuilds the whole file under an exclusive lock; `sqlite3.connect` (`:871`) uses the default 5s timeout. If a run overlaps the 7:00–7:15 window (cron overrun or a manual invocation), `VACUUM` raises `OperationalError: database is locked`, which is uncaught — it fires at the top of `run()` before the token try/except, aborting that cycle. No data-loss risk (deletes are committed at `:916` before VACUUM; VACUUM itself is transactional), but the lock failure path is unguarded. Low probability given runs are deadline-bounded, but a one-line try/except around the VACUUM would make the once/day housekeeping non-fatal.

### 7. `listing_fingerprint()` is title+seller only — collides across distinct items, misses real relists
`ebay_deal_alert.py:1004-1019`, relist gate `:3165-3181`.

- **False match:** same seller + same normalized title collapses genuinely distinct items (a bulk seller with multiple identical "Brooks Brothers navy blazer 42R" listings). The pricier one is then suppressed as a relist by `total_price >= best_price * 0.95` (`:3175`).
- **False miss:** any title change on a real relist (typo fix, added keyword, changed size token) changes the sha256 → no match → the item re-alerts as new / re-burns an AI call.
- **No seller → no fingerprint:** `:1005-1007` returns `None` when `seller.username` is absent, disabling relist/undercut protection entirely for that listing (no fingerprint → the `:3165` block is skipped).

### 8. `get_shipping_cost()` reads only `shippingOptions[0]`
`ebay_deal_alert.py:1107-1112`.

If a listing has multiple shipping options and the first is free/cheap (e.g. "Local pickup" $0 before a real $15 shipping option), landed cost is understated. Taking `min()` over options, or the first non-zero, would be safer; as written the first option wins.

### 9. `draft_resale_listing()` reads image files outside the try
`ebay_deal_alert.py:1821` (`with path.open("rb")`).

The `open()`/`read()` loop is *before* the try/except (`:1840`). A bad/empty `image_paths` entry raises uncaught `FileNotFoundError`. Manual CLI tool, so low — but a clean error beats a stack trace (the same reasoning already applied to the Gemini call at `:1842-1850`).

### 10. `append_alert_log()` drops the entire log if a single record exceeds the cap
`ebay_deal_alert.py:2404-2407`.

The trim loop breaks on the first oversized line; if one record's bytes alone exceed `ALERTS_LOG_MAX_BYTES`, `kept` stays empty and the file is rewritten empty (all history lost). Practically unreachable (records are bounded well under 800KB), but a `if not kept: kept = [line]` guard would preserve the newest record.

---

## Confirmed NOT problems

- **Backoff math off-by-one / overflow:** `30 * 2**(streak-1)` yields 30/60/120/120… — first trip 30 min, cap reached at streak 3, correct doubling. No off-by-one; Python ints don't overflow (the real issue is the pre-cap exponentiation in finding 5, not overflow).
- **Streak reset:** `_clear_ebay_circuit_breaker_if_tripped()` is called after every successful auction (`:3015`) and regular (`:3030`) search, and clears on any non-zero streak. No reset-failure under normal operation.
- **`append_alert_log()` byte trim drops OLDEST, keeps NEWEST:** `reversed(lines)` iterates newest-first, breaking before an older line would exceed the cap (`:2404-2409`). Newline accounting is exact: `+1` per line for `"\n"` written under `newline=""` (`:2417-2419`), so the file cannot exceed the cap.
- **Token cache expiry/300s margin:** margin is baked in at write (`:329`); a missing `expires_at` reads as 0 and forces a refetch (`:320`); `float(...)`/JSON errors are all caught (`:323`). Correct.
- **`prune_old_seen_entries()` cutoff comparison:** both cutoff and row `seen_at` are `datetime.now(timezone.utc).isoformat()`, so UTC string comparison is chronological. Correct.
