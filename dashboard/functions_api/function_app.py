"""G2.x Dashboard — Functions API (§2, §5). Thin HTTP layer over Postgres,
reusing the pipeline's own `discovery.models`/`discovery.db` rather than
redeclaring the schema (see README.md for why, and the deploy-time caveat).

Write surface is deliberately narrow: only what a human reviewer changes
(prospects.status, prospects.notes, suppression add/remove, clearing
prospects.suppression_flag on confirm/dismiss). Everything the pipeline
writes (scores, gap_rank, trigger_*) is read-only here.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import azure.functions as func
import psycopg
from discovery.clients.enrichment_provider import EnrichmentResult
from discovery.clients.fullenrich_adapter import FullEnrichProvider, domain_from_website
from discovery.db import get_database_url
from discovery.stages.enrich import (
    EnrichAttempt,
    build_candidate,
    can_enrich,
    check_enrichment,
    compute_retention_expires_at,
    select_primary_officer,
    start_enrichment,
)
from discovery.stages.qa import _normalize_for_match, _program_text_corpus
from discovery.stages.score import ALIGNMENT_CRITERIA
from psycopg.rows import dict_row
from psycopg.types.json import Json

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)
logger = logging.getLogger(__name__)

# Mirrors migration 4a2e9c1f7b3d's ck_prospects_status — contacted/responded
# belong to the Phase 2 Outreach queue and are out of scope for G2.x.
VALID_STATUSES = {"new", "reviewed", "approved", "rejected"}
VALID_SUPPRESSION_KINDS = {"client", "active_prospect", "partner_attribution"}

# E2's Enrich button is a single-contact call, not the ~19-item batch E1 measured —
# E1 never logged real submit-to-completion latency for either shape (see STATUS.md),
# so rather than assume a number, the route bounds its own wait and hands off to a
# resumable status poll instead of blocking further. Worst case ~12s per attempt
# loop, comfortably inside any Azure Functions HTTP timeout tier.
ENRICH_POLL_ATTEMPTS = 4
ENRICH_POLL_INTERVAL_S = 3.0

# E2 fix (Copilot's independent review, test_e2_independent.py): a fast, per-instance
# pre-check so a same-instance double-click never even reaches the DB. This is a
# UX/cost optimization only — it is NOT what prevents a duplicate submission across
# Azure Functions instances (a fresh instance starts with an empty dict). The actual
# cross-instance guarantee is the `enrichment_jobs` UNIQUE(client_id, ein) constraint
# (migration d3f9a1c6e8b2) via `_claim_pending_slot` below; this cache is just a
# same-process shortcut that avoids a round trip when it can.
_PENDING_JOBS: dict[tuple[int, str], str | None] = {}

# Total alignment criteria in the active framework (currently 6 — criterion 2 is
# excluded pending confirmation, see score.py), used as the "n/6" denominator.
ALIGNMENT_CRITERIA_TOTAL = len(ALIGNMENT_CRITERIA)

LEADERSHIP_NEEDS_CONFIRMATION = "needs human confirmation"

CSV_BOM = "﻿"
# Leading characters a spreadsheet treats as the start of a formula (OWASP CSV injection).
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
# Digits, +, spaces, parentheses, dashes, dots only, with at least one digit.
PHONE_NUMBER_PATTERN = re.compile(r"[0-9+ ().\-]*[0-9][0-9+ ().\-]*")

# Customer-facing labels for triggers.TRIGGER_PRIORITY codes (CSV only; the
# dashboard and the stored prospects.assigned_trigger keep the codes).
TRIGGER_LABELS: dict[str, str] = {
    "new_ed": "New executive director",
    "dd_departure": "Development director departed",
    "transformational_revenue_jump": "Major revenue increase",
    "first_filing_above_floor": "First filing above revenue floor",
}

# (internal key, customer-facing header), in export order. Internal keys are what
# _prospect_row_to_csv_dict returns; only the header row uses the plain-English names.
CSV_COLUMNS: tuple[tuple[str, str], ...] = (
    ("ein", "EIN"),
    ("name", "Organization"),
    ("city", "City"),
    ("state", "State"),
    ("revenue", "Revenue (most recent 990)"),
    ("gap_rank", "Gap rank"),
    ("alignment", f"Alignment (n/{ALIGNMENT_CRITERIA_TOTAL})"),
    ("qualifies", "Qualifies"),
    ("assigned_trigger", "Trigger"),
    ("trigger_angle", "Trigger angle"),
    ("values_population_served", "Values: Population served"),
    ("values_leadership", "Values: Leadership"),
    ("dd_present", "Development director present"),
    ("fundraising_spend_ratio", "Fundraising spend ratio"),
    ("website", "Website"),
    ("suppression_flag", "Suppression flag"),
    ("contact_name", "Contact name"),
    ("contact_title", "Contact title"),
    ("contact_email", "Contact email"),
    ("contact_phone", "Contact phone"),
    ("contact_status", "Contact status"),
)
CSV_FIELDNAMES = [key for key, _ in CSV_COLUMNS]
CSV_HEADERS = [header for _, header in CSV_COLUMNS]

# Shared by the dashboard list (PROSPECT_QUERY) and the CSV export (EXPORT_QUERY).
# {extra_select}/{extra_join} are empty for the list, so it never loads 990 text.
_PROSPECT_QUERY_TEMPLATE = """
    SELECT
        p.id, p.ein, p.client_id, p.status, p.assigned_trigger, p.trigger_angle,
        p.trigger_evidence, p.gap_rank, p.suppression_flag, p.notes, p.updated_by, p.updated_at,
        o.name AS org_name, o.city, o.state, o.revenue_latest, o.website,
        s.values_signals, s.alignment, s.capacity, s.soft_flags, s.disqualified, s.dq_reason,
        f.officers, ic.config AS icp_config,{extra_select}
        e.id AS enr_id, e.contact_name AS enr_contact_name, e.contact_title AS enr_contact_title,
        e.email AS enr_email, e.email_status AS enr_email_status,
        e.stale_detail AS enr_stale_detail, e.phone AS enr_phone,
        e.provider_confidence AS enr_provider_confidence,
        e.credits_spent AS enr_credits_spent, e.retention_expires_at AS enr_retention_expires_at
    FROM prospects p
    JOIN organizations o ON o.ein = p.ein
    LEFT JOIN LATERAL (
        SELECT values_signals, alignment, capacity, soft_flags, disqualified, dq_reason
        FROM scores
        WHERE scores.client_id = p.client_id AND scores.ein = p.ein AND scores.stage = 'sonnet'
        ORDER BY icp_version DESC
        LIMIT 1
    ) s ON true
    LEFT JOIN LATERAL (
        SELECT officers FROM filings f2
        WHERE f2.ein = p.ein AND f2.officers IS NOT NULL
        ORDER BY tax_year DESC LIMIT 1
    ) f ON true{extra_join}
    LEFT JOIN LATERAL (
        SELECT config FROM icp_configs ic2
        WHERE ic2.client_id = p.client_id AND ic2.active = true
        ORDER BY version DESC LIMIT 1
    ) ic ON true
    LEFT JOIN LATERAL (
        SELECT id, contact_name, contact_title, email, email_status, stale_detail, phone,
               provider_confidence, credits_spent, retention_expires_at
        FROM enrichments en
        WHERE en.client_id = p.client_id AND en.ein = p.ein
        ORDER BY requested_at DESC LIMIT 1
    ) e ON true
    WHERE p.client_id = %(client_id)s
    ORDER BY p.gap_rank DESC NULLS LAST
