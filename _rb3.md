# _rb3 — High-value improvements, grounded in alerts_log.jsonl

**Data window:** 722 log rows, 2026-08-16T21:11Z → 2026-08-17T02:46Z (5.58 h, 58 runs detected by ≥90 s gaps). 716 PASS / 6 REVIEW. Small window — one night, not a season — so treat *rates* as solid and *absolute daily totals* as extrapolation. Enrichment fields are sparse (`brand_tier` on 49/722, `deal_rating` on 83/722) because most candidates die before enrichment; that sparsity is itself finding #2.

Config baseline: 117 saved searches, 101 enabled (37 fast, 64 slow). Fast lane 11/run → each fast search revisited every **20 min**; slow lane 4/run → every **80 min** (18 looks/day).

---

## 1. Which searches earn their slot

Every candidate that reached the log, by query and outcome:

| Query | Total | REVIEW (sent) | noAI¹ | lowRating² | gender | photoFail | condition | logo | noAI-est³ |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| seiko watch | 361 | 1 | 292 | 22 | 35 | 5 | 6 | 0 | 0 |
| bulova watch | 254 | 1 | 196 | 21 | 23 | 11 | 2 | 0 | 0 |
| peter millar gamecocks jacket | 49 | 0 | 0 | 24 | 0 | 1 | 1 | 9 | 11 |
| movado watch | 34 | 0 | 23 | 3 | 7 | 1 | 0 | 0 | 0 |
| hamilton watch | 6 | 0 | 2 | 1 | 3 | 0 | 0 | 0 | 0 |
| allen edmonds | 4 | 1 | 0 | 1 | 0 | 0 | 0 | 0 | 2 |
| trafalgar belt | 3 | 0 | 0 | 2 | 0 | 0 | 0 | 0 | 1 |
| longines watch | 2 | 1 | 0 | 0 | 1 | 0 | 0 | 0 | 0 |
| omega watch | 2 | 0 | 1 | 1 | 0 | 0 | 0 | 0 | 0 |
| ferragamo belt | 1 | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| loro piana suit | 1 | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| zegna sweater, pringle, rolex, zenith, vacheron | 5 | 0 | 0 | 1 | 0 | 1 | 2 | 0 | 0 |

¹ `watches bar: no AI price/authenticity check ran` ² any `deal_rating below steal bar` ³ `no AI price estimate and brand not grab_on_sight-tier`

### The headline

**Three queries — `seiko watch`, `bulova watch`, `movado watch` — are 649 of 722 candidates (89.9%) and 511 of 514 noAI blocks. They produced 2 REVIEWs in 5.6 h, and both were ShopGoodwill mixed junk lots:**

- *"Bulova Emporio Armani Citizen Skagen Dress Watch Lot"* — $36 + $13.50 ship, "brand not recognized — manual check needed"
- *"Seiko Pulsar Gruen & Sheffield Assorted Vintage Watches Lot"* — $10 + $13.50 ship, same

Neither is a wearable Ivy-trad watch or a clean resale flip. So the mass-market watch lane's true yield over this window is **0 usable alerts for 90% of the pipeline's throughput.**

The mechanism is listing volume, straight from `search_total_listings`:

| Query | Live listings | Candidates produced | Usable alerts |
|---|---:|---:|---:|
| seiko watch | **22,321** | 361 | 0 |
| bulova watch | **11,152** | 254 | 0 |
| movado watch | 1,336 | 34 | 0 |
| loro piana suit | 1,391 | 1 | **1 (Steal, 71% off, comps-backed)** |
| trafalgar belt | 993 | 3 | 0 |
| hamilton watch | 849 | 6 | 0 |
| longines watch | 339 | 2 | **1** |
| omega watch | 288 | 2 | 0 |
| zenith watch | 56 | 1 | 0 |

A 22k-listing query at `max_price 150` is a haystack query: it returns the *cheapest 22,321-deep tail* of a brand whose median used piece is worth about what you're paying. Confirmation: of the 514 noAI-blocked watches, **303 (59%) were priced above their own brand's `WATCH_PRICE_BANDS` low** — i.e. even the config's own valuation floor says there was no steal margin to find. And the queue is buying the wrong end of the tail anyway: median price of a noAI-blocked watch was **$73** (p25 $48, p75 $90).

