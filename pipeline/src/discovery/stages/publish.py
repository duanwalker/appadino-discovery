"""Stage 6 — Publish (§4). Upserts `prospects` for every non-disqualified, Sonnet-
scored survivor that isn't EIN-suppressed, computes gap_rank, and emits a CSV.

gap_rank formula is NOT specified anywhere in the brief beyond "rank on gap,
composition over size" (§4 Stage 1) — this is a first-pass, transparent, config-
weighted formula, not an authoritative spec. Tune `signal_weights` in
icp_configs.config once real prospect review data exists to validate against.
"""

from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import Any

import psycopg

logger = logging.getLogger(__name__)

DEFAULT_GAP_RANK_WEIGHTS: dict[str, float] = {
    "alignment": 0.5,
    "capacity_gap": 0.3,
    "trigger": 0.2,
}
ALIGNMENT_MAX_CRITERIA = 6


def compute_gap_rank(
    criteria_met_count: int,
    dd_present: bool | None,
    has_trigger: bool,
    weights: dict[str, float] | None = None,
) -> float:
    """0-100, higher = higher priority. Composition over size (§4 Stage 1): revenue
    size isn't a factor here at all — alignment, development-capacity gap, and
    filing-derived timing urgency are."""
    weights = {**DEFAULT_GAP_RANK_WEIGHTS, **(weights or {})}

    alignment_score = criteria_met_count / ALIGNMENT_MAX_CRITERIA
    # No development capacity = the biggest gap (most opportunity for a fundraising
    # consultancy); unknown capacity is treated as a moderate gap, not a large one,
    # since "no evidence" isn't the same claim as "confirmed absent".
    capacity_gap_score = 1.0 if dd_present is False else (0.5 if dd_present is None else 0.0)
    trigger_score = 1.0 if has_trigger else 0.0

    score = (
        weights["alignment"] * alignment_score
        + weights["capacity_gap"] * capacity_gap_score
        + weights["trigger"] * trigger_score
    )
    return round(100 * score, 2)


def load_publishable(conn: psycopg.Connection, client_id: int, icp_version: int) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT sc.ein, sc.alignment, sc.capacity
            FROM scores sc
            WHERE sc.client_id = %s AND sc.icp_version = %s AND sc.stage = 'sonnet'
                AND sc.disqualified = false
            """,
            (client_id, icp_version),
        )
        return [{"ein": row[0], "alignment": row[1], "capacity": row[2]} for row in cur.fetchall()]


def load_filing_pair(conn: psycopg.Connection, ein: str) -> tuple[dict[str, Any], dict[str, Any] | None]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT tax_year, revenue_total, officers FROM filings "
            "WHERE ein = %s AND extracted_at IS NOT NULL ORDER BY tax_year DESC LIMIT 2",
            (ein,),
        )
        rows = cur.fetchall()
    # revenue_total comes back as Decimal (NUMERIC column) — cast to float so trigger
    # evidence built from it is JSON-serializable when written to trigger_evidence.
    filings = [
        {"tax_year": r[0], "revenue_total": float(r[1]) if r[1] is not None else None, "officers": r[2] or []}
        for r in rows
    ]
    current = filings[0] if filings else {"tax_year": None, "revenue_total": None, "officers": []}
    previous = filings[1] if len(filings) > 1 else None
    return current, previous


def upsert_prospect(conn: psycopg.Connection, row: dict[str, Any]) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO prospects
                (client_id, ein, status, assigned_trigger, trigger_angle, trigger_evidence,
                 gap_rank, notes, updated_by, updated_at)
            VALUES
                (%(client_id)s, %(ein)s, 'new', %(assigned_trigger)s, %(trigger_angle)s,
                 %(trigger_evidence)s, %(gap_rank)s, %(notes)s, 'pipeline', %(updated_at)s)
            ON CONFLICT (client_id, ein) DO UPDATE SET
                assigned_trigger = EXCLUDED.assigned_trigger,
                trigger_angle = EXCLUDED.trigger_angle,
                trigger_evidence = EXCLUDED.trigger_evidence,
                gap_rank = EXCLUDED.gap_rank,
                notes = EXCLUDED.notes,
                updated_by = 'pipeline',
                updated_at = EXCLUDED.updated_at
            """,
            row,
        )
    conn.commit()


def update_score_gap_rank(conn: psycopg.Connection, client_id: int, ein: str, icp_version: int, gap_rank: float) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE scores SET gap_rank = %s WHERE client_id = %s AND ein = %s AND icp_version = %s AND stage = 'sonnet'",
            (gap_rank, client_id, ein, icp_version),
        )
    conn.commit()


def export_csv(rows: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "ein",
        "name",
        "city",
        "state",
        "gap_rank",
        "criteria_met_count",
        "qualifies",
        "assigned_trigger",
        "trigger_angle",
        "dd_present",
        "fundraising_spend_ratio",
        "suppression_flag",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fieldnames})
