# Infra — Appadino Discovery

Bicep templates for the Phase 1 architecture (§2 of the implementation brief): PostgreSQL Flexible
Server, Key Vault, Container Apps environment + scheduled job, Application Insights.

## Prerequisites

- Azure CLI signed in (`az login`) to the target subscription.
- Bicep CLI (`az bicep install`).
- A resource group to deploy into.

## Deploying locally

```powershell
$rg = "rg-appadino-discovery-dev"
$pw = <generate a strong password, do not commit it>

az deployment group create `
  -g $rg `
  -f infra/main.bicep `
  -p infra/main.bicepparam `
  -p postgresAdminPassword=$pw
```

The admin password is never stored in the repo. `main.bicepparam` carries the non-secret defaults
(region, name prefix, admin login, the operator's AAD object ID for Key Vault access); the password
is supplied only at deploy time.

## What gets created

| Resource | Purpose |
|---|---|
| `adisc-dev-pg` | PostgreSQL Flexible Server (Burstable B1ms), `discovery` database |
| `adisc-dev-kv` | Key Vault (RBAC-authorized). Seeded with `db-connection-string`. Add `anthropic-api-key` manually after deploy; `fullenrich-api-key` only if the E-gates (§8) pass |
| `adisc-dev-law` / `adisc-dev-cae` | Log Analytics workspace + Container Apps environment |
| `adisc-dev-ai` | Application Insights, wired to the same Log Analytics workspace |
| `adisc-dev-pipeline-job` | Container Apps Job, cron-scheduled. **Runs a `mcr.microsoft.com/azure-cli` hello-world placeholder for the G1.1 acceptance test** — swapped for the real pipeline image (built from `/pipeline`) in G1.2 |

The job's user-assigned managed identity has `Key Vault Secrets User` on the vault; no secrets are
passed as plaintext environment variables. The signed-in operator (via `keyVaultAdminPrincipalId`)
has `Key Vault Secrets Officer` to add/rotate secrets by hand.

## Adding the Anthropic API key

```powershell
az keyvault secret set --vault-name adisc-dev-kv --name anthropic-api-key --value <key>
```

## CI/CD deploy

`.github/workflows/infra-deploy.yml` is a manual-dispatch workflow but is **not wired up yet** — it
needs OIDC federated credentials (`AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`) and
a `POSTGRES_ADMIN_PASSWORD` secret configured on the repo before it will run. Until then, infra
changes are deployed locally with the command above.