### Specific calls

**Drop outright:**
- `bulova watch` — 254 candidates, 0 usable alerts, 11,152-listing haystack, band avg $250 vs `max_price 100`. There is no Bulova at $100 that is both a steal and worth your closet space.
- `movado watch` — 34 candidates, 0 alerts, and the config's own `WATCH_PRICE_BANDS._comment` already records that Movado is where the AI hallucinated $595–795 resale on $150–550 watches. The brand is a known false-positive generator.
- `peter millar gamecocks jacket` — 49 candidates, **0 REVIEWs**, worst signature in the file: 24 lowRating + 9 corporate-logo + 11 no-AI-estimate. It's a *fast*-profile search, so it burns 1/37th of the 20-min lane forever to produce nothing. The polo/quarter-zip variants are fine; the jacket one is dead.

**Retune, don't drop:**
- `seiko watch` → replace the generic brand token with model-level queries. Generic Seiko is 22k listings of $40 quartz; the money is in specific references. Suggested replacements at the same daily call cost (1 slot → 3 slots, but each returns a needle not a haystack): `"king seiko"`, `seiko 6139` (Pogue chrono), `seiko alpinist SARB017`, `seiko "cocktail time"`, `seiko 6105`/`6309` (divers). Keep `grand seiko` as-is — it's already model-precise and produced zero noise.
- Raise `max_price` on the *good* lanes rather than adding new brands. `zenith watch` (56 listings), `omega watch` (288), `longines watch` (339) are needle queries producing near-zero noise; they can afford wider price windows without any of the flood dynamics above.

**Deduplicate:** `peter millar gamecocks polo` and `"peter millar" gamecocks polo` are both enabled and both hit eBay. Same for the quarter-zip pair (one disabled). That's a wasted rotation slot.

**Add (cheap, needle-shaped, matches your stated wardrobe):** the config has no `drake's london tie`, `hermes tie`, `sid mashburn`, `o'connell's`, `southwick blazer`, or `rota trousers` — all Ivy-trad, all low-listing-count, all in the same free-API envelope. Add them to the *slow* lane only after removing the three above, so the 80-min cycle doesn't get longer.

---

## 2. Block reasons: where effort dies

| Reason | Count | % of PASS |
|---|---:|---:|
| **watches bar: no AI price/authenticity check ran** | **514** | **71.8%** |
| excluded gender keyword in title/description | 69 | 9.6% |
| deal_rating below steal bar (all variants) | 76 | 10.6% |
| no AI price estimate and brand not grab_on_sight | 14 | 2.0% |
| AI photo check found damage / unwanted logo | 19 | 2.7% |
| condition hard-fail keyword | 11 | 1.5% |
| corporate logo keyword | 9 | 1.3% |

### What the pattern says

**(a) The dominant block isn't a judgment, it's a budget failure.** `deal_rating is None` at `ebay_deal_alert.py:2226` means *no AI check ever ran* — the candidate was fetched, scored, deduped, gated, and logged, and then thrown away for lacking the one input the pipeline chose not to buy. 71.8% of all work product is discarded for want of a call the program declined to make.

**(b) The AI budget is simultaneously starved and idle.** This is the single most actionable number in the file:

```
58 runs × 3 calls = 174 AI slots available
102 slots actually used                    (59% — approx; counts rows where
                                            deal_rating or an AI photo note exists)
 66 slots left idle on runs that starved nobody
393 candidates starved on runs that hit the cap
  8 runs used 0 of 3 slots      19 runs used all 3
```

The load is bursty and the cap is flat, so the two failure modes coexist. The bursts are perfectly predictable — they are the 80-minute slow-lane rotation bringing `seiko`/`bulova` back around:

```
21:11  29 candidates  AI used 0  starved 29
22:26  79 candidates  AI used 3  starved 68
23:46  77 candidates  AI used 2  starved 70
01:06  89 candidates  AI used 3  starved 75
02:26  83 candidates  AI used 3  starved 78
```

