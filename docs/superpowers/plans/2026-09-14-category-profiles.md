# Category Profiles Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: none of superpowers:subagent-driven-development or superpowers:executing-plans apply here — this plan is executed by dispatching each task below as an independent Codex (`cxe`) task against the live `FB464CC/ebay-deal-alert` repo, one at a time (same-file edits are sequential, not parallel, per this repo's orchestration convention), with the orchestrating Claude session independently re-verifying (`ast.parse`, `json.load`, `pytest -q`, and a manual diff read) and deploying (fetch/rebase/push/byte-verify against `gh api`) before starting the next task. Steps use checkbox (`- [ ]`) syntax for tracking task completion, not literal manual keystrokes.

**Goal:** Generalize the hand-written golf/poker/watches pre-AI quality gates into a reusable, data-driven `category_profiles.json` architecture, with zero regression to any currently-deployed behavior, so a future new category (cars, shirts, etc.) can be onboarded without bespoke Python per category.

**Architecture:** Extract the one real cross-category signal (nonfunctional/broken condition language) into a shared, generalized regex; introduce `category_profiles.json` plus a fail-open loader; migrate golf-equipment, poker-chips, and watches into profile entries one at a time, each proven behavior-identical to today via the existing test suite; then let `_pretriage_score`'s `promise_score` read graduated, profile-driven contributions instead of only its current two hardcoded signals.

**Tech Stack:** Python 3 (stdlib `re`, `json`), the existing `unittest`-based `test_ebay_deal_alert.py` suite, no new dependencies.

**Spec:** `docs/superpowers/specs/2026-09-14-category-profiles-design.md` (commit `f6214a7`)

## Global Constraints

- Personal steal-detector, not resale/flip - per the spec's explicit non-goal, no task below may add "resale margin" or "flip profit" framing.
- No "cars" or "shirts" category profiles in this plan - architecture only. Onboarding an actual new category is a future, separate dispatch.
- Every migration task (3, 4, 5 below) must leave 100% of that category's existing tests in `test_ebay_deal_alert.py` passing **unmodified** - a migration that requires editing an existing passing test to keep it green is a behavior change, not a refactor, and must be treated as a bug in that task.
- `AI_PAID_MONTHLY_BUDGET_USD`, `GEMINI_CALL_LIMIT`, `GEMINI_BATCH_SIZE`, and every `saved_search` field in `config.json` are out of scope for every task in this plan.
- A malformed or missing `category_profiles.json` entry must fail open (no category-specific rules applied, generic layer only) - it must never crash a run or be treated as "reject everything."
- Full suite is `python3 -m pytest -q`; expect it to report `737 passed` before Task 1 starts (re-confirm the real current number when Task 1 begins - this repo auto-commits every few minutes and the count may have grown).
- Exact line numbers cited below are from HEAD as read while writing this plan and **will drift** - every task's first step is locating the named function/constant by name in the current checkout, not trusting a line number.

---

### Task 1: Extract the shared nonfunctional-condition signal

**Files:**
- Modify: `ebay_deal_alert.py` (near `WATCH_NONFUNCTIONAL_TEXT_SIGNALS`, currently ~line 176)
- Test: `test_ebay_deal_alert.py` (new test class near the existing `WatchPriceBand`/nonfunctional tests, currently ~line 1904-2881)

**Interfaces:**
- Produces: `NONFUNCTIONAL_CONDITION_TEXT_SIGNALS` (module-level compiled `re.Pattern`) - the generalized, category-agnostic version of today's `WATCH_NONFUNCTIONAL_TEXT_SIGNALS`. Later tasks (and the generic layer more broadly) import this name.
- Consumes: nothing new - this is a pure extraction from existing code.

This is the one real cross-category signal already proven correct for watches
tonight: "not running", "does not run/wind/tick", "nonfunctional", "stopped
running/working", "not keeping time", "dead movement". None of that language
is watch-specific in real life - "not running" and "parts only" describe a
broken lawnmower exactly as well as a broken watch. Extract it once, keep
`WATCH_NONFUNCTIONAL_TEXT_SIGNALS` as a backward-compatible alias so nothing
else in the file needs to change in this task.

