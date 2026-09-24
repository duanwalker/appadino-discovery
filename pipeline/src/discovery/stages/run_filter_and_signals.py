"""Orchestrates Stage 1 (filter.py) + an early EIN-exact suppression gate + Stage 2
(extract_signals.py) as one G1.3 run, logged to `runs` the same way Stage 0's ingest
is (§7) — the automated stand-in for "Duan reads the output".

The suppression gate calls Stage 4's apply_suppression() (suppress.py) right after
Stage 1, before Stage 2/3 ever run — an EIN-exact match to an existing ARCHITECT
client/prospect is real, known information the moment Stage 1 produces it; there's no
reason to pay for extraction or a real Sonnet scoring call on an org that's already
going to be suppressed. Fuzzy name matches are NOT dropped here: Lauren's rule is
that a fuzzy match gets flagged for human review, never silently excluded, which
requires the org to go through full extraction and scoring so a reviewer has real
data to look at. apply_suppression() is called again, unchanged, at Publish (Stage
6) — cheap to recompute, and it's what actually sets prospects.suppression_flag on
whatever made it through scoring.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from typing import Any

import psycopg
from psycopg.types.json import Json

from discovery.stages.extract_signals import extract_signals_for_survivors
from discovery.stages.filter import select_survivor_eins
from discovery.stages.suppress import apply_suppression

logger = logging.getLogger(__name__)


def run_filter_and_signals(client_id: int, database_url: str | None = None) -> dict[str, Any]:
    database_url = database_url or os.environ["DATABASE_URL"]

    started_at = datetime.now(UTC)
    counts: dict[str, Any] = {}
    status = "success"
    error: str | None = None
    run_id: int | None = None

    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO runs (client_id, stage, started_at, status) "
                "VALUES (%s, 'filter_and_signals', %s, 'running') RETURNING id",
                (client_id, started_at),
            )
            row = cur.fetchone()
            assert row is not None
            run_id = row[0]
        conn.commit()

    try:
        with psycopg.connect(database_url) as conn:
            survivor_eins = select_survivor_eins(conn, client_id)
            counts["survivors"] = len(survivor_eins)
            logger.info("Stage 1: %d survivors for client_id=%s", len(survivor_eins), client_id)

            remaining_eins, fuzzy_flags = apply_suppression(conn, client_id, survivor_eins)
        counts["ein_suppressed_pre_extraction"] = len(survivor_eins) - len(remaining_eins)
        counts["fuzzy_flagged_pre_extraction"] = len(fuzzy_flags)
        logger.info(
            "Suppression (pre-Stage2): %d EIN-exact suppressed, %d fuzzy-flagged (continuing to Stage 2/3)",
            counts["ein_suppressed_pre_extraction"],
            counts["fuzzy_flagged_pre_extraction"],
        )

        signal_counts = extract_signals_for_survivors(database_url, remaining_eins)
        counts.update(signal_counts)
        logger.info("Stage 2: %s", signal_counts)
    except Exception as exc:
        status = "failed"
        error = str(exc)
        logger.exception("RUN_FAILED stage=filter_and_signals client_id=%s error=%s", client_id, error)
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

    if status == "success" and counts.get("survivors") == 0:
        logger.warning("ZERO_OUTPUT_RUN stage=filter_and_signals client_id=%s counts=%s", client_id, counts)

    return counts
