# Functions API — G2.x Dashboard

Python Azure Functions app (v2 programming model) that sits between the React
dashboard and Postgres, per brief §2/§5. Read-mostly: it renders `prospects` +
`scores` + `organizations` + `suppression` + `runs`, and writes exactly what a
human reviewer can change (`prospects.status`, `prospects.notes`,
`suppression` add/remove, `prospects.suppression_flag` clear-on-review). It
never touches anything the pipeline itself writes (`scores`, `gap_rank`,
`trigger_*`) — those stay pipeline-owned.

## How this app gets the `discovery` models (decided, not improvised)

The Functions app does not redeclare the schema. It imports
`pipeline/src/discovery/models/*` and `discovery.db` directly, so the
dashboard is reading exactly the same SQLAlchemy model definitions the
pipeline writes with — the one seam between the two apps stays a single
source of truth instead of two schemas that can drift apart.

**Local development:** `requirements.txt` installs the pipeline package as an
editable path dependency (`-e ../../pipeline`). This works because local dev
always has the full monorepo checked out — `func start` / `pip install -r
requirements.txt` resolves the relative path straight off disk. This is the
only import mechanism implemented so far, and it's what's exercised by
running the dashboard locally for this review.

**Deployment (not yet implemented — flagged, not built this pass):** Azure
Functions zip-deploys only this directory's contents; a sibling relative path
like `../../pipeline` will not exist in that zip, so the editable install
above will *not* work unqualified for a real deploy. The intended fix, to be
done as an explicit CI step before this ships: build the `discovery` package
as a wheel from `pipeline/` (`pip wheel ./pipeline -w
dashboard/functions_api/.python_packages/lib/site-packages`, or an
equivalent Oryx remote-build step) and vendor it into this app's deployment
payload before `func azure functionapp publish`. Do this as a build step, not
by committing a copy of the source — the goal is "one source of truth,"
publishing a snapshot at build time preserves that; hand-copying the source
into this directory would defeat the whole point. No Functions infra
(staticwebapp.bicep / function app registration) is provisioned yet either —
this app currently only runs via `func start` against a local settings file.

## Running locally

1. `cp local.settings.json.example local.settings.json` and fill in
   `DATABASE_URL` (see STATUS.md's "Notes for cold-start" for where the dev
   connection string lives in Key Vault).
2. `python -m venv .venv && .venv/Scripts/activate` (or your shell's
   equivalent), then `pip install -r requirements.txt`.
3. `func start` (Azure Functions Core Tools) — serves on `http://localhost:7071`.
4. The React dev server (`dashboard/web`) proxies `/api/*` to this port — see
   `dashboard/web/vite.config.ts`.

## Endpoints

Every route needs a logged-in caller (see "Access control" below). None takes
`client_id`: it comes from the caller's `client_users` row. Anything a request
sends as `client_id` is ignored.

- `GET  /api/prospects`
- `PATCH /api/prospects/{id}` — body `{status?, notes?}`
- `POST /api/prospects/{id}/enrich`
- `GET  /api/prospects/{id}/enrich-status?job_id=`
- `POST /api/prospects/{id}/suppression-review` — body `{action: "confirm"|"dismiss"}`
- `GET  /api/suppression`
- `POST /api/suppression` — body `{ein?, org_name, kind, source?}`
- `DELETE /api/suppression/{id}`
- `GET  /api/export.csv` — same column set as `pipeline/output/prospects_client2.csv`
- `GET  /api/runs?stage=&limit=`

`/api/clients` exists only in local operator mode (below).

## Access control (v1 item 5, ACCESS-CONTROL-PROPOSAL.md Option A)

Each route calls `_authenticate(req)` before anything else:

1. It reads the `x-ms-client-principal` header that Static Web Apps adds after login
   (base64 JSON: `identityProvider`, `userDetails`, `userRoles`). If the header is
   missing or malformed, the answer is **401**.
2. `userRoles` must contain `customer`, the invite-only SWA role. Otherwise the
   answer is **403**, before any database query.
3. It looks up `client_users` by (`identity_provider`, `lower(email)`). If there is
   no row, or the row has `active = false`, the answer is **403**. The row's
   `client_id` is the only tenant the request can touch, and the email becomes
   `updated_by`.

A route that takes a row id (`PATCH`/`enrich`/`enrich-status`/`suppression-review`
on a prospect, and `DELETE` on a suppression entry) matches on that id **and** the
caller's `client_id`. Another customer's row looks the same as a missing one:
**404**.

The header can only be trusted behind SWA, which overwrites any copy a browser
sends. Never expose this API on its own address.

To add a customer: insert `client_users (identity_provider, email, client_id)`, for
example `('aad', 'lauren@example.org', 2)`, and invite them in SWA with the
`customer` role. To remove one: `UPDATE client_users SET active = false ...`, which
takes effect on their next request, then remove the role in SWA.

### Local operator mode: LOCAL ONLY, skips login

This mode lets you switch between customers while running `func start` on your own
machine. Add these to your own `local.settings.json` (never to Azure):

```json
"DISCOVERY_LOCAL_OPERATOR_MODE": "local-only-skips-login",
"DISCOVERY_LOCAL_OPERATOR_EMAIL": "you@example.com"
```

With it on, the API skips the login checks and takes `client_id` from the
`x-local-operator-client-id` request header. `/api/clients` is registered, and the
host logs a warning at startup. The app **refuses to start** if the value isn't
exactly `local-only-skips-login`, if Core Tools' `FUNCTIONS_CORETOOLS_ENVIRONMENT=true`
/ `AZURE_FUNCTIONS_ENVIRONMENT=Development` aren't set, or if an Azure host marker
(`WEBSITE_INSTANCE_ID`, `CONTAINER_NAME`) is present. So if the setting is copied
into Azure, the result is no API at all, not an API without login.

The dashboard (`dashboard/web`) doesn't send either header yet. Until it does, it
can't load data against this API locally (that's gate 2 of item 5).
