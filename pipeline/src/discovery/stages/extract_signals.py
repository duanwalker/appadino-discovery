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
import os
import re
import zipfile
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import httpx
import psycopg
from psycopg.types.json import Json

from discovery.stages.ingest import default_target_years

logger = logging.getLogger(__name__)

ARCHIVE_URL_TEMPLATE = "https://apps.irs.gov/pub/epostcard/990/xml/{year}/{filename}"
MONTHS = range(1, 13)
SUFFIXES = ("A", "B", "C", "D")

# Container Apps Job mount path for the Azure Files-backed archive cache (see
# infra/modules/storage.bicep); overridable via ARCHIVE_CACHE_DIR for local/dev runs
# where nothing is mounted at that path.
DEFAULT_ARCHIVE_CACHE_DIR = "/mnt/irs-archive-cache"

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

# Common abbreviations for the keywords above that a plain substring match on the
# full words misses (e.g. "VP Dev" — a real near-miss, TEST-COVERAGE-GAPS.md G1.3
# Gap 8). These need word-boundary matching rather than substring search: "dev" as a
# bare substring would also false-positive on unrelated titles like "IT Developer" or
# "Device Manager", which the full-word keywords above are specific enough to avoid.
DEVELOPMENT_TITLE_ABBREVIATIONS = ("dev",)
_DEVELOPMENT_ABBREVIATION_PATTERN = re.compile(
    r"\b(?:" + "|".join(re.escape(a) for a in DEVELOPMENT_TITLE_ABBREVIATIONS) + r")\b"
)


def _title_indicates_development_role(title: str | None) -> bool:
    title_lower = (title or "").lower()
    if any(keyword in title_lower for keyword in DEVELOPMENT_TITLE_KEYWORDS):
        return True
    return bool(_DEVELOPMENT_ABBREVIATION_PATTERN.search(title_lower))

# Confirmed against real filings: WebsiteAddressTxt (Item 5 disclosure) frequently
# holds a placeholder rather than an actual site — filtered out rather than stored.
WEBSITE_PLACEHOLDER_VALUES = frozenset({"NONE", "N/A", "NA", "NONE.", "N.A.", "-"})


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


def _extract_program_text(irs990: ET.Element) -> list[dict[str, Any]]:
    """The numbered Part III program-service accomplishments. Program 1's desc/$
    fields are unwrapped direct children of IRS990 (confirmed against a real filing —
    see tests/stages/fixtures/sample_990.xml); programs 2-4 are each in their own
    ProgSrvcAccomActy{n}Grp wrapper."""
    programs: list[dict[str, Any]] = []
    first_desc = irs990.findtext("Desc")
    if first_desc:
        programs.append(
            {
                "desc": first_desc,
                "expense": _find_amount(irs990, ("ExpenseAmt",)),
                "revenue": _find_amount(irs990, ("RevenueAmt",)),
            }
        )
    for n in (2, 3, 4):
        grp = irs990.find(f"ProgSrvcAccomActy{n}Grp")
        if grp is None:
            continue
        desc = grp.findtext("Desc")
        if not desc:
            continue
        programs.append(
            {
                "desc": desc,
                "expense": _find_amount(grp, ("ExpenseAmt",)),
                "revenue": _find_amount(grp, ("RevenueAmt",)),
            }
        )
    return programs


