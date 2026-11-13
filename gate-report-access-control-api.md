# Gate report: v1 item 5, gate 1 — access control in the Functions API

Date: 2026-11-12. **Not committed, not pushed, not deployed. No Azure resource was
created or touched, and nothing under `infra/` changed.** The change is in
`gate.patch`, made against HEAD `a1a6c6a400ccfb31aa02402a74e1e6d4ada13081`.
Migrations ran only against throwaway `postgres:16` Docker containers started by
the tests on 127.0.0.1 and killed afterwards.

This is Option A from `ACCESS-CONTROL-PROPOSAL.md`, API side only. The dashboard
(`dashboard/web`) and the Static Web Apps setup are gate 2.

## What changed

**1. `client_users` table** (migration `a5c2e7d9f1b3`, model `ClientUser`).
Columns: `identity_provider`, `email`, `client_id` (foreign key to `clients`),
`active` (default true), `created_at`. A unique index on
`(identity_provider, lower(email))` stops two spellings of one address mapping to
two customers, and checks reject blank email or provider. The real-Postgres tests
get the table the normal way, through `alembic upgrade head`.

**2. Who is calling.** Every route's first line is `_authenticate(req)`:

| Situation | Answer | Database touched? |
|---|---|---|
| `x-ms-client-principal` header missing, not base64, not JSON, not an object, blank provider or email, or `userRoles` not a list of strings | 401 | no |
| Valid header, but `userRoles` doesn't contain `customer` (including only `authenticated`, or a wrong-case `Customer`) | 403 | no |
| Has the role, but no `client_users` row for (provider, lowercased email), or the row is inactive | 403 | one lookup |
| Active row | the row's `client_id` and email are used | — |

**3. No `client_id` from the request.** It is gone from every query string
(`prospects`, `suppression`, `export.csv`, `runs`) and from the `POST /suppression`
body. A forged value is ignored, and tests prove it on real Postgres.
`/api/clients` is no longer registered in the deployed API. It exists only in
local operator mode (see below).

**4. Ownership checks.** These routes now match on the id **and** the caller's
`client_id`, so another customer's row looks the same as a missing one (404):

- `PATCH /prospects/{id}`
- `POST /prospects/{id}/enrich` and `GET .../enrich-status` (through
  `ENRICH_CANDIDATE_QUERY`; the 404 comes before any FullEnrich client is built)
- `POST /prospects/{id}/suppression-review` (both the lookup and the
  `UPDATE`)
- `DELETE /suppression/{id}`

**5. `updated_by`** is the logged-in email on `PATCH` and `suppression-review`
(and in the `suppression.source` text "dashboard confirm by …"). A body
`updated_by` is ignored and is no longer required.

## How you keep viewing several customers locally

**Local operator mode. It is a bypass and skips login, so it is clearly marked in
the code and README.** Add two lines to your own `local.settings.json`:

```json
"DISCOVERY_LOCAL_OPERATOR_MODE": "local-only-skips-login",
"DISCOVERY_LOCAL_OPERATOR_EMAIL": "you@example.com"
```

Then `func start` registers `/api/clients`, takes `client_id` from an
`x-local-operator-client-id` header, records your email as `updated_by`, and logs
a warning at startup.

Why it can't be switched on by accident in Azure:

- **The exact value is required.** `1`, `true` or a typo makes the app refuse to
  start, rather than silently ignoring the setting.
- **It only works under Core Tools.** It needs
  `FUNCTIONS_CORETOOLS_ENVIRONMENT=true` and
  `AZURE_FUNCTIONS_ENVIRONMENT=Development`. I checked with a throwaway probe app
  that `func start` (Core Tools 4.14.0) passes both to the Python worker, and
  passes no Azure markers.
- **It refuses to start in Azure.** If `WEBSITE_INSTANCE_ID` or `CONTAINER_NAME`
  (set by the Azure host) is present while the setting is on, the app raises at
  import. A copied setting gives you no API at all, never an API without login.
- **The rest of the code is unaffected when it's off.** The decision is made once
  at startup. When it's off, the operator header is never read, `/api/clients` is
  not registered, and `list_clients_local_operator` returns 404 even if it were
  called.

The alternative with no bypass at all is the SWA CLI emulator (`swa start`), which
fakes the login header, plus one `client_users` row per customer you want to
view. Those would be real access rows in the shared database, so I chose the
guarded switch instead. It's your call if you'd rather have zero bypass code.

**The dashboard (`dashboard/web`) doesn't send either header yet.** Until gate 2
it can't load data against this API, locally or anywhere.

## Tests

Run with Docker running, with `DATABASE_URL` unset in the test shell:

| Suite | Result |
|---|---|
| `pipeline`: `pytest` | **510 passed**, 0 skipped (the Docker Stage 2 integration tests ran) |
| `dashboard/functions_api`: `pytest` (with `TEST_DATABASE_URL` set to a throwaway container) | **572 passed, 4 xfailed**, 0 skipped. The 4 xfails are existing strict xfails for known bugs. |
| `pipeline`: `ruff check .` | clean |
| `pipeline`: `mypy src` | clean (36 files) |
| `dashboard/functions_api`: `ruff check .` | 9 findings, **the same 9 as at HEAD** (checked on an exact HEAD copy). CI doesn't lint this folder. |
| `mypy function_app.py` | 7 "missing stubs" import notices, the same as at HEAD; no type errors |

