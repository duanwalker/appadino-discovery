"""Stage 2 integration tests against a real, throwaway local Postgres.

Why these exist: the existing Stage 2 suite (test_extract_signals_stage2_scale.py)
drives extract_signals.py through a hand-rolled fake connection/cursor, so the SQL
introduced by the Stage 2 refactor (commits f280833, d994133) — the
`_select_eins_already_signaled` two-subquery join, `_select_filing_rows`' LEFT JOIN +
ORDER BY, and `_flush_stage2_batch`'s executemany batches — has never actually been
executed by Postgres. These tests run it for real: a `postgres:16` container on a
random free port, schema applied by the repo's own alembic migrations, real rows,
real constraints, real transaction semantics. Where the Docker daemon can't run at
all (a broken Docker Desktop/WSL backend is common on Windows) the harness falls back
to `initdb`/`pg_ctl` on PATH and builds an equally throwaway local cluster instead.

Deliberately self-contained and disposable:
  - never reads DATABASE_URL (or any credential) from the environment — the container
    URL is constructed locally and passed explicitly, and the alembic subprocess gets
    an env with DATABASE_URL *overwritten*, so a developer's Azure connection string
    being exported can't redirect a migration at the real database;
  - never touches Azure, Key Vault, or the network: `sync_archive_manifest` (the only
    outbound HTTP in Stage 2) is monkeypatched to a no-op in every test that calls the
    orchestrator, and archives are tiny zips built in tmp_path;
  - skips cleanly (not fails) when no local Postgres backend is available at all.

Production code is not modified by this module. Anything that behaved differently from
the docstrings is recorded in a `DIVERGENCE:` comment on the relevant test.
"""

from __future__ import annotations

import os
import random
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import zipfile
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Json

from discovery.stages import extract_signals
from discovery.stages.extract_signals import (
    ArchiveFileMissingError,
    _flush_stage2_batch,
    _select_eins_already_signaled,
    _select_filing_rows,
    extract_signals_for_survivors,
)
from discovery.stages.filter import build_survivor_query

POSTGRES_IMAGE = "postgres:16"
PIPELINE_DIR = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.integration


# --------------------------------------------------------------------------------
# Throwaway Postgres
# --------------------------------------------------------------------------------


def _docker_available() -> bool:
    """True only if the docker CLI exists *and* its daemon answers — `docker info`
    returns non-zero (rather than raising) when the CLI is installed but Docker
    Desktop isn't running, which is the common local case."""
    try:
        return (
            subprocess.run(["docker", "info"], capture_output=True, timeout=30, check=False).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        return False


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_until_ready(url: str, timeout_s: float = 90.0) -> None:
    deadline = time.monotonic() + timeout_s
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with psycopg.connect(url, connect_timeout=3):
                return
        except psycopg.Error as exc:  # container still booting
            last_error = exc
            time.sleep(0.5)
    raise TimeoutError(f"postgres container not ready within {timeout_s}s: {last_error}")


def _apply_schema(url: str) -> None:
    """Apply the repo's real schema with `alembic upgrade head`.

    DATABASE_URL is *overwritten* (not defaulted) in the subprocess env so an exported
    Azure connection string in the developer's shell can never become the target here.
    """
    env = {**os.environ, "DATABASE_URL": url}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=PIPELINE_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"alembic upgrade head failed:\n{result.stdout}\n{result.stderr}")