def _extract_website(irs990: ET.Element) -> str | None:
    text = irs990.findtext("WebsiteAddressTxt")
    if text is None:
        return None
    text = text.strip()
    if not text or text.upper() in WEBSITE_PLACEHOLDER_VALUES:
        return None
    return text


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
        "mission_text": None,
        "program_text": [],
        "website": None,
        "significant_change_ind": None,
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

    significant_change_text = irs990.findtext("SignificantChangeInd")
    significant_change_ind = (
        significant_change_text.strip().lower() == "true" if significant_change_text is not None else None
    )

    return {
        "revenue_total": _find_amount(irs990, AMOUNT_FIELD_TAGS["revenue_total"]),
        "contributions": _find_amount(irs990, AMOUNT_FIELD_TAGS["contributions"]),
        "program_revenue": _find_amount(irs990, AMOUNT_FIELD_TAGS["program_revenue"]),
        "govt_grants": _find_amount(irs990, AMOUNT_FIELD_TAGS["govt_grants"]),
        "fundraising_expense": fundraising_expense,
        "officers": officers,
        "mission_text": irs990.findtext("MissionDesc") or irs990.findtext("ActivityOrMissionDesc"),
        "program_text": _extract_program_text(irs990),
        "website": _extract_website(irs990),
        "significant_change_ind": significant_change_ind,
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
        # Deliberately NOT round(..., 4) like revenue_composition["govt_pct"] above
        # (which exists for display/citation, where a clean 4-decimal number is more
        # useful to Sonnet than one isn't). This raw value is what later feeds
        # compute_soft_flags()'s >= govt_funding_heavy_pct comparison (score.py,
        # Stage 3 — the threshold itself is per-client config, so the comparison
        # can't happen here in the client-agnostic Stage 2 signal). Rounding first
        # can push a genuinely-below-threshold ratio across the line — e.g.
        # 399,999 / 1,000,000 = 0.399999 rounds to 0.4000 at 4 decimals, a real
        # near-miss TEST-COVERAGE-GAPS.md's G1.3 review found. Compare raw, round
        # only for display/storage after the threshold decision is made.
        gov_funding_pct = govt_grants / revenue_total if govt_grants is not None else None

    fundraising_spend_ratio = None
    if fundraising_expense is not None and revenue_total:
        fundraising_spend_ratio = round(fundraising_expense / revenue_total, 4)

    officers = current.get("officers") or []
    dd_present = (
        any(_title_indicates_development_role(officer.get("title")) for officer in officers)
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


def _probe_month(
    client: httpx.Client, year: int, month: int, known_suffixes: frozenset[str] = frozenset()
) -> Iterator[tuple[str, str]]:
    """Yields (suffix, url) for each shard confirmed to exist this call via a HEAD
    request, skipping any suffix already in `known_suffixes` (no HEAD call for it —
    it's already confirmed, either earlier this call or by a prior manifest sync).
    Stops probing the month once suffix "A" comes back non-200 and wasn't already
    known — matches the real IRS archive layout: a month with zero shards published
    yet never has a B/C/D either, so there's no point checking further.
    """
    for suffix in SUFFIXES:
        if suffix in known_suffixes:
            continue
        filename = f"{year}_TEOS_XML_{month:02d}{suffix}.zip"
        url = ARCHIVE_URL_TEMPLATE.format(year=year, filename=filename)
        response = client.head(url)
        if response.status_code == 200:
            yield suffix, url
        elif suffix == "A":
            break


def discover_zip_urls(client: httpx.Client, year: int) -> list[str]:
    """Enumerate the monthly ZIP archives that actually exist for a submission year
    (naming: `{year}_TEOS_XML_{MM}{A-D}.zip`) via cheap HEAD requests — no full
    downloads. Confirmed against apps.irs.gov; see pipeline/README.md.
    """
    return [url for month in MONTHS for _suffix, url in _probe_month(client, year, month)]


def _known_manifest_shards(conn: psycopg.Connection, year: int) -> dict[int, dict[str, dict[str, Any]]]:
    """month -> {suffix: {"url", "local_path", "size_bytes"}} for this year's rows
    already recorded in archive_manifest."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT month, suffix, url, local_path, size_bytes FROM archive_manifest WHERE year = %s",
            (year,),
        )
        rows = cur.fetchall()
    known: dict[int, dict[str, dict[str, Any]]] = {}
    for month, suffix, url, local_path, size_bytes in rows:
        known.setdefault(month, {})[suffix] = {
            "url": url,
            "local_path": local_path,
            "size_bytes": size_bytes,
        }
    return known


def _probe_new_shards(
    client: httpx.Client, year: int, known: dict[int, dict[str, dict[str, Any]]]
) -> list[tuple[int, str, str]]:
    """(month, suffix, url) triples for shards IRS has published that archive_manifest
    doesn't have yet — reuses _probe_month's A->B->C->D logic per month, skipping a
    HEAD call for any suffix the manifest already knows about."""
    new_shards: list[tuple[int, str, str]] = []
    for month in MONTHS:
        known_suffixes = frozenset(known.get(month, {}))
        for suffix, url in _probe_month(client, year, month, known_suffixes):
            new_shards.append((month, suffix, url))
    return new_shards


def _sanity_check_manifest(
    client: httpx.Client, year: int, known: dict[int, dict[str, dict[str, Any]]]
) -> None:
    """Cheap sanity check for shards already in the manifest (never re-downloaded):
    a fresh HEAD's Content-Length should still match what's on disk. A mismatch would
    mean the IRS silently replaced a "confirmed immutable" archive out from under us
    — log it as a warning to investigate, don't crash the run over it.
    """
    for month, suffixes in known.items():
        for suffix, entry in suffixes.items():
            filename = f"{year}_TEOS_XML_{month:02d}{suffix}.zip"
            url = ARCHIVE_URL_TEMPLATE.format(year=year, filename=filename)
            response = client.head(url)
            if response.status_code != 200:
                continue
            remote_length = response.headers.get("content-length")
            if remote_length is None:
                continue
            if int(remote_length) != entry["size_bytes"]:
                logger.warning(
                    "ARCHIVE_SIZE_MISMATCH year=%s month=%s suffix=%s manifest_size=%s remote_size=%s url=%s",
                    year,
                    month,
                    suffix,
                    entry["size_bytes"],
                    remote_length,
                    url,
                )


def _download_archive_to(client: httpx.Client, url: str, local_path: Path) -> int:
    """Streams `url` to `local_path` (via a .part temp file, renamed on success so a
    crash mid-download never leaves a file that looks complete) and returns its size
    in bytes."""
    local_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = local_path.with_name(local_path.name + ".part")
    with client.stream("GET", url) as response:
        response.raise_for_status()
        with open(tmp_path, "wb") as f:
            f.writelines(response.iter_bytes())
    tmp_path.replace(local_path)
    return local_path.stat().st_size


def _insert_manifest_row(
    conn: psycopg.Connection, year: int, month: int, suffix: str, url: str, local_path: str, size_bytes: int
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO archive_manifest (year, month, suffix, url, local_path, size_bytes, downloaded_at)
            VALUES (%(year)s, %(month)s, %(suffix)s, %(url)s, %(local_path)s, %(size_bytes)s, %(downloaded_at)s)
            ON CONFLICT (year, month, suffix) DO NOTHING
            """,
            {
                "year": year,
                "month": month,
                "suffix": suffix,
                "url": url,
                "local_path": local_path,
                "size_bytes": size_bytes,
                "downloaded_at": datetime.now(UTC),
            },
        )
    conn.commit()


def sync_archive_manifest(
    client: httpx.Client, conn: psycopg.Connection, years: Iterable[int], cache_dir: str | Path
) -> None:
    """Ensures every shard IRS has published for `years` is downloaded to `cache_dir`
    and recorded in archive_manifest. Existing rows are immutable — never
    re-downloaded, only sanity-checked (see _sanity_check_manifest). Run at the start
    of every extract_signals_for_survivors() call, across every year currently in
    default_target_years() — not just the years this run's survivors happen to need —
    so the manifest stays current with what IRS has published even for a year with no
    survivor filings yet this run.
    """
    cache_dir = Path(cache_dir)
    for year in years:
        known = _known_manifest_shards(conn, year)
        _sanity_check_manifest(client, year, known)
        for month, suffix, url in _probe_new_shards(client, year, known):
            filename = f"{year}_TEOS_XML_{month:02d}{suffix}.zip"
            local_path = cache_dir / str(year) / filename
            size_bytes = _download_archive_to(client, url, local_path)
            _insert_manifest_row(conn, year, month, suffix, url, str(local_path), size_bytes)


def build_year_index(conn: psycopg.Connection, year: int) -> dict[str, tuple[str, str]]:
    """Maps object_id -> (local_path, member_name) for every filing submitted in
    `year`, reading each archive's central directory from the local cache (no network
    calls) — sync_archive_manifest() must have already downloaded every shard for
    this year and recorded it in archive_manifest before this runs. Built once per
    year, shared across all survivors.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT local_path FROM archive_manifest WHERE year = %s", (year,))
        local_paths = [row[0] for row in cur.fetchall()]

    index: dict[str, tuple[str, str]] = {}
    for local_path in local_paths:
        with zipfile.ZipFile(local_path) as archive:
            for member in archive.namelist():
                if not member.endswith("_public.xml"):
                    continue
                object_id = member.rsplit("/", 1)[-1].removesuffix("_public.xml")
                index[object_id] = (local_path, member)
    return index


def fetch_filing_xml(year_index: dict[str, tuple[str, str]], object_id: str) -> bytes | None:
    entry = year_index.get(object_id)
    if entry is None:
        return None
    local_path, member = entry
    with zipfile.ZipFile(local_path) as archive:
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
                mission_text = %(mission_text)s,
                program_text = %(program_text)s,
                significant_change_ind = %(significant_change_ind)s,
                extracted_at = %(extracted_at)s
            WHERE id = %(id)s
            """,
            {
                **parsed,
                "officers": Json(parsed["officers"]),
                "program_text": Json(parsed["program_text"]),
                "extracted_at": datetime.now(UTC),
                "id": filing_id,
            },
        )
    conn.commit()