"""

PROSPECT_QUERY = _PROSPECT_QUERY_TEMPLATE.format(extra_select="", extra_join="")

# Export-only: revenue plus the 990 text the quoted-values columns are re-verified
# against (see _grounded_citation). Same "current filing" publish.load_filing_pair
# (and so trigger detection) uses.
EXPORT_QUERY = _PROSPECT_QUERY_TEMPLATE.format(
    extra_select="""
        rf.revenue_total AS filing_revenue_total,
        rf.mission_text AS filing_mission_text, rf.program_text AS filing_program_text,""",
    extra_join="""
    LEFT JOIN LATERAL (
        SELECT revenue_total, mission_text, program_text FROM filings f3
        WHERE f3.ein = p.ein AND f3.extracted_at IS NOT NULL
        ORDER BY tax_year DESC LIMIT 1
    ) rf ON true""",
)

# Lighter-weight fetch for the enrich routes — only what's needed to run the guard
# (hard rule 8, §9) and build a candidate, not the full review-table payload.
ENRICH_CANDIDATE_QUERY = """
    SELECT p.id, p.ein, p.client_id, p.status, o.website, f.officers, ic.config AS icp_config
    FROM prospects p
    JOIN organizations o ON o.ein = p.ein
    LEFT JOIN LATERAL (
        SELECT officers FROM filings f2
        WHERE f2.ein = p.ein AND f2.officers IS NOT NULL
        ORDER BY tax_year DESC LIMIT 1
    ) f ON true
    LEFT JOIN LATERAL (
        SELECT config FROM icp_configs ic2
        WHERE ic2.client_id = p.client_id AND ic2.active = true
        ORDER BY version DESC LIMIT 1
    ) ic ON true
    WHERE p.id = %(id)s
