# Test Suite Audit — test_ebay_deal_alert.py (143 tests)

Scope: read `test_ebay_deal_alert.py` in full, `ebay_deal_alert.py` in full,
`platforms.py` as needed. Read-only.

**Bottom line:** the suite is strong at the *leaf* level (pure functions and
regexes are covered exhaustively) and almost absent at the *orchestrator*
level. Every finding below that matters is the same shape: the gate/DB/dedupe
*functions* are unit-tested in isolation, but the **wiring in `run()`** that
calls them is untested — which is exactly where the two cited production bugs
(the pre-AI discard, the dict/list crash) actually lived. Findings ranked by
real risk left uncovered.

---

## RANK 1 — `run()` has zero direct tests (the orchestrator is untested)

**Where:** `ebay_deal_alert.py:2869-3890` (`run()`). The only test that
indirectly touches any of it is `GrailedBatching.test_batch_adapter_called_once_not_per_search`
(`test_ebay_deal_alert.py:1665`), which calls `prefetch_marketplaces()` — a
helper, not `run()`.

**Why it's weak:** Both bugs the spec cites lived *only* in `run()`:
- the dict/list type confusion: `review_candidates` is a dict keyed by
  `item_id` (`ebay_deal_alert.py:2996`, appended at `:3397`), then
  `list(review_candidates)` at `:3433` is expected to yield item-ids for
  `get_ai_pending_minutes()`, then it's rebinding to a list at `:3517`.
  None of these transitions is exercised by a test; a one-line mistake here
  crashes every run and stays green.
- the pre-AI discard: `run()`'s pre-AI skip at `:3541-3548` and the
  mark_seen-vs-mark_ai_pending dispatch at `:3842-3851` implement the rule
  that makes the "no AI price" marker safe; no test drives them.

Every `is_blocked_by_steal_quality_gate`, `mark_seen`, `mark_ai_pending`,
`is_new`, `is_jacket_only_suit_listing` test proves the *leaf* is right and
says nothing about whether `run()` calls them in the right order, with the
right arguments, at the right time.

**Stronger version — the single highest-value test to add** (would have caught
*both* cited bugs, fast + deterministic, no network, no Gemini):

```python
class RunEndToEnd(unittest.TestCase):
    def test_unvetted_candidate_is_left_unseen_not_discarded(self):
        item_id = "e1"
        listing = {"itemId": item_id, "title": "Loro Piana Cashmere Sweater L",
                   "price": {"value": 50.0, "currency": "USD"},
                   "seller": {"username": "s"}, "itemWebUrl": "https://x/1",
                   "image": {"imageUrl": "https://x/1.jpg"}}
        search = {"query": "loro piana cashmere sweater", "enabled": True,
                  "platforms": [], "max_price": 200}
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(m, "DB_PATH", f"{d}/seen.db"), \
             mock.patch.object(m, "ALERTS_LOG_PATH", Path(d) / "alerts.jsonl"), \
             mock.patch.object(m, "EBAY_RATE_LIMIT_STATE_PATH", Path(d) / "rl.json"), \
             mock.patch.object(m, "SAVED_SEARCHES", [search]), \
             mock.patch.object(m, "EBAY_AUCTION_SEARCHES", []), \
             mock.patch.object(m, "GEMINI_CALL_LIMIT", 0), \
             mock.patch.object(m, "get_ebay_token", return_value="t"), \
             mock.patch.object(m, "fetch_gap_report", return_value={}), \
             mock.patch.object(m, "prefetch_marketplaces", return_value={}), \
             mock.patch.object(m, "search_ebay", return_value=([listing], 1)), \
             mock.patch.object(m, "search_ebay_ending_soon_auctions", return_value=([], 0)), \
             mock.patch.object(m, "ebay_circuit_breaker_allows_calls", return_value=True), \
             mock.patch.object(m, "get_ebay_rate_limit_remaining", return_value=(4000, 5000)), \
             mock.patch.object(m, "check_photos_with_gemini") as gemini, \
             mock.patch.object(m, "send_alert") as send_alert:
            m.run()
        conn = sqlite3.connect(f"{d}/seen.db")
        self.assertTrue(m.is_new(conn, item_id))          # pre-AI discard regression
        self.assertIn(item_id, m.get_ai_pending_minutes(conn, [item_id]))  # aging path ran
        gemini.assert_not_called()
        send_alert.assert_not_called()
```