def _update_organization_website(conn: psycopg.Connection, ein: str, website: str | None) -> None:
    if website is None:
        return
    with conn.cursor() as cur:
        cur.execute("UPDATE organizations SET website = %s WHERE ein = %s", (website, ein))
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


def extract_signals_for_survivors(
    database_url: str, eins: Iterable[str], cache_dir: str | None = None
) -> dict[str, Any]:
    """Stage 2 orchestrator (§4): resolves + parses each survivor's up-to-2 filings and
    computes derived signals. Commits per filing/signal, not once at the end, so a run
    interrupted partway through loses no completed work on restart.
    """
    eins = list(eins)
    counts: dict[str, Any] = {"filings_parsed": 0, "filings_failed": 0, "signals_computed": 0}
    year_indexes: dict[int, dict[str, tuple[str, str]]] = {}
    cache_dir = cache_dir or os.environ.get("ARCHIVE_CACHE_DIR", DEFAULT_ARCHIVE_CACHE_DIR)

    with httpx.Client(timeout=60.0) as client, psycopg.connect(database_url) as conn:
        sync_archive_manifest(client, conn, default_target_years(), cache_dir)

        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, ein, tax_year, object_id, xml_object_url FROM filings "
                "WHERE ein = ANY(%s) ORDER BY ein, tax_year DESC",
                (eins,),
            )
            filing_rows = cur.fetchall()

        parsed_by_ein: dict[str, list[dict[str, Any]]] = {}
        website_backfilled: set[str] = set()
        for filing_id, ein, tax_year, object_id, xml_object_url in filing_rows:
            year = _year_from_archive_url(xml_object_url)
            if year is None:
                counts["filings_failed"] += 1
                continue
            if year not in year_indexes:
                year_indexes[year] = build_year_index(conn, year)

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

            # Rows arrive ordered ein, tax_year DESC — the first hit per ein is
            # already the most recent filing, so only that one backfills the website.
            if ein not in website_backfilled:
                _update_organization_website(conn, ein, parsed["website"])
                website_backfilled.add(ein)

        for ein, filings in parsed_by_ein.items():
            filings.sort(key=lambda f: f["tax_year"], reverse=True)
            current, previous = filings[0], filings[1] if len(filings) > 1 else None
            ruling_year = _get_ruling_year(conn, ein)
            signal = compute_signals(current, previous, ruling_year, current["tax_year"])
            _upsert_signal(conn, ein, current["tax_year"], signal)
            counts["signals_computed"] += 1

    # §10 risk mitigation: "990 XML variance ... coverage % logged per run" — surfaces
    # tolerated failures (unsupported zip compression, unresolved archives, malformed
    # XML) as a rate to watch, not just a raw count. Denominator is Stage 1's survivor
    # count (`eins`), not filings_parsed + filings_failed (TEST-COVERAGE-GAPS.md G1.3
    # Gap 11): a survivor with zero filing rows at all never appears in filing_rows,
    # so a filing-based denominator silently excludes it from both sides of the ratio
    # and can read as complete coverage even when real survivors were never parsed.
    # Numerator is signals_computed, the actual survivor-level success signal — an
    # EIN whose first filing fails but whose second filing parses still gets a
    # computed signal, so this is stricter and more meaningful than filings_parsed.
    counts["coverage_pct"] = round(counts["signals_computed"] / len(eins), 4) if eins else None

    return counts
