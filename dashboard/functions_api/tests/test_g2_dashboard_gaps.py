"""Independent G2.x dashboard/functions_api coverage review.

These tests deliberately use synthetic rows and tiny fake DB connections instead
of a live Postgres instance. They characterize both safe behavior and current
validation gaps so TEST-COVERAGE-GAPS.md can distinguish clean 4xx handling from
malformed input that still reaches SQL.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import azure.functions as func
import pytest
from discovery.stages.publish import upsert_prospect
from function_app import (
    app,
    create_suppression,
    delete_suppression,
    export_csv,
    list_prospects,
    list_runs,
    list_suppression,
    suppression_review,
    update_prospect,
    _prospect_row_to_csv_dict,
    _row_to_prospect,
)


def _request(
    method: str,
    url: str,
    *,
    params: dict[str, str] | None = None,
    route_params: dict[str, str] | None = None,
    body: dict[str, Any] | bytes | None = None,
) -> func.HttpRequest:
    raw_body = body if isinstance(body, bytes) else json.dumps(body or {}).encode()
    return func.HttpRequest(method=method, url=url, params=params or {}, route_params=route_params or {}, body=raw_body)


def _json_body(response: func.HttpResponse) -> dict[str, Any]:
    return json.loads(response.get_body().decode())


def test_publish_upsert_republish_preserves_human_notes_and_updated_by() -> None:
    """Synthetic replay of the real data-loss bug: Lauren has already approved a
    prospect and written a note; the pipeline republishes the same prospect.
    The conflict-update path must refresh pipeline-owned fields only."""
    existing = {
        "client_id": 2,
        "ein": "010343943",
        "status": "approved",
        "notes": "Lauren reviewed: strong fit, keep in approved list.",
        "updated_by": "lauren@appadino.test",
        "assigned_trigger": "new_ed",
        "trigger_angle": "first-100-days",
        "trigger_evidence": {"old": True},
        "gap_rank": 66.67,
        "suppression_flag": "Possible suppression match: 'Old Name' (fuzzy, not auto-excluded)",
        "updated_at": datetime(2026, 9, 18, tzinfo=timezone.utc),
    }
    conn = ProspectUpsertConnection(existing)

    upsert_prospect(
        conn,
        {
            "client_id": 2,
            "ein": "010343943",
            "assigned_trigger": "transformational_revenue_jump",
            "trigger_angle": "growth inflection",
            "trigger_evidence": {"new": True},
            "gap_rank": 88.5,
            "suppression_flag": None,
            "updated_at": datetime(2026, 9, 19, tzinfo=timezone.utc),
        },
    )

    assert conn.row["status"] == "approved"
    assert conn.row["notes"] == "Lauren reviewed: strong fit, keep in approved list."
    assert conn.row["updated_by"] == "lauren@appadino.test"
    assert conn.row["assigned_trigger"] == "transformational_revenue_jump"
    assert conn.row["trigger_evidence"] == {"new": True}
    assert conn.row["gap_rank"] == 88.5
    assert conn.row["suppression_flag"] is None
    assert conn.commits == 1


def test_prospects_status_check_constraint_rejects_arbitrary_string_at_db_layer() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE prospects (
            id INTEGER PRIMARY KEY,
            status TEXT NOT NULL CHECK (status IN ('new', 'reviewed', 'approved', 'rejected'))
        )
        """
    )
    conn.execute("INSERT INTO prospects (status) VALUES (?)", ("approved",))

    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        conn.execute("INSERT INTO prospects (status) VALUES (?)", ("contacted",))


