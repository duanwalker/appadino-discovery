"""Real-Postgres checks for the SQL behind scoring resume: the unfinished-run guard
(JSONB `?` operator, retention window) and the counts write/read round trip on the
`runs` table. Reuses the Stage 2 integration module's Docker/initdb server fixture;
skips when no local Postgres backend is available.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Json

from discovery.stages.run_scoring import (
    ResumeRunError,
    _find_unfinished_scoring_runs,
    _load_run_for_resume,
    _persist_counts,
)
from tests.stages.test_extract_signals_integration import _pg_server  # noqa: F401 (pytest fixture)

pytestmark = pytest.mark.integration

BATCHES = {"batches": {"haiku": ["msgbatch_1"]}, "icp_version": 1, "haiku_cut_n": 3000}


@pytest.fixture
def runs_db(_pg_server: str) -> Iterator[str]:  # noqa: F811
    with psycopg.connect(_pg_server) as conn, conn.cursor() as cur:
        cur.execute("TRUNCATE runs RESTART IDENTITY CASCADE")
        conn.commit()
    yield _pg_server


def _insert_run(url: str, client_id: int, stage: str, status: str, counts: dict[str, Any] | None, age_days: int = 0) -> int:
    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO runs (client_id, stage, started_at, status, counts) VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (client_id, stage, datetime.now(UTC) - timedelta(days=age_days), status, Json(counts) if counts else None),
        )
        row = cur.fetchone()
        assert row is not None
        conn.commit()
        return int(row[0])


def test_unfinished_guard_matches_only_recent_unfinished_runs_with_batches(runs_db: str) -> None:
    killed = _insert_run(runs_db, 2, "scoring", "running", BATCHES)
    failed = _insert_run(runs_db, 2, "scoring", "failed", BATCHES)
    _insert_run(runs_db, 2, "scoring", "failed", {"eins_requested": 5})  # failed before any submit
    _insert_run(runs_db, 2, "scoring", "success", BATCHES)
    _insert_run(runs_db, 3, "scoring", "running", BATCHES)  # another client
    _insert_run(runs_db, 2, "filter_and_signals", "running", BATCHES)  # another stage
    _insert_run(runs_db, 2, "scoring", "running", BATCHES, age_days=30)  # results expired
    _insert_run(runs_db, 2, "scoring", "running", None)

    with psycopg.connect(runs_db) as conn:
        found = _find_unfinished_scoring_runs(conn, 2)

    assert [row[0] for row in found] == [killed, failed]


def test_persisted_counts_round_trip_into_resume(runs_db: str) -> None:
    run_id = _insert_run(runs_db, 2, "scoring", "running", None)
    counts = {**BATCHES, "batches": {"haiku": ["msgbatch_1", "msgbatch_2"]}, "eins": ["000000001"], "eins_overridden": True}

    _persist_counts(runs_db, run_id, counts)
    with psycopg.connect(runs_db) as conn:
        loaded = _load_run_for_resume(conn, run_id, 2)

    assert loaded == counts


def test_resume_load_refuses_a_successful_run(runs_db: str) -> None:
    run_id = _insert_run(runs_db, 2, "scoring", "success", BATCHES)
    with psycopg.connect(runs_db) as conn, pytest.raises(ResumeRunError, match="already finished"):
        _load_run_for_resume(conn, run_id, 2)
