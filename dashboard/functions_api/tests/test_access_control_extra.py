"""Independent gate-1 attacks; real SQL uses only the existing throwaway PG fixture.

Strict xfails describe unfixed input-handling gaps, not accepted behavior. No
provider is contacted and no enrichment is enabled by these tests.
"""

from __future__ import annotations

import base64
import json
import re
from typing import Any, Self

import azure.functions as func
import function_app
import psycopg
import pytest
import test_access_control_postgres as pg_tests
from _auth_helpers import TEST_CLIENT_ID, TEST_EMAIL, customer_headers, encode_principal
from test_access_control import ROUTE_IDS, ROUTES, _request
from test_access_control_postgres import (
    World,
    _call,
    _json,
    _prospect,
    _query,
)

pg_url = pg_tests.pg_url
world = pg_tests.world


def _no_db() -> Any:
    pytest.fail("untrusted input reached a data connection")


@pytest.mark.parametrize(
    "roles", [["customer "], ["CUSTOMER"], [" customer"], ["customer\t"]]
)
@pytest.mark.parametrize(("method", "route", "handler"), ROUTES, ids=ROUTE_IDS)
def test_role_near_misses_fail_before_lookup(
    monkeypatch: pytest.MonkeyPatch,
    roles: list[str],
    method: str,
    route: str,
    handler: Any,
) -> None:
    monkeypatch.setattr(function_app, "_conn", _no_db)
    monkeypatch.setattr(function_app, "_lookup_client_id", lambda *args: _no_db())
    assert (
        handler(_request(method, route, customer_headers(roles=roles))).status_code
        == 403
    )


@pytest.mark.parametrize(
    "body", [None, [], ["status", "approved"], "approved", 1, True]
)
@pytest.mark.parametrize(("method", "route", "handler"), ROUTES, ids=ROUTE_IDS)
def test_non_object_bodies_cannot_bypass_authentication(
    monkeypatch: pytest.MonkeyPatch,
    body: Any,
    method: str,
    route: str,
    handler: Any,
) -> None:
    monkeypatch.setattr(function_app, "_conn", _no_db)
    req = func.HttpRequest(
        method=method,
        url="/api/x",
        body=json.dumps(body).encode(),
        params={"job_id": "foreign"},
        route_params={"id": "1"},
    )
    assert handler(req).status_code == 401


def test_extra_principal_fields_do_not_override_allowlist_or_audit_identity() -> None:
    header = encode_principal(
        {
            "identityProvider": "aad",
            "userDetails": TEST_EMAIL,
            "userRoles": ["customer"],
            "client_id": 999,
            "updated_by": "forged",
            "userId": "foreign-subject",
            "claims": [{"typ": "roles", "val": "admin"}],
        }
    )
    req = _request("GET", "prospects", {function_app.PRINCIPAL_HEADER: header})
    assert function_app._authenticate(req) == function_app.Caller(
        TEST_CLIENT_ID, TEST_EMAIL
    )


@pytest.mark.parametrize("roles", [["authenticated"], ["customer"]])
def test_one_megabyte_extra_field_does_not_change_role_decision(
    roles: list[str],
) -> None:
    header = encode_principal(
        {
            "identityProvider": "aad",
            "userDetails": TEST_EMAIL,
            "userRoles": roles,
            "padding": "x" * (1024 * 1024),
        }
    )
    result = function_app._authenticate(
        _request("GET", "prospects", {function_app.PRINCIPAL_HEADER: header})
    )
    if "customer" in roles:
        assert result == function_app.Caller(TEST_CLIENT_ID, TEST_EMAIL)
    else:
        assert isinstance(result, func.HttpResponse) and result.status_code == 403


