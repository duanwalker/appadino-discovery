from typing import ClassVar

from discovery.clients import fullenrich_adapter
from discovery.clients.enrichment_provider import EnrichmentCandidate, PollOutcome
from discovery.clients.fullenrich_adapter import (
    FullEnrichProvider,
    classify_domain,
    domain_from_website,
    parse_contact_result,
)


def _contact_result(email: str | None, fe_status: str | None, phone: str | None = None, full_name: str | None = None) -> dict:
    contact_info: dict = {}
    if email:
        contact_info["most_probable_work_email"] = {"email": email, "status": fe_status}
    if phone:
        contact_info["most_probable_phone"] = {"number": phone, "ownership_match_confidence": 88}
    result: dict = {"contact_info": contact_info}
    if full_name:
        result["profile"] = {"full_name": full_name, "linkedin_url": "https://linkedin.com/in/x"}
    return result


class TestDomainFromWebsite:
    def test_strips_protocol_and_www(self) -> None:
        assert domain_from_website("https://www.example.org/about") == "example.org"

    def test_none_when_blank(self) -> None:
        assert domain_from_website(None) is None
        assert domain_from_website("") is None


class TestClassifyDomain:
    def test_personal_domain_never_flagged_stale(self) -> None:
        # E1's false-positive mode: a board volunteer's real, current gmail address
        # must not be read as "moved" just because it isn't the org's own domain.
        status, detail = classify_domain("gmail.com", "org.org")
        assert (status, detail) == (None, None)

    def test_matching_domain_not_flagged(self) -> None:
        status, detail = classify_domain("org.org", "org.org")
        assert (status, detail) == (None, None)

    def test_mismatched_non_personal_domain_flagged_stale(self) -> None:
        # The E1 spot-check finding: Day One's officer resolved to sheppardpratt.org.
        status, detail = classify_domain("sheppardpratt.org", "dayonemaine.org")
        assert status == "stale_likely_moved"
        assert detail == "sheppardpratt.org"

    def test_no_org_domain_never_flagged(self) -> None:
        status, detail = classify_domain("sheppardpratt.org", None)
        assert (status, detail) == (None, None)


class TestParseContactResult:
    def test_verified_matching_domain(self) -> None:
        result = parse_contact_result(_contact_result("jsmith@org.org", "DELIVERABLE"), "org.org")
        assert result.email_status == "verified"
        assert result.stale_detail is None
        assert result.provider_confidence == 95
        assert result.credits_spent == 1

    def test_mismatched_domain_overrides_status_despite_fullenrich_verified(self) -> None:
        result = parse_contact_result(_contact_result("gbowers@sheppardpratt.org", "DELIVERABLE"), "dayonemaine.org")
        assert result.email_status == "stale_likely_moved"
        assert result.stale_detail == "sheppardpratt.org"

    def test_personal_domain_not_overridden_even_if_mismatched(self) -> None:
        result = parse_contact_result(_contact_result("jsmith@gmail.com", "HIGH_PROBABILITY"), "org.org")
        assert result.email_status == "catch_all"
        assert result.stale_detail is None

    def test_no_email_no_phone_is_not_found(self) -> None:
        result = parse_contact_result({"contact_info": {}}, "org.org")
        assert result.email_status == "not_found"
        assert result.provider_confidence == 0
        assert result.credits_spent == 0

    def test_phone_confidence_preferred_over_email_heuristic(self) -> None:
        result = parse_contact_result(
            _contact_result("jsmith@org.org", "CATCH_ALL", phone="+12075550100"), "org.org"
        )
        assert result.provider_confidence == 88  # phone's ownership_match_confidence, not the email heuristic (50)
        assert result.credits_spent == 11  # 1 (email) + 10 (phone)

    def test_prefers_resolved_profile_name(self) -> None:
        result = parse_contact_result(
            _contact_result("slundin@org.org", "DELIVERABLE", full_name="Sarah Lundin"), "org.org"
        )
        assert result.contact_name == "Sarah Lundin"


class _FakeFullEnrichModule:
    """Swapped in place of discovery.clients.fullenrich inside the adapter under
    test, so no real HTTP call is made."""

    TERMINAL_STATUSES: ClassVar[set[str]] = {"FINISHED", "CANCELED", "CREDITS_INSUFFICIENT", "RATE_LIMIT", "UNKNOWN"}

    def __init__(self) -> None:
        self.start_calls: list[tuple] = []
        self.get_calls: list[str] = []
        self.next_get_response: dict = {"status": "PENDING"}

    def start_bulk_enrichment(self, api_key, name, contacts, client=None):
        self.start_calls.append((api_key, name, contacts))
        return "job-123"

    def get_bulk_enrichment(self, api_key, enrichment_id, client=None):
        self.get_calls.append(enrichment_id)
        return self.next_get_response


class TestFullEnrichProvider:
    def test_submit_builds_expected_payload(self, monkeypatch) -> None:
        fake = _FakeFullEnrichModule()
        monkeypatch.setattr(fullenrich_adapter, "fullenrich", fake)
        provider = FullEnrichProvider(api_key="key-1")
        candidate = EnrichmentCandidate(ein="123456789", first_name="Jane", last_name="Smith", title="ED", domain="org.org")

        job_id = provider.submit(candidate)

        assert job_id == "job-123"
        api_key, _name, contacts = fake.start_calls[0]
        assert api_key == "key-1"
        assert contacts == [
            {
                "firstname": "Jane",
                "lastname": "Smith",
                "domain": "org.org",
                "enrich_fields": fullenrich_adapter.ENRICH_FIELDS,
                "custom": {"ein": "123456789"},
            }
        ]

    def test_poll_pending_when_not_terminal(self, monkeypatch) -> None:
        fake = _FakeFullEnrichModule()
        fake.next_get_response = {"status": "PROCESSING"}
        monkeypatch.setattr(fullenrich_adapter, "fullenrich", fake)
        provider = FullEnrichProvider(api_key="key-1")

        poll_result = provider.poll("job-123", "org.org")

        assert poll_result.outcome is PollOutcome.PENDING

    def test_poll_done_with_match(self, monkeypatch) -> None:
        fake = _FakeFullEnrichModule()
        fake.next_get_response = {
            "status": "FINISHED",
            "data": [_contact_result("jsmith@org.org", "DELIVERABLE")],
        }
        monkeypatch.setattr(fullenrich_adapter, "fullenrich", fake)
        provider = FullEnrichProvider(api_key="key-1")

        poll_result = provider.poll("job-123", "org.org")

        assert poll_result.outcome is PollOutcome.DONE
        assert poll_result.result.email_status == "verified"

    def test_poll_done_with_no_match(self, monkeypatch) -> None:
        fake = _FakeFullEnrichModule()
        fake.next_get_response = {"status": "FINISHED", "data": []}
        monkeypatch.setattr(fullenrich_adapter, "fullenrich", fake)
        provider = FullEnrichProvider(api_key="key-1")

        poll_result = provider.poll("job-123", "org.org")

        assert poll_result.outcome is PollOutcome.DONE
        assert poll_result.result.email_status == "not_found"
        assert poll_result.result.provider_confidence == 0