"""


def _conn() -> psycopg.Connection[Any]:
    return psycopg.connect(get_database_url(), row_factory=dict_row)


def _json_default(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    return str(obj)


def _json_response(data: Any, status_code: int = 200) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps(data, default=_json_default), status_code=status_code, mimetype="application/json"
    )


def _error(message: str, status_code: int = 400) -> func.HttpResponse:
    return _json_response({"error": message}, status_code)


def _tenant_enrichment_config(icp_config: dict[str, Any] | None) -> tuple[bool, str | None]:
    config = icp_config or {}
    return bool(config.get("enrichment_enabled", False)), config.get("fullenrich_subaccount_id")


def _build_contact(row: dict[str, Any]) -> dict[str, Any]:
    """Name/Title (§5 v2.3): always present, free, sourced from filings.officers —
    not gated by enrichment_enabled, and always the *current* latest-filing officer
    (never overwritten by an enrichment result — see the brief's own open question in
    §5 v2.3 about whether it should). Email/Phone/Status: only populated once
    row['enr_id'] is set, i.e. a human has actually clicked Enrich for this prospect
    at least once; enr_id present with a null email means a real "not matched" result,
    not "never tried" — the dashboard/CSV need to tell those apart (see
    _prospect_row_to_csv_dict).

    Because Name/Title always tracks the current officer while the enrichment result
    stays pinned to whoever was actually submitted, the two can legitimately diverge
    when leadership changes between an enrichment and a later filing update (real
    case found against live data: Day One's enrichment is for a departed CEO, Gregory
    Bowers, while Name/Title now correctly shows his successor, Cassandra Humphrey).
    `enriched_contact_name`/`enriched_contact_title` expose who the email/phone
    actually belong to, so the dashboard can flag this rather than silently implying
    the enriched contact info belongs to whoever Name/Title currently shows.
    """
    enrichment_enabled, subaccount_id = _tenant_enrichment_config(row["icp_config"])
    officer = select_primary_officer(row["officers"])
    allowed, cannot_enrich_reason = can_enrich(row["status"], enrichment_enabled, subaccount_id)
    retention_expires_at = row["enr_retention_expires_at"]
    retention_expired = bool(retention_expires_at is not None and retention_expires_at < datetime.now(timezone.utc))
    current_name = officer.get("name") if officer else None
    enriched_contact_name = row["enr_contact_name"]
    contact_mismatch = bool(
        row["enr_id"] is not None
        and enriched_contact_name
        and current_name
        and enriched_contact_name.strip().casefold() != current_name.strip().casefold()
    )
    return {
        "name": current_name,
        "title": officer.get("title") if officer else None,
        "enriched": row["enr_id"] is not None,
        "email": row["enr_email"],
        "email_status": row["enr_email_status"],
        "stale_detail": row["enr_stale_detail"],
        "phone": row["enr_phone"],
        "provider_confidence": row["enr_provider_confidence"],
        "credits_spent": row["enr_credits_spent"],
        "retention_expires_at": retention_expires_at,
        "retention_expired": retention_expired,
        "enrichment_enabled": enrichment_enabled,
        "can_enrich": allowed,
        "cannot_enrich_reason": cannot_enrich_reason,
        "enriched_contact_name": enriched_contact_name,
        "enriched_contact_title": row["enr_contact_title"],
        "contact_mismatch": contact_mismatch,
    }


def _row_to_prospect(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "ein": row["ein"],
        "client_id": row["client_id"],
        "status": row["status"],
        "assigned_trigger": row["assigned_trigger"],
        "trigger_angle": row["trigger_angle"],
        "trigger_evidence": row["trigger_evidence"],
        "gap_rank": float(row["gap_rank"]) if row["gap_rank"] is not None else None,
        "suppression_flag": row["suppression_flag"],
        "notes": row["notes"],
        "updated_by": row["updated_by"],
        "updated_at": row["updated_at"],
        "org": {
            "name": row["org_name"],
            "city": row["city"],
            "state": row["state"],
            "revenue_latest": float(row["revenue_latest"]) if row["revenue_latest"] is not None else None,
        },
        "score": {
            "values_signals": row["values_signals"],
            "alignment": row["alignment"],
            "capacity": row["capacity"],
            "soft_flags": row["soft_flags"],
            "disqualified": row["disqualified"],
            "dq_reason": row["dq_reason"],
        },
        "contact": _build_contact(row),
    }


@app.route(route="prospects", methods=["GET"])
def list_prospects(req: func.HttpRequest) -> func.HttpResponse:
    client_id = req.params.get("client_id")
    if not client_id:
        return _error("client_id is required")
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(PROSPECT_QUERY, {"client_id": client_id})
        rows = cur.fetchall()
    return _json_response([_row_to_prospect(r) for r in rows])


@app.route(route="prospects/{id}", methods=["PATCH"])
def update_prospect(req: func.HttpRequest) -> func.HttpResponse:
    prospect_id = req.route_params.get("id")
    try:
        body = req.get_json()
    except ValueError:
        return _error("request body must be JSON")

    updated_by = body.get("updated_by")
    if not updated_by or not str(updated_by).strip():
        return _error("updated_by is required")

    status = body.get("status")
    if status is not None and status not in VALID_STATUSES:
        return _error(f"status must be one of {sorted(VALID_STATUSES)}")

    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE prospects
            SET status = COALESCE(%(status)s, status),
                notes = CASE WHEN %(notes_provided)s THEN %(notes)s ELSE notes END,
                updated_by = %(updated_by)s,
                updated_at = now()
            WHERE id = %(id)s
            RETURNING id
            """,
            {
                "status": status,
                "notes": body.get("notes"),
                "notes_provided": "notes" in body,
                "updated_by": updated_by,
                "id": prospect_id,
            },
        )
        row = cur.fetchone()
        conn.commit()

    if row is None:
        return _error("prospect not found", 404)
    return _json_response({"id": prospect_id, "status": "updated"})


