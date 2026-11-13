# Access control: independent gate-1 mutation and attack report

Date: 2026-11-12. Scope: v1 item 5, gate 1, using the current v2.7 brief,
access-control proposal, gate report, and STATUS. The older v2.3 brief is absent.

## Result

- **Original access-control tests detected 26/36 mutations; 10 were NOT caught.**
- **With the new tests, 36/36 are caught:** 35 by behavioral tests and one
  redundant SQL guard by an explicit statement-contract test.
- Final validation against the **original** module: **381 passed, 25 strict
  xfailed, zero failed/skipped**. Of these, the existing tests contribute 251
  passes; the new file contributes 130 passes and 25 known-gap xfails.
- No confirmed unauthorized read/write on the unmodified API under its documented
  trusted-SWA-header assumption. The mutants demonstrably returned tenant B's
  prospect IDs to tenant A, returned 200 instead of 404 for a cross-tenant PATCH,
  and persisted `mallory` as `updated_by`.
- Only [test_access_control_extra.py](dashboard/functions_api/tests/test_access_control_extra.py)
  and this report were added. Existing assertions were not changed.

## Safety and execution

All 36 mutants ran **one at a time**, each generated from the pristine source,
in a separate Python/pytest process importing a **scratch copy**. Removing a
route's authentication assigned a synthetic Caller instead of leaving an
undefined variable; the test failures are not NameError artifacts. Positional
SQL arguments were adjusted when removing predicates, avoiding placeholder-count
errors as false kills. The shared enrichment predicate was mutated once: both
entry points were exercised.

Mutation runs used an owned, throwaway `postgres:16` Docker container bound only
to `127.0.0.1`, a generated password, and SCRAM host authentication. A deliberately
wrong password was verified to fail. The scratch fixture reused that container;
the real migrations built its schema. DATABASE_URL was overwritten, never taken
from local settings. Final tests used the existing disposable-container fixtures,
with an unusable loopback DATABASE_URL as the inherited safety default.
All owned containers were removed. No real database, provider, infrastructure
file, or commit was touched.

The production file's SHA-256 before and after was:

```text
F67E9705F7F0D4EF886CF8D94AAA1E61B573B603B9324B4AC882C6C526F05018
```

The harness also compared the original bytes directly on exit, including failure
cleanup. The production module was never a mutation target.

## Mutation matrix

Test names below are pytest function names; parameterized families identify
the affected route/action. **Original verdict** refers only to the pre-existing
[unit](dashboard/functions_api/tests/test_access_control.py) and
[Postgres](dashboard/functions_api/tests/test_access_control_postgres.py) tests.
The final column includes the new file.

For all ten **a** rows, **AUTH** means these original tests failed for that route:
`test_no_principal_header_is_401_on_every_route` (1),
`test_malformed_principal_is_401_on_every_route` (12),
`test_valid_header_without_customer_role_is_403_before_any_lookup` (6),
`test_customer_role_but_not_in_client_users_is_403_on_every_route` (1), and
`test_listed_email_from_a_different_provider_is_403` (1).
Each also failed the new `test_role_near_misses_fail_before_lookup` (4) and
`test_non_object_bodies_cannot_bypass_authentication` (6).

**FORGED-ID** below means the new
`test_forged_tenant_on_by_id_routes_cannot_select_foreign_rows`.