**(c) Over half the log is the same items being re-litigated.** 722 rows cover only **331 distinct items**. 133 items appear more than once; **391 rows (54% of the file) are re-scores of something already seen and already blocked.** Six items were logged 7 times each. Each re-score costs a scoring pass, a gate evaluation, a DB write and a log line, and — because they re-enter `review_candidates` — they keep competing for AI slots against genuinely new listings.

**(d) 69 gender blocks are being paid for at the wrong layer.** 65 of them are women's watches from the seiko/bulova/movado queries (*"Seiko Women's Quartz Wristwatch"*, *"Vintage Seiko Yellow Ladies Watch"*). Those queries already carry ~40 negative keywords; they just don't carry `-women -womens -ladies`. Every one of these consumed a result slot in eBay's response page, a scoring pass, and a log row, to be killed by a string match that eBay would have done for free.

**(e) eBay call budget is structurally over quota.** (11 fast + 4 slow + 3 auction) × 288 runs = **5,184 calls/day** against a ~5,000 cap, *before* the per-item `fetch_ebay_item_description()` calls inside the AI branch (up to 3/run = +864). The quota pre-check at `run()` (~line 2955) catches this and skips runs, which means the bot goes eBay-blind for the tail of each daily window rather than erroring — but it means there is **no headroom to add searches**. Every addition below must be paid for by a removal.

---

## 3. Improvements, ranked by value ÷ effort

### #1 — Rolling daily AI budget instead of a flat 3/run
**Value: very high. Effort: ~15 lines.**

