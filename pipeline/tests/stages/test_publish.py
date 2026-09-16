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
