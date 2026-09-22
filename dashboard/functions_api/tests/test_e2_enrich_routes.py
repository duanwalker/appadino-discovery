"""E2 — per-prospect Enrich button routes (§5.5, hard rule 8). Synthetic rows and a
tiny queued-fetchone fake DB connection, same pattern as test_g2_dashboard_gaps.py's
RouteConnection but able to return different results across the multi-step
select-guard / submit-or-poll / persist flow these routes go through.
"""

from __future__ import annotations

import json
from contextlib import AbstractContextManager
from typing import Any

import azure.functions as func
import pytest
from discovery.clients.enrichment_provider import (
    EnrichmentResult,
    PollOutcome,
    PollResult,
)
from function_app import enrich_prospect, enrich_status


def _request(
    method: str,
    url: str,
    *,
    params: dict[str, str] | None = None,
    route_params: dict[str, str] | None = None,
) -> func.HttpRequest:
    return func.HttpRequest(method=method, url=url, params=params or {}, route_params=route_params or {}, body=b"{}")


def _json_body(response: func.HttpResponse) -> dict[str, Any]:
    return json.loads(response.get_body().decode())


def _candidate_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "id": 42,
        "ein": "010343943",
        "client_id": 2,
        "status": "approved",
        "website": "https://synthetic.org",
        "officers": [{"name": "Jane Smith", "title": "Executive Director", "is_officer": True}],
        "icp_config": {"enrichment_enabled": True, "fullenrich_subaccount_id": "sub-1"},
    }
    row.update(overrides)
    return row


def _result(email_status: str = "verified") -> EnrichmentResult:
    return EnrichmentResult(
        contact_name="Jane Smith",
        contact_title="Executive Director",
        email="jsmith@synthetic.org",
        email_status=email_status,
        stale_detail=None,
        phone=None,
        linkedin_url=None,
        provider_confidence=95,
        credits_spent=1,
        raw={"status": "FINISHED"},
    )


def _provider_must_not_be_called(subaccount_id: str | None) -> None:
    raise AssertionError("provider should not be constructed when the guard blocks or the prospect isn't found")


class _StubProvider:
    def __init__(self, poll_results: list[PollResult]) -> None:
        self.poll_results = poll_results
        self.submit_calls: list[Any] = []
        self.poll_calls: list[tuple[str, str | None]] = []
        self.name = "stub"

    def submit(self, candidate: Any) -> str:
        self.submit_calls.append(candidate)
        return "job-xyz"

    def poll(self, job_id: str, org_domain: str | None) -> PollResult:
        self.poll_calls.append((job_id, org_domain))
        idx = min(len(self.poll_calls) - 1, len(self.poll_results) - 1)
        return self.poll_results[idx]


class ExecutedStatement:
    def __init__(self, sql: str, params: Any) -> None:
        self.sql = " ".join(sql.split())
        self.params = params


class QueuedConnection(AbstractContextManager["QueuedConnection"]):
    """Returns fetchone results in order across however many SELECTs/INSERTs a
    route issues — the fixed single-fetchone RouteConnection elsewhere in this
    package isn't expressive enough for the enrich routes' multi-step flow
    (candidate lookup, then a dedupe check, then an INSERT ... RETURNING)."""

    def __init__(self, fetchone_queue: list[dict[str, Any] | None] | None = None) -> None:
        self.fetchone_queue = list(fetchone_queue or [])
        self.executed: list[ExecutedStatement] = []
        self.commits = 0

    def cursor(self) -> QueuedCursor:
        return QueuedCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        return None


class QueuedCursor(AbstractContextManager["QueuedCursor"]):
    def __init__(self, connection: QueuedConnection) -> None:
        self.connection = connection

    def execute(self, sql: str, params: Any = None) -> None:
        self.connection.executed.append(ExecutedStatement(sql, params))

    def fetchone(self) -> dict[str, Any] | None:
        if not self.connection.fetchone_queue:
            return None
        return self.connection.fetchone_queue.pop(0)

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        return None