Replace the per-run counter with a day-scoped counter persisted in the SQLite DB that already exists (`seen_items.db`, `init_db()` at :870). Keep a daily ceiling of ~850 (under the 1000 free-tier limit, matching today's 3×288=864) and a per-run burst maximum of ~12.

- **What the data says:** 66 idle slots and 393 starved candidates in the *same 5.6 hours*. A rolling budget converts idle capacity into checks on burst runs at zero additional daily spend. Burst runs peaked at 89 candidates; a 12-call burst cap covers ~4× today's throughput on exactly the runs that need it, and quiet runs (8 runs used 0 slots) bank the difference.
- **Where:** `GEMINI_CALL_LIMIT` (:248); `gemini_calls = 0` at `run()` :3521; new `ai_calls_today` table alongside `init_db()`. Read at run start, increment on each call, compare against `GEMINI_DAILY_LIMIT`.
- **What could go wrong:** Gemini free tier has a *per-minute* rate limit as well as per-day. Bursting 12 calls back-to-back could 429. Mitigate with the existing `GEMINI_INTER_CALL_SLEEP_SECONDS` (currently 2 s → 12 calls ≈ 24 s, fine inside a 5-min run) and treat a 429 as "budget exhausted this run," not a crash. Second risk: a clock/timezone bug in the day boundary could let a single day spend two days' budget — key the row on `date.today().isoformat()` in UTC and let it be self-correcting.

### #2 — Cut the three mass-market watch searches
**Value: very high. Effort: config-only, zero code.**

Set `enabled: false` on `bulova watch` and `movado watch`; replace `seiko watch` with 3–4 model-level queries.

- **What the data says:** 649/722 candidates (89.9%), 511/514 noAI blocks, 2 alerts and both junk lots. 303/514 blocked watches were priced above their own brand's band floor. Removing them removes ~90% of pipeline load and, combined with #1, means the *remaining* candidates get AI checks essentially on demand — the noAI block should approach zero without spending another cent.
- **Where:** `config.json` `SAVED_SEARCHES`.
- **What could go wrong:** you genuinely lose coverage of the occasional grail-in-the-haystack (a mispriced King Seiko listed as "seiko watch"). That's the real cost, and it's why the recommendation is *model-level replacement* for Seiko rather than deletion. Also: with the flood gone, verify a subsequent window still shows AI slots being used — if utilization collapses to near-zero, the budget freed by #1 should be reinvested in *more searches* (#7), not left idle.

### #3 — Push the negative keywords eBay already supports into the query strings
**Value: high. Effort: config-only, zero code.**

Append `-women -womens -ladies -lady -girls` to every watch query, matching the existing `-radio -canteen -bottle …` pattern.

- **What the data says:** 69 gender blocks, 65 from watch queries, 9.6% of all blocks. eBay filters these server-side for free; the current design pays a result slot, a scoring pass and a log row for each. Freeing ~10% of every 200-item response page for actual candidates is a pure gain.
- **Where:** `config.json` query strings only.
- **What could go wrong:** eBay's negative-keyword matching is substring-ish and can over-exclude — `-lady` would kill a "Ladyhawk" model name, and unisex 34 mm vintage pieces sometimes carry "ladies" in a title despite being wearable. Keep `-women -womens -ladies` and skip `-lady`. The `GENDER_EXCLUDE_KEYWORDS` check in `score_listing()` (:1552) stays as the backstop — this is belt-and-braces, not a replacement.

### #4 — Backoff on repeatedly-starved candidates
**Value: high. Effort: ~20 lines.**

After a candidate has been re-scored N times (say 5) without ever winning an AI slot, stop re-queuing it for a few hours instead of re-entering it every run.

- **What the data says:** 391 of 722 rows (54%) are re-scores; 133 items repeat; 6 items were logged 7× in 5.6 h. The `ai_pending` machinery (`get_ai_pending_minutes()` :926, `mark_ai_pending()` :949) already tracks wait time and already feeds the priority sort as `-pending_minutes` — the data is there, it just has no give-up path. Halving log churn also halves the noise in every future analysis like this one.
- **Where:** `run()` PASS 1 collect loop (~:3390) — skip enqueue when attempts exceed the threshold and last-attempt age is under the cooloff; increment an attempt counter in the same table.
- **What could go wrong:** the age-first tiebreak at :3513 exists precisely *because* items were starving forever (the comment documents a Brooks Brothers suit that retried 57 times). A backoff can re-open that starvation if the threshold is too aggressive. Set the cooloff to hours, not days, and exempt `is_ending_soon_auction` candidates entirely — they have no next run.

### #5 — Rank the AI queue by underpricing, not by absolute price
**Value: high. Effort: ~25 lines.**

The final tiebreak in `_ai_check_priority()` is `-(result.get("price"))` — most expensive first. Within a single search that is a weak proxy: the priciest Seiko in a 22k-listing tail is not the most underpriced one. Replace it (for watches, at minimum) with a *discount-vs-cohort* score computed from data already in hand: the median asking price of the same search's own returned listings.

- **What the data says:** the code's own comment at :3491 records that AI slots went to a Rolex crystal ($14.73), a loose second hand ($15.79) and a Vacheron price *tag* ($7.42) under the old cheapest-first sort — and the current price-descending fix targets the top of a distribution that is itself junk. Meanwhile the median noAI-blocked watch sat at $73 with p75 at $90, i.e. the candidates cluster tightly and absolute price barely separates them. A cohort-relative score does separate them, and costs **zero extra API calls** — `search_ebay()` already returns the full result set the median would be computed from.
- **Where:** compute a per-search median in `search_ebay()` (:397) or right after it in `run()`, stash it on the listing, and read it in `_ai_check_priority()` (:3436) as a new tuple element ahead of `-price`.
- **What could go wrong:** asking prices are not sold prices, and a cohort median over a flooded query is itself depressed — the exact market-saturation trap the gate already warns about at :2271. So this must be used **only for queue ordering**, never as evidence to alert on, and never as a substitute for the watch AI check at :2226. Guard on cohort size (skip when fewer than ~10 listings) or a single weird result will dominate the median.

### #6 — Cheap pre-filter: don't queue a watch that can't clear its own band
**Value: medium-high. Effort: ~10 lines.**

Before enqueuing a watch candidate, if `price > WATCH_PRICE_BANDS[brand][0] × k` (k ≈ 0.6) — i.e. it's already priced near or above the bottom of the brand's real resale range — don't create a candidate at all.

- **What the data says:** 303 of 514 noAI-blocked watches (59%) were priced *above* their brand's band low. There is no arithmetic under which those clear a "Steal / Great Deal" bar; they are guaranteed to consume a scoring pass, a gate call, a log row, and a queue position, and then be rejected. `watch_price_band()` (:1317) already exists and already does the brand lookup.
- **Where:** PASS 1 collect loop in `run()`, before the `review_candidates[item_id] = {...}` append (~:3395).
- **What could go wrong:** the bands are explicitly self-described as "rough… not a precise valuation," so a hard filter on soft numbers will drop real finds — a rare Seiko reference is worth many multiples of the generic Seiko band. Two mitigations: apply it only where the brand's band avg is under $500 (the same `mass_market_watch` test already used at :3472), and make it a *suppression* (skip silently, don't log) so it doesn't distort the block-reason stats you'll want to re-measure afterward.

