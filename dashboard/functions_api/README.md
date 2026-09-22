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

- `GET  /api/prospects?client_id=`
- `PATCH /api/prospects/{id}` — body `{status?, notes?, updated_by}`
- `POST /api/prospects/{id}/suppression-review` — body `{action: "confirm"|"dismiss", updated_by}`
- `GET  /api/suppression?client_id=`
- `POST /api/suppression` — body `{client_id, ein?, org_name, kind, source}`
- `DELETE /api/suppression/{id}`
- `GET  /api/export.csv?client_id=` — same column set as `pipeline/output/prospects_client2.csv`
- `GET  /api/runs?client_id=&stage=&limit=`

No auth is implemented yet — flagged as a pre-go-live gap alongside the
personal-API-key item already in STATUS.md's cold-start notes. Fine for a
single-operator dev review, not for handing Lauren a public URL.