- [ ] **Step 1: Write the failing test proving the alias is identical**

```python
class SharedNonfunctionalConditionSignal(unittest.TestCase):
    def test_watch_alias_is_the_generalized_pattern(self):
        self.assertIs(m.WATCH_NONFUNCTIONAL_TEXT_SIGNALS, m.NONFUNCTIONAL_CONDITION_TEXT_SIGNALS)

    def test_matches_generic_nonfunctional_phrases_regardless_of_item(self):
        for phrase in (
            "not running", "does not run", "won't wind", "nonfunctional",
            "stopped working", "not keeping time", "dead movement",
        ):
            with self.subTest(phrase=phrase):
                self.assertTrue(m.NONFUNCTIONAL_CONDITION_TEXT_SIGNALS.search(phrase))

    def test_does_not_match_a_positive_repaired_claim(self):
        # Real false-positive risk already documented for the watch version:
        # "recently repaired and serviced" must NOT match, since it is the
        # opposite of a nonfunctional disclosure.
        self.assertIsNone(
            m.NONFUNCTIONAL_CONDITION_TEXT_SIGNALS.search("recently repaired and serviced, runs great")
        )
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python3 -m pytest test_ebay_deal_alert.py -k SharedNonfunctionalConditionSignal -v`
Expected: FAIL - `AttributeError: module '...' has no attribute 'NONFUNCTIONAL_CONDITION_TEXT_SIGNALS'`

- [ ] **Step 3: Rename and alias in `ebay_deal_alert.py`**

Locate the existing definition (search for `WATCH_NONFUNCTIONAL_TEXT_SIGNALS = re.compile`) and rename it, adding a backward-compatible alias immediately after:

```python
NONFUNCTIONAL_CONDITION_TEXT_SIGNALS = re.compile(
    r"\bnot\s+(?:currently\s+)?running\b|"
    r"\b(?:does\s+not|doesn['\N{RIGHT SINGLE QUOTATION MARK}]?t|"
    r"won['\N{RIGHT SINGLE QUOTATION MARK}]?t|will\s+not)\s+(?:run|wind|tick)\b|"
    r"\bnon[\s-]?functional\b|\bnot\s+functional\b|"
    r"\bstopped\s+(?:running|working)\b|"
    r"\bnot\s+keeping(?:\s+accurate)?\s+time\b|"
    r"\bdead\s+(?:watch\s+)?movement\b|"
    r"\bparts\s+only\b|\bfor\s+parts\b|\buntested\b",
    re.IGNORECASE,
)
# Backward-compatible alias: every existing call site (watch_pre_ai_hard_fail_reason
# and its tests) keeps working unchanged. New code should use the generalized name.
WATCH_NONFUNCTIONAL_TEXT_SIGNALS = NONFUNCTIONAL_CONDITION_TEXT_SIGNALS
```

Note the two additions (`\bparts\s+only\b|\bfor\s+parts\b|\buntested\b`) beyond
tonight's original watch pattern - real, low-risk generic signals identified
during tonight's design discussion, safe because they are unambiguous
disclosures with no positive-context collision (unlike "as is" or bare
"repair", which were explicitly rejected tonight as too collision-prone for a
keyword match and are NOT included here).

- [ ] **Step 4: Run the new tests and the full existing watch suite**

Run: `python3 -m pytest test_ebay_deal_alert.py -k "SharedNonfunctionalConditionSignal or Watch" -v`
Expected: every test PASSES, including every pre-existing `Watch*` test class
unmodified - this proves the rename was behavior-neutral.

- [ ] **Step 5: Run the full suite**

Run: `python3 -m pytest -q`
Expected: same pass count as the global-constraints baseline, plus the 3 new
tests - zero regressions, zero modified existing tests.

- [ ] **Step 6: Commit**

```bash
git add ebay_deal_alert.py test_ebay_deal_alert.py
git commit -m "Extract NONFUNCTIONAL_CONDITION_TEXT_SIGNALS as the shared generic-layer signal"
```

---

### Task 2: `category_profiles.json` schema and fail-open loader

