"""Real-Postgres regression for status/notes updates reordering tied prospects.

Run with TEST_DATABASE_URL pointing at localhost or 127.0.0.1 and pytest -s to
see the version and informational old-ordering probe. Unset means skip; unsafe
targets fail before connecting. DATABASE_URL and Azure settings are never read.

The pipeline integration module currently owns a Docker/local-binary server, not
a TEST_DATABASE_URL fixture. Reuse its real Alembic migration approach instead,
in a unique schema that is dropped even on failure; never truncate shared tables.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from function_app import EXPORT_QUERY, PROSPECT_QUERY
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row
from sqlalchemy.engine import URL

PIPELINE_DIR = Path(__file__).resolve().parents[3] / "pipeline"
ROUNDS = 20


def _local_database_url(value: str, schema: str) -> str:
    """Pin the transport too: PGHOSTADDR must not redirect a localhost URL."""
    params = {key: str(item) for key, item in conninfo_to_dict(value).items() if item is not None}
    if (
        params.get("host") not in {"localhost", "127.0.0.1"}
        or params.get("hostaddr", "127.0.0.1") != "127.0.0.1"
        or "service" in params
    ):
        pytest.fail(
            "Unsafe TEST_DATABASE_URL: host must be localhost or 127.0.0.1; "
            "remote hostaddr, multiple hosts and service indirection are forbidden",
            pytrace=False,
        )
    # Rebuild rather than forward arbitrary DSN options (including search_path).
    return URL.create(
        "postgresql",
        username=params.get("user"),
        password=params.get("password"),
        host=params["host"],
        port=int(params["port"]) if "port" in params else None,
        database=params.get("dbname"),
        query={
            "hostaddr": "127.0.0.1",
            "connect_timeout": "5",
            "sslmode": params.get("sslmode", "prefer"),
            "options": f"-csearch_path={schema}",
        },
    ).render_as_string(hide_password=False)


@pytest.mark.parametrize(
    "value",
    [
        "postgresql://example.postgres.database.azure.com/discovery",
        "postgresql://192.0.2.1/discovery",
        "postgresql://localhost.example.com/discovery",
        "postgresql:///discovery",
        "host=localhost,example.com dbname=discovery",
        "host=localhost hostaddr=192.0.2.1 dbname=discovery",
        "host=127.0.0.1 service=dev dbname=discovery",
        "postgresql://localhost/discovery?host=example.com",
    ],
)
def test_database_guard_rejects_unsafe_targets(value: str) -> None:
    with pytest.raises(pytest.fail.Exception, match="Unsafe TEST_DATABASE_URL"):
        _local_database_url(value, "unused")


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1"])
def test_database_guard_pins_loopback_and_schema(host: str) -> None:
    result = conninfo_to_dict(
        _local_database_url(
            f"postgresql://{host}/scratch?options=-csearch_path%3Dpublic", "isolated"
        )
    )
    assert result["host"] == host
    assert result["hostaddr"] == "127.0.0.1"
    assert result["options"] == "-csearch_path=isolated"


@pytest.fixture
def postgres() -> Iterator[psycopg.Connection[dict[str, Any]]]:
    value = os.environ.get("TEST_DATABASE_URL")
    if not value:
        pytest.skip("TEST_DATABASE_URL is unset: requires a scratch local Postgres")
    schema = f"dashboard_ordering_{uuid4().hex}"
    url = _local_database_url(value, schema)
    with psycopg.connect(url, autocommit=True, row_factory=dict_row) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            # env.py passes this through Alembic's ConfigParser: URI escapes
            # (including the encoded search_path '=') need literal percent signs.
            result = subprocess.run(
                [sys.executable, "-m", "alembic", "upgrade", "head"],
                cwd=PIPELINE_DIR,
                env={**os.environ, "DATABASE_URL": url.replace("%", "%%")},
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            assert result.returncode == 0, (
                f"Alembic migration failed:\n{result.stdout}\n{result.stderr}"
            )
            with psycopg.connect(url, autocommit=True, row_factory=dict_row) as conn:
                assert conn.execute("SELECT current_schema() AS name").fetchone() == {
                    "name": schema
                }
                yield conn
        finally:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def _seed(
    conn: psycopg.Connection[dict[str, Any]], include_nulls: bool
) -> tuple[int, dict[int, Decimal | None]]:
    client = conn.execute(
        "INSERT INTO clients (name, status, created_at) "
        "VALUES ('Ordering regression tenant', 'active', now()) RETURNING id"
    ).fetchone()
    assert client is not None
    client_id = int(client["id"])
    ranks = [Decimal("66.67")] * 6 + [Decimal("50.0")] * 4
    ranks += [Decimal(90), Decimal(10)]
    by_id: dict[int, Decimal | None] = dict(enumerate(ranks, start=1))
    if include_nulls:
        by_id.update({13: None, 14: None, 15: None})
    # Physical insertion order deliberately differs from primary-key tie order.
    for prospect_id in reversed(by_id):
        ein = f"{900000000 + prospect_id:09d}"
        conn.execute(
            "INSERT INTO organizations (ein, name, bmf_updated_at) VALUES (%s, %s, now())",
            (ein, f"Synthetic ordering organization {prospect_id}"),
        )
        conn.execute(
            "INSERT INTO prospects (id, client_id, ein, status, gap_rank, updated_at) "
            "VALUES (%s, %s, %s, 'new', %s, now())",
            (prospect_id, client_id, ein, by_id[prospect_id]),
        )
    for year, revenue in [(2023, 100000), (2024, 200000)]:
        conn.execute(
            "INSERT INTO filings "
            "(ein, tax_year, form_type, object_id, revenue_total, mission_text, "
            "program_text, officers, extracted_at) "
            "VALUES ('900000001', %s, '990', %s, %s, 'Synthetic mission', "
            "'[]'::jsonb, '[]'::jsonb, now())",
            (year, f"ordering-{year}", revenue),
        )
    return client_id, by_id


def _rows(
    conn: psycopg.Connection[dict[str, Any]], query: str, client_id: int
) -> list[dict[str, Any]]:
    return conn.execute(query, {"client_id": client_id}).fetchall()


def _old_ordering(query: str) -> str:
    # Change only the outer ORDER BY; retain every lateral join in the real query.
    old, count = re.subn(
        r"ORDER BY p\.gap_rank DESC NULLS LAST(?:, p\.id ASC)?\s*$",
        "ORDER BY p.gap_rank DESC NULLS LAST",
        query,
    )
    assert count == 1, "Could not identify the query's outer ordering for the probe"
    return old


@pytest.mark.parametrize("include_nulls", [False, True], ids=["tied-ranks", "tied-nulls"])
def test_list_and_export_stay_ordered_after_updates(
    postgres: psycopg.Connection[dict[str, Any]], include_nulls: bool
) -> None:
    version = postgres.execute("SELECT version() AS version").fetchone()
    assert version is not None
    print(f"\nPostgres version: {version['version']}")
    client_id, ranks = _seed(postgres, include_nulls)
    expected = sorted(
        ranks,
        key=lambda prospect_id: (
            ranks[prospect_id] is None,
            -(ranks[prospect_id] or Decimal(0)),
            prospect_id,
        ),
    )
    queries = {"list": PROSPECT_QUERY, "export": EXPORT_QUERY}
    old_queries = {name: _old_ordering(query) for name, query in queries.items()}
    old_baselines = {
        name: [row["id"] for row in _rows(postgres, query, client_id)]
        for name, query in old_queries.items()
    }
    old_changes: dict[str, list[str]] = {name: [] for name in queries}

    def assert_order(label: str) -> None:
        results = {name: _rows(postgres, query, client_id) for name, query in queries.items()}
        list_ids = [row["id"] for row in results["list"]]
        export_ids = [row["id"] for row in results["export"]]
        assert list_ids == expected, f"{label}: list order differs from baseline"
        assert export_ids == expected, f"{label}: export order differs from baseline"
        assert export_ids == list_ids, f"{label}: export/list mismatch"
        for rows in results.values():
            assert {row["id"]: row["gap_rank"] for row in rows} == ranks
        filing = next(row for row in results["export"] if row["id"] == 1)
        assert filing["filing_revenue_total"] == Decimal(200000)
        assert filing["filing_mission_text"] == "Synthetic mission"
        assert filing["filing_program_text"] == []
        for name, query in old_queries.items():
            ids = [row["id"] for row in _rows(postgres, query, client_id)]
            if ids != old_baselines[name]:
                old_changes[name].append(label)

    assert_order("baseline")
    tied_ids = list(range(1, 11)) + ([13, 14, 15] if include_nulls else [])
    for round_index in range(ROUNDS):
        prospect_id = tied_ids[round_index % len(tied_ids)]
        status = "reviewed" if (round_index // len(tied_ids)) % 2 == 0 else "approved"
        for field, value in [("status", status), ("notes", f"Review round {round_index + 1}")]:
            # autocommit makes each write visible to the subsequent reload, just
            # like separate PATCH and GET requests; no planner/heap knobs are set.
            changed = postgres.execute(
                sql.SQL(
                    "UPDATE prospects SET {} = %s, updated_at = now() "
                    "WHERE client_id = %s AND id = %s RETURNING status, notes"
                ).format(sql.Identifier(field)),
                (value, client_id, prospect_id),
            ).fetchone()
            assert changed is not None and changed[field] == value
            assert_order(f"round {round_index + 1} after {field} on id={prospect_id}")
    print(f"Stable list/export: {len(ranks)} prospects, {ROUNDS} rounds, 40 committed updates")
    for name, changes in old_changes.items():
        print(
            f"Old-order probe ({name}, include_nulls={include_nulls}): "
            f"visibly_changed={bool(changes)}, changed_reloads={len(changes)}/40, "
            f"first_change={changes[0] if changes else 'none'}"
        )