@pytest.mark.parametrize(
    ("key", "first", "last"),
    [
        ("userRoles", '["authenticated"]', '["customer"]'),
        ("userRoles", '["customer"]', '["authenticated"]'),
        ("identityProvider", '"github"', '"aad"'),
        ("identityProvider", '"aad"', '"github"'),
        ("userDetails", '"stranger@example.test"', json.dumps(TEST_EMAIL)),
        ("userDetails", json.dumps(TEST_EMAIL), '"stranger@example.test"'),
    ],
)
@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="JSON duplicate principal keys use last value, not reject",
)
def test_duplicate_security_fields_are_rejected(
    key: str, first: str, last: str
) -> None:
    fields = {
        "identityProvider": '"aad"',
        "userDetails": json.dumps(TEST_EMAIL),
        "userRoles": '["customer"]',
    }
    ordinary = ",".join(
        f'"{name}":{value}' for name, value in fields.items() if name != key
    )
    raw = f'{{{ordinary},"{key}":{first},"{key}":{last}}}'
    header = base64.b64encode(raw.encode()).decode()
    result = function_app._authenticate(
        _request("GET", "prospects", {function_app.PRINCIPAL_HEADER: header})
    )
    assert isinstance(result, func.HttpResponse) and result.status_code == 401


@pytest.mark.xfail(
    strict=True, raises=RecursionError, reason="deep principal JSON escapes parser"
)
def test_deeply_nested_extra_field_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(function_app, "_conn", _no_db)
    raw = '{"padding":' + "[" * 10000 + "0" + "]" * 10000 + "}"
    header = base64.b64encode(raw.encode()).decode()
    response = function_app.list_prospects(
        _request("GET", "prospects", {function_app.PRINCIPAL_HEADER: header})
    )
    assert response.status_code == 401


@pytest.mark.real_client_lookup
def test_mixed_case_stored_email_matches_lowercase_login(world: World) -> None:
    with psycopg.connect(world.url, autocommit=True) as conn:
        conn.execute(
            "UPDATE client_users SET email = upper(email) WHERE email = %s",
            (world.a.email,),
        )
    response = _call(function_app.list_prospects, "GET", world.a.email)
    assert response.status_code == 200
    assert {row["id"] for row in _json(response)} == set(world.a.prospect_ids)


@pytest.mark.real_client_lookup
@pytest.mark.parametrize(
    ("variant", "allowed"),
    [
        ("trailing-dot", False),
        ("unicode-lookalike", False),
        ("plus", False),
        ("whitespace", True),
        ("mixed-case", True),
        ("internal-space", False),
    ],
)
def test_email_variants_do_not_alias_other_addresses(
    world: World,
    variant: str,
    allowed: bool,
) -> None:
    local, domain = world.a.email.split("@")
    email = {
        "trailing-dot": world.a.email + ".",
        "unicode-lookalike": "\u0430" + world.a.email[1:],
        "plus": f"{local}+alias@{domain}",
        "whitespace": f" \t{world.a.email}\r\n",
        "mixed-case": world.a.email.upper(),
        "internal-space": f"{local} @{domain}",
    }[variant]
    response = _call(function_app.list_prospects, "GET", email)
    assert response.status_code == (200 if allowed else 403)
    if allowed:
        assert {row["client_id"] for row in _json(response)} == {world.a.client_id}


@pytest.mark.real_client_lookup
def test_same_email_in_two_providers_maps_to_the_correct_tenant(world: World) -> None:
    with psycopg.connect(world.url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO client_users (identity_provider, email, client_id) VALUES ('github', %s, %s)",
            (world.a.email, world.b.client_id),
        )
    for provider, tenant in [("aad", world.a), ("github", world.b)]:
        response = function_app.list_prospects(
            func.HttpRequest(
                method="GET",
                url="/api/prospects",
                body=b"",
                headers=customer_headers(world.a.email, provider=provider),
            )
        )
        assert response.status_code == 200
        assert {row["id"] for row in _json(response)} == set(tenant.prospect_ids)


@pytest.mark.real_client_lookup
def test_operator_header_cannot_override_a_valid_customer(world: World) -> None:
    headers = customer_headers(world.a.email)
    headers[function_app.LOCAL_OPERATOR_CLIENT_HEADER] = str(world.b.client_id)
    response = function_app.list_prospects(
        func.HttpRequest(
            method="GET",
            url="/api/prospects",
            body=b"",
            headers=headers,
        )
    )
    assert response.status_code == 200
    assert {row["id"] for row in _json(response)} == set(world.a.prospect_ids)


