"""Stage 3 orchestrator (§4): loads survivors with extracted signals, runs the Haiku
pre-screen then the Sonnet deep pass over the Batch API, enforces §9 hard rules on
every Sonnet result, and persists both stages to `scores`. Logs one `runs` row.

Each pass is split into Message Batches under the API's per-batch limits (see
score.chunk_requests), and every batch id is written to this run's
`runs.counts["batches"]` the moment it is submitted. A run that dies after paying
for batches is resumed with `score <client_id> --resume-run <run_id>`, which
collects the recorded batches instead of resubmitting them; a plain `score` refuses
to start while such a run exists (UnfinishedScoringRunError).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import anthropic
import psycopg
from anthropic.types.messages.batch_create_params import Request
from psycopg.types.json import Json

from discovery.stages.filter import get_active_icp_config, select_survivor_eins
from discovery.stages.score import (
    BATCH_MAX_BYTES,
    BATCH_MAX_REQUESTS,
    DEFAULT_HAIKU_CUT_N,
    assemble_alignment,
    build_haiku_request,
    build_sonnet_request,
    chunk_requests,
    clamp_pre_score,
    collect_batch,
    compute_capacity,
    compute_priority_tier_metro,
    compute_soft_flags,
    enforce_hard_rules,
    redact_dq_reason,
    submit_batch,
)

logger = logging.getLogger(__name__)

# Message Batch results are retrievable for 29 days after creation. An unfinished run
# older than that can't be resumed (its results are gone), so it no longer blocks a
# new run.
BATCH_RESULTS_RETENTION_DAYS = 29


class UnfinishedScoringRunError(RuntimeError):
    """A plain `score` found an earlier run for the same client that submitted paid
    batches and never finished. Starting a new run would pay for those orgs again."""


class ResumeRunError(ValueError):
    """`--resume-run` named a run that can't be resumed."""


def _find_unfinished_scoring_runs(conn: psycopg.Connection, client_id: int) -> list[tuple[Any, ...]]:
    """Scoring runs for this client that recorded at least one submitted batch, never
    reached 'success' (status 'running' after a kill, or 'failed'), and are still
    inside the batch results retention window."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, status, started_at FROM runs
            WHERE client_id = %s AND stage = 'scoring' AND status IN ('running', 'failed')
                AND counts ? 'batches'
                AND started_at > now() - make_interval(days => %s)
            ORDER BY id
            """,
            (client_id, BATCH_RESULTS_RETENTION_DAYS),
        )
        return list(cur.fetchall())


def _load_run_for_resume(conn: psycopg.Connection, run_id: int, client_id: int) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute("SELECT client_id, stage, status, counts FROM runs WHERE id = %s", (run_id,))
        row = cur.fetchone()
    if row is None:
        raise ResumeRunError(f"run {run_id} does not exist")
    run_client_id, stage, status, counts = row
    if run_client_id != client_id or stage != "scoring":
        raise ResumeRunError(f"run {run_id} is a {stage!r} run for client_id={run_client_id}, not scoring for client_id={client_id}")
    if status == "success":
        raise ResumeRunError(f"run {run_id} already finished successfully; nothing to resume")
    if not counts or "icp_version" not in counts:
        raise ResumeRunError(f"run {run_id} recorded no batches; start a new run instead")
    return dict(counts)


def _persist_counts(database_url: str, run_id: int, counts: dict[str, Any]) -> None:
    """Writes counts (including counts['batches']) to the runs row on its own
    connection and commits immediately, so a recorded batch id survives the process
    dying right after submit."""
    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE runs SET counts = %s WHERE id = %s", (Json(counts), run_id))
        conn.commit()


