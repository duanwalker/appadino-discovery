"""National-scale Stage 2 fixes: cached zip handles, per-EIN streaming (bounded
memory), EIN-level resumability, and batched writes (extract_signals.py). STATUS.md
"Still open — Stage 2 per-filing throughput" has the real-run numbers (~2.2-2.5
filings/sec, ~29h projected) these changes target.

Hand-rolled fake connection/cursor, matching this suite's house style (see
test_g13_filter_signal_gaps.py / test_run_filter_and_signals.py) — no respx/MagicMock.
Unlike test_g13's fake (which only records the last SQL/params), FakeDB here actually
applies executemany'd writes to in-memory tables, since several of these tests assert
on persisted state across EIN boundaries and across two separate
extract_signals_for_survivors() calls (resume / force-recompute).
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any, ClassVar

import pytest

from discovery.stages import extract_signals
from discovery.stages.extract_signals import extract_signals_for_survivors

EMPTY_PARSED: dict[str, Any] = {
    "revenue_total": None,
    "contributions": None,
    "program_revenue": None,
    "govt_grants": None,
    "fundraising_expense": None,
    "officers": [],
    "mission_text": None,
    "program_text": [],
    "website": None,
    "significant_change_ind": None,
}


def _parsed(**overrides: Any) -> dict[str, Any]:
    return {**EMPTY_PARSED, **overrides}


class NullHttpClient(AbstractContextManager["NullHttpClient"]):
    def __exit__(self, *exc: object) -> None:
        return None


class FakeDB:
    """Shared mutable state behind FakeConnection — lets a test seed pre-existing
    signals (resume) and inspect what Stage 2 actually wrote (filings_updated,
    websites_updated, signals), including across two separate
    extract_signals_for_survivors() calls against the same FakeDB."""

    def __init__(
        self,
        filing_rows: list[tuple[Any, ...]],
        signals: dict[str, tuple[int, dict[str, Any]]] | None = None,
    ) -> None:
        self.all_filing_rows = filing_rows  # (id, ein, tax_year, object_id, xml_object_url, ruling_year)
        self.signals: dict[str, tuple[int, dict[str, Any]]] = dict(signals or {})
        self.filings_updated: dict[int, dict[str, Any]] = {}
        self.websites_updated: dict[str, str] = {}


class FakeCursor(AbstractContextManager["FakeCursor"]):
    def __init__(self, db: FakeDB) -> None:
        self.db = db
        self._last_sql = ""
        self._last_params: Any = None

    def execute(self, sql: str, params: Any = None) -> None:
        self._last_sql = sql
        self._last_params = params

    def executemany(self, sql: str, params_seq: list[dict[str, Any]]) -> None:
        if "UPDATE filings SET" in sql:
            for p in params_seq:
                self.db.filings_updated[p["id"]] = p
        elif "UPDATE organizations SET website" in sql:
            for p in params_seq:
                self.db.websites_updated[p["ein"]] = p["website"]
        elif "INSERT INTO signals" in sql:
            for p in params_seq:
                self.db.signals[p["ein"]] = (p["tax_year"], p["signal"].obj)
        else:
            raise AssertionError(f"unexpected executemany SQL: {sql!r}")

    def fetchall(self) -> list[tuple[Any, ...]]:
        if "FROM filings f LEFT JOIN organizations" in self._last_sql:
            requested = set(self._last_params[0])
            return [row for row in self.db.all_filing_rows if row[1] in requested]
        if "SELECT DISTINCT ein FROM signals" in self._last_sql:
            requested = set(self._last_params[0])
            return [(ein,) for ein in self.db.signals if ein in requested]
        return []

    def fetchone(self) -> tuple[Any, ...] | None:
        return None

    def __exit__(self, *exc: object) -> None:
        return None


class FakeConnection(AbstractContextManager["FakeConnection"]):
    def __init__(self, db: FakeDB) -> None:
        self.db = db

    def cursor(self) -> FakeCursor:
        return FakeCursor(self.db)

    def commit(self) -> None:
        return None

    def close(self) -> None:
        return None

    def __exit__(self, *exc: object) -> None:
        return None


def _wire_common_mocks(
    monkeypatch: pytest.MonkeyPatch,
    db: FakeDB,
    parsed_by_object_id: dict[str, dict[str, Any]],
) -> None:
    conn = FakeConnection(db)
    monkeypatch.setattr(extract_signals.psycopg, "connect", lambda _database_url: conn)
    monkeypatch.setattr(extract_signals.httpx, "Client", lambda **_kwargs: NullHttpClient())
    monkeypatch.setattr(extract_signals, "sync_archive_manifest", lambda _c, _conn, _years, _dir: None)
    monkeypatch.setattr(extract_signals, "build_year_index", lambda _conn, _year: {})
    monkeypatch.setattr(
        extract_signals,
        "fetch_filing_xml",
        lambda _year_index, object_id, _archive_cache=None: object_id.encode()
        if object_id in parsed_by_object_id
        else None,
    )
    monkeypatch.setattr(
        extract_signals,
        "parse_990_xml",
        lambda xml_bytes: parsed_by_object_id[xml_bytes.decode()],
    )


_URL_2024 = "https://apps.irs.gov/pub/epostcard/990/xml/2024/"


def test_single_filing_ein_produces_a_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    db = FakeDB(filing_rows=[(1, "111111111", 2023, "f1", _URL_2024, 2010)])
    _wire_common_mocks(monkeypatch, db, {"f1": _parsed(revenue_total=500_000)})

    counts = extract_signals_for_survivors("postgres://example", ["111111111"])

    assert counts["filings_parsed"] == 1
    assert counts["signals_computed"] == 1
    assert db.signals["111111111"][0] == 2023
    assert db.signals["111111111"][1]["org_age"] == 13


def test_first_filing_fails_second_parses_still_produces_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Matches pre-existing behavior (STATUS.md's survivor-based coverage_pct note):
    the most recent filing failing to resolve must not cost the EIN its signal — the
    next most recent successfully-parsed filing becomes `current`, with no
    `previous`, exactly as when parsed_by_ein held every filing in memory."""
    db = FakeDB(
        filing_rows=[
            (1, "111111111", 2023, "missing", _URL_2024, 2010),  # most recent, fails to resolve
            (2, "111111111", 2022, "f2", _URL_2024, 2010),  # older, parses fine
        ]
    )
    _wire_common_mocks(monkeypatch, db, {"f2": _parsed(revenue_total=500_000, website="example.org")})

    counts = extract_signals_for_survivors("postgres://example", ["111111111"])

    assert counts["filings_parsed"] == 1
    assert counts["filings_failed"] == 1
    assert counts["signals_computed"] == 1
    tax_year, signal = db.signals["111111111"]
    assert tax_year == 2022  # the filing that actually parsed, not the one that failed
    assert signal["revenue_trend"] is None  # no `previous` — only one filing ever parsed
    # Website backfill still happens off whichever filing actually parsed, same as before.
    assert db.websites_updated["111111111"] == "example.org"