@contextmanager
def _docker_server() -> Iterator[str]:
    port = _free_port()
    name = f"stage2-it-{random.randint(10**6, 10**7 - 1)}"
    url = f"postgresql://postgres:stage2test@127.0.0.1:{port}/postgres"

    run = subprocess.run(
        [
            "docker", "run", "--rm", "-d",
            "--name", name,
            "-p", f"127.0.0.1:{port}:5432",
            "-e", "POSTGRES_PASSWORD=stage2test",
            "-e", "POSTGRES_DB=postgres",
            POSTGRES_IMAGE,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if run.returncode != 0:
        raise RuntimeError(f"could not start {POSTGRES_IMAGE}: {run.stderr.strip()}")

    container_id = run.stdout.strip()
    try:
        _wait_until_ready(url)
        yield url
    finally:
        subprocess.run(["docker", "kill", container_id], capture_output=True, check=False)


@contextmanager
def _local_binaries_server() -> Iterator[str]:
    """Fallback for machines where the Docker daemon can't run (a broken Docker
    Desktop/WSL backend is common on Windows): initdb a brand-new throwaway cluster
    from `initdb`/`pg_ctl` on PATH and run it on a free loopback port.

    Still a throwaway, still local, still credential-free: the cluster is created
    here, its superuser password is generated per-run (never read from the
    environment), it listens on 127.0.0.1 only, and the whole data directory is
    deleted on teardown.
    """
    initdb = shutil.which("initdb")
    pg_ctl = shutil.which("pg_ctl")
    if not initdb or not pg_ctl:
        raise RuntimeError("initdb/pg_ctl not on PATH")

    root = Path(tempfile.mkdtemp(prefix="stage2-it-"))
    data_dir = root / "data"
    pwfile = root / "pw"
    password = f"t{random.randint(10**12, 10**13 - 1)}"
    pwfile.write_text(password, encoding="ascii")
    port = _free_port()
    url = f"postgresql://postgres:{password}@127.0.0.1:{port}/postgres"

    started = False
    try:
        init = subprocess.run(
            [initdb, "-D", str(data_dir), "-U", "postgres", f"--pwfile={pwfile}",
             "-E", "UTF8", "--no-locale"],
            capture_output=True, text=True, timeout=300, check=False,
        )
        if init.returncode != 0:
            raise RuntimeError(f"initdb failed:\n{init.stdout}\n{init.stderr}")
        pwfile.unlink()

        start = subprocess.run(
            [pg_ctl, "-D", str(data_dir), "-l", str(root / "pg.log"),
             "-o", f"-p {port} -c listen_addresses=127.0.0.1", "-w", "start"],
            capture_output=True, text=True, timeout=180, check=False,
        )
        if start.returncode != 0:
            raise RuntimeError(f"pg_ctl start failed:\n{start.stdout}\n{start.stderr}")
        started = True

        _wait_until_ready(url)
        yield url
    finally:
        if started:
            subprocess.run(
                [pg_ctl, "-D", str(data_dir), "-m", "immediate", "-w", "stop"],
                capture_output=True, timeout=120, check=False,
            )
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture(scope="module")
def _pg_server() -> Iterator[str]:
    """One server per module (migrations are slow); per-test isolation comes from the
    function-scoped `pg` fixture truncating the tables these tests touch.

    Docker is the primary backend; local `initdb`/`pg_ctl` binaries are the fallback.
    If neither is available the whole module skips rather than fails.
    """
    if _docker_available():
        backend = _docker_server()
    elif shutil.which("initdb") and shutil.which("pg_ctl"):
        backend = _local_binaries_server()
    else:
        pytest.skip(
            "no local Postgres backend available (docker daemon down and initdb/pg_ctl "
            "not on PATH) — skipping Stage 2 Postgres integration tests"
        )

    with backend as url:
        _apply_schema(url)
        yield url


# Seeded rows (clients, icp_configs, suppression) come from the repo's own migrations;
# truncating everything would drop them, so only Stage 2's tables are reset per test.
_STAGE2_TABLES = ("signals", "filings", "archive_manifest", "organizations")


@pytest.fixture
def pg(_pg_server: str) -> Iterator[str]:
    with psycopg.connect(_pg_server) as conn, conn.cursor() as cur:
        cur.execute(f"TRUNCATE {', '.join(_STAGE2_TABLES)} RESTART IDENTITY CASCADE")
        conn.commit()
    yield _pg_server


@pytest.fixture
def conn(pg: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(pg) as connection:
        yield connection


# --------------------------------------------------------------------------------
# Row helpers
# --------------------------------------------------------------------------------


def _insert_org(conn: psycopg.Connection, ein: str, ruling_year: int | None = 2000) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organizations (ein, name, ruling_year, bmf_updated_at) VALUES (%s, %s, %s, %s)",
            (ein, f"Org {ein}", ruling_year, datetime.now(UTC)),
        )
    conn.commit()


def _insert_filing(
    conn: psycopg.Connection,
    ein: str,
    tax_year: int,
    object_id: str,
    xml_object_url: str = "https://apps.irs.gov/pub/epostcard/990/xml/2023/",
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO filings (ein, tax_year, form_type, object_id, xml_object_url) "
            "VALUES (%s, %s, '990', %s, %s) RETURNING id",
            (ein, tax_year, object_id, xml_object_url),
        )
        row = cur.fetchone()
    conn.commit()
    assert row is not None
    return int(row[0])


def _insert_signal(conn: psycopg.Connection, ein: str, tax_year: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO signals (ein, tax_year, signal, computed_at) VALUES (%s, %s, %s, %s)",
            (ein, tax_year, Json({"seeded": True}), datetime.now(UTC)),
        )
    conn.commit()


def _insert_manifest(
    conn: psycopg.Connection, year: int, month: int, suffix: str, local_path: str
) -> None:
    path = Path(local_path)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO archive_manifest "
            "(year, month, suffix, url, local_path, size_bytes, downloaded_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                year,
                month,
                suffix,
                f"https://apps.irs.gov/pub/epostcard/990/xml/{year}/{year}_TEOS_XML_{month:02d}{suffix}.zip",
                local_path,
                path.stat().st_size if path.exists() else 1,
                datetime.now(UTC),
            ),
        )
    conn.commit()


def _filing_params(filing_id: int, **overrides: Any) -> dict[str, Any]:
    params: dict[str, Any] = {
        "id": filing_id,
        "revenue_total": None,
        "contributions": None,
        "program_revenue": None,
        "govt_grants": None,
        "fundraising_expense": None,
        "officers": Json([]),
        "mission_text": None,
        "program_text": Json([]),
        "significant_change_ind": None,
        "extracted_at": datetime.now(UTC),
    }
    params.update(overrides)
    return params


# --------------------------------------------------------------------------------
# Synthetic IRS archive
# --------------------------------------------------------------------------------