def _run_batched_pass(
    client: anthropic.Anthropic,
    pass_name: str,
    requests: list[Request],
    counts: dict[str, Any],
    persist: Callable[[], None],
    poll_interval: float,
) -> dict[str, Any]:
    """Runs one scoring pass ('haiku' or 'sonnet') as chunked Message Batches and
    returns {custom_id: parsed_json | None} for every request.

    Batches already recorded under counts['batches'][pass_name] (a resumed run) are
    collected first; every custom_id in their results counts as already paid for,
    including errored items, and is never resubmitted. Only requests not covered by
    a recorded batch are chunked and submitted, each batch id recorded and persisted
    right after its submit, then all new batches are collected.
    """
    recorded: list[str] = counts.setdefault("batches", {}).setdefault(pass_name, [])
    wanted = {request["custom_id"] for request in requests}
    results: dict[str, Any] = {}
    for batch_id in list(recorded):
        logger.info("collecting recorded %s batch %s", pass_name, batch_id)
        for custom_id, value in collect_batch(client, batch_id, poll_interval).items():
            if custom_id in wanted:
                results[custom_id] = value

    remaining = [request for request in requests if request["custom_id"] not in results]
    submitted: list[str] = []
    for chunk in chunk_requests(remaining, BATCH_MAX_REQUESTS, BATCH_MAX_BYTES):
        batch_id = submit_batch(client, chunk)
        recorded.append(batch_id)
        submitted.append(batch_id)
        persist()
        logger.info("recorded %s batch %s (%d of this pass's batches)", pass_name, batch_id, len(recorded))
    for batch_id in submitted:
        results.update(collect_batch(client, batch_id, poll_interval))
    return results


def _load_scoreable_orgs(conn: psycopg.Connection, eins: list[str]) -> dict[str, dict[str, Any]]:
    """One row per EIN: organization fields + its most recent extracted filing's text
    + its most recent computed signal. Only EINs with both are scoreable."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT o.ein, o.name, o.city, o.state, o.ruling_year,
                   f.mission_text, f.program_text, f.significant_change_ind, f.tax_year,
                   s.signal
            FROM organizations o
            JOIN LATERAL (
                SELECT mission_text, program_text, significant_change_ind, tax_year
                FROM filings
                WHERE ein = o.ein AND extracted_at IS NOT NULL
                ORDER BY tax_year DESC
                LIMIT 1
            ) f ON true
            JOIN LATERAL (
                SELECT signal
                FROM signals
                WHERE ein = o.ein
                ORDER BY tax_year DESC
                LIMIT 1
            ) s ON true
            WHERE o.ein = ANY(%s)
            """,
            (eins,),
        )
        rows = cur.fetchall()

    orgs: dict[str, dict[str, Any]] = {}
    for ein, name, city, state, ruling_year, mission_text, program_text, significant_change_ind, tax_year, signal in rows:
        orgs[ein] = {
            "name": name,
            "city": city,
            "state": state,
            "ruling_year": ruling_year,
            "mission_text": mission_text,
            "program_text": program_text,
            "significant_change_ind": significant_change_ind,
            "tax_year": tax_year,
            **(signal or {}),
        }
    return orgs


