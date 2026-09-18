"""§9 rule 7: suppressed orgs never surface; fuzzy suppression matches surface as
flags, not silent drops. These test the pure matching logic (no DB)."""

from discovery.stages.suppress import (
    FUZZY_MATCH_THRESHOLD,
    find_fuzzy_match,
    fuzzy_match_score,
    normalize_name,
)


def test_normalize_name_strips_and_lowercases() -> None:
    assert normalize_name("  She Dreams In Color  ") == "she dreams in color"


def test_normalize_name_strips_legal_suffix_words() -> None:
    assert normalize_name("Butterfly Dreamz, Inc.") == "butterfly dreamz"
    assert normalize_name("She Dreams in Color Foundation") == "she dreams in color"
    assert normalize_name("Example Corp.") == "example"
    assert normalize_name("Example LLC") == "example"


def test_normalize_name_does_not_strip_meaningful_words() -> None:
    # "Foundation" is stripped as a suffix, but the rest of a name with real
    # distinguishing content must survive intact.
    assert normalize_name("Blue Bowtie Foundation") == "blue bowtie"


def test_fuzzy_match_score_identical_names() -> None:
    assert fuzzy_match_score("Butterfly Dreamz", "Butterfly Dreamz") == 1.0


def test_fuzzy_match_score_near_identical_with_suffix() -> None:
    score = fuzzy_match_score("She Dreams in Color", "She Dreams in Color, Inc.")
    assert score > 0.85


def test_fuzzy_match_score_unrelated_names_is_low() -> None:
    score = fuzzy_match_score("Butterfly Dreamz", "Cincinnati Symphony Orchestra")
    assert score < 0.5


class TestRealNearMissesFromIndependentReview:
    """Real gaps caught by an independent test-developer review of the live 154-org
    dataset + real ARCHITECT seed list (TEST-COVERAGE-GAPS.md, "G1.5" section) —
    fixed via normalize_name stripping legal-suffix words, not by lowering the
    threshold (which would make unrelated-org false positives more likely instead)."""

    def test_butterfly_dreamz_inc_matches_seed_name(self) -> None:
        assert fuzzy_match_score("Butterfly Dreamz, Inc.", "Butterfly Dreamz") >= FUZZY_MATCH_THRESHOLD

    def test_she_dreams_in_color_foundation_matches_seed_name(self) -> None:
        assert fuzzy_match_score("SHE DREAMS IN COLOR FOUNDATION", "She Dreams in Color") >= FUZZY_MATCH_THRESHOLD

    def test_find_fuzzy_match_flags_corporate_suffix_variant(self) -> None:
        candidates = ["Butterfly Dreamz", "Common Cause", "WEMH"]
        assert find_fuzzy_match("Butterfly Dreamz, Inc.", candidates) == "Butterfly Dreamz"

    def test_find_fuzzy_match_flags_foundation_variant(self) -> None:
        candidates = ["She Dreams in Color", "Common Cause"]
        assert find_fuzzy_match("SHE DREAMS IN COLOR FOUNDATION", candidates) == "She Dreams in Color"


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
