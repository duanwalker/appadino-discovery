"""Tests for the Stage 2 archive-manifest cache (extract_signals.py) — the fix for
the per-filing RemoteZip-open performance problem STATUS.md flagged as still open
after G1.5 ("Before G1.5 can be considered fully accepted: the Stage 2 performance
fix (bulk zip download)"). Covers: a fresh shard appearing on a manifest re-probe,
the size-mismatch sanity-check warning path, and the local zipfile read path that
replaces the old per-filing RemoteZip open.

Hand-rolled fakes only (no respx/MagicMock) — matches this suite's existing house
style (see test_g13_filter_signal_gaps.py's FakeConnection/FakeCursor).
"""

from __future__ import annotations

import logging
import zipfile
from pathlib import Path
from typing import Any, Self, cast

import httpx
import psycopg
import pytest

from discovery.stages import extract_signals
from discovery.stages.extract_signals import (
    ARCHIVE_URL_TEMPLATE,
    _known_manifest_shards,
    _probe_new_shards,
    _sanity_check_manifest,
    build_year_index,
    fetch_filing_xml,
    sync_archive_manifest,
)
from discovery.stages.ingest import default_target_years

# Hand-rolled fakes below are structurally compatible with httpx.Client/psycopg.Connection
# (same methods, same call shape) but aren't declared as subclasses of them, so every call
# site below casts explicitly rather than relying on nominal typing mypy strict requires.


def _archive_url(year: int, month: int, suffix: str) -> str:
    filename = f"{year}_TEOS_XML_{month:02d}{suffix}.zip"
    return ARCHIVE_URL_TEMPLATE.format(year=year, filename=filename)


class FakeResponse:
    def __init__(self, status_code: int, content_length: str | None = None) -> None:
        self.status_code = status_code
        self.headers: dict[str, str] = {"content-length": content_length} if content_length is not None else {}


class FakeStreamResponse:
    def __init__(self, content: bytes) -> None:
        self._content = content

    def raise_for_status(self) -> None:
        return None

    def iter_bytes(self) -> Any:
        yield self._content

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class FakeHttpClient:
    """Stand-in for httpx.Client. `head_responses` maps url -> FakeResponse for
    .head(); any url not in the map falls back to a 404 (matches how a month with
    nothing published yet behaves against the real IRS site). `head_calls` records
    every url actually probed, so tests can assert a known suffix's HEAD call was
    skipped."""

    def __init__(self, head_responses: dict[str, FakeResponse] | None = None, stream_content: bytes = b"") -> None:
        self.head_responses = head_responses or {}
        self.stream_content = stream_content
        self.head_calls: list[str] = []

    def head(self, url: str) -> FakeResponse:
        self.head_calls.append(url)
        return self.head_responses.get(url, FakeResponse(404))

    def stream(self, _method: str, _url: str) -> FakeStreamResponse:
        return FakeStreamResponse(self.stream_content)


class FakeManifestCursor:
    def __init__(self, conn: FakeManifestConn) -> None:
        self.conn = conn
        self._result: list[tuple[Any, ...]] = []

    def execute(self, sql: str, params: Any = None) -> None:
        stripped = sql.strip()
        if stripped.startswith("SELECT local_path FROM archive_manifest"):
            year = params[0]
            self._result = [
                (row["local_path"],) for (y, _m, _s), row in self.conn.rows.items() if y == year
            ]
        elif stripped.startswith("SELECT month, suffix, url, local_path, size_bytes"):
            year = params[0]
            self._result = [
                (m, s, row["url"], row["local_path"], row["size_bytes"])
                for (y, m, s), row in self.conn.rows.items()
                if y == year
            ]
        elif stripped.startswith("INSERT INTO archive_manifest"):
            key = (params["year"], params["month"], params["suffix"])
            self.conn.rows.setdefault(
                key,
                {
                    "url": params["url"],
                    "local_path": params["local_path"],
                    "size_bytes": params["size_bytes"],
                },
            )
        else:
            raise AssertionError(f"unexpected SQL in FakeManifestCursor: {sql!r}")

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._result

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class FakeManifestConn:
    def __init__(self, rows: dict[tuple[int, int, str], dict[str, Any]] | None = None) -> None:
        self.rows = rows or {}
        self.commits = 0

    def cursor(self) -> FakeManifestCursor:
        return FakeManifestCursor(self)

    def commit(self) -> None:
        self.commits += 1