def _insert_score(conn: psycopg.Connection, row: dict[str, Any]) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO scores
                (client_id, ein, icp_version, stage, values_signals, alignment, capacity,
                 gap_rank, disqualified, dq_reason, soft_flags, scored_at)
            VALUES
                (%(client_id)s, %(ein)s, %(icp_version)s, %(stage)s, %(values_signals)s,
                 %(alignment)s, %(capacity)s, NULL, %(disqualified)s, %(dq_reason)s,
                 %(soft_flags)s, %(scored_at)s)
            ON CONFLICT (client_id, ein, icp_version, stage) DO UPDATE SET
                values_signals = EXCLUDED.values_signals,
                alignment = EXCLUDED.alignment,
                capacity = EXCLUDED.capacity,
                disqualified = EXCLUDED.disqualified,
                dq_reason = EXCLUDED.dq_reason,
                soft_flags = EXCLUDED.soft_flags,
                scored_at = EXCLUDED.scored_at
            """,
            {
                **row,
                "values_signals": Json(row["values_signals"]) if row["values_signals"] is not None else None,
                "alignment": Json(row["alignment"]) if row["alignment"] is not None else None,
                "capacity": Json(row["capacity"]) if row["capacity"] is not None else None,
                "soft_flags": Json(row["soft_flags"]) if row["soft_flags"] is not None else None,
            },
        )
    conn.commit()


def run_scoring(
    client_id: int,
    eins: list[str] | None,
    database_url: str | None = None,
    haiku_cut_n: int | None = None,
    eins_overridden: bool = False,
    resume_run_id: int | None = None,
    poll_interval: float = 10.0,
) -> dict[str, Any]:
    """`eins_overridden=True` means `eins` came from `score --eins` instead of
    select_survivor_eins — recorded in runs.counts (same key as filter's override) and
    logged, so a bounded run never looks like an ordinary one in the `runs` table.

    `resume_run_id` continues that earlier run on its own runs row: `eins` must be
    None (the EINs come from the run's recorded override list, or are re-selected
    for an ordinary run), and the run's recorded ICP version and Haiku cut are used.
    Without it, raises UnfinishedScoringRunError (before creating a runs row or
    calling the API) if an unfinished run with recorded batches exists."""
    database_url = database_url or os.environ["DATABASE_URL"]
    anthropic_client = anthropic.Anthropic()

    started_at = datetime.now(UTC)
    status = "success"
    error: str | None = None
    run_id: int

    if resume_run_id is None:
        if eins is None:
            raise ValueError("eins is required unless resume_run_id is given")
        with psycopg.connect(database_url) as conn:
            unfinished = _find_unfinished_scoring_runs(conn, client_id)
        if unfinished:
            listed = ", ".join(f"run {r[0]} (status {r[1]}, started {r[2]})" for r in unfinished)
            raise UnfinishedScoringRunError(
                f"client_id={client_id} has unfinished scoring run(s) with submitted, paid batches: {listed}. "
                f"Resume with: discovery score {client_id} --resume-run {unfinished[-1][0]}. "
                "Starting a new run would pay again for every org in those batches."
            )
        # Recorded before any work so the override is visible even on a failed run.
        counts: dict[str, Any] = {"eins_overridden": eins_overridden, "eins_requested": len(eins)}
        if eins_overridden:
            counts["eins"] = list(eins)  # a resume of this run needs the exact list
            logger.warning(
                "OVERRIDE (--eins): select_survivor_eins bypassed for client_id=%s — scoring %d "
                "explicitly-provided EINs instead of the client's survivor set",
                client_id,
                len(eins),
            )
        with psycopg.connect(database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO runs (client_id, stage, started_at, status) "
                    "VALUES (%s, 'scoring', %s, 'running') RETURNING id",
                    (client_id, started_at),
                )
                row = cur.fetchone()
                assert row is not None
                run_id = row[0]
            conn.commit()
    else:
        if eins is not None or haiku_cut_n is not None:
            raise ValueError("a resumed run uses its recorded EINs and Haiku cut; don't pass eins or haiku_cut_n")
        with psycopg.connect(database_url) as conn:
            counts = _load_run_for_resume(conn, resume_run_id, client_id)
            if counts.get("eins_overridden"):
                eins = list(counts["eins"])
            else:
                eins = select_survivor_eins(conn, client_id)
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE runs SET status = 'running', finished_at = NULL, error = NULL WHERE id = %s",
                    (resume_run_id,),
                )
            conn.commit()
        run_id = resume_run_id
        counts["resumed_times"] = counts.get("resumed_times", 0) + 1
        logger.warning(
            "RESUME: continuing scoring run %s for client_id=%s; recorded batches: %s",
            run_id,
            client_id,
            counts.get("batches", {}),
        )

    def persist() -> None:
        _persist_counts(database_url, run_id, counts)

    try:
        with psycopg.connect(database_url) as conn:
            config = get_active_icp_config(conn, client_id)
            with conn.cursor() as cur:
                cur.execute("SELECT version FROM icp_configs WHERE client_id = %s AND active = true", (client_id,))
                version_row = cur.fetchone()
                assert version_row is not None
                icp_version = version_row[0]
            if resume_run_id is not None and counts["icp_version"] != icp_version:
                raise ResumeRunError(
                    f"run {run_id} scored against icp_version {counts['icp_version']}, but the active "
                    f"version is now {icp_version}; its batches can't be reused"
                )
            counts["icp_version"] = icp_version
            cut_n = (
                counts["haiku_cut_n"]
                if resume_run_id is not None
                else haiku_cut_n or config.get("haiku_cut_n", DEFAULT_HAIKU_CUT_N)
            )
            counts["haiku_cut_n"] = cut_n

            orgs = _load_scoreable_orgs(conn, eins)
        counts["scoreable"] = len(orgs)
        logger.info("Stage 3: %d scoreable orgs (of %d requested)", len(orgs), len(eins))

        alignment_keywords = config.get("alignment_keywords", [])
        priority_metros = config.get("geography_tiers", {}).get("priority_metros", [])
        govt_funding_heavy_pct = config.get("govt_funding_heavy_pct", 0.40)

        # --- Haiku pass ---
        haiku_requests = [build_haiku_request(ein, org, alignment_keywords) for ein, org in orgs.items()]
        haiku_results = _run_batched_pass(
            anthropic_client, "haiku", haiku_requests, counts, persist, poll_interval
        )
        counts["haiku_succeeded"] = sum(1 for v in haiku_results.values() if v is not None)
        counts["haiku_failed"] = sum(1 for v in haiku_results.values() if v is None)

        # Same missing schema-level range guarantee as Sonnet's values_signals scores
        # (see enforce_hard_rules) — clamp here rather than null, since pre_score
        # drives the top-N cut below and must stay a usable sort key.
        counts["pre_score_out_of_range"] = 0
        for ein, result in haiku_results.items():
            if result is None:
                continue
            raw_pre_score = result["pre_score"]
            clamped = clamp_pre_score(raw_pre_score)
            if clamped != raw_pre_score:
                counts["pre_score_out_of_range"] += 1
                logger.warning("ein=%s pre_score %r out of [0,100], clamped to %r", ein, raw_pre_score, clamped)
                result["pre_score"] = clamped

        with psycopg.connect(database_url) as conn:
            for ein, result in haiku_results.items():
                if result is None:
                    continue
                _insert_score(
                    conn,
                    {
                        "client_id": client_id,
                        "ein": ein,
                        "icp_version": icp_version,
                        "stage": "haiku",
                        "values_signals": {"pre_score": result["pre_score"]},
                        "alignment": None,
                        "capacity": compute_capacity(orgs[ein].get("dd_present"), orgs[ein].get("fundraising_spend_ratio")),
                        "disqualified": result["obvious_disqualifier"],
                        "dq_reason": result["disqualifier_reason"],
                        "soft_flags": compute_soft_flags(orgs[ein].get("gov_funding_pct"), govt_funding_heavy_pct),
                        "scored_at": datetime.now(UTC),
                    },
                )

        # --- Cut to top N, excluding Haiku's obvious disqualifiers ---
        # Global across every Haiku batch: haiku_results already merges all chunks,
        # so the cut ranks all of them together and happens once.
        candidates = [
            (ein, haiku_results[ein]["pre_score"])
            for ein in orgs
            if haiku_results.get(ein) is not None and not haiku_results[ein]["obvious_disqualifier"]
        ]
        # Deterministic secondary sort key (G1.4 coverage-review Gap 5): ties at the
        # cut threshold previously depended on incidental DB/dict insertion order —
        # `_load_scoreable_orgs()` has no explicit ORDER BY. EIN is a unique,
        # stable per-org identifier, so sorting descending pre_score then ascending
        # EIN makes the top-N cut fully deterministic regardless of load order —
        # re-runs on the same data always produce the same Sonnet candidate set.
        candidates.sort(key=lambda pair: (-pair[1], pair[0]))
        sonnet_eins = [ein for ein, _ in candidates[:cut_n]]
        counts["sonnet_candidates"] = len(sonnet_eins)

        # --- Sonnet pass ---
        sonnet_requests = [build_sonnet_request(ein, orgs[ein]) for ein in sonnet_eins]
        sonnet_results = _run_batched_pass(
            anthropic_client, "sonnet", sonnet_requests, counts, persist, poll_interval
        )
        counts["sonnet_succeeded"] = sum(1 for v in sonnet_results.values() if v is not None)
        counts["sonnet_failed"] = sum(1 for v in sonnet_results.values() if v is None)
        counts["hard_rule_violations"] = 0

        counts["sonnet_persisted_as_failed"] = 0
        with psycopg.connect(database_url) as conn:
            for ein, result in sonnet_results.items():
                org = orgs[ein]
                if result is None:
                    # G1.4 coverage-review Gap 6: a batch item that errored/expired/
                    # was canceled (see run_batch's docstring) previously left no row
                    # at all for this (client_id, ein, icp_version, stage='sonnet')
                    # key — indistinguishable from "never selected for Sonnet in the
                    # first place" without cross-referencing this run's aggregate
                    # counts/logs. A placeholder row makes the failure durable and
                    # queryable per-EIN (values_signals/alignment NULL, distinct from
                    # every successful row); load_publishable() excludes alignment IS
                    # NULL rows so it's never mistaken for a publishable score, and
                    # re-scoring this EIN later overwrites it via the existing
                    # ON CONFLICT upsert once the item succeeds — recoverable, not a
                    # dead end.
                    counts["sonnet_persisted_as_failed"] += 1
                    _insert_score(
                        conn,
                        {
                            "client_id": client_id,
                            "ein": ein,
                            "icp_version": icp_version,
                            "stage": "sonnet",
                            "values_signals": None,
                            "alignment": None,
                            "capacity": compute_capacity(org.get("dd_present"), org.get("fundraising_spend_ratio")),
                            "disqualified": False,
                            "dq_reason": None,
                            "soft_flags": compute_soft_flags(org.get("gov_funding_pct"), govt_funding_heavy_pct),
                            "scored_at": datetime.now(UTC),
                        },
                    )
                    continue

                values_signals, model_criteria, violations = enforce_hard_rules(
                    result["values_signals"], result["alignment_criteria"], org_context=org
                )
                dq_reason, dq_redacted = redact_dq_reason(result["dq_reason"])
                if dq_redacted:
                    violations.append("dq_reason: forbidden phrase in disqualification reason, redacted")
                counts["hard_rule_violations"] += len(violations)
                if violations:
                    logger.warning("hard-rule violations for ein=%s: %s", ein, violations)

                priority_tier_metro = compute_priority_tier_metro(org.get("city"), priority_metros)
                alignment = assemble_alignment(priority_tier_metro, model_criteria)

                _insert_score(
                    conn,
                    {
                        "client_id": client_id,
                        "ein": ein,
                        "icp_version": icp_version,
                        "stage": "sonnet",
                        "values_signals": values_signals,
                        "alignment": alignment,
                        "capacity": compute_capacity(org.get("dd_present"), org.get("fundraising_spend_ratio")),
                        "disqualified": result["disqualified"],
                        "dq_reason": dq_reason,
                        "soft_flags": compute_soft_flags(org.get("gov_funding_pct"), govt_funding_heavy_pct),
                        "scored_at": datetime.now(UTC),
                    },
                )
    except Exception as exc:
        status = "failed"
        error = str(exc)
        logger.exception("RUN_FAILED stage=scoring client_id=%s error=%s", client_id, error)
        raise
    finally:
        finished_at = datetime.now(UTC)
        with psycopg.connect(database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE runs SET finished_at = %s, status = %s, counts = %s, error = %s WHERE id = %s",
                    (finished_at, status, Json(counts), error, run_id),
                )
            conn.commit()

    if status == "success" and counts.get("scoreable", 0) > 0 and counts.get("sonnet_succeeded") == 0:
        logger.warning("ZERO_OUTPUT_RUN stage=scoring client_id=%s counts=%s", client_id, counts)

    return counts
