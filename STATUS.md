# Status

_Session-start document. Read this first — see the implementation brief (`discovery-phase1-implementation-brief-v2.2.md`) for full context._

## Done

**G1.2 — Ingest** ✅ closed 2026-09-16

- `pipeline/src/discovery/`: SQLAlchemy models (`Organization`, `Filing`, `Run`) + Alembic migration `102f11dd6616` applied to the live DB; `db.py`; `stages/ingest.py` (Stage 0, §4); `clients/propublica.py` (backfill client, not yet called anywhere — see below); `cli.py` (`python -m discovery.cli ingest`).
- **Schema deviation from the brief's §3 sketch**: `filings` gains an `object_id` column. The IRS's AWS S3 dataset for individually-addressable 990 XML files was deprecated Dec 2021 (confirmed via AWS's own registry page) — filings are now only distributed in monthly ZIP archives per submission year. `xml_object_url` stores the archive directory for the filing's submission year; `object_id` is the key G1.3 will need to locate the specific filing once it resolves which archive contains it. Documented in the model's docstring.
- BMF loader pulls all 4 IRS regional extracts (`eo1`–`eo4.csv`); 990 index loader pulls the e-file index for the current submission year + 2 prior (absorbs the 12–18mo filing lag, §10), filtered to `990`/`990EZ` and EINs already in `organizations`, keeping the latest 2 filings per EIN. Both use raw psycopg COPY + staging-table upsert (not the ORM) for national-scale throughput.
- **Ran for real against the deployed Postgres, twice locally + once as the actual Container Apps Job on Azure — all three produced identical counts (no duplication), confirming idempotency**: 1,964,958 organizations, 1,125,019 filings. `runs` table has one logged row per execution (id 1–3), each `status='success'` with `counts` JSONB populated. All three G1.2 acceptance criteria met.
- Container Apps Job swapped from the G1.1 hello-world placeholder to the real pipeline: added `adiscdevacr` (Basic ACR) to Bicep, `discovery-pipeline:latest` built via `az acr build` and confirmed pulled successfully by the job; job identity granted `AcrPull`; `DATABASE_URL` now sourced as a Container Apps secret directly from the `db-connection-string` Key Vault entry (no more manual env wiring); `replicaTimeout` raised to 3600s and resources to 1 vCPU/2Gi for the real workload.
- 8 unit tests for the pure transform functions (`transform_bmf_row`, `transform_index_row`, `latest_two_per_ein`) — no network/DB dependency, run in CI unchanged. `ruff` and `mypy --strict` both clean.
- `pipeline/README.md` added: local venv setup, running migrations/ingest against the deployed DB, building/pushing the image.
- Installed Python 3.12 on the dev machine (was only 3.11) since the package requires it.
- **Post-gate test review (Copilot, test developer role) found 3 real gaps in `stages/ingest.py`, all fixed**: (1) no EIN format validation — malformed EINs could pollute `organizations`' primary key, now dropped in `transform_bmf_row`/`transform_index_row`; (2) `staging_filings`/`staging_organizations` `object_id`/EIN could exceed their DB column lengths and fail the upsert mid-batch — now validated and dropped in `transform_index_row`; (3) both staging-table upserts used `SELECT DISTINCT ON (...)` without an `ORDER BY`, making the winning row non-deterministic when the same key appears twice in one batch — fixed by adding a `seq bigserial` column to each staging table and ordering by `seq DESC` (last-in-batch wins, consistent with how `ON CONFLICT` already makes the last write win across batches/runs). All three fixes verified against the live Postgres with synthetic duplicate-key batches. New tests added in `pipeline/tests/stages/test_ingest_gaps.py` (29 cases); full report in `TEST-COVERAGE-GAPS.md` at repo root. Suite is now 37/37 passing, ruff/mypy clean.

## Next

**G1.3 — Filters + signals**: Stage 1 SQL filter from `icp_configs` (introduces the `clients`/`icp_configs` tables, not yet created), Stage 2 990 XML parser. Stage 2 is also where the deferred zip-archive resolution from G1.2 gets solved: for survivors only, download the relevant monthly ZIP(s) for their `filings.xml_object_url` year, locate the member matching `object_id`, and parse it to fill in `revenue_total`, `contributions`, `program_revenue`, `govt_grants`, `fundraising_expense`, `officers`, `extracted_at`.

## Open questions / decisions awaiting Duan

- None blocking. G1.2 is closed; ready to start G1.3 whenever you are.

## Notes for cold-start

- Azure CLI is signed into the Pay-As-You-Go subscription used for this project; `rg-appadino-discovery-dev` is the only resource group so far.
- The Postgres admin password is stored as the `postgres-admin-password` secret in `adisc-dev-kv` (alongside `db-connection-string`, which already has it baked into the connection URI).
- Postgres firewall has an `AllowDevMachine` rule for this dev machine's IP (added during this gate to run migrations/ingest locally) — IP-specific, low risk, but flagging it exists in case it needs rotating or removing later.
- `anthropic-api-key` has not been added to Key Vault yet — needed before G1.4 (Scoring).
- No enrichment code exists anywhere, per §5.5/§9 — correct until gate E1 is explicitly opened.
- `discovery.clients.propublica.get_organization()` exists but is unused so far — it's meant for opportunistic backfill of survivors once G1.3's Stage 1 filter narrows the set, not for the national ingest.
