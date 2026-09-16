"""Stage 2 — 990 signal extraction (§4, survivors only). Resolves the ZIP-archive
lookup deferred from G1.2 (the IRS's per-filing S3 URLs were deprecated Dec 2021;
filings are now only distributed bundled in monthly ZIP archives per submission year),
parses the latest 2 filings per survivor, fills in filings' financial columns, and
computes the derived signals used by later gates.

990-XML schema variance across years is expected (§10): every extracted field is
nullable, and a filing this stage can't resolve or parse is counted, not fatal.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any
from xml.etree import ElementTree as ET

import httpx
import psycopg
from psycopg.types.json import Json
from remotezip import RemoteZip

logger = logging.getLogger(__name__)

ARCHIVE_URL_TEMPLATE = "https://apps.irs.gov/pub/epostcard/990/xml/{year}/{filename}"
MONTHS = range(1, 13)
SUFFIXES = ("A", "B", "C", "D")

# Confirmed against real filings (see STATUS.md / commit notes) rather than guessed
# from IRS schema docs alone. CY* = current-year values on the Form 990 Part I summary;
# Total* fallbacks cover older/alternate schema versions.
AMOUNT_FIELD_TAGS: dict[str, tuple[str, ...]] = {
    "revenue_total": ("CYTotalRevenueAmt", "TotalRevenueAmt"),
    "contributions": ("CYContributionsGrantsAmt", "TotalContributionsAmt"),
    "program_revenue": ("CYProgramServiceRevenueAmt", "TotalProgramServiceRevenueAmt"),
    "govt_grants": ("GovernmentGrantsAmt",),
}

# Titles that count as "development capacity present" for the dd_present signal (§4
# Stage 2). Kept here as a default rather than in icp_configs since this is a fact
# about Part VII job titles, not a client-tunable ICP criterion.
DEVELOPMENT_TITLE_KEYWORDS = ("development", "fundraising", "advancement")


def _strip_namespaces(elem: ET.Element) -> None:
    for e in elem.iter():
        if "}" in e.tag:
            e.tag = e.tag.split("}", 1)[1]


def _find_amount(scope: ET.Element | None, tags: tuple[str, ...]) -> int | None:
    if scope is None:
        return None
    for tag in tags:
        text = scope.findtext(tag)
        if text is not None:
            try:
                return int(float(text))
            except ValueError:
                continue
    return None


def parse_990_xml(xml_bytes: bytes) -> dict[str, Any]:
    """Best-effort extraction from a single 990 e-file XML document. Never raises on
    missing fields — only on bytes that aren't parseable XML at all.
    """
    empty: dict[str, Any] = {
        "revenue_total": None,
        "contributions": None,
        "program_revenue": None,
        "govt_grants": None,
        "fundraising_expense": None,
        "officers": [],
    }

    root = ET.fromstring(xml_bytes)
    _strip_namespaces(root)

    return_data = root.find("ReturnData")
    if return_data is None:
        return empty

    irs990 = return_data.find("IRS990")
    if irs990 is None:
        return empty

    fundraising_expense = None
    expense_grp = irs990.find("TotalFunctionalExpensesGrp")
    if expense_grp is not None:
        text = expense_grp.findtext("FundraisingAmt")
        if text is not None:
            try:
                fundraising_expense = int(float(text))
            except ValueError:
                pass

    officers: list[dict[str, Any]] = []
    for grp in irs990.findall("Form990PartVIISectionAGrp"):
        name = grp.findtext("PersonNm") or grp.findtext("BusinessNameLine1Txt")
        if not name:
            continue
        officers.append(
            {
                "name": name,
                "title": grp.findtext("TitleTxt"),
                "is_officer": grp.find("OfficerInd") is not None,
            }
        )

    return {
        "revenue_total": _find_amount(irs990, AMOUNT_FIELD_TAGS["revenue_total"]),
        "contributions": _find_amount(irs990, AMOUNT_FIELD_TAGS["contributions"]),
        "program_revenue": _find_amount(irs990, AMOUNT_FIELD_TAGS["program_revenue"]),
        "govt_grants": _find_amount(irs990, AMOUNT_FIELD_TAGS["govt_grants"]),
        "fundraising_expense": fundraising_expense,
        "officers": officers,
    }


def compute_signals(
    current: dict[str, Any],
    previous: dict[str, Any] | None,
    ruling_year: int | None,
    tax_year: int,
) -> dict[str, Any]:
    """Derive the §3/§4 signal set from up to 2 parsed filings for one EIN. Pure
    function — every input value may be None (schema variance, §10) and every output
    value may be None as a result; that's expected, not an error.
    """
    revenue_total = current.get("revenue_total")
    contributions = current.get("contributions")
    program_revenue = current.get("program_revenue")
    govt_grants = current.get("govt_grants")
    fundraising_expense = current.get("fundraising_expense")

    revenue_composition = None
    gov_funding_pct = None
    if revenue_total:
        revenue_composition = {
            "contributions_pct": round(contributions / revenue_total, 4)
            if contributions is not None
            else None,
            "program_pct": round(program_revenue / revenue_total, 4)
            if program_revenue is not None
            else None,
            "govt_pct": round(govt_grants / revenue_total, 4) if govt_grants is not None else None,
        }
        gov_funding_pct = revenue_composition["govt_pct"]

    fundraising_spend_ratio = None
    if fundraising_expense is not None and revenue_total:
        fundraising_spend_ratio = round(fundraising_expense / revenue_total, 4)

    officers = current.get("officers") or []
    dd_present = (
        any(
            keyword in (officer.get("title") or "").lower()
            for officer in officers
            for keyword in DEVELOPMENT_TITLE_KEYWORDS
        )
        if officers
        else None
    )

    org_age = tax_year - ruling_year if ruling_year else None

    revenue_trend = None
    if previous is not None and previous.get("revenue_total") and revenue_total is not None:
        prev_revenue = previous["revenue_total"]
        change = (revenue_total - prev_revenue) / prev_revenue
        if change > 0.10:
            revenue_trend = "growth"
        elif change < -0.10:
            revenue_trend = "decline"
        else:
            revenue_trend = "stable"

    return {
        "gov_funding_pct": gov_funding_pct,
        "revenue_composition": revenue_composition,
        "dd_present": dd_present,
        "fundraising_spend_ratio": fundraising_spend_ratio,
        "org_age": org_age,
        "revenue_trend": revenue_trend,
    }


def _year_from_archive_url(xml_object_url: str) -> int | None:
    match = re.search(r"/(\d{4})/?$", xml_object_url.rstrip("/") + "/")
    return int(match.group(1)) if match else None


def discover_zip_urls(client: httpx.Client, year: int) -> list[str]:
    """Enumerate the monthly ZIP archives that actually exist for a submission year
    (naming: `{year}_TEOS_XML_{MM}{A-D}.zip`) via cheap HEAD requests — no full
    downloads. Confirmed against apps.irs.gov; see pipeline/README.md.
    """
    urls = []
    for month in MONTHS:
        for suffix in SUFFIXES:
            filename = f"{year}_TEOS_XML_{month:02d}{suffix}.zip"
            url = ARCHIVE_URL_TEMPLATE.format(year=year, filename=filename)
            response = client.head(url)
            if response.status_code == 200:
                urls.append(url)
            elif suffix == "A":
                break
    return urls


def build_year_index(client: httpx.Client, year: int) -> dict[str, tuple[str, str]]:
    """Maps object_id -> (zip_url, member_name) for every filing submitted in `year`,
    reading only each archive's central directory (via remotezip range requests) —
    not the archives themselves. Built once per year, shared across all survivors.
    """
    index: dict[str, tuple[str, str]] = {}
    for zip_url in discover_zip_urls(client, year):
        with RemoteZip(zip_url) as archive:
            for member in archive.namelist():
                if not member.endswith("_public.xml"):
                    continue
                object_id = member.rsplit("/", 1)[-1].removesuffix("_public.xml")
                index[object_id] = (zip_url, member)
    return index


def fetch_filing_xml(year_index: dict[str, tuple[str, str]], object_id: str) -> bytes | None:
    entry = year_index.get(object_id)
    if entry is None:
        return None
    zip_url, member = entry
    with RemoteZip(zip_url) as archive:
        data: bytes = archive.read(member)
        return data


def _update_filing(conn: psycopg.Connection, filing_id: int, parsed: dict[str, Any]) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE filings SET
                revenue_total = %(revenue_total)s,
                contributions = %(contributions)s,
                program_revenue = %(program_revenue)s,
                govt_grants = %(govt_grants)s,
                fundraising_expense = %(fundraising_expense)s,
                officers = %(officers)s,
                extracted_at = %(extracted_at)s
            WHERE id = %(id)s
            """,
            {
                **parsed,
                "officers": Json(parsed["officers"]),
                "extracted_at": datetime.now(UTC),
                "id": filing_id,
            },
        )
    conn.commit()