def _sample_990(
    *,
    revenue_total: int,
    website: str | None = None,
    officer_title: str = "Director of Development",
) -> bytes:
    website_tag = f"<WebsiteAddressTxt>{website}</WebsiteAddressTxt>" if website else ""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Return xmlns="http://www.irs.gov/efile">
  <ReturnData>
    <IRS990>
      <CYTotalRevenueAmt>{revenue_total}</CYTotalRevenueAmt>
      <CYContributionsGrantsAmt>{revenue_total // 4}</CYContributionsGrantsAmt>
      <CYProgramServiceRevenueAmt>{revenue_total // 2}</CYProgramServiceRevenueAmt>
      <GovernmentGrantsAmt>{revenue_total // 10}</GovernmentGrantsAmt>
      <TotalFunctionalExpensesGrp>
        <FundraisingAmt>{revenue_total // 20}</FundraisingAmt>
      </TotalFunctionalExpensesGrp>
      <MissionDesc>Mission for a synthetic test filing.</MissionDesc>
      {website_tag}
      <Desc>Program one accomplishments.</Desc>
      <ExpenseAmt>1234</ExpenseAmt>
      <RevenueAmt>5678</RevenueAmt>
      <Form990PartVIISectionAGrp>
        <PersonNm>Pat Example</PersonNm>
        <TitleTxt>{officer_title}</TitleTxt>
        <OfficerInd>X</OfficerInd>
      </Form990PartVIISectionAGrp>
      <SignificantChangeInd>true</SignificantChangeInd>
    </IRS990>
  </ReturnData>
</Return>
""".encode()


def _build_archive(path: Path, members: dict[str, bytes]) -> Path:
    """Writes a tiny stand-in for an IRS monthly shard: members are named
    `<object_id>_public.xml`, which is what build_year_index keys on."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        for object_id, xml_bytes in members.items():
            archive.writestr(f"{object_id}_public.xml", xml_bytes)
    return path


class _NullHttpClient(AbstractContextManager["_NullHttpClient"]):
    def __exit__(self, *exc: object) -> None:
        return None


@pytest.fixture
def offline_stage2(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keeps orchestrator runs entirely local: no IRS HEAD/GET requests, no manifest
    sync (tests seed archive_manifest themselves)."""
    monkeypatch.setattr(extract_signals.httpx, "Client", lambda **_kw: _NullHttpClient())
    monkeypatch.setattr(extract_signals, "sync_archive_manifest", lambda *a, **k: None)


# ================================================================================
# 1. _select_eins_already_signaled
# ================================================================================


class TestSelectEinsAlreadySignaled:
    def test_signal_at_latest_filing_year_is_returned(self, conn: psycopg.Connection) -> None:
        _insert_org(conn, "100000001")
        _insert_filing(conn, "100000001", 2023, "obj-a")
        _insert_signal(conn, "100000001", 2023)

        assert _select_eins_already_signaled(conn, ["100000001"]) == {"100000001"}

    def test_signal_newer_than_latest_filing_is_returned(self, conn: psycopg.Connection) -> None:
        """`>=`, not `==`: a signal ahead of the latest filing still counts as done."""
        _insert_org(conn, "100000002")
        _insert_filing(conn, "100000002", 2022, "obj-b")
        _insert_signal(conn, "100000002", 2023)

        assert _select_eins_already_signaled(conn, ["100000002"]) == {"100000002"}

    def test_signal_older_than_latest_filing_is_not_returned(self, conn: psycopg.Connection) -> None:
        _insert_org(conn, "100000003")
        _insert_filing(conn, "100000003", 2022, "obj-c1")
        _insert_filing(conn, "100000003", 2023, "obj-c2")
        _insert_signal(conn, "100000003", 2022)

        assert _select_eins_already_signaled(conn, ["100000003"]) == set()

    def test_no_signal_is_not_returned(self, conn: psycopg.Connection) -> None:
        _insert_org(conn, "100000004")
        _insert_filing(conn, "100000004", 2023, "obj-d")

        assert _select_eins_already_signaled(conn, ["100000004"]) == set()

    def test_ein_with_no_filings_is_not_returned(self, conn: psycopg.Connection) -> None:
        """The INNER JOIN between the two aggregates means an EIN with a signal but no
        filings row drops out — there's no left-hand row to satisfy the >= comparison.
        Pinned because it's the one case where "not returned" means "will be
        reprocessed and produce nothing", not "has work pending"."""
        _insert_org(conn, "100000005")
        _insert_signal(conn, "100000005", 2023)  # signal, but zero filings

        assert _select_eins_already_signaled(conn, ["100000005"]) == set()

    def test_unrelated_eins_do_not_leak_in(self, conn: psycopg.Connection) -> None:
        """Both ANY(%s) placeholders must be bound to the same list — a bug binding
        only one would let an out-of-scope EIN appear in the result."""
        for ein in ("100000006", "100000007"):
            _insert_org(conn, ein)
            _insert_filing(conn, ein, 2023, f"obj-{ein}")
            _insert_signal(conn, ein, 2023)

        assert _select_eins_already_signaled(conn, ["100000006"]) == {"100000006"}

    def test_empty_ein_list_returns_empty_set(self, conn: psycopg.Connection) -> None:
        assert _select_eins_already_signaled(conn, []) == set()

    def test_120k_ein_list_is_accepted_and_fast(self, conn: psycopg.Connection) -> None:
        """National scale: ~120k EINs through ANY(%s). Guards against both a hard
        parameter/protocol limit and an accidental O(n^2) plan."""
        eins = [f"{n:09d}" for n in range(200_000_000, 200_120_000)]
        signaled = eins[:50]
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO organizations (ein, name, ruling_year, bmf_updated_at) "
                "VALUES (%s, 'bulk', 2000, now())",
                [(ein,) for ein in eins[:1000]],
            )
            cur.executemany(
                "INSERT INTO filings (ein, tax_year, form_type, object_id, xml_object_url) "
                "VALUES (%s, 2023, '990', %s, 'https://apps.irs.gov/pub/epostcard/990/xml/2023/')",
                [(ein, f"bulk-{ein}") for ein in eins[:1000]],
            )
            cur.executemany(
                "INSERT INTO signals (ein, tax_year, signal, computed_at) "
                "VALUES (%s, 2023, '{}'::jsonb, now())",
                [(ein,) for ein in signaled],
            )
        conn.commit()

        started = time.monotonic()
        result = _select_eins_already_signaled(conn, eins)
        elapsed = time.monotonic() - started

        assert result == set(signaled)
        assert elapsed < 30.0, f"120k-EIN resume check took {elapsed:.1f}s"


# ================================================================================
# 2. _select_filing_rows
# ================================================================================


class TestSelectFilingRows:
    def test_left_join_returns_ruling_year(self, conn: psycopg.Connection) -> None:
        _insert_org(conn, "200000001", ruling_year=1987)
        filing_id = _insert_filing(conn, "200000001", 2023, "obj-g")

        rows = _select_filing_rows(conn, ["200000001"])

        assert len(rows) == 1
        assert rows[0][0] == filing_id
        assert rows[0][1] == "200000001"
        assert rows[0][5] == 1987

    def test_filing_without_organization_row_returned_with_null_ruling_year(
        self, conn: psycopg.Connection
    ) -> None:
        """The LEFT-vs-INNER distinction the docstring calls out. No organizations row
        is inserted — the filing must still come back rather than vanishing."""
        filing_id = _insert_filing(conn, "200000002", 2023, "obj-h")

        rows = _select_filing_rows(conn, ["200000002"])

        assert len(rows) == 1
        assert rows[0][0] == filing_id
        assert rows[0][5] is None

    def test_rows_ordered_ein_asc_tax_year_desc(self, conn: psycopg.Connection) -> None:
        """The per-EIN streaming in extract_signals_for_survivors depends on this exact
        order — rows for an EIN must be contiguous, newest filing first."""
        for ein in ("200000030", "200000010", "200000020"):
            _insert_org(conn, ein)
            for tax_year in (2021, 2023, 2022):  # deliberately scrambled on insert
                _insert_filing(conn, ein, tax_year, f"obj-{ein}-{tax_year}")

        rows = _select_filing_rows(conn, ["200000030", "200000010", "200000020"])

        assert [(row[1], row[2]) for row in rows] == [
            ("200000010", 2023), ("200000010", 2022), ("200000010", 2021),
            ("200000020", 2023), ("200000020", 2022), ("200000020", 2021),
            ("200000030", 2023), ("200000030", 2022), ("200000030", 2021),
        ]

    def test_one_row_per_filing_no_join_fan_out(self, conn: psycopg.Connection) -> None:
        """organizations.ein is the PK, so one filing can only join one org row — a
        fan-out here would double-count filings and corrupt the 2-filing buffer
        compute_signals reads."""
        _insert_org(conn, "200000003", ruling_year=1999)
        _insert_filing(conn, "200000003", 2023, "obj-i")

        assert len(_select_filing_rows(conn, ["200000003"])) == 1


# ================================================================================
# 3. _flush_stage2_batch
# ================================================================================


class TestFlushStage2Batch:
    def test_json_params_round_trip_exactly(self, conn: psycopg.Connection, pg: str) -> None:
        _insert_org(conn, "300000001")
        filing_id = _insert_filing(conn, "300000001", 2023, "obj-j")

        officers = [
            {"name": "Pat Example", "title": "Director of Development", "is_officer": True},
            {"name": "Sam Example", "title": None, "is_officer": False},
        ]
        program_text = [
            {"desc": "Program one", "expense": 1234, "revenue": 5678},
            {"desc": "Program two", "expense": None, "revenue": None},
        ]
        signal = {
            "gov_funding_pct": 0.123456789,
            "revenue_composition": {"contributions_pct": 0.25, "program_pct": 0.5, "govt_pct": 0.1},
            "dd_present": True,
            "fundraising_spend_ratio": 0.05,
            "org_age": 23,
            "revenue_trend": None,
        }

        _flush_stage2_batch(
            conn,
            [
                _filing_params(
                    filing_id,
                    revenue_total=1_000_000,
                    contributions=250_000,
                    officers=Json(officers),
                    mission_text="Mission text with 'quotes' and \"doubles\".",
                    program_text=Json(program_text),
                    significant_change_ind=True,
                )
            ],
            [{"website": "https://example.org", "ein": "300000001"}],
            [
                {
                    "ein": "300000001",
                    "tax_year": 2023,
                    "signal": Json(signal),
                    "computed_at": datetime.now(UTC),
                }
            ],
        )

        with psycopg.connect(pg) as verify, verify.cursor() as cur:
            cur.execute(
                "SELECT revenue_total, contributions, officers, mission_text, program_text, "
                "significant_change_ind, extracted_at FROM filings WHERE id = %s",
                (filing_id,),
            )
            row = cur.fetchone()
            assert row is not None
            assert int(row[0]) == 1_000_000
            assert int(row[1]) == 250_000
            assert row[2] == officers
            assert row[3] == "Mission text with 'quotes' and \"doubles\"."
            assert row[4] == program_text
            assert row[5] is True
            assert row[6] is not None

            cur.execute("SELECT website FROM organizations WHERE ein = %s", ("300000001",))
            assert cur.fetchall() == [("https://example.org",)]

            cur.execute(
                "SELECT signal FROM signals WHERE ein = %s AND tax_year = %s", ("300000001", 2023)
            )
            assert cur.fetchall() == [(signal,)]

    def test_website_update_touches_only_the_named_ein(
        self, conn: psycopg.Connection, pg: str
    ) -> None:
        for ein in ("300000010", "300000011", "300000012"):
            _insert_org(conn, ein)
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE organizations SET website = %s WHERE ein = %s",
                ("https://pre.example", "300000012"),
            )
        conn.commit()

        _flush_stage2_batch(
            conn, [], [{"website": "https://only-this.example", "ein": "300000011"}], []
        )

        with psycopg.connect(pg) as verify, verify.cursor() as cur:
            cur.execute("SELECT ein, website FROM organizations ORDER BY ein")
            assert cur.fetchall() == [
                ("300000010", None),
                ("300000011", "https://only-this.example"),
                ("300000012", "https://pre.example"),
            ]

    def test_signal_upsert_overwrites_on_conflict(self, conn: psycopg.Connection, pg: str) -> None:
        _insert_org(conn, "300000020")
        _insert_signal(conn, "300000020", 2023)  # seeded {"seeded": true}

        _flush_stage2_batch(
            conn,
            [],
            [],
            [
                {
                    "ein": "300000020",
                    "tax_year": 2023,
                    "signal": Json({"recomputed": True}),
                    "computed_at": datetime.now(UTC),
                }
            ],
        )

        with psycopg.connect(pg) as verify, verify.cursor() as cur:
            cur.execute("SELECT signal FROM signals WHERE ein = %s", ("300000020",))
            assert cur.fetchall() == [({"recomputed": True},)]

    def test_failure_on_last_statement_rolls_back_whole_batch(
        self, conn: psycopg.Connection, pg: str
    ) -> None:
        """Forces a real constraint error on the *final* statement group (a NOT NULL
        violation on signals.computed_at) and asserts the earlier filing UPDATE and
        website UPDATE in the same batch are not committed.

        This is the invariant the EIN-level resume check rests on: a batch is
        all-or-nothing, so a visible signals row always implies its filings rows
        landed too.
        """
        _insert_org(conn, "300000030")
        filing_id = _insert_filing(conn, "300000030", 2023, "obj-k")

        with pytest.raises(psycopg.Error):
            _flush_stage2_batch(
                conn,
                [_filing_params(filing_id, revenue_total=999_999, mission_text="should not persist")],
                [{"website": "https://should-not-persist.example", "ein": "300000030"}],
                [
                    {
                        "ein": "300000030",
                        "tax_year": 2023,
                        "signal": Json({"ok": True}),
                        "computed_at": datetime.now(UTC),
                    },
                    # NOT NULL violation — the batch's last statement.
                    {
                        "ein": "300000030",
                        "tax_year": 2024,
                        "signal": Json({"bad": True}),
                        "computed_at": None,
                    },
                ],
            )
        conn.rollback()

        # Separate connection: proves nothing was *committed*, not merely that this
        # session's aborted transaction can't see it.
        with psycopg.connect(pg) as verify, verify.cursor() as cur:
            cur.execute("SELECT revenue_total, mission_text FROM filings WHERE id = %s", (filing_id,))
            assert cur.fetchall() == [(None, None)]
            cur.execute("SELECT website FROM organizations WHERE ein = %s", ("300000030",))
            assert cur.fetchall() == [(None,)]
            cur.execute("SELECT count(*) FROM signals WHERE ein = %s", ("300000030",))
            assert cur.fetchall() == [(0,)]

    def test_empty_batch_commits_cleanly(self, conn: psycopg.Connection) -> None:
        _flush_stage2_batch(conn, [], [], [])  # must not raise


# ================================================================================
# 4. Resume, end to end
# ================================================================================


RESUME_EINS = ["400000001", "400000002", "400000003", "400000004"]


@pytest.fixture
def resume_world(conn: psycopg.Connection, tmp_path: Path) -> dict[str, Any]:
    """Four survivors x two filings each, all resolvable out of one tiny synthetic
    archive recorded in archive_manifest."""
    members: dict[str, bytes] = {}
    for index, ein in enumerate(RESUME_EINS):
        _insert_org(conn, ein, ruling_year=1990 + index)
        for tax_year, revenue in ((2023, 500_000 + index), (2022, 400_000 + index)):
            object_id = f"obj-{ein}-{tax_year}"
            _insert_filing(conn, ein, tax_year, object_id)
            members[object_id] = _sample_990(
                revenue_total=revenue,
                website=f"https://{ein}.example" if tax_year == 2023 else None,
            )

    archive = _build_archive(tmp_path / "2023" / "2023_TEOS_XML_01A.zip", members)
    _insert_manifest(conn, 2023, 1, "A", str(archive))
    return {"archive": archive, "cache_dir": str(tmp_path)}


def _snapshot(dsn: str) -> dict[str, Any]:
    """Everything Stage 2 writes, minus the wall-clock columns (extracted_at /
    computed_at), which differ between runs by construction."""
    with psycopg.connect(dsn) as verify, verify.cursor() as cur:
        cur.execute(
            "SELECT ein, tax_year, revenue_total, contributions, program_revenue, govt_grants, "
            "fundraising_expense, officers, mission_text, program_text, significant_change_ind, "
            "(extracted_at IS NOT NULL) FROM filings ORDER BY ein, tax_year"
        )
        filings = cur.fetchall()
        cur.execute("SELECT ein, tax_year, signal FROM signals ORDER BY ein, tax_year")
        signals = cur.fetchall()
        cur.execute("SELECT ein, website FROM organizations ORDER BY ein")
        websites = cur.fetchall()
    return {"filings": filings, "signals": signals, "websites": websites}


class TestResumeEndToEnd:
    def test_interrupted_run_resumes_to_same_state_as_one_clean_run(
        self,
        pg: str,
        resume_world: dict[str, Any],
        offline_stage2: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        real_parse = extract_signals.parse_990_xml
        calls = {"n": 0}

        def exploding_parse(xml_bytes: bytes) -> dict[str, Any]:
            calls["n"] += 1
            if calls["n"] == 5:
                # BaseException, so the orchestrator's `except Exception` per-filing
                # tolerance can't swallow it — this is a job kill, not a bad filing.
                raise KeyboardInterrupt("simulated job kill mid-run")
            return real_parse(xml_bytes)

        monkeypatch.setattr(extract_signals, "parse_990_xml", exploding_parse)
        with pytest.raises(KeyboardInterrupt):
            extract_signals_for_survivors(pg, RESUME_EINS, cache_dir=resume_world["cache_dir"])
        monkeypatch.setattr(extract_signals, "parse_990_xml", real_parse)

        # Core resume invariant: never a signals row whose own filings row wasn't
        # extracted — that would make the next run skip an EIN whose filings are blank.
        with psycopg.connect(pg) as verify, verify.cursor() as cur:
            cur.execute(
                "SELECT s.ein, s.tax_year FROM signals s "
                "JOIN filings f ON f.ein = s.ein AND f.tax_year = s.tax_year "
                "WHERE f.extracted_at IS NULL"
            )
            orphans = cur.fetchall()
        assert orphans == [], f"signals rows exist for unextracted filings: {orphans}"

        extract_signals_for_survivors(pg, RESUME_EINS, cache_dir=resume_world["cache_dir"])
        after_resume = _snapshot(pg)

        # Reset to the identical starting state and do one uninterrupted run.
        with psycopg.connect(pg) as reset, reset.cursor() as cur:
            cur.execute("TRUNCATE signals RESTART IDENTITY")
            cur.execute(
                "UPDATE filings SET revenue_total = NULL, contributions = NULL, "
                "program_revenue = NULL, govt_grants = NULL, fundraising_expense = NULL, "
                "officers = NULL, mission_text = NULL, program_text = NULL, "
                "significant_change_ind = NULL, extracted_at = NULL"
            )
            cur.execute("UPDATE organizations SET website = NULL")
            reset.commit()

        extract_signals_for_survivors(pg, RESUME_EINS, cache_dir=resume_world["cache_dir"])
        clean = _snapshot(pg)

        assert after_resume["filings"] == clean["filings"]
        assert after_resume["signals"] == clean["signals"]
        assert after_resume["websites"] == clean["websites"]

    def test_every_survivor_ends_up_fully_extracted_after_resume(
        self,
        pg: str,
        resume_world: dict[str, Any],
        offline_stage2: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The failure mode the EIN-level skip could hide: an EIN gets a signals row,
        is skipped on the rerun, but still has filings rows that were never
        extracted."""
        real_parse = extract_signals.parse_990_xml
        calls = {"n": 0}

        def exploding_parse(xml_bytes: bytes) -> dict[str, Any]:
            calls["n"] += 1
            if calls["n"] == 3:
                raise KeyboardInterrupt("simulated job kill mid-run")
            return real_parse(xml_bytes)

        monkeypatch.setattr(extract_signals, "parse_990_xml", exploding_parse)
        with pytest.raises(KeyboardInterrupt):
            extract_signals_for_survivors(pg, RESUME_EINS, cache_dir=resume_world["cache_dir"])
        monkeypatch.setattr(extract_signals, "parse_990_xml", real_parse)

        extract_signals_for_survivors(pg, RESUME_EINS, cache_dir=resume_world["cache_dir"])

        with psycopg.connect(pg) as verify, verify.cursor() as cur:
            cur.execute(
                "SELECT ein FROM filings WHERE extracted_at IS NULL AND ein = ANY(%s)", (RESUME_EINS,)
            )
            unextracted = sorted({row[0] for row in cur.fetchall()})
            cur.execute("SELECT DISTINCT ein FROM signals WHERE ein = ANY(%s)", (RESUME_EINS,))
            signaled = sorted({row[0] for row in cur.fetchall()})

        assert signaled == sorted(RESUME_EINS)
        assert unextracted == [], f"EINs left signaled but with unextracted filings: {unextracted}"

    def test_second_run_skips_everything_already_signaled(
        self, pg: str, resume_world: dict[str, Any], offline_stage2: None
    ) -> None:
        first = extract_signals_for_survivors(pg, RESUME_EINS, cache_dir=resume_world["cache_dir"])
        assert first["signals_computed"] == len(RESUME_EINS)
        assert first["eins_skipped_already_signaled"] == 0

        second = extract_signals_for_survivors(pg, RESUME_EINS, cache_dir=resume_world["cache_dir"])
        assert second["eins_skipped_already_signaled"] == len(RESUME_EINS)
        assert second["filings_parsed"] == 0
        assert second["signals_computed"] == 0

    def test_force_recompute_bypasses_the_skip(
        self, pg: str, resume_world: dict[str, Any], offline_stage2: None
    ) -> None:
        extract_signals_for_survivors(pg, RESUME_EINS, cache_dir=resume_world["cache_dir"])
        forced = extract_signals_for_survivors(
            pg, RESUME_EINS, cache_dir=resume_world["cache_dir"], force_recompute=True
        )
        assert forced["eins_skipped_already_signaled"] == 0
        assert forced["signals_computed"] == len(RESUME_EINS)

    def test_newer_filing_after_a_signal_is_reprocessed(
        self,
        conn: psycopg.Connection,
        pg: str,
        resume_world: dict[str, Any],
        offline_stage2: None,
    ) -> None:
        extract_signals_for_survivors(pg, RESUME_EINS, cache_dir=resume_world["cache_dir"])

        ein = RESUME_EINS[0]
        object_id = f"obj-{ein}-2024"
        with zipfile.ZipFile(resume_world["archive"], "a") as archive:
            archive.writestr(f"{object_id}_public.xml", _sample_990(revenue_total=900_000))
        _insert_filing(conn, ein, 2024, object_id)

        again = extract_signals_for_survivors(pg, RESUME_EINS, cache_dir=resume_world["cache_dir"])

        assert again["eins_skipped_already_signaled"] == len(RESUME_EINS) - 1
        assert again["signals_computed"] == 1
        with psycopg.connect(pg) as verify, verify.cursor() as cur:
            cur.execute("SELECT max(tax_year) FROM signals WHERE ein = %s", (ein,))
            assert cur.fetchall() == [(2024,)]


# ================================================================================
# 5. build_survivor_query determinism
# ================================================================================


class TestBuildSurvivorQueryDeterminism:
    @pytest.fixture
    def survivors(self, conn: psycopg.Connection) -> list[str]:
        eins = [f"5{n:08d}" for n in range(60)]
        shuffled = eins[:]
        random.Random(1234).shuffle(shuffled)
        with conn.cursor() as cur:
            for ein in shuffled:  # scrambled physical insert order on purpose
                cur.execute(
                    "INSERT INTO organizations (ein, name, ruling_year, revenue_latest, "
                    "foundation_code, ntee, bmf_updated_at) "
                    "VALUES (%s, 'Survivor', 2000, 2000000, '15', 'T30', now())",
                    (ein,),
                )
                cur.execute(
                    "INSERT INTO filings (ein, tax_year, form_type, object_id, xml_object_url) "
                    "VALUES (%s, 2023, '990', %s, "
                    "'https://apps.irs.gov/pub/epostcard/990/xml/2023/')",
                    (ein, f"surv-{ein}"),
                )
        conn.commit()
        return sorted(eins)

    def test_repeated_runs_return_the_same_sequence(
        self, conn: psycopg.Connection, survivors: list[str]
    ) -> None:
        query, params = build_survivor_query({})
        runs = []
        for _ in range(5):
            with conn.cursor() as cur:
                cur.execute(query, params)
                runs.append([row[0] for row in cur.fetchall()])
        assert all(run == runs[0] for run in runs)
        assert runs[0] == survivors

    def test_limit_eins_picks_the_same_first_n(
        self, conn: psycopg.Connection, survivors: list[str]
    ) -> None:
        """--limit-eins truncates the survivor list in Python, so the guarantee lives
        entirely in this query's ORDER BY."""
        query, params = build_survivor_query({})
        limit = 10
        samples = []
        for _ in range(5):
            with conn.cursor() as cur:
                cur.execute(query, params)
                samples.append([row[0] for row in cur.fetchall()][:limit])
        assert all(sample == samples[0] for sample in samples)
        assert samples[0] == survivors[:limit]

    def test_order_survives_a_plan_change(
        self, conn: psycopg.Connection, survivors: list[str]
    ) -> None:
        """Same query forced onto a different plan (seqscan disabled). An ordering that
        only held by accident of the plan would break here."""
        query, params = build_survivor_query({})
        with conn.cursor() as cur:
            cur.execute(query, params)
            baseline = [row[0] for row in cur.fetchall()]
            cur.execute("SET LOCAL enable_seqscan = off")
            cur.execute(query, params)
            alternate = [row[0] for row in cur.fetchall()]
        conn.rollback()
        assert alternate == baseline
        assert baseline == survivors


# ================================================================================
# 6. ArchiveFileMissingError
# ================================================================================


class TestArchiveFileMissing:
    def test_run_stops_with_year_month_suffix_and_path(
        self, conn: psycopg.Connection, pg: str, tmp_path: Path, offline_stage2: None
    ) -> None:
        _insert_org(conn, "600000001")
        _insert_filing(conn, "600000001", 2023, "obj-missing")

        missing = tmp_path / "2023" / "2023_TEOS_XML_07C.zip"
        missing.parent.mkdir(parents=True, exist_ok=True)
        missing.touch()
        _insert_manifest(conn, 2023, 7, "C", str(missing))
        missing.unlink()  # manifest row now points at nothing

        with pytest.raises(ArchiveFileMissingError) as excinfo:
            extract_signals_for_survivors(pg, ["600000001"], cache_dir=str(tmp_path))

        message = str(excinfo.value)
        assert "year=2023" in message
        assert "month=7" in message
        assert "suffix='C'" in message
        # The message formats the path with !r, which doubles backslashes on Windows.
        assert repr(str(missing))[1:-1] in message
        assert "not auto-deleting" in message

    def test_nothing_from_the_unflushed_batch_is_committed(
        self, conn: psycopg.Connection, pg: str, tmp_path: Path, offline_stage2: None
    ) -> None:
        """One good archive (2023) plus a broken manifest row for a *different* year,
        so filings are parsed into the pending batch before the missing-archive error
        aborts the run — well below STAGE2_BATCH_COMMIT_EVERY_FILINGS, so nothing has
        been flushed yet."""
        good_members: dict[str, bytes] = {}
        for ein in ("600000010", "600000011"):
            _insert_org(conn, ein)
            object_id = f"obj-{ein}-2023"
            _insert_filing(conn, ein, 2023, object_id)
            good_members[object_id] = _sample_990(revenue_total=750_000, website="https://x.example")
        _insert_manifest(
            conn, 2023, 1, "A", str(_build_archive(tmp_path / "2023" / "a.zip", good_members))
        )

        broken_ein = "600000012"
        _insert_org(conn, broken_ein)
        _insert_filing(
            conn,
            broken_ein,
            2022,
            "obj-broken",
            xml_object_url="https://apps.irs.gov/pub/epostcard/990/xml/2022/",
        )
        gone = tmp_path / "2022" / "2022_TEOS_XML_03B.zip"
        gone.parent.mkdir(parents=True, exist_ok=True)
        gone.touch()
        _insert_manifest(conn, 2022, 3, "B", str(gone))
        gone.unlink()

        with pytest.raises(ArchiveFileMissingError):
            extract_signals_for_survivors(
                pg, ["600000010", "600000011", broken_ein], cache_dir=str(tmp_path)
            )

        with psycopg.connect(pg) as verify, verify.cursor() as cur:
            cur.execute("SELECT count(*) FROM filings WHERE extracted_at IS NOT NULL")
            assert cur.fetchall() == [(0,)], "pending batch was committed despite the aborted run"
            cur.execute("SELECT count(*) FROM signals")
            assert cur.fetchall() == [(0,)]
            cur.execute("SELECT count(*) FROM organizations WHERE website IS NOT NULL")
            assert cur.fetchall() == [(0,)]

    def test_cached_zip_handles_are_closed_on_abort(
        self,
        conn: psycopg.Connection,
        pg: str,
        tmp_path: Path,
        offline_stage2: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The finally block must close every cached handle even on the error path — on
        Windows an unclosed handle also leaves the file undeletable, asserted directly
        below."""
        opened: list[zipfile.ZipFile] = []
        real_zipfile = zipfile.ZipFile

        def tracking_zipfile(*args: Any, **kwargs: Any) -> zipfile.ZipFile:
            archive = real_zipfile(*args, **kwargs)
            opened.append(archive)
            return archive

        monkeypatch.setattr(extract_signals.zipfile, "ZipFile", tracking_zipfile)

        _insert_org(conn, "600000020")
        _insert_filing(conn, "600000020", 2023, "obj-good")
        good_archive = _build_archive(
            tmp_path / "2023" / "good.zip", {"obj-good": _sample_990(revenue_total=100_000)}
        )
        _insert_manifest(conn, 2023, 1, "A", str(good_archive))

        _insert_org(conn, "600000021")
        _insert_filing(
            conn,
            "600000021",
            2022,
            "obj-bad",
            xml_object_url="https://apps.irs.gov/pub/epostcard/990/xml/2022/",
        )
        gone = tmp_path / "2022" / "gone.zip"
        gone.parent.mkdir(parents=True, exist_ok=True)
        gone.touch()
        _insert_manifest(conn, 2022, 5, "D", str(gone))
        gone.unlink()

        with pytest.raises(ArchiveFileMissingError):
            extract_signals_for_survivors(pg, ["600000020", "600000021"], cache_dir=str(tmp_path))

        assert opened, "expected at least one archive to have been opened"
        assert all(archive.fp is None for archive in opened), "a cached zip handle was left open"
        good_archive.unlink()  # PermissionError on Windows if a handle survived

    def test_build_year_index_error_names_the_shard(
        self, conn: psycopg.Connection, tmp_path: Path
    ) -> None:
        """Same failure reached directly through build_year_index (the index-building
        path, not the per-filing read path)."""
        missing = tmp_path / "2021" / "2021_TEOS_XML_11B.zip"
        missing.parent.mkdir(parents=True, exist_ok=True)
        missing.touch()
        _insert_manifest(conn, 2021, 11, "B", str(missing))
        missing.unlink()

        with pytest.raises(ArchiveFileMissingError) as excinfo:
            extract_signals.build_year_index(conn, 2021)

        message = str(excinfo.value)
        assert "year=2021" in message
        assert "month=11" in message
        assert "suffix='B'" in message
