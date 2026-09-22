"""E1 FullEnrich validation spike (§5.5, §8 gate table).

Standalone, one-time, manually-invoked script — NOT wired into discovery.cli, NOT a
pipeline stage, NOT auto-triggered by anything. Its entire purpose is to produce the
match-rate / verified-rate / cost-per-usable-contact numbers gate E1 needs, so Duan
can record a go/no-go on icp_configs.config.enrichment_enabled in STATUS.md.

Run with:
    DATABASE_URL=... FULLENRICH_API_KEY=... python pipeline/scripts/e1_fullenrich_spike.py

Hard rules this script still enforces even as a one-off (§9 rule 8):
  - Only ever reads status='approved' prospects (never any other status).
  - Never fabricates or pattern-guesses a contact/email — every contact submitted to
    FullEnrich comes from a named officer/director on the org's own 990 filing, and
    every result written to `enrichments` is exactly what FullEnrich returned.
The one rule this script deliberately does NOT enforce is "never fire when
enrichment_enabled is false" — that flag's whole purpose is to gate the *production*
enrichment path once E1 has been evaluated; this script IS the E1 evaluation, so it
runs regardless and prints the flag's current value prominently instead (see
_check_tenant_config below).

Contact selection: prospects here are organizations (identified by EIN), not people,
so there's no single "the contact" already on file. Each prospect's latest 990 filing
lists its officers/directors/trustees (`filings.officers` JSONB, IRS Form 990 Part
VII) with a name, title, and an `is_officer` flag. This script picks one person per
org — preferring is_officer=true entries, then titles matching
president/executive-director/CEO — as a stand-in for "the person to enrich." This is
a real methodology limitation worth weighing when reading the results: these are
often board officers (President, Treasurer, Secretary), not paid staff, and 990 name
fields are inconsistently formatted (case, trailing "TERMED ..."/"THRU ..." notes,
ambiguous first/last order) — see the report's caveats section.

email_status mapping: FullEnrich's v2 API returns a five-way status enum
(DELIVERABLE, HIGH_PROBABILITY, CATCH_ALL, INVALID, INVALID_DOMAIN) per
https://docs.fullenrich.com/api/v2/contact/enrich/bulk/get (confirmed 2026-09-19).
The brief's §5.5 sketch only names three buckets. This script maps conservatively:
    DELIVERABLE      -> verified
    HIGH_PROBABILITY -> catch_all   (NOT verified — deliberately conservative,
                                      since the adoption threshold is verified-rate)
    CATCH_ALL        -> catch_all
    INVALID / INVALID_DOMAIN -> not_found
This collapses two distinct FullEnrich confidence levels into one bucket; the raw
FullEnrich status string is always preserved in `enrichments.raw` regardless.

Sub-account header: the brief's §5.5 spec describes a `Sub-Account-Id` header for
per-client isolation, sourced from icp_configs.config.fullenrich_subaccount_id. That
config key is currently unset (None) for client_id=2, and FullEnrich's public v2 API
docs (docs.fullenrich.com, checked 2026-09-19) do not document any such header at
all — it may be a reseller-agreement-specific feature not in the public docs, per
STATUS.md's open item that reseller terms are still being reconciled. This script
proceeds without that header (Bearer-token-only auth, matching the public API
contract) and flags this explicitly as an open question for that reconciliation
rather than guessing a header name/value.
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import psycopg
from psycopg.types.json import Json

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from discovery.clients import fullenrich  # noqa: E402

logger = logging.getLogger(__name__)

PROVIDER = "fullenrich"
ENRICH_FIELDS = ["contact.work_emails", "contact.phones"]
RETENTION_DAYS = 90

# Ordered most- to least-preferred; matched as a substring of the officer's title.
TITLE_PRIORITY = ["EXECUTIVE DIRECTOR", "CHIEF EXECUTIVE", "CEO", "PRESIDENT"]

EMAIL_STATUS_MAP = {
    "DELIVERABLE": "verified",
    "HIGH_PROBABILITY": "catch_all",
    "CATCH_ALL": "catch_all",
    "INVALID": "not_found",
    "INVALID_DOMAIN": "not_found",
}

# Fallback confidence when a match has no phone (so no ownership_match_confidence is
# available) — FullEnrich's v2 API exposes no numeric confidence for emails, only the
# status enum above, so this is our own heuristic mapping, not a FullEnrich value.
# Hard rule 3 (§9) requires every enrichments record to carry a confidence value, not
# just phone-matched ones — an email-only match must not end up with confidence=NULL.
EMAIL_STATUS_CONFIDENCE = {
    "DELIVERABLE": 95,
    "HIGH_PROBABILITY": 70,
    "CATCH_ALL": 50,
    "INVALID": 10,
    "INVALID_DOMAIN": 5,
}

_TRAILING_NOTE_RE = re.compile(r"\s+(?:TERMED|THRU|\()", re.IGNORECASE)


@dataclass
class Candidate:
    prospect_id: int
    ein: str
    org_name: str
    first_name: str
    last_name: str
    title: str | None
    domain: str


@dataclass
class SpikeResult:
    total_approved: int
    submitted: list[Candidate]
    skipped_no_contact: list[tuple[str, str]] = field(default_factory=list)  # (ein, reason)
    matched: list[dict[str, Any]] = field(default_factory=list)
    not_matched: list[dict[str, Any]] = field(default_factory=list)
    batch_cost_credits: int | None = None
    computed_total_credits: int = 0
    enrichment_enabled: bool = False
    fullenrich_subaccount_id: str | None = None
    dry_run: bool = False


def _clean_name(raw_name: str) -> tuple[str, str] | None:
    name = _TRAILING_NOTE_RE.split(raw_name, maxsplit=1)[0].strip()
    parts = name.split()
    if len(parts) < 2:
        return None
    return parts[0].title(), " ".join(p.title() for p in parts[1:])


def _select_contact(officers: list[dict[str, Any]] | None) -> tuple[str, str, str | None] | None:
    if not officers:
        return None
    is_officers = [o for o in officers if o.get("is_officer")]
    pool = is_officers or officers

    def rank(o: dict[str, Any]) -> int:
        title = (o.get("title") or "").upper()
        for i, key in enumerate(TITLE_PRIORITY):
            if key in title:
                return i
        return len(TITLE_PRIORITY)

    for officer in sorted(pool, key=rank):
        parsed = _clean_name(officer.get("name") or "")
        if parsed:
            return parsed[0], parsed[1], officer.get("title")
    return None


def _domain_from_website(website: str | None) -> str | None:
    if not website:
        return None
    domain = website.strip().lower()
    domain = re.sub(r"^https?://", "", domain)
    domain = re.sub(r"^www\.", "", domain)
    domain = domain.split("/")[0].strip()
    return domain or None


def _load_candidates(conn: psycopg.Connection, client_id: int, limit: int | None) -> tuple[list[Candidate], list[tuple[str, str]]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT p.id, p.ein, p.status, o.name, o.website, f.officers
            FROM prospects p
            JOIN organizations o ON o.ein = p.ein
            LEFT JOIN LATERAL (
                SELECT officers FROM filings f2
                WHERE f2.ein = p.ein AND f2.officers IS NOT NULL
                ORDER BY tax_year DESC LIMIT 1
            ) f ON true
            WHERE p.client_id = %s AND p.status = 'approved'
            ORDER BY p.updated_at
            """,
            (client_id,),
        )
        rows = cur.fetchall()

    candidates: list[Candidate] = []
    skipped: list[tuple[str, str]] = []
    for prospect_id, ein, status, org_name, website, officers in rows:
        assert status == "approved", f"guard violated: ein={ein} status={status}"  # defense in depth (§9 rule 8)

        domain = _domain_from_website(website)
        if not domain:
            skipped.append((ein, "no website/domain on file"))
            continue

        contact = _select_contact(officers)
        if not contact:
            skipped.append((ein, "no usable officer name on latest filing"))
            continue

        first_name, last_name, title = contact
        candidates.append(
            Candidate(
                prospect_id=prospect_id,
                ein=ein,
                org_name=org_name,
                first_name=first_name,
                last_name=last_name,
                title=title,
                domain=domain,
            )
        )

    if limit is not None:
        candidates = candidates[:limit]
    return candidates, skipped


