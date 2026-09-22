from datetime import UTC, datetime

from discovery.clients.enrichment_provider import (
    EnrichmentCandidate,
    EnrichmentResult,
    PollOutcome,
    PollResult,
)
from discovery.stages.enrich import (
    build_candidate,
    can_enrich,
    check_enrichment,
    compute_retention_expires_at,
    select_primary_officer,
    split_officer_name,
    start_enrichment,
)


class _StubProvider:
    """Records calls so guard-failure tests can assert the provider was never hit —
    hard rule 8 must block before any network call, not just before persisting."""

    def __init__(self, poll_outcomes: list[PollResult] | None = None, job_id: str = "job-1") -> None:
        self.poll_outcomes = poll_outcomes or []
        self.job_id = job_id
        self.submit_calls: list[EnrichmentCandidate] = []
        self.poll_calls: list[tuple[str, str | None]] = []
        self.name = "stub"

    def submit(self, candidate: EnrichmentCandidate) -> str:
        self.submit_calls.append(candidate)
        return self.job_id

    def poll(self, job_id: str, org_domain: str | None) -> PollResult:
        self.poll_calls.append((job_id, org_domain))
        idx = len(self.poll_calls) - 1
        if idx < len(self.poll_outcomes):
            return self.poll_outcomes[idx]
        return self.poll_outcomes[-1]


def _result(email_status: str = "verified") -> EnrichmentResult:
    return EnrichmentResult(
        contact_name="Jane Smith",
        contact_title="Executive Director",
        email="jsmith@org.org",
        email_status=email_status,
        stale_detail=None,
        phone=None,
        linkedin_url=None,
        provider_confidence=95,
        credits_spent=1,
        raw={},
    )


class TestCanEnrich:
    def test_blocks_when_not_approved(self) -> None:
        allowed, reason = can_enrich("new", True, "sub-1")
        assert allowed is False
        assert reason == "prospect is not approved"

    def test_blocks_when_tenant_disabled(self) -> None:
        allowed, reason = can_enrich("approved", False, "sub-1")
        assert allowed is False
        assert reason == "enrichment is not enabled for this tenant"

    def test_blocks_when_subaccount_unset(self) -> None:
        allowed, reason = can_enrich("approved", True, None)
        assert allowed is False
        assert reason == "tenant has no fullenrich_subaccount_id configured"

    def test_blocks_when_subaccount_empty_string(self) -> None:
        allowed, _reason = can_enrich("approved", True, "")
        assert allowed is False

    def test_allows_when_all_three_hold(self) -> None:
        allowed, reason = can_enrich("approved", True, "sub-1")
        assert allowed is True
        assert reason is None


class TestSelectPrimaryOfficer:
    def test_none_when_no_officers(self) -> None:
        assert select_primary_officer(None) is None
        assert select_primary_officer([]) is None

    def test_prefers_is_officer_true(self) -> None:
        officers = [
            {"name": "Board Member", "title": "Director", "is_officer": False},
            {"name": "Jane Smith", "title": "Treasurer", "is_officer": True},
        ]
        assert select_primary_officer(officers)["name"] == "Jane Smith"

    def test_prefers_executive_title_within_officer_pool(self) -> None:
        officers = [
            {"name": "Treasurer Tom", "title": "Treasurer", "is_officer": True},
            {"name": "Jane Smith", "title": "Executive Director", "is_officer": True},
        ]
        assert select_primary_officer(officers)["name"] == "Jane Smith"

    def test_falls_back_to_first_officer_when_no_title_matches(self) -> None:
        officers = [{"name": "A", "title": "Secretary", "is_officer": True}]
        assert select_primary_officer(officers)["name"] == "A"

    def test_falls_back_to_all_officers_when_none_flagged_is_officer(self) -> None:
        officers = [{"name": "A", "title": "Board Member", "is_officer": False}]
        assert select_primary_officer(officers)["name"] == "A"

    def test_ceo_preferred_over_board_president(self) -> None:
        # Real data, Day One (EIN 010322532), 2024 filing: a board president and a
        # CEO both listed as officers. The CEO (paid staff) should win, not the
        # board president — the opposite of what a naive reuse of triggers.py's
        # executive-title keyword order (which ranks "president" above "chief
        # executive"/doesn't recognize "CEO" at all) produced.
        officers = [
            {"name": "Katie Grant", "title": "PRESIDENT", "is_officer": True},
            {"name": "Cassandra Humphrey", "title": "CEO", "is_officer": True},
        ]
        assert select_primary_officer(officers)["name"] == "Cassandra Humphrey"

    def test_current_officer_preferred_over_departed_even_with_worse_title_rank(self) -> None:
        # Real data, same org: a departed CEO ("CEO (THRU MAR 2024)") and a current
        # board president. Even though CEO outranks PRESIDENT in the keyword list,
        # a departed officer must never outrank a current one.
        officers = [
            {"name": "Gregory Bowers", "title": "CEO (THRU MAR 2024)", "is_officer": True},
            {"name": "Katie Grant", "title": "PRESIDENT", "is_officer": True},
        ]
        assert select_primary_officer(officers)["name"] == "Katie Grant"

    def test_current_ceo_preferred_over_departed_ceo(self) -> None:
        officers = [
            {"name": "Gregory Bowers", "title": "CEO (THRU MAR 2024)", "is_officer": True},
            {"name": "Cassandra Humphrey", "title": "CEO", "is_officer": True},
        ]
        assert select_primary_officer(officers)["name"] == "Cassandra Humphrey"

    def test_departed_officer_still_selectable_as_last_resort(self) -> None:
        officers = [{"name": "Gregory Bowers", "title": "CEO (THRU MAR 2024)", "is_officer": True}]
        assert select_primary_officer(officers)["name"] == "Gregory Bowers"


