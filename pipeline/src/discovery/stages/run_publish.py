"""Stage 4-6 + QA orchestrator (§4, §7): suppress, detect triggers, publish
prospects, sample and verify claims. Logs one `runs` row (stage='publish').
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Json

from discovery.stages.filter import get_active_icp_config
from discovery.stages.publish import (
    compute_gap_rank,
    export_csv,
    load_filing_pair,
    load_publishable,
    update_score_gap_rank,
    upsert_prospect,
)
from discovery.stages.qa import compute_mismatch_rate, extract_claims, sample_claims, verify_claim
from discovery.stages.suppress import apply_suppression
from discovery.stages.triggers import detect_triggers, resolve_angle, select_primary_trigger

logger = logging.getLogger(__name__)

QA_SAMPLE_SIZE = 20
QA_MISMATCH_PAGE_THRESHOLD = 0.10


def _insert_qa_samples(conn: psycopg.Connection, run_id: int, sampled: list[dict[str, Any]]) -> list[str]:
    verdicts = []
    with conn.cursor() as cur:
        for claim in sampled:
            cur.execute(
                """
                INSERT INTO qa_samples (run_id, ein, claim, citation, verdict, checked_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (run_id, claim["ein"], claim["claim"], claim["citation"], claim["verdict"], datetime.now(UTC)),
            )
            verdicts.append(claim["verdict"])
    conn.commit()
    return verdicts


