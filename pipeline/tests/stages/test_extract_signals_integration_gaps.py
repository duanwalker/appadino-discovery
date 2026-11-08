"""Additional contracts exposed by the Stage 2 real-Postgres mutation run."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import psycopg
import pytest

from discovery.stages import extract_signals

from . import test_extract_signals_integration as integration
from .test_extract_signals_integration import (
    _build_archive,
    _insert_filing,
    _insert_manifest,
    _insert_org,
    _insert_signal,
    _sample_990,
)

pytestmark = pytest.mark.integration

_pg_server = integration._pg_server
pg = integration.pg
conn = integration.conn
offline_stage2 = integration.offline_stage2


def test_resume_query_bounds_both_aggregates(conn: psycopg.Connection) -> None:
    """Removing one predicate preserves results via the join, but loses SQL scope."""
    requested = ["710000001"]
    for ein in (*requested, "710000002"):
        _insert_org(conn, ein)
        _insert_filing(conn, ein, 2023, f"scope-{ein}")
        _insert_signal(conn, ein, 2023)

    # Spy on SQL submission only; the real cursor still executes against Postgres.
    with patch.object(
        psycopg.Cursor, "execute", autospec=True, side_effect=psycopg.Cursor.execute
    ) as execute:
        result = extract_signals._select_eins_already_signaled(conn, requested)

    assert result == set(requested)
    assert execute.call_count == 1
    _, query, params = execute.call_args.args
    sql = " ".join(query.split())
    assert "FROM filings WHERE ein = ANY(%s) GROUP BY ein" in sql
    assert "FROM signals WHERE ein = ANY(%s) GROUP BY ein" in sql
    assert params == (requested, requested)


def test_archive_disappearing_after_index_aborts_without_committing(
    conn: psycopg.Connection,
    pg: str,
    tmp_path: Path,
    offline_stage2: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    good_ein, broken_ein = "720000001", "720000002"
    good_id, broken_id = "gap-good", "gap-disappearing"
    for ein, object_id in ((good_ein, good_id), (broken_ein, broken_id)):
        _insert_org(conn, ein)
        _insert_filing(conn, ein, 2023, object_id)

    good = _build_archive(
        tmp_path / "2023" / "good.zip",
        {good_id: _sample_990(revenue_total=750_000, website="https://good.example")},
    )
    disappearing = _build_archive(
        tmp_path / "2023" / "disappearing.zip",
        {broken_id: _sample_990(revenue_total=900_000)},
    )
    _insert_manifest(conn, 2023, 1, "A", str(good))
    _insert_manifest(conn, 2023, 2, "B", str(disappearing))
    real_build = extract_signals.build_year_index

    def index_then_remove(connection: psycopg.Connection, year: int) -> dict[str, tuple[str, str]]:
        index = real_build(connection, year)
        assert good_id in index and broken_id in index
        disappearing.unlink()
        return index

    monkeypatch.setattr(extract_signals, "build_year_index", index_then_remove)

    with pytest.raises(extract_signals.ArchiveFileMissingError) as excinfo:
        extract_signals.extract_signals_for_survivors(
            pg, [good_ein, broken_ein], cache_dir=str(tmp_path)
        )

    # This error must originate in fetch_filing_xml, not build_year_index.
    assert f"object_id={broken_id}" in str(excinfo.value)
    assert repr(str(disappearing)) in str(excinfo.value)
    with psycopg.connect(pg) as verify, verify.cursor() as cur:
        cur.execute("SELECT count(*) FROM filings WHERE extracted_at IS NOT NULL")
        assert cur.fetchall() == [(0,)]
        cur.execute("SELECT count(*) FROM organizations WHERE website IS NOT NULL")
        assert cur.fetchall() == [(0,)]
        cur.execute("SELECT count(*) FROM signals")
        assert cur.fetchall() == [(0,)]
        cur.execute("SELECT count(*) FROM archive_manifest")
        assert cur.fetchall() == [(2,)]
