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
monthly ZIP archive (the old per-filing S3 URLs were deprecated Dec 2021 — see
`discovery.stages.extract_signals` for how archive/member lookup works via `remotezip` range
requests), parses the XML, fills in `filings`' financial columns, and computes derived signals into
`signals`. Every filing that can't be resolved or parsed (unsupported ZIP compression, schema
variance, network errors) is counted, not fatal — the run logs a `coverage_pct` so degraded
extraction is visible without failing the job (§10).

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

## Tests

```powershell
./.venv/Scripts/python -m pytest
./.venv/Scripts/python -m ruff check .
./.venv/Scripts/python -m mypy src
```

Tests exercise pure functions only (`transform_bmf_row`, `transform_index_row`,
`latest_two_per_ein`, `build_survivor_query`, `parse_990_xml`, `compute_signals`) with fixture
data — they don't hit the network or a live database, so they run in CI unchanged.
`tests/stages/fixtures/sample_990.xml` is a real filing fetched during G1.3 to ground the XML
field-mapping tests in actual IRS data rather than schema docs alone.

## Building and pushing the image

```powershell
az acr build --registry adiscdevacr --image discovery-pipeline:latest .
```

Then re-run the infra deploy (`infra/README.md`) to point the Container Apps Job at the new tag,
or just re-run `az acr build` with the same tag — the job's cron trigger and any manual
`az containerapp job start` will pick up `latest` on the next execution.
