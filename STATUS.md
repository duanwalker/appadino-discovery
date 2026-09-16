# Status

_Session-start document. Read this first — see the implementation brief (`discovery-phase1-implementation-brief-v2.2.md`) for full context._

## Done

**G1.3 — Filters + signals** ✅ closed 2026-09-16

- New tables (migration `4539971ffa35`): `clients`, `icp_configs`, `signals`. Seed migration (`1cded851bc4e`) creates the ARCHITECT client (**id=2** in the dev DB — id 1 was consumed during a downgrade/fix cycle mid-development, see below) with an active v1 `icp_configs` row carrying the §4 Stage 1 defaults explicitly (not just relying on code fallbacks), plus placeholder keys (`signal_weights`, `alignment_keywords`, `govt_funding_heavy_pct`, `enrichment_enabled`, `fullenrich_subaccount_id`) for gates that read them later.
- **Stage 1** (`stages/filter.py`): fully config-driven SQL recall filter — `build_survivor_query()` is a pure function (unit tested) that `select_survivor_eins()` executes. Foundation-code excludes (`00,02,03,04,12,13,14`) and NTEE-prefix excludes (`B4,B5,E2,Y`) are sourced from the **authoritative IRS EO BMF FOUNDATION CODE table** (fetched and read directly from `irs.gov/pub/irs-soi/eo-info.pdf` this gate, not guessed) — codes 02-04 are private foundations, 12/13/14 are hospitals/university-benefit-orgs/government units, and 00 means "not 501(c)(3) at all," so this one BMF field implements both the "public charities only" and "hospitals/universities/government" hard excludes from §4 Stage 1 in one shot. Run against the real DB for ARCHITECT: **119,218 survivors** out of ~1.96M national orgs.
- **Stage 2** (`stages/extract_signals.py`): resolves the ZIP-archive lookup deferred from G1.2. Confirmed via AWS's own Registry of Open Data page that the old S3 per-filing dataset was deprecated Dec 31 2021; filings are only in monthly ZIP archives (`{year}_TEOS_XML_{MM}{A-D}.zip`, existence discovered via cheap HEAD requests). Uses `remotezip` to read each archive's central directory via HTTP range requests — confirmed empirically (no full downloads) — building one `object_id → (zip_url, member)` index per submission year, shared across all survivors needing that year. XML field mapping (`CYTotalRevenueAmt`, `CYContributionsGrantsAmt`, `CYProgramServiceRevenueAmt`, `GovernmentGrantsAmt`, `TotalFunctionalExpensesGrp/FundraisingAmt`, `Form990PartVIISectionAGrp` for officers) was **confirmed against real downloaded filings**, not just schema docs — including specifically searching real filings until one with `GovernmentGrantsAmt` present was found, since the first sample didn't have any. `compute_signals()` is a pure function producing `gov_funding_pct`, `revenue_composition`, `dd_present`, `fundraising_spend_ratio`, `org_age`, `revenue_trend` exactly as named in §3/§4.
- **§10 tolerance implemented for real, not just documented**: a live run hit `NotImplementedError` from Python's `zipfile` on some filings using an unsupported compression method (Deflate64) partway through — fixed by catching broadly around the fetch+parse step per filing (was only catching `ET.ParseError`) so one bad filing degrades coverage instead of crashing the run. Every run now returns a `coverage_pct` (`filings_parsed / (filings_parsed + filings_failed)`), the concrete form of "coverage % logged per run" from §10's risk mitigation.
- **Validated against real data — the "spot-check 10 orgs by hand" acceptance criterion**: ran Stage 2 on 15 real ARCHITECT survivors (Maine nonprofits — Bangor Symphony Orchestra, YMCA chapters, Boy Scouts councils, United Way, etc.). 26/30 filings parsed (coverage_pct 0.867), 15 signals computed. Manually verified: revenue compositions and ages are plausible; a `dd_present: true` result was traced to an actual "VP Development" officer title on a real filing; a `revenue_trend: "decline"` result was verified arithmetically against the two real filed revenue totals. All values checked out.
- **Not run at national scale**: Stage 2 across all 119,218 survivors would mean ~240K individual remote-zip fetches — likely tens of hours at the per-filing rate observed. Flagged as a real design tradeoff to revisit (e.g. fully downloading each needed monthly zip once instead of many small range-requests) before any full-scale run; not required by this gate's acceptance criteria, which only calls for the hand spot-check.
- `discovery.cli filter <client_id>` runs Stages 1+2 and logs one `runs` row (`stage='filter_and_signals'`) with `survivors`, `filings_parsed`, `filings_failed`, `signals_computed`, `coverage_pct`.
- 14 new unit tests (pure functions only — `build_survivor_query`, `parse_990_xml` against a real fixture filing, `compute_signals`, `_year_from_archive_url`); suite is now 51/51, ruff/mypy clean. Rebuilt and pushed `discovery-pipeline:latest` to ACR so the deployed job has this gate's code.

## Next

**G1.4 — Scoring**: Haiku + Sonnet batch scoring (`scores` table, not yet created), five-signal values scoring with citations, DQ rules, 3-of-7 alignment framework. This is also where `signal_weights`/`alignment_keywords`/`govt_funding_heavy_pct` in `icp_configs.config` get consumed for the first time (currently unused placeholders), and where `discovery.clients.propublica.get_organization()` becomes relevant as an opportunistic backfill for survivors with missing/stale BMF fields. Needs `anthropic-api-key` in Key Vault before it can run for real (see below).

## Open questions / decisions awaiting Duan

- None blocking. G1.3 is closed; ready to start G1.4 whenever you are.
- Worth a look when convenient, not blocking: the Stage 2 national-scale performance tradeoff noted above (per-filing remote-zip reads vs. bulk zip downloads) — fine for gate acceptance, but should be revisited before a real full-national Stage 2 run.

## Notes for cold-start

- Azure CLI is signed into the Pay-As-You-Go subscription used for this project; `rg-appadino-discovery-dev` is the only resource group so far.
- The Postgres admin password is stored as the `postgres-admin-password` secret in `adisc-dev-kv` (alongside `db-connection-string`, which already has it baked into the connection URI).
- Postgres firewall has an `AllowDevMachine` rule for this dev machine's IP (added during G1.2 to run migrations/ingest locally) — IP-specific, low risk, but flagging it exists in case it needs rotating or removing later.
- `anthropic-api-key` has not been added to Key Vault yet — needed before G1.4 (Scoring).
- No enrichment code exists anywhere, per §5.5/§9 — correct until gate E1 is explicitly opened.
- `discovery.clients.propublica.get_organization()` still exists but is unused — see G1.4 above.
- The ARCHITECT client's id in the dev DB is **2**, not 1 — a downgrade/re-upgrade cycle while fixing a JSON double-encoding bug in the seed migration consumed id 1. Harmless, but don't assume `client_id=1` when testing.
