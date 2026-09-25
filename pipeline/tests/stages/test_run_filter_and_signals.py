"""Orchestrator-level tests for run_filter_and_signals.py's new early suppression
gate: an EIN-exact suppression match must never reach extract_signals_for_survivors'
input set (no Stage 2 extraction, no Stage 3 Sonnet cost for an org already known to
be suppressed), while a fuzzy match must still flow through untouched, since Lauren's
rule requires a human reviewer to see real extracted/scored data before a fuzzy match
is ever excluded.

Hand-rolled fake connection (no respx/MagicMock), matching this suite's existing
house style (see test_g13_filter_signal_gaps.py). Only psycopg.connect and
extract_signals_for_survivors are monkeypatched — select_survivor_eins is also
monkeypatched (Stage 1's own SQL isn't what's under test here), but apply_suppression
itself runs for real against the fake connection, so this exercises the actual
EIN-exact-drop / fuzzy-flag-keep decision, not a mocked stand-in for it.
"""

from __future__ import annotations

from typing import Any, Self

import pytest

from discovery.stages import run_filter_and_signals

_FAKE_SIGNAL_COUNTS = {"filings_parsed": 0, "filings_failed": 0, "signals_computed": 0, "coverage_pct": None}


def _make_capturing_extract_signals(captured_calls: list[list[str]]) -> Any:
    def fake_extract_signals_for_survivors(_database_url: str, eins: list[str]) -> dict[str, Any]:
        captured_calls.append(list(eins))
        return _FAKE_SIGNAL_COUNTS

    return fake_extract_signals_for_survivors


class FakeCursor:
    def __init__(self, conn: FakeConn) -> None:
        self.conn = conn
        self._result: list[tuple[Any, ...]] = []

    def execute(self, sql: str, params: Any = None) -> None:
        stripped = sql.strip()
        if stripped.startswith("INSERT INTO runs"):
            self._result = [(1,)]
        elif stripped.startswith("UPDATE runs SET"):
            self.conn.last_update_params = params
        elif stripped.startswith("SELECT ein, org_name, kind FROM suppression"):
            self._result = [(e["ein"], e["org_name"], e["kind"]) for e in self.conn.suppression_entries]
        elif stripped.startswith("SELECT ein, name FROM organizations"):
            eins = params[0]
            self._result = [(ein, name) for ein, name in self.conn.org_names.items() if ein in eins]
        else:
            raise AssertionError(f"unexpected SQL in FakeCursor: {sql!r}")

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._result[0] if self._result else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._result

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class FakeConn:
    def __init__(self, suppression_entries: list[dict[str, str | None]], org_names: dict[str, str]) -> None:
        self.suppression_entries = suppression_entries
        self.org_names = org_names
        self.last_update_params: Any = None

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        return None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def test_ein_exact_match_never_reaches_extract_signals_input(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_conn = FakeConn(
        suppression_entries=[
            {"ein": "111111111", "org_name": "Known Client", "kind": "client"},
            {"ein": None, "org_name": "Butterfly Dreamz", "kind": "prospect"},
        ],
        org_names={
            "111111111": "Known Client",
            "222222222": "Butterfly Dreamz, Inc.",  # fuzzy match
            "333333333": "Totally Unrelated Nonprofit",  # no match
        },
    )
    survivor_eins = ["111111111", "222222222", "333333333"]
    captured_extract_calls: list[list[str]] = []

    monkeypatch.setattr(
        "discovery.stages.run_filter_and_signals.psycopg.connect", lambda _database_url: fake_conn
    )
    monkeypatch.setattr(run_filter_and_signals, "select_survivor_eins", lambda _conn, _client_id: survivor_eins)
    monkeypatch.setattr(
        run_filter_and_signals,
        "extract_signals_for_survivors",
        _make_capturing_extract_signals(captured_extract_calls),
    )

    counts = run_filter_and_signals.run_filter_and_signals(client_id=1, database_url="postgres://example")

    assert len(captured_extract_calls) == 1
    extracted_eins = captured_extract_calls[0]

    # EIN-exact suppression match: never reaches Stage 2 at all.
    assert "111111111" not in extracted_eins
    # Fuzzy match: flagged, but NOT dropped — still flows through to Stage 2/3.
    assert "222222222" in extracted_eins
    # Unmatched survivor: passes through untouched.
    assert "333333333" in extracted_eins

    assert counts["survivors"] == 3
    assert counts["ein_suppressed_pre_extraction"] == 1
    assert counts["fuzzy_flagged_pre_extraction"] == 1


def test_no_suppression_matches_passes_full_survivor_set_through(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_conn = FakeConn(suppression_entries=[], org_names={"444444444": "Some Org"})
    survivor_eins = ["444444444"]
    captured_extract_calls: list[list[str]] = []

    monkeypatch.setattr(
        "discovery.stages.run_filter_and_signals.psycopg.connect", lambda _database_url: fake_conn
    )
    monkeypatch.setattr(run_filter_and_signals, "select_survivor_eins", lambda _conn, _client_id: survivor_eins)
    monkeypatch.setattr(
        run_filter_and_signals,
        "extract_signals_for_survivors",
        _make_capturing_extract_signals(captured_extract_calls),
    )

    counts = run_filter_and_signals.run_filter_and_signals(client_id=1, database_url="postgres://example")

    assert captured_extract_calls == [["444444444"]]
    assert counts["ein_suppressed_pre_extraction"] == 0
    assert counts["fuzzy_flagged_pre_extraction"] == 0