### #7 — Rebalance rotation toward needle queries once #2 lands
**Value: medium. Effort: config-only.**

Slow lane is 64 searches at 4/run = 80-min revisit = 18 looks/day. `loro piana suit`, `edward green`, `kiton`, `john lobb` get the same 18 looks/day as `seiko watch` did — and those are the lanes where a real steal appears and sells in minutes. Removing 3 searches (#2) shortens the slow cycle to ~75 min; promoting the highest-value low-volume queries to the fast lane (20-min revisit) costs nothing extra because the lane sizes are fixed.

- **What the data says:** the only genuinely good alert in the window — `loro piana suit`, Hickey Freeman × Loro Piana cloth, $24.99 + $8.99, `deal_rating: Steal`, 71% off, `high` confidence backed by real Grailed comps (median $125, n=10) — came from a 1,391-listing needle query that produced exactly 1 candidate in 5.6 h. That's the shape of a good lane: near-zero noise, occasional real find. Give those lanes more looks per day.
- **Where:** `profile` field in `config.json`.
- **What could go wrong:** eBay call budget is already at 5,184/day against a ~5,000 cap (see 2e), so this must be a *reallocation*, never an addition. Moving a search from slow→fast doesn't change call count (lane sizes are fixed at 11 and 4); adding searches does. If you want net-new searches, remove `bulova`/`movado`/`gamecocks jacket` first and spend that budget.

### #8 — Widen sold-comp coverage beyond Grailed
**Value: medium. Effort: high — listed last for exactly that reason.**

`fetch_grailed_sold_comps()` (`platforms.py:331`) is the only real sold-price evidence in the system, and it only attaches to *Grailed* listings (`_grailed_hit_to_listing`, :377). eBay candidates — the overwhelming majority — never get comps, which is why they depend so totally on a vision model's guess.

- **What the data says:** the two best-evidenced REVIEWs in the window both rested on Grailed comps: the Ferragamo belt where comps ($115, n=49) *overrode* a medium-confidence AI estimate of $180, and the Loro Piana suit ($125, n=10). Meanwhile the code comment at :3718 records that of 97 alerts ever sent, 56 rested on a medium-confidence AI guess and only 13 on a high-confidence one. Comps are the difference between "the model thinks" and "this actually sold for."
- **Where:** query Grailed's sold index by *brand + item type* extracted from an eBay listing's title, and attach the result to eBay candidates the same way `_grailed_hit_to_listing()` does, feeding the existing override logic at :3734.
- **What could go wrong:** a lot, and it's already documented — `platforms.py:347` records that loose queries returned garbage ("oxxford suit" matched Taylor Stitch *oxford* shirts, median $28) which then got used as "genuine sold-price data." Cross-brand comp contamination is worse than no comps, because it launders a bad number into `price_confidence: high`. Also costs one extra Algolia call per candidate — needs the same budget discipline as the Gemini calls. And note the watches exclusion at :3736 is deliberate and should stay: a price median cannot address counterfeit risk.

---

## Suggested order

1. **#2 + #3** (config only, zero risk, removes ~90% of load) — do first, then re-measure for a day.
2. **#1** (rolling budget) — with load down, this likely drives the 71.8% noAI block toward zero on its own.
3. **#4 + #6** (churn and pre-filter) — cleans up the remaining 54% log duplication.
4. **#5** (queue ordering) — matters most once budget is no longer the binding constraint.
5. **#7**, then **#8** only if alert volume is still too thin afterward.

The one-line version: the bot is not short of API budget, it is spending 90% of its throughput on two brands that have never produced a usable alert, and throwing away 41% of its AI budget to a flat per-run cap while 393 candidates starve.
