# Appadino Discovery — Phase 1 Implementation Brief (v2.2)

**Project:** Nonprofit prospect discovery pipeline (working name: **Discovery** / DiscoveryAI)
**Repo:** `appadino-discovery` (private)
**Roles:** Claude = lead architect (this brief) · Claude Code = implementer · Copilot = test developer · Duan = reviewer at every gate
**Cadence:** Sequence-gated, not calendar-gated. Work happens in irregular sessions; gates advance when reviewed and approved, never on a date.
**Design partner:** ARCHITECT Philanthropic Collective (Track B — consulting-client discovery). First revenue already collected ($500 POC/discovery, paid).

**Changelog (v2.0 → v2.1):** FullEnrich reseller terms confirmed by Hugo (FullEnrich) — Reseller Agreement is the access path (not self-serve Pro); per-tenant `fullenrich_subaccount_id` added for the `Sub-Account-Id` API header; 90-day retention field added to `enrichments`; nonprofit/advocacy vertical confirmed in scope, consumer political/voter data confirmed out of scope. Scoping call rescheduled from Sept 10 to **Wednesday, Sept 16, 2026**.

**Changelog (v2.1 → v2.2):** Switched the FullEnrich starter tier from the $250/mo annual-commit plan (10,000 credits/month, ~$3,000/year obligation) to the **$500 one-time 12,500-credit pack** (6-month validity, no recurring commitment) — sized to current single-client volume, and avoids locking in a 12-month spend before gate E1 has validated real match rates and cost-per-contact.

---

## 1. What this is

A multi-tenant pipeline that ingests the national IRS nonprofit universe, applies a client's ICP as *configuration*, scores survivors with the Claude API, suppresses known contacts, optionally enriches approved prospects with verified contact data (candidate stage, pending validation — see §5.5), and produces a human-reviewable prospect list with citations. A web dashboard for review ships after the pipeline; an automated intake flow after that. Outreach is **always human-gated** — this system never sends anything autonomously.

**Non-goals for Phase 1:** auth/billing/self-serve signup, CRM integrations, autonomous outreach, Track A/Track C tooling, mobile.

**Prime directives:**
1. **Absentee operation is a feature of the MVP**, not a nice-to-have: scheduled runs, failure alerts, config changes without deploys, automated QA. The operator will have limited, irregular hours indefinitely — every manual step is a permanent tax.
2. **Resumability.** Build sessions are irregular and may be days apart. Every gate closes with the repo in a clean, documented state: passing tests, an updated `STATUS.md` (what's done, what's next, open questions), and no half-finished work on `main`. Any session must be able to start cold from `STATUS.md` in under five minutes.

---

## 2. Architecture

```
                        ┌──────────────────────────────────────────┐
                        │  Azure Container Apps Job (scheduled)    │
  IRS BMF (monthly CSV) │  "pipeline" — Python 3.12                │
  990 e-file XML  ─────▶│  ingest → filter → signals → AI score →  │
  ProPublica API        │  suppress → triggers → publish           │
                        │  (→ enrich: candidate stage, flag-gated) │
                        └───────────────┬──────────────────────────┘
                                        │
                         Azure Database for PostgreSQL
                         Flexible Server (Burstable B1ms)
                                        │
                        ┌───────────────┴──────────────────────────┐
                        │  Azure Static Web Apps (React)           │
                        │  + built-in Functions API (Python)       │
                        │  review table · statuses · CSV export    │
                        └──────────────────────────────────────────┘

  Secrets: Azure Key Vault (Anthropic key, DB conn string,
           FullEnrich key if E-gates pass)
  Telemetry/alerts: Application Insights → email alert rules
  Scoring: Anthropic API — Haiku cheap pass, Sonnet deep pass,
           via the Message Batches API (50% discount, fits overnight runs)
```

**Why these choices**
- **Container Apps Job** over Functions for the pipeline: batch work with unbounded runtime, cron-scheduled, scales to zero, no 10-minute ceiling. One container, one entrypoint per stage.
- **PostgreSQL Flexible Server (B1ms)**: real SQL for the staged filters, JSONB for ICP configs and extracted signals, ~$15/mo. SQLite would be cheaper but kills the multi-tenant story and concurrent dashboard access.
- **Static Web Apps**: Duan's proven pattern (portfolio site, AlphaBot). Free tier fine for Phase 1.
- **Batch API for scoring**: runs are overnight anyway; 50% off makes national-scale scoring a non-event cost-wise.