@pytest.mark.real_client_lookup
def test_suppression_create_ignores_forged_query_client_id(world: World) -> None:
    response = _call(
        function_app.create_suppression,
        "POST",
        world.a.email,
        params={"client_id": str(world.b.client_id)},
        body={"org_name": "Query forgery", "kind": "client"},
    )
    assert response.status_code == 201
    assert _query(
        world,
        "SELECT client_id FROM suppression WHERE id = %s",
        (_json(response)["id"],),
    ) == [{"client_id": world.a.client_id}]


@pytest.mark.real_client_lookup
@pytest.mark.parametrize("body", [None, [], ["approved"], "approved", 1, True])
@pytest.mark.xfail(
    strict=True, raises=AttributeError, reason="PATCH assumes JSON is an object"
)
def test_authenticated_patch_non_objects_return_400_without_writes(
    world: World, body: Any
) -> None:
    target = world.a.prospect_ids[0]
    before = _prospect(world, target)
    try:
        response = function_app.update_prospect(
            func.HttpRequest(
                method="PATCH",
                url="/api/prospects/x",
                route_params={"id": str(target)},
                body=json.dumps(body).encode(),
                headers=customer_headers(world.a.email),
            )
        )
        assert response.status_code == 400
    finally:
        assert _prospect(world, target) == before


@pytest.mark.real_client_lookup
@pytest.mark.parametrize("status", [[], {}])
@pytest.mark.xfail(
    strict=True, raises=TypeError, reason="PATCH hashes unvalidated status"
)
def test_patch_unhashable_status_returns_400_without_writes(
    world: World, status: Any
) -> None:
    target = world.a.prospect_ids[0]
    before = _prospect(world, target)
    try:
        response = _call(
            function_app.update_prospect,
            "PATCH",
            world.a.email,
            route_params={"id": str(target)},
            body={"status": status},
        )
        assert response.status_code == 400
    finally:
        assert _prospect(world, target) == before


ID_HANDLERS = [
    (function_app.update_prospect, "PATCH", {"status": "reviewed"}),
    (function_app.enrich_prospect, "POST", {}),
    (function_app.enrich_status, "GET", {}),
    (function_app.suppression_review, "POST", {"action": "dismiss"}),
    (function_app.delete_suppression, "DELETE", {}),
]


@pytest.mark.real_client_lookup
@pytest.mark.parametrize(("handler", "method", "body"), ID_HANDLERS)
def test_forged_tenant_on_by_id_routes_cannot_select_foreign_rows(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    handler: Any,
    method: str,
    body: dict[str, Any],
) -> None:
    monkeypatch.setattr(
        function_app, "_get_fullenrich_provider", lambda *args: _no_db()
    )
    target = (
        world.b.suppression_ids[0]
        if handler is function_app.delete_suppression
        else world.b.prospect_ids[0]
    )
    before = _prospect(world, world.b.prospect_ids[0])
    response = _call(
        handler,
        method,
        world.a.email,
        route_params={"id": str(target)},
        params={"client_id": str(world.b.client_id), "job_id": "foreign"},
        body={**body, "client_id": world.b.client_id},
    )
    assert response.status_code == 404
    assert _prospect(world, world.b.prospect_ids[0]) == before
    assert _query(
        world, "SELECT id FROM suppression WHERE id = %s", (world.b.suppression_ids[0],)
    ) == [{"id": world.b.suppression_ids[0]}]


@pytest.mark.real_client_lookup
def test_shared_ein_does_not_mix_tenant_notes_or_review_writes(world: World) -> None:
    with psycopg.connect(world.url, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO prospects "
            "(client_id, ein, status, notes, updated_by, suppression_flag, updated_at) "
            "VALUES (%s, %s, 'new', 'B private note', 'pipeline', 'possible match', now()) "
            "RETURNING id",
            (world.b.client_id, world.a.eins[0]),
        ).fetchone()
        assert row is not None
        foreign_id = int(row[0])
    before = _prospect(world, foreign_id)
    response = _call(function_app.list_prospects, "GET", world.a.email)
    assert response.status_code == 200
    assert {row["id"] for row in _json(response)} == set(world.a.prospect_ids)
    assert b"B private note" not in response.get_body()
    response = _call(
        function_app.suppression_review,
        "POST",
        world.a.email,
        route_params={"id": str(world.a.prospect_ids[0])},
        body={"action": "confirm"},
    )
    assert response.status_code == 200
    assert _prospect(world, foreign_id) == before
    assert _query(
        world,
        "SELECT count(*) AS n FROM suppression WHERE client_id = %s AND ein = %s",
        (world.b.client_id, world.a.eins[0]),
    ) == [{"n": 0}]