class TestEnrichProspectGuard:
    def test_blocked_when_not_approved_never_constructs_provider(self, monkeypatch: pytest.MonkeyPatch) -> None:
        conn = QueuedConnection(fetchone_queue=[_candidate_row(status="new")])
        monkeypatch.setattr("function_app._conn", lambda: conn)
        monkeypatch.setattr("function_app._get_fullenrich_provider", _provider_must_not_be_called)

        response = enrich_prospect(_request("POST", "/api/prospects/42/enrich", route_params={"id": "42"}))

        assert response.status_code == 403
        assert _json_body(response) == {"error": "prospect is not approved"}

    def test_blocked_when_tenant_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        conn = QueuedConnection(fetchone_queue=[_candidate_row(icp_config={"enrichment_enabled": False})])
        monkeypatch.setattr("function_app._conn", lambda: conn)
        monkeypatch.setattr("function_app._get_fullenrich_provider", _provider_must_not_be_called)

        response = enrich_prospect(_request("POST", "/api/prospects/42/enrich", route_params={"id": "42"}))

        assert response.status_code == 403
        assert _json_body(response) == {"error": "enrichment is not enabled for this tenant"}

    def test_blocked_when_subaccount_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        row = _candidate_row(icp_config={"enrichment_enabled": True, "fullenrich_subaccount_id": None})
        conn = QueuedConnection(fetchone_queue=[row])
        monkeypatch.setattr("function_app._conn", lambda: conn)
        monkeypatch.setattr("function_app._get_fullenrich_provider", _provider_must_not_be_called)

        response = enrich_prospect(_request("POST", "/api/prospects/42/enrich", route_params={"id": "42"}))

        assert response.status_code == 403
        assert _json_body(response) == {"error": "tenant has no fullenrich_subaccount_id configured"}

    def test_prospect_not_found_returns_404(self, monkeypatch: pytest.MonkeyPatch) -> None:
        conn = QueuedConnection(fetchone_queue=[None])
        monkeypatch.setattr("function_app._conn", lambda: conn)
        monkeypatch.setattr("function_app._get_fullenrich_provider", _provider_must_not_be_called)

        response = enrich_prospect(_request("POST", "/api/prospects/999/enrich", route_params={"id": "999"}))

        assert response.status_code == 404

    def test_missing_api_key_returns_500_after_guard_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        conn = QueuedConnection(fetchone_queue=[_candidate_row()])
        monkeypatch.setattr("function_app._conn", lambda: conn)
        monkeypatch.setattr("function_app._get_fullenrich_provider", lambda subaccount_id: None)

        response = enrich_prospect(_request("POST", "/api/prospects/42/enrich", route_params={"id": "42"}))

        assert response.status_code == 500

    def test_no_usable_officer_returns_422(self, monkeypatch: pytest.MonkeyPatch) -> None:
        conn = QueuedConnection(fetchone_queue=[_candidate_row(officers=[])])
        monkeypatch.setattr("function_app._conn", lambda: conn)
        provider = _StubProvider(poll_results=[PollResult(outcome=PollOutcome.DONE, result=_result())])
        monkeypatch.setattr("function_app._get_fullenrich_provider", lambda subaccount_id: provider)

        response = enrich_prospect(_request("POST", "/api/prospects/42/enrich", route_params={"id": "42"}))

        assert response.status_code == 422
        assert provider.submit_calls == []


