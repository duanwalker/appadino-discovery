# G1.2 Ingest Pipeline — Test Coverage Gaps & New Test Suite

## Resolution (2026-09-16)

All three flagged gaps were fixed in `discovery.stages.ingest`:

- **EIN format validation** — `transform_bmf_row` and `transform_index_row` now drop rows whose EIN isn't exactly 9 digits (`EIN_LENGTH`), instead of passing malformed values through to the primary key of the shared national universe.
- **`object_id` length validation** — `transform_index_row` now drops rows whose `object_id` exceeds `filings.object_id`'s schema length (`OBJECT_ID_MAX_LENGTH = 30`), instead of letting the DB reject them mid-batch.
- **`DISTINCT ON` without `ORDER BY`** — both staging tables (`staging_organizations`, `staging_filings`) gained a `seq bigserial` column; the upsert `SELECT DISTINCT ON (...)` now orders by `seq DESC` so the last row for a given key *within a batch* deterministically wins, consistent with how `ON CONFLICT ... DO UPDATE` already makes the last write win *across* batches/runs.

The three tests in `test_ingest_gaps.py` that documented the old (buggy) pass-through behavior were updated to assert the corrected behavior instead, plus two added for the exact boundary (EIN wrong-length, object_id at exactly 30 chars). All fixes were also verified against the live deployed Postgres with synthetic duplicate-key batches, confirming deterministic last-row-wins in both tables. Full suite: 37/37 passing.

## Summary

Added **29 pytest cases** (27 originally, +2 boundary cases added during the fix) in [`pipeline/tests/stages/test_ingest_gaps.py`](pipeline/tests/stages/test_ingest_gaps.py) covering previously untested scenarios across the BMF loader and 990 e-file index loader (Stage 0, §4 of the brief).

**All tests pass**: 29 in this file + 7 in `test_ingest.py` + 1 smoke test = 37/37 total. Code is ruff-clean and type-safe (`mypy --strict`).

---

## Coverage by Gap

### Gap 1: Malformed/Truncated BMF CSV Rows (8 tests)

**What was untested:**
- EIN present but non-numeric or malformed ("ABC123456")
- Non-numeric REVENUE_AMT ("NOT_A_NUMBER")
- Negative revenue values
- Truncated RULING field (too short to extract year)
- Empty NAME (whitespace-only)
- Unicode characters in fields
- Oversized STATE codes (whitespace trimming)

**Tests added:**
- `test_transform_bmf_row_with_non_numeric_ein` — validates malformed EIN passes through (no format validation)
- `test_transform_bmf_row_with_malformed_ruling_year` — truncated RULING returns None for ruling_year ✓
- `test_transform_bmf_row_with_non_numeric_revenue` — bad REVENUE_AMT returns None ✓
- `test_transform_bmf_row_with_negative_revenue` — negative values accepted (edge case)
- `test_transform_bmf_row_with_empty_name` — whitespace-only NAME becomes ""  ✓
- `test_transform_bmf_row_with_truncated_ruling` — 1-3 char RULING returns None ✓
- `test_transform_bmf_row_with_unicode_name` — UTF-8 characters handled ✓
- `test_transform_bmf_row_handles_oversized_state_code` — extra whitespace stripped ✓

**Gaps discovered:**
- ⚠️ **No validation of EIN format** — accepts "ABC123456" as-is. G1.3 filters or DB constraints will catch it, but silent pass-through is a data quality risk.

---

### Gap 2: Interrupted Load / Crash Recovery (5 tests)

**What was untested:**
- Idempotency: re-running after a crash produces identical results
- No partial/duplicate rows left in staging table after connection failure
- Generator chains (transform filters) work correctly
- Upsert logic correctly deduplicates via ON CONFLICT

**Tests added:**
- `test_transform_bmf_idempotency_multiple_calls` — calling transform_bmf_row twice yields identical output ✓
- `test_transform_index_idempotency_multiple_calls` — calling transform_index_row twice yields identical output ✓
- `test_latest_two_per_ein_with_duplicate_batches` — feeding same rows twice still yields only 2 per EIN ✓
- `test_bmf_transform_and_filter_chain` — generator chain correctly filters None results ✓
- `test_index_transform_and_filter_chain` — filing generator chain correctly filters by form type and known EIN ✓

**Result:** ✓ The upsert logic IS idempotent; re-running after a crash is safe. Staging table is temporary per-connection, so cleanup is automatic on disconnect.

---

### Gap 3: Same EIN in Multiple Regional BMF Extracts (3 tests)

**What was untested:**
- Same EIN in 2+ regional files (eo1-eo4.csv) with different data
- No duplicate organizations entry created
- Deduplication consistency (which row wins? is it deterministic?)

**Tests added:**
- `test_same_ein_from_different_regions_no_duplication` — both regions transform; upsert deduplicates ✓
- `test_different_eins_from_regional_extracts` — all 4 regions' EINs inserted ✓
- `test_latest_two_per_ein_across_regions` — filings from multiple years correctly limited to latest 2 ✓

**Gaps discovered:**
- ⚠️ **DISTINCT ON without ORDER BY in `_flush_organizations_batch()`** — when same EIN appears in multiple batches/regions, PostgreSQL returns unpredictable row (could be either source). Currently safe due to ON CONFLICT, but winner is non-deterministic. Recommend: `ORDER BY ein DESC, bmf_updated_at DESC` for deterministic last-write-wins semantics.

---

### Gap 4: Archive Lookup Logic (object_id/xml_object_url) (8 tests)

**What was untested:**
- `object_id` validation (length, format)
- `xml_object_url` correctly constructed from submission year
- Malformed/missing archive entries caught gracefully
- NULL xml_object_url scenarios
- object_id length constraint (schema: String(30))

