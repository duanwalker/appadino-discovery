"""FullEnrichProvider — the §5.5/E2 EnrichmentProvider implementation. Single-contact
calls reuse the bulk endpoint with a one-item batch (FullEnrich has no single-contact
endpoint); async submit-then-poll per E1's confirmed API ergonomics.
"""

from __future__ import annotations

import re
from typing import Any

from discovery.clients import fullenrich
from discovery.clients.enrichment_provider import (
    EnrichmentCandidate,
    EnrichmentResult,
    PollOutcome,
    PollResult,
)

PROVIDER_NAME = "fullenrich"
ENRICH_FIELDS = ["contact.work_emails", "contact.phones"]

# Mirrors pipeline/scripts/e1_fullenrich_spike.py's already-validated mapping (E1
# closed GO against these exact buckets, 2026-09-19). Duplicated rather than shared
# because that script is a closed, frozen validation artifact — reaching into it from
# production code isn't part of this gate.
EMAIL_STATUS_MAP: dict[str, str] = {
    "DELIVERABLE": "verified",
    "HIGH_PROBABILITY": "catch_all",
    "CATCH_ALL": "catch_all",
    "INVALID": "not_found",
    "INVALID_DOMAIN": "not_found",
}

# Fallback confidence when a match has no phone (FullEnrich's v2 API exposes no
# numeric email confidence, only this status enum) — hard rule 3 (§9) requires every
# written row to carry a confidence value, never NULL.
EMAIL_STATUS_CONFIDENCE: dict[str, int] = {
    "DELIVERABLE": 95,
    "HIGH_PROBABILITY": 70,
    "CATCH_ALL": 50,
    "INVALID": 10,
    "INVALID_DOMAIN": 5,
}

# Not exhaustive by design (see _classify_domain) — common consumer providers a
# nonprofit board volunteer might list on a 990 instead of an org address. Missing an
# obscure one just means an occasional wrongly-flagged stale rather than a wrongly-
# cleared one, the safer direction to err in per hard rule 3 ("blank beats wrong").
PERSONAL_EMAIL_DOMAINS: frozenset[str] = frozenset(
    {
        "gmail.com",
        "yahoo.com",
        "ymail.com",
        "hotmail.com",
        "outlook.com",
        "live.com",
        "msn.com",
        "icloud.com",
        "me.com",
        "aol.com",
        "protonmail.com",
        "proton.me",
        "mail.com",
        "gmx.com",
        "zoho.com",
        "yandex.com",
        "comcast.net",
        "verizon.net",
        "att.net",
        "sbcglobal.net",
    }
)


def domain_from_website(website: str | None) -> str | None:
    if not website:
        return None
    domain = website.strip().lower()
    domain = re.sub(r"^https?://", "", domain)
    domain = re.sub(r"^www\.", "", domain)
    domain = domain.split("/")[0].strip()
    return domain or None


def classify_domain(email_domain: str, org_domain: str | None) -> tuple[str | None, str | None]:
    """Returns (email_status_override, stale_detail). (None, None) means: trust
    FullEnrich's own reported status, no override.

    E1's spot-check found FullEnrich's own verified/deliverable status doesn't mean
    the contact is still at the org being prospected (a Day One officer resolved to a
    sheppardpratt.org address) — a domain mismatch against the org's own website is
    real signal. But a domain-only check has a false-positive mode: unpaid nonprofit
    board officers very often list a personal address (gmail, yahoo, ...) on the 990
    even when their listed role is completely current — that isn't staleness, it's
    just how they filed. Personal-domain matches are excluded from the mismatch
    inference entirely and fall back to FullEnrich's own status untouched.
    """
    if email_domain in PERSONAL_EMAIL_DOMAINS:
        return None, None
    if org_domain and email_domain != org_domain:
        return "stale_likely_moved", email_domain
    return None, None


def parse_contact_result(contact_result: dict[str, Any], org_domain: str | None) -> EnrichmentResult:
    # Actual API response nests these under `contact_info`, not top-level as the
    # public docs page's summary implies — confirmed against a real response, E1.
    contact_info = contact_result.get("contact_info") or {}
    work_email_obj = contact_info.get("most_probable_work_email") or {}
    phone_obj = contact_info.get("most_probable_phone") or {}

    email = work_email_obj.get("email")
    fe_status = work_email_obj.get("status")
    email_status = EMAIL_STATUS_MAP.get(fe_status) if fe_status else None
    stale_detail: str | None = None

    if email:
        email_domain = email.rsplit("@", 1)[-1].lower()
        override_status, override_detail = classify_domain(email_domain, org_domain)
        if override_status:
            email_status = override_status
            stale_detail = override_detail

    phone = phone_obj.get("number")
    phone_confidence = phone_obj.get("ownership_match_confidence")
    matched = bool(email or phone)
    provider_confidence = (
        phone_confidence if phone_confidence is not None else EMAIL_STATUS_CONFIDENCE.get(fe_status or "", 0)
    )

    profile = contact_result.get("profile") or {}
    # 990 officer names are inconsistently ordered/formatted (e.g. "Lundin Sarah")
    # — prefer FullEnrich's resolved LinkedIn profile name when it found one (E1).
    contact_name = profile.get("full_name")

    credits_spent = (1 if email else 0) + (10 if phone else 0)

    return EnrichmentResult(
        contact_name=contact_name,
        contact_title=None,
        email=email,
        email_status=email_status if matched else "not_found",
        stale_detail=stale_detail,
        phone=phone,
        linkedin_url=profile.get("linkedin_url"),
        provider_confidence=provider_confidence if matched else 0,
        credits_spent=credits_spent,
        raw=contact_result,
    )


class FullEnrichProvider:
    name = PROVIDER_NAME

    def __init__(self, api_key: str, subaccount_id: str | None = None) -> None:
        self._api_key = api_key
        # Unused on the wire until the reseller-terms reconciliation confirms the
        # Sub-Account-Id header (STATUS.md open item — no such header appears in
        # FullEnrich's public v2 docs as of E1). Stored so the call site is ready
        # the moment that's confirmed, without another design pass.
        self._subaccount_id = subaccount_id

    def submit(self, candidate: EnrichmentCandidate) -> str:
        contact = {
            "firstname": candidate.first_name,
            "lastname": candidate.last_name,
            "domain": candidate.domain,
            "enrich_fields": ENRICH_FIELDS,
            "custom": {"ein": candidate.ein},
        }
        batch_name = f"e2-enrich-{candidate.ein}"
        return fullenrich.start_bulk_enrichment(self._api_key, batch_name, [contact])

    def poll(self, job_id: str, org_domain: str | None) -> PollResult:
        batch_result = fullenrich.get_bulk_enrichment(self._api_key, job_id)
        status = batch_result.get("status")
        if status not in fullenrich.TERMINAL_STATUSES:
            return PollResult(outcome=PollOutcome.PENDING)

        data = batch_result.get("data") or []
        if not data:
            return PollResult(
                outcome=PollOutcome.DONE,
                result=EnrichmentResult(
                    contact_name=None,
                    contact_title=None,
                    email=None,
                    email_status="not_found",
                    stale_detail=None,
                    phone=None,
                    linkedin_url=None,
                    provider_confidence=0,
                    credits_spent=0,
                    raw=batch_result,
                ),
            )
        return PollResult(outcome=PollOutcome.DONE, result=parse_contact_result(data[0], org_domain))