class TestEnrichProspectOutcomes:
    def test_done_within_budget_persists_and_returns_200(self, monkeypatch: pytest.MonkeyPatch) -> None:
        conn = QueuedConnection(
            fetchone_queue=[
                _candidate_row(),
                {"claimed": True},  # INSERT enrichment_jobs ... RETURNING id (claim won)
                None,  # dedupe SELECT: no existing row for this job_id
                {"id": 7},  # INSERT ... RETURNING id
            ]
        )
        monkeypatch.setattr("function_app._conn", lambda: conn)
        provider = _StubProvider(poll_results=[PollResult(outcome=PollOutcome.DONE, result=_result())])
        monkeypatch.setattr("function_app._get_fullenrich_provider", lambda subaccount_id: provider)

        response = enrich_prospect(_request("POST", "/api/prospects/42/enrich", route_params={"id": "42"}))

        assert response.status_code == 200
        body = _json_body(response)
        assert body["status"] == "done"
        assert body["enrichment_id"] == 7
        assert body["contact"]["email"] == "jsmith@synthetic.org"
        assert any("INSERT INTO enrichments" in s.sql for s in conn.executed)
        assert any("DELETE FROM enrichment_jobs" in s.sql for s in conn.executed)
        assert conn.commits >= 1

    def test_pending_after_budget_returns_202_with_job_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        conn = QueuedConnection(fetchone_queue=[_candidate_row(), {"claimed": True}])
        monkeypatch.setattr("function_app._conn", lambda: conn)
        provider = _StubProvider(poll_results=[PollResult(outcome=PollOutcome.PENDING)])
        monkeypatch.setattr("function_app._get_fullenrich_provider", lambda subaccount_id: provider)
        monkeypatch.setattr("function_app.ENRICH_POLL_ATTEMPTS", 1)
        monkeypatch.setattr("function_app.ENRICH_POLL_INTERVAL_S", 0)

        response = enrich_prospect(_request("POST", "/api/prospects/42/enrich", route_params={"id": "42"}))

        assert response.status_code == 202
        assert _json_body(response) == {"status": "pending", "job_id": "job-xyz"}
        assert not any("INSERT INTO enrichments" in s.sql for s in conn.executed)

    def test_double_submit_blocked_by_db_claim_when_in_process_cache_is_cold(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Simulates a second Functions instance racing the same enrich call — no
        shared _PENDING_JOBS cache, so the real guarantee has to be the DB-level
        UNIQUE(client_id, ein) claim, not just the same-process fast path."""
        conn = QueuedConnection(
            fetchone_queue=[
                _candidate_row(),
                None,  # INSERT ... ON CONFLICT DO NOTHING RETURNING id -> conflict
                {"job_id": "job-elsewhere"},  # SELECT job_id from the existing claim
            ]
        )
        monkeypatch.setattr("function_app._conn", lambda: conn)
        provider = _StubProvider(poll_results=[PollResult(outcome=PollOutcome.PENDING)])
        monkeypatch.setattr("function_app._get_fullenrich_provider", lambda subaccount_id: provider)

        response = enrich_prospect(_request("POST", "/api/prospects/42/enrich", route_params={"id": "42"}))

        assert response.status_code == 202
        assert _json_body(response) == {"status": "pending", "job_id": "job-elsewhere"}
        assert provider.submit_calls == []

    def test_done_result_is_idempotent_on_job_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A double-click (or a status-poll racing the initial submit) must not
        double-insert or double-count credits for the same FullEnrich job."""
        conn = QueuedConnection(
            fetchone_queue=[
                _candidate_row(),
                {"claimed": True},  # INSERT enrichment_jobs ... RETURNING id (claim won)
                {"id": 7},  # dedupe SELECT: a row for this job_id already exists
            ]
        )
        monkeypatch.setattr("function_app._conn", lambda: conn)
        provider = _StubProvider(poll_results=[PollResult(outcome=PollOutcome.DONE, result=_result())])
        monkeypatch.setattr("function_app._get_fullenrich_provider", lambda subaccount_id: provider)

        response = enrich_prospect(_request("POST", "/api/prospects/42/enrich", route_params={"id": "42"}))

        assert response.status_code == 200
        assert _json_body(response)["enrichment_id"] == 7
        assert not any("INSERT INTO enrichments" in s.sql for s in conn.executed)


class TestEnrichStatus:
    def test_missing_job_id_returns_400_without_db(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _must_not_be_called() -> None:
            raise AssertionError("no DB call should happen before job_id is validated")

        monkeypatch.setattr("function_app._conn", _must_not_be_called)

        response = enrich_status(_request("GET", "/api/prospects/42/enrich-status", route_params={"id": "42"}))

        assert response.status_code == 400

    def test_guard_reblocked_on_resume_if_status_changed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        conn = QueuedConnection(fetchone_queue=[_candidate_row(status="rejected")])
        monkeypatch.setattr("function_app._conn", lambda: conn)
        monkeypatch.setattr("function_app._get_fullenrich_provider", _provider_must_not_be_called)

        response = enrich_status(
            _request(
                "GET", "/api/prospects/42/enrich-status", params={"job_id": "job-xyz"}, route_params={"id": "42"}
            )
        )

        assert response.status_code == 403

    def test_resolves_and_persists_on_resume(self, monkeypatch: pytest.MonkeyPatch) -> None:
        conn = QueuedConnection(fetchone_queue=[_candidate_row(), None, {"id": 9}])
        monkeypatch.setattr("function_app._conn", lambda: conn)
        provider = _StubProvider(poll_results=[PollResult(outcome=PollOutcome.DONE, result=_result())])
        monkeypatch.setattr("function_app._get_fullenrich_provider", lambda subaccount_id: provider)

        response = enrich_status(
            _request(
                "GET", "/api/prospects/42/enrich-status", params={"job_id": "job-xyz"}, route_params={"id": "42"}
            )
        )

        assert response.status_code == 200
        assert _json_body(response)["enrichment_id"] == 9
        assert provider.poll_calls == [("job-xyz", "synthetic.org")]

    def test_still_pending_returns_202(self, monkeypatch: pytest.MonkeyPatch) -> None:
        conn = QueuedConnection(fetchone_queue=[_candidate_row()])
        monkeypatch.setattr("function_app._conn", lambda: conn)
        provider = _StubProvider(poll_results=[PollResult(outcome=PollOutcome.PENDING)])
        monkeypatch.setattr("function_app._get_fullenrich_provider", lambda subaccount_id: provider)

        response = enrich_status(
            _request(
                "GET", "/api/prospects/42/enrich-status", params={"job_id": "job-xyz"}, route_params={"id": "42"}
            )
        )

        assert response.status_code == 202
