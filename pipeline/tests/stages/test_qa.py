"""§7 QA job: the automated stand-in for "Duan reads the output"."""

from discovery.stages.qa import compute_mismatch_rate, extract_claims, sample_claims, verify_claim


class TestExtractClaims:
    def test_extracts_values_signal_claims_with_citation(self) -> None:
        values_signals = {
            "mission_language": {"score": 60, "rationale": "x", "citation": "mission_text", "needs_human_verification": False},
            "leadership_composition": {"score": None, "rationale": "x", "citation": None, "needs_human_verification": True},
        }
        claims = extract_claims("123456789", values_signals, {"criteria": {}})
        assert len(claims) == 1
        assert claims[0]["field"] == "values_signals.mission_language"

    def test_extracts_alignment_claims_with_citation(self) -> None:
        alignment = {
            "criteria": {
                "mission_alignment": {"met": True, "rationale": "x", "citation": "mission_text"},
                "case_study_potential": {"met": False, "rationale": "x", "citation": None},
            }
        }
        claims = extract_claims("123456789", {}, alignment)
        assert len(claims) == 1
        assert claims[0]["field"] == "alignment.mission_alignment"

    def test_excludes_priority_tier_metro_deterministic_criterion(self) -> None:
        """Computed in Python, not by the model — sampling it wouldn't test the LLM."""
        alignment = {"criteria": {"priority_tier_metro": {"met": True, "rationale": "geography match", "citation": None}}}
        claims = extract_claims("123456789", {}, alignment)
        assert claims == []


class TestSampleClaims:
    def test_returns_all_when_fewer_than_sample_size(self) -> None:
        claims = [{"ein": str(i)} for i in range(5)]
        assert len(sample_claims(claims, sample_size=20)) == 5

    def test_returns_exactly_sample_size_when_more_available(self) -> None:
        claims = [{"ein": str(i)} for i in range(50)]
        sampled = sample_claims(claims, sample_size=20, seed=42)
        assert len(sampled) == 20

    def test_seeded_sample_is_deterministic(self) -> None:
        claims = [{"ein": str(i)} for i in range(50)]
        a = sample_claims(claims, sample_size=20, seed=42)
        b = sample_claims(claims, sample_size=20, seed=42)
        assert a == b