def test_probe_new_shards_finds_fresh_shard_and_skips_already_known_suffix() -> None:
    """A month whose suffix A is already in the manifest, but whose suffix B just
    appeared on IRS's site — the manifest-aware probe finds B without re-checking A."""
    year = 2025
    known_url_a = _archive_url(year, 1, "A")
    new_url_b = _archive_url(year, 1, "B")
    known = {1: {"A": {"url": known_url_a, "local_path": "/cache/2025/01A.zip", "size_bytes": 100}}}

    client = FakeHttpClient(head_responses={new_url_b: FakeResponse(200)})

    new_shards = _probe_new_shards(cast(httpx.Client, client), year, known)

    assert new_shards == [(1, "B", new_url_b)]
    assert known_url_a not in client.head_calls  # already known — never re-probed for existence


def test_probe_new_shards_stops_at_unpublished_month() -> None:
    """A month with nothing published yet (suffix A 404s, nothing known) stops
    probing that month's B/C/D — matches discover_zip_urls()'s original behavior."""
    year = 2026
    client = FakeHttpClient(head_responses={})  # every url 404s

    new_shards = _probe_new_shards(cast(httpx.Client, client), year, known={})

    assert new_shards == []
    # Exactly one HEAD per month (suffix A only) — B/C/D never probed once A misses.
    assert len(client.head_calls) == 12
    assert all(url.endswith("A.zip") for url in client.head_calls)


def test_sanity_check_manifest_warns_on_size_mismatch(caplog: pytest.LogCaptureFixture) -> None:
    year = 2025
    url = _archive_url(year, 1, "A")
    known = {1: {"A": {"url": url, "local_path": "/cache/2025/01A.zip", "size_bytes": 100}}}
    client = FakeHttpClient(head_responses={url: FakeResponse(200, content_length="999")})

    with caplog.at_level(logging.WARNING, logger="discovery.stages.extract_signals"):
        _sanity_check_manifest(cast(httpx.Client, client), year, known)

    assert "ARCHIVE_SIZE_MISMATCH" in caplog.text
    assert "manifest_size=100" in caplog.text
    assert "remote_size=999" in caplog.text


def test_sanity_check_manifest_silent_when_sizes_match(caplog: pytest.LogCaptureFixture) -> None:
    year = 2025
    url = _archive_url(year, 1, "A")
    known = {1: {"A": {"url": url, "local_path": "/cache/2025/01A.zip", "size_bytes": 100}}}
    client = FakeHttpClient(head_responses={url: FakeResponse(200, content_length="100")})

    with caplog.at_level(logging.WARNING, logger="discovery.stages.extract_signals"):
        _sanity_check_manifest(cast(httpx.Client, client), year, known)

    assert "ARCHIVE_SIZE_MISMATCH" not in caplog.text


def test_sanity_check_manifest_does_not_crash_on_unavailable_archive() -> None:
    """A previously-downloaded archive that now 404s (shouldn't happen, but the IRS
    site is out of our control) is skipped, not treated as a mismatch or a crash."""
    year = 2025
    url = _archive_url(year, 1, "A")
    known = {1: {"A": {"url": url, "local_path": "/cache/2025/01A.zip", "size_bytes": 100}}}
    client = FakeHttpClient(head_responses={url: FakeResponse(404)})

    _sanity_check_manifest(cast(httpx.Client, client), year, known)  # must not raise


def test_sync_archive_manifest_downloads_fresh_shard_and_records_it(tmp_path: Path) -> None:
    year = 2025
    url_jan_a = _archive_url(year, 1, "A")
    client = FakeHttpClient(head_responses={url_jan_a: FakeResponse(200)}, stream_content=b"fake-zip-bytes")
    conn = FakeManifestConn()

    sync_archive_manifest(cast(httpx.Client, client), cast(psycopg.Connection, conn), [year], tmp_path)

    local_path = tmp_path / str(year) / "2025_TEOS_XML_01A.zip"
    assert local_path.read_bytes() == b"fake-zip-bytes"
    assert conn.rows[(year, 1, "A")]["size_bytes"] == len(b"fake-zip-bytes")
    assert conn.rows[(year, 1, "A")]["local_path"] == str(local_path)
    assert conn.commits >= 1