def _get_ruling_year(conn: psycopg.Connection, ein: str) -> int | None:
    with conn.cursor() as cur:
        cur.execute("SELECT ruling_year FROM organizations WHERE ein = %s", (ein,))
        row = cur.fetchone()
    return row[0] if row else None


def _upsert_signal(conn: psycopg.Connection, ein: str, tax_year: int, signal: dict[str, Any]) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO signals (ein, tax_year, signal, computed_at)
            VALUES (%(ein)s, %(tax_year)s, %(signal)s, %(computed_at)s)
            ON CONFLICT (ein, tax_year) DO UPDATE SET
                signal = EXCLUDED.signal,
                computed_at = EXCLUDED.computed_at
            """,
            {
                "ein": ein,
                "tax_year": tax_year,
                "signal": Json(signal),
                "computed_at": datetime.now(UTC),
            },
        )
    conn.commit()


def extract_signals_for_survivors(database_url: str, eins: Iterable[str]) -> dict[str, Any]:
    """Stage 2 orchestrator (§4): resolves + parses each survivor's up-to-2 filings and
    computes derived signals. Commits per filing/signal, not once at the end, so a run
    interrupted partway through loses no completed work on restart.
    """
    eins = list(eins)
    counts: dict[str, Any] = {"filings_parsed": 0, "filings_failed": 0, "signals_computed": 0}
    year_indexes: dict[int, dict[str, tuple[str, str]]] = {}

    with httpx.Client(timeout=60.0) as client, psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, ein, tax_year, object_id, xml_object_url FROM filings "
                "WHERE ein = ANY(%s) ORDER BY ein, tax_year DESC",
                (eins,),
            )
            filing_rows = cur.fetchall()

        parsed_by_ein: dict[str, list[dict[str, Any]]] = {}
        for filing_id, ein, tax_year, object_id, xml_object_url in filing_rows:
            year = _year_from_archive_url(xml_object_url)
            if year is None:
                counts["filings_failed"] += 1
                continue
            if year not in year_indexes:
                year_indexes[year] = build_year_index(client, year)

            try:
                xml_bytes = fetch_filing_xml(year_indexes[year], object_id)
                if xml_bytes is None:
                    counts["filings_failed"] += 1
                    continue
                parsed = parse_990_xml(xml_bytes)
            except Exception:
                # Tolerated per §10 (990 XML variance is expected): unsupported zip
                # compression methods, corrupt archive entries, transient network
                # errors, and malformed XML are all "this one filing didn't work",
                # not "the run is broken" — count it and move on to the next filing.
                logger.warning(
                    "failed to fetch/parse filing ein=%s object_id=%s", ein, object_id, exc_info=True
                )
                counts["filings_failed"] += 1
                continue

            _update_filing(conn, filing_id, parsed)
            counts["filings_parsed"] += 1
            parsed_by_ein.setdefault(ein, []).append({**parsed, "tax_year": tax_year})

        for ein, filings in parsed_by_ein.items():
            filings.sort(key=lambda f: f["tax_year"], reverse=True)
            current, previous = filings[0], filings[1] if len(filings) > 1 else None
            ruling_year = _get_ruling_year(conn, ein)
            signal = compute_signals(current, previous, ruling_year, current["tax_year"])
            _upsert_signal(conn, ein, current["tax_year"], signal)
            counts["signals_computed"] += 1

    total = counts["filings_parsed"] + counts["filings_failed"]
    # §10 risk mitigation: "990 XML variance ... coverage % logged per run" — surfaces
    # tolerated failures (unsupported zip compression, unresolved archives, malformed
    # XML) as a rate to watch, not just a raw count.
    counts["coverage_pct"] = round(counts["filings_parsed"] / total, 4) if total else None

    return counts
