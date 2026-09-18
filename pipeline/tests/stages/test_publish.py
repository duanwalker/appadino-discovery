from discovery.stages.publish import compute_gap_rank


def test_gap_rank_full_alignment_no_capacity_with_trigger_is_high() -> None:
    score = compute_gap_rank(criteria_met_count=6, dd_present=False, has_trigger=True)
    assert score == 100.0


def test_gap_rank_no_alignment_full_capacity_no_trigger_is_zero() -> None:
    score = compute_gap_rank(criteria_met_count=0, dd_present=True, has_trigger=False)
    assert score == 0.0


def test_gap_rank_unknown_capacity_is_between_present_and_absent() -> None:
    absent = compute_gap_rank(criteria_met_count=0, dd_present=False, has_trigger=False)
    unknown = compute_gap_rank(criteria_met_count=0, dd_present=None, has_trigger=False)
    present = compute_gap_rank(criteria_met_count=0, dd_present=True, has_trigger=False)
    assert present < unknown < absent


def test_gap_rank_custom_weights_applied() -> None:
    default = compute_gap_rank(criteria_met_count=3, dd_present=None, has_trigger=False)
    weighted = compute_gap_rank(
        criteria_met_count=3, dd_present=None, has_trigger=False, weights={"alignment": 1.0, "capacity_gap": 0.0, "trigger": 0.0}
    )
    assert weighted != default
    assert weighted == 50.0  # (3/6) * 1.0 * 100


def test_gap_rank_deterministic() -> None:
    a = compute_gap_rank(criteria_met_count=2, dd_present=False, has_trigger=True)
    b = compute_gap_rank(criteria_met_count=2, dd_present=False, has_trigger=True)
    assert a == b


class TestGapRankBounds:
    """Real gaps caught by an independent test-developer review
    (TEST-COVERAGE-GAPS.md, "G1.5" Gap 4): neither bound was previously enforced,
    so a data bug (criteria_met_count > 6) or a careless custom signal_weights
    config could silently produce a gap_rank outside the documented 0-100 range."""

    def test_criteria_met_count_above_max_is_clamped_not_inflated(self) -> None:
        at_max = compute_gap_rank(criteria_met_count=6, dd_present=True, has_trigger=False)
        over_max = compute_gap_rank(criteria_met_count=7, dd_present=True, has_trigger=False)
        assert over_max == at_max

    def test_negative_criteria_met_count_is_clamped_to_zero(self) -> None:
        zero = compute_gap_rank(criteria_met_count=0, dd_present=True, has_trigger=False)
        negative = compute_gap_rank(criteria_met_count=-2, dd_present=True, has_trigger=False)
        assert negative == zero

    def test_weights_summing_above_one_are_normalized_not_left_unbounded(self) -> None:
        # Without normalization this would be (6/6)*2.0 + 1.0*1.0 + 1.0*1.0 -> 400.0
        score = compute_gap_rank(
            criteria_met_count=6,
            dd_present=False,
            has_trigger=True,
            weights={"alignment": 2.0, "capacity_gap": 1.0, "trigger": 1.0},
        )
        assert score == 100.0

    def test_weights_summing_above_one_preserve_relative_proportions(self) -> None:
        # Doubling every weight uniformly should normalize back to the same result
        # as the un-doubled (sum-to-1.0) weights — relative priority is preserved,
        # not flattened to the max.
        baseline = compute_gap_rank(
            criteria_met_count=3,
            dd_present=None,
            has_trigger=False,
            weights={"alignment": 0.5, "capacity_gap": 0.3, "trigger": 0.2},
        )
        doubled = compute_gap_rank(
            criteria_met_count=3,
            dd_present=None,
            has_trigger=False,
            weights={"alignment": 1.0, "capacity_gap": 0.6, "trigger": 0.4},
        )
        assert doubled == baseline

    def test_weights_summing_below_one_are_left_alone(self) -> None:
        # Only sums *above* 1.0 are normalized — a deliberately "softer" config
        # (smaller max possible output) isn't a bug.
        score = compute_gap_rank(
            criteria_met_count=6, dd_present=False, has_trigger=True, weights={"alignment": 0.2, "capacity_gap": 0.1, "trigger": 0.1}
        )
        assert score == 40.0

    def test_output_always_within_zero_to_hundred(self) -> None:
        score = compute_gap_rank(
            criteria_met_count=100,
            dd_present=False,
            has_trigger=True,
            weights={"alignment": 10.0, "capacity_gap": 10.0, "trigger": 10.0},
        )
        assert 0.0 <= score <= 100.0
