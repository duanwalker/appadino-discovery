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

