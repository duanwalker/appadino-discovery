"""v1 item 5, gate 1: tenant isolation against a real Postgres.

The fake connections the other route tests use return their canned rows whatever the
SQL says, so they can't prove a WHERE clause. This module runs the real routes, real
queries and the real client_users lookup against a throwaway `postgres:16` Docker
container, with the schema built by the repo's own `alembic upgrade head`.

Safety: the container is started here on 127.0.0.1 with a generated password and
killed at the end. DATABASE_URL is overwritten (never read) for the alembic subprocess
and for the routes, so no other database can be the target. Skips if Docker is down.

Two customers, A and B, each with prospects, suppression rows and runs. Every test
logs in as A's user and tries to read or change B's rows.
"""

from __future__ import annotations

import csv
import io
import json
import os
import secrets
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import azure.functions as func
import psycopg
import pytest
from _auth_helpers import customer_headers
from psycopg.rows import dict_row

import function_app

pytestmark = pytest.mark.real_client_lookup

POSTGRES_IMAGE = "postgres:16"
PIPELINE_DIR = Path(__file__).resolve().parents[3] / "pipeline"


def _docker_available() -> bool:
    try:
        result = subprocess.run(["docker", "info"], capture_output=True, timeout=30, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="module")
def pg_url() -> Iterator[str]:
    if not _docker_available():
        pytest.skip("Docker daemon not available: access-control Postgres tests need a throwaway container")
    port = _free_port()
    password = secrets.token_hex(12)
    url = f"postgresql://postgres:{password}@127.0.0.1:{port}/postgres"
    run = subprocess.run(
        [
            "docker", "run", "--rm", "-d",
            "--name", f"access-control-it-{secrets.token_hex(4)}",
            "-p", f"127.0.0.1:{port}:5432",
            "-e", f"POSTGRES_PASSWORD={password}",
            POSTGRES_IMAGE,
        ],
        capture_output=True, text=True, check=False,
    )
    if run.returncode != 0:
        pytest.fail(f"could not start {POSTGRES_IMAGE}: {run.stderr.strip()}")
    container_id = run.stdout.strip()
    try:
        deadline = time.monotonic() + 90
        while True:
            try:
                with psycopg.connect(url, connect_timeout=2) as conn:
                    conn.execute("SELECT 1")
                break
            except psycopg.Error:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.5)
        migrate = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=PIPELINE_DIR,
            env={**os.environ, "DATABASE_URL": url},  # overwritten, never inherited
            capture_output=True, text=True, timeout=300, check=False,
        )
        assert migrate.returncode == 0, f"alembic upgrade head failed:\n{migrate.stdout}\n{migrate.stderr}"
        yield url
    finally:
        subprocess.run(["docker", "kill", container_id], capture_output=True, check=False)


@dataclass(frozen=True)
class Tenant:
    client_id: int
    email: str
    prospect_ids: list[int]
    eins: list[str]
    suppression_ids: list[int]
    run_ids: list[int]


@dataclass(frozen=True)
class World:
    url: str
    a: Tenant
    b: Tenant


def _ein() -> str:
    return f"{secrets.randbelow(10**9):09d}"


def _seed_tenant(conn: psycopg.Connection[dict[str, Any]], label: str) -> Tenant:
    tag = secrets.token_hex(4)
    client = conn.execute(
        "INSERT INTO clients (name, status, created_at) VALUES (%s, 'active', now()) RETURNING id",
        (f"Access-control tenant {label} {tag}",),
    ).fetchone()
    assert client is not None
    client_id = int(client["id"])
    email = f"{label.lower()}-{tag}@customer.test"
    conn.execute(
        "INSERT INTO client_users (identity_provider, email, client_id) VALUES ('aad', %s, %s)",
        (email, client_id),
    )
    prospect_ids, eins, suppression_ids, run_ids = [], [], [], []
    for index in range(3):
        ein = _ein()
        conn.execute(
            "INSERT INTO organizations (ein, name, bmf_updated_at) VALUES (%s, %s, now())",
            (ein, f"{label} org {index}"),
        )
        row = conn.execute(
            "INSERT INTO prospects (client_id, ein, status, notes, updated_by, suppression_flag, updated_at) "
            "VALUES (%s, %s, 'new', %s, 'pipeline', 'possible match', now()) RETURNING id",
            (client_id, ein, f"{label} original note"),
        ).fetchone()
        assert row is not None
        prospect_ids.append(int(row["id"]))
        eins.append(ein)
        supp = conn.execute(
            "INSERT INTO suppression (client_id, ein, org_name, kind, source, added_at) "
            "VALUES (%s, %s, %s, 'client', 'seed', now()) RETURNING id",
            (client_id, _ein(), f"{label} suppressed org {index}"),
        ).fetchone()
        assert supp is not None
        suppression_ids.append(int(supp["id"]))
        run = conn.execute(
            "INSERT INTO runs (client_id, stage, started_at, status) VALUES (%s, 'publish', now(), 'ok') "
            "RETURNING id",
            (client_id,),
        ).fetchone()
        assert run is not None
        run_ids.append(int(run["id"]))
    return Tenant(client_id, email, prospect_ids, eins, suppression_ids, run_ids)