**Estimated run cost:** Postgres ~$15/mo + Container Apps ~$2–5/mo + Claude API ~$10–40/full national run (see §6 token math) + SWA free + FullEnrich (one-time $500/12,500-credit pack, 6-month validity, chosen reseller starter tier — only if E-gates pass, only on approved prospects; see §5.5). Well under the $399/mo Discovery price point, even fully loaded.

---

## 3. Multi-tenant data model

`client_id` on every tenant-scoped table from row one. ICP is **data, not code** — onboarding customer #2 is an insert, not a deploy.

```
clients            id, name, status, created_at
icp_configs        id, client_id, version, config JSONB, active bool, created_at
                   -- full ICP: geography tiers, revenue band, excludes,
                   -- disqualifiers, signal weights, alignment keywords,
                   -- enrichment_enabled bool (default false),
                   -- fullenrich_subaccount_id text NULL
                   -- (set when enrichment_enabled = true; passed as the
                   -- Sub-Account-Id header on FullEnrich v2 calls)
organizations      ein PK, name, state, city, ntee, ruling_year,
                   revenue_latest, foundation_code, bmf_updated_at
                   -- SHARED national universe (not tenant-scoped)
filings            id, ein, tax_year, form_type, xml_object_url,
                   revenue_total, contributions, program_revenue,
                   govt_grants, fundraising_expense, officers JSONB,
                   extracted_at
signals            id, ein, tax_year, signal JSONB
                   -- gov_funding_pct, revenue_composition, dd_present,
                   -- fundraising_spend_ratio, org_age, revenue_trend
scores             id, client_id, ein, icp_version, stage (haiku|sonnet),
                   values_signals JSONB (5 separate scores + citations),
                   alignment JSONB (criteria hit, 3-of-7),
                   capacity JSONB (2 public criteria ONLY),
                   gap_rank numeric, disqualified bool, dq_reason,
                   soft_flags JSONB, scored_at
suppression        id, client_id, ein NULL, org_name, kind
                   (client|active_prospect|partner_attribution), source, added_at
prospects          id, client_id, ein, status
                   (new|reviewed|approved|rejected|contacted|responded),
                   assigned_trigger, notes, updated_by, updated_at
enrichments        id, client_id, ein, prospect_id, provider (fullenrich),
                   contact_name, contact_title, email, email_status
                   (verified|catch_all|not_found), phone, linkedin_url,
                   provider_confidence, raw JSONB, credits_spent,
                   requested_at, completed_at, retention_expires_at
                   -- populated ONLY for status=approved prospects,
                   -- ONLY when icp_configs.enrichment_enabled = true
                   -- retention_expires_at = completed_at + 90 days
                   -- (FullEnrich reseller terms); dashboard flags expired rows
runs               id, client_id NULL, stage, started_at, finished_at,
                   status, counts JSONB, error
qa_samples         id, run_id, ein, claim, citation_url, verdict, checked_at
```

---

## 4. Pipeline stages (all technical risk lives here)

**Stage 0 — Ingest (national, shared)**
- Pull IRS Business Master File (all-states CSV), upsert into `organizations`. Monthly schedule.
- ProPublica Nonprofit Explorer as the per-EIN detail/backfill source (free, no key).
- 990 e-file XML index: fetch and store object URLs for target years (latest 2 filings per org).

**Stage 1 — Cheap SQL recall filter (no AI, no per-org cost)**
Wide net; err toward recall. From config, not code:
- 501(c)(3) public charities only (`foundation_code` — exclude private foundations)
- Revenue ≥ **$500K hard floor** (Lauren; watch list retired), ≤ configurable ceiling (default $10M)
- Hard excludes: hospitals/health systems, universities, government/quasi-public, fiscally sponsored (no own 990), membership associations — implemented via foundation/affiliation codes + NTEE **for recall shaping only, never scoring** (Lauren: characteristics decide, sectors emerge)
- Geography: **national**. Tier weights (Charlotte metro, Cincinnati metro = priority) applied at ranking, not as walls.