| Mutation | Original tests that failed | Original verdict | New decisive test, if needed | Final verdict |
|---|---|---|---|---|
| a: remove auth, `list_prospects` | AUTH; `test_operator_client_header_is_ignored_when_mode_is_off`; `test_listed_user_reads_own_prospects`; `test_email_match_is_case_insensitive`; `test_inactive_user_is_403_and_reactivating_restores_access`; `test_unlisted_user_is_403`; `test_same_email_from_another_provider_is_403`; `test_list_ignores_forged_client_id` (28 original failures total) | caught | Not needed | caught |
| a: remove auth, `update_prospect` | AUTH; `test_patch_own_prospect_records_login_email_not_body` (22) | caught | Not needed | caught |
| a: remove auth, `enrich_prospect` | AUTH; `test_enrich_routes_on_other_customers_prospect_are_404_before_any_provider[enrich]` (22) | caught | Not needed | caught |
| a: remove auth, `enrich_status` | AUTH; `test_enrich_routes_on_other_customers_prospect_are_404_before_any_provider[enrich-status]` (22) | caught | Not needed | caught |
| a: remove auth, `suppression_review` | AUTH; `test_suppression_review_of_own_prospect_records_login_email` (22) | caught | Not needed | caught |
| a: remove auth, `list_suppression` | AUTH; `test_suppression_list_ignores_forged_client_id` (22) | caught | Not needed | caught |
| a: remove auth, `create_suppression` | AUTH; `test_create_suppression_ignores_forged_client_id_in_body` (22) | caught | Not needed | caught |
| a: remove auth, `delete_suppression` | AUTH; `test_delete_own_suppression_works` (22) | caught | Not needed | caught |
| a: remove auth, `export_csv` | AUTH; `test_inactive_user_is_403_and_reactivating_restores_access`; `test_export_ignores_forged_client_id` (23) | caught | Not needed | caught |
| a: remove auth, `list_runs` | AUTH; `test_runs_ignore_forged_client_id` (22) | caught | Not needed | caught |
| b: accept `authenticated` as well as `customer` | `test_valid_header_without_customer_role_is_403_before_any_lookup[authenticated-only]` on all 10 routes | caught | Not needed | caught |
| c: ignore allowlist `active` | `test_inactive_user_is_403_and_reactivating_restores_access` | caught | Not needed | caught |
| d: remove SQL `lower(email)` | None | **NOT caught** | `test_mixed_case_stored_email_matches_lowercase_login` | caught |
| e: query tenant, `list_prospects` | `test_list_ignores_forged_client_id` | caught | Not needed | caught |
| e: query tenant, `update_prospect` | None | **NOT caught** | FORGED-ID, PATCH | caught |
| e: query tenant, `enrich_prospect` | None | **NOT caught** | FORGED-ID, enrich | caught |
| e: query tenant, `enrich_status` | None | **NOT caught** | FORGED-ID, enrich-status | caught |
| e: query tenant, `suppression_review` | None | **NOT caught** | FORGED-ID, suppression-review | caught |
| e: query tenant, `list_suppression` | `test_suppression_list_ignores_forged_client_id` | caught | Not needed | caught |
| e: query tenant, `create_suppression` | None | **NOT caught** | `test_suppression_create_ignores_forged_query_client_id` | caught |
| e: query tenant, `delete_suppression` | None | **NOT caught** | FORGED-ID, DELETE | caught |
| e: query tenant, `export_csv` | `test_export_ignores_forged_client_id` | caught | Not needed | caught |
| e: query tenant, `list_runs` | `test_runs_ignore_forged_client_id` | caught | Not needed | caught |
| e: body tenant, `update_prospect` | None | **NOT caught** | FORGED-ID, PATCH | caught |
| e: body tenant, `create_suppression` | `test_create_suppression_ignores_forged_client_id_in_body` | caught | Not needed | caught |
| e: body tenant, `suppression_review` | None | **NOT caught** | FORGED-ID, suppression-review | caught |
| f: remove PATCH tenant predicate | `test_patch_other_customers_prospect_is_404_and_unchanged` | caught | Also FORGED-ID, PATCH | caught |
| f: remove shared enrich/enrich-status tenant predicate | `test_enrich_routes_on_other_customers_prospect_are_404_before_any_provider` (both parameters) | caught | Also FORGED-ID, both enrichment routes | caught |
| f: remove suppression-review SELECT tenant predicate | `test_suppression_review_of_other_customers_prospect_is_404_and_unchanged` (confirm and dismiss) | caught | Also FORGED-ID, suppression-review | caught |
| f: remove suppression-review UPDATE tenant predicate | None | **NOT caught** | `test_review_second_statement_still_binds_tenant` | caught, **statement contract only** |
| f: remove suppression DELETE tenant predicate | `test_delete_other_customers_suppression_is_404_and_row_survives` | caught | Also FORGED-ID, DELETE | caught |
| g: PATCH trusts body `updated_by` | `test_patch_own_prospect_records_login_email_not_body` | caught | Not needed | caught |
| g: suppression-review trusts body `updated_by` | `test_suppression_review_of_own_prospect_records_login_email` | caught | Not needed | caught |
| h: operator mode accepts `true` | `test_operator_mode_wrong_value_refuses_to_start[true]` | caught | Not needed | caught |
| i: omit Azure marker check | `test_operator_mode_in_azure_refuses_to_start` (both markers) | caught | Not needed | caught |
| j: listed email bypasses role check | `test_valid_header_without_customer_role_is_403_before_any_lookup` (60 parameters) | caught | Also 40 role-near-miss cases and the large-header role test | caught |

The query-tenant/create-suppression mutant initially survived the first extra
suite too. A separate query-only POST test was added and the pristine baseline
and that exact mutant rerun: baseline passed; **one new test failed**.

For the suppression-review UPDATE, the ownership-checked SELECT plus immutable
primary key masks the missing second predicate in sequential HTTP tests. The new
test captures the actual executed statement and its tenant binding while still
executing it on real Postgres. This is a defense-in-depth contract check, **not
evidence of an exploitable cross-tenant race or write in the original code**.