Why this one test catches both bugs:
- **pre-AI discard:** with `GEMINI_CALL_LIMIT=0` the candidate never gets AI.
  The knitwear gate returns `"…no AI price estimate yet…"` (retry-eligible).
  Correct code leaves it unseen (`is_new == True`) and records
  `mark_ai_pending`. The buggy gate ("below Steal" fires on `None`) returns a
  permanent reason, so `run()`'s pre-AI skip at `:3542` would `mark_seen` it
  and `is_new` becomes `False` → test fails.
- **dict/list crash:** the single REVIEW candidate forces `run()` through the
  full `review_candidates` dict→`list()`→`sorted(...values())`→PASS-3 sequence;
  the buggy transition raises and the test fails on the exception itself.

Mock the *narrow seams* `run()` already has (`get_ebay_token`,
`fetch_gap_report`, `prefetch_marketplaces`, `search_ebay`,
`search_ebay_ending_soon_auctions`, `check_photos_with_gemini`,
`send_alert`), not `requests` globally — higher fidelity, no network.
Redirect `DB_PATH`/`ALERTS_LOG_PATH`/`EBAY_RATE_LIMIT_STATE_PATH` to a
`tempfile` dir. Pin `SAVED_SEARCHES`/`EBAY_AUCTION_SEARCHES`/
`GEMINI_CALL_LIMIT` so the rotation and budget are deterministic. A companion
variant with `GEMINI_CALL_LIMIT=1` and `check_photos_with_gemini` returning a
Steal-worthy result asserting `send_alert` is called once is the natural
second test, but the one above is the highest-value single add.

---

## RANK 2 — "every alert is AI-vetted" rule untested; blind-trust tests assert now-dead behavior

**Where:** the rule is `ebay_deal_alert.py:3817-3820`
(`if not gate_reason and ai_result is None: gate_reason = "no AI price estimate - every alert must be AI-vetted before sending"`).
It has **no test**. Meanwhile three existing tests assert the *opposite* at the
unit level — that grab_on_sight + no-AI-data *clears* the gate:

- `StealQualityGate.test_default_category_no_ai_data_blind_trusts_grab_on_sight_only` (`test_ebay_deal_alert.py:1017`)
- `StealQualityGate.test_other_grab_on_sight_brands_still_blind_trust_normally` (`:748`)
- `StealQualityGate.test_suit_blind_trust_has_a_price_ceiling` (`:877`, the `under` case at `:894-895`)

**Why it's weak / false confidence:** those three call
`is_blocked_by_steal_quality_gate()` directly, which returns `None` (blind
trust) for grab_on_sight with `deal_rating is None`. But `run()` now overrides
that path: even when the gate returns `None`, `ai_result is None` re-blocks the
candidate with the "no AI price" reason. The tests green-light a code path that
can no longer ship an alert, and nothing in the suite pins the *actual*
criterion the rule uses — which is `ai_result is not None`, **not**
`deal_rating is not None`. A grab_on_sight item where Gemini ran but abstained
on price (`ai_result` present, no `estimated_resale_value`) would still
blind-trust alert, and no test documents or guards that edge.