class TestSplitOfficerName:
    def test_simple_two_part_name(self) -> None:
        assert split_officer_name("Jane Smith") == ("Jane", "Smith")

    def test_strips_trailing_thru_note(self) -> None:
        assert split_officer_name("Gregory Bowers (THRU MAR 2024)") == ("Gregory", "Bowers")

    def test_strips_trailing_termed_note(self) -> None:
        assert split_officer_name("Gregory Bowers TERMED 3/2024") == ("Gregory", "Bowers")

    def test_single_word_name_unusable(self) -> None:
        assert split_officer_name("Madonna") is None

    def test_multi_word_last_name_joined(self) -> None:
        assert split_officer_name("Mary Anne Smith Jones") == ("Mary", "Anne Smith Jones")


class TestBuildCandidate:
    def test_none_when_no_officer(self) -> None:
        assert build_candidate("123", "org.org", None) is None

    def test_none_when_no_domain(self) -> None:
        officer = {"name": "Jane Smith", "title": "Executive Director"}
        assert build_candidate("123", None, officer) is None

    def test_none_when_name_unparseable(self) -> None:
        officer = {"name": "Madonna", "title": "Executive Director"}
        assert build_candidate("123", "org.org", officer) is None

    def test_builds_candidate_from_valid_officer(self) -> None:
        officer = {"name": "Jane Smith", "title": "Executive Director"}
        candidate = build_candidate("123456789", "org.org", officer)
        assert candidate == EnrichmentCandidate(
            ein="123456789", first_name="Jane", last_name="Smith", title="Executive Director", domain="org.org"
        )


class TestStartEnrichment:
    def test_blocked_never_calls_provider(self) -> None:
        provider = _StubProvider()
        officer = {"name": "Jane Smith", "title": "Executive Director"}
        attempt = start_enrichment(
            provider, "123", "org.org", officer,
            status="new", enrichment_enabled=True, fullenrich_subaccount_id="sub-1",
            poll_attempts=1, poll_interval_s=0,
        )
        assert attempt.status == "blocked"
        assert attempt.reason == "prospect is not approved"
        assert provider.submit_calls == []
        assert provider.poll_calls == []

    def test_no_candidate_never_calls_provider(self) -> None:
        provider = _StubProvider()
        attempt = start_enrichment(
            provider, "123", "org.org", None,
            status="approved", enrichment_enabled=True, fullenrich_subaccount_id="sub-1",
            poll_attempts=1, poll_interval_s=0,
        )
        assert attempt.status == "no_candidate"
        assert provider.submit_calls == []

    def test_resolves_done_within_budget(self) -> None:
        provider = _StubProvider(poll_outcomes=[PollResult(outcome=PollOutcome.DONE, result=_result())])
        officer = {"name": "Jane Smith", "title": "Executive Director"}
        attempt = start_enrichment(
            provider, "123", "org.org", officer,
            status="approved", enrichment_enabled=True, fullenrich_subaccount_id="sub-1",
            poll_attempts=3, poll_interval_s=0,
        )
        assert attempt.status == "done"
        assert attempt.result is not None
        assert attempt.result.email_status == "verified"
        assert len(provider.submit_calls) == 1

    def test_pending_after_budget_exhausted(self) -> None:
        provider = _StubProvider(poll_outcomes=[PollResult(outcome=PollOutcome.PENDING)])
        officer = {"name": "Jane Smith", "title": "Executive Director"}
        attempt = start_enrichment(
            provider, "123", "org.org", officer,
            status="approved", enrichment_enabled=True, fullenrich_subaccount_id="sub-1",
            poll_attempts=2, poll_interval_s=0,
        )
        assert attempt.status == "pending"
        assert attempt.job_id == "job-1"
        assert len(provider.poll_calls) == 2


class TestCheckEnrichment:
    def test_resumes_and_resolves(self) -> None:
        provider = _StubProvider(
            poll_outcomes=[
                PollResult(outcome=PollOutcome.PENDING),
                PollResult(outcome=PollOutcome.DONE, result=_result()),
            ]
        )
        attempt = check_enrichment(provider, "job-1", "org.org", poll_attempts=2, poll_interval_s=0)
        assert attempt.status == "done"
        assert attempt.job_id == "job-1"

    def test_still_pending(self) -> None:
        provider = _StubProvider(poll_outcomes=[PollResult(outcome=PollOutcome.PENDING)])
        attempt = check_enrichment(provider, "job-1", "org.org", poll_attempts=1, poll_interval_s=0)
        assert attempt.status == "pending"


class TestComputeRetentionExpiresAt:
    def test_adds_90_days(self) -> None:
        completed = datetime(2026, 9, 19, tzinfo=UTC)
        expires = compute_retention_expires_at(completed)
        assert (expires - completed).days == 90