**Files:**
- Create: `category_profiles.json`
- Modify: `ebay_deal_alert.py` (new loader function, placed near the existing `_CONFIG = json.load(...)` config-loading code)
- Test: `test_ebay_deal_alert.py` (new test class)

**Interfaces:**
- Produces: `load_category_profiles(path="category_profiles.json")` returning a `dict[str, dict]` keyed by category id (e.g. `"golf-equipment"`), and `get_category_profile(category)` returning `{}` (never `None`, never raising) for any unknown/missing/malformed category. Tasks 3-5 consume `get_category_profile`.
- Consumes: nothing new.

```json
{
  "golf-equipment": {
    "blocked_brands": [],
    "blocked_subtypes": [],
    "price_floor_brands": {},
    "promise_score_signals": {"positive": [], "negative": []}
  }
}
```

This task ships the empty/scaffolded file (every category present with empty
lists) - Tasks 3-5 populate the real migrated content one category at a time.
Shipping the empty scaffold first, separately tested, means a schema bug is
caught before any real migrated data is riding on it.

- [ ] **Step 1: Write the failing tests**

```python
class CategoryProfileLoader(unittest.TestCase):
    def test_loads_every_known_category_key(self):
        profiles = m.load_category_profiles()
        for category in ("golf-equipment", "poker-chips", "watches"):
            self.assertIn(category, profiles)

    def test_get_category_profile_returns_empty_dict_for_unknown_category(self):
        self.assertEqual(m.get_category_profile("cars"), {})

    def test_get_category_profile_fails_open_on_missing_file(self, ):
        with mock.patch.object(m, "_CATEGORY_PROFILES_PATH", "does-not-exist.json"):
            m._reset_category_profiles_cache()
            self.assertEqual(m.get_category_profile("golf-equipment"), {})

    def test_get_category_profile_fails_open_on_malformed_json(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            fh.write("{not valid json")
            bad_path = fh.name
        try:
            with mock.patch.object(m, "_CATEGORY_PROFILES_PATH", bad_path):
                m._reset_category_profiles_cache()
                self.assertEqual(m.get_category_profile("golf-equipment"), {})
        finally:
            os.unlink(bad_path)
            m._reset_category_profiles_cache()
```

(`tempfile` and `os` are already imported at the top of `test_ebay_deal_alert.py`
for other tests in the file - reuse those imports, do not add new ones if they
already exist; check with `grep -n "^import tempfile\|^import os" test_ebay_deal_alert.py`
before adding.)

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest test_ebay_deal_alert.py -k CategoryProfileLoader -v`
Expected: FAIL - `AttributeError: module '...' has no attribute 'load_category_profiles'`

- [ ] **Step 3: Create `category_profiles.json`**

```json
{
  "golf-equipment": {
    "blocked_brands": [],
    "blocked_subtypes": [],
    "price_floor_brands": {},
    "promise_score_signals": {"positive": [], "negative": []}
  },
  "poker-chips": {
    "blocked_brands": [],
    "blocked_subtypes": [],
    "price_floor_brands": {},
    "promise_score_signals": {"positive": [], "negative": []}
  },
  "watches": {
    "blocked_brands": [],
    "blocked_subtypes": [],
    "price_floor_brands": {},
    "promise_score_signals": {"positive": [], "negative": []}
  }
}
```

- [ ] **Step 4: Add the loader to `ebay_deal_alert.py`**

Place near the existing `_CONFIG = json.load(...)` top-of-file config load:

```python
_CATEGORY_PROFILES_PATH = "category_profiles.json"
_category_profiles_cache = None


def _reset_category_profiles_cache():
    """Test-only hook so a test can point the loader at a temp file/path
    and be sure the next call re-reads it instead of returning a cached
    dict from an earlier test."""
    global _category_profiles_cache
    _category_profiles_cache = None