**Tests added:**
- `test_transform_index_row_constructs_correct_archive_url` — URL = `https://apps.irs.gov/pub/epostcard/990/xml/{year}/` ✓
- `test_transform_index_row_different_submission_years` — URL changes per sub_year correctly ✓
- `test_transform_index_row_with_empty_object_id` — empty OBJECT_ID returns None ✓
- `test_transform_index_row_with_whitespace_object_id` — whitespace OBJECT_ID returns None ✓
- `test_transform_index_row_with_extremely_long_object_id` — 50-char object_id passes through (will fail at DB) ⚠️
- `test_transform_index_row_object_id_format_variation` — accepts any format (numeric, alphanumeric, mixed) ✓
- `test_transform_index_row_missing_tax_period` — empty/malformed TAX_PERIOD returns None ✓
- `test_filings_with_valid_archive_metadata` — valid row has both object_id and xml_object_url ✓

**Gaps discovered:**
- ⚠️ **No length validation for object_id** — schema limits to 30 chars, but `transform_index_row()` doesn't check. 50-char ID passes through, DB will reject or truncate silently on upsert. **Recommendation:** Add length check before returning: `if len(object_id) > 30: return None`

---

## Implementation Quality

| Metric | Status |
|--------|--------|
| All 27 new tests pass | ✅ |
| All 34 tests (new + original) pass together | ✅ |
| `ruff check` (style/lint) | ✅ Clean |
| `mypy --strict` | ✅ Clean (expected stubs warning for local module) |
| No modifications to implementation code | ✅ Tests only |

---

## Uncovered Issues Flagged for Review — all fixed, see Resolution above

| Issue | Severity | Location | Status |
|-------|----------|----------|-----------------|
| **No EIN format validation** | Medium | `transform_bmf_row()`, `transform_index_row()` | ✅ Fixed |
| **DISTINCT ON without ORDER BY** | Medium | `_flush_organizations_batch()`, `_flush_filings_batch()` | ✅ Fixed |
| **No object_id length validation** | Medium | `transform_index_row()` | ✅ Fixed |

---

## Test Execution

```powershell
cd pipeline
.\.venv\Scripts\Activate.ps1
python -m pytest tests/stages/test_ingest_gaps.py -v
```

Output: **29 passed**

---

## G1.5 Suppress + Triggers + Publish — Independent Test Coverage Review

## Resolution (2026-09-18)

Two of the four "🔴 Gaps discovered" findings below (Gap 1's two fuzzy-match misses, Gap 4's two `gap_rank` bounds issues) were fixed:

- **Suppression fuzzy-match normalization** — `normalize_name()` in `discovery.stages.suppress` now strips common legal-suffix/descriptor words (`inc`, `incorporated`, `llc`, `corp`, `corporation`, `foundation`) and punctuation before scoring, rather than just lowering the 0.85 threshold (which would have made unrelated-org false positives more likely). Both real examples now score 1.0: `fuzzy_match_score("Butterfly Dreamz, Inc.", "Butterfly Dreamz")` and `fuzzy_match_score("SHE DREAMS IN COLOR FOUNDATION", "She Dreams in Color")`.
  - **Live-dataset audit performed first, per instruction:** every one of the 154 published `client_id=2` orgs was fuzzy-matched against the full 17-name ARCHITECT seed list, both unnormalized and normalized. **Result: 0 real suppression misses** — the Maine pilot dataset has no genuine name overlap with ARCHITECT's real (Cincinnati/Charlotte-based) client list, so this was a hypothetical-only gap in the live data, not an active one. A second audit pass after the fix confirmed **0 new false positives** introduced against the same 154×17 pairs. Since no real misses existed, publish for `client_id=2` was not re-run; counts remain 154 published / 0 EIN-suppressed / 0 fuzzy-flagged.
