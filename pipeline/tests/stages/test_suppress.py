"""§9 rule 7: suppressed orgs never surface; fuzzy suppression matches surface as
flags, not silent drops. These test the pure matching logic (no DB)."""

from discovery.stages.suppress import find_fuzzy_match, fuzzy_match_score, normalize_name


def test_normalize_name_strips_and_lowercases() -> None:
    assert normalize_name("  She Dreams In Color  ") == "she dreams in color"


def test_fuzzy_match_score_identical_names() -> None:
    assert fuzzy_match_score("Butterfly Dreamz", "Butterfly Dreamz") == 1.0


def test_fuzzy_match_score_near_identical_with_suffix() -> None:
    score = fuzzy_match_score("She Dreams in Color", "She Dreams in Color, Inc.")
    assert score > 0.85


def test_fuzzy_match_score_unrelated_names_is_low() -> None:
    score = fuzzy_match_score("Butterfly Dreamz", "Cincinnati Symphony Orchestra")
    assert score < 0.5


def test_find_fuzzy_match_returns_matching_candidate() -> None:
    candidates = ["Butterfly Dreamz", "Common Cause", "WEMH"]
    assert find_fuzzy_match("Butterfly Dreamz Inc", candidates) == "Butterfly Dreamz"


def test_find_fuzzy_match_returns_none_when_no_candidate_close_enough() -> None:
    candidates = ["Butterfly Dreamz", "Common Cause"]
    assert find_fuzzy_match("Totally Unrelated Nonprofit", candidates) is None


def test_find_fuzzy_match_respects_custom_threshold() -> None:
    candidates = ["Common Cause"]
    # "Cause" alone is a partial/weak match — shouldn't pass a strict threshold
    assert find_fuzzy_match("Cause", candidates, threshold=0.95) is None
