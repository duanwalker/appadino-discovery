"""Independent E2 HTTP-route and dashboard-payload regression coverage."""

from __future__ import annotations

import json
from contextlib import AbstractContextManager
from datetime import datetime, timedelta, timezone
from typing import Any

import azure.functions as func
import pytest
from discovery.clients.enrichment_provider import PollOutcome, PollResult
from function_app import (
    _prospect_row_to_csv_dict,
    _row_to_prospect,
    enrich_prospect,
    enrich_status,
)


def _request(method: str, route: str, *, params: dict[str, str] | None = None) -> func.HttpRequest:
    return func.HttpRequest(
        method=method,
        url=f"http://localhost/api{route}",
        params=params or {},
        route_params={"id": "42"},
        body=b"{}",
    )


def _body(response: func.HttpResponse) -> dict[str, Any]:
    return json.loads(response.get_body().decode())


def _candidate_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "id": 42,
        "ein": "010343943",
        "client_id": 2,
        "status": "approved",
        "website": "https://nonprofit.example",
        "officers": [{"name": "New Chief", "title": "CEO", "is_officer": True}],
        "icp_config": {"enrichment_enabled": True, "fullenrich_subaccount_id": "sub-1"},
    }
    row.update(overrides)
    return row


def _prospect_row(**overrides: Any) -> dict[str, Any]:
    row = {
        **_candidate_row(),
        "assigned_trigger": None,
        "trigger_angle": None,
        "trigger_evidence": None,
        "gap_rank": None,
        "suppression_flag": None,
        "notes": None,
        "updated_by": None,
        "updated_at": datetime(2026, 9, 21, tzinfo=timezone.utc),
        "org_name": "Synthetic Nonprofit",
        "city": None,
        "state": None,
        "revenue_latest": None,
        "values_signals": None,
        "alignment": {},
        "capacity": {},
        "soft_flags": None,
        "disqualified": False,
        "dq_reason": None,
        "enr_id": 7,
        "enr_contact_name": "Old Chief",
        "enr_contact_title": "CEO",
        "enr_email": "old.chief@former-employer.org",
        "enr_email_status": "stale_likely_moved",
        "enr_stale_detail": "former-employer.org",
        "enr_phone": None,
        "enr_provider_confidence": 95,
        "enr_credits_spent": 1,
        "enr_retention_expires_at": datetime.now(timezone.utc) + timedelta(days=90),
    }
    row.update(overrides)
    return row


class _Connection(AbstractContextManager["_Connection"]):
    def __init__(self, rows: list[dict[str, Any] | None]) -> None:
        self.rows = list(rows)

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def commit(self) -> None:
        return None

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        return None


class _Cursor(AbstractContextManager["_Cursor"]):
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    def execute(self, sql: str, params: Any = None) -> None:
        return None

    def fetchone(self) -> dict[str, Any] | None:
        return self.connection.rows.pop(0) if self.connection.rows else None

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        return None


@pytest.mark.parametrize(
    ("row", "reason"),
    [
        (_candidate_row(status="reviewed"), "prospect is not approved"),
        (_candidate_row(icp_config={"enrichment_enabled": False, "fullenrich_subaccount_id": "sub-1"}), "enrichment is not enabled for this tenant"),
        (_candidate_row(icp_config={"enrichment_enabled": True, "fullenrich_subaccount_id": ""}), "tenant has no fullenrich_subaccount_id configured"),
    ],
)
def test_status_route_rechecks_each_hard_rule_eight_condition(
    monkeypatch: pytest.MonkeyPatch, row: dict[str, Any], reason: str
) -> None:
    monkeypatch.setattr("function_app._conn", lambda: _Connection([row]))
    monkeypatch.setattr("function_app._get_fullenrich_provider", lambda subaccount_id: pytest.fail("provider called"))

    response = enrich_status(_request("GET", "/prospects/42/enrich-status", params={"job_id": "in-flight"}))

    assert response.status_code == 403
    assert _body(response) == {"error": reason}


def test_predecessor_enrichment_is_explicitly_marked_as_contact_mismatch() -> None:
    payload = _row_to_prospect(_prospect_row())
    assert payload["contact"]["name"] == "New Chief"
    assert payload["contact"]["enriched_contact_name"] == "Old Chief"
    assert payload["contact"]["contact_mismatch"] is True


def test_reenrichment_for_current_ceo_clears_contact_mismatch() -> None:
    payload = _row_to_prospect(_prospect_row(enr_contact_name="New Chief", enr_contact_title="CEO"))
    assert payload["contact"]["contact_mismatch"] is False


@pytest.mark.xfail(strict=True, reason="E2 only compares names, so a changed current title is silently presented as the enriched title.")
def test_same_name_but_changed_ceo_title_is_a_contact_mismatch() -> None:
    payload = _row_to_prospect(_prospect_row(enr_contact_name="New Chief", enr_contact_title="Former CEO"))
    assert payload["contact"]["contact_mismatch"] is True


def test_double_click_while_pending_submits_only_once(monkeypatch: pytest.MonkeyPatch) -> None:
    class _PendingProvider:
        name = "pending"

        def __init__(self) -> None:
            self.submits = 0

        def submit(self, candidate: object) -> str:
            self.submits += 1
            return f"job-{self.submits}"

        def poll(self, job_id: str, org_domain: str | None) -> PollResult:
            return PollResult(outcome=PollOutcome.PENDING)

    provider = _PendingProvider()
    monkeypatch.setattr("function_app._conn", lambda: _Connection([_candidate_row(), _candidate_row()]))
    monkeypatch.setattr("function_app._get_fullenrich_provider", lambda subaccount_id: provider)
    monkeypatch.setattr("function_app.ENRICH_POLL_ATTEMPTS", 1)

    first = enrich_prospect(_request("POST", "/prospects/42/enrich"))
    second = enrich_prospect(_request("POST", "/prospects/42/enrich"))

    assert first.status_code == second.status_code == 202
    assert provider.submits == 1


def test_csv_and_table_payload_agree_for_locked_and_stale_contact_states() -> None:
    locked = _prospect_row(enr_id=None, enr_contact_name=None, enr_email=None, enr_email_status=None)
    stale = _prospect_row()
    locked_payload = _row_to_prospect(locked)
    locked_csv = _prospect_row_to_csv_dict(locked)
    stale_payload = _row_to_prospect(stale)
    stale_csv = _prospect_row_to_csv_dict(stale)

    assert locked_payload["contact"]["enriched"] is False
    assert locked_payload["contact"]["can_enrich"] is True
    assert locked_csv["contact_email"] == "Enrich to unlock"
    assert stale_payload["contact"]["email_status"] == stale_csv["contact_status"] == "stale_likely_moved"
    assert stale_payload["contact"]["stale_detail"] == "former-employer.org"


def test_csv_carries_retention_and_mismatch_signals_visible_in_dashboard() -> None:
    row = _prospect_row(enr_retention_expires_at=datetime.now(timezone.utc) - timedelta(days=1))
    payload = _row_to_prospect(row)
    csv_row = _prospect_row_to_csv_dict(row)

    assert payload["contact"]["retention_expired"] is True
    assert payload["contact"]["contact_mismatch"] is True
    assert "expired" in " ".join(str(value).lower() for value in csv_row.values())
    assert "old chief" in " ".join(str(value).lower() for value in csv_row.values())