**Stronger version:** a `run()`-level test (extends RANK 1's harness):
`GEMINI_CALL_LIMIT=1`, `check_photos_with_gemini` returns a result with
`looks_good`/no price fields, candidate is grab_on_sight + `deal_rating None`
→ assert `send_alert.assert_not_called()` and the item is left unseen. That
pins the real rule (`ai_result` gates the alert), not the gate function's
isolated return value.

---

## RANK 3 — `review_candidates` dedupe/merge untested (the exact code of the latest fix)

**Where:** `ebay_deal_alert.py:3353-3405` — the `item_id` dedupe, the
"prefer the ending-soon auction copy", and the enrichment merge loop
(`for enrichment in ("sold_comp_median", "sold_comp_count", "description")`).

**Why it's missing:** commit `886683f` ("Auction dedupe was destroying
sold-comp price evidence; move already-alerted check earlier") fixed precisely
this merge — that an auction copy replacing a regular-search copy must carry
over `sold_comp_median`/`sold_comp_count`/`description`, or a real steal loses
its comps and gets gate-blocked. There is no test that two listings with the
same `item_id` (one plain, one `is_ending_soon_auction`) merge correctly, or
that a duplicate plain copy is dropped rather than double-spending an AI call /
double-alerting. This is the most recently-changed non-trivial logic in the
file and it is completely uncovered.

**Stronger version:** extract nothing — test it through `run()` (RANK 1
harness) with `search_ebay` returning a plain listing and
`search_ebay_ending_soon_auctions` returning the same `item_id` with
`sold_comp_median` missing but the plain copy carrying it; assert exactly one
AI call was attempted for that item and that the alerted result retained the
`$sold_comp_median`. Alternatively, if this logic is worth a unit, it must be
factored out first — today it's inline in `run()`, which is *why* it has no
test.

---

## RANK 4 — auction `is_new` bypass + `auction-alerted:{item_id}` key untested

**Where:** the seen-dedupe bypass `ebay_deal_alert.py:3130`
(`if not listing.get("is_ending_soon_auction") and not is_new(conn, item_id): continue`),
the early already-alerted guard `:3143`, and the send-time namespaced key
`:3864-3868`.

**Why it's missing:** `EbayEndingSoonAuctions` (`test_ebay_deal_alert.py:1386-1450`)
tests only `search_ebay_ending_soon_auctions()`'s window filtering — not how
`run()` consumes `is_ending_soon_auction`. Three recent commits (the two
auction fixes and the dedupe fix) all touched these lines, and none of the
behavior is pinned: that an in-window auction is reconsidered even when
`seen` already contains its `item_id`; that an auction already alerted on is
dropped *before* burning a Gemini call (the `auction-alerted:` key); that the
namespaced key is written on alert and survives the `is_new` bypass so the same
auction doesn't re-alert across its 15-minute window. A regression here either
spams duplicate pushes or re-kills the entire auction lane — both silent, both
ship on the 5-min cron.

**Stronger version:** `run()`-level (RANK 1 harness) with two passes: pass 1
alerts on an auction, pass 2 (fresh `run()`) returns the same auction and must
NOT call `send_alert` again — proving the `auction-alerted:{item_id}` key
holds against the `is_new` bypass.

---

## RANK 5 — `_ai_check_priority` sort key untested

**Where:** `ebay_deal_alert.py:3436-3515` — the priority tuple (auction-first,
`must_have_ai` before blind-trust-able, `mass_market_watch` deprioritization,
age, then price-descending).

**Why it's missing:** this is the most intricate pure logic in the file and it
is entirely untested. In particular `mass_market_watch` (`:3472-3476`, the
`band[1] < 500` deprioritization of cheap watches) and the
`one_check_from_alerting` split (`:3453-3455`, which re-derives must-have-AI
from the gate's "no AI price" marker) have no coverage. The documented 4-hour
starvation bug this sort was rebuilt to fix would regress with zero test
failure. It's also *only* reachable through `run()` today, so it shares
RANK 1's root cause.

**Stronger version:** drive `run()` with two candidates — a
`must_have_ai=True` standard-tier watch and a `grab_on_sight` knitwear — with
`GEMINI_CALL_LIMIT=1`, and assert the AI call went to the *must-have-AI*
candidate (the one that cannot alert without it), not the grab_on_sight one.
This is a cheaper, behavior-level proxy for testing the sort directly.

---

## RANK 6 — pre-AI gate skip + `no AI price` mark_seen-vs-mark_ai_pending dispatch untested

**Where:** `ebay_deal_alert.py:3541-3548` (skip spending an AI call on a
permanently-gate-blocked candidate) and `:3842-3851` (mark_seen only when the
reason is NOT retry-eligible, else `mark_ai_pending`).

**Why it's missing:** the *marker* is tested (`test_permanent_blocks_are_distinguishable_from_needs_ai`,
`test_all_never_got_ai_check_reasons_share_the_no_ai_price_substring`,
`test_ai_evaluated_rejections_do_not_share_the_no_ai_price_substring`), but
the *dispatch on it* in `run()` is not. The `"no AI price" in gate_reason`
substring is a load-bearing string contract between the gate and `run()` —
nothing tests that a candidate whose gate reason *contains* the marker is
actually left unseen, and one whose reason *lacks* it is actually marked seen.
This is the exact seam where the pre-AI discard bug lived. The RANK 1 test
covers it; no dedicated test exists.

---

## RANK 7 — `_attach_seller_feedback` untested (gate tests would pass if it were deleted)

**Where:** `_attach_seller_feedback` (`ebay_deal_alert.py:368-394`) is the
only place eBay's raw `seller.feedbackScore`/`seller.feedbackPercentage`
(string `"99.2"` → float `99.2`, missing-field → `None`) is parsed.

**Why it's weak:** every test in `SellerFeedbackWatchGate`
(`test_ebay_deal_alert.py:1058-1127`) and the send_alert feedback tests
(`:1562-1581`) inject `seller_feedback_score`/`seller_feedback_percentage`
directly as already-parsed floats. If `_attach_seller_feedback` were deleted,
or the string→float parse broke (a `"99.2"` string left as str makes
`feedback_pct >= 95.0` raise `TypeError`), the whole suite stays green. The
gate is tested against a value the production code never actually produces.

**Stronger version:** one test that feeds a raw eBay `item_summary`
(`{"seller": {"feedbackScore": 5, "feedbackPercentage": "90.0"}}`) through
`_attach_seller_feedback` and asserts the flat fields come out as `int`/`float`,
plus the missing-fields case yields `None`/`None`.

---

## RANK 8 — `search_total_listings` wiring untested (saturation gate would go blind in production)

**Where:** `ebay_deal_alert.py:3339-3340` is the only place
`search_total_listings` gets copied from the search response onto `result`.

**Why it's weak:** `MarketSaturationGate` (`test_ebay_deal_alert.py:1130-1171`)
injects `search_total_listings` directly into its fixtures. If the wiring in
`run()` were removed or the key name drifted, the saturation gate would never
fire in production while all five tests stay green. Same false-confidence shape
as RANK 7: the gate is tested against a field that nothing proves `run()` still
supplies.

**Stronger version:** assert through the RANK 1 harness that a
`search_total_listings` returned by `search_ebay` reaches the gate and blocks a
Great-Deal candidate — or unit-test `run()`'s result-assembly separately.

---

## RANK 9 — `test_fingerprint_not_written_without_mark_seen` cannot fail (vacuous)

**Where:** `MarkSeenFingerprintTiming.test_fingerprint_not_written_without_mark_seen`
(`test_ebay_deal_alert.py:352-358`).

**Why:** the test asserts `get_fingerprint_best_price(conn, "abc123") is None`
on a freshly-created empty table. It never invokes the collection path it
claims to guard against ("fingerprints written at COLLECTION time"). It proves
only that an empty table returns `None` — an invariant of the test's own
`setUp`, not a behavior of the code. If the bug it is named for were
reintroduced (a fingerprint written the moment a listing is collected), this
test would still pass, because nothing in it performs a collection.

**Stronger version:** either delete it (the atomicity is already covered by
`test_mark_seen_writes_both_atomically`, `:360-363`) or make it assert the
*actual* contract: that `listing_fingerprint()` + `get_fingerprint_best_price()`
write nothing, by calling the real collection seam it claims to protect. As
written it is a no-op that documents intent without testing anything.

---

## RANK 10 — `SizeMatching` tests a copy of `run()`'s inline regex, not the code

**Where:** `SizeMatching` (`test_ebay_deal_alert.py:253-279`) re-implements
both the size-normalization `re.sub(r"\b(\d{2})\s?(R|L|S|XL|XS)\b", …)` and the
`(?!\.\d)` half-size guard inside `_normalize()`. The class docstring admits it:
the logic is "inlined in run()… these tests exercise the identical regex logic
in isolation."

**Why it's weak:** the production logic lives at `ebay_deal_alert.py:3210-3226`.
If `run()`'s inline regex or the size filter diverged from the copy here (the
normalization dropped, the description folded into `size_haystack` removed, the
`(?!\.\d)` guard deleted), the tests stay green — and this is the exact filter
that once silently dropped 16 of 30 real suits. The tests pass while guarding a
regex that exists in two places, only one of which matters.