def _get_fullenrich_provider(subaccount_id: str | None) -> FullEnrichProvider | None:
    api_key = os.environ.get("FULLENRICH_API_KEY")
    if not api_key:
        return None
    return FullEnrichProvider(api_key=api_key, subaccount_id=subaccount_id)


def _enrichment_result_to_dict(result: EnrichmentResult) -> dict[str, Any]:
    return {
        "contact_name": result.contact_name,
        "contact_title": result.contact_title,
        "email": result.email,
        "email_status": result.email_status,
        "stale_detail": result.stale_detail,
        "phone": result.phone,
        "provider_confidence": result.provider_confidence,
        "credits_spent": result.credits_spent,
    }


def _persist_enrichment(
    conn: psycopg.Connection[Any],
    client_id: int,
    ein: str,
    prospect_id: int,
    job_id: str,
    result: EnrichmentResult,
) -> int:
    """Idempotent on job_id: a status-poll retry (or a double-click racing an
    in-flight submit) must never write a second row / double-count credits for the
    same FullEnrich job, even though polling itself is free."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM enrichments WHERE client_id = %(client_id)s AND ein = %(ein)s "
            "AND raw->>'_job_id' = %(job_id)s",
            {"client_id": client_id, "ein": ein, "job_id": job_id},
        )
        existing = cur.fetchone()
        if existing is not None:
            return int(existing["id"])

        # requested_at/completed_at both stamped "now": a submit-then-poll can span
        # the initial POST and a later status-poll GET, so there's no single accurate
        # request timestamp to carry across requests — this records when the result
        # was first confirmed, which is what the retention window (§5.5) keys off.
        now = datetime.now(timezone.utc)
        raw = dict(result.raw)
        raw["_job_id"] = job_id
        cur.execute(
            """
            INSERT INTO enrichments (
                client_id, ein, prospect_id, provider, contact_name, contact_title,
                email, email_status, stale_detail, phone, linkedin_url,
                provider_confidence, raw, credits_spent, requested_at, completed_at, retention_expires_at
            ) VALUES (
                %(client_id)s, %(ein)s, %(prospect_id)s, 'fullenrich', %(contact_name)s, %(contact_title)s,
                %(email)s, %(email_status)s, %(stale_detail)s, %(phone)s, %(linkedin_url)s,
                %(provider_confidence)s, %(raw)s, %(credits_spent)s, %(now)s, %(now)s, %(retention_expires_at)s
            )
            RETURNING id
            """,
            {
                "client_id": client_id,
                "ein": ein,
                "prospect_id": prospect_id,
                "contact_name": result.contact_name,
                "contact_title": result.contact_title,
                "email": result.email,
                "email_status": result.email_status,
                "stale_detail": result.stale_detail,
                "phone": result.phone,
                "linkedin_url": result.linkedin_url,
                "provider_confidence": result.provider_confidence,
                "raw": Json(raw),
                "credits_spent": result.credits_spent,
                "now": now,
                "retention_expires_at": compute_retention_expires_at(now),
            },
        )
        new_row = cur.fetchone()
        conn.commit()
    assert new_row is not None  # RETURNING id always yields a row on successful INSERT
    return int(new_row["id"])


def _claim_pending_slot(conn: psycopg.Connection[Any], client_id: int, ein: str) -> tuple[bool, str | None]:
    """The real cross-instance double-submit guard (E2 fix, Copilot's independent
    review). Atomic at the database layer: `enrichment_jobs` has UNIQUE(client_id,
    ein) (migration d3f9a1c6e8b2), so a concurrent second INSERT ... ON CONFLICT DO
    NOTHING can never win against an existing row, regardless of which Functions
    instance handles it — this is what an in-process-only lock can't provide.

    Returns (claimed, existing_job_id). claimed=True means this call now owns
    submitting a new job and must call _set_claimed_job_id/_clear_pending_slot
    itself. claimed=False means another request already has one in flight;
    existing_job_id is whatever's on record for it (can be None in the narrow
    window where that other request has claimed the slot but not yet heard back
    from the provider with a real job id)."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO enrichment_jobs (client_id, ein, created_at) "
            "VALUES (%(client_id)s, %(ein)s, now()) "
            "ON CONFLICT (client_id, ein) DO NOTHING RETURNING id",
            {"client_id": client_id, "ein": ein},
        )
        claimed_row = cur.fetchone()
        conn.commit()
        if claimed_row is not None:
            return True, None

        cur.execute(
            "SELECT job_id FROM enrichment_jobs WHERE client_id = %(client_id)s AND ein = %(ein)s",
            {"client_id": client_id, "ein": ein},
        )
        existing = cur.fetchone()
        return False, (existing["job_id"] if existing else None)