def load_category_profiles(path=None):
    """Load category_profiles.json. Never raises: a missing file, invalid
    JSON, or a top-level value that isn't a dict all fail open to {} so a
    run never crashes or silently rejects everything over a bad profile
    file - see the design spec's explicit safety-rail requirement."""
    global _category_profiles_cache
    target_path = path or _CATEGORY_PROFILES_PATH
    if _category_profiles_cache is not None and path is None:
        return _category_profiles_cache
    try:
        with open(target_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    if path is None:
        _category_profiles_cache = data
    return data


def get_category_profile(category):
    """Return category's profile dict, or {} for any unknown/missing
    category - callers must never need a None-check."""
    profiles = load_category_profiles()
    profile = profiles.get(category)
    return profile if isinstance(profile, dict) else {}
```

- [ ] **Step 5: Run tests, then full suite**

Run: `python3 -m pytest test_ebay_deal_alert.py -k CategoryProfileLoader -v`
Expected: PASS

Run: `python3 -m pytest -q`
Expected: no regressions. `category_profiles.json` is inert (empty lists) at
this point - no existing behavior can change from adding it.

- [ ] **Step 6: Commit**

```bash
git add category_profiles.json ebay_deal_alert.py test_ebay_deal_alert.py
git commit -m "Add category_profiles.json scaffold and fail-open loader"
```

---

### Task 3: Migrate golf-equipment into its profile (zero behavior change)

**Files:**
- Modify: `ebay_deal_alert.py` (`GOLF_BLOCKED_BRANDS`, `GOLF_BEGINNER_UNSUITABLE_IRON_SIGNAL`, `golf_blocked_brand()`, `golf_wrong_item_title_reason()`)
- Modify: `category_profiles.json` (`golf-equipment` entry)
- Test: `test_ebay_deal_alert.py` - **run the existing golf test classes unmodified as the acceptance test for this task; do not write new tests that duplicate them.**

**Interfaces:**
- Consumes: `get_category_profile("golf-equipment")` from Task 2.
- Produces: `golf_blocked_brand()` and `golf_wrong_item_title_reason()` keep their exact existing signatures and return values - only their *source of truth* for the blocked-brand list moves to the profile. Nothing downstream of these two functions changes.

Move the current `GOLF_BLOCKED_BRANDS` set's contents into
`category_profiles.json`'s `golf-equipment.blocked_brands`, and change
`golf_blocked_brand()` to read from `get_category_profile("golf-equipment")`
instead of the module-level set. This is the first real migration - keep it
strictly to the blocked-brands list in this task; the iron-subtype signal
(`GOLF_BEGINNER_UNSUITABLE_IRON_SIGNAL`) and any price-floor logic can move in
a follow-up dispatch once this narrower migration is proven safe in
production, per the spec's explicit preference to bound the size of any
single change to this already-live money-adjacent path.

- [ ] **Step 1: Locate the exact current brand list**

Run: `grep -n -A5 "^GOLF_BLOCKED_BRANDS = {" ebay_deal_alert.py`

Copy the exact current set contents - do not retype from memory, the list may
have grown since this plan was written (Task 3 in the golf-blade dispatch
earlier tonight added `"top flite"`; there may be further additions since).

- [ ] **Step 2: Write the failing test proving profile-sourced == old hardcoded set**

```python
class GolfBlockedBrandsMigration(unittest.TestCase):
    def test_profile_contains_every_previously_hardcoded_brand(self):
        # Paste the exact brand list read in Step 1 here - do not
        # reconstruct it from memory.
        previously_hardcoded = {
            "big brother", "confidence", "ram", "founders club", "precise golf",
            "tour edge", "intech", "dunlop", "northwestern", "spalding", "knight",
            "pinseeker", "alien", "macgregor", "golden bear", "top flite",
        }
        profile = m.get_category_profile("golf-equipment")
        self.assertEqual(set(profile["blocked_brands"]), previously_hardcoded)

    def test_golf_blocked_brand_still_rejects_a_known_brand(self):
        self.assertEqual(m.golf_blocked_brand("Spalding"), "spalding")

    def test_golf_blocked_brand_still_accepts_wilson(self):
        self.assertIsNone(m.golf_blocked_brand("Wilson"))
```

- [ ] **Step 2: Run to verify current failure mode**

Run: `python3 -m pytest test_ebay_deal_alert.py -k GolfBlockedBrandsMigration -v`
Expected: the first test FAILs (profile's `blocked_brands` is still `[]` from
Task 2's scaffold); the other two PASS already against the untouched hardcoded
set - confirming the starting state before migration.

- [ ] **Step 3: Populate `category_profiles.json`'s golf-equipment.blocked_brands**

Paste the exact set copied in Step 1, as a sorted JSON array, into
`category_profiles.json`'s `golf-equipment.blocked_brands`.

- [ ] **Step 4: Change `golf_blocked_brand()` to read from the profile**

Locate the function (search `def golf_blocked_brand`) and change only its
source of the brand set - keep every other line (the segment-splitting,
casefold, Top-Flite hyphen canonicalization) exactly as-is:

```python
def golf_blocked_brand(identified_brand):
    """..."""  # keep the existing docstring verbatim
    if not isinstance(identified_brand, str) or not identified_brand.strip():
        return None
    brand_text = identified_brand.casefold()
    brand_text = re.sub(r"\btop-flite\b", "top flite", brand_text)

    blocked_brands = set(get_category_profile("golf-equipment").get("blocked_brands", []))
    segments = re.split(r"[,/():]|\s+[\N{EN DASH}\N{EM DASH}-]\s+", brand_text)
    for segment in segments:
        segment = segment.strip()
        for blocked in blocked_brands:
            if segment.startswith(blocked):
                return blocked
    return None
```

(Re-read the real current function body with `sed -n '/^def golf_blocked_brand/,/^def /p' ebay_deal_alert.py`
before editing - the exact segment-matching logic must be preserved verbatim;
only the `blocked_brands`/`GOLF_BLOCKED_BRANDS` line changes.)

- [ ] **Step 5: Run the new tests, then every existing golf test**

Run: `python3 -m pytest test_ebay_deal_alert.py -k "GolfBlockedBrandsMigration or Golf" -v`
Expected: every test PASSES, including every pre-existing `Golf*`/golf-named
test unmodified.

- [ ] **Step 6: Run the full suite**

Run: `python3 -m pytest -q`
Expected: no regressions, only the 3 new tests added to the count.

- [ ] **Step 7: Commit**

```bash
git add ebay_deal_alert.py category_profiles.json test_ebay_deal_alert.py
git commit -m "Migrate golf-equipment blocked brands into category_profiles.json"
```

---

### Task 4: Migrate poker-chips into its profile (zero behavior change)

**Files:**
- Modify: `ebay_deal_alert.py` (`POKER_INELIGIBLE_CONSTRUCTION_SIGNALS`, `POKER_CHIPS_MIN_SET_SIZE` reference stays a numeric constant - not moved, it's a value already sourced from `config.json` per tonight's earlier work, not something this migration touches)
- Modify: `category_profiles.json` (`poker-chips` entry)
- Test: existing poker test classes, run unmodified as acceptance.

**Interfaces:**
- Consumes: `get_category_profile("poker-chips")`.
- Produces: `poker_pre_ai_hard_fail_reason()` keeps its exact signature; only the construction-signal source moves to the profile as a list of pattern fragments the function compiles once at call time (mirroring how blocked_brands works in Task 3).

Only `POKER_INELIGIBLE_CONSTRUCTION_SIGNALS`'s word list (plastic, ABS, clay
composite, metal-core, iron-core, metal-slugged) moves in this task - the chip
*identity* signals (`POKER_CHIP_IDENTITY_SIGNALS`, `POKER_CHIP_MAKER_SIGNALS`)
and the count/sample-storage checks stay as module-level regexes for now,
same bounded-migration reasoning as Task 3.

- [ ] **Step 1: Locate the exact current construction-signal word list**

Run: `grep -n -A6 "^POKER_INELIGIBLE_CONSTRUCTION_SIGNALS = re.compile" ebay_deal_alert.py`

- [ ] **Step 2: Write the failing test**

```python
class PokerConstructionSignalMigration(unittest.TestCase):
    def test_profile_lists_every_previously_hardcoded_construction_term(self):
        profile = m.get_category_profile("poker-chips")
        # Paste the exact terms read in Step 1 - do not reconstruct from memory.
        previously_hardcoded = {
            "plastic", "abs", "clay composite", "metal-core", "iron-core", "metal-slugged",
        }
        self.assertEqual(set(profile["blocked_subtypes"]), previously_hardcoded)

    def test_poker_pre_ai_hard_fail_still_rejects_plastic_chips(self):
        self.assertIsNotNone(
            m.poker_pre_ai_hard_fail_reason("200 plastic poker chips set")
        )
```

- [ ] **Step 3: Run to verify failure, then populate `category_profiles.json`'s poker-chips.blocked_subtypes** with the exact list from Step 1.

- [ ] **Step 4: Change the construction check to read from the profile**

Locate `POKER_INELIGIBLE_CONSTRUCTION_SIGNALS`'s use inside
`poker_pre_ai_hard_fail_reason()` and replace the module-level compiled
pattern's word source with a pattern built from
`get_category_profile("poker-chips")["blocked_subtypes"]` at call time,
preserving the exact surrounding regex structure (the `\b...\s+(?:poker\s+)?chips?\b`
wrapper) - re-read the real current pattern with
`sed -n '/^POKER_INELIGIBLE_CONSTRUCTION_SIGNALS/,/^)/p' ebay_deal_alert.py`
before writing this, since the exact wrapper regex must be preserved
character-for-character around the now-profile-sourced word alternation.

- [ ] **Step 5: Run new tests, then every existing poker test, then full suite**

Run: `python3 -m pytest test_ebay_deal_alert.py -k "PokerConstructionSignalMigration or Poker" -v`
Run: `python3 -m pytest -q`
Expected: zero regressions both times.

- [ ] **Step 6: Commit**

```bash
git add ebay_deal_alert.py category_profiles.json test_ebay_deal_alert.py
git commit -m "Migrate poker-chips construction blocklist into category_profiles.json"
```

---

### Task 5: Migrate watches into its profile (zero behavior change)

**Files:**
- Modify: `ebay_deal_alert.py` (`watch_pre_ai_hard_fail_reason()`'s price-floor math stays in Python - it's a real formula, not a list; only per-brand price-floor *numbers*, if any exist beyond the generic `watch_price_band()` table, move to the profile)
- Modify: `category_profiles.json` (`watches` entry)
- Test: existing watch test classes, run unmodified as acceptance.

**Interfaces:**
- Consumes: `get_category_profile("watches")`, `NONFUNCTIONAL_CONDITION_TEXT_SIGNALS` from Task 1 (already used via the `WATCH_NONFUNCTIONAL_TEXT_SIGNALS` alias - no call-site change needed here, Task 1 already covers it).
- Produces: no new public interface - this task is intentionally the lightest of the three migrations, since watches' real category-specific logic (the price-band margin math) is a formula, not a data list, and stays exactly where it is.

Before writing any code: run
`sed -n '/^def watch_pre_ai_hard_fail_reason/,/^def /p' ebay_deal_alert.py` and
confirm whether anything in it beyond the already-migrated (via Task 1)
nonfunctional-text check is actually a *list* worth moving to the profile, or
whether it's all real formula/math (per the spec's own note, the margin check
is "mathematically unable to alert" logic, not a lookup table). If the honest
answer at execution time is "there is nothing left to migrate here beyond
Task 1's alias," this task's deliverable becomes: add an explicit
`"watches"` entry comment/marker in `category_profiles.json` noting the
category deliberately has no additional migrated fields yet, and a test
proving `watch_pre_ai_hard_fail_reason()`'s behavior is completely unchanged
end-to-end. Do not invent a list to move just to make this task look bigger -
that would violate the plan's own no-placeholder, no-busywork spirit.

- [ ] **Step 1: Write the acceptance test**

```python
class WatchCategoryProfilePresence(unittest.TestCase):
    def test_watches_profile_entry_exists(self):
        self.assertIn("watches", m.load_category_profiles())

    def test_watch_pre_ai_hard_fail_unchanged_for_nonfunctional_listing(self):
        listing = {"title": "Seiko 5 automatic, not running, for parts"}
        self.assertIsNotNone(m.watch_pre_ai_hard_fail_reason(listing, landed_price=20))

    def test_watch_pre_ai_hard_fail_unchanged_for_the_real_oris_reproduction(self):
        # Same real listing used in tonight's scraped-eBay condition-gap fix -
        # proves this migration didn't reopen that fixed bug.
        listing = {
            "title": "Oris Gold Filled 10 Microns 17 Jewels Mesh Band Square Face Watch W/ Box 1960s C",
            "ebay_condition_id": "7000",
        }
        self.assertIsNotNone(m.watch_pre_ai_hard_fail_reason(listing, landed_price=42.40))
```

- [ ] **Step 2: Run to verify current pass/fail state, then make the minimal real change** (either populate a genuinely-migrated field per the investigation in the task description above, or add the explicit "nothing to migrate yet" marker - determined by what Step 1's investigation actually finds, not decided in advance here).

- [ ] **Step 3: Run new tests, every existing watch test, and the full suite**

Run: `python3 -m pytest test_ebay_deal_alert.py -k "WatchCategoryProfilePresence or Watch" -v`
Run: `python3 -m pytest -q`
Expected: zero regressions both times.

- [ ] **Step 4: Commit**

```bash
git add ebay_deal_alert.py category_profiles.json test_ebay_deal_alert.py
git commit -m "Add watches category_profiles.json entry"
```

---

### Task 6: Profile-driven `promise_score` contributions + final regression pass

**Files:**
- Modify: `ebay_deal_alert.py` (`_pretriage_score()`, currently ~line 8270)
- Modify: `category_profiles.json` (populate `promise_score_signals` for all three migrated categories with real, evidence-justified entries - or leave empty with a documented reason if no category currently has real evidence for one, matching tonight's repeated "don't invent a number" discipline)
- Test: `test_ebay_deal_alert.py` (new test class)

**Interfaces:**
- Consumes: `get_category_profile(category)` from Task 2.
- Produces: `_pretriage_score()` keeps its exact `(repeat_failure_tier, promise_score)` return shape - `_ai_check_priority`'s sort key (unchanged, not touched by this task) keeps working without modification.

Add a third, small `promise_score` contribution sourced from
`get_category_profile(category)["promise_score_signals"]`: a `positive` list
of `{"pattern": "...", "score": <float>}` entries checked against the
candidate's title (adds `score` if matched) and a `negative` list of the same
shape (subtracts `score` if matched). This is deliberately a *small* addition
to `promise_score` - the two existing signals (sold-comp backing, seller
feedback) are already carefully weighted (+2.0, +1.0) and proven not to
dominate the sort ahead of `pending_minutes` aging; new profile-driven
contributions must use comparably small magnitudes (recommend starting at
+/-0.5 per matched signal) so they act as a tiebreak refinement, not a
reordering that could starve the anti-starvation aging tonight's fix already
protects.

**Do not populate any category's `promise_score_signals` with an invented
number.** If, at execution time, there isn't a real backtested reason to
weight a specific pattern, leave both lists empty for that category and say
so in the commit message - matching every other fix tonight's explicit
refusal to ship an unevidenced threshold.

- [ ] **Step 1: Write the failing test**

```python
class ProfileDrivenPromiseScore(unittest.TestCase):
    def test_positive_signal_adds_its_configured_score(self):
        profile_patch = {
            "promise_score_signals": {
                "positive": [{"pattern": "mint condition", "score": 0.5}],
                "negative": [],
            }
        }
        candidate = {
            "listing": {"title": "Widget in mint condition"},
            "result": {},
            "category": "test-category",
        }
        with mock.patch.object(m, "get_category_profile", return_value=profile_patch):
            _, promise_score = m._pretriage_score(candidate, ai_no_price_attempts=0)
        self.assertEqual(promise_score, 0.5)

    def test_negative_signal_subtracts_its_configured_score(self):
        profile_patch = {
            "promise_score_signals": {
                "positive": [],
                "negative": [{"pattern": "assorted", "score": 0.5}],
            }
        }
        candidate = {
            "listing": {"title": "Assorted widget lot"},
            "result": {},
            "category": "test-category",
        }
        with mock.patch.object(m, "get_category_profile", return_value=profile_patch):
            _, promise_score = m._pretriage_score(candidate, ai_no_price_attempts=0)
        self.assertEqual(promise_score, -0.5)

    def test_empty_profile_signals_leave_promise_score_unchanged(self):
        # Zero-signal category must behave identically to today - proves this
        # task cannot regress a category with no evidenced signals yet.
        candidate = {"listing": {"title": "anything"}, "result": {}, "category": "watches"}
        _, promise_score = m._pretriage_score(candidate, ai_no_price_attempts=0)
        self.assertEqual(promise_score, 0.0)
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest test_ebay_deal_alert.py -k ProfileDrivenPromiseScore -v`
Expected: FAIL - `_pretriage_score` doesn't yet read profile signals.

- [ ] **Step 3: Extend `_pretriage_score()`**

Re-read the real current function in full with
`sed -n '/^def _pretriage_score/,/^def /p' ebay_deal_alert.py` before editing
(this plan's earlier quoted version may have drifted). Add immediately before
the existing `return repeat_failure_tier, promise_score` line:

```python
    category = candidate.get("category")
    title = (listing.get("title") or "")
    profile_signals = get_category_profile(category).get("promise_score_signals", {})
    for signal in profile_signals.get("positive", []):
        if re.search(signal["pattern"], title, re.IGNORECASE):
            promise_score += float(signal["score"])
    for signal in profile_signals.get("negative", []):
        if re.search(signal["pattern"], title, re.IGNORECASE):
            promise_score -= float(signal["score"])
```

- [ ] **Step 4: Run the new tests, then the full pretriage/priority-related test classes**

Run: `python3 -m pytest test_ebay_deal_alert.py -k "ProfileDrivenPromiseScore or Pretriage or AiCheckPriority" -v`
Expected: all PASS.

- [ ] **Step 5: Run the complete full suite**

Run: `python3 -m pytest -q`
Expected: zero regressions against the running baseline (baseline count from
Task 1's step 5, plus every new test added across Tasks 1-6).

- [ ] **Step 6: Final independent audit pass (this step is the orchestrator's, not part of the Codex dispatch for this task)**

After this task's Codex dispatch reports done: re-clone fresh, run
`python3 -c "import ast; ast.parse(open('ebay_deal_alert.py', encoding='utf-8').read())"`,
`python3 -c "import json; json.load(open('config.json')); json.load(open('category_profiles.json'))"`,
`python3 -m pytest -q`, and read the full six-task cumulative diff against the
pre-Task-1 baseline before pushing - confirming end to end that no golf/poker/
watches behavior actually changed (the entire point of Tasks 3-5 being
"zero behavior change" migrations), and that `AI_PAID_MONTHLY_BUDGET_USD`,
`GEMINI_CALL_LIMIT`, `GEMINI_BATCH_SIZE`, and every `saved_search` field are
byte-identical to before Task 1.

- [ ] **Step 7: Commit**

```bash
git add ebay_deal_alert.py category_profiles.json test_ebay_deal_alert.py
git commit -m "Wire category-profile signals into _pretriage_score's promise_score"
```

---

## Self-review notes (completed while writing this plan)

- **Spec coverage:** generic-layer extraction (Task 1), `category_profiles.json` schema + fail-open loader (Task 2), golf/poker/watches migration (Tasks 3-5), `promise_score` integration (Task 6) - every section of the spec's Architecture and Error Handling sections maps to a task. The spec's "onboarding a new category" process is deliberately NOT a task here - it's a process using this architecture, exercised the next time an actual new category is added, not part of building the architecture itself.
- **Placeholder scan:** no TBD/TODO left in any task; the one place a decision is deferred (Task 5's "is there anything to migrate beyond Task 1's alias") is deferred because the honest answer depends on re-reading real current code at execution time, not because it wasn't thought through - and it names exactly what to do in either real outcome.
- **Type consistency:** `get_category_profile(category) -> dict` (never `None`) is used identically in Tasks 3, 4, 5, and 6. `_pretriage_score(candidate, ai_no_price_attempts) -> (int, float)` keeps its exact existing shape through Task 6, so `_ai_check_priority`'s consumption of it needs no change anywhere in this plan.