def test_sync_archive_manifest_never_redownloads_known_shard(tmp_path: Path) -> None:
    """An archive already in the manifest is sanity-checked (one HEAD call) but never
    downloaded again, even though its file doesn't actually exist on disk in this
    test — proves the skip is unconditional on manifest presence, not a disk check."""
    year = 2025
    url_jan_a = _archive_url(year, 1, "A")
    known_local_path = str(tmp_path / str(year) / "2025_TEOS_XML_01A.zip")
    conn = FakeManifestConn(rows={(year, 1, "A"): {"url": url_jan_a, "local_path": known_local_path, "size_bytes": 100}})
    client = FakeHttpClient(head_responses={url_jan_a: FakeResponse(200, content_length="100")})

    sync_archive_manifest(cast(httpx.Client, client), cast(psycopg.Connection, conn), [year], tmp_path)

    assert not Path(known_local_path).exists()  # never (re-)downloaded
    assert conn.rows[(year, 1, "A")]["size_bytes"] == 100  # untouched


def test_build_year_index_and_fetch_filing_xml_read_local_zip_not_remotezip(tmp_path: Path) -> None:
    """The local-read path (stdlib zipfile against archive_manifest's local_path) that
    replaces the old per-filing RemoteZip(zip_url) open."""
    year = 2025
    member_name = "some/path/123456789_public.xml"
    xml_bytes = b"<Return><ReturnData><IRS990/></ReturnData></Return>"
    zip_path = tmp_path / "2025_TEOS_XML_01A.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr(member_name, xml_bytes)

    conn = FakeManifestConn(
        rows={
            (year, 1, "A"): {
                "url": _archive_url(year, 1, "A"),
                "local_path": str(zip_path),
                "size_bytes": zip_path.stat().st_size,
            }
        }
    )

    year_index = build_year_index(cast(psycopg.Connection, conn), year)

    assert year_index["123456789"] == (str(zip_path), member_name)
    assert fetch_filing_xml(year_index, "123456789") == xml_bytes
    assert fetch_filing_xml(year_index, "not_in_any_archive") is None


def test_known_manifest_shards_groups_by_month_and_suffix() -> None:
    year = 2025
    conn = FakeManifestConn(
        rows={
            (year, 1, "A"): {"url": "u1", "local_path": "p1", "size_bytes": 10},
            (year, 5, "A"): {"url": "u2", "local_path": "p2", "size_bytes": 20},
            (year, 5, "B"): {"url": "u3", "local_path": "p3", "size_bytes": 30},
            (2024, 1, "A"): {"url": "other-year", "local_path": "p4", "size_bytes": 40},  # different year, excluded
        }
    )

    known = _known_manifest_shards(cast(psycopg.Connection, conn), year)

    assert set(known.keys()) == {1, 5}
    assert set(known[5].keys()) == {"A", "B"}
    assert known[1]["A"]["size_bytes"] == 10


def test_extract_signals_for_survivors_calls_sync_archive_manifest_before_reading_filings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Orchestrator wiring: sync happens once per run, across default_target_years(),
    before any filing lookup — not lazily per encountered filing year."""
    calls: list[tuple[Any, ...]] = []

    class NullConn:
        def cursor(self) -> NullCursor:
            return NullCursor()

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    class NullCursor:
        def execute(self, _sql: str, _params: Any = None) -> None:
            return None

        def fetchall(self) -> list[Any]:
            return []

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    class NullHttpClient:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    monkeypatch.setattr(extract_signals.psycopg, "connect", lambda _database_url: NullConn())
    monkeypatch.setattr(extract_signals.httpx, "Client", lambda **_kwargs: NullHttpClient())
    monkeypatch.setattr(
        extract_signals,
        "sync_archive_manifest",
        lambda client, conn, years, cache_dir: calls.append((years, cache_dir)),
    )

    extract_signals.extract_signals_for_survivors("postgres://example", [], cache_dir="/tmp/cache")

    assert len(calls) == 1
    years, cache_dir = calls[0]
    assert list(years) == default_target_years()
    assert cache_dir == "/tmp/cache"