def _set_claimed_job_id(conn: psycopg.Connection[Any], client_id: int, ein: str, job_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE enrichment_jobs SET job_id = %(job_id)s WHERE client_id = %(client_id)s AND ein = %(ein)s",
            {"job_id": job_id, "client_id": client_id, "ein": ein},
        )
        conn.commit()


def _clear_pending_slot(conn: psycopg.Connection[Any], client_id: int, ein: str) -> None:
    """Releases the claim once a job resolves, so a future enrichment attempt for
    this org is never blocked by a completed (or abandoned — see the migration's
    docstring for that known, out-of-scope gap) job."""
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM enrichment_jobs WHERE client_id = %(client_id)s AND ein = %(ein)s",
            {"client_id": client_id, "ein": ein},
        )
        conn.commit()
    _PENDING_JOBS.pop((client_id, ein), None)


def _respond_to_enrich_attempt(
    conn: psycopg.Connection[Any], client_id: int, ein: str, prospect_id: int, attempt: EnrichAttempt
) -> func.HttpResponse:
    if attempt.status == "blocked":
        return _error(attempt.reason or "enrichment blocked", 403)
    if attempt.status == "no_candidate":
        return _error(attempt.reason or "no usable contact on file", 422)
    if attempt.status == "pending":
        return _json_response({"status": "pending", "job_id": attempt.job_id}, 202)
    assert attempt.job_id is not None and attempt.result is not None  # guaranteed by "done" (discovery.stages.enrich)
    enrichment_id = _persist_enrichment(conn, client_id, ein, prospect_id, attempt.job_id, attempt.result)
    _clear_pending_slot(conn, client_id, ein)
    return _json_response(
        {"status": "done", "enrichment_id": enrichment_id, "contact": _enrichment_result_to_dict(attempt.result)}
    )


@app.route(route="prospects/{id}/enrich", methods=["POST"])
def enrich_prospect(req: func.HttpRequest) -> func.HttpResponse:
    """§5.5/E2: enriches the one prospect's primary 990 officer via FullEnrich.
    Never fires outside hard rule 8's three conditions (§9) — checked here before
    any provider is even constructed, so a blocked call never reaches FullEnrich.

    Double-submit guard (E2 fix): checked only after a candidate is confirmed
    buildable, so a request that would 422 anyway never claims a slot it can't
    use. `_PENDING_JOBS` is a same-instance fast path; `_claim_pending_slot` is
    the real cross-instance guarantee — see both docstrings."""
    prospect_id = req.route_params.get("id")
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(ENRICH_CANDIDATE_QUERY, {"id": prospect_id})
        row = cur.fetchone()
        if row is None:
            return _error("prospect not found", 404)

        enrichment_enabled, subaccount_id = _tenant_enrichment_config(row["icp_config"])
        allowed, reason = can_enrich(row["status"], enrichment_enabled, subaccount_id)
        if not allowed:
            return _error(reason or "enrichment blocked", 403)

        provider = _get_fullenrich_provider(subaccount_id)
        if provider is None:
            return _error("FullEnrich API key not configured", 500)

        officer = select_primary_officer(row["officers"])
        org_domain = domain_from_website(row["website"])
        candidate = build_candidate(row["ein"], org_domain, officer)
        if candidate is None:
            return _error("no usable officer name or org domain on file", 422)

        client_id, ein = row["client_id"], row["ein"]
        cache_key = (client_id, ein)

        if cache_key in _PENDING_JOBS:
            cached_job_id = _PENDING_JOBS[cache_key]
            body: dict[str, Any] = {"status": "pending"}
            if cached_job_id is not None:
                body["job_id"] = cached_job_id
            return _json_response(body, 202)

        claimed, existing_job_id = _claim_pending_slot(conn, client_id, ein)
        if not claimed:
            _PENDING_JOBS[cache_key] = existing_job_id
            body = {"status": "pending"}
            if existing_job_id is not None:
                body["job_id"] = existing_job_id
            return _json_response(body, 202)

        _PENDING_JOBS[cache_key] = None
        try:
            attempt = start_enrichment(
                provider,
                ein,
                org_domain,
                officer,
                status=row["status"],
                enrichment_enabled=enrichment_enabled,
                fullenrich_subaccount_id=subaccount_id,
                poll_attempts=ENRICH_POLL_ATTEMPTS,
                poll_interval_s=ENRICH_POLL_INTERVAL_S,
            )
            if attempt.job_id is not None:
                _set_claimed_job_id(conn, client_id, ein, attempt.job_id)
                _PENDING_JOBS[cache_key] = attempt.job_id
            return _respond_to_enrich_attempt(conn, client_id, ein, row["id"], attempt)
        except Exception:
            # Never leave a claimed slot stuck on an unexpected error — a raised
            # exception here means no job is actually in flight, so the claim must
            # release rather than permanently block future attempts for this org.
            _clear_pending_slot(conn, client_id, ein)
            raise


