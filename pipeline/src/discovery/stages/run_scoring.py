"""Stage 3 orchestrator (§4): loads survivors with extracted signals, runs the Haiku
pre-screen then the Sonnet deep pass over the Batch API, enforces §9 hard rules on
every Sonnet result, and persists both stages to `scores`. Logs one `runs` row.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from typing import Any

import anthropic
import psycopg
from psycopg.types.json import Json

from discovery.stages.filter import get_active_icp_config
from discovery.stages.score import (
    DEFAULT_HAIKU_CUT_N,
    assemble_alignment,
    build_haiku_request,
    build_sonnet_request,
    clamp_pre_score,
    compute_capacity,
    compute_priority_tier_metro,
    compute_soft_flags,
    enforce_hard_rules,
    run_batch,
)

logger = logging.getLogger(__name__)


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
    eins: list[str],
    database_url: str | None = None,
    haiku_cut_n: int | None = None,
) -> dict[str, Any]:
    database_url = database_url or os.environ["DATABASE_URL"]
    anthropic_client = anthropic.Anthropic()

    started_at = datetime.now(UTC)
    counts: dict[str, Any] = {}
    status = "success"
    error: str | None = None
    run_id: int | None = None

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

    try:
        with psycopg.connect(database_url) as conn:
            config = get_active_icp_config(conn, client_id)
            with conn.cursor() as cur:
                cur.execute("SELECT version FROM icp_configs WHERE client_id = %s AND active = true", (client_id,))
                version_row = cur.fetchone()
                assert version_row is not None
                icp_version = version_row[0]

            orgs = _load_scoreable_orgs(conn, eins)
        counts["scoreable"] = len(orgs)
        logger.info("Stage 3: %d scoreable orgs (of %d requested)", len(orgs), len(eins))

        alignment_keywords = config.get("alignment_keywords", [])
        priority_metros = config.get("geography_tiers", {}).get("priority_metros", [])
        govt_funding_heavy_pct = config.get("govt_funding_heavy_pct", 0.40)

        # --- Haiku pass ---
        haiku_requests = [build_haiku_request(ein, org, alignment_keywords) for ein, org in orgs.items()]
        haiku_results = run_batch(anthropic_client, haiku_requests)
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
        cut_n = haiku_cut_n or config.get("haiku_cut_n", DEFAULT_HAIKU_CUT_N)
        candidates = [
            (ein, haiku_results[ein]["pre_score"])
            for ein in orgs
            if haiku_results.get(ein) is not None and not haiku_results[ein]["obvious_disqualifier"]
        ]
        candidates.sort(key=lambda pair: pair[1], reverse=True)
        sonnet_eins = [ein for ein, _ in candidates[:cut_n]]
        counts["sonnet_candidates"] = len(sonnet_eins)

        # --- Sonnet pass ---
        sonnet_requests = [build_sonnet_request(ein, orgs[ein]) for ein in sonnet_eins]
        sonnet_results = run_batch(anthropic_client, sonnet_requests)
        counts["sonnet_succeeded"] = sum(1 for v in sonnet_results.values() if v is not None)
        counts["sonnet_failed"] = sum(1 for v in sonnet_results.values() if v is None)
        counts["hard_rule_violations"] = 0

        with psycopg.connect(database_url) as conn:
            for ein, result in sonnet_results.items():
                if result is None:
                    continue
                org = orgs[ein]
                values_signals, model_criteria, violations = enforce_hard_rules(
                    result["values_signals"], result["alignment_criteria"]
                )
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
                        "dq_reason": result["dq_reason"],
                        "soft_flags": compute_soft_flags(org.get("gov_funding_pct"), govt_funding_heavy_pct),
                        "scored_at": datetime.now(UTC),
                    },
                )
    except Exception as exc:
        status = "failed"
        error = str(exc)
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

    return counts