The SQL-lowercase survivor was different: the existing case test uppercases
the login, which the parser lowercases anyway. It never stored an uppercase
allowlist email. The new test changes the stored spelling, exercising the SQL.

## Attack attempts on the original code

| Idea tried | Result | Coverage/result classification |
|---|---|---|
| Duplicate `userRoles`, `identityProvider`, `userDetails`, both orders | JSON uses the last occurrence. Depending on that value, the result is Caller or 403, not a malformed-principal 401. | 6 strict xfails for desired ambiguity rejection; parser-hardening gap, **not a demonstrated SWA bypass**. |
| Extra `client_id`, `updated_by`, `userId`, claims | Extra values do not override the provider/email lookup or Caller audit email. | Passed. Email/provider mapping, not `userId`, is the documented design. |
| Very large principal header | A 1 MiB extra string (base64 header approximately 1.4 MiB) is accepted with a valid customer; `authenticated` alone still gets 403. | Both passed. No application header-size limit was observed; upstream transport limits were not tested. |
| Deeply nested principal JSON | A 10,000-level array in an extra field raises uncaught `RecursionError`. | 1 strict xfail; error-handling gap, not an unauthorized read. |
| Email trailing dot, Cyrillic look-alike, plus-address, internal space | All four variants get 403; they do not alias the listed email. | Passed against real allowlist SQL. |
| Email surrounding whitespace / mixed case | Both normalize to the listed email and return only its tenant's rows. Uppercase **stored** email also works. | Passed; the stored-email case kills mutation d. |
| Route IDs `-1`, `0` on all five by-id routes | All return 404. | 10 passes. |
| 200-digit route IDs / `1 OR 1=1; DROP TABLE client_users;--` on all five routes | Parameterized SQL raises uncaught psycopg data errors. The allowlist table and foreign prospects remain intact. | 10 strict xfails for desired 400/404; injection **did not execute**. |
| Roles `customer `, `CUSTOMER`, leading space, trailing tab | 403 before lookup/data access on every route. | 40 passes. |
| Valid customer, same email under two listed providers | AAD maps to tenant A; GitHub maps to tenant B, never A. Unlisted second-provider refusal is also covered by existing real-SQL tests. | Passed; provider-specific allowlisting, not email-only authorization. SWA's planned GitHub block is gate 2, outside this test. |
| PATCH bodies null, arrays, string, number, boolean | Unauthenticated requests still return 401 before body parsing on all routes. Authenticated PATCH raises `AttributeError`; no row changes. | 60 auth-order passes and 6 strict xfails for object validation. |
| PATCH `status` as list/object | Uncaught `TypeError`; original row unchanged. | 2 strict xfails. |
| Forged tenant in query/body on by-id routes and suppression creation | Foreign rows remain inaccessible/unchanged; inserted suppression belongs to the authenticated tenant. | Passed; closes eight caller-controlled-tenant mutation gaps across query/body surfaces. |
| Operator tenant header alongside a valid customer | Header is ignored with local mode off; only the customer's rows return. | Passed. |
| Same organization EIN in two tenants | List contains only A's prospects/notes; confirming A's suppression flag does not change B's prospect or create B suppression. | Passed. |

The **25 strict xfails are intentionally visible unresolved input/parser gaps**:
6 duplicate-key hardening expectations, 1 recursion error, 6 non-object PATCH
errors, 2 unhashable-status errors, and 10 invalid-ID errors. Their assertions
express the desired rejection behavior; expected exception types are constrained
and XPASS is a failure. No existing test was marked xfail or weakened.

## Security-review summary

| # | Severity | File | Lines | Vulnerability | Confidence |
|---|----------|------|-------|---------------|------------|
| - | - | - | - | No confirmed exploitable vulnerability under the documented SWA-only ingress assumption | - |

This is not deployment certification. Direct access to the Functions backend
would allow forged principal headers, as the existing proposal/STATUS explicitly
warn. No live endpoint or Azure resource was accessed. The already-documented
unbound enrichment `job_id` limitation remains outside this disabled-customer-
enrichment gate; no provider was called and these tests never enable enrichment.

## Final validation

From `dashboard/functions_api`, with DATABASE_URL set to an unusable loopback
address, operator mode off, and provider credentials empty:

```powershell
& .venv\Scripts\python.exe -B -m pytest -q --tb=short -ra tests\test_access_control.py tests\test_access_control_postgres.py tests\test_access_control_extra.py
```

Result: **381 passed, 25 xfailed in 32.79s**. Pylance syntax validation passed.
Repository Ruff check and formatting passed for the new file. No dependencies
were installed. Unrelated pre-existing worktree changes were left alone.
