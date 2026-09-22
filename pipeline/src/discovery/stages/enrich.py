"""E2 — per-prospect enrichment orchestration (§5.5, hard rule 8). Not a pipeline
stage in the Stage 0-6 sense (§4) — invoked on demand by the dashboard's Enrich
button, never by a scheduled run. Lives in `stages/` alongside the other per-EIN
business logic (triggers.py, publish.py) since it reads the same tables through the
same conventions, not because it participates in the publish pipeline.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from discovery.clients.enrichment_provider import (
    EnrichmentCandidate,
    EnrichmentProvider,
    EnrichmentResult,
    PollOutcome,
)

RETENTION_DAYS = 90  # FullEnrich reseller terms (§5.5)

# Deliberately NOT triggers.py's EXECUTIVE_TITLE_KEYWORDS: that list orders
# ("executive director", "president", "chief executive") for trigger-detection
# purposes (did the top-listed exec change), where "president" ranking above
# "chief executive" is a reasonable fallback for small orgs with no separate ED. For
# picking the single best *contact*, that ordering picked a wrong result against real
# data: Day One's 2024 filing lists both a Board President (Katie Grant, unpaid) and
# a CEO (Cassandra Humphrey, paid) — reusing triggers.py's order would submit the
# board president for enrichment over the actual chief executive. This list ranks
# paid-staff-likely titles first and leaves "president" last, since it's the title
# most likely to mean an unpaid board role rather than staff.
_TITLE_PRIORITY = ("EXECUTIVE DIRECTOR", "CEO", "CHIEF EXECUTIVE", "PRESIDENT")

# 990 officer titles carry their own departure notes (e.g. "CEO (THRU MAR 2024)"),
# distinct from _TRAILING_NOTE_RE below which strips the same kind of note from
# *names*. A departed officer is still a last-resort candidate (better than nothing
# per hard rule 3's "blank beats wrong" — E1 enriched several THRU-marked officers
# with real, if uneven, results) but must never outrank a current one in the same
# filing, which Day One's data shows can otherwise happen.
_DEPARTURE_MARKER_RE = re.compile(r"\bTHRU\b|\bTERMED\b|\bFORMER\b", re.IGNORECASE)
_TRAILING_NOTE_RE = re.compile(r"\s+(?:TERMED|THRU|\()", re.IGNORECASE)


def can_enrich(
    status: str | None,
    enrichment_enabled: bool,
    fullenrich_subaccount_id: str | None,
) -> tuple[bool, str | None]:
    """Hard rule 8 (§9) as executable code: enrichment never fires unless all three
    hold. Returns (allowed, reason_if_not) so the caller can surface *why*, not just
    a bare 403 — the tenant-flag and subaccount checks are meaningfully different
    situations for a reviewer to see."""
    if status != "approved":
        return False, "prospect is not approved"
    if not enrichment_enabled:
        return False, "enrichment is not enabled for this tenant"
    if not fullenrich_subaccount_id:
        return False, "tenant has no fullenrich_subaccount_id configured"
    return True, None


def select_primary_officer(officers: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    """Prefer is_officer=true entries, then a current (non-departed) officer over a
    departed one regardless of title, then executive-title keywords, else the first
    officer listed. Used both for the dashboard's always-visible Name/Title columns
    and to pick who gets submitted to the enrichment provider — the brief treats
    these as the same "primary contact" concept (§5 v2.3)."""
    if not officers:
        return None
    is_officers = [o for o in officers if o.get("is_officer")]
    pool = is_officers or officers

    def rank(officer: dict[str, Any]) -> tuple[bool, int]:
        title = officer.get("title") or ""
        departed = bool(_DEPARTURE_MARKER_RE.search(title))
        title_upper = title.upper()
        for i, key in enumerate(_TITLE_PRIORITY):
            if key in title_upper:
                return departed, i
        return departed, len(_TITLE_PRIORITY)

    return min(pool, key=rank)


def split_officer_name(raw_name: str) -> tuple[str, str] | None:
    """990 officer names carry inconsistent trailing notes ("TERMED ...", "THRU
    ...") and ordering — same cleanup E1's spike script validated against real
    ARCHITECT data."""
    name = _TRAILING_NOTE_RE.split(raw_name, maxsplit=1)[0].strip()
    parts = name.split()
    if len(parts) < 2:
        return None
    return parts[0].title(), " ".join(p.title() for p in parts[1:])


def build_candidate(
    ein: str, org_domain: str | None, officer: dict[str, Any] | None
) -> EnrichmentCandidate | None:
    """None means nothing usable to submit — no officer name, or no org domain on
    file. The pipeline never constructs or guesses an email (§9 rule 3), so a missing
    domain is never worked around; the caller surfaces this as "can't enrich," not a
    silent skip."""
    if officer is None or not org_domain:
        return None
    parsed = split_officer_name(officer.get("name") or "")
    if parsed is None:
        return None
    first_name, last_name = parsed
    return EnrichmentCandidate(
        ein=ein, first_name=first_name, last_name=last_name, title=officer.get("title"), domain=org_domain
    )


@dataclass
class EnrichAttempt:
    status: str  # "done" | "pending" | "blocked" | "no_candidate"
    reason: str | None = None
    job_id: str | None = None
    result: EnrichmentResult | None = None  # set only when status == "done"


def start_enrichment(
    provider: EnrichmentProvider,
    ein: str,
    org_domain: str | None,
    officer: dict[str, Any] | None,
    status: str | None,
    enrichment_enabled: bool,
    fullenrich_subaccount_id: str | None,
    poll_attempts: int,
    poll_interval_s: float,
) -> EnrichAttempt:
    """Submits one contact and waits up to poll_attempts * poll_interval_s before
    giving up and returning "pending" — a bounded budget so an HTTP handler calling
    this never risks a gateway timeout, regardless of how long FullEnrich actually
    takes (E1 never measured real submit-to-completion latency for a single contact,
    so this doesn't assume a number; see check_enrichment for the resume path)."""
    allowed, reason = can_enrich(status, enrichment_enabled, fullenrich_subaccount_id)
    if not allowed:
        return EnrichAttempt(status="blocked", reason=reason)

    candidate = build_candidate(ein, org_domain, officer)
    if candidate is None:
        return EnrichAttempt(status="no_candidate", reason="no usable officer name or org domain on file")

    job_id = provider.submit(candidate)
    return check_enrichment(provider, job_id, org_domain, poll_attempts=poll_attempts, poll_interval_s=poll_interval_s)


def check_enrichment(
    provider: EnrichmentProvider,
    job_id: str,
    org_domain: str | None,
    poll_attempts: int,
    poll_interval_s: float,
) -> EnrichAttempt:
    """Resume path for a job that was still pending after start_enrichment's own
    budget — same bounded-attempts shape, called again by the dashboard's status
    poll until it resolves."""
    for attempt in range(poll_attempts):
        poll_result = provider.poll(job_id, org_domain)
        if poll_result.outcome is PollOutcome.DONE:
            return EnrichAttempt(status="done", job_id=job_id, result=poll_result.result)
        if attempt < poll_attempts - 1:
            time.sleep(poll_interval_s)
    return EnrichAttempt(status="pending", job_id=job_id)


def compute_retention_expires_at(completed_at: datetime) -> datetime:
    return completed_at + timedelta(days=RETENTION_DAYS)