@app.route(route="prospects/{id}/enrich-status", methods=["GET"])
def enrich_status(req: func.HttpRequest) -> func.HttpResponse:
    """Resume path for a job still pending after enrich_prospect's own bounded
    wait — the dashboard's Enrich button polls this (spinner visible) until it
    resolves, rather than the initial request blocking indefinitely."""
    prospect_id = req.route_params.get("id")
    job_id = req.params.get("job_id")
    if not job_id:
        return _error("job_id is required")

    with _conn() as conn, conn.cursor() as cur:
        cur.execute(ENRICH_CANDIDATE_QUERY, {"id": prospect_id})
        row = cur.fetchone()
        if row is None:
            return _error("prospect not found", 404)

        enrichment_enabled, subaccount_id = _tenant_enrichment_config(row["icp_config"])
        # Defense in depth: re-check the guard on the resume path too, in case
        # status/config changed between the initial POST and this poll.
        allowed, reason = can_enrich(row["status"], enrichment_enabled, subaccount_id)
        if not allowed:
            return _error(reason or "enrichment blocked", 403)

        provider = _get_fullenrich_provider(subaccount_id)
        if provider is None:
            return _error("FullEnrich API key not configured", 500)

        org_domain = domain_from_website(row["website"])
        attempt = check_enrichment(provider, job_id, org_domain, poll_attempts=1, poll_interval_s=0)
        return _respond_to_enrich_attempt(conn, row["client_id"], row["ein"], row["id"], attempt)


@app.route(route="prospects/{id}/suppression-review", methods=["POST"])
def suppression_review(req: func.HttpRequest) -> func.HttpResponse:
    prospect_id = req.route_params.get("id")
    try:
        body = req.get_json()
    except ValueError:
        return _error("request body must be JSON")

    action = body.get("action")
    updated_by = body.get("updated_by")
    if action not in {"confirm", "dismiss"}:
        return _error("action must be 'confirm' or 'dismiss'")
    if not updated_by or not str(updated_by).strip():
        return _error("updated_by is required")

    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT p.client_id, p.ein, o.name AS org_name FROM prospects p "
            "JOIN organizations o ON o.ein = p.ein WHERE p.id = %s",
            (prospect_id,),
        )
        prospect = cur.fetchone()
        if prospect is None:
            return _error("prospect not found", 404)

        if action == "confirm":
            cur.execute(
                """
                INSERT INTO suppression (client_id, ein, org_name, kind, source, added_at)
                VALUES (%(client_id)s, %(ein)s, %(org_name)s, 'client', %(source)s, now())
                """,
                {
                    "client_id": prospect["client_id"],
                    "ein": prospect["ein"],
                    "org_name": prospect["org_name"],
                    "source": f"dashboard confirm by {updated_by}",
                },
            )

        cur.execute(
            "UPDATE prospects SET suppression_flag = NULL, updated_by = %s, updated_at = now() WHERE id = %s",
            (updated_by, prospect_id),
        )
        conn.commit()

    return _json_response({"id": prospect_id, "action": action})


@app.route(route="suppression", methods=["GET"])
def list_suppression(req: func.HttpRequest) -> func.HttpResponse:
    client_id = req.params.get("client_id")
    if not client_id:
        return _error("client_id is required")
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, client_id, ein, org_name, kind, source, added_at, partner_window_expires_at "
            "FROM suppression WHERE client_id = %s ORDER BY added_at DESC",
            (client_id,),
        )
        rows = cur.fetchall()
    return _json_response(rows)


