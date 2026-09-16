# Status

_Session-start document. Read this first — see the implementation brief (`discovery-phase1-implementation-brief-v2.2.md`) for full context._

## Done

**G1.1 — Repo + infra** ✅ closed 2026-09-16

- Repo scaffolded: `pipeline/` (Python 3.12 package skeleton, pyproject.toml, smoke test), `dashboard/` (placeholder for G2.x), `infra/` (Bicep), `.github/workflows/` (CI + manual infra-deploy).
- Bicep deployed to Azure subscription "Pay-As-You-Go", resource group `rg-appadino-discovery-dev` (eastus2):
  - `adisc-dev-pg` — PostgreSQL Flexible Server (Burstable B1ms), `discovery` database, `AllowAzureServices` firewall rule.
  - `adisc-dev-kv` — Key Vault, RBAC-authorized. Contains `db-connection-string`. Job identity has Key Vault Secrets User; operator (Duan, object ID `053a555a-2d92-4234-a24b-54c6ad64d668`) has Key Vault Secrets Officer.
  - `adisc-dev-law` / `adisc-dev-cae` — Log Analytics + Container Apps environment.
  - `adisc-dev-ai` — Application Insights (alert rules are a G1.5 item, not yet wired).
  - `adisc-dev-pipeline-job` — Container Apps Job, monthly cron (`0 6 1 * *` UTC), user-assigned managed identity `adisc-dev-job-identity`. Currently runs a `mcr.microsoft.com/azure-cli` **hello-world placeholder** — manually triggered and confirmed `Succeeded`.
- CI (`ci.yml`) runs pytest + ruff on push/PR to `main`.
- `infra-deploy.yml` exists but is **not wired up** — no OIDC federated credentials configured yet. Deploys are run locally (see `infra/README.md`).
- Renamed the brief file from `discovery-phase1-implementation-brief-v2.1` to `-v2.2.md` to match its actual content/self-reference.

## Next

**G1.2 — Ingest**: BMF loader, ProPublica client, 990 XML index + fetch. This is also when the Container Apps Job's placeholder image gets swapped for a real pipeline image (built from `/pipeline`, pushed to a registry — need to decide ACR vs. another registry as part of that gate).

## Open questions / decisions awaiting Duan

- None blocking. G1.1 is closed; ready to start G1.2 whenever you are.

## Notes for cold-start

- Azure CLI is signed into the Pay-As-You-Go subscription used for this project; `rg-appadino-discovery-dev` is the only resource group so far.
- The Postgres admin password was generated at deploy time and is **not in the repo** — it's stored as the `postgres-admin-password` secret in `adisc-dev-kv` (alongside `db-connection-string`, which already has it baked into the connection URI).
- `anthropic-api-key` has not been added to Key Vault yet — needed before G1.4 (Scoring).
- No enrichment code exists anywhere, per §5.5/§9 — correct until gate E1 is explicitly opened.