def _check_tenant_config(conn: psycopg.Connection, client_id: int) -> tuple[bool, str | None]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT config FROM icp_configs WHERE client_id = %s AND active = true ORDER BY version DESC LIMIT 1",
            (client_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise ValueError(f"no active icp_config for client_id={client_id}")
    config = row[0]
    enrichment_enabled = bool(config.get("enrichment_enabled", False))
    subaccount_id = config.get("fullenrich_subaccount_id")
    return enrichment_enabled, subaccount_id


def _insert_enrichment_row(
    conn: psycopg.Connection,
    client_id: int,
    candidate: Candidate,
    contact_name: str,
    email: str | None,
    email_status: str | None,
    phone: str | None,
    provider_confidence: int | None,
    raw: dict[str, Any],
    credits_spent: int,
    requested_at: datetime,
    completed_at: datetime,
) -> None:
    retention_expires_at = completed_at + timedelta(days=RETENTION_DAYS)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO enrichments (
                client_id, ein, prospect_id, provider, contact_name, contact_title,
                email, email_status, phone, provider_confidence, raw, credits_spent,
                requested_at, completed_at, retention_expires_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                client_id,
                candidate.ein,
                candidate.prospect_id,
                PROVIDER,
                contact_name,
                candidate.title,
                email,
                email_status,
                phone,
                provider_confidence,
                Json(raw),
                credits_spent,
                requested_at,
                completed_at,
                retention_expires_at,
            ),
        )
    conn.commit()