def test_three_filings_one_ein_only_uses_first_two_for_the_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every filing row still gets its `filings` columns updated, but compute_signals
    only ever sees the 2 most recent successfully-parsed ones — confirms the
    bounded-memory buffer (cap of 2) doesn't change what gets computed vs. the old
    whole-run parsed_by_ein list, which likewise only ever used filings[0]/filings[1]
    after a DESC sort."""
    db = FakeDB(
        filing_rows=[
            (1, "111111111", 2023, "f1", _URL_2024, 2010),
            (2, "111111111", 2022, "f2", _URL_2024, 2010),
            (3, "111111111", 2021, "f3", _URL_2024, 2010),
        ]
    )
    _wire_common_mocks(
        monkeypatch,
        db,
        {
            "f1": _parsed(revenue_total=150_000),
            "f2": _parsed(revenue_total=100_000),
            "f3": _parsed(revenue_total=1),  # would blow up revenue_trend if ever treated as `previous`
        },
    )

    counts = extract_signals_for_survivors("postgres://example", ["111111111"])

    assert counts["filings_parsed"] == 3  # all 3 filings rows written
    assert len(db.filings_updated) == 3
    assert counts["signals_computed"] == 1  # but exactly one signal for the EIN
    _tax_year, signal = db.signals["111111111"]
    assert signal["revenue_trend"] == "growth"  # 150k vs 100k (f1/f2) — f3 (rev=1) never consulted


def test_resume_skips_eins_with_existing_signals_row(monkeypatch: pytest.MonkeyPatch) -> None:
    db = FakeDB(
        filing_rows=[
            (1, "111111111", 2023, "f1", _URL_2024, 2010),
            (2, "222222222", 2023, "f2", _URL_2024, 2012),
        ],
        signals={"111111111": (2022, {"stale": True})},  # already has a signal from a prior run
    )
    _wire_common_mocks(monkeypatch, db, {"f1": _parsed(revenue_total=1), "f2": _parsed(revenue_total=2)})

    counts = extract_signals_for_survivors("postgres://example", ["111111111", "222222222"])

    assert counts["eins_skipped_already_signaled"] == 1
    assert counts["filings_parsed"] == 1  # only 222222222's filing was even fetched
    assert 1 not in db.filings_updated  # 111111111's filing row was never touched
    assert db.signals["111111111"] == (2022, {"stale": True})  # untouched — not recomputed
    assert db.signals["222222222"][0] == 2023


def test_force_recompute_reprocesses_already_signaled_eins(monkeypatch: pytest.MonkeyPatch) -> None:
    db = FakeDB(
        filing_rows=[(1, "111111111", 2023, "f1", _URL_2024, 2010)],
        signals={"111111111": (2022, {"stale": True})},
    )
    _wire_common_mocks(monkeypatch, db, {"f1": _parsed(revenue_total=750_000)})

    counts = extract_signals_for_survivors(
        "postgres://example", ["111111111"], force_recompute=True
    )

    assert counts["eins_skipped_already_signaled"] == 0
    assert counts["filings_parsed"] == 1
    assert 1 in db.filings_updated
    tax_year, signal = db.signals["111111111"]
    assert tax_year == 2023  # recomputed from the real filing, not left as the stale stub
    assert signal != {"stale": True}


class _RecordingZipFile:
    """Stands in for zipfile.ZipFile: records every instance opened (by local_path)
    and whether/when it was closed, without touching a real file — just enough to
    prove the archive_cache (a) reuses one instance per local_path and (b) every
    opened instance gets closed by extract_signals_for_survivors' finally block, even
    when the run raises partway through."""

    instances: ClassVar[list[_RecordingZipFile]] = []

    def __init__(self, local_path: str) -> None:
        self.local_path = local_path
        self.closed = False
        self.open_count = 1
        _RecordingZipFile.instances.append(self)

    def read(self, member: str) -> bytes:
        return f"{self.local_path}:{member}".encode()

    def close(self) -> None:
        self.closed = True


def test_fetch_filing_xml_reuses_cached_handle_across_filings_same_archive(monkeypatch: pytest.MonkeyPatch) -> None:
    _RecordingZipFile.instances = []
    monkeypatch.setattr(extract_signals.zipfile, "ZipFile", _RecordingZipFile)
    year_index = {"obj-a": ("archive.zip", "a.xml"), "obj-b": ("archive.zip", "b.xml")}
    cache: dict[str, Any] = {}

    first = extract_signals.fetch_filing_xml(year_index, "obj-a", cache)
    second = extract_signals.fetch_filing_xml(year_index, "obj-b", cache)

    assert first == b"archive.zip:a.xml"
    assert second == b"archive.zip:b.xml"
    assert len(_RecordingZipFile.instances) == 1  # one open, reused for the second read
    assert cache["archive.zip"] is _RecordingZipFile.instances[0]


def test_handle_cache_closes_all_open_zip_handles_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bug surfacing after at least one filing from each of two archives has been
    read (e.g. in compute_signals, well past any try/except that tolerates per-filing
    failures) must still leave every already-opened zip handle closed, not leaked —
    the whole point of caching handles for the run's duration instead of using a
    `with` block per filing."""
    _RecordingZipFile.instances = []
    monkeypatch.setattr(extract_signals.zipfile, "ZipFile", _RecordingZipFile)

    db = FakeDB(
        filing_rows=[
            (1, "111111111", 2023, "obj-a", _URL_2024, 2010),
            (2, "222222222", 2023, "obj-b", _URL_2024, 2012),
        ]
    )
    conn = FakeConnection(db)
    monkeypatch.setattr(extract_signals.psycopg, "connect", lambda _database_url: conn)
    monkeypatch.setattr(extract_signals.httpx, "Client", lambda **_kwargs: NullHttpClient())
    monkeypatch.setattr(extract_signals, "sync_archive_manifest", lambda _c, _conn, _years, _dir: None)
    monkeypatch.setattr(
        extract_signals,
        "build_year_index",
        lambda _conn, _year: {"obj-a": ("archive_a.zip", "a.xml"), "obj-b": ("archive_b.zip", "b.xml")},
    )
    monkeypatch.setattr(extract_signals, "parse_990_xml", lambda _xml_bytes: _parsed(revenue_total=1))

    call_count = {"n": 0}
    real_compute_signals = extract_signals.compute_signals

    def flaky_compute_signals(*args: Any, **kwargs: Any) -> dict[str, Any]:
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise RuntimeError("boom — simulated bug after both archives are already open")
        return real_compute_signals(*args, **kwargs)

    monkeypatch.setattr(extract_signals, "compute_signals", flaky_compute_signals)

    with pytest.raises(RuntimeError, match="boom"):
        extract_signals_for_survivors("postgres://example", ["111111111", "222222222"])

    assert len(_RecordingZipFile.instances) == 2  # both archives were opened before the crash
    assert all(instance.closed for instance in _RecordingZipFile.instances)
