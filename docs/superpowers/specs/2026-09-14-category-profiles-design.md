# Category Profiles: a reusable pre-AI triage architecture

Status: draft, pending user review
Author: Claude Sonnet 5, in collaboration with the repo owner
Date: 2026-09-14

## Purpose

Tonight's session built real, evidence-backed pre-AI quality gates for three
categories (golf-equipment, poker-chips, watches), each requiring its own
multi-hour research dispatch and hand-written Python (`GOLF_BLOCKED_BRANDS`,
`WATCH_NONFUNCTIONAL_TEXT_SIGNALS`, `POKER_INELIGIBLE_CONSTRUCTION_SIGNALS`,
and the category-specific `*_pre_ai_hard_fail_reason()` functions). The owner
wants to add more categories over time (cars, shirts, and whatever else they
personally shop for) without each one costing a from-scratch investigation and
a bespoke code path, while keeping the two things that made tonight's fixes
trustworthy: real evidence behind every rule, and AI cost is spent only on
candidates that survive free, deterministic triage first.

**Explicit non-goal:** this is a personal steal-detector, not a resale/flip
tool - confirmed directly by the owner ("this entire thing is for me not for
reselling. i just like good deals"). "Steal" means paying well under an
item's real fair value for something they'll personally use, not resale
margin. The `[fast-flip]` label already present in alert titles is legacy
naming from an earlier framing and is out of scope for this spec (noted as a
candidate for a later cosmetic cleanup, not addressed here).

**Explicit non-goal:** "gifts for friends" is not in scope. Verifying price
vs. fair value is a different problem from matching another person's taste;
folding it into this system would degrade both. Parked for a separate future
conversation if the owner wants it.

## Current architecture (what this builds on, not replaces)

- `_pretriage_score(candidate, ai_no_price_attempts)` (line 8270) returns
  `(repeat_failure_tier, promise_score)`. `repeat_failure_tier` penalizes
  candidates that already burned a real AI check with no usable result,
  capped at `AI_NO_PRICE_MAX_ATTEMPTS`. `promise_score` currently has exactly
  two contributors: real sold-comp backing (+2.0) and strong eBay seller
  feedback (+1.0) - honestly documented in the function's own docstring as
  "genuinely unvalidated against today's data" beyond those two signals, and
  a previously-tried "price vs. category max_price headroom" term was
  deliberately dropped because real evidence contradicted it.
- `_ai_check_priority` (referenced throughout PASS 3, exact definition to be
  re-confirmed at implementation time since line numbers drift with every
  commit) sorts the AI-check queue using that tuple plus a `pending_minutes`
  aging tiebreak.
- `mark_ai_pending()` (line 1870) / the `ai_pending` SQLite table already
  gives real cross-run carry-forward: a candidate that doesn't win a slot
  this run keeps its `first_seen_at` and accumulates priority via
  `pending_minutes` in a later run. This spec reuses it as-is - it does not
  need to be rebuilt.
- Category-specific pre-AI rejection today is three separate, hand-written
  mechanisms: `golf_wrong_item_title_reason()` (line 2307),
  `poker_pre_ai_hard_fail_reason()` (line 2403), and
  `watch_pre_ai_hard_fail_reason()` (line 2824), each called from its own
  place in the PASS 3 loop, each duplicating similar shapes of logic (title
  regex matching, hard reject with a logged reason) with no shared
  abstraction between them.
- `is_blocked_by_steal_quality_gate()` (line 5530) is the final post-AI gate
  that also branches per category.
- `config.json` is already 3,267 lines and holds every saved search plus
  every numeric constant touched tonight (`AI_PAID_MONTHLY_BUDGET_USD`,
  `GEMINI_CALL_LIMIT`, `GEMINI_BATCH_SIZE`, etc.). It is the wrong place to
  add a fourth category's worth of blocked-brand lists and price floors -
  this spec adds a sibling file instead of growing it further.

## Architecture

### Two layers

**Generic layer** - signals that apply regardless of category, extracted
from what already exists (today scattered inside watch-specific and
poker-specific code) into shared, reusable functions:
- Condition/nonfunctional language (`WATCH_NONFUNCTIONAL_TEXT_SIGNALS`
  generalizes almost as-is: "broken", "not working", "parts only", "damaged"
  aren't watch-specific; the false-positive care that already went into it -
  e.g. never matching "recently repaired" as if it meant "currently broken" -
  must carry over).
- Scam/bot signal scoring (soft, not hard-reject, per tonight's discussion of
  the "zero feedback + brand new listing" idea): zero-feedback-on-high-value,
  off-platform-payment language, implausibly-cheap-for-a-known-authentic-brand.
- Counterfeit/authenticity language detection (an equivalent already exists
  for watches at a line not yet re-confirmed; generalize its pattern, don't
  duplicate it).

**Category profile** - data, not code, per category. New file
`category_profiles.json` alongside `config.json`, not inside it. Each entry:

```json
{
  "golf-equipment": {
    "blocked_brands": ["top flite", "ram", "dunlop", "..."],
    "blocked_subtypes": [
      {"pattern": "muscle-back blade irons?", "reason": "advanced-player-only, wrong for a beginner buyer"}
    ],
    "price_floor_brands": {},
    "valuation_guidance": "Never transfer a full 2-PW/3-PW set comp onto a smaller partial group without matching count/condition evidence.",
    "steal_definition": "well under real fair value AND actually playable/usable for the buyer's stated skill level"
  }
}
```

Exact schema finalized during implementation planning, not frozen here -
the shape above is illustrative, not final. What *is* fixed by this spec:
it is a single structured file, versioned in git, loaded once at run start
the same way `config.json` already is, and every existing category
(golf-equipment, poker-chips, watches) gets migrated into this shape as
part of implementation - not left as three special-cased legacy functions
sitting next to a new generic system for future categories only.

### Onboarding a new category

Not a fully automated pipeline - that solves a problem the owner doesn't
have yet (they are not adding categories weekly). A repeatable, documented
process:

1. Owner (or Claude, on request) defines the new category's intent - e.g.
   "used Honda Civic, under $8,000, no salvage title."
2. One real research dispatch, same standard as every fix tonight: pull
   real comparable market data, research real category-specific red flags
   (for a car: salvage title language, odometer rollback signs, flood
   damage; for a shirt: known counterfeit tells for the specific brand),
   propose a draft `category_profiles.json` entry with cited sources, and
   backtest against real historical data if any exists for anything
   similar.
3. Draft comes back for a human look before going live - same review
   discipline this whole session has used before any threshold shipped.
4. Once approved, the category runs fully AI-free for triage, same as
   golf/poker/watches today.
5. Periodic re-review as real alert history accumulates, same iterative
   pattern as tonight's golf/poker/watches fixes (budget fix -> pretriage
   tightening -> blade-iron fix, each building on real accumulated
   evidence) - not a one-shot "done forever" profile.