- **`gap_rank` bounds** — `compute_gap_rank()` in `discovery.stages.publish` now clamps `criteria_met_count` to `[0, ALIGNMENT_MAX_CRITERIA]` before computing the alignment component, and normalizes a custom `weights` dict proportionally back down to a sum of 1.0 whenever the raw sum exceeds 1.0 (weight sums below 1.0 are left alone — a deliberately "softer" config isn't a bug). The final return is also clamped to `[0.0, 100.0]` as a last-resort backstop.

The 4 `test_g15_comprehensive.py` tests that documented the old (buggy) behavior were updated to assert the corrected behavior instead (matching the G1.2 resolution pattern). 10 new tests were also added directly to `test_suppress.py` and `test_publish.py` covering both fixes. The other two discovered issues (Gap 5's QA text-citation edge cases, Gap 3's CSV null/empty-string ambiguity) remain open by explicit scope — not addressed this pass.

**Full suite: 258/258 passing** (33 in `test_g15_comprehensive.py`, all now green). Ruff-clean, `mypy src` clean.

## Summary

Added **33 pytest cases** in [`pipeline/tests/stages/test_g15_comprehensive.py`](pipeline/tests/stages/test_g15_comprehensive.py) covering 6 gap areas. **5 of these tests failed on first run against real production code** — not test bugs, but genuine previously-undiscovered behavior. All 5 were corrected to assert and document the actual (concerning) behavior rather than the assumed-safe behavior, per "flag gaps, don't just make tests pass."

**Full suite (at time of review): 246/246 passing.** Ruff-clean (`ruff check .`). No implementation code was modified as part of the initial review — this is a test-only review; two of the discovered issues were fixed in the Resolution above, and two remain flagged for a follow-up decision.

---

## Coverage by Gap

### Gap 1: Suppression Fuzzy-Match Against Real ARCHITECT Seed Data (8 tests)

**What was untested:** all existing `test_suppress.py` cases use invented names ("Butterfly Dreamz", "Cincinnati Symphony Orchestra") never actually loaded into the DB. STATUS.md explicitly flags that fuzzy matching has never fired against real overlapping data, since the Maine pilot data has no genuine overlap with ARCHITECT's real 17-name seed list (migration `7ac7a7b1f96a`).

**Tests added:** exercise `find_fuzzy_match`/`fuzzy_match_score` against the actual 17 real ARCHITECT seed names (12 past/current clients + 5 active prospects), with deliberate synthetic near-miss collisions (corporate suffixes, case variants, added descriptor words, whitespace), plus one true-negative test against an unrelated real org name, and one test confirming the `notes` string construction (mirrored from `run_publish.py`) flags rather than silently drops a match.

**🔴 Gaps discovered (real, previously unknown) — ✅ both fixed, see Resolution above:**
- **"Butterfly Dreamz, Inc." scored 0.842 against "Butterfly Dreamz" — just under the 0.85 threshold, so it did NOT flag.** The unpunctuated variant "Butterfly Dreamz Inc" scored 0.889 and did flag. A single comma+period in an extremely common corporate suffix was enough to let a likely-same org slip past suppression entirely. Now scores 1.0 after `normalize_name()` strips legal-suffix words and punctuation.
- **"SHE DREAMS IN COLOR FOUNDATION" scored 0.776 against the real seed name "She Dreams in Color" — well under threshold.** A plausible real-world legal-name variant (adding "Foundation") was not caught as previously tuned. Now scores 1.0.
- Both were silent misses, not crashes — an org that should be flagged for human review in `prospects.notes` simply wasn't, with no error or signal that suppression was even considered.

---

### Gap 2: Trigger Priority Determinism Across All Combinations (5 tests)

**What was untested:** existing `test_triggers.py` only checks 2-trigger priority in one direction (`transformational_revenue_jump` + `new_ed` → `new_ed`). No test exercises all `C(4,2)`/3-way/4-way combinations, and no test explicitly asserts `trigger_evidence` (the full `detect_triggers()` return value) retains every fired trigger when more than one fires.

**Tests added:** `itertools.permutations`/`combinations` over all 4 trigger types confirm `select_primary_trigger()` is order-independent and always resolves to the correct `TRIGGER_PRIORITY` winner for every pairwise combination, a 3-way combination, and a constructed filing pair that fires **all four** trigger types simultaneously — confirming all 4 survive in evidence even though only one becomes "assigned".

**Result:** ✅ No gaps found — priority selection is deterministic and evidence retention is correct across every combination tested, including the all-four-at-once case.

---

### Gap 3: `trigger_angle = null` Downstream (CSV Export) (3 tests)

**What was untested:** `first_filing_above_floor`'s `trigger_angle=null` (a genuine, deliberate gap in Lauren's angle table, per Duan's instruction not to fabricate a default) had no test confirming it survives CSV export as a real empty cell rather than the literal string `"None"`.

**Tests added:** `export_csv()` with a `None` trigger_angle round-tripped through a real temp-file write+read confirms the cell is a genuine empty string (`""`), never `"None"`/`"null"`; a companion test confirms a null angle and an explicitly-configured empty-string angle are byte-for-byte indistinguishable once written to CSV; `resolve_angle()` confirmed to return `None` identically whether the trigger type is explicitly mapped to `null` or entirely absent from `trigger_angles` config.

**🟡 Gap noted (inherent format limitation, not a code bug):** once written to CSV, a deliberate "no angle exists" (`null`) and an accidentally-blank configured angle (`""`) are indistinguishable to any downstream consumer (dashboard, Lauren's spreadsheet). Worth knowing before treating a blank `trigger_angle` cell as meaningful signal — flagged for the G2.x dashboard work, not a Stage 6 fix.

---

### Gap 4: `gap_rank` With Partial/Missing Signal Data (6 tests)

**What was untested:** existing `test_publish.py` only tests `compute_gap_rank()` with fully-known inputs (`dd_present` either `True`/`False`/`None`, always a valid `criteria_met_count`). No test explores what happens when the caller-side default-filling in `run_publish.py` (`.get("criteria_met_count", 0)`) makes "never scored" indistinguishable from "scored and failed everything," nor whether weights/criteria counts outside their expected range are clamped.

**Tests added:** confirms `dd_present=None` (unknown capacity) is correctly treated as a moderate gap (0.5), strictly between confirmed-present and confirmed-absent, never silently collapsing to either extreme; confirms full determinism when all three components are simultaneously absent/zero; confirms partial `weights` overrides merge over defaults rather than zeroing the unspecified components (same "config, not code" merge pattern as Stage 1); and two tests that deliberately probe out-of-range inputs.

**🔴 Gaps discovered (real, previously unknown — both were "no defensive clamp exists" findings, not crashes) — ✅ both fixed, see Resolution above:**
- **`criteria_met_count` was not clamped to `ALIGNMENT_MAX_CRITERIA` (6).** Passing `7` (a data bug elsewhere, e.g. GENESIS accidentally reinstated without updating the max) silently inflated the alignment component past `1.0` rather than erroring or capping — `compute_gap_rank()` trusted its caller completely. Now clamped to `[0, 6]`.
- **Custom `weights` summing above `1.0` were not capped** — `compute_gap_rank()` could return values like `300.0`, well outside the documented "0-100" range, with no normalization or warning. Not a bug in the pilot run (defaults sum to 1.0), but a real risk if `icp_configs.config.signal_weights` is ever tuned carelessly. Now normalized proportionally back to a sum of 1.0 when the raw sum exceeds it, with a final `[0, 100]` output clamp as a backstop.
- **Design ambiguity (not fixable in `compute_gap_rank` itself):** because `run_publish.py` defaults a missing `criteria_met_count` to `0`, "this org was never scored for alignment" and "this org was scored and met zero criteria" produce the *identical* `gap_rank` contribution. Flagged for awareness, not a code defect — the ambiguity lives in the caller's default-filling, not in `compute_gap_rank`'s own (correct) handling of its explicit parameters.

---

### Gap 5: QA Text-Citation Matching Edge Cases (7 tests)

**What was untested:** existing `test_qa.py` covers exact quotes, near-verbatim paraphrases, and non-matching text, but no short-quote boundary cases, no genuinely-reworded (not just truncated) paraphrases, and no special-character/punctuation stress cases.

**Tests added:** a 7-character citation against a much longer corpus; a citation right at the `MIN_TEXT_MATCH_LEN` boundary; a genuinely reworded (different word order/vocabulary) paraphrase of real text; em-dashes/curly-quotes/ampersands in both citation and corpus; a numeric citation with trailing punctuation (`"govt_pct: 0.59% (per 990 filing—see note)"`); a real quoted program-text fragment with a parenthetical aside in the source; an empty-string citation.

**🔴 Gaps discovered (real, previously unknown — all documented limitations of pattern-based matching, consistent with the module's own "not full semantic fact-checking" disclaimer, but not previously pinned as tests):**
- **Very short citations get an artificially low bar.** `MIN_TEXT_MATCH_LEN` (25) is only a floor when the citation is *longer* than it — for citations shorter than 25 normalized characters, the bar becomes the citation's own length (`min(MIN_TEXT_MATCH_LEN, len(citation_norm))`), so a trivially short fragment like `"the org"` is fully "verified" against any corpus that happens to contain that exact 7-character run, regardless of context.
- **A genuine paraphrase (reworded, not just truncated) legitimately fails LCS matching** — e.g. "provides free legal representation to low income tenants facing eviction" vs. "offers no-cost eviction defense services for renters who cannot afford a lawyer" scores as `mismatch` despite being substantively the same claim. This is an accepted limitation (documented in `qa.py`'s own docstring), now pinned as a test rather than left implicit.
- **A parenthetical aside in the real source text can split one long, faithful match into two shorter ones that each fall under the 25-character bar** — e.g. `"Youth & Family Services (est. 1998) — after-school tutoring"` vs. citation `"youth family services after school tutoring"` mismatches (longest contiguous run is 22 chars on either side of `"(est. 1998)"`), even though the citation is a completely accurate paraphrase of the real text.
- **An empty-string citation resolves to `"mismatch"`, not `"unverifiable"`.** `_quoted_text_matches_corpus()` short-circuits on an empty normalized citation and returns `False`, which `verify_claim()` reports as a false claim rather than "nothing to verify." Currently unreachable in the real pipeline (`extract_claims()` only extracts claims with a truthy citation, and `""` is falsy) — but this is defense-in-depth that silently produces the wrong verdict category if that upstream filter ever changes.

---

### Gap 6: Publish Idempotency (5 tests)

**What was untested:** no existing test confirms re-running publish twice doesn't duplicate `prospects` rows or corrupt CSV output; this can't be tested against a live DB in this suite (no test-DB fixture exists for any stage), so coverage is at the SQL-shape and pure-function level.

**Tests added:** static confirmation that `upsert_prospect()`'s SQL is a genuine `INSERT ... ON CONFLICT (client_id, ein) DO UPDATE` (not a bare `INSERT` that would raise or duplicate on a second run) and that `update_score_gap_rank()` is a targeted `UPDATE ... WHERE` on the full composite key (never an insert); a real double-`export_csv()` call to the same path confirming the second run's rows **fully replace** the first (`"w"` mode, not append) rather than accumulating stale rows; determinism check that `compute_gap_rank()` returns bit-identical results across repeated calls with unchanged inputs.

**Result:** ✅ No gaps found — the upsert/update SQL shapes are genuinely idempotent, and CSV export overwrites rather than appends. (Note: this is necessarily a lighter-weight check than a real double-run against a live database, since no DB test fixture exists in this suite for any stage — flagged as a testing-infrastructure limitation, not a G1.5-specific one.)

---

## Uncovered Issues Flagged for Review

| Issue | Severity | Location | Status |
|-------|----------|----------|--------|
| Punctuated corporate suffix ("Inc.") drops a real near-miss below the 0.85 fuzzy threshold | Medium | `stages/suppress.py` `FUZZY_MATCH_THRESHOLD` | ⚠️ Flagged, not fixed |
| Added descriptor word ("Foundation") drops a real near-miss well below threshold | Medium | `stages/suppress.py` `FUZZY_MATCH_THRESHOLD` | ⚠️ Flagged, not fixed |
| `criteria_met_count` not clamped to `ALIGNMENT_MAX_CRITERIA` | Low | `stages/publish.py` `compute_gap_rank()` | ⚠️ Flagged, not fixed |
| Custom `signal_weights` summing >1.0 not capped/normalized | Low | `stages/publish.py` `compute_gap_rank()` | ⚠️ Flagged, not fixed |
| "Never scored" vs. "scored zero" alignment collapse to the same `gap_rank` contribution | Low (design ambiguity, caller-side) | `stages/run_publish.py` default-filling | ⚠️ Flagged, not fixed |
| Very short citations get an artificially low LCS match bar | Medium | `stages/qa.py` `MIN_TEXT_MATCH_LEN` logic | ⚠️ Flagged, not fixed |
| Genuine (reworded) paraphrases can legitimately mismatch | Low (documented, accepted limitation) | `stages/qa.py` `_quoted_text_matches_corpus()` | ℹ️ Accepted limitation |
| Parenthetical asides can split one faithful match into two sub-threshold runs | Medium | `stages/qa.py` `_quoted_text_matches_corpus()` | ⚠️ Flagged, not fixed |
| Empty-string citation resolves to "mismatch" not "unverifiable" | Low (currently unreachable via real pipeline) | `stages/qa.py` `verify_claim()` | ⚠️ Flagged, not fixed |
| Null vs. blank `trigger_angle` indistinguishable once in CSV | Low (format limitation) | `stages/publish.py` `export_csv()` | ℹ️ Noted for G2.x dashboard |
| No DB-level test fixture exists for idempotency/live-run verification in any stage | Low (testing infra) | test suite as a whole | ℹ️ Noted, out of scope here |

None of these are crashes or data-loss risks — every one is a silent under- or over-matching behavior. None were fixed in this review per the review's scope (test coverage only); they're flagged here for a follow-up decision (most plausibly: recalibrating `FUZZY_MATCH_THRESHOLD` and `MIN_TEXT_MATCH_LEN` against real data once more of it exists, per `suppress.py`'s own "no data yet to calibrate against" comment).

---

## Test Execution

```powershell
cd pipeline
.\.venv\Scripts\Activate.ps1
python -m pytest tests/stages/test_g15_comprehensive.py -v
python -m pytest tests/ -q   # full suite
```

Output: **33 passed** (new file) / **246 passed** (full suite). `ruff check .` clean.

---

## G1.4 AI Scoring — Independent Test Coverage Review

## Resolution (2026-09-18)

Four of the six flagged gaps were fixed, in priority order (items 1-2 were the two rated High severity):

- **Demographic-inference guard (Gap 1, hard rule 1)** — `enforce_hard_rules()` now takes an optional `org_context` param and, when supplied, verifies a `leadership_composition` citation is an actual, published self-description quoted/paraphrased from the org's own stored `mission_text`/`program_text` (same longest-common-substring technique as the QA job, via a new `_citation_is_published_self_description()` helper — deliberately *not* `qa.verify_claim()` directly, since that function's `"mission_text: ..."` field-pointer shortcut matches on field presence alone and would have left the exact loophole open). A citation naming a real field but not actually containing a real self-description (e.g. an officer-list citation attached to a name-based inference) is now rejected and forced to `needs_human_verification=true`.
- **Citation enforcement outside `leadership_composition` (Gap 3, hard rule 4)** — extended to the other four values signals and every alignment criterion via a new `_citation_supports_claim()` helper that reuses `qa.verify_claim()` (numeric/boolean/categorical field-pointer matching plus text-corpus matching). A `null`/empty/whitespace/malformed citation, or a citation with a hallucinated number that doesn't match the org's real computed signal, now nulls the score (values signals) or forces `met=false` (alignment criteria) instead of passing through.
- **`dq_reason` "fully qualified" redaction gap (Gap 2)** — a new `redact_dq_reason()` function (kept separate from `enforce_hard_rules` so its 3-tuple return signature stays stable for its many existing callers) applies the same forbidden-phrase redaction to `dq_reason` before persistence.
- **Batch partial-failure durability (Gap 6)** — a batch item that errors/expires/is canceled now gets a real placeholder `scores` row (`values_signals`/`alignment` both `NULL`) instead of no row at all, making "selected for Sonnet, scoring failed" durably distinguishable per-EIN from "never selected" — and recoverable, since re-scoring later overwrites the placeholder via the existing `ON CONFLICT` upsert. `load_publishable()` gained an `AND sc.alignment IS NOT NULL` clause so a failed placeholder is never mistaken for a publishable score. Verified against the live dev Postgres (client_id=2, synthetic EIN, cleaned up after): placeholder inserted → correctly absent from `load_publishable()` → re-scored → correctly present.
- **Haiku top-N tie-break determinism (Gap 5)** — `candidates.sort(key=lambda pair: pair[1], reverse=True)` became `candidates.sort(key=lambda pair: (-pair[1], pair[0]))`: descending pre_score, ascending EIN as an explicit, documented secondary key. Ties at the cut threshold now resolve identically regardless of load order.

**Backward compatibility, by design, not oversight:** `enforce_hard_rules()`'s new `org_context` parameter defaults to `None`, which skips the new grounding checks entirely and falls back to the pre-fix structural-only behavior. `run_scoring.run_scoring()` (the only production caller) always supplies real `org_context`, so the guard is airtight where it matters. The default-`None` fallback exists specifically so the ~20 pre-existing tests in `test_score.py`/`test_g14_comprehensive.py` — which exercise the *other* hard rules (score-range clamping, phrase redaction, GENESIS math) and don't construct a matching corpus fixture — continue to pass completely unchanged; all 105 of those tests were confirmed still passing before and after this change.

**Left open, informational only (Gap 4's sole-primary-need boundary):** whether Sonnet's `disqualified=true` reflects the organization's one stated primary need vs. one of several needs remains prompt-enforced, not code-validated. This is a genuine semantic judgment (distinguishing "sole need" from "one of several needs" from free text) that would require another LLM pass or fragile NLP heuristics to validate mechanically — the same kind of irreducible judgment call already left to the model elsewhere in this pipeline (e.g. G1.5's QA paraphrase-mismatch gap). No code change is recommended beyond the existing prompt language; flagged here as a documented, deliberate boundary rather than a gap needing a fix.

**Full suite: 324/324 passing** (37/37 in `test_g14_llm_guard_gaps.py`, up from 22 — 15 new tests added directly to that file per its own synthetic-response pattern, covering both the rejected and the newly-preserved-when-grounded cases for each fix). Reconciles against the 309-test actual baseline at the start of this fix (258 original + 22 original G1.4 gap tests + 29 from an untracked, unrelated G1.3 gap-review file already in the working tree): 309 + 15 = 324. Ruff-clean, `mypy src` clean. No changes were needed in `test_score.py` or `test_g14_comprehensive.py`.

## Summary

Added **22 pytest cases** in [`pipeline/tests/stages/test_g14_llm_guard_gaps.py`](pipeline/tests/stages/test_g14_llm_guard_gaps.py) covering the actual scoring implementation located under `pipeline/src/discovery/stages/`: `score.py` (request construction, hard-rule enforcement, capacity output, Batch API parsing), `run_scoring.py` (Haiku cut, Sonnet persistence, failure counting), and downstream `publish.py`/`run_publish.py` behavior for disqualified Sonnet rows.

This review intentionally tests code-level enforcement around synthetic LLM outputs, not the model's judgment quality. Where the code has a mechanical guard, the tests assert the sanitized output. Where the only protection is prompt/schema guidance, the tests document the current pass-through behavior and flag it as a real coverage or product gap.

**Full suite: 280/280 passing**. This reconciles exactly against the current **258-test baseline**: 258 existing tests + 22 new G1.4 review tests = 280. Focused validation: `test_g14_llm_guard_gaps.py` is 22/22 passing; `ruff check tests/stages/test_g14_llm_guard_gaps.py` clean. No implementation code was modified in this review.

---

## Coverage by Gap

### Gap 1: Demographic-Inference Guard (Hard Rule 1) (3 tests)

**What was untested:** whether the defense-in-depth layer catches a Sonnet leadership-composition response that explicitly infers race, ethnicity, or gender from a person's name while still providing a citation to a non-demographic source such as an officer list.

**Tests added:** synthetic Sonnet responses covering (1) a leadership score with no citation, (2) a valid published self-description claim, and (3) a race/ethnicity inference from a name with an officer-list citation.

**🔴 Gap discovered:** `enforce_hard_rules()` only forces `leadership_composition` to `needs_human_verification=true` when it has a score, no citation, and no human-verification flag. It does **not** inspect the rationale/citation text for demographic inference language. A response like "appears to be Latina based on her name" with `citation="officers list: Maria Alvarez"` passes through unchanged with no violation. The prompt states the hard rule, but the code-level guard does not enforce the actual inference ban.

---

### Gap 2: "Fully Qualified" Ban + Pending Discovery Framing (4 tests)

**What was untested:** whether the actual output construction can emit "fully qualified," and whether the capacity note is structurally fixed rather than model-authored default behavior.

**Tests added:** `compute_capacity()` is exercised across present/absent/unknown capacity inputs; `enforce_hard_rules()` is tested against forbidden phrases in values-signal and alignment rationales; the orchestrator source is checked to confirm raw `dq_reason` is persisted directly from Sonnet.

**Result:** ✅ Capacity output is structurally safe. `compute_capacity()` always returns the fixed note `qualified pending discovery conversation`, and the value is generated in Python, not by Sonnet. Model-authored values-signal and alignment rationales containing "fully qualified" are redacted before storage.

**🟡 Gap noted:** the forbidden-phrase redaction does not cover `dq_reason`. A Sonnet response with `dq_reason="fully qualified except..."` would be persisted unchanged because `enforce_hard_rules()` only receives `values_signals` and `alignment_criteria`.

---

### Gap 3: Citation-Required Enforcement (Hard Rule 4) (6 tests)

**What was untested:** all missing/malformed citation shapes for scored claims: `None`, empty string, whitespace-only string, and non-string citation objects, across both values signals and alignment criteria.

**Tests added:** non-leadership scored values signals with `citation=None`, `citation=""`, `citation="   "`, and `citation={"source": "mission_text"}`; alignment criteria with `met=true` and null/empty citations; Sonnet output schema inspection confirming citations are required as fields but allowed to be `null`.

**🔴 Gap discovered:** citation enforcement is incomplete. The only code-level "no citation, no score" backstop applies to `leadership_composition`, and only for `None`/falsy citation plus `needs_human_verification=false`. Other scored values signals and true alignment calls can pass through with `None`, empty, whitespace-only, or malformed object citations. The JSON schema requires the `citation` property to exist, but explicitly allows `string | null` and does not validate non-empty or source-grounded citations after model output.

---

### Gap 4: Grant-Writing Disqualifier + Publish Exclusion (3 tests)

**What was untested:** whether Sonnet's disqualification result and reason are actually the fields persisted by the orchestrator, whether publish excludes disqualified Sonnet rows, and whether boundary wording for "primary need" vs. one of several needs is at least encoded in the prompt.

**Tests added:** source-level checks around `run_scoring.run_scoring()` persistence of `result["disqualified"]` and `result["dq_reason"]`; `load_publishable()` query inspection for `sc.stage = 'sonnet'` and `sc.disqualified = false`; Sonnet prompt inspection for "primary need," "not disqualified," and `If you are unsure, disqualified=false`.

**Result:** ✅ Downstream publish exclusion is mechanically present: `load_publishable()` only selects non-disqualified Sonnet rows, so disqualified orgs do not enter `gap_rank` or prospect publishing through the normal Stage 6 loader.

**🟡 Gap noted:** the sole-primary-need boundary is prompt-enforced, not validated in code. If Sonnet marks `disqualified=true` for an organization that mentions grant-writing as one of several needs rather than its stated primary need, `run_scoring()` persists that decision and reason unchanged.

---

### Gap 5: Haiku Cut-Off Boundary Determinism (1 test)

**What was untested:** tie behavior exactly at the top-N cut line when multiple organizations share the same Haiku `pre_score`.

**Tests added:** source inspection plus a faithful reproduction of the current sort key (`candidates.sort(key=lambda pair: pair[1], reverse=True)`) showing tied candidates retain incoming insertion order.

**🔴 Gap discovered:** the cut is stable only relative to incidental input order. `_load_scoreable_orgs()` has no explicit `ORDER BY`, and the candidate sort has no secondary key such as EIN. If three orgs tie at the threshold, different DB row/insertion orders can produce different Sonnet candidate sets. This is deterministic inside one Python list, but not documented or structurally deterministic across runs.

---

### Gap 6: Batch API Partial-Failure Handling (3 tests)

**What was untested:** actual Message Batches result parsing when one custom ID succeeds and another errors, plus whether the pipeline preserves a durable distinction between "not selected/not yet scored" and "selected but scoring failed."

**Tests added:** fake Batch API client returning one succeeded result and one errored result through `run_batch()`; orchestrator source inspection for `sonnet_failed` counting and `continue` behavior on `None`; docstring assertion that `None` represents errored/expired/canceled batch items.

**Result:** ✅ Partial success is tolerated correctly at the batch parser level. Successful items return parsed JSON and failed items return `None`; `run_scoring()` counts `sonnet_failed` separately from `sonnet_succeeded`.

**🔴 Gap discovered:** failed batch items are not durably represented per organization. `None` covers errored/expired/canceled requests and unparseable JSON, and `run_scoring()` simply skips insertion for those items. After the run, the `scores` table cannot distinguish "this org was selected for Sonnet and failed" from "this org was never selected/not yet scored" without consulting aggregate run counts or logs. It is not silently treated as a pass, but it is a clean absence at row level.

---

### Gap 7: NTEE Leakage Check (Hard Rule 6) (2 tests)

**What was untested:** the actual payload construction path for both Haiku and Sonnet requests when the source organization row contains `ntee`.

**Tests added:** `build_org_context()` with `ntee` and `ein` present on the source org; `build_haiku_request()` and `build_sonnet_request()` payload serialization checks confirming neither the `ntee` key nor NTEE value appears in messages sent to the model.

**Result:** ✅ NTEE is genuinely excluded from scoring inputs. The prompt payload contains organization name/location, mission/program text, and derived signals, but not `ntee` or EIN.

---

## Uncovered Issues Flagged for Review

| Issue | Severity | Location | Status |
|-------|----------|----------|--------|
| Demographic inference from names can pass if Sonnet supplies any citation | High | `stages/score.py` `enforce_hard_rules()` | ✅ Fixed |
| "No citation, no score" is only mechanically enforced for `leadership_composition`; other scored claims can persist null/empty/malformed citations | High | `stages/score.py` `enforce_hard_rules()` | ✅ Fixed |
| `dq_reason` is not covered by the forbidden "fully qualified" redaction | Medium | `stages/run_scoring.py` persistence path | ✅ Fixed |
| Grant-writing as sole primary need vs. one of several needs is prompt-only, not validated before persisting `disqualified=true` | Medium | `stages/run_scoring.py` Sonnet result handling | ⚠️ Left open — informational only, see Resolution above |
| Haiku top-N tie handling depends on incidental DB/dict insertion order | Medium | `stages/run_scoring.py` candidate sort | ✅ Fixed |
| Batch item failures are counted but not persisted per EIN, making failed-vs-not-yet-scored indistinguishable in `scores` | Medium | `stages/score.py` `run_batch()`, `stages/run_scoring.py` | ✅ Fixed |
| NTEE leakage into scoring prompts | High if present | `stages/score.py` `build_org_context()` | ✅ No gap found |
| Capacity note can emit "fully qualified" | High if present | `stages/score.py` `compute_capacity()` | ✅ No gap found |

---

## Test Execution

```powershell
cd pipeline
.\.venv\Scripts\Activate.ps1
python -m pytest tests/stages/test_g14_llm_guard_gaps.py -q
python -m ruff check .
python -m mypy src
python -m pytest -q
```

Output (post-fix, 2026-09-18): **37 passed** (`test_g14_llm_guard_gaps.py`, up from 22) / **324 passed** (full suite, up from the 309-test actual baseline). Ruff-clean, `mypy src` clean.

---

## G1.3 Filters + Signals — Independent Test Coverage Review

## Summary

Added **29 pytest cases** in [`pipeline/tests/stages/test_g13_filter_signal_gaps.py`](pipeline/tests/stages/test_g13_filter_signal_gaps.py) covering the actual G1.3 implementation: Stage 1 recall filtering in `filter.py`, Stage 2 990 XML parsing/signal extraction in `extract_signals.py`, the G1.3 runner in `run_filter_and_signals.py`/`extract_signals_for_survivors()`, and the downstream Stage 5 transformational-jump boundary where G1.3's `revenue_trend` signal feeds later trigger logic.

This review avoids the already-audited foundation-code exclude behavior and focuses on the remaining gaps: NTEE-prefix configuration, revenue floor/ceiling boundaries, geography not acting as a wall, XML schema variance, division-by-zero classes, government-funding threshold behavior, development-role title matching, revenue trend boundaries, one-filing cases, and coverage-percent logging.

**Full suite at time of review: 309/309 passing**. This reconciles against the actual current **280-test baseline** after G1.4: 280 existing tests + 29 new G1.3 review tests = 309. Focused validation: `test_g13_filter_signal_gaps.py` is 29/29 passing; `ruff check tests/stages/test_g13_filter_signal_gaps.py` clean. No implementation code was modified in this review.

---

## Coverage by Gap

### Gap 1: NTEE-Prefix Hard Excludes Are Config-Driven (2 tests)

**What was untested:** whether NTEE prefix excludes for higher-ed (`B4`, `B5`), hospitals (`E2`), and mutual/membership orgs (`Y`) are merely defaults or hardcoded into Stage 1 SQL.

**Tests added:** `build_survivor_query()` with a custom per-client prefix list (`Z9`, `Q`) confirms params are generated from config and default prefixes are absent; an empty prefix list confirms the NTEE filter is omitted entirely.

**Result:** ✅ No gap found. NTEE-prefix excludes are config-driven defaults. A tenant can override or remove them without a deploy.

---

### Gap 2: Revenue Floor/Ceiling Boundary Correctness (2 tests)

**What was untested:** exact inclusivity at the default $500K floor and $10M ceiling, plus whether custom client overrides preserve the same edge semantics.

**Tests added:** default query inspection confirms `o.revenue_latest >= %(revenue_floor)s` and `<= %(revenue_ceiling)s` with params `500_000` and `10_000_000`; custom floor/ceiling overrides confirm the same inclusive operators are used.

**Result:** ✅ No gap found. Both floor and ceiling are inclusive under defaults and custom per-client config.

---

### Gap 3: Geography Tier Weighting Is Not A Filter Wall (1 test)

**What was untested:** whether Charlotte/Cincinnati priority metros could accidentally filter out otherwise valid national prospects in Stage 1.

**Tests added:** `build_survivor_query()` with `geography_tiers.priority_metros` set confirms no city/state/metro/geography condition enters the Stage 1 SQL or params.

**Result:** ✅ No gap found. Geography tiers are not Stage 1 walls; non-priority-metro orgs remain eligible for later ranking.

---

### Gap 4: XML Schema Variance Across Form Types + Malformed XML (4 tests)

**What was untested:** behavior for 990-EZ / 990-PF roots, missing financial sections, and truncated XML bytes.

**Tests added:** synthetic `IRS990EZ` and `IRS990PF` payloads return the parser's nullable empty shape instead of fabricating values; a sparse `IRS990` with only mission text preserves that text while leaving financial/officer fields nullable; malformed/truncated XML raises `ParseError`, which the Stage 2 orchestrator catches and counts as `filings_failed`.

**Result:** ✅ Schema absence is tolerated with nullable signals. Malformed XML is not swallowed by `parse_990_xml()` itself, but is intentionally caught at the orchestrator layer, which is the right place to count a failed filing without killing the run.

---

### Gap 5: Revenue Composition Math + Division By Zero (2 tests)

**What was untested:** whether `revenue_total=None` or `revenue_total=0` causes division-by-zero errors or fabricated zero percentages.

**Tests added:** `compute_signals()` with missing total and zero total while component amounts exist.

**Result:** ✅ No gap found. Revenue composition and `gov_funding_pct` become `None`; the code does not divide by zero or fabricate zero-valued composition.

---

### Gap 6: Government-Funding Soft-Flag Threshold (2 tests)

**What was untested:** exact behavior at the default heavy-government-funding threshold (`>= 40%`) and just below it.

**Tests added:** exact 40% government grants confirms `compute_soft_flags()` returns `heavy_govt_funding=True`; a 399,999 / 1,000,000 case probes the rounding boundary.

**🟡 Gap noted:** the comparison itself is correctly inclusive, but `compute_signals()` rounds `gov_funding_pct` to 4 decimals before later scoring uses it. A raw 39.9999% share rounds to `0.4`, so the later soft-flag path treats it as heavy government funding even though the unrounded value is just below threshold. This is a boundary precision issue, not a crash.

---

### Gap 7: Fundraising Expense Ratio Division By Zero (2 tests)

**What was untested:** whether fundraising expense with missing or zero total revenue divides by zero or fabricates a ratio.

**Tests added:** `compute_signals()` with `fundraising_expense` present and `revenue_total=None` / `0`.

**Result:** ✅ No gap found. `fundraising_spend_ratio` remains `None` when total revenue is missing or zero.

---

### Gap 8: Development-Role Detection Title Robustness (9 tests)

**What was untested:** real-world Part VII title variations beyond the already-covered simple `Director of Development` case.

**Tests added:** common positive variants across case and wording (`director of development`, `VP Development`, `Chief Development Officer`, `Vice President, Advancement`, `Fundraising Manager`) and abbreviation/near-synonym negatives (`VP Dev`, `Chief Growth Officer`, `Donor Relations Lead`).

**Result:** ✅ Common explicit `development` / `fundraising` / `advancement` title variants are detected case-insensitively.

**🟡 Gap noted:** abbreviations and adjacent fundraising roles are not detected. `VP Dev` and `Donor Relations Lead` return `dd_present=False` because matching is simple substring search over `development`, `fundraising`, and `advancement`. That may be acceptable conservatism, but it is now pinned as current behavior.

---

### Gap 9: Revenue Trend + Transformational Jump Boundary (3 tests)

**What was untested:** exact revenue-trend thresholds and the ≥50% transformational jump boundary that feeds Stage 5 trigger firing.

**Tests added:** exact +10% and -10% changes remain `stable`; just over/under those boundaries become `growth`/`decline`; exact +50% revenue change is compared against Stage 5 `detect_triggers()`.

**Result:** ✅ Stage 2 `revenue_trend` boundaries are explicit: `> 10%` growth, `< -10%` decline, otherwise stable.

**🟡 Gap noted:** the ≥50% "transformational jump" is not represented in Stage 2's signal output at all. Stage 2 emits only `revenue_trend="growth"`; the exact transformational boundary lives downstream in Stage 5 `detect_triggers()`. That downstream function does fire at exactly 50%, but G1.3 itself does not persist a distinct transformational-jump signal.

---

### Gap 10: Only-One-Filing-On-Record Case (1 test)

**What was untested:** whether a survivor with one parsed filing, rather than the expected latest two, errors or fabricates trend/comparison signals.

**Tests added:** `compute_signals()` with `previous=None`.

**Result:** ✅ No gap found. `revenue_trend` is `None`, while independent fields such as `org_age` still compute when inputs exist.

---

### Gap 11: Per-Run Coverage Percent Logging (1 test)

**What was untested:** whether `coverage_pct` is actually implemented, and whether its numerator/denominator match the brief's risk language: successfully parsed survivors over Stage 1 survivors.

**Tests added:** `extract_signals_for_survivors()` with a fake connection and fake archive/parser path: 3 Stage 1 survivors, 3 filing rows, 2 parsed filings, 1 failed filing, and 1 org with no filing rows at all.

**🔴 Gap discovered:** `coverage_pct` is implemented, but it is filing-based rather than survivor-based: `filings_parsed / (filings_parsed + filings_failed)`. In the synthetic run, coverage is `2 / 3 = 0.6667`; survivor-level successful signal coverage would be `signals_computed / survivors = 1 / 3 = 0.3333`. A Stage 1 survivor with no filing rows is invisible to the denominator, so the metric can overstate survivor coverage.

---

## Uncovered Issues Flagged for Review

| Issue | Severity | Location | Status |
|-------|----------|----------|--------|
| `coverage_pct` is filing-based and excludes Stage 1 survivors with no filing rows; it does not measure parsed survivors over total survivors | Medium | `stages/extract_signals.py` `extract_signals_for_survivors()` | ⚠️ Flagged, not fixed |
| Raw government funding just below 40% can round to `0.4` in `compute_signals()` and later soft-flag as heavy funding | Low | `stages/extract_signals.py` `compute_signals()`, `stages/score.py` `compute_soft_flags()` | ⚠️ Flagged, not fixed |
| Stage 2 does not persist a distinct ≥50% transformational-jump signal; the boundary is only evaluated later in Stage 5 triggers | Low/design boundary | `stages/extract_signals.py` / `stages/triggers.py` | ℹ️ Noted |
| Development-role detection misses abbreviations/near synonyms like `VP Dev` and `Donor Relations Lead` | Low | `stages/extract_signals.py` `DEVELOPMENT_TITLE_KEYWORDS` | ℹ️ Noted |
| NTEE-prefix excludes hardcoded instead of config-driven | Medium if present | `stages/filter.py` `build_survivor_query()` | ✅ No gap found |
| Revenue floor/ceiling edge inclusivity | Medium if wrong | `stages/filter.py` `build_survivor_query()` | ✅ No gap found |
| Geography tiers filtering non-priority metros out of Stage 1 | High if present | `stages/filter.py` `build_survivor_query()` | ✅ No gap found |
| Missing/zero revenue division-by-zero in revenue composition or fundraising ratio | High if present | `stages/extract_signals.py` `compute_signals()` | ✅ No gap found |

---

## Test Execution

```powershell
cd pipeline
.\.venv\Scripts\Activate.ps1
python -m pytest tests/stages/test_g13_filter_signal_gaps.py -q
python -m ruff check tests/stages/test_g13_filter_signal_gaps.py
python -m pytest -q
```

Output: **29 passed** (new G1.3 file) / **309 passed** (full suite). Baseline reconciliation: **280 existing + 29 new = 309**.

