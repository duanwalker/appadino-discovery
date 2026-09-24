"""§9 rule 7: suppressed orgs never surface; fuzzy suppression matches surface as
flags, not silent drops. Most of these test the pure matching logic (no DB); the
apply_suppression tests near the bottom use a hand-rolled fake connection, matching
this suite's existing house style (see test_g13_filter_signal_gaps.py)."""

from __future__ import annotations

from typing import Any, Self

from discovery.stages.suppress import (
    FUZZY_MATCH_THRESHOLD,
    apply_suppression,
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


class _FakeCursor:
    def __init__(self, conn: _FakeConn) -> None:
        self.conn = conn
        self._result: list[tuple[Any, ...]] = []

    def execute(self, sql: str, params: Any = None) -> None:
        stripped = sql.strip()
        if stripped.startswith("SELECT ein, org_name, kind FROM suppression"):
            self._result = [(e["ein"], e["org_name"], e["kind"]) for e in self.conn.suppression_entries]
        elif stripped.startswith("SELECT ein, name FROM organizations"):
            eins = params[0]
            self._result = [(ein, name) for ein, name in self.conn.org_names.items() if ein in eins]
        else:
            raise AssertionError(f"unexpected SQL in _FakeCursor: {sql!r}")

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._result

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class _FakeConn:
    def __init__(self, suppression_entries: list[dict[str, str | None]], org_names: dict[str, str]) -> None:
        self.suppression_entries = suppression_entries
        self.org_names = org_names

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)


def test_apply_suppression_drops_ein_exact_matches() -> None:
    conn = _FakeConn(
        suppression_entries=[{"ein": "111111111", "org_name": "Known Client", "kind": "client"}],
        org_names={"111111111": "Known Client", "222222222": "Other Org"},
    )
    remaining, fuzzy_flags = apply_suppression(conn, client_id=1, eins=["111111111", "222222222"])  # type: ignore[arg-type]

    assert remaining == ["222222222"]
    assert fuzzy_flags == {}


def test_apply_suppression_flags_fuzzy_matches_without_dropping() -> None:
    conn = _FakeConn(
        suppression_entries=[{"ein": None, "org_name": "Butterfly Dreamz", "kind": "prospect"}],
        org_names={"222222222": "Butterfly Dreamz, Inc."},
    )
    remaining, fuzzy_flags = apply_suppression(conn, client_id=1, eins=["222222222"])  # type: ignore[arg-type]

    assert remaining == ["222222222"]  # flagged, never dropped
    assert fuzzy_flags == {"222222222": "Butterfly Dreamz"}


def test_apply_suppression_unrelated_org_passes_through_untouched() -> None:
    conn = _FakeConn(
        suppression_entries=[{"ein": None, "org_name": "Butterfly Dreamz", "kind": "prospect"}],
        org_names={"333333333": "Totally Unrelated Nonprofit"},
    )
    remaining, fuzzy_flags = apply_suppression(conn, client_id=1, eins=["333333333"])  # type: ignore[arg-type]

    assert remaining == ["333333333"]
    assert fuzzy_flags == {}


def test_apply_suppression_on_already_reduced_input_set_still_flags_fuzzy() -> None:
    """Simulates Stage 6/Publish receiving a set that's already had EIN-exact matches
    dropped upstream (the new early gate in run_filter_and_signals, §pipeline reorder):
    apply_suppression makes no assumption its input still contains every EIN-exact
    match — the suppression table lookup is independent of what's in `eins` — and
    still correctly fuzzy-flags whatever fuzzy matches remain in the smaller set."""
    conn = _FakeConn(
        suppression_entries=[
            {"ein": "111111111", "org_name": "Known Client", "kind": "client"},  # already excluded upstream
            {"ein": None, "org_name": "Butterfly Dreamz", "kind": "prospect"},
        ],
        org_names={"222222222": "Butterfly Dreamz, Inc.", "333333333": "Totally Unrelated"},
    )
    # "111111111" deliberately absent from eins — simulating the pre-Stage2-reduced set
    remaining, fuzzy_flags = apply_suppression(conn, client_id=1, eins=["222222222", "333333333"])  # type: ignore[arg-type]

    assert remaining == ["222222222", "333333333"]
    assert fuzzy_flags == {"222222222": "Butterfly Dreamz"}
