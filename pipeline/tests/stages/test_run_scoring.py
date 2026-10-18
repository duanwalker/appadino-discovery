"""Orchestrator-level tests for run_scoring.py's Stage 3 org loader.

Hand-rolled fake connection (no respx/MagicMock), matching this suite's existing
house style (see test_run_filter_and_signals.py, test_g13_filter_signal_gaps.py).
_load_scoreable_orgs() takes a connection directly rather than opening its own, so
these call it straight — no psycopg.connect monkeypatching needed. FakeCursor
re-implements the real query's inner-join semantics (an EIN needs both an extracted
filing and a computed signal to be scoreable) against synthetic per-EIN fixtures,
rather than returning preset rows, so the test actually exercises the exclusion
behavior instead of just re-asserting whatever rows it's handed.
"""

from __future__ import annotations

from typing import Any, Self

import pytest

from discovery.stages import run_scoring as run_scoring_module
from discovery.stages.run_scoring import _load_scoreable_orgs


class FakeCursor:
    def __init__(self, conn: FakeConn) -> None:
        self.conn = conn
        self._result: list[tuple[Any, ...]] = []

    def execute(self, sql: str, params: Any = None) -> None:
        stripped = sql.strip()
        if stripped.startswith("SELECT o.ein, o.name, o.city, o.state, o.ruling_year,"):
            eins = params[0]
            rows = []
            for ein in eins:
                org = self.conn.organizations.get(ein)
                filing = self.conn.extracted_filings.get(ein)  # None if not extracted
                signal = self.conn.signals.get(ein)  # None if Stage 2 never computed one
                if org is None or filing is None or signal is None:
                    continue  # mirrors the real query's inner JOIN LATERALs: both required
                rows.append(
                    (
                        ein,
                        org["name"],
                        org["city"],
                        org["state"],
                        org["ruling_year"],
                        filing["mission_text"],
                        filing["program_text"],
                        filing["significant_change_ind"],
                        filing["tax_year"],
                        signal,
                    )
                )
            self._result = rows
        else:
            raise AssertionError(f"unexpected SQL in FakeCursor: {sql!r}")

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._result

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class FakeConn:
    def __init__(
        self,
        organizations: dict[str, dict[str, Any]],
        extracted_filings: dict[str, dict[str, Any]],
        signals: dict[str, dict[str, Any]],
    ) -> None:
        self.organizations = organizations
        self.extracted_filings = extracted_filings
        self.signals = signals

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)


def test_ein_with_no_signals_row_is_excluded_from_scoreable_orgs() -> None:
    """The property that keeps Stage 3 from ever spending Sonnet cost on a survivor
    Stage 2 skipped: today it's a side effect of the query's inner JOIN LATERAL
    against `signals` (no row -> no match -> dropped), not an asserted behavior."""
    organizations = {
        "111111111": {"name": "Has Signal Org", "city": "Portland", "state": "ME", "ruling_year": 1998},
        "222222222": {"name": "No Signal Org", "city": "Bangor", "state": "ME", "ruling_year": 2001},
    }
    extracted_filings = {
        "111111111": {
            "mission_text": "Mentorship for local youth.",
            "program_text": [{"desc": "After-school tutoring."}],
            "significant_change_ind": False,
            "tax_year": 2023,
        },
        # 222222222 has an extracted filing too — the row Stage 2 would have
        # produced a signal from, if it hadn't been skipped/suppressed first.
        "222222222": {
            "mission_text": "Community health outreach.",
            "program_text": [{"desc": "Mobile clinics."}],
            "significant_change_ind": False,
            "tax_year": 2023,
        },
    }
    signals = {
        "111111111": {"revenue_total": 750_000, "contributions_pct": 0.6},
        # 222222222 intentionally has no signals row.
    }
    fake_conn = FakeConn(organizations, extracted_filings, signals)

    orgs = _load_scoreable_orgs(fake_conn, ["111111111", "222222222"])  # type: ignore[arg-type]

    assert "111111111" in orgs
    assert orgs["111111111"]["name"] == "Has Signal Org"
    assert orgs["111111111"]["revenue_total"] == 750_000

    assert "222222222" not in orgs


# --- runs.counts records the `score --eins` override (no network, no DB) ---

class _RunsCursor:
    def __init__(self, log: list[tuple[str, Any]]) -> None:
        self.log = log

    def execute(self, sql: str, params: Any = None) -> None:
        self.log.append((sql, params))

    def fetchone(self) -> tuple[Any, ...]:
        return (1,)  # runs.id for the INSERT ... RETURNING, icp version for the SELECT

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class _RunsConn:
    def __init__(self, log: list[tuple[str, Any]]) -> None:
        self.log = log

    def cursor(self) -> _RunsCursor:
        return _RunsCursor(self.log)

    def commit(self) -> None:
        return None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _run_scoring_offline(monkeypatch: pytest.MonkeyPatch, eins: list[str], **kwargs: Any) -> dict[str, Any]:
    """Runs the real run_scoring with every external boundary replaced: no Anthropic
    client, no batch submission, no database. Returns the counts persisted to runs."""
    log: list[tuple[str, Any]] = []
    monkeypatch.setattr("psycopg.connect", lambda url: _RunsConn(log))
    monkeypatch.setattr("anthropic.Anthropic", lambda: object())
    monkeypatch.setattr(run_scoring_module, "get_active_icp_config", lambda conn, client_id: {})
    monkeypatch.setattr(run_scoring_module, "_load_scoreable_orgs", lambda conn, eins: {})

    def no_batches(client: object, requests: list[Any]) -> dict[str, Any]:
        assert requests == []  # nothing scoreable, so nothing may be submitted
        return {}

    monkeypatch.setattr(run_scoring_module, "run_batch", no_batches)
    returned = run_scoring_module.run_scoring(3, eins, database_url="postgresql://offline", **kwargs)
    update = next(params for sql, params in log if sql.startswith("UPDATE runs SET"))
    persisted = update[2].obj  # psycopg Json wrapper around the counts dict
    assert persisted == returned
    return returned


def test_run_scoring_records_eins_override_in_runs_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    counts = _run_scoring_offline(monkeypatch, ["010343943", "010322532"], eins_overridden=True)
    assert counts["eins_overridden"] is True
    assert counts["eins_requested"] == 2


def test_run_scoring_default_records_no_override(monkeypatch: pytest.MonkeyPatch) -> None:
    counts = _run_scoring_offline(monkeypatch, ["111111111"])
    assert counts["eins_overridden"] is False
    assert counts["eins_requested"] == 1