@pytest.mark.real_client_lookup
@pytest.mark.parametrize(("handler", "method", "body"), ID_HANDLERS)
@pytest.mark.parametrize("route_id", ["-1", "0"])
def test_nonexistent_numeric_ids_fail_closed(
    world: World,
    handler: Any,
    method: str,
    body: dict[str, Any],
    route_id: str,
) -> None:
    response = _call(
        handler,
        method,
        world.a.email,
        route_params={"id": route_id},
        params={"job_id": "foreign"},
        body=body,
    )
    assert response.status_code == 404


@pytest.mark.real_client_lookup
@pytest.mark.parametrize(("handler", "method", "body"), ID_HANDLERS)
@pytest.mark.parametrize(
    "route_id",
    ["9" * 200, "1 OR 1=1; DROP TABLE client_users;--"],
    ids=["huge", "sql"],
)
@pytest.mark.xfail(
    strict=True,
    raises=psycopg.DataError,
    reason="parameterized SQL prevents injection but invalid IDs escape as database errors",
)
def test_invalid_ids_return_client_error_and_leave_foreign_rows_intact(
    world: World,
    handler: Any,
    method: str,
    body: dict[str, Any],
    route_id: str,
) -> None:
    before = [_prospect(world, target) for target in world.b.prospect_ids]
    try:
        response = _call(
            handler,
            method,
            world.a.email,
            route_params={"id": route_id},
            params={"job_id": "foreign"},
            body=body,
        )
        assert response.status_code in {400, 404}
    finally:
        assert [_prospect(world, target) for target in world.b.prospect_ids] == before
        assert _query(
            world,
            "SELECT count(*) AS n FROM client_users WHERE client_id = %s",
            (world.a.client_id,),
        ) == [{"n": 1}]


@pytest.mark.real_client_lookup
def test_review_second_statement_still_binds_tenant(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contract check for the redundant UPDATE guard after the scoped SELECT.

    An immutable primary key and the scoped SELECT otherwise mask this mutation.
    Execute the actual SQL on Postgres as well; do not rely on a canned fake row.
    """
    connect = function_app._conn
    updates: list[tuple[str, Any]] = []

    class Cursor:
        def __init__(self, cursor: Any) -> None:
            self.cursor = cursor

        def __enter__(self) -> Self:
            self.cursor.__enter__()
            return self

        def __exit__(self, *args: object) -> Any:
            return self.cursor.__exit__(*args)

        def execute(self, query: str, params: Any = None) -> Any:
            if re.match(r"\s*UPDATE prospects SET suppression_flag", query):
                updates.append((query, params))
            return self.cursor.execute(query, params)

        def fetchone(self) -> Any:
            return self.cursor.fetchone()

    class Connection:
        def __init__(self) -> None:
            self.connection = connect()

        def __enter__(self) -> Self:
            self.connection.__enter__()
            return self

        def __exit__(self, *args: object) -> Any:
            return self.connection.__exit__(*args)

        def cursor(self) -> Cursor:
            return Cursor(self.connection.cursor())

        def commit(self) -> None:
            self.connection.commit()

    monkeypatch.setattr(function_app, "_conn", Connection)
    response = _call(
        function_app.suppression_review,
        "POST",
        world.a.email,
        route_params={"id": str(world.a.prospect_ids[0])},
        body={"action": "dismiss"},
    )
    assert response.status_code == 200
    assert len(updates) == 1
    query, params = updates[0]
    assert re.search(r"\bWHERE\s+id\s*=\s*%s\s+AND\s+client_id\s*=\s*%s", query)
    assert params == (world.a.email, str(world.a.prospect_ids[0]), world.a.client_id)