### Scoring integration

`promise_score` gains category-profile-driven contributions: a title
matching a profile's known-good pattern is a positive contribution; a soft
negative signal (generic/vague listing language, no seller trust signal)
is a small negative contribution - without becoming a hard reject on its
own. Hard rejects stay reserved for near-certain-bad signals only (exactly
the blade-iron-vs-blade-putter precision already shipped tonight), each
one individually backtested against real delivered alerts before it ships,
exactly like every rule tonight. No change to `_ai_check_priority`'s
existing ordering logic or to `ai_pending`'s cross-run carry-forward -
both already work and are reused as-is.

## Data flow

1. Candidate scraped/ingested as today - unchanged.
2. Generic-layer checks run first (condition language, scam signals,
   counterfeit language) - unchanged in spirit from today's watch/poker
   checks, just no longer category-siloed code.
3. Category profile checks run next: blocked brands/subtypes are hard
   rejects (hard-fail before any AI cost, exactly like today);
   `promise_score` contributions are computed for whatever survives.
4. Surviving candidates enter the existing `_ai_check_priority` ordering
   and existing `GEMINI_CALL_LIMIT`/`ai_pending` machinery - unchanged.
5. Post-AI, `is_blocked_by_steal_quality_gate()` remains the final backstop,
   migrated to read from the category profile where it currently
   hard-codes per-category logic.

## Error handling and safety rails

- A malformed or missing `category_profiles.json` entry must fail open to
  "no category-specific rules, generic layer only" - never crash a run and
  never silently treat a missing profile as "reject everything."
- A brand-new category's profile starts conservative: prefer sending a
  borderline candidate to AI over auto-rejecting it, until enough real
  alert history exists to safely tighten - the same posture every fix
  tonight took, made an explicit, permanent rule for all future categories
  rather than something re-decided each time.
- Every hard-reject rule in a profile must ship with a citation of the real
  evidence (backtest result or sourced research) that justified it - this
  is a review requirement, not just a suggestion, enforced the same way
  every fix tonight was independently re-verified before deploying.

## Testing

- A schema/lint check for `category_profiles.json` at load time (or a
  dedicated test), catching a malformed profile before it reaches
  production rather than at runtime.
- Golf, poker, and watches keep their exact existing regression tests after
  migration - the migration must not change any currently-passing test's
  expected outcome, since those tests encode tonight's real, hard-won
  evidence.
- Every new category ships with real regression tests using real example
  listings (the same pattern used for the Top-Flite/Wilson fix tonight:
  exact IDs, titles, and prices from real listings, not synthetic
  fixtures) proving both a rejection and a preserved-for-vision case.

## Explicit open questions for implementation planning (not resolved here)

- Exact final `category_profiles.json` schema (the shape above is
  illustrative).
- Whether `is_blocked_by_steal_quality_gate()`'s per-category branches
  should be fully replaced by profile-driven logic in one pass, or migrated
  category-by-category to bound the size of any single change to
  already-live money-adjacent code.
- Real line numbers for `_ai_check_priority` and other symbols that were
  not re-confirmed while writing this spec (this repo auto-commits every
  few minutes; exact locations must be re-verified at implementation time
  against current HEAD, not assumed from this document).
