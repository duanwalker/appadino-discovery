"""§9 hard rules as test cases (per the kickoff prompt: "The hard rules in §9 are test
cases first"). These test the code-level enforcement in discovery.stages.score —
defense in depth beyond the system prompt, which an LLM response can't bypass."""

from discovery.stages.score import (
    CAPACITY_NOTE,
    assemble_alignment,
    build_org_context,
    clamp_pre_score,
    compute_capacity,
    compute_priority_tier_metro,
    compute_soft_flags,
    enforce_hard_rules,
)


class TestRule1LeadershipComposition:
    """§9 rule 1: never infer race/ethnicity/gender from names or photos — cite
    published self-description only, or emit needs_human_verification."""

    def test_score_without_citation_forced_to_needs_human_verification(self) -> None:
        values_signals = {
            "leadership_composition": {
                "score": 80,
                "rationale": "The leadership team appears diverse based on names.",
                "citation": None,
                "needs_human_verification": False,
            }
        }
        sanitized, _, violations = enforce_hard_rules(values_signals, {})
        assert sanitized["leadership_composition"]["score"] is None
        assert sanitized["leadership_composition"]["needs_human_verification"] is True
        assert len(violations) == 1

    def test_score_with_citation_is_left_alone(self) -> None:
        values_signals = {
            "leadership_composition": {
                "score": 70,
                "rationale": "Org's own materials state it is Black-led.",
                "citation": "mission_text: 'a Black-led organization committed to...'",
                "needs_human_verification": False,
            }
        }
        sanitized, _, violations = enforce_hard_rules(values_signals, {})
        assert sanitized["leadership_composition"]["score"] == 70
        assert violations == []

    def test_null_score_with_needs_human_verification_is_the_expected_case(self) -> None:
        values_signals = {
            "leadership_composition": {
                "score": None,
                "rationale": "No published self-description found.",
                "citation": None,
                "needs_human_verification": True,
            }
        }
        _, _, violations = enforce_hard_rules(values_signals, {})
        assert violations == []


class TestRule2NeverFullyQualified:
    """§9 rule 2: capacity = 2 public criteria + "pending discovery conversation",
    never "fully qualified"."""

    def test_capacity_note_is_always_the_fixed_string(self) -> None:
        capacity = compute_capacity(dd_present=True, fundraising_spend_ratio=0.05)
        assert capacity["note"] == CAPACITY_NOTE
        assert "fully qualified" not in capacity["note"].lower()

    def test_capacity_note_fixed_regardless_of_inputs(self) -> None:
        # Even with no evidence at all, the note text never changes — it's Python,
        # never model-generated, so there's no path for the model to author it.
        capacity = compute_capacity(dd_present=None, fundraising_spend_ratio=None)
        assert capacity["note"] == CAPACITY_NOTE

    def test_forbidden_phrase_in_rationale_is_redacted(self) -> None:
        values_signals = {
            "programming": {
                "score": 90,
                "rationale": "This organization is fully qualified for our services.",
                "citation": "program_text[0].desc",
                "needs_human_verification": False,
            }
        }
        sanitized, _, violations = enforce_hard_rules(values_signals, {})
        assert "fully qualified" not in sanitized["programming"]["rationale"].lower()
        assert len(violations) == 1

    def test_forbidden_phrase_in_alignment_rationale_is_redacted(self) -> None:
        alignment_criteria = {
            "mission_alignment": {
                "met": True,
                "rationale": "Fully Qualified match on community-centered mission.",
                "citation": "mission_text",
            }
        }
        _, sanitized, violations = enforce_hard_rules({}, alignment_criteria)
        assert "fully qualified" not in sanitized["mission_alignment"]["rationale"].lower()
        assert len(violations) == 1

    def test_clean_rationale_untouched(self) -> None:
        values_signals = {
            "mission_language": {
                "score": 60,
                "rationale": "Mission text emphasizes community-centered service.",
                "citation": "mission_text",
                "needs_human_verification": False,
            }
        }
        sanitized, _, violations = enforce_hard_rules(values_signals, {})
        assert sanitized["mission_language"]["rationale"] == values_signals["mission_language"]["rationale"]
        assert violations == []