def run_publish(
    client_id: int,
    database_url: str | None = None,
    csv_path: Path | None = None,
) -> dict[str, Any]:
    database_url = database_url or os.environ["DATABASE_URL"]
    csv_path = csv_path or Path("output") / f"prospects_client{client_id}.csv"

    started_at = datetime.now(UTC)
    counts: dict[str, Any] = {}
    status = "success"
    error: str | None = None
    run_id: int | None = None

    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO runs (client_id, stage, started_at, status) "
                "VALUES (%s, 'publish', %s, 'running') RETURNING id",
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

            revenue_floor = config.get("recall_filter", {}).get("revenue_floor", 500_000)
            trigger_angles = config.get("trigger_angles", {})
            gap_rank_weights = config.get("signal_weights") or None

            publishable = load_publishable(conn, client_id, icp_version)
            counts["scored_not_disqualified"] = len(publishable)

            eins = [p["ein"] for p in publishable]
            remaining_eins, fuzzy_flags = apply_suppression(conn, client_id, eins)
            counts["ein_suppressed"] = len(eins) - len(remaining_eins)
            counts["fuzzy_flagged"] = len(fuzzy_flags)

            by_ein = {p["ein"]: p for p in publishable}
            csv_rows: list[dict[str, Any]] = []
            trigger_type_counts: dict[str, int] = {}
            all_claims: list[dict[str, Any]] = []

            with conn.cursor() as cur:
                cur.execute("SELECT ein, name, city, state FROM organizations WHERE ein = ANY(%s)", (remaining_eins,))
                org_info = {r[0]: {"name": r[1], "city": r[2], "state": r[3]} for r in cur.fetchall()}

            for ein in remaining_eins:
                score = by_ein[ein]
                current_filing, previous_filing = load_filing_pair(conn, ein)
                detected = detect_triggers(current_filing, previous_filing, revenue_floor)
                primary = select_primary_trigger(detected)
                assigned_trigger = primary["type"] if primary else None
                trigger_angle = resolve_angle(assigned_trigger, trigger_angles) if assigned_trigger else None
                if assigned_trigger:
                    trigger_type_counts[assigned_trigger] = trigger_type_counts.get(assigned_trigger, 0) + 1

                capacity = score.get("capacity") or {}
                gap_rank = compute_gap_rank(
                    criteria_met_count=(score.get("alignment") or {}).get("criteria_met_count", 0),
                    dd_present=capacity.get("dd_present"),
                    has_trigger=assigned_trigger is not None,
                    weights=gap_rank_weights,
                )

                suppression_flag = (
                    f"Possible suppression match: '{fuzzy_flags[ein]}' (fuzzy, not auto-excluded)"
                    if ein in fuzzy_flags
                    else None
                )

                upsert_prospect(
                    conn,
                    {
                        "client_id": client_id,
                        "ein": ein,
                        "assigned_trigger": assigned_trigger,
                        "trigger_angle": trigger_angle,
                        "trigger_evidence": Json(detected) if detected else None,
                        "gap_rank": gap_rank,
                        "suppression_flag": suppression_flag,
                        "updated_at": datetime.now(UTC),
                    },
                )
                update_score_gap_rank(conn, client_id, ein, icp_version, gap_rank)

                info = org_info.get(ein, {})
                alignment = score.get("alignment") or {}
                csv_rows.append(
                    {
                        "ein": ein,
                        "name": info.get("name"),
                        "city": info.get("city"),
                        "state": info.get("state"),
                        "gap_rank": gap_rank,
                        "criteria_met_count": alignment.get("criteria_met_count"),
                        "qualifies": alignment.get("qualifies"),
                        "assigned_trigger": assigned_trigger,
                        "trigger_angle": trigger_angle,
                        "dd_present": capacity.get("dd_present"),
                        "fundraising_spend_ratio": capacity.get("fundraising_spend_ratio"),
                        "suppression_flag": fuzzy_flags.get(ein, ""),
                    }
                )

            counts["published"] = len(csv_rows)
            counts["triggers"] = trigger_type_counts
            export_csv(sorted(csv_rows, key=lambda r: r["gap_rank"] or 0, reverse=True), csv_path)
            counts["csv_path"] = str(csv_path)

            # --- QA (§7) ---
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT ein, values_signals, alignment FROM scores "
                    "WHERE client_id = %s AND icp_version = %s AND stage = 'sonnet'",
                    (client_id, icp_version),
                )
                for ein, values_signals, alignment in cur.fetchall():
                    all_claims.extend(extract_claims(ein, values_signals, alignment))

            sampled_claims = sample_claims(all_claims, QA_SAMPLE_SIZE)
            org_contexts: dict[str, dict[str, Any]] = {}
            for claim in sampled_claims:
                ein = claim["ein"]
                if ein not in org_contexts:
                    current_filing, _ = load_filing_pair(conn, ein)
                    with conn.cursor() as cur:
                        cur.execute("SELECT signal FROM signals WHERE ein = %s ORDER BY tax_year DESC LIMIT 1", (ein,))
                        sig_row = cur.fetchone()
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT mission_text, program_text, significant_change_ind FROM filings "
                            "WHERE ein = %s AND extracted_at IS NOT NULL ORDER BY tax_year DESC LIMIT 1",
                            (ein,),
                        )
                        text_row = cur.fetchone()
                    signal = sig_row[0] if sig_row and sig_row[0] else {}
                    org_contexts[ein] = {
                        **signal,
                        # govt_pct/program_pct/contributions_pct live nested inside
                        # revenue_composition (see compute_signals) — Sonnet's
                        # citations reference them by their nested name, so they need
                        # to be flattened here too, not just gov_funding_pct.
                        **(signal.get("revenue_composition") or {}),
                        "mission_text": text_row[0] if text_row else None,
                        "program_text": text_row[1] if text_row else None,
                        # significant_change_ind lives on filings, not in the
                        # computed `signals` JSONB (compute_signals doesn't produce
                        # it) — pulled separately so QA can actually verify it.
                        "significant_change_ind": text_row[2] if text_row else None,
                    }
                claim["verdict"] = verify_claim(claim["citation"], org_contexts[ein])

            verdicts = _insert_qa_samples(conn, run_id, sampled_claims)
            mismatch_rate = compute_mismatch_rate(verdicts)
            counts["qa_sample_size"] = len(sampled_claims)
            counts["qa_mismatch_rate"] = mismatch_rate
            if mismatch_rate is not None and mismatch_rate > QA_MISMATCH_PAGE_THRESHOLD:
                logger.error(
                    "QA_MISMATCH_RATE_EXCEEDED client_id=%s rate=%.4f threshold=%.2f — needs human review",
                    client_id,
                    mismatch_rate,
                    QA_MISMATCH_PAGE_THRESHOLD,
                )
    except Exception as exc:
        status = "failed"
        error = str(exc)
        logger.exception("RUN_FAILED stage=publish client_id=%s error=%s", client_id, error)
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

    if status == "success" and counts.get("scored_not_disqualified", 0) > 0 and counts.get("published") == 0:
        logger.warning("ZERO_OUTPUT_RUN stage=publish client_id=%s counts=%s", client_id, counts)

    return counts