**Stage 2 — 990 signal extraction (survivors only)**
Parse latest 2 XML filings per survivor:
- Revenue composition (contributions vs program vs govt) — **rank on gap**, composition over size
- `govt_grants / total_revenue` → **soft flag** when heavy (federal awards can't fund fundraising consulting; threshold in config, default ≥40%)
- Development-capacity readables: fundraising expense ratio, officer/staff titles containing development roles (Part VII)
- Revenue trend across the 2 filings (growth/decline/transformational jump)
- **Capacity scoring uses ONLY the 2 publicly readable criteria. Never emit "fully qualified" — the other 3 capacity criteria are unknowable from public data. Output language: "qualified pending discovery conversation."**

**Stage 3 — AI scoring, two passes (Batch API)**
- **Haiku pass** (all Stage-2 survivors): mission/program text from 990 + website title/description → cheap 0–100 pre-score on alignment keywords + obvious disqualifiers. Cut to top N (config, default 3,000).
- **Sonnet pass** (top N): full five-signal values scoring, **each signal scored separately with citations**:
  1. Leadership composition — **HARD RULE: never infer race/ethnicity/gender from names or photos. Cite published self-description only, or emit `needs_human_verification`.**
  2. Population served
  3. Mission language
  4. Programming
  5. Funder base (Phase 1: what's readable from 990 + website; grantmaker Schedule I trails are a Phase 2 plumbing item)
- Strategic alignment: 3-of-7 framework from the spec, criteria hit-list with citations.
- **Hard disqualifier:** primary need is grant writing → `disqualified=true`, reason recorded, never surfaced as a prospect.
- Every scored claim carries a citation URL or document reference. **A blank cell beats a wrong cell. The pipeline never constructs or guesses email addresses.**

**Stage 4 — Suppression**
- Load ARCHITECT lists (seed data, then editable via dashboard): past/current clients — Butterfly Dreamz, Project OutPour, Our Tribe Cincy, Blue Bowtie Foundation, Queen City Cocoa B.E.A.N.S., Common Cause, WEMH, She Dreams in Color, LPCCD, CCIP, Clinton Hill Community Action, CBAC. Active prospects — LBFE Cincinnati, The Partnership Fund, Spring Clean, CCT Center for Community Transitions, Sanford Institute/National University.
- Match on EIN where resolvable, fuzzy name match (flag, don't silently drop) where not.
- Partner-attribution entries carry the 18-month window metadata (12% commission context lives in ARCHITECT's world, not ours; we just track the flag).

**Stage 5 — Triggers (Phase 1 = filing-derived only)**
- Diff latest vs prior filing: new ED/CEO name in Part VII, development-director disappearance, transformational revenue jump, first filing above floor.
- Each trigger maps to an outreach *angle* (trigger→angle→offer table is a client-config asset; ships with ARCHITECT defaults: new ED → first-100-days; DD departure → Fractional/Interim DD; transformational grant → absorb-and-build; new strategic plan → fund-the-plan). **Lauren's rule: the trigger shapes the message, it doesn't just confirm timing.** Website/news-based triggers (48-hour standard) are Phase 2.

**Stage 6 — Publish**
- Upsert `prospects` (status `new`), compute `gap_rank` ordering, write run record, emit CSV to blob + dashboard.

---

## 5.5. Candidate stage — FullEnrich contact enrichment (NOT yet committed scope)

**Status: unvalidated, commercial terms confirmed.** This stage ships dark (feature flag off, no cron trigger) until the E-gates in §8 pass. It does not block, delay, or entangle any other gate. Reseller terms with FullEnrich were confirmed by Hugo (FullEnrich) on Sept 15, 2026, pending final package lock on a Friday follow-up call — the technical design below reflects those terms.

**What it would do:** for prospects a human has marked `approved` in the dashboard, call the FullEnrich API (waterfall enrichment across upstream providers) to retrieve decision-maker contact data — verified email, phone, LinkedIn — and store results in `enrichments`.

**Placement rationale — after human approval, not after scoring:**
- FullEnrich bills per enrichment credit. Enriching only approved prospects means paying for tens of contacts per run, not thousands.
- It keeps the human gate upstream of any contact-data acquisition: no contact data exists for an org until a person has decided it belongs on the list.
- Scoring stays 100% public-source and citation-backed; enrichment is a separate, clearly-labeled data class that never feeds back into scores.

**Access model — Reseller Agreement, not self-serve.** FullEnrich's standard ToS prohibits delivering enriched data to third parties (i.e., our clients) from one account. Multi-client delivery on this architecture requires their standard Reseller Agreement — fixed terms, no enterprise procurement cycle, no redlines expected. A written OK on a self-serve Pro plan is not sufficient on its own; the Reseller Agreement is the actual vehicle. This resolves the rule-3 reconciliation question below in FullEnrich's favor.

**Sub-accounts.** One Appadino parent workspace, one API key. Each client gets a FullEnrich sub-account, passed as a `Sub-Account-Id` header on v2 API calls — `icp_configs.fullenrich_subaccount_id`, set alongside `enrichment_enabled` when a tenant is onboarded to enrichment. History, cache, and logs isolate per sub-account on FullEnrich's side; billing draws from one pooled Appadino credit balance, so consolidated billing and per-client compliance isolation both hold at once. Sub-account provisioning is manual for Phase 1 (client count is low); revisit API-driven provisioning in Phase 2 if client count grows.

**Retention.** Delivered personal data carries a mandatory 90-day retention/refresh policy under the reseller terms. `enrichments.retention_expires_at` = `completed_at` + 90 days; the dashboard visually flags or grays out any contact past that window rather than treating it as permanently valid. Re-enrichment after expiry consumes a fresh credit.

**Vertical scope — confirmed by FullEnrich.** Standard B2B professional-contact use (institutional donors, foundation directors, CSR leads, officials acting in a professional capacity) is fully supported. Consumer political profiling and voter data are explicitly excluded (GDPR Art. 9) — outside this system's scope regardless, but useful as written vendor confirmation for Lauren's sign-off.

**Pricing — chosen tier.** ~$500 one-time for a 12,500-credit pack, valid 6 months, no recurring commitment. Chosen over the ~$250/mo annual-commit tier (10,000 credits/month, ~$3,000/year obligation) because it's a single bounded spend sized to current volume — one pilot client, pre-E1 — rather than a 12-month commitment made before gate E1 has validated real match rates and cost-per-contact. Revisit the recurring tier once client volume actually justifies 10,000+ credits/month; upgrading later carries no per-client renegotiation. Final package terms confirmed on a Friday follow-up call with FullEnrich.

**Rule reconciliation (needs Duan + Lauren sign-off — flag for the Wednesday Sept 16 scoping call):**
v1.0 hard rule 3 said "no data-broker sources," written to ban *guessed* emails and sketchy scrapes in the scoring layer. FullEnrich is different in kind — verified, consented-workflow B2B enrichment, now under a confirmed reseller agreement rather than a gray-area account — but it is still third-party contact data, so adopting it is a deliberate amendment, not a loophole. The amended rule (§9.3) permits enrichment-API results only when: provider and confidence are stored with every record, `email_status` ≠ verified is visually flagged in the dashboard, retention expiry is tracked and surfaced, and nothing is ever pattern-guessed. If Lauren objects to any third-party contact data for ARCHITECT's tenant, `enrichment_enabled` stays false for them — it's per-tenant config, not a platform decision.

**Validation spike (gate E1) measures, on ~25 real approved prospects:**
1. Match rate (found a decision-maker contact at all)
2. Verified-email rate vs catch-all/not-found
3. Cost per *usable* contact (credits ÷ verified emails)
4. Accuracy spot-check: Duan manually verifies 10 results
5. API ergonomics: sub-account header behavior, async webhook vs polling, rate limits, bulk endpoint fit

**Adoption threshold (tune after seeing data):** ≥60% match rate, ≥70% of matches verified, cost per usable contact comfortably inside the confirmed $500/12,500-credit package — well within the $399/mo Discovery price point even fully loaded. Below threshold → stage stays dark, revisit providers in Phase 2.

---

## 5. Dashboard

React on Static Web Apps, Functions API over Postgres:
- Prospect review table: rank, org, revenue, composition, five signal scores with expandable citations, alignment hits, soft flags, trigger
- Status workflow: new → reviewed → approved / rejected (checkbox-fast), notes field
- Suppression manager (add/remove entries)
- Outreach queue **stub**: approved prospects listed with assigned trigger/angle — drafting and sending are Phase 2, human-gated by design
- Enrichment column (renders only when `enrichment_enabled`): contact, email with status badge (including retention-expired state), provider + confidence on hover; "Enrich" action button per approved prospect (manual trigger first; batch auto-enrich only after E2 passes)
- CSV export button (Lauren's spreadsheet, one click; enrichment columns included when present)
- Run history + last-run health banner
- Multi-tenant from day one: client switcher hidden behind a config flag; single-tenant deployment

## 6. Intake + dogfood

- AlphaBot-style intake: questionnaire + document upload → Claude compiles a draft `icp_config` JSON → **Duan reviews every config before first run** (config-as-data makes this a review, not a build)
- Write the **Appadino dogfood config**: target = nonprofit/fundraising consultancies (990 Part VII contractor tables >$100K, Schedule G fundraising counsel, state charitable-solicitation registries, AFP chapter directories) → generate the customer-2/3 prospect list with the same pipeline
- Token math checkpoint: Haiku pass ~50K orgs × ~1.5K tokens ≈ $10–20 batched; Sonnet pass 3K orgs × ~4K tokens ≈ $20–40 batched. Verify actuals, tune N.

## 7. Absentee + irregular-session operation (built in from gate one, not bolted on)

- Cron schedules on the Container Apps Job (monthly BMF, weekly score refresh)
- App Insights alert rules → email on run failure, zero-output runs, cost anomalies
- All thresholds/weights/tiers in `icp_configs` — changes are DB updates, zero deploys
- **QA job:** every run samples 20 scored claims into `qa_samples`, re-fetches each citation, verdicts match/mismatch; mismatch rate >10% pages Duan. This is the automated stand-in for "Duan reads the output."
- **`STATUS.md` discipline:** Claude Code updates it as the final commit of every gate — done / next / open questions / any decision awaiting Duan. It is the session-start document. A gate is not closed while `STATUS.md` is stale.

---

## 8. Sequenced gates (no calendar attached)

Gates advance strictly in order within a track; Duan reviews and approves each before the next begins. Copilot writes tests per commit (pytest for pipeline, minimal Playwright smoke for dashboard). Sessions are irregular — a gate can span one night or two weeks, and that is fine by design.

| Gate | Scope | Acceptance |
|---|---|---|
| **G1.1** Repo + infra | Bicep for RG, Postgres, Key Vault, Container Apps env, App Insights; CI via GitHub Actions; `STATUS.md` created | infra deploys clean; secrets in KV; hello-world job runs on schedule |
| **G1.2** Ingest | BMF loader, ProPublica client, 990 XML index + fetch | national `organizations` populated; row counts logged; idempotent re-run |
| **G1.3** Filters + signals | Stage 1 SQL from config; Stage 2 XML parser | survivor counts per stage logged; spot-check 10 orgs by hand |
| **G1.4** Scoring | Haiku + Sonnet batch scoring, five-signal output, citations, DQ rules | 100-org pilot batch reviewed by Duan; zero demographic inferences; every claim cited |
| **G1.5** Suppress + triggers + publish | Stages 4–6, CSV out, QA job, alerts | ARCHITECT seed suppression verified; end-to-end national run completes unattended |
| **G2.x** Dashboard | table → statuses → suppression UI → export → run health | Lauren-usable without training; CSV matches POC format |
| **E1** FullEnrich spike | standalone script, no pipeline integration: enrich ~25 approved prospects, capture §5.5 metrics | Duan reviews metrics against adoption threshold; go/no-go recorded in `STATUS.md` |
| **E2** Enrichment stage *(only if E1 = go)* | `enrichments` table live, per-prospect Enrich button, provider adapter behind an interface (FullEnrich first, swappable), flag-gated | enrichment never fires on non-approved prospects (tested); sub-account ID passed on every call; credits logged per call; `retention_expires_at` set and surfaced in dashboard; ARCHITECT tenant flag set per Lauren's decision |
| **G3.x** Intake + dogfood | intake flow → config compiler → dogfood run | customer-2 prospect list exists; Duan approved dogfood config |
| **G4** Hardening | fixes, docs, runbook | runbook lets a low-availability operator run everything in <2 hrs/mo |

**Ordering notes:** E1 can run any time after G2.x produces approved prospects (it needs real approvals to enrich). It is deliberately parallel-safe — a standalone script — so it can be a "small session" task. E2 slots in whenever E1 passes; if E1 fails, delete both E-rows and nothing else moves.

**Wednesday Sept 16 scoping call checkpoint:** demo whatever exists at that point — no gate is pinned to it. Committed deliverable stays "scored national pipeline your team reviews." Reseller terms with FullEnrich are now confirmed, so if enrichment comes up, present it as "evaluating a verified-contact layer, commercial terms settled" — still not committed until E1 passes. Anything new from the call goes to the Phase 2 list unless it displaces something in this brief.

---

## 9. Hard rules (encode as tests, not comments)

1. Never infer race/ethnicity/gender from names or photos — published self-description or `needs_human_verification`.
2. Never emit "fully qualified." Capacity = 2 public criteria + "pending discovery conversation."
3. Never construct, guess, or pattern-generate contact emails anywhere in the system. Blank beats wrong. Third-party contact data enters *only* through the flag-gated enrichment stage (§5.5), only for approved prospects, always stored with provider + confidence + verification status + retention expiry. No scraping, no guessed patterns, no other data sources.
4. Every scored claim cites a source. Enrichment data is never a scoring input.
5. No autonomous outreach paths — the outreach queue has no send capability in Phase 1 at all.
6. NTEE never appears in scoring inputs (recall shaping only).
7. Suppressed orgs never surface, and fuzzy suppression matches surface as flags, not silent drops.
8. Enrichment never fires on a prospect whose status is not `approved`, never when the tenant's `enrichment_enabled` flag is false, and never when the tenant's `fullenrich_subaccount_id` is unset.

## 10. Risks

- **990 XML variance** (schema versions, missing parts) → parser tolerates absence; signals nullable; coverage % logged per run.
- **Filing lag 12–18 months** → fine for qualification, weak for timing; set that expectation with Lauren (already in the v3 doc).
- **Haiku cut too aggressive** → keep cut threshold in config; log score distribution; Duan reviews the boundary band on the pilot batch.
- **Irregular sessions → context loss** → mitigated by `STATUS.md` discipline, one-gate-at-a-time, and clean-main rule. If it fails anyway, the fix is smaller gates, not longer sessions.
- **Enrichment scope creep** → E-gates are the only door. No enrichment code touches the pipeline before E1 passes; no auto-enrichment before E2 acceptance.
- **Retention compliance** → FullEnrich reseller terms require refreshing delivered personal data after 90 days; `retention_expires_at` tracking and dashboard flagging (§5.5) are the enforcement mechanism, tested in E2 acceptance.
- **Lauren declines third-party contact data** → per-tenant flag makes this a config value, not a fork. Dogfood tenant can still use it.
- **Scope creep from the Sept 16 call** → anything new goes to the Phase 2 list unless it displaces something in this brief.

---

## 11. Claude Code kickoff prompt

> You are the implementer for `appadino-discovery`, working from `discovery-phase1-implementation-brief-v2.2.md` at repo root. Work gate by gate (§8), strictly in sequence, one scoped commit per gate item, conventional commit messages. Never start the next gate before I approve the current one. My sessions are irregular and may be days apart: end every gate with passing tests, a clean `main`, and an updated `STATUS.md` (done / next / open questions / decisions awaiting me) — assume the next session starts cold from that file. Python 3.12, type-hinted, pytest per module; infra as Bicep in `/infra`; config never hardcoded — everything tenant-variable lives in `icp_configs.config`. The hard rules in §9 are test cases first. The enrichment stage (§5.5, gates E1/E2) is candidate scope: write zero enrichment code unless I explicitly open gate E1. Start with G1.1: propose the repo layout and the Bicep plan, then wait for my review.

---
*Owner: Duan Walker, Appadino AI LLC · Architect of record: Claude · v2.2, September 2026 (supersedes v2.1 — FullEnrich starter tier switched to the $500/12,500-credit one-time pack)*
