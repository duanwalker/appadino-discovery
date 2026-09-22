from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from discovery.db import Base


class Enrichment(Base):
    """One contact-enrichment result per (client_id, ein), from a provider waterfall
    (currently FullEnrich only) — §5.5. In steady state this is populated only for
    status='approved' prospects, only when icp_configs.config.enrichment_enabled is
    true (§9 rule 8); the E1 validation spike (pipeline/scripts/e1_fullenrich_spike.py)
    is the documented one-time exception, since its purpose is to produce the data
    that enrichment_enabled go/no-go decision is made from — see that script's
    docstring and STATUS.md for the E1 write-up.

    email_status mirrors the brief's three-way bucket (verified|catch_all|not_found),
    which is coarser than FullEnrich's own v2 API status enum (DELIVERABLE,
    HIGH_PROBABILITY, CATCH_ALL, INVALID, INVALID_DOMAIN) — see the spike script for
    the mapping used and why it's a judgment call worth reconciling with FullEnrich.
    """

    __tablename__ = "enrichments"
    __table_args__ = (
        CheckConstraint(
            "email_status IS NULL OR email_status IN "
            "('verified', 'catch_all', 'not_found', 'stale_likely_moved')",
            name="ck_enrichments_email_status",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"))
    ein: Mapped[str] = mapped_column(String(9), index=True)
    prospect_id: Mapped[int | None] = mapped_column(ForeignKey("prospects.id"))
    provider: Mapped[str] = mapped_column(String(50))
    contact_name: Mapped[str | None] = mapped_column(String(255))
    contact_title: Mapped[str | None] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(255))
    # E2 addition: 'stale_likely_moved' (distinct from 'not_found') covers a
    # match FullEnrich reports as findable/verified but whose email domain
    # doesn't belong to the org being prospected — a real signal from E1's
    # spot-check (a Day One officer resolving to a sheppardpratt.org address),
    # not a failure state. See discovery.clients.enrichment_adapter for the
    # domain-comparison logic that sets this (with a personal-email-domain
    # carve-out, so a board volunteer's gmail.com address isn't misread as
    # "moved").
    email_status: Mapped[str | None] = mapped_column(String(20))
    # E2 addition: the mismatched domain/company that triggered
    # email_status='stale_likely_moved', so the dashboard can show *why*,
    # not just the bare label. NULL unless email_status is stale_likely_moved.
    stale_detail: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(String(50))
    linkedin_url: Mapped[str | None] = mapped_column(String(500))
    # 0-100. FullEnrich's v2 API exposes a native numeric confidence only for phone
    # (phone.ownership_match_confidence); emails only get a status enum, no number.
    # Hard rule 3 (§9) requires every record here to carry a confidence value, so a
    # matched contact with no phone gets a heuristic score derived from its email
    # status instead (see pipeline/scripts/e1_fullenrich_spike.py's
    # EMAIL_STATUS_CONFIDENCE) — never left NULL for an inserted row.
    provider_confidence: Mapped[int | None] = mapped_column(Integer)
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    credits_spent: Mapped[int | None] = mapped_column(Integer)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # completed_at + 90 days (FullEnrich reseller terms, §5.5) — dashboard flags
    # expired rows; not enforced/purged by any job yet (Phase 2).
    retention_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
