"""Orchestrates Stage 1 (filter.py) + Stage 2 (extract_signals.py) as one G1.3 run,
logged to `runs` the same way Stage 0's ingest is (§7) — the automated stand-in for
"Duan reads the output".
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

        signal_counts = extract_signals_for_survivors(database_url, survivor_eins)
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
