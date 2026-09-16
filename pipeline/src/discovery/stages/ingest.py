"""Stage 0 — Ingest (§4). National, shared: populates `organizations` from the IRS
Business Master File and indexes `filings` (latest 2 per EIN) from the IRS 990 e-file
index. No AI, no per-tenant config — this is the universe every client's ICP filters
narrow down from.

Bulk loads use raw psycopg COPY into a staging table + ON CONFLICT upsert rather than
the ORM, since this runs against the full national universe (~1M+ rows). The
SQLAlchemy models in discovery.models remain the source of truth for schema/Alembic.
"""

from __future__ import annotations

import csv
import logging
import os
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from typing import Any

import httpx
import psycopg
from psycopg.types.json import Json

logger = logging.getLogger(__name__)

BMF_URLS = (
    "https://www.irs.gov/pub/irs-soi/eo1.csv",
    "https://www.irs.gov/pub/irs-soi/eo2.csv",
    "https://www.irs.gov/pub/irs-soi/eo3.csv",
    "https://www.irs.gov/pub/irs-soi/eo4.csv",
)

INDEX_URL_TEMPLATE = "https://apps.irs.gov/pub/epostcard/990/xml/{year}/index_{year}.csv"
ARCHIVE_DIR_TEMPLATE = "https://apps.irs.gov/pub/epostcard/990/xml/{year}/"

# 990-PF (private foundations) and 990-T (unrelated business income) are out of scope
# per §4 Stage 1's hard excludes — no point indexing them here.
RELEVANT_RETURN_TYPES = frozenset({"990", "990EZ"})

BATCH_SIZE = 20_000


def default_target_years(as_of: datetime | None = None) -> list[int]:
    """Current submission year plus the 2 prior, to absorb the 12-18mo filing lag (§10)."""
    year = (as_of or datetime.now(UTC)).year
    return [year - 2, year - 1, year]


