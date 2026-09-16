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