@app.route(route="suppression", methods=["POST"])
def create_suppression(req: func.HttpRequest) -> func.HttpResponse:
    try:
        body = req.get_json()
    except ValueError:
        return _error("request body must be JSON")

    client_id = body.get("client_id")
    org_name = body.get("org_name")
    kind = body.get("kind")
    if not client_id or not org_name or not kind:
        return _error("client_id, org_name, and kind are required")
    if kind not in VALID_SUPPRESSION_KINDS:
        return _error(f"kind must be one of {sorted(VALID_SUPPRESSION_KINDS)}")

    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO suppression (client_id, ein, org_name, kind, source, added_at)
            VALUES (%(client_id)s, %(ein)s, %(org_name)s, %(kind)s, %(source)s, now())
            RETURNING id
            """,
            {
                "client_id": client_id,
                "ein": body.get("ein"),
                "org_name": org_name,
                "kind": kind,
                "source": body.get("source") or "dashboard manual entry",
            },
        )
        new_row = cur.fetchone()
        conn.commit()
    assert new_row is not None  # RETURNING id always yields a row on successful INSERT
    return _json_response({"id": new_row["id"]}, 201)


@app.route(route="suppression/{id}", methods=["DELETE"])
def delete_suppression(req: func.HttpRequest) -> func.HttpResponse:
    suppression_id = req.route_params.get("id")
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM suppression WHERE id = %s RETURNING id", (suppression_id,))
        row = cur.fetchone()
        conn.commit()
    if row is None:
        return _error("suppression entry not found", 404)
    return _json_response({"id": suppression_id, "status": "deleted"})


def _grounded_citation(values_signals: dict[str, Any] | None, key: str, corpus: str) -> str | None:
    """The stored citation for one values signal, only when it's grounded: a non-null
    score not flagged needs_human_verification, AND the whole citation, after qa's
    normalization (lower-case, punctuation/whitespace collapsed), appears verbatim in
    the org's own stored 990 mission/program text.

    Deliberately stricter than score.enforce_hard_rules' scoring-time check
    (qa._quoted_text_matches_corpus), which accepts any 25-character overlap and so
    passes a real opening with an invented tail. A customer-facing "quoted" cell must
    be a quote, so this export requires the full citation. Consequences, all failing
    safe to blank: quotes with "..." elisions, paraphrases, bare field pointers (e.g.
    "govt_pct 0.59"), and non-Latin text (the normalizer strips it to nothing) are
    not shown. The re-check is also why rows scored before the grounding check
    existed (2026-09-18) can't show an unverified quote. Never a fallback."""
    signal = (values_signals or {}).get(key)
    if not isinstance(signal, dict):
        return None
    citation = signal.get("citation")
    if signal.get("score") is None or signal.get("needs_human_verification"):
        return None
    if not isinstance(citation, str) or not citation.strip():
        return None
    normalized_citation = _normalize_for_match(citation)
    if not normalized_citation or normalized_citation not in _normalize_for_match(corpus):
        return None
    return citation


def _yes_no(value: Any) -> Any:
    """True/False -> Yes/No for customer-facing cells; None stays blank. Anything
    else passes through unchanged rather than being coerced into a yes/no claim."""
    if value is True:
        return "Yes"
    if value is False:
        return "No"
    return "" if value is None else value


def _alignment_cell(criteria_met_count: Any) -> str:
    """"n/6" only for a count that's actually possible; a bool, string, or a value
    outside 0..ALIGNMENT_CRITERIA_TOTAL (a data bug upstream) renders blank rather
    than publishing an impossible "7/6"."""
    if (
        isinstance(criteria_met_count, int)
        and not isinstance(criteria_met_count, bool)
        and 0 <= criteria_met_count <= ALIGNMENT_CRITERIA_TOTAL
    ):
        return f"{criteria_met_count}/{ALIGNMENT_CRITERIA_TOTAL}"
    return ""


def _neutralize_formula_cells(cells: dict[str, Any]) -> dict[str, Any]:
    """CSV/formula injection guard, applied once to every exported row. A text cell
    starting with = + - @ tab or CR is prefixed with a single quote so a spreadsheet
    shows it as text instead of evaluating it. Only str cells are touched, so the
    numeric columns (Revenue, Gap rank, Fundraising spend ratio — Decimal/float from
    the DB) are written as-is, negative numbers included. The one exemption is a
    Contact phone value made only of phone characters (e.g. "+1 207-671-2868"): it
    has no letters, so it can't name a function or DDE command."""
    neutralized = dict(cells)
    for key, value in cells.items():
        if not isinstance(value, str) or not value.startswith(FORMULA_PREFIXES):
            continue
        if key == "contact_phone" and PHONE_NUMBER_PATTERN.fullmatch(value):
            continue
        neutralized[key] = "'" + value
    return neutralized


def _trigger_label(code: str | None) -> str:
    """Unknown codes (e.g. a trigger type added to triggers.py before this mapping)
    pass through as-is rather than erroring or being blanked."""
    if not code:
        return ""
    return TRIGGER_LABELS.get(code, code)


