"""v1 item 5, gate 1: who may call the API, decided before any data is touched.

No database here: `_conn` is replaced with a function that fails the test if called,
so every 401/403 below is proven to happen before a query runs. The routes are found
from the FunctionApp's own registry, so a route added later without
`_authenticate(req)` fails these tests. Cross-customer ownership (list, export and
every by-id route) is tested against real Postgres in test_access_control_postgres.py.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from typing import Any

import azure.functions as func
import pytest
from _auth_helpers import TEST_CLIENT_ID, TEST_EMAIL, customer_headers, encode_principal

import function_app

EXPECTED_ROUTES = {
    ("GET", "prospects"),
    ("PATCH", "prospects/{id}"),
    ("POST", "prospects/{id}/enrich"),
    ("GET", "prospects/{id}/enrich-status"),
    ("POST", "prospects/{id}/suppression-review"),
    ("GET", "suppression"),
    ("POST", "suppression"),
    ("DELETE", "suppression/{id}"),
    ("GET", "export.csv"),
    ("GET", "runs"),
}


def _registered_routes() -> list[tuple[str, str, Callable[[func.HttpRequest], func.HttpResponse]]]:
    routes = []
    for function in function_app.app.get_functions():
        trigger = next(b for b in function.get_bindings() if b.type == "httpTrigger")
        for method in trigger.methods:
            routes.append((str(method.value), trigger.route, function.get_user_function()))
    return routes


ROUTES = _registered_routes()
ROUTE_IDS = [f"{method} {route}" for method, route, _ in ROUTES]


def _request(method: str, route: str, headers: dict[str, str] | None = None) -> func.HttpRequest:
    # A request that would get past validation on every route (id, job_id, a full
    # suppression body), so only authentication can stop it.
    body = {"action": "dismiss", "org_name": "Synthetic Org", "kind": "client", "status": "reviewed"}
    return func.HttpRequest(
        method=method,
        url=f"/api/{route}",
        params={"job_id": "job-1", "client_id": str(TEST_CLIENT_ID)},
        route_params={"id": "1"},
        body=json.dumps(body).encode(),
        headers=headers or {},
    )


def _db_must_not_be_touched() -> Any:
    raise AssertionError("route reached the database before authentication rejected it")


@pytest.fixture
def no_db(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(function_app, "_conn", _db_must_not_be_touched)


@pytest.fixture
def lookup_must_not_run(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(identity_provider: str, email: str) -> int | None:
        raise AssertionError("client_users lookup ran before the role check rejected the caller")

    monkeypatch.setattr(function_app, "_lookup_client_id", fail)


def test_registered_routes_are_exactly_the_customer_routes() -> None:
    """Pins the route list: a new route must be added here deliberately, which pulls it
    into every test below. /api/clients is not registered (local operator mode off)."""
    assert {(method, route) for method, route, _ in ROUTES} == EXPECTED_ROUTES
    assert not function_app.LOCAL_OPERATOR_MODE


def test_functions_key_level_is_anonymous_because_auth_is_in_code() -> None:
    assert function_app.app._auth_level == func.AuthLevel.ANONYMOUS


@pytest.mark.usefixtures("no_db")
@pytest.mark.parametrize(("method", "route", "handler"), ROUTES, ids=ROUTE_IDS)
def test_no_principal_header_is_401_on_every_route(method: str, route: str, handler: Any) -> None:
    response = handler(_request(method, route))
    assert response.status_code == 401
    assert json.loads(response.get_body()) == {"error": "authentication required"}


MALFORMED_HEADERS = {
    "empty": "",
    "not-base64": "%%%not base64%%%",
    "base64-not-json": base64.b64encode(b"not json").decode(),
    "base64-not-utf8": base64.b64encode(b"\xff\xfe\xfa").decode(),
    "json-list": encode_principal(["customer"]),
    "json-null": encode_principal(None),
    "missing-userDetails": encode_principal({"identityProvider": "aad", "userRoles": ["customer"]}),
    "blank-userDetails": encode_principal(
        {"identityProvider": "aad", "userDetails": "  ", "userRoles": ["customer"]}
    ),
    "missing-provider": encode_principal({"userDetails": TEST_EMAIL, "userRoles": ["customer"]}),
    "roles-not-list": encode_principal(
        {"identityProvider": "aad", "userDetails": TEST_EMAIL, "userRoles": "customer"}
    ),
    "roles-not-strings": encode_principal(
        {"identityProvider": "aad", "userDetails": TEST_EMAIL, "userRoles": [["customer"]]}
    ),
    "email-not-string": encode_principal(
        {"identityProvider": "aad", "userDetails": ["x"], "userRoles": ["customer"]}
    ),
}


@pytest.mark.usefixtures("no_db", "lookup_must_not_run")
@pytest.mark.parametrize("value", MALFORMED_HEADERS.values(), ids=MALFORMED_HEADERS.keys())
@pytest.mark.parametrize(("method", "route", "handler"), ROUTES, ids=ROUTE_IDS)
def test_malformed_principal_is_401_on_every_route(method: str, route: str, handler: Any, value: str) -> None:
    response = handler(_request(method, route, {"x-ms-client-principal": value}))
    assert response.status_code == 401


@pytest.mark.usefixtures("no_db", "lookup_must_not_run")
@pytest.mark.parametrize(
    "roles",
    [[], ["anonymous"], ["anonymous", "authenticated"], ["Customer"], ["customers"], ["admin"]],
    ids=["none", "anonymous", "authenticated-only", "wrong-case", "near-miss", "other-role"],
)
@pytest.mark.parametrize(("method", "route", "handler"), ROUTES, ids=ROUTE_IDS)
def test_valid_header_without_customer_role_is_403_before_any_lookup(
    method: str, route: str, handler: Any, roles: list[str]
) -> None:
    """The listed user (TEST_EMAIL) is refused without the invite-only role, and the
    client_users lookup never runs."""
    response = handler(_request(method, route, customer_headers(roles=roles)))
    assert response.status_code == 403


@pytest.mark.usefixtures("no_db")
@pytest.mark.parametrize(("method", "route", "handler"), ROUTES, ids=ROUTE_IDS)
def test_customer_role_but_not_in_client_users_is_403_on_every_route(
    method: str, route: str, handler: Any
) -> None:
    response = handler(_request(method, route, customer_headers("stranger@example.test")))
    assert response.status_code == 403
    assert json.loads(response.get_body()) == {"error": "forbidden"}


@pytest.mark.usefixtures("no_db")
@pytest.mark.parametrize(("method", "route", "handler"), ROUTES, ids=ROUTE_IDS)
def test_listed_email_from_a_different_provider_is_403(method: str, route: str, handler: Any) -> None:
    response = handler(_request(method, route, customer_headers(provider="github")))
    assert response.status_code == 403


def test_parse_principal_normalizes_case_and_whitespace() -> None:
    header = customer_headers("  Lauren@AppAdino.TEST ", provider="AAD")["x-ms-client-principal"]
    principal = function_app._parse_principal(header)
    assert principal == function_app.Principal(
        "aad", TEST_EMAIL, frozenset({"anonymous", "authenticated", "customer"})
    )


def test_authenticate_returns_caller_from_lookup_not_request(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, str]] = []

    def lookup(identity_provider: str, email: str) -> int | None:
        seen.append((identity_provider, email))
        return 7

    monkeypatch.setattr(function_app, "_lookup_client_id", lookup)
    request = func.HttpRequest(
        method="GET",
        url="/api/prospects",
        params={"client_id": "2"},
        body=b"",
        headers=customer_headers("Lauren@AppAdino.test"),
    )
    caller = function_app._authenticate(request)
    assert caller == function_app.Caller(client_id=7, email=TEST_EMAIL)
    assert seen == [("aad", TEST_EMAIL)]


# --- LOCAL-ONLY operator bypass guard -----------------------------------------------------

CORE_TOOLS_ENV = {
    "FUNCTIONS_CORETOOLS_ENVIRONMENT": "true",
    "AZURE_FUNCTIONS_ENVIRONMENT": "Development",
}
OPERATOR_ON = {
    function_app.LOCAL_OPERATOR_ENV: function_app.LOCAL_OPERATOR_ENABLE_VALUE,
    function_app.LOCAL_OPERATOR_EMAIL_ENV: "duan@appadino.test",
}


def test_operator_mode_off_by_default_even_under_core_tools() -> None:
    assert function_app._local_operator_mode({}) is False
    assert function_app._local_operator_mode(CORE_TOOLS_ENV) is False


def test_operator_mode_on_only_with_exact_value_under_core_tools() -> None:
    assert function_app._local_operator_mode({**CORE_TOOLS_ENV, **OPERATOR_ON}) is True


@pytest.mark.parametrize("value", ["1", "true", "yes", "local-only-skips-login ", "LOCAL-ONLY-SKIPS-LOGIN"])
def test_operator_mode_wrong_value_refuses_to_start(value: str) -> None:
    env = {**CORE_TOOLS_ENV, **OPERATOR_ON, function_app.LOCAL_OPERATOR_ENV: value}
    with pytest.raises(RuntimeError, match="refusing to start"):
        function_app._local_operator_mode(env)


@pytest.mark.parametrize("marker", function_app.AZURE_HOST_MARKERS)
def test_operator_mode_in_azure_refuses_to_start(marker: str) -> None:
    """Even with every Core Tools marker faked, an Azure host marker means no app at
    all, not an app without login."""
    env = {**CORE_TOOLS_ENV, **OPERATOR_ON, marker: "abc123"}
    with pytest.raises(RuntimeError, match="looks like Azure"):
        function_app._local_operator_mode(env)


@pytest.mark.parametrize(
    "env",
    [
        {"AZURE_FUNCTIONS_ENVIRONMENT": "Development"},
        {"FUNCTIONS_CORETOOLS_ENVIRONMENT": "true"},
        {"FUNCTIONS_CORETOOLS_ENVIRONMENT": "true", "AZURE_FUNCTIONS_ENVIRONMENT": "Production"},
        {},
    ],
    ids=["no-coretools", "no-development", "production", "nothing"],
)
def test_operator_mode_outside_core_tools_refuses_to_start(env: dict[str, str]) -> None:
    with pytest.raises(RuntimeError, match="only works under `func start`"):
        function_app._local_operator_mode({**env, **OPERATOR_ON})


def test_operator_mode_needs_an_operator_email() -> None:
    env = {**CORE_TOOLS_ENV, function_app.LOCAL_OPERATOR_ENV: function_app.LOCAL_OPERATOR_ENABLE_VALUE}
    with pytest.raises(RuntimeError, match=function_app.LOCAL_OPERATOR_EMAIL_ENV):
        function_app._local_operator_mode(env)


def test_operator_client_header_is_ignored_when_mode_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(function_app, "_conn", _db_must_not_be_touched)
    request = func.HttpRequest(
        method="GET", url="/api/prospects", body=b"", headers={"x-local-operator-client-id": "2"}
    )
    response = function_app.list_prospects(request)
    assert response.status_code == 401


def test_operator_mode_takes_client_from_operator_header(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(function_app, "LOCAL_OPERATOR_MODE", True)
    monkeypatch.setenv(function_app.LOCAL_OPERATOR_EMAIL_ENV, "duan@appadino.test")
    ok = func.HttpRequest(
        method="GET", url="/api/prospects", body=b"", headers={"x-local-operator-client-id": "3"}
    )
    assert function_app._authenticate(ok) == function_app.Caller(client_id=3, email="duan@appadino.test")
    for bad in ["", "0", "-1", "abc", "2; DROP"]:
        request = func.HttpRequest(
            method="GET", url="/api/prospects", body=b"", headers={"x-local-operator-client-id": bad}
        )
        response = function_app._authenticate(request)
        assert isinstance(response, func.HttpResponse) and response.status_code == 400


def test_clients_listing_is_404_when_mode_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(function_app, "_conn", _db_must_not_be_touched)
    response = function_app.list_clients_local_operator(
        func.HttpRequest(method="GET", url="/api/clients", body=b"", headers=customer_headers())
    )
    assert response.status_code == 404
