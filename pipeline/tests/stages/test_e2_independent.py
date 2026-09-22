"""Independent adversarial E2 coverage, separate from the implementation suite."""

from __future__ import annotations

import pytest

from discovery.clients.enrichment_provider import PollOutcome, PollResult
from discovery.clients.fullenrich_adapter import parse_contact_result
from discovery.stages.enrich import check_enrichment, select_primary_officer


def _provider_response(email: str, status: str) -> dict[str, object]:
    return {"contact_info": {"most_probable_work_email": {"email": email, "status": status}}}


@pytest.mark.parametrize(
    ("officers", "expected_name"),
    [
        (
            [
                {"name": "Pat Board", "title": "Board President", "is_officer": True},
                {"name": "Chris Chief", "title": "CEO", "is_officer": True},
                {"name": "Alex Former", "title": "CFO (THRU JUNE 2025)", "is_officer": True},
                {"name": "Morgan Finance", "title": "CFO", "is_officer": True},
            ],
            "Chris Chief",
        ),
        (
            [
                {"name": "Pat Board", "title": "Board President", "is_officer": True},
                {"name": "Sam Trustee", "title": "Trustee", "is_officer": True},
            ],
            "Pat Board",
        ),
        (
            [
                {"name": "Taylor Former", "title": "CFO - FORMER", "is_officer": True},
                {"name": "Jordan Current", "title": "CFO", "is_officer": True},
                {"name": "Casey Current", "title": "CFO", "is_officer": True},
            ],
            "Jordan Current",
        ),
    ],
)
def test_select_primary_officer_handles_adversarial_current_and_departed_lists(
    officers: list[dict[str, object]], expected_name: str
) -> None:
    """A board-only filing is usable, but current staff must beat departed peers."""
    selected = select_primary_officer(officers)
    assert selected is not None
    assert selected["name"] == expected_name


@pytest.mark.parametrize(
    ("address", "provider_status", "expected_status"),
    [
        ("person@gmail.com", "DELIVERABLE", "verified"),
        ("person@yahoo.com", "HIGH_PROBABILITY", "catch_all"),
        ("person@hotmail.com", "DELIVERABLE", "verified"),
        ("person@outlook.com", "CATCH_ALL", "catch_all"),
    ],
)
def test_personal_domains_never_become_stale_against_an_organization_domain(
    address: str, provider_status: str, expected_status: str
) -> None:
    result = parse_contact_result(_provider_response(address, provider_status), "nonprofit.example")
    assert result.email_status == expected_status
    assert result.stale_detail is None


def test_organizational_domain_mismatch_becomes_stale_with_domain_detail() -> None:
    result = parse_contact_result(_provider_response("leader@former-employer.org", "DELIVERABLE"), "nonprofit.example")
    assert result.email_status == "stale_likely_moved"
    assert result.stale_detail == "former-employer.org"


def test_missing_organization_domain_trusts_provider_status() -> None:
    result = parse_contact_result(_provider_response("leader@former-employer.org", "DELIVERABLE"), None)
    assert result.email_status == "verified"
    assert result.stale_detail is None


class _NeverResolvingProvider:
    name = "never-resolving"

    def __init__(self) -> None:
        self.poll_calls = 0

    def submit(self, candidate: object) -> str:
        raise AssertionError("check_enrichment must not submit")

    def poll(self, job_id: str, org_domain: str | None) -> PollResult:
        self.poll_calls += 1
        return PollResult(outcome=PollOutcome.PENDING)


def test_poll_window_stays_bounded_when_provider_never_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _NeverResolvingProvider()
    sleeps: list[float] = []
    monkeypatch.setattr("discovery.stages.enrich.time.sleep", sleeps.append)

    attempt = check_enrichment(provider, "still-running", "nonprofit.example", poll_attempts=3, poll_interval_s=0.1)

    assert attempt.status == "pending"
    assert attempt.job_id == "still-running"
    assert provider.poll_calls == 3
    assert sleeps == [0.1, 0.1]