There are 251 new tests:

- **`tests/test_access_control.py` (231, no database).** The routes are read from
  the FunctionApp's own registry, and the route list is pinned, so a future route
  without `_authenticate` fails. For **every route**, it checks:
  - 401 with no header;
  - 401 for 12 malformed headers;
  - 403 for 6 role sets without `customer`, and the lookup never runs;
  - 403 for an unlisted email;
  - 403 for a listed email from another provider.

  `_conn` is replaced with a function that fails the test, so none of these
  reach the database. It also tests the operator-mode guard: off by default,
  exact value only, refuses in Azure, refuses outside Core Tools, needs an email,
  and ignores the header when off.
- **`tests/test_access_control_postgres.py` (20, real Postgres).** These use the
  real lookup, queries and migrations, with two customers, A and B, and A's login
  trying B's rows:
  - list, export (CSV EINs and filename), suppression list and runs all ignore
    `?client_id=B`;
  - `POST /suppression` with `client_id: B` in the body is stored under A;
  - PATCH, suppression-review (confirm and dismiss), DELETE suppression, enrich
    and enrich-status on B's ids all return 404 with B's rows unchanged and no
    `enrichment_jobs` claim;
  - a control call shows B's own login reaches the same rows;
  - inactive returns 403, and reactivating restores access;
  - an unlisted email and a wrong provider return 403;
  - email matching is case-insensitive;
  - the unique index rejects a second spelling of one email;
  - `updated_by` is the login email even when the body says otherwise.

**Mutation check.** I removed the five ownership filters from a copy of
`function_app.py` and reran the Postgres suite: exactly the 6 by-id ownership
tests failed. The original file was restored and verified byte-identical.

**Existing tests changed.** Their request helpers now send a valid customer
header. Tests that asserted the old "client_id is required" 400s or the old
`/api/clients` listing were rewritten to assert the new behaviour. To keep the
fake-connection tests working, `conftest.py` stubs `_lookup_client_id` for them
(one known user mapped to client 2). Header parsing and the role check still run
for real. The real-Postgres module opts out of the stub with the
`real_client_lookup` marker.

## Found along the way, not fixed (outside this gate's scope)

1. **`enrich-status` trusts the `job_id` query parameter.** The prospect is now
   ownership-checked, but `job_id` isn't tied to that prospect's own
   `enrichment_jobs` row. A customer could poll some other FullEnrich job id
   under their own subaccount and have its result saved onto one of their own
   prospects. With one customer this is low risk. It should be tied to
   `enrichment_jobs` before a second customer.
2. **Two database connections per request**: the `client_users` lookup, then the
   route's own query. That's fine at one customer's traffic, but it adds a
   connection setup (about 100 ms to Azure Postgres) to every call. Connection
   pooling, or reusing one connection, is a later tidy-up.
3. **Malformed ids** (`/prospects/abc`) still reach SQL and return 500. This was
   already the case (TEST-COVERAGE-GAPS.md) and doesn't leak data.
4. **Rows with a NULL `client_id` in `runs`** (national runs) are not visible to
   customers. That was already true before this change, because the old filter
   also excluded them.

## Files in the patch

New:

- `pipeline/alembic/versions/a5c2e7d9f1b3_v1_5_client_users_table.py`
- `pipeline/src/discovery/models/client_users.py`
- `dashboard/functions_api/tests/_auth_helpers.py`
- `dashboard/functions_api/tests/test_access_control.py`
- `dashboard/functions_api/tests/test_access_control_postgres.py`
- `gate-report-access-control-api.md` (this file)

Modified:

- `pipeline/src/discovery/models/__init__.py`
- `dashboard/functions_api/function_app.py`
- `dashboard/functions_api/README.md`
- `dashboard/functions_api/tests/conftest.py`
- `dashboard/functions_api/tests/test_g2_dashboard_gaps.py`
- `dashboard/functions_api/tests/test_csv_export.py`
- `dashboard/functions_api/tests/test_csv_export_adversarial.py`
- `dashboard/functions_api/tests/test_e2_enrich_routes.py`
- `dashboard/functions_api/tests/test_e2_independent.py`

Not included: `.gitignore`, `infra/modules/containerAppsJob.bicep`, the untracked
report/output files, and `prospects-backup-client2.json` (which appeared during
the session and isn't from this work). STATUS.md is not updated; item 5 stays
open until gate 2.

## Before deploying (gate 2 and after)

- The migration has **not** been run against the dev or production database.
  It needs `alembic upgrade head` there before this API is deployed, or every
  call returns 500 because `client_users` won't exist.
- Insert Lauren's row (`'aad'` or `'google'`, depending on how she signs in).
- Dashboard: drop the client switcher and the name box, handle 401/403, and send
  `x-local-operator-client-id` in local dev.
- `staticwebapp.config.json`: require the `customer` role on `/*` and `/api/*`,
  and block GitHub login.