def run_spike(
    client_id: int,
    database_url: str,
    api_key: str,
    limit: int | None = None,
    poll_interval_s: float = 10.0,
    poll_timeout_s: float = 600.0,
    dry_run: bool = False,
    replay_enrichment_id: str | None = None,
) -> SpikeResult:
    with psycopg.connect(database_url) as conn:
        enrichment_enabled, subaccount_id = _check_tenant_config(conn, client_id)
        logger.info(
            "TENANT_CONFIG client_id=%s enrichment_enabled=%s fullenrich_subaccount_id=%s",
            client_id,
            enrichment_enabled,
            subaccount_id,
        )

        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM prospects WHERE client_id = %s AND status = 'approved'", (client_id,))
            total_approved = cur.fetchone()[0]

        candidates, skipped = _load_candidates(conn, client_id, limit)
        result = SpikeResult(
            total_approved=total_approved,
            submitted=candidates,
            skipped_no_contact=skipped,
            enrichment_enabled=enrichment_enabled,
            fullenrich_subaccount_id=subaccount_id,
            dry_run=dry_run,
        )

        if not candidates:
            logger.warning("NO_CANDIDATES client_id=%s total_approved=%s skipped=%s", client_id, total_approved, len(skipped))
            return result

        contacts_payload = [
            {
                "first_name": c.first_name,
                "last_name": c.last_name,
                "domain": c.domain,
                "enrich_fields": ENRICH_FIELDS,
                "custom": {"ein": c.ein},
            }
            for c in candidates
        ]

        if dry_run:
            logger.info("DRY_RUN would submit %d contacts: %s", len(contacts_payload), contacts_payload)
            return result

        with httpx.Client(timeout=30.0) as client:
            requested_at = datetime.now(UTC)
            if replay_enrichment_id:
                # Re-fetch an already-completed batch (GET is free) instead of
                # re-submitting — used to fix a parsing bug after the fact without
                # spending credits twice on the same 19 contacts.
                enrichment_id = replay_enrichment_id
                logger.info("REPLAYING enrichment_id=%s (no new credits spent)", enrichment_id)
            else:
                credits = fullenrich.get_credits(api_key, client=client)
                logger.info("FULLENRICH_CREDITS balance=%s", credits)
                batch_name = f"e1-spike-client{client_id}-{requested_at:%Y%m%d}"
                enrichment_id = fullenrich.start_bulk_enrichment(api_key, batch_name, contacts_payload, client=client)
                logger.info("FULLENRICH_BATCH_STARTED enrichment_id=%s n=%d", enrichment_id, len(contacts_payload))

            batch_result = fullenrich.poll_bulk_enrichment(
                api_key, enrichment_id, poll_interval_s=poll_interval_s, timeout_s=poll_timeout_s, client=client
            )
            completed_at = datetime.now(UTC)

        batch_status = batch_result.get("status")
        logger.info("FULLENRICH_BATCH_DONE status=%s", batch_status)
        result.batch_cost_credits = (batch_result.get("cost") or {}).get("credits")

        data = batch_result.get("data") or []
        if len(data) != len(candidates):
            logger.warning(
                "RESULT_COUNT_MISMATCH submitted=%d returned=%d — falling back to index-order zip",
                len(candidates),
                len(data),
            )

        by_ein = {c.ein: c for c in candidates}
        for i, contact_result in enumerate(data):
            result_ein = (contact_result.get("custom") or {}).get("ein")
            candidate = by_ein.get(result_ein) if result_ein else None
            if candidate is None:
                if i >= len(candidates):
                    logger.warning("UNMATCHED_RESULT index=%d custom=%s — skipping", i, contact_result.get("custom"))
                    continue
                candidate = candidates[i]
                logger.warning("RESULT_CORRELATION_FALLBACK index=%d used positional match (no/unknown custom.ein)", i)

            # Actual API response nests these under `contact_info`, not top-level —
            # the public docs page (docs.fullenrich.com/api/v2/contact/enrich/bulk/get)
            # describes them flat; confirmed by inspecting a real response 2026-09-19.
            contact_info = contact_result.get("contact_info") or {}
            work_email_obj = contact_info.get("most_probable_work_email") or {}
            phone_obj = contact_info.get("most_probable_phone") or {}
            email = work_email_obj.get("email")
            fe_status = work_email_obj.get("status")
            email_status = EMAIL_STATUS_MAP.get(fe_status) if fe_status else None
            phone = phone_obj.get("number")
            # Prefer FullEnrich's own numeric phone confidence when we have it (real
            # provider data); fall back to the email-status heuristic above only when
            # there's no phone — every matched record must carry SOME confidence
            # value per hard rule 3, never NULL.
            phone_confidence = phone_obj.get("ownership_match_confidence")
            provider_confidence = phone_confidence if phone_confidence is not None else EMAIL_STATUS_CONFIDENCE.get(fe_status)

            credits_spent = (1 if email else 0) + (10 if phone else 0)
            result.computed_total_credits += credits_spent

            # 990 officer names are inconsistently ordered/formatted (e.g. "Lundin
            # Sarah" for Sarah Lundin) — when FullEnrich resolves a LinkedIn profile
            # for the match, its `profile.full_name` is the real name and preferred
            # over our own first/last split of the filing's raw text.
            profile = contact_result.get("profile")
            contact_name = profile["full_name"] if profile and profile.get("full_name") else f"{candidate.first_name} {candidate.last_name}"
            record = {
                "ein": candidate.ein,
                "org_name": candidate.org_name,
                "contact_name": contact_name,
                "contact_title": candidate.title,
                "email": email,
                "email_status": email_status,
                "fullenrich_status": fe_status,
                "phone": phone,
                "provider_confidence": provider_confidence,
                "credits_spent": credits_spent,
            }

            if email or phone:
                _insert_enrichment_row(
                    conn,
                    client_id,
                    candidate,
                    contact_name,
                    email,
                    email_status,
                    phone,
                    provider_confidence,
                    contact_result,
                    credits_spent,
                    requested_at,
                    completed_at,
                )
                result.matched.append(record)
            else:
                result.not_matched.append(record)

    return result