class TestFunctionsApiMalformedInput:
    def test_list_prospects_missing_client_id_returns_400_without_db(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("function_app._conn", _conn_that_must_not_be_called)
        response = list_prospects(_request("GET", "/api/prospects"))
        assert response.status_code == 400
        assert _json_body(response) == {"error": "client_id is required"}

    def test_update_prospect_out_of_enum_status_returns_400_without_db(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("function_app._conn", _conn_that_must_not_be_called)
        response = update_prospect(
            _request(
                "PATCH",
                "/api/prospects/not-an-id",
                route_params={"id": "not-an-id"},
                body={"status": "contacted", "updated_by": "lauren@appadino.test"},
            )
        )
        assert response.status_code == 400
        assert "status must be one of" in _json_body(response)["error"]

    def test_suppression_review_bad_action_returns_400_without_db(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("function_app._conn", _conn_that_must_not_be_called)
        response = suppression_review(
            _request(
                "POST",
                "/api/prospects/abc/suppression-review",
                route_params={"id": "abc"},
                body={"action": "maybe", "updated_by": "lauren@appadino.test"},
            )
        )
        assert response.status_code == 400
        assert _json_body(response) == {"error": "action must be 'confirm' or 'dismiss'"}

    def test_list_suppression_malformed_client_id_falls_through_to_db(self, monkeypatch: pytest.MonkeyPatch) -> None:
        conn = RouteConnection(fetchall=[])
        monkeypatch.setattr("function_app._conn", lambda: conn)
        response = list_suppression(_request("GET", "/api/suppression", params={"client_id": "not-an-int"}))
        assert response.status_code == 200
        assert conn.executed[0].params == ("not-an-int",)

    def test_create_suppression_malformed_ein_falls_through_to_insert(self, monkeypatch: pytest.MonkeyPatch) -> None:
        conn = RouteConnection(fetchone={"id": 123})
        monkeypatch.setattr("function_app._conn", lambda: conn)
        response = create_suppression(
            _request(
                "POST",
                "/api/suppression",
                body={"client_id": 2, "ein": "not-an-ein", "org_name": "Synthetic Org", "kind": "client"},
            )
        )
        assert response.status_code == 201
        assert conn.executed[0].params["ein"] == "not-an-ein"

    def test_delete_suppression_nonexistent_id_returns_clean_404(self, monkeypatch: pytest.MonkeyPatch) -> None:
        conn = RouteConnection(fetchone=None)
        monkeypatch.setattr("function_app._conn", lambda: conn)
        response = delete_suppression(
            _request("DELETE", "/api/suppression/not-an-id", route_params={"id": "not-an-id"})
        )
        assert response.status_code == 404
        assert _json_body(response) == {"error": "suppression entry not found"}

    def test_export_csv_missing_client_id_returns_400_without_db(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("function_app._conn", _conn_that_must_not_be_called)
        response = export_csv(_request("GET", "/api/export.csv"))
        assert response.status_code == 400
        assert _json_body(response) == {"error": "client_id is required"}

    def test_list_runs_bad_limit_returns_400_without_db(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("function_app._conn", _conn_that_must_not_be_called)
        response = list_runs(_request("GET", "/api/runs", params={"client_id": "2", "limit": "many"}))
        assert response.status_code == 400
        assert _json_body(response) == {"error": "limit must be an integer"}


def test_dismiss_suppression_flag_only_clears_prospect_flag_not_suppression_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = RouteConnection(fetchone={"client_id": 2, "ein": "010343943", "org_name": "Synthetic Org"})
    monkeypatch.setattr("function_app._conn", lambda: conn)

    response = suppression_review(
        _request(
            "POST",
            "/api/prospects/42/suppression-review",
            route_params={"id": "42"},
            body={"action": "dismiss", "updated_by": "lauren@appadino.test"},
        )
    )

    assert response.status_code == 200
    sql_log = "\n".join(statement.sql for statement in conn.executed)
    assert "INSERT INTO suppression" not in sql_log
    assert "DELETE FROM suppression" not in sql_log
    assert "UPDATE prospects SET suppression_flag = NULL" in sql_log
    assert conn.executed[-1].params == ("lauren@appadino.test", "42")


def test_confirm_suppression_flag_adds_client_suppression_then_clears_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = RouteConnection(fetchone={"client_id": 2, "ein": "010343943", "org_name": "Synthetic Org"})
    monkeypatch.setattr("function_app._conn", lambda: conn)

    response = suppression_review(
        _request(
            "POST",
            "/api/prospects/42/suppression-review",
            route_params={"id": "42"},
            body={"action": "confirm", "updated_by": "lauren@appadino.test"},
        )
    )

    assert response.status_code == 200
    assert any("INSERT INTO suppression" in statement.sql for statement in conn.executed)
    assert "UPDATE prospects SET suppression_flag = NULL" in conn.executed[-1].sql
    insert_params = next(statement.params for statement in conn.executed if "INSERT INTO suppression" in statement.sql)
    assert insert_params["kind"] if "kind" in insert_params else "client"
    assert insert_params["ein"] == "010343943"


def test_functions_api_routes_are_registered_anonymous_not_function_key_protected() -> None:
    assert app._auth_level == func.AuthLevel.ANONYMOUS  # noqa: SLF001 - Azure Functions exposes no public accessor.


def test_row_to_prospect_passes_partial_null_jsonb_shapes_without_throwing() -> None:
    row = _sample_prospect_row(values_signals={"leadership_composition": None}, alignment=None, capacity=None)
    prospect = _row_to_prospect(row)
    assert prospect["score"]["values_signals"] == {"leadership_composition": None}
    assert prospect["score"]["alignment"] is None
    assert prospect["score"]["capacity"] is None


def test_csv_transform_throws_on_malformed_alignment_jsonb_shape() -> None:
    row = _sample_prospect_row(alignment=["not", "an", "object"], capacity={"dd_present": False})
    with pytest.raises(AttributeError, match="'list' object has no attribute 'get'"):
        _prospect_row_to_csv_dict(row)


def test_csv_transform_throws_on_malformed_capacity_jsonb_shape() -> None:
    row = _sample_prospect_row(alignment={"criteria_met_count": 2}, capacity="not-an-object")
    with pytest.raises(AttributeError, match="'str' object has no attribute 'get'"):
        _prospect_row_to_csv_dict(row)


def _sample_prospect_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "id": 1,
        "ein": "010343943",
        "client_id": 2,
        "status": "new",
        "assigned_trigger": "new_ed",
        "trigger_angle": "first-100-days",
        "trigger_evidence": {"new_ed": {"prior": "Jane", "current": "John"}},
        "gap_rank": Decimal("66.67"),
        "suppression_flag": None,
        "notes": None,
        "updated_by": "pipeline",
        "updated_at": datetime(2026, 9, 19, tzinfo=timezone.utc),
        "org_name": "Synthetic Org",
        "city": "Portland",
        "state": "ME",
        "revenue_latest": Decimal("123456"),
        "values_signals": {},
        "alignment": {"criteria_met_count": 2, "qualifies": False},
        "capacity": {"dd_present": False, "fundraising_spend_ratio": 0.0},
        "soft_flags": {},
        "disqualified": False,
        "dq_reason": None,
        "website": "https://synthetic.org",
        "officers": [{"name": "Jane Smith", "title": "Executive Director", "is_officer": True}],
        "icp_config": {"enrichment_enabled": False, "fullenrich_subaccount_id": None},
        "enr_id": None,
        "enr_contact_name": None,
        "enr_contact_title": None,
        "enr_email": None,
        "enr_email_status": None,
        "enr_stale_detail": None,
        "enr_phone": None,
        "enr_provider_confidence": None,
        "enr_credits_spent": None,
        "enr_retention_expires_at": None,
    }
    row.update(overrides)
    return row


def _conn_that_must_not_be_called() -> None:
    raise AssertionError("malformed input should have been rejected before opening a DB connection")


class ExecutedStatement:
    def __init__(self, sql: str, params: Any) -> None:
        self.sql = " ".join(sql.split())
        self.params = params


class ProspectUpsertConnection(AbstractContextManager["ProspectUpsertConnection"]):
    def __init__(self, row: dict[str, Any]) -> None:
        self.row = dict(row)
        self.commits = 0

    def cursor(self) -> ProspectUpsertCursor:
        return ProspectUpsertCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        return None


class ProspectUpsertCursor(AbstractContextManager["ProspectUpsertCursor"]):
    def __init__(self, connection: ProspectUpsertConnection) -> None:
        self.connection = connection

    def execute(self, sql: str, params: dict[str, Any]) -> None:
        update_clause = sql.split("DO UPDATE SET", maxsplit=1)[1].lower()
        for field in [
            "assigned_trigger",
            "trigger_angle",
            "trigger_evidence",
            "gap_rank",
            "suppression_flag",
            "updated_at",
            "notes",
            "updated_by",
            "status",
        ]:
            if field in update_clause and field in params:
                self.connection.row[field] = params[field]

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        return None


class RouteConnection(AbstractContextManager["RouteConnection"]):
    def __init__(self, *, fetchone: dict[str, Any] | None = None, fetchall: Iterable[dict[str, Any]] = ()) -> None:
        self.fetchone_result = fetchone
        self.fetchall_result = list(fetchall)
        self.executed: list[ExecutedStatement] = []
        self.commits = 0

    def cursor(self) -> RouteCursor:
        return RouteCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        return None


class RouteCursor(AbstractContextManager["RouteCursor"]):
    def __init__(self, connection: RouteConnection) -> None:
        self.connection = connection

    def execute(self, sql: str, params: Any = None) -> None:
        self.connection.executed.append(ExecutedStatement(sql, params))

    def fetchone(self) -> dict[str, Any] | None:
        return self.connection.fetchone_result

    def fetchall(self) -> list[dict[str, Any]]:
        return self.connection.fetchall_result

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        return None