@pytest.fixture
def world(pg_url: str, monkeypatch: pytest.MonkeyPatch) -> World:
    monkeypatch.setenv("DATABASE_URL", pg_url)  # the routes' real _conn() targets the container
    with psycopg.connect(pg_url, autocommit=True, row_factory=dict_row) as conn:
        return World(pg_url, _seed_tenant(conn, "A"), _seed_tenant(conn, "B"))


def _query(world: World, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    with psycopg.connect(world.url, row_factory=dict_row) as conn:
        return conn.execute(sql, params).fetchall()


def _call(
    handler: Any,
    method: str,
    email: str,
    *,
    route_params: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
) -> func.HttpResponse:
    return handler(
        func.HttpRequest(
            method=method,
            url="/api/x",
            params=params or {},
            route_params=route_params or {},
            body=json.dumps(body or {}).encode(),
            headers=customer_headers(email),
        )
    )


def _json(response: func.HttpResponse) -> Any:
    return json.loads(response.get_body())


def _prospect(world: World, prospect_id: int) -> dict[str, Any]:
    return _query(world, "SELECT status, notes, updated_by, suppression_flag FROM prospects WHERE id = %s",
                  (prospect_id,))[0]


# --- who gets in ------------------------------------------------------------------------


def test_listed_user_reads_own_prospects(world: World) -> None:
    response = _call(function_app.list_prospects, "GET", world.a.email)
    assert response.status_code == 200
    assert sorted(row["id"] for row in _json(response)) == sorted(world.a.prospect_ids)


def test_email_match_is_case_insensitive(world: World) -> None:
    response = _call(function_app.list_prospects, "GET", world.a.email.upper())
    assert response.status_code == 200
    assert {row["client_id"] for row in _json(response)} == {world.a.client_id}


def test_inactive_user_is_403_and_reactivating_restores_access(world: World) -> None:
    with psycopg.connect(world.url, autocommit=True) as conn:
        conn.execute("UPDATE client_users SET active = false WHERE email = %s", (world.a.email,))
    assert _call(function_app.list_prospects, "GET", world.a.email).status_code == 403
    assert _call(function_app.export_csv, "GET", world.a.email).status_code == 403
    with psycopg.connect(world.url, autocommit=True) as conn:
        conn.execute("UPDATE client_users SET active = true WHERE email = %s", (world.a.email,))
    assert _call(function_app.list_prospects, "GET", world.a.email).status_code == 200


def test_unlisted_user_is_403(world: World) -> None:
    assert _call(function_app.list_prospects, "GET", "nobody@customer.test").status_code == 403


def test_same_email_from_another_provider_is_403(world: World) -> None:
    response = function_app.list_prospects(
        func.HttpRequest(method="GET", url="/api/prospects", body=b"",
                         headers=customer_headers(world.a.email, provider="github"))
    )
    assert response.status_code == 403


def test_unique_index_rejects_a_second_spelling_of_one_email(world: World) -> None:
    with psycopg.connect(world.url, autocommit=True) as conn, pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute(
            "INSERT INTO client_users (identity_provider, email, client_id) VALUES ('aad', %s, %s)",
            (world.a.email.upper(), world.b.client_id),
        )


# --- reads: list, export, suppression, runs (forged client_id ignored) ------------------


def test_list_ignores_forged_client_id(world: World) -> None:
    response = _call(function_app.list_prospects, "GET", world.a.email,
                     params={"client_id": str(world.b.client_id)})
    assert response.status_code == 200
    ids = {row["id"] for row in _json(response)}
    assert ids == set(world.a.prospect_ids)
    assert not ids & set(world.b.prospect_ids)


def test_export_ignores_forged_client_id(world: World) -> None:
    response = _call(function_app.export_csv, "GET", world.a.email,
                     params={"client_id": str(world.b.client_id)})
    assert response.status_code == 200
    rows = list(csv.DictReader(io.StringIO(response.get_body().decode("utf-8-sig"))))
    assert {row["EIN"] for row in rows} == set(world.a.eins)
    assert f"prospects_client{world.a.client_id}.csv" in response.headers["Content-Disposition"]


def test_suppression_list_ignores_forged_client_id(world: World) -> None:
    response = _call(function_app.list_suppression, "GET", world.a.email,
                     params={"client_id": str(world.b.client_id)})
    assert response.status_code == 200
    assert {row["id"] for row in _json(response)} == set(world.a.suppression_ids)


def test_runs_ignore_forged_client_id(world: World) -> None:
    response = _call(function_app.list_runs, "GET", world.a.email,
                     params={"client_id": str(world.b.client_id)})
    assert response.status_code == 200
    assert {row["id"] for row in _json(response)} == set(world.a.run_ids)


def test_create_suppression_ignores_forged_client_id_in_body(world: World) -> None:
    response = _call(function_app.create_suppression, "POST", world.a.email,
                     body={"client_id": world.b.client_id, "org_name": "Forged", "kind": "client"})
    assert response.status_code == 201
    new_id = _json(response)["id"]
    assert _query(world, "SELECT client_id FROM suppression WHERE id = %s", (new_id,)) == [
        {"client_id": world.a.client_id}
    ]


# --- by-id routes: another customer's row is 404 and untouched ---------------------------


def test_patch_other_customers_prospect_is_404_and_unchanged(world: World) -> None:
    target = world.b.prospect_ids[0]
    before = _prospect(world, target)
    response = _call(function_app.update_prospect, "PATCH", world.a.email,
                     route_params={"id": str(target)},
                     body={"status": "rejected", "notes": "hijacked", "updated_by": "mallory"})
    assert response.status_code == 404
    assert _prospect(world, target) == before


def test_patch_own_prospect_records_login_email_not_body(world: World) -> None:
    target = world.a.prospect_ids[0]
    response = _call(function_app.update_prospect, "PATCH", world.a.email,
                     route_params={"id": str(target)},
                     body={"status": "reviewed", "notes": "ok", "updated_by": "mallory"})
    assert response.status_code == 200
    after = _prospect(world, target)
    assert (after["status"], after["notes"], after["updated_by"]) == ("reviewed", "ok", world.a.email)


@pytest.mark.parametrize("action", ["confirm", "dismiss"])
def test_suppression_review_of_other_customers_prospect_is_404_and_unchanged(
    world: World, action: str
) -> None:
    target = world.b.prospect_ids[1]
    before_prospect = _prospect(world, target)
    count_sql = "SELECT count(*) AS n FROM suppression WHERE client_id = ANY(%s)"
    clients = [world.a.client_id, world.b.client_id]
    before_count = _query(world, count_sql, (clients,))
    response = _call(function_app.suppression_review, "POST", world.a.email,
                     route_params={"id": str(target)}, body={"action": action})
    assert response.status_code == 404
    assert _prospect(world, target) == before_prospect
    assert _query(world, count_sql, (clients,)) == before_count


def test_suppression_review_of_own_prospect_records_login_email(world: World) -> None:
    target = world.a.prospect_ids[1]
    response = _call(function_app.suppression_review, "POST", world.a.email,
                     route_params={"id": str(target)}, body={"action": "confirm", "updated_by": "mallory"})
    assert response.status_code == 200
    after = _prospect(world, target)
    assert (after["suppression_flag"], after["updated_by"]) == (None, world.a.email)
    sources = _query(world, "SELECT source FROM suppression WHERE client_id = %s AND ein = %s",
                     (world.a.client_id, world.a.eins[1]))
    assert sources == [{"source": f"dashboard confirm by {world.a.email}"}]


def test_delete_other_customers_suppression_is_404_and_row_survives(world: World) -> None:
    target = world.b.suppression_ids[0]
    response = _call(function_app.delete_suppression, "DELETE", world.a.email,
                     route_params={"id": str(target)})
    assert response.status_code == 404
    assert _query(world, "SELECT id FROM suppression WHERE id = %s", (target,)) == [{"id": target}]


def test_delete_own_suppression_works(world: World) -> None:
    target = world.a.suppression_ids[0]
    response = _call(function_app.delete_suppression, "DELETE", world.a.email,
                     route_params={"id": str(target)})
    assert response.status_code == 200
    assert _query(world, "SELECT id FROM suppression WHERE id = %s", (target,)) == []


@pytest.mark.parametrize(
    ("handler", "method", "params"),
    [
        (function_app.enrich_prospect, "POST", {}),
        (function_app.enrich_status, "GET", {"job_id": "job-from-elsewhere"}),
    ],
    ids=["enrich", "enrich-status"],
)
def test_enrich_routes_on_other_customers_prospect_are_404_before_any_provider(
    world: World, monkeypatch: pytest.MonkeyPatch, handler: Any, method: str, params: dict[str, str]
) -> None:
    def no_provider(subaccount_id: str | None) -> None:
        raise AssertionError("FullEnrich provider built for another customer's prospect")

    monkeypatch.setattr(function_app, "_get_fullenrich_provider", no_provider)
    target = world.b.prospect_ids[2]
    response = _call(handler, method, world.a.email, route_params={"id": str(target)}, params=params)
    assert response.status_code == 404
    assert _json(response) == {"error": "prospect not found"}
    assert _query(world, "SELECT count(*) AS n FROM enrichment_jobs WHERE ein = %s",
                  (world.b.eins[2],)) == [{"n": 0}]
    # Control: the owner reaches the row (then stops at the enrichment guard: B has no
    # enabled ICP config), so the 404 above is ownership, not a missing row.
    own = _call(handler, method, world.b.email, route_params={"id": str(target)}, params=params)
    assert own.status_code == 403
    assert _json(own) != {"error": "forbidden"}
