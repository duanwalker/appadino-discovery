"""Independent, offline FullEnrich v2 contract and billing-safety tests.

Source: https://docs.fullenrich.com/api/v2/reference/openapi.yaml
Fetched with curl on 2026-10-29, via https://docs.fullenrich.com/llms.txt.
Neither project venv has jsonschema or a YAML parser. Request checks below are
manually transcribed from RequestPostContactBulkEnrich and its Contact schema;
they do not import the adapter's field lists or status mappings. The external
scratch spec is deliberately not a test dependency or a vendored repository file.
"""

from __future__ import annotations

import copy
import json
import logging
import re
import socket
from collections.abc import Iterator
from typing import Any

import function_app
import httpx
import pytest
from discovery.clients.enrichment_provider import EnrichmentCandidate, PollOutcome
from discovery.clients.fullenrich_adapter import FullEnrichProvider
from discovery.stages.enrich import EnrichAttempt, start_enrichment
from test_e2_enrich_routes import QueuedConnection, _candidate_row, _json_body, _request

API_KEY = "independent-synthetic-api-key-not-a-real-secret"
JOB_ID = "2db5ea61-1752-42cf-8ea1-ab1da060cd0a"
BATCH_STATUSES = (
    "CREATED", "IN_PROGRESS", "CANCELED", "CREDITS_INSUFFICIENT",
    "FINISHED", "RATE_LIMIT", "UNKNOWN",
)
EMAIL_STATUSES = (
    "DELIVERABLE", "HIGH_PROBABILITY", "CATCH_ALL", "INVALID", "INVALID_DOMAIN",
)
FAILED_STATUSES = ("CANCELED", "CREDITS_INSUFFICIENT", "RATE_LIMIT", "UNKNOWN")
# Independent of the app's own redaction pattern on purpose. The bare word "bearer"
# is allowed (FullEnrich's documented 401 message contains it); a token after it is not.
BEARER_TOKEN = re.compile(r"\bbearer\s+[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE)


def assert_no_credentials(text: str) -> None:
    assert API_KEY not in text
    assert not BEARER_TOKEN.search(text), BEARER_TOKEN.search(text)


def published_example() -> dict[str, Any]:
    """Contact-bearing projection of GET's 200 example, with values unchanged.

    Unused profile biography/employment and duplicate email/phone arrays omitted.
    The batch cost (14) is copied verbatim, not recomputed from our field prices.
    """
    return {
        "id": JOB_ID,
        "name": "Sales Operations in London",
        "status": "FINISHED",
        "cost": {"credits": 14},
        "data": [{
            "input": {
                "first_name": "John", "last_name": "Snow",
                "company_domain": "example.com", "company_name": "Example Inc",
                "professional_network_url": "https://www.linkedin.com/in/demoge/",
            },
            "custom": {"user_id": "12584"},
            "contact_info": {
                "most_probable_work_email": {
                    "email": "john.snow@example.com", "status": "DELIVERABLE",
                },
                "most_probable_personal_email": {
                    "email": "johnsnow@gmail.com", "status": "DELIVERABLE",
                },
                "most_probable_phone": {
                    "number": "+1 555-123-4567", "region": "US",
                    "line_type": "MOBILE", "line_status": "ACTIVE",
                    "ownership_match": "CONFIRMED", "ownership_match_confidence": 90,
                    "connect_rate": "HIGHEST",
                },
            },
            "profile": {
                "id": "746e4816-19c8-54d8-b558-65a5a52cc85c",
                "full_name": "John Snow", "first_name": "John", "last_name": "Snow",
                "social_profiles": {"professional_network": {
                    "id": 1234, "url": "https://www.linkedin.com/in/john-doe",
                    "handle": "john-doe", "connection_count": 500,
                }},
            },
        }],
    }


def assert_request_contract(body: Any) -> None:
    """Manual contract check, additionally rejecting unknown keys to catch typos.

    The schema requires name/data only; contact requirements are documented by
    POST's 400 examples, not a Contact.required list. The custom description says
    20 entries, while the error example says 10; this request uses only one.
    """
    assert isinstance(body, dict)
    assert {"name", "data"} <= body.keys()
    assert body.keys() <= {"name", "data", "webhook_url", "webhook_events"}
    assert isinstance(body["name"], str) and body["name"]
    assert isinstance(body["data"], list) and len(body["data"]) == 1
    contact = body["data"][0]
    assert isinstance(contact, dict)
    allowed = {
        "first_name", "last_name", "domain", "company_name", "linkedin_url",
        "enrich_fields", "custom",
    }
    assert contact.keys() <= allowed
    assert {"first_name", "last_name", "domain", "enrich_fields"} <= contact.keys()
    for field in ("first_name", "last_name", "domain"):
        assert isinstance(contact[field], str) and contact[field]
    for field in ("company_name", "linkedin_url"):
        if field in contact:
            assert isinstance(contact[field], str)
    fields = contact["enrich_fields"]
    assert isinstance(fields, list) and fields
    for value in fields:
        assert isinstance(value, str)
        assert value in {"contact.work_emails", "contact.phones", "contact.personal_emails"}
    custom = contact.get("custom", {})
    assert isinstance(custom, dict) and len(custom) <= 10
    assert all(isinstance(key, str) and isinstance(value, str) for key, value in custom.items())
    assert all(len(value) <= 100 for value in custom.values())


class OfflineWire:
    def __init__(self) -> None:
        self.responses: list[httpx.Response | str] = []
        self.requests: list[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert self.responses, f"Unexpected offline request: {request.method} {request.url}"
        response = self.responses.pop(0)
        if response == "timeout":
            raise httpx.ReadTimeout("synthetic timeout", request=request)
        if response == "transport":
            raise httpx.ReadError("synthetic connection lost after send", request=request)
        if response == "malformed":
            return httpx.Response(200, content=b"not-json")
        assert isinstance(response, httpx.Response)
        return response


@pytest.fixture(autouse=True)
def wire(monkeypatch: pytest.MonkeyPatch) -> Iterator[OfflineWire]:
    offline = OfflineWire()
    original_init = httpx.Client.__init__

    def init(client: httpx.Client, *args: Any, **kwargs: Any) -> None:
        kwargs["transport"] = httpx.MockTransport(offline.handle)
        original_init(client, *args, **kwargs)

    def deny_network(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Live network access is forbidden in these tests")

    monkeypatch.setattr(httpx.Client, "__init__", init)
    monkeypatch.setattr(socket.socket, "connect", deny_network)
    monkeypatch.setattr(socket, "getaddrinfo", deny_network)
    monkeypatch.setenv("FULLENRICH_API_KEY", API_KEY)
    monkeypatch.setattr(function_app, "ENRICH_POLL_ATTEMPTS", 1)
    monkeypatch.setattr(function_app, "ENRICH_POLL_INTERVAL_S", 0)
    yield offline


def candidate() -> EnrichmentCandidate:
    return EnrichmentCandidate("010343943", "John", "Snow", "Director", "example.com")


def start(provider: FullEnrichProvider) -> EnrichAttempt:
    return start_enrichment(
        provider, "010343943", "example.com", {"name": "John Snow", "title": "Director"},
        status="approved", enrichment_enabled=True, fullenrich_subaccount_id="sub-test",
        poll_attempts=1, poll_interval_s=0,
    )


def route_connection(monkeypatch: pytest.MonkeyPatch) -> QueuedConnection:
    conn = QueuedConnection([_candidate_row(website="https://example.com"), {"id": 1}])
    monkeypatch.setattr(function_app, "_conn", lambda: conn)
    return conn


def failure_response(kind: str) -> httpx.Response | str:
    if kind == "500":
        return httpx.Response(500, json={"code": "error.internal", "message": "try later"})
    return kind


def test_real_submit_body_satisfies_published_contract(wire: OfflineWire) -> None:
    wire.responses.append(httpx.Response(200, json={"enrichment_id": JOB_ID}))
    assert FullEnrichProvider(API_KEY).submit(candidate()) == JOB_ID
    assert len(wire.requests) == 1
    request = wire.requests[0]
    assert request.method == "POST"
    assert str(request.url) == "https://app.fullenrich.com/api/v2/contact/enrich/bulk"
    assert request.headers["content-type"] == "application/json"
    assert request.headers["authorization"] == f"Bearer {API_KEY}"
    body = json.loads(request.content)
    assert_request_contract(body)
    assert body["data"][0]["custom"] == {"ein": "010343943"}
    assert body["data"][0]["first_name"] == "John"
    assert body["data"][0]["last_name"] == "Snow"


@pytest.mark.parametrize("mutation", [
    "firstname", "lastname", "missing_name", "missing_data", "wrong_name_type",
    "wrong_data_type", "wrong_first_name_type", "wrong_domain_type", "bad_enum",
    "numeric_custom", "missing_enrich_fields",
])
def test_manual_checker_rejects_contract_violations(mutation: str) -> None:
    body: dict[str, Any] = {
        "name": "test", "data": [{
            "first_name": "John", "last_name": "Snow", "domain": "example.com",
            "enrich_fields": ["contact.work_emails", "contact.phones"], "custom": {"ein": "1"},
        }],
    }
    contact = body["data"][0]
    if mutation in {"firstname", "lastname"}:
        correct = "first_name" if mutation == "firstname" else "last_name"
        contact[mutation] = contact.pop(correct)
    elif mutation.startswith("missing_"):
        key = mutation.removeprefix("missing_")
        del (contact if key == "enrich_fields" else body)[key]
    elif mutation == "wrong_name_type":
        body["name"] = 1
    elif mutation == "wrong_data_type":
        body["data"] = {}
    elif mutation == "wrong_first_name_type":
        contact["first_name"] = 1
    elif mutation == "wrong_domain_type":
        contact["domain"] = None
    elif mutation == "bad_enum":
        contact["enrich_fields"] = ["contact.emails"]
    else:
        contact["custom"]["ein"] = 1
    with pytest.raises(AssertionError):
        assert_request_contract(body)


@pytest.mark.parametrize("status", BATCH_STATUSES)
def test_every_published_batch_status_observed_behavior(wire: OfflineWire, status: str) -> None:
    body = published_example()
    body["status"] = status
    wire.responses.append(httpx.Response(200, json=body))
    result = FullEnrichProvider(API_KEY).poll(JOB_ID, "example.com")
    if status in {"CREATED", "IN_PROGRESS"}:
        expected = PollOutcome.PENDING
    elif status in FAILED_STATUSES:
        expected = PollOutcome.FAILED  # even with data present: nothing is recorded
    else:
        expected = PollOutcome.DONE
    assert result.outcome is expected
    if expected is PollOutcome.FAILED:
        assert result.result is None
        assert result.provider_status == status
    if expected is PollOutcome.DONE:
        assert result.result is not None
        assert result.result.email == "john.snow@example.com"
        assert result.result.phone == "+1 555-123-4567"
        assert result.result.linkedin_url == "https://www.linkedin.com/in/john-doe"
        assert result.result.provider_confidence == 90
        assert result.result.raw == body["data"][0]


@pytest.mark.parametrize("status", FAILED_STATUSES)
def test_failed_batch_must_not_be_reported_as_success(wire: OfflineWire, status: str) -> None:
    body = published_example()
    body.update(status=status, data=[])
    wire.responses.append(httpx.Response(200, json=body))
    result = FullEnrichProvider(API_KEY).poll(JOB_ID, "example.com")
    assert result.outcome is not PollOutcome.DONE, f"Provider failure {status} became a successful no-match"


def test_documented_in_progress_400_is_pending(wire: OfflineWire) -> None:
    wire.responses.append(httpx.Response(400, json={
        "code": "error.enrichment.in_progress",
        "message": "Enrichment not ready, try again in 30 seconds",
    }))
    assert FullEnrichProvider(API_KEY).poll(JOB_ID, "example.com").outcome is PollOutcome.PENDING


def test_documented_402_is_an_http_error_not_a_no_match(wire: OfflineWire) -> None:
    body = published_example()
    body.update(status="CREDITS_INSUFFICIENT", data=[])
    wire.responses.append(httpx.Response(402, json=body))
    with pytest.raises(httpx.HTTPStatusError) as error:
        FullEnrichProvider(API_KEY).poll(JOB_ID, "example.com")
    assert error.value.response.status_code == 402


@pytest.mark.parametrize("status,expected", tuple(zip(
    EMAIL_STATUSES, ("verified", "catch_all", "catch_all", "not_found", "not_found"), strict=True,
)))
def test_every_email_status_with_nullable_phone(
    wire: OfflineWire, status: str, expected: str,
) -> None:
    body = published_example()
    info = body["data"][0]["contact_info"]
    info["most_probable_phone"] = None
    info["most_probable_work_email"]["status"] = status
    wire.responses.append(httpx.Response(200, json=body))
    result = FullEnrichProvider(API_KEY).poll(JOB_ID, "example.com").result
    assert result is not None
    assert result.email_status == expected
    assert result.phone is None
    assert result.email == "john.snow@example.com"


@pytest.mark.parametrize("phone_present", [False, True])
def test_missing_work_email_never_uses_personal_email_or_constructs_one(
    wire: OfflineWire, phone_present: bool,
) -> None:
    body = published_example()
    info = body["data"][0]["contact_info"]
    del info["most_probable_work_email"]
    if not phone_present:
        info["most_probable_phone"] = None
    wire.responses.append(httpx.Response(200, json=body))
    result = FullEnrichProvider(API_KEY).poll(JOB_ID, "example.com").result
    assert result is not None
    assert result.email is None
    assert result.email_status == "not_found", "Phone-only result must not persist a NULL email status"


def test_finished_empty_data_is_no_match(wire: OfflineWire) -> None:
    body = published_example()
    body["data"] = []
    wire.responses.append(httpx.Response(200, json=body))
    result = FullEnrichProvider(API_KEY).poll(JOB_ID, "example.com")
    assert result.outcome is PollOutcome.DONE and result.result is not None
    assert result.result.email is None and result.result.phone is None
    assert result.result.email_status == "not_found"
    assert result.result.credits_spent == 0


def test_unexpected_extra_fields_are_preserved_without_changing_contact(wire: OfflineWire) -> None:
    body = published_example()
    body["future_batch_field"] = {"version": 3}
    body["data"][0]["future_contact_field"] = ["opaque"]
    before = copy.deepcopy(body)
    wire.responses.append(httpx.Response(200, json=body))
    result = FullEnrichProvider(API_KEY).poll(JOB_ID, "example.com").result
    assert result is not None and result.email == "john.snow@example.com"
    assert result.raw == before["data"][0]


@pytest.mark.xfail(
    strict=True,
    reason="Deferred post-v1: credits_spent should come from the response's cost.credits "
    "when present (the spec example shows 14); today it is estimated as 1 per email plus "
    "10 per phone.",
)
def test_published_batch_credit_cost_is_not_silently_reestimated(wire: OfflineWire) -> None:
    body = published_example()
    wire.responses.append(httpx.Response(200, json=body))
    result = FullEnrichProvider(API_KEY).poll(JOB_ID, "example.com").result
    assert result is not None
    assert result.credits_spent == body["cost"]["credits"]


@pytest.mark.parametrize("kind", ["timeout", "500", "malformed", "transport"])
def test_first_poll_failure_keeps_paid_job_id(wire: OfflineWire, kind: str) -> None:
    wire.responses.extend([
        httpx.Response(200, json={"enrichment_id": JOB_ID}), failure_response(kind),
    ])
    attempt = start(FullEnrichProvider(API_KEY))
    assert attempt.status == "pending" and attempt.job_id == JOB_ID
    assert [request.method for request in wire.requests] == ["POST", "GET"]


@pytest.mark.parametrize("kind", ["timeout", "500", "malformed", "transport"])
def test_route_poll_failure_keeps_claim_and_next_click_does_not_pay_again(
    monkeypatch: pytest.MonkeyPatch, wire: OfflineWire, kind: str,
) -> None:
    conn = route_connection(monkeypatch)
    wire.responses.extend([
        httpx.Response(200, json={"enrichment_id": JOB_ID}), failure_response(kind),
    ])
    response = function_app.enrich_prospect(
        _request("POST", "/api/prospects/42/enrich", route_params={"id": "42"}),
    )
    assert response.status_code == 202
    assert _json_body(response) == {"status": "pending", "job_id": JOB_ID}
    assert not any("DELETE FROM enrichment_jobs" in statement.sql for statement in conn.executed)
    assert any("UPDATE enrichment_jobs SET job_id" in statement.sql for statement in conn.executed)
    second_conn = route_connection(monkeypatch)
    second = function_app.enrich_prospect(
        _request("POST", "/api/prospects/42/enrich", route_params={"id": "42"}),
    )
    assert second.status_code == 202 and _json_body(second)["job_id"] == JOB_ID
    assert sum(request.method == "POST" for request in wire.requests) == 1
    assert not any("INSERT INTO enrichment_jobs" in statement.sql for statement in second_conn.executed)


@pytest.mark.xfail(
    strict=True,
    reason="Deferred post-v1: a network error on the submit call itself may leave a billed "
    "job we cannot see, because FullEnrich has no idempotency key; the claim is released "
    "and the next click submits (and pays) again.",
)
def test_submit_network_error_must_not_lead_to_a_second_paid_submit(
    monkeypatch: pytest.MonkeyPatch, wire: OfflineWire,
) -> None:
    # First-poll failures used to be parametrized here as observed behavior (claim
    # released, second submit). They now keep the job id: see
    # test_route_poll_failure_keeps_claim_and_next_click_does_not_pay_again.
    route_connection(monkeypatch)
    wire.responses.append("transport")
    with pytest.raises(httpx.ReadError):
        function_app.enrich_prospect(
            _request("POST", "/api/prospects/42/enrich", route_params={"id": "42"}),
        )
    route_connection(monkeypatch)
    wire.responses.extend([
        httpx.Response(200, json={"enrichment_id": JOB_ID}),
        httpx.Response(200, json={"status": "IN_PROGRESS"}),
    ])
    response = function_app.enrich_prospect(
        _request("POST", "/api/prospects/42/enrich", route_params={"id": "42"}),
    )
    assert response.status_code == 202
    assert sum(request.method == "POST" for request in wire.requests) == 1


def test_status_402_returns_readable_error_but_releases_paid_job_claim(
    monkeypatch: pytest.MonkeyPatch, wire: OfflineWire,
) -> None:
    conn = route_connection(monkeypatch)
    function_app._PENDING_JOBS[(2, "010343943")] = JOB_ID
    body = published_example()
    body.update(status="CREDITS_INSUFFICIENT", data=[])
    wire.responses.append(httpx.Response(402, json=body))
    response = function_app.enrich_status(_request(
        "GET", "/api/prospects/42/enrich-status", params={"job_id": JOB_ID},
        route_params={"id": "42"},
    ))
    assert response.status_code == 502
    assert _json_body(response)["provider_status"] == 402
    assert _json_body(response)["provider_code"] is None
    assert "HTTP 402" in _json_body(response)["error"]
    assert any("DELETE FROM enrichment_jobs" in statement.sql for statement in conn.executed)
    assert (2, "010343943") not in function_app._PENDING_JOBS


@pytest.mark.parametrize("phase", ["submit", "first_poll", "status"])
@pytest.mark.parametrize("kind", ["400", "401", "402", "404", "429", "500",
                                  "timeout", "transport", "malformed"])
def test_failure_logs_never_include_credentials_from_request_headers(
    monkeypatch: pytest.MonkeyPatch, wire: OfflineWire, caplog: pytest.LogCaptureFixture,
    phase: str, kind: str,
) -> None:
    route_connection(monkeypatch)
    if phase == "first_poll":
        wire.responses.append(httpx.Response(200, json={"enrichment_id": JOB_ID}))
    wire.responses.append(
        httpx.Response(int(kind), json={"code": "error.synthetic", "message": "failure"})
        if kind.isdecimal() else kind,
    )
    with caplog.at_level(logging.DEBUG):
        try:
            if phase == "status":
                function_app.enrich_status(_request(
                    "GET", "/api/prospects/42/enrich-status", params={"job_id": JOB_ID},
                    route_params={"id": "42"},
                ))
            else:
                function_app.enrich_prospect(_request(
                    "POST", "/api/prospects/42/enrich", route_params={"id": "42"},
                ))
        except (httpx.TransportError, ValueError):
            pass
    assert wire.requests
    assert all(request.headers["authorization"] == f"Bearer {API_KEY}" for request in wire.requests)
    assert_no_credentials(caplog.text)


@pytest.mark.parametrize("phase", ["submit", "first_poll", "status"])
def test_failure_logs_redact_credentials_echoed_by_provider(
    monkeypatch: pytest.MonkeyPatch, wire: OfflineWire, caplog: pytest.LogCaptureFixture,
    phase: str,
) -> None:
    route_connection(monkeypatch)
    if phase == "first_poll":
        wire.responses.append(httpx.Response(200, json={"enrichment_id": JOB_ID}))
    wire.responses.append(httpx.Response(500, json={
        "code": "error.internal", "message": f"upstream echoed Authorization: Bearer {API_KEY}",
    }))
    with caplog.at_level(logging.WARNING):
        if phase == "status":
            function_app.enrich_status(_request(
                "GET", "/api/prospects/42/enrich-status", params={"job_id": JOB_ID},
                route_params={"id": "42"},
            ))
        else:
            function_app.enrich_prospect(_request(
                "POST", "/api/prospects/42/enrich", route_params={"id": "42"},
            ))
    assert "upstream echoed" in caplog.text  # the failure is still logged, just redacted
    assert_no_credentials(caplog.text)


@pytest.mark.parametrize("phase", ["submit", "status"])
def test_documented_auth_error_logs_no_credentials(
    monkeypatch: pytest.MonkeyPatch, wire: OfflineWire, caplog: pytest.LogCaptureFixture,
    phase: str,
) -> None:
    route_connection(monkeypatch)
    wire.responses.append(httpx.Response(401, json={
        "code": "error.authorization.not_bearer",
        "message": "Authorization headers do not have prefix 'bearer'",
    }))
    with caplog.at_level(logging.WARNING):
        if phase == "status":
            response = function_app.enrich_status(_request(
                "GET", "/api/prospects/42/enrich-status", params={"job_id": JOB_ID},
                route_params={"id": "42"},
            ))
        else:
            response = function_app.enrich_prospect(_request(
                "POST", "/api/prospects/42/enrich", route_params={"id": "42"},
            ))
    assert response.status_code == 502
    assert _json_body(response)["provider_code"] == "error.authorization.not_bearer"
    # FullEnrich's own documented message (bare word "bearer", no token) stays readable.
    assert "do not have prefix 'bearer'" in caplog.text
    assert_no_credentials(caplog.text)