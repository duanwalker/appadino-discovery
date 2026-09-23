# Pipeline — Appadino Discovery

Python 3.12 package that implements the pipeline stages (§4 of the implementation brief).
Runs as the Azure Container Apps Job (`adisc-dev-pipeline-job`), built from this directory's
`Dockerfile` and pushed to `adiscdevacr`.

## Local setup

```powershell
py -3.12 -m venv .venv
./.venv/Scripts/python -m pip install -e ".[dev]"
```

## Running against the deployed database

```powershell
$env:DATABASE_URL = az keyvault secret show --vault-name adisc-dev-kv --name db-connection-string --query value -o tsv
```

Postgres only accepts connections from Azure services and explicitly firewalled IPs — add your
current one once per machine:

```powershell
az postgres flexible-server firewall-rule create `
  --resource-group rg-appadino-discovery-dev --name adisc-dev-pg --rule-name AllowDevMachine `
  --start-ip-address <your IP> --end-ip-address <your IP>
```

### Migrations

```powershell
./.venv/Scripts/python -m alembic upgrade head
./.venv/Scripts/python -m alembic revision --autogenerate -m "..."   # after changing models
```

### Ingest (Stage 0, §4)

```powershell
./.venv/Scripts/python -m discovery.cli ingest
```

Populates `organizations` from the IRS Business Master File (all 4 regional extracts) and indexes
the latest 2 filings per EIN into `filings` from the IRS 990 e-file index (current submission year
+ 2 prior, to absorb the 12–18mo filing lag). Idempotent — safe to re-run; upserts are keyed on
`organizations.ein` and `filings.(ein, object_id)`. Logs a row to `runs` with counts either way.

Takes roughly 15-20 minutes end to end (national BMF is ~2M orgs; the 990 index spans ~90MB/year).

### Filter + signal extraction (Stages 1-2, §4)

```powershell
./.venv/Scripts/python -m discovery.cli filter <client_id>
```

Stage 1 reads the client's active `icp_configs` row and runs a config-driven SQL recall filter
over `organizations`/`filings` (revenue band, foundation-code and NTEE-prefix excludes) — no AI,
no network calls. Stage 2 then resolves each survivor's up-to-2 filings to their IRS 990 e-file
monthly ZIP archive (the old per-filing S3 URLs were deprecated Dec 2021). Every archive IRS has
published for `default_target_years()` is downloaded once to a local cache — an Azure Files share
mounted at `ARCHIVE_CACHE_DIR` (default `/mnt/irs-archive-cache`) in production — and recorded in
the `archive_manifest` table; see `discovery.stages.extract_signals.sync_archive_manifest` for the
probe/download/sanity-check logic. Filing lookups then read the local copy via stdlib `zipfile`
instead of opening a fresh remote connection per filing. Stage 2 parses the XML, fills in
`filings`' financial columns, and computes derived signals into `signals`. Every filing that can't
be resolved or parsed (unsupported ZIP compression, schema variance, network errors) is counted,
not fatal — the run logs a `coverage_pct` so degraded extraction is visible without failing the
job (§10).

Only the ARCHITECT client (`client_id=2` in the dev DB) has a seeded `icp_configs` row so far.

### Scoring (Stage 3, §4)

```powershell
$env:ANTHROPIC_API_KEY = az keyvault secret show --vault-name adisc-dev-kv --name appadino-discoveryAI-key --query value -o tsv
./.venv/Scripts/python -m discovery.cli score <client_id> [--haiku-cut-n N]
```

Haiku pre-screens every scoreable survivor (has both extracted filing text and a computed signal)
on mission/program text; Sonnet deep-scores the top N (config `haiku_cut_n`, default 3000) that
survive the cut. Both passes run over the Message Batches API (§2, 50% discount) and write one
`scores` row each (`stage='haiku'` / `stage='sonnet'`) per org, keyed on `(client_id, ein,
icp_version, stage)`.

Text inputs are 990 Part III mission/program narrative only — §4's "mission/program text from 990
+ website title/description" was narrowed to 990-only for this gate; see STATUS.md. `capacity` is
always computed in Python from `signals`, never by the model. Every Sonnet response passes through
`discovery.stages.score.enforce_hard_rules` before being persisted — a code-level, not just
prompt-level, enforcement of §9 rules 1 (no demographic inference) and 2 (never "fully qualified").

The Anthropic key in Key Vault is named `appadino-discoveryAI-key`, not `anthropic-api-key` — code
and docs reference it as-is.

### Suppress + triggers + publish + QA (Stages 4-6, §4, §7)

```powershell
./.venv/Scripts/python -m discovery.cli publish <client_id>
```

Stage 4 excludes EIN-exact suppression matches outright; fuzzy name matches (against ARCHITECT's
seed list, §4 Stage 4) are flagged in `prospects.notes`, never silently dropped (§9 rule 7). Stage 5
diffs each survivor's latest vs. prior filing for the 4 filing-derived triggers; the trigger→angle
mapping is read from `icp_configs.config.trigger_angles` and stays `null` when the brief doesn't
give one (e.g. `first_filing_above_floor`) rather than inventing text. Stage 6 upserts `prospects`
for every non-disqualified, non-suppressed, Sonnet-scored survivor, computes `gap_rank` (a
first-pass, config-weighted formula — see STATUS.md for a real consequence of the default weights
worth knowing about), and writes a CSV to `pipeline/output/` (gitignored — no blob storage
provisioned yet). The QA job then samples up to 20 cited claims and re-verifies each against our own
stored source data (`discovery.stages.qa.verify_claim`); a mismatch rate over 10% logs a
`QA_MISMATCH_RATE_EXCEEDED` marker.

Every orchestrator (`ingest`, `filter`, `score`, `publish`) now logs `RUN_FAILED`/`ZERO_OUTPUT_RUN`
markers to stdout on the relevant condition — picked up by 3 Azure Monitor scheduled query rules
(§7) watching the Container Apps environment's `ContainerAppConsoleLogs_CL` table, emailing the
operator via the `adisc-dev-ops-ag` action group.

## Tests

```powershell
./.venv/Scripts/python -m pytest
./.venv/Scripts/python -m ruff check .
./.venv/Scripts/python -m mypy src
```

Tests exercise pure functions only (`transform_bmf_row`, `transform_index_row`,
`latest_two_per_ein`, `build_survivor_query`, `parse_990_xml`, `compute_signals`,
`enforce_hard_rules`, `detect_triggers`, `compute_gap_rank`, `verify_claim`, `find_fuzzy_match`)
with fixture data — they don't hit the network or a live database, so they run in CI unchanged.
`tests/stages/fixtures/sample_990.xml` is a real filing fetched during G1.3 to ground the XML
field-mapping tests in actual IRS data rather than schema docs alone.

## Building and pushing the image

```powershell
az acr build --registry adiscdevacr --image discovery-pipeline:latest .
```

Then re-run the infra deploy (`infra/README.md`) to point the Container Apps Job at the new tag,
or just re-run `az acr build` with the same tag — the job's cron trigger and any manual
`az containerapp job start` will pick up `latest` on the next execution.
