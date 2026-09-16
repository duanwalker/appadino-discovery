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

## Tests

```powershell
./.venv/Scripts/python -m pytest
./.venv/Scripts/python -m ruff check .
./.venv/Scripts/python -m mypy src
```

Tests exercise the pure transform functions (`transform_bmf_row`, `transform_index_row`,
`latest_two_per_ein`) with fixture data — they don't hit the network or a live database, so they
run in CI unchanged.

## Building and pushing the image

```powershell
az acr build --registry adiscdevacr --image discovery-pipeline:latest .
```

Then re-run the infra deploy (`infra/README.md`) to point the Container Apps Job at the new tag,
or just re-run `az acr build` with the same tag — the job's cron trigger and any manual
`az containerapp job start` will pick up `latest` on the next execution.