**Stronger version:** assert the production behavior through `run()` (a suit
listed as `"Kiton Suit 42R"` against `size:["42"]` must reach REVIEW, not be
`mark_seen`-dropped) — or, if it stays a unit, the regex must be hoisted to a
module-level constant in `ebay_deal_alert.py` that both `run()` and the test
import, so the two can't drift.

---

## RANK 11 — jacket-only *description* re-check site in PASS 3 untested

**Where:** `ebay_deal_alert.py:3575-3587` — the second
`is_jacket_only_suit_listing(title, query, description)` call, which is the one
that makes eBay descriptions effective (they're only fetched at `:3562-3565`).

**Why it's lower risk:** the *function* is well covered, including description
inputs (`test_description_disclaimer_blocks_a_complete_looking_suit_title`,
`:141-160`; `test_description_check_does_not_overblock_real_two_piece_suits`,
`:162-176`). But no test proves `run()` actually re-checks after fetching the
eBay description — the wiring that turns "we now have a description" into
"block this listing" is untested. A dropped re-check would silently re-admit
exactly the jacket-only listings the user reported. Lower severity only because
the function itself is trustworthy.

**Stronger version:** RANK 1 harness where `fetch_ebay_item_description`
returns `"jacket only, pants not included"` for an eBay listing with a
clean title, and assert it is blocked (no alert, marked seen).

---

## RANK 12 — incidental wording / exact-format assertions that break on harmless refactors

Lower risk, but they couple the suite to exact strings and formatting that a
cleanup would break without catching any bug:

- `test_loro_piana_cucinelli_bar_requires_steal_tier` — `assertIn("below Steal", reason)`
  (`test_ebay_deal_alert.py:1010`) pins the f-string wording
  `"deal_rating '…' below Steal"`, not the branch.
- `test_gamecocks_bar_does_not_apply_to_off_target_matches` — `assertNotIn("gamecocks bar", reason)`
  (`:998`) distinguishes branches by reason-wording rather than by outcome.
- `AsciiSafeHeader.test_common_smart_punctuation_translated_not_just_stripped`
  (`:1515-1517`) asserts the exact output `"'quoted' - em-dash ..."` — a
  character-map detail, not a behavior.
- `SendAlertRetailResaleLine` (`:1542-1595`) asserts exact message substrings
  (`"retail ~$800"`, `"seller: 120 feedback, 99.2% positive"`,
  `"verify: https://…&LH_Sold=1…"`), which lock in `_format_estimated_usd`'s
  `round()` formatting and URL encoding; a formatting tweak breaks them without
  a real regression.

These are *not* the `assertIn("no AI price", reason)` assertions — those are a
genuine load-bearing contract (`run()` dispatches `mark_seen` vs
`mark_ai_pending` on that exact substring) and should stay.

**Stronger version:** for the gate-branch ones, assert the *outcome* (blocked
vs not blocked) plus, where the marker matters, the marker — not the
human-readable branch label. For the send_alert/format ones, assert presence of
a value (`retail ~$` + the rounded number) or the URL's query parameters
parsed, not a full literal string.

---

## Summary table

| Rank | Finding | file:line | Can the suite go green while prod breaks? |
|------|---------|-----------|-------------------------------------------|
| 1 | `run()` orchestrator untested (dict/list + pre-AI discard both lived here) | ebay:2869-3890 | yes — the two cited bugs did exactly that |
| 2 | every-alert-AI-vetted rule untested; blind-trust tests assert dead behavior | ebay:3817 / test:1017,748,877 | yes |
| 3 | review_candidates dedupe/merge untested | ebay:3353-3405 | yes (commit 886683f is uncovered) |
| 4 | auction is_new bypass + `auction-alerted:` key untested | ebay:3130,3143,3864 | yes (recent auction fixes uncovered) |
| 5 | `_ai_check_priority` sort untested | ebay:3436-3515 | yes |
| 6 | pre-AI skip + no-AI-price dispatch untested | ebay:3541-3548,3842-3851 | yes |
| 7 | `_attach_seller_feedback` untested | ebay:368-394 | yes (tests inject parsed floats) |
| 8 | `search_total_listings` wiring untested | ebay:3339-3340 | yes (tests inject the field) |
| 9 | vacuous `test_fingerprint_not_written_without_mark_seen` | test:352-358 | n/a (never fails, never catches) |
| 10 | `SizeMatching` tests a copy of run()'s regex | test:253-279 | yes |
| 11 | jacket-only description re-check site untested | ebay:3575-3587 | yes |
| 12 | incidental wording/format assertions | test:998,1010,1515,1542-1595 | breaks on harmless refactor, catches little |

The single highest-leverage fix is RANK 1's `run()` end-to-end test; RANKS
2-6, 8, 11 all become reachable once that harness exists.