def _prospect_row_to_csv_dict(row: dict[str, Any]) -> dict[str, Any]:
    alignment = row["alignment"] or {}
    capacity = row["capacity"] or {}
    # Reuses _build_contact rather than re-deriving locked/expired/mismatch from raw
    # row fields a second time — guarantees the CSV and the dashboard table can never
    # silently disagree on these signals (E2 fix, Copilot's independent review: the
    # CSV was missing both entirely).
    contact = _build_contact(row)
    locked = not contact["enriched"]

    if locked:
        # Locked (never enriched) gets the literal "Enrich to unlock" cell text per
        # §5 v2.3 — applies the same whether it's locked by the tenant flag or simply
        # not yet clicked for this prospect; a real "not matched" result (enriched,
        # email null) stays a blank cell, not "Enrich to unlock".
        contact_email = "Enrich to unlock"
        contact_phone = "Enrich to unlock"
        contact_status = "Enrich to unlock"
    else:
        contact_email = contact["email"] or ""
        if contact["contact_mismatch"]:
            # Surfaces who the email/phone actually belong to right on the email
            # cell, matching the dashboard table's "⚠ enriched for ..." note —
            # doesn't touch the mismatch-detection comparison itself (product
            # decision under separate review), just carries the existing signal
            # into the CSV instead of silently dropping it.
            contact_email = f"{contact_email} (enriched for {contact['enriched_contact_name']})".strip()
        contact_phone = contact["phone"] or ""
        contact_status = contact["email_status"] or ""
        if contact["retention_expired"]:
            contact_status = f"{contact_status} (expired)".strip()

    corpus =f"{row['filing_mission_text'] or ''} {_program_text_corpus(row['filing_program_text'])}"
    leadership_citation = _grounded_citation(row["values_signals"], "leadership_composition", corpus)

    return _neutralize_formula_cells({
        "ein": row["ein"],
        "name": row["org_name"],
        "city": row["city"],
        "state": row["state"],
        "revenue": row["filing_revenue_total"],
        "gap_rank": row["gap_rank"],
        "alignment": _alignment_cell(alignment.get("criteria_met_count")),
        "qualifies": _yes_no(alignment.get("qualifies")),
        "assigned_trigger": _trigger_label(row["assigned_trigger"]),
        "trigger_angle": row["trigger_angle"],
        "values_population_served": _grounded_citation(row["values_signals"], "population_served", corpus)
        or "",
        "values_leadership": leadership_citation or LEADERSHIP_NEEDS_CONFIRMATION,
        "dd_present": _yes_no(capacity.get("dd_present")),
        "fundraising_spend_ratio": capacity.get("fundraising_spend_ratio"),
        "website": row["website"] or "",
        "suppression_flag": row["suppression_flag"] or "",
        "contact_name": contact["name"] or "",
        "contact_title": contact["title"] or "",
        "contact_email": contact_email,
        "contact_phone": contact_phone,
        "contact_status": contact_status,
    })


def _render_csv(rows: list[dict[str, Any]]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CSV_FIELDNAMES)
    writer.writerow(dict(zip(CSV_FIELDNAMES, CSV_HEADERS, strict=True)))
    for row in rows:
        writer.writerow(_prospect_row_to_csv_dict(row))
    return buffer.getvalue()


@app.route(route="export.csv", methods=["GET"])
def export_csv(req: func.HttpRequest) -> func.HttpResponse:
    client_id = req.params.get("client_id")
    if not client_id:
        return _error("client_id is required")

    with _conn() as conn, conn.cursor() as cur:
        cur.execute(EXPORT_QUERY, {"client_id": client_id})
        rows = cur.fetchall()

    return func.HttpResponse(
        # BOM so Excel detects UTF-8 instead of mis-decoding accented/non-Latin text.
        CSV_BOM + _render_csv(rows),
        status_code=200,
        mimetype="text/csv",
        charset="utf-8",
        headers={"Content-Disposition": f"attachment; filename=prospects_client{client_id}.csv"},
    )


@app.route(route="runs", methods=["GET"])
def list_runs(req: func.HttpRequest) -> func.HttpResponse:
    client_id = req.params.get("client_id")
    stage = req.params.get("stage")
    limit_param = req.params.get("limit", "10")
    if not client_id:
        return _error("client_id is required")
    try:
        limit = max(1, min(int(limit_param), 100))
    except ValueError:
        return _error("limit must be an integer")

    query = (
        "SELECT id, client_id, stage, started_at, finished_at, status, counts, error "
        "FROM runs WHERE client_id = %(client_id)s"
    )
    params: dict[str, Any] = {"client_id": client_id, "limit": limit}
    if stage:
        query += " AND stage = %(stage)s"
        params["stage"] = stage
    query += " ORDER BY started_at DESC LIMIT %(limit)s"

    with _conn() as conn, conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
    return _json_response(rows)


@app.route(route="clients", methods=["GET"])
def list_clients(req: func.HttpRequest) -> func.HttpResponse:
    """Internal dev tool for the dashboard's client switcher (no auth in this app
    today — see App.tsx). Unfiltered on purpose: every row, every caller."""
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, name FROM clients ORDER BY id")
        rows = cur.fetchall()
    return _json_response(rows)