class TestScoreRangeEnforcement:
    """output_config.format's JSON schema can't constrain integer ranges (confirmed
    via a live 400), so the 0-100 range on scores is only a system-prompt instruction
    at request time, not a mechanical guarantee. These test the code-level backstop —
    without it, an out-of-range score would silently reach the DB and skew any
    downstream ranking (e.g. G1.5's gap_rank) built on these values."""

    def test_score_above_100_is_nulled_not_clamped(self) -> None:
        values_signals = {
            "programming": {
                "score": 150,
                "rationale": "Extremely strong program alignment.",
                "citation": "program_text[0].desc",
                "needs_human_verification": False,
            }
        }
        sanitized, _, violations = enforce_hard_rules(values_signals, {})
        assert sanitized["programming"]["score"] is None
        assert sanitized["programming"]["needs_human_verification"] is True
        assert len(violations) == 1
        assert "out of [0,100] range" in violations[0]

    def test_negative_score_is_nulled(self) -> None:
        values_signals = {
            "funder_base": {
                "score": -5,
                "rationale": "x",
                "citation": "y",
                "needs_human_verification": False,
            }
        }
        sanitized, _, violations = enforce_hard_rules(values_signals, {})
        assert sanitized["funder_base"]["score"] is None
        assert sanitized["funder_base"]["needs_human_verification"] is True
        assert len(violations) == 1

    def test_boundary_values_are_valid(self) -> None:
        values_signals = {
            "programming": {"score": 0, "rationale": "x", "citation": "y", "needs_human_verification": False},
            "funder_base": {"score": 100, "rationale": "x", "citation": "y", "needs_human_verification": False},
        }
        sanitized, _, violations = enforce_hard_rules(values_signals, {})
        assert sanitized["programming"]["score"] == 0
        assert sanitized["funder_base"]["score"] == 100
        assert violations == []

    def test_null_score_is_untouched(self) -> None:
        values_signals = {
            "population_served": {
                "score": None,
                "rationale": "No evidence provided.",
                "citation": None,
                "needs_human_verification": True,
            }
        }
        _, _, violations = enforce_hard_rules(values_signals, {})
        assert violations == []


class TestClampPreScore:
    """Haiku's pre_score has the same missing schema guarantee, but it's a ranking
    heuristic (used to cut to the top N before Sonnet), not a persisted evidentiary
    claim — clamped rather than nulled, since nulling would break the sort."""

    def test_within_range_untouched(self) -> None:
        assert clamp_pre_score(50) == 50
        assert clamp_pre_score(0) == 0
        assert clamp_pre_score(100) == 100

    def test_above_100_clamped_to_100(self) -> None:
        assert clamp_pre_score(150) == 100

    def test_below_0_clamped_to_0(self) -> None:
        assert clamp_pre_score(-20) == 0


class TestRule6NteeNeverInScoringInputs:
    """§9 rule 6: NTEE never appears in scoring inputs (recall shaping only)."""

    def test_build_org_context_never_includes_ntee(self) -> None:
        org = {
            "name": "Test Org",
            "city": "Cincinnati",
            "state": "OH",
            "ntee": "P20",  # present on the source row but must not leak through
            "mission_text": "We serve the community.",
            "program_text": [],
        }
        context = build_org_context(org)
        assert "ntee" not in context
        assert "P20" not in str(context)


class TestAlignmentFramework:
    """3-of-6 framework (criterion 2/GENESIS excluded per Duan — see score.py docstring)."""

    def test_priority_tier_metro_matches_configured_city(self) -> None:
        assert compute_priority_tier_metro("Charlotte", ["Charlotte", "Cincinnati"]) is True
        assert compute_priority_tier_metro("Boston", ["Charlotte", "Cincinnati"]) is False

    def test_priority_tier_metro_handles_missing_data(self) -> None:
        assert compute_priority_tier_metro(None, ["Charlotte"]) is False
        assert compute_priority_tier_metro("Charlotte", []) is False

    def test_qualifies_at_exactly_three_of_six(self) -> None:
        model_criteria = {
            "leadership_advances_equity": {"met": True, "rationale": "x", "citation": "y"},
            "mission_alignment": {"met": True, "rationale": "x", "citation": "y"},
            "case_study_potential": {"met": False, "rationale": "x", "citation": None},
            "connected_to_influencer_networks": {"met": False, "rationale": "x", "citation": None},
            "at_inflection_point": {"met": False, "rationale": "x", "citation": None},
        }
        alignment = assemble_alignment(priority_tier_metro=True, model_criteria=model_criteria)
        assert alignment["criteria_met_count"] == 3
        assert alignment["qualifies"] is True

    def test_does_not_qualify_below_threshold(self) -> None:
        model_criteria = {
            "leadership_advances_equity": {"met": True, "rationale": "x", "citation": "y"},
            "mission_alignment": {"met": False, "rationale": "x", "citation": None},
            "case_study_potential": {"met": False, "rationale": "x", "citation": None},
            "connected_to_influencer_networks": {"met": False, "rationale": "x", "citation": None},
            "at_inflection_point": {"met": False, "rationale": "x", "citation": None},
        }
        alignment = assemble_alignment(priority_tier_metro=False, model_criteria=model_criteria)
        assert alignment["criteria_met_count"] == 1
        assert alignment["qualifies"] is False

    def test_priority_tier_metro_is_a_criterion_in_the_assembled_output(self) -> None:
        alignment = assemble_alignment(priority_tier_metro=True, model_criteria={})
        assert alignment["criteria"]["priority_tier_metro"]["met"] is True


class TestSoftFlags:
    def test_heavy_govt_funding_above_threshold(self) -> None:
        assert compute_soft_flags(0.55, govt_funding_heavy_pct=0.40)["heavy_govt_funding"] is True

    def test_not_heavy_below_threshold(self) -> None:
        assert compute_soft_flags(0.10, govt_funding_heavy_pct=0.40)["heavy_govt_funding"] is False

    def test_none_when_no_data(self) -> None:
        assert compute_soft_flags(None, govt_funding_heavy_pct=0.40)["heavy_govt_funding"] is None