class TestVerifyClaim:
    def test_numeric_citation_matches_actual_value(self) -> None:
        citation = "govt_pct 0.59 from 990 filing data"
        org_context = {"govt_pct": 0.59}
        assert verify_claim(citation, org_context) == "match"

    def test_numeric_citation_within_tolerance_matches(self) -> None:
        citation = "fundraising_spend_ratio 0.051 from 990 filing data"
        org_context = {"fundraising_spend_ratio": 0.05}
        assert verify_claim(citation, org_context) == "match"

    def test_numeric_citation_mismatches_when_value_differs(self) -> None:
        citation = "govt_pct 0.90 from 990 filing data"
        org_context = {"govt_pct": 0.10}
        assert verify_claim(citation, org_context) == "mismatch"

    def test_multiple_numeric_pointers_in_one_citation_all_match(self) -> None:
        """Real Sonnet output cites several revenue-composition figures in one
        citation — only the first recognized pointer is checked (§ design note), but
        it must still resolve correctly rather than mismatching on the first field."""
        citation = "govt_pct 0.0702, program_pct 0.6918, contributions_pct 0.2387 from 990 filing data"
        org_context = {"govt_pct": 0.0702, "program_pct": 0.6918, "contributions_pct": 0.2387}
        assert verify_claim(citation, org_context) == "match"

    def test_boolean_citation_matches_actual_value(self) -> None:
        citation = "significant_change_ind false, revenue_trend growth from 990 filing data"
        org_context = {"significant_change_ind": False, "revenue_trend": "growth"}
        assert verify_claim(citation, org_context) == "match"

    def test_boolean_citation_mismatches_when_value_differs(self) -> None:
        citation = "significant_change_ind true from 990 filing data"
        org_context = {"significant_change_ind": False}
        assert verify_claim(citation, org_context) == "mismatch"

    def test_categorical_citation_matches_quoted_value(self) -> None:
        citation = "revenue_trend 'stable' and significant_change_ind false from 990 filing data"
        org_context = {"revenue_trend": "stable", "significant_change_ind": False}
        assert verify_claim(citation, org_context) == "match"

    def test_categorical_citation_matches_colon_separated_value(self) -> None:
        citation = "revenue_trend: growth; significant_change_ind: false"
        org_context = {"revenue_trend": "growth", "significant_change_ind": False}
        assert verify_claim(citation, org_context) == "match"

    def test_categorical_citation_null_matches_actual_none(self) -> None:
        citation = "significant_change_ind false, revenue_trend null from 990 filing data"
        org_context = {"significant_change_ind": False, "revenue_trend": None}
        assert verify_claim(citation, org_context) == "match"

    def test_categorical_citation_mismatches_wrong_category(self) -> None:
        citation = "revenue_trend growth from 990 filing data"
        org_context = {"revenue_trend": "decline"}
        assert verify_claim(citation, org_context) == "mismatch"

    def test_numeric_citation_mismatches_when_field_actually_null(self) -> None:
        """Citing a specific figure for a field we don't actually have is a fabrication."""
        citation = "govt_pct 0.59 from 990 filing data"
        org_context = {"govt_pct": None}
        assert verify_claim(citation, org_context) == "mismatch"

    def test_mission_text_citation_matches_when_present(self) -> None:
        citation = "mission_text: 'serves the community'"
        org_context = {"mission_text": "We serve the community with pride."}
        assert verify_claim(citation, org_context) == "match"

    def test_mission_text_citation_mismatches_when_actually_null(self) -> None:
        citation = "mission_text"
        org_context = {"mission_text": None}
        assert verify_claim(citation, org_context) == "mismatch"

    def test_program_text_citation_matches_when_present(self) -> None:
        citation = "program_text[0].desc"
        org_context = {"program_text": [{"desc": "Youth mentoring program"}]}
        assert verify_claim(citation, org_context) == "match"

    def test_program_text_citation_mismatches_when_empty(self) -> None:
        citation = "program_text[0].desc"
        org_context = {"program_text": []}
        assert verify_claim(citation, org_context) == "mismatch"

    def test_unrecognized_citation_with_no_source_text_at_all_is_unverifiable(self) -> None:
        citation = "the organization's website states this explicitly"
        org_context = {"mission_text": None, "program_text": None}
        assert verify_claim(citation, org_context) == "unverifiable"

    def test_quoted_citation_matching_actual_mission_text_is_match(self) -> None:
        """No bare field-name pointer — the citation is itself a close paraphrase of
        the org's real stored mission text (this is what Sonnet actually produces in
        practice, not just bare pointers)."""
        citation = "THE MISSION OF THE BRICK STORE MUSEUM IS TO IGNITE PERSONAL CONNECTIONS TO HISTORY"
        org_context = {
            "mission_text": "the mission of the brick store museum is to ignite personal connections to history and culture",
            "program_text": [],
        }
        assert verify_claim(citation, org_context) == "match"

    def test_quoted_citation_not_found_in_corpus_is_mismatch(self) -> None:
        citation = "This organization provides free legal aid to indigent defendants statewide"
        org_context = {"mission_text": "We run a summer camp for children.", "program_text": []}
        assert verify_claim(citation, org_context) == "mismatch"

    def test_short_unrelated_citation_against_present_corpus_is_mismatch_not_unverifiable(self) -> None:
        citation = "the organization's website states this explicitly"
        org_context = {"mission_text": "something", "program_text": []}
        assert verify_claim(citation, org_context) == "mismatch"


class TestComputeMismatchRate:
    def test_all_match_is_zero(self) -> None:
        assert compute_mismatch_rate(["match", "match", "match"]) == 0.0

    def test_all_mismatch_is_one(self) -> None:
        assert compute_mismatch_rate(["mismatch", "mismatch"]) == 1.0

    def test_mixed_rate_computed_correctly(self) -> None:
        assert compute_mismatch_rate(["match", "match", "match", "mismatch"]) == 0.25

    def test_unverifiable_excluded_from_denominator(self) -> None:
        # 1 mismatch out of 2 *verified* claims — unverifiable doesn't count either way
        assert compute_mismatch_rate(["match", "mismatch", "unverifiable", "unverifiable"]) == 0.5

    def test_no_verified_claims_returns_none(self) -> None:
        assert compute_mismatch_rate(["unverifiable", "unverifiable"]) is None

    def test_empty_list_returns_none(self) -> None:
        assert compute_mismatch_rate([]) is None