def _format_report(client_id: int, result: SpikeResult) -> str:
    submitted_n = len(result.submitted)
    matched_n = len(result.matched)
    match_rate = matched_n / submitted_n if submitted_n else 0.0

    with_email = [m for m in result.matched if m["email"]]
    verified = [m for m in with_email if m["email_status"] == "verified"]
    verified_rate = len(verified) / len(with_email) if with_email else None

    total_credits = result.computed_total_credits
    avg_credits = total_credits / submitted_n if submitted_n else 0.0
    cost_per_usable = total_credits / len(verified) if verified else None

    lines: list[str] = []
    lines.append(f"# E1 FullEnrich Validation Spike — client_id={client_id}")
    lines.append("")
    lines.append(f"Run at: {datetime.now(UTC).isoformat()}")
    lines.append("")
    lines.append("## Tenant config")
    lines.append(f"- `icp_configs.config.enrichment_enabled` = **{result.enrichment_enabled}**")
    lines.append(f"- `icp_configs.config.fullenrich_subaccount_id` = **{result.fullenrich_subaccount_id}**")
    if not result.enrichment_enabled:
        lines.append(
            "  - **Flag:** this is currently `false`. Any rows this run writes to `enrichments` "
            "will NOT render in the dashboard's contact columns until this is set `true` for "
            "client_id={} — the data exists in the DB but is silently invisible until then.".format(client_id)
        )
    if not result.fullenrich_subaccount_id:
        lines.append(
            "  - **Open question:** unset. FullEnrich's public v2 API docs do not document a "
            "`Sub-Account-Id` header; this run authenticated with the Bearer API key alone. "
            "Needs reconciling with the reseller-agreement terms (per STATUS.md open item)."
        )
    lines.append("")
    lines.append("## Volume")
    lines.append(f"- Approved prospects (client_id={client_id}): **{result.total_approved}**")
    lines.append(
        f"- Note on sample size: the brief's spot-check-10 criterion wants closer to 25 approved "
        f"prospects for a meaningful read; {result.total_approved} is "
        f"{'enough for a meaningful read' if result.total_approved >= 25 else 'below that — treat this run as mechanics-only, not a final adoption verdict'}."
    )
    lines.append(f"- Submitted to FullEnrich: **{submitted_n}**")
    lines.append(f"- Skipped (no usable contact/domain before ever calling the API): **{len(result.skipped_no_contact)}**")
    for ein, reason in result.skipped_no_contact:
        lines.append(f"  - {ein}: {reason}")
    lines.append("")
    lines.append("## Results")
    lines.append(f"- Matched (email and/or phone found): **{matched_n}** ({match_rate:.0%} of submitted)")
    lines.append(f"- Not matched: **{submitted_n - matched_n}**")
    lines.append(f"- Of matches with an email, verified: **{len(verified)}/{len(with_email)}**"
                 + (f" ({verified_rate:.0%})" if verified_rate is not None else " (n/a — no emails found)"))
    lines.append(f"- Total credits spent (computed from per-field pricing: 1/work-email, 10/phone): **{total_credits}**")
    if result.batch_cost_credits is not None:
        lines.append(f"- FullEnrich-reported batch cost: **{result.batch_cost_credits}** credits"
                     + ("" if result.batch_cost_credits == total_credits else " (differs from computed total — see API-ergonomics notes)"))
    lines.append(f"- Average credits per submitted contact: **{avg_credits:.1f}**")
    lines.append(
        "- Cost per usable (verified) contact: "
        + (f"**{cost_per_usable:.1f} credits**" if cost_per_usable is not None else "**n/a — zero verified contacts**")
    )
    lines.append("")
    lines.append("## API ergonomics findings")
    lines.append("- Response pattern: **asynchronous** — POST /contact/enrich/bulk returns only an `enrichment_id`; "
                  "results are fetched via GET /contact/enrich/bulk/{id} (polling) or a `webhook_url`/`webhook_events` "
                  "callback. This script used polling (no public endpoint available to receive a webhook); "
                  "FullEnrich's own docs call polling \"not recommended\" in favor of webhooks.")
    lines.append("- Credits: v2 docs list per-field pricing (work email 1 credit, mobile phone 10 credits, personal "
                  "email 3 credits) — matches the brief's '~11 credits/contact' estimate for email+phone exactly.")
    lines.append("- No `Sub-Account-Id` header (or equivalent) appears in FullEnrich's public v2 API docs — see the "
                  "open question above.")
    lines.append("- Email status is a 5-way enum (DELIVERABLE, HIGH_PROBABILITY, CATCH_ALL, INVALID, INVALID_DOMAIN), "
                  "coarser-mapped here to the brief's 3-way verified/catch_all/not_found (HIGH_PROBABILITY -> "
                  "catch_all, conservatively, since it's not SMTP-confirmed) — raw status preserved in `enrichments.raw`.")
    lines.append("")
    lines.append("## Methodology caveat")
    lines.append("Prospects are organizations, not individuals — there's no pre-existing 'the contact' per prospect. "
                  "Each submitted contact was picked from the org's own latest 990 filing officers list "
                  "(`filings.officers`), preferring is_officer=true entries and president/executive-director/CEO "
                  "titles. Many of these are volunteer board officers (President, Treasurer, Secretary), not paid "
                  "staff — a real limitation on how useful a 'match' actually is for outreach, independent of "
                  "FullEnrich's own accuracy.")
    lines.append("")
    lines.append(f"## Manual spot-check checklist ({min(10, matched_n)} of {matched_n} matches)")
    if matched_n == 0:
        lines.append("No matches to spot-check.")
    else:
        for i, m in enumerate(result.matched[:10], start=1):
            lines.append(f"- [ ] {i}. **{m['org_name']}** (EIN {m['ein']}) — {m['contact_name']}, {m['contact_title'] or 'title unknown'}")
            lines.append(f"      Email: {m['email'] or 'none'} ({m['email_status'] or 'n/a'}, raw status {m['fullenrich_status'] or 'n/a'})")
            lines.append(f"      Phone: {m['phone'] or 'none'}")
            lines.append("      - [ ] Person still holds this title / still at this org (check org website or LinkedIn)")
            lines.append("      - [ ] Email domain matches the org's actual domain (not a generic/parked domain)")
            lines.append("      - [ ] If phone present, plausible for this person/org (area code, not obviously wrong)")
    lines.append("")
    lines.append("## Adoption threshold (brief §5.5)")
    lines.append("Target: >=60% match rate, >=70% of matches verified, cost per usable contact comfortably inside budget.")
    match_ok = match_rate >= 0.60
    verified_ok = verified_rate is not None and verified_rate >= 0.70
    lines.append(f"- Match rate {match_rate:.0%} {'meets' if match_ok else 'does NOT meet'} the >=60% bar.")
    lines.append(
        f"- Verified rate {f'{verified_rate:.0%}' if verified_rate is not None else 'n/a'} "
        f"{'meets' if verified_ok else 'does NOT meet'} the >=70% bar."
    )
    lines.append(
        "- Treat this as **internal validation only** pending reseller-terms reconciliation with FullEnrich "
        "(per STATUS.md open item) — not yet cleared for delivering enrichment results to ARCHITECT."
    )
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="E1 FullEnrich validation spike — standalone, one-time invocation only."
    )
    parser.add_argument("--client-id", type=int, default=2)
    parser.add_argument("--limit", type=int, default=None, help="Cap the number of approved prospects submitted")
    parser.add_argument("--poll-interval", type=float, default=10.0)
    parser.add_argument("--poll-timeout", type=float, default=600.0)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build the request payload and report skip/candidate counts without calling FullEnrich or spending credits",
    )
    parser.add_argument(
        "--replay-enrichment-id",
        type=str,
        default=None,
        help="Re-fetch and re-process an already-completed FullEnrich batch instead of submitting a new one (no credits spent)",
    )
    parser.add_argument(
        "--report-dir",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "reports",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    database_url = os.environ["DATABASE_URL"]
    api_key = os.environ["FULLENRICH_API_KEY"]

    result = run_spike(
        args.client_id,
        database_url,
        api_key,
        limit=args.limit,
        poll_interval_s=args.poll_interval,
        poll_timeout_s=args.poll_timeout,
        dry_run=args.dry_run,
        replay_enrichment_id=args.replay_enrichment_id,
    )

    report = _format_report(args.client_id, result)
    print(report)

    if not args.dry_run:
        args.report_dir.mkdir(parents=True, exist_ok=True)
        report_path = args.report_dir / f"e1_spike_{datetime.now(UTC):%Y%m%d}.md"
        report_path.write_text(report, encoding="utf-8")
        logger.info("REPORT_WRITTEN path=%s", report_path)

    return 0


if __name__ == "__main__":
    sys.exit(main())