def _clean_str(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _clean_int(value: str | None) -> int | None:
    value = _clean_str(value)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def transform_bmf_row(raw: dict[str, str]) -> dict[str, Any] | None:
    """Map one raw IRS BMF CSV row to an `organizations` upsert row.

    Returns None if the row has no EIN (shouldn't happen, but a blank cell beats a
    crashed ingest run).
    """
    ein = _clean_str(raw.get("EIN"))
    if ein is None:
        return None
    ruling = _clean_str(raw.get("RULING"))
    ruling_year = int(ruling[:4]) if ruling and len(ruling) >= 4 and ruling[:4].isdigit() else None
    return {
        "ein": ein,
        "name": _clean_str(raw.get("NAME")) or "",
        "state": _clean_str(raw.get("STATE")),
        "city": _clean_str(raw.get("CITY")),
        "ntee": _clean_str(raw.get("NTEE_CD")),
        "ruling_year": ruling_year,
        "revenue_latest": _clean_int(raw.get("REVENUE_AMT")),
        "foundation_code": _clean_str(raw.get("FOUNDATION")),
    }


def transform_index_row(raw: dict[str, str], sub_year: int) -> dict[str, Any] | None:
    """Map one raw 990 e-file index CSV row to a `filings` upsert row.

    Returns None for return types out of scope, or rows missing the fields needed to
    identify and later locate the filing.
    """
    return_type = (_clean_str(raw.get("RETURN_TYPE")) or "").upper()
    if return_type not in RELEVANT_RETURN_TYPES:
        return None
    ein = _clean_str(raw.get("EIN"))
    object_id = _clean_str(raw.get("OBJECT_ID"))
    tax_period = _clean_str(raw.get("TAX_PERIOD"))
    if not (ein and object_id and tax_period and len(tax_period) >= 6 and tax_period[:6].isdigit()):
        return None
    return {
        "ein": ein,
        "tax_year": int(tax_period[:4]),
        "form_type": return_type,
        "object_id": object_id,
        "xml_object_url": ARCHIVE_DIR_TEMPLATE.format(year=sub_year),
    }


def latest_two_per_ein(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep only the 2 most recent filings (by tax_year) per EIN, per §4 Stage 0."""
    by_ein: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_ein.setdefault(row["ein"], []).append(row)
    result: list[dict[str, Any]] = []
    for ein_rows in by_ein.values():
        ein_rows.sort(key=lambda r: r["tax_year"], reverse=True)
        result.extend(ein_rows[:2])
    return result


def fetch_bmf_rows(urls: Iterable[str] = BMF_URLS, client: httpx.Client | None = None) -> Iterator[dict[str, Any]]:
    owns_client = client is None
    client = client or httpx.Client(timeout=120.0, follow_redirects=True)
    try:
        for url in urls:
            logger.info("fetching BMF file %s", url)
            with client.stream("GET", url) as response:
                response.raise_for_status()
                yield from csv.DictReader(response.iter_lines())
    finally:
        if owns_client:
            client.close()


def fetch_index_rows(
    years: Iterable[int], client: httpx.Client | None = None
) -> Iterator[tuple[dict[str, Any], int]]:
    owns_client = client is None
    client = client or httpx.Client(timeout=180.0, follow_redirects=True)
    try:
        for year in years:
            url = INDEX_URL_TEMPLATE.format(year=year)
            logger.info("fetching 990 index %s", url)
            with client.stream("GET", url) as response:
                response.raise_for_status()
                for row in csv.DictReader(response.iter_lines()):
                    yield row, year
    finally:
        if owns_client:
            client.close()


def load_known_eins(conn: psycopg.Connection) -> set[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT ein FROM organizations")
        return {row[0] for row in cur.fetchall()}


def upsert_organizations(conn: psycopg.Connection, rows: Iterable[dict[str, Any]]) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TEMP TABLE IF NOT EXISTS staging_organizations (
                ein text, name text, state text, city text, ntee text,
                ruling_year integer, revenue_latest numeric, foundation_code text
            )
            """
        )
    total = 0
    batch: list[dict[str, Any]] = []
    for row in rows:
        batch.append(row)
        if len(batch) >= BATCH_SIZE:
            total += _flush_organizations_batch(conn, batch)
            batch = []
    if batch:
        total += _flush_organizations_batch(conn, batch)
    return total


def _flush_organizations_batch(conn: psycopg.Connection, batch: list[dict[str, Any]]) -> int:
    with conn.cursor() as cur:
        with cur.copy(
            "COPY staging_organizations "
            "(ein, name, state, city, ntee, ruling_year, revenue_latest, foundation_code) "
            "FROM STDIN"
        ) as copy:
            for row in batch:
                copy.write_row(
                    (
                        row["ein"],
                        row["name"],
                        row["state"],
                        row["city"],
                        row["ntee"],
                        row["ruling_year"],
                        row["revenue_latest"],
                        row["foundation_code"],
                    )
                )
        cur.execute(
            """
            INSERT INTO organizations
                (ein, name, state, city, ntee, ruling_year, revenue_latest, foundation_code, bmf_updated_at)
            SELECT DISTINCT ON (ein)
                ein, name, state, city, ntee, ruling_year, revenue_latest, foundation_code, now()
            FROM staging_organizations
            ON CONFLICT (ein) DO UPDATE SET
                name = EXCLUDED.name,
                state = EXCLUDED.state,
                city = EXCLUDED.city,
                ntee = EXCLUDED.ntee,
                ruling_year = EXCLUDED.ruling_year,
                revenue_latest = EXCLUDED.revenue_latest,
                foundation_code = EXCLUDED.foundation_code,
                bmf_updated_at = EXCLUDED.bmf_updated_at
            """
        )
        cur.execute("TRUNCATE staging_organizations")
    conn.commit()
    return len(batch)


def upsert_filings(conn: psycopg.Connection, rows: Iterable[dict[str, Any]]) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TEMP TABLE IF NOT EXISTS staging_filings (
                ein text, tax_year integer, form_type text, object_id text, xml_object_url text
            )
            """
        )
    total = 0
    batch: list[dict[str, Any]] = []
    for row in rows:
        batch.append(row)
        if len(batch) >= BATCH_SIZE:
            total += _flush_filings_batch(conn, batch)
            batch = []
    if batch:
        total += _flush_filings_batch(conn, batch)
    return total


def _flush_filings_batch(conn: psycopg.Connection, batch: list[dict[str, Any]]) -> int:
    with conn.cursor() as cur:
        with cur.copy(
            "COPY staging_filings (ein, tax_year, form_type, object_id, xml_object_url) FROM STDIN"
        ) as copy:
            for row in batch:
                copy.write_row(
                    (row["ein"], row["tax_year"], row["form_type"], row["object_id"], row["xml_object_url"])
                )
        cur.execute(
            """
            INSERT INTO filings (ein, tax_year, form_type, object_id, xml_object_url)
            SELECT DISTINCT ON (ein, object_id) ein, tax_year, form_type, object_id, xml_object_url
            FROM staging_filings
            ON CONFLICT (ein, object_id) DO UPDATE SET
                tax_year = EXCLUDED.tax_year,
                form_type = EXCLUDED.form_type,
                xml_object_url = EXCLUDED.xml_object_url
            """
        )
        cur.execute("TRUNCATE staging_filings")
    conn.commit()
    return len(batch)


def run_ingest(database_url: str | None = None, years: list[int] | None = None) -> dict[str, Any]:
    """Orchestrates Stage 0 end to end and logs the result to `runs` (§7). Idempotent —
    every upsert is keyed on the tables' natural keys (ein; ein+object_id), so re-running
    updates existing rows instead of duplicating them.
    """
    database_url = database_url or os.environ["DATABASE_URL"]
    years = years or default_target_years()

    started_at = datetime.now(UTC)
    counts: dict[str, int] = {}
    status = "success"
    error: str | None = None
    run_id: int | None = None

    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO runs (client_id, stage, started_at, status) "
                "VALUES (NULL, 'ingest', %s, 'running') RETURNING id",
                (started_at,),
            )
            row = cur.fetchone()
            assert row is not None
            run_id = row[0]
        conn.commit()

    try:
        with psycopg.connect(database_url) as conn:
            org_rows = (r for raw in fetch_bmf_rows() if (r := transform_bmf_row(raw)) is not None)
            counts["organizations"] = upsert_organizations(conn, org_rows)

            known_eins = load_known_eins(conn)
            index_rows = (
                t
                for raw, sub_year in fetch_index_rows(years)
                if (t := transform_index_row(raw, sub_year)) is not None and t["ein"] in known_eins
            )
            counts["filings"] = upsert_filings(conn, latest_two_per_ein(index_rows))
    except Exception as exc:
        status = "failed"
        error = str(exc)
        raise
    finally:
        finished_at = datetime.now(UTC)
        with psycopg.connect(database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE runs SET finished_at = %s, status = %s, counts = %s, error = %s WHERE id = %s",
                    (finished_at, status, Json(counts), error, run_id),
                )
            conn.commit()

    return counts
