"""§5.5/E2 provider interface — FullEnrich first, swappable later. Written to the
async shape E1 actually confirmed (POST returns a job id, then poll or webhook for
results) rather than assuming a synchronous provider that would need faking here.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol


@dataclass
class EnrichmentCandidate:
    """One person to enrich, resolved from a prospect's latest 990 officers list
    before any provider call — providers never see the prospect/EIN directly, only
    what's needed to submit a request."""

    ein: str
    first_name: str
    last_name: str
    title: str | None
    domain: str | None  # the org's own website domain, for stale-detection (see adapter)


class PollOutcome(Enum):
    PENDING = "pending"
    DONE = "done"


@dataclass
class EnrichmentResult:
    """Provider-agnostic result, already mapped onto enrichments' columns (§3) so
    callers never touch a provider-specific response shape."""

    contact_name: str | None
    contact_title: str | None
    email: str | None
    email_status: str | None  # verified|catch_all|not_found|stale_likely_moved
    stale_detail: str | None
    phone: str | None
    linkedin_url: str | None
    provider_confidence: int  # hard rule 3 (§9): never null on a written row
    credits_spent: int
    raw: dict[str, Any]


@dataclass
class PollResult:
    outcome: PollOutcome
    result: EnrichmentResult | None = None  # set only when outcome is DONE


class EnrichmentProvider(Protocol):
    """Implementations: discovery.clients.fullenrich_adapter.FullEnrichProvider."""

    name: str

    def submit(self, candidate: EnrichmentCandidate) -> str:
        """Starts enrichment for one contact. Returns a provider job id."""
        ...

    def poll(self, job_id: str, org_domain: str | None) -> PollResult:
        """Single, non-blocking status check — callers own their own wait/retry
        budget (see discovery.stages.enrich for the bounded-wait policy)."""
        ...
