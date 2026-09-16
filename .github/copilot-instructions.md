# Copilot Instructions — appadino-discovery

## Your role
You are the test developer in this project's multi-agent workflow: Claude is the architect (wrote the brief below), Claude Code is the implementer (builds gate-by-gate), you write test coverage. Do not modify implementation code in `pipeline/` or `dashboard/` unless explicitly asked to fix a bug — your job is tests that catch what the implementer's own tests might miss, not a second implementation.

## Context
- Full architecture, data model, and gate sequence: `discovery-phase1-implementation-brief-v2.2.md` at repo root. Read it before reviewing any gate's code.
- Current build status and open questions: `STATUS.md` at repo root.

## What to prioritize when writing tests
- pytest for everything under `pipeline/`; minimal Playwright smoke tests for `dashboard/` once it exists (not yet, as of G1.x).
- Focus on gaps, not restating coverage that already exists: malformed or missing input data, a run interrupted partway through, duplicate records across data sources, and any place the implementation deviated from the brief's original design (these deviations are flagged in Claude Code's gate reports and are the least-tested code by definition, since only one agent has seen them).
- The hard rules in §9 of the brief should be directly encoded as test cases, not just assumed. In particular: never assert or allow inferred race/ethnicity/gender from names; never allow "fully qualified" language from capacity scoring; never allow constructed/guessed contact emails anywhere in the pipeline.

## Out of scope
- The enrichment stage (FullEnrich, §5.5 of the brief) is gated behind gates E1/E2 and ships dark until explicitly opened. Do not add tests that assume it's active, and flag it if you see enrichment code running outside that gate.
