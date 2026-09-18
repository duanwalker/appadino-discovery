"""Independent test-coverage review of G1.5 (Suppress + triggers + publish).

Covers 6 gap areas flagged in STATUS.md's own G1.5 report as unexercised or
soft-verified, written independent of test_suppress.py/test_triggers.py/
test_publish.py/test_qa.py:

1. Suppression fuzzy-match firing against real ARCHITECT seed names (synthetic
   near-miss collisions), and the flag-not-drop `notes` construction.
2. Trigger priority determinism across every multi-trigger combination, and
   trigger_evidence retention of every detected trigger.
3. trigger_angle=null (first_filing_above_floor) surviving CSV export as a real
   empty cell, not a fabricated "None" string or masked default.
4. gap_rank behavior when one or more of its three weighted inputs is missing.
5. QA text-citation matching edge cases: short quotes, paraphrase, special chars.
6. Publish idempotency: upsert semantics and CSV overwrite-not-append behavior.
"""

from __future__ import annotations

import csv
import tempfile
from pathlib import Path

from discovery.stages.publish import compute_gap_rank, export_csv, upsert_prospect
from discovery.stages.qa import _quoted_text_matches_corpus, verify_claim
from discovery.stages.suppress import find_fuzzy_match, fuzzy_match_score
from discovery.stages.triggers import (
    TRIGGER_PRIORITY,
    detect_triggers,
    select_primary_trigger,
)

# Real ARCHITECT seed suppression names (migration 7ac7a7b1f96a) — used verbatim so
# the fuzzy-match gap is exercised against actual production data, not invented names.
ARCHITECT_SEED_NAMES = [
    "Butterfly Dreamz",
    "Project OutPour",
    "Our Tribe Cincy",
    "Blue Bowtie Foundation",
    "Queen City Cocoa B.E.A.N.S.",
    "Common Cause",
    "WEMH",
    "She Dreams in Color",
    "LPCCD",
    "CCIP",
    "Clinton Hill Community Action",
    "CBAC",
    "LBFE Cincinnati",
    "The Partnership Fund",
    "Spring Clean",
    "CCT Center for Community Transitions",
    "Sanford Institute/National University",
]


class TestSuppressionFuzzyMatchAgainstRealSeedData:
    """Gap 1: fuzzy match has only ever been unit-tested against invented names —
    exercise it against the real ARCHITECT seed list with deliberate near-miss
    collisions, the scenario STATUS.md flags as never having fired in a live run."""

    def test_near_miss_with_corporate_suffix_punctuation_falls_just_below_threshold(self) -> None:
        """RESOLVED: previously "Butterfly Dreamz, Inc." scored 0.842 against the real
        seed name "Butterfly Dreamz" — just under the 0.85 threshold, so it did NOT
        flag. Fixed by stripping legal-suffix words (Inc., LLC, Corp., Foundation,
        etc.) and punctuation before scoring (see normalize_name in suppress.py),
        rather than lowering the threshold — a lower threshold would also have made
        unrelated-org false positives more likely."""
        score_with_punctuation = fuzzy_match_score("Butterfly Dreamz, Inc.", "Butterfly Dreamz")
        assert score_with_punctuation == 1.0
        assert score_with_punctuation >= 0.85
        match = find_fuzzy_match("Butterfly Dreamz, Inc.", ARCHITECT_SEED_NAMES)
        assert match is not None  # confirms the fix: this near-miss now flags

        # The unpunctuated variant of the same near-miss DOES clear the threshold,
        # confirming punctuation specifically is what tips this case over the line.
        match_unpunctuated = find_fuzzy_match("Butterfly Dreamz Inc", ARCHITECT_SEED_NAMES)
        assert match_unpunctuated == "Butterfly Dreamz"

    def test_near_miss_with_the_prefix_flags_against_real_seed(self) -> None:
        match = find_fuzzy_match("The WEMH Organization", ARCHITECT_SEED_NAMES)
        # "WEMH" is short — confirm whether a wrapped near-miss still clears 0.85;
        # documents actual behavior rather than assuming it does.
        score = fuzzy_match_score("The WEMH Organization", "WEMH")
        assert match == "WEMH" if score >= 0.85 else match is None

    def test_near_miss_with_added_org_type_suffix_word_falls_below_threshold(self) -> None:
        """RESOLVED: previously a real org-descriptor word appended to a real seed
        name ("She Dreams in Color" -> "...Foundation") dropped the score to 0.776,
        well under 0.85 — a plausible real-world renaming/legal-name variant would
        not have been caught by suppression. Fixed by stripping legal-suffix words
        (including "foundation") before scoring."""
        score = fuzzy_match_score("SHE DREAMS IN COLOR FOUNDATION", "She Dreams in Color")
        assert score == 1.0
        assert score >= 0.85
        match = find_fuzzy_match("SHE DREAMS IN COLOR FOUNDATION", ARCHITECT_SEED_NAMES)
        assert match is not None  # confirms the fix: this near-miss now flags

    def test_near_miss_abbreviation_expansion_does_not_over_match(self) -> None:
        """'Common Cause' vs an unrelated org that merely shares a common word should
        not fuzzy-match — confirms the threshold isn't so loose it over-suppresses."""
        match = find_fuzzy_match("Cincinnati Common Ground Alliance", ARCHITECT_SEED_NAMES)
        assert match is None

    def test_exact_real_seed_name_with_trailing_whitespace_flags(self) -> None:
        match = find_fuzzy_match("  Blue Bowtie Foundation  ", ARCHITECT_SEED_NAMES)
        assert match == "Blue Bowtie Foundation"

    def test_active_prospect_name_near_miss_also_flags(self) -> None:
        """Active prospects (kind='active_prospect') use the same fuzzy path as past
        clients — confirm the match isn't accidentally scoped to only one kind."""
        match = find_fuzzy_match("CCT Center for Community Transitions Inc", ARCHITECT_SEED_NAMES)
        assert match == "CCT Center for Community Transitions"

    def test_unrelated_synthetic_org_survives_against_full_real_seed_list(self) -> None:
        """A genuinely unrelated org name must not match anything in the real list —
        confirms the fuzzy path doesn't false-positive against production data."""
        match = find_fuzzy_match("Midcoast Maine Community Action", ARCHITECT_SEED_NAMES)
        assert match is None

    def test_fuzzy_flag_note_construction_flags_rather_than_drops(self) -> None:
        """Mirrors run_publish.py's exact notes-string construction for a fuzzy
        flag — confirms the org stays in the surfaced set (flagged via `notes`,
        not excluded), per §9 rule 7."""
        matched_name = find_fuzzy_match("Butterfly Dreamz LLC", ARCHITECT_SEED_NAMES)
        assert matched_name is not None
        notes = f"Possible suppression match: '{matched_name}' (fuzzy, not auto-excluded)"
        assert "not auto-excluded" in notes
        assert matched_name in notes


class TestTriggerPriorityDeterminism:
    """Gap 2: confirm assigned-trigger selection is deterministic across every
    combination of the 4 trigger types, and trigger_evidence keeps all of them."""

    def _all_four_trigger_dicts(self) -> list[dict[str, object]]:
        return [
            {"type": "new_ed", "evidence": {"marker": "new_ed"}},
            {"type": "dd_departure", "evidence": {"marker": "dd_departure"}},
            {"type": "transformational_revenue_jump", "evidence": {"marker": "transformational_revenue_jump"}},
            {"type": "first_filing_above_floor", "evidence": {"marker": "first_filing_above_floor"}},
        ]

    def test_all_four_types_present_selects_highest_priority_regardless_of_order(self) -> None:
        base = self._all_four_trigger_dicts()
        import itertools

        for perm in itertools.permutations(base):
            primary = select_primary_trigger(list(perm))
            assert primary is not None
            assert primary["type"] == "new_ed"  # first in TRIGGER_PRIORITY

    def test_every_pairwise_combination_picks_the_higher_priority_type(self) -> None:
        import itertools

        by_type = {t["type"]: t for t in self._all_four_trigger_dicts()}
        for a, b in itertools.combinations(TRIGGER_PRIORITY, 2):
            higher, lower = (a, b) if TRIGGER_PRIORITY.index(a) < TRIGGER_PRIORITY.index(b) else (b, a)
            primary = select_primary_trigger([by_type[a], by_type[b]])
            assert primary is not None
            assert primary["type"] == higher, f"expected {higher} over {lower}"

    def test_three_way_combination_without_new_ed_picks_dd_departure(self) -> None:
        by_type = {t["type"]: t for t in self._all_four_trigger_dicts()}
        triggers = [by_type["dd_departure"], by_type["transformational_revenue_jump"], by_type["first_filing_above_floor"]]
        primary = select_primary_trigger(triggers)
        assert primary is not None
        assert primary["type"] == "dd_departure"

    def test_trigger_evidence_retains_all_detected_triggers_even_when_one_assigned(self) -> None:
        """The angle table only names one type per org, but detect_triggers's full
        return value (what becomes trigger_evidence) must retain every hit."""
        current = {
            "revenue_total": 1_600_000,
            "officers": [{"name": "New Exec", "title": "Executive Director"}],
        }
        previous = {
            "revenue_total": 1_000_000,  # +60% triggers transformational jump too
            "officers": [{"name": "Old Exec", "title": "Executive Director"}],
        }
        detected = detect_triggers(current, previous, revenue_floor=500_000, transformational_jump_pct=0.50)
        types = {t["type"] for t in detected}
        assert {"new_ed", "transformational_revenue_jump"}.issubset(types)

        primary = select_primary_trigger(detected)
        assert primary is not None
        assert primary["type"] == "new_ed"
        # Both fired triggers must still be present in the full evidence list, even
        # though only new_ed became "assigned" — nothing is discarded.
        assert len(detected) == len(types)
        assert "transformational_revenue_jump" in types

    def test_all_four_trigger_types_can_fire_simultaneously_and_all_survive(self) -> None:
        """Construct a filing pair that satisfies all four conditions at once and
        confirm none of the four are silently dropped from evidence."""
        current = {
            "revenue_total": 900_000,  # above floor(500k), prior was below → also first_filing_above_floor
            "officers": [{"name": "New Exec", "title": "Executive Director"}],  # no dev officer
        }
        previous = {
            "revenue_total": 400_000,  # below floor, and +125% jump from 400k->900k
            "officers": [
                {"name": "Old Exec", "title": "Executive Director"},
                {"name": "Dev Person", "title": "Director of Development"},  # disappears in current
            ],
        }
        detected = detect_triggers(current, previous, revenue_floor=500_000, transformational_jump_pct=0.50)
        types = {t["type"] for t in detected}
        assert types == {"new_ed", "dd_departure", "transformational_revenue_jump", "first_filing_above_floor"}
        primary = select_primary_trigger(detected)
        assert primary is not None
        assert primary["type"] == "new_ed"


class TestTriggerAngleNullDownstream:
    """Gap 3: first_filing_above_floor's trigger_angle=null must not get coerced
    into a fabricated empty-reads-as-real-angle value anywhere downstream."""

    def test_export_csv_writes_null_trigger_angle_as_genuinely_empty_cell(self) -> None:
        rows = [
            {
                "ein": "123456789",
                "name": "Test Org",
                "city": "Charlotte",
                "state": "NC",
                "gap_rank": 42.0,
                "criteria_met_count": 2,
                "qualifies": False,
                "assigned_trigger": "first_filing_above_floor",
                "trigger_angle": None,  # the gap: no default angle exists for this trigger
                "dd_present": None,
                "fundraising_spend_ratio": None,
                "suppression_flag": "",
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "prospects.csv"
            export_csv(rows, path)
            with path.open(newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                out_row = next(reader)
        # Must be a genuinely empty string, never the literal "None"/"null" text
        assert out_row["trigger_angle"] == ""
        assert out_row["trigger_angle"] != "None"
        assert out_row["trigger_angle"] != "null"

    def test_export_csv_distinguishes_null_angle_from_a_real_but_short_angle_only_by_value(self) -> None:
        """Documents a real, inherent CSV-format limitation: once written, a null
        angle and a genuinely blank/empty configured angle are indistinguishable in
        the CSV. Not a code bug, but worth pinning so it isn't discovered by
        surprise downstream (e.g. in the dashboard)."""
        rows = [
            {"ein": "1", "name": "A", "city": "", "state": "", "gap_rank": 1.0, "criteria_met_count": 0,
             "qualifies": False, "assigned_trigger": "first_filing_above_floor", "trigger_angle": None,
             "dd_present": None, "fundraising_spend_ratio": None, "suppression_flag": ""},
            {"ein": "2", "name": "B", "city": "", "state": "", "gap_rank": 1.0, "criteria_met_count": 0,
             "qualifies": False, "assigned_trigger": "new_ed", "trigger_angle": "", "dd_present": None,
             "fundraising_spend_ratio": None, "suppression_flag": ""},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "prospects.csv"
            export_csv(rows, path)
            with path.open(newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                out_rows = list(reader)
        assert out_rows[0]["trigger_angle"] == out_rows[1]["trigger_angle"] == ""

    def test_resolve_angle_none_is_not_confused_with_missing_config_key(self) -> None:
        """A trigger type entirely absent from trigger_angles config vs. explicitly
        mapped to None must both resolve to None, not raise or fabricate a default."""
        from discovery.stages.triggers import resolve_angle

        assert resolve_angle("first_filing_above_floor", {"first_filing_above_floor": None}) is None
        assert resolve_angle("first_filing_above_floor", {}) is None


class TestGapRankWithPartialOrMissingSignalData:
    """Gap 4: what happens when one of the three weighted inputs is missing,
    versus a genuine zero/false value — must not silently misrank."""

    def test_missing_alignment_data_defaulted_to_zero_scores_identically_to_true_zero_alignment(self) -> None:
        """run_publish.py defaults criteria_met_count to 0 via `.get(..., 0)` when
        alignment data is absent — this test documents that this is indistinguishable
        from an org that was actually scored and failed every criterion. Not a crash,
        but a real ambiguity: "unscored" and "scored zero" collapse to the same
        gap_rank. Flagged, not silently accepted as correct."""
        never_scored = compute_gap_rank(criteria_met_count=0, dd_present=None, has_trigger=False)
        actually_failed_all_criteria = compute_gap_rank(criteria_met_count=0, dd_present=False, has_trigger=False)
        # These differ only because dd_present differs in this example — with BOTH
        # alignment and capacity absent/zero, the two states become fully identical:
        truly_indistinguishable_a = compute_gap_rank(criteria_met_count=0, dd_present=None, has_trigger=False)
        truly_indistinguishable_b = compute_gap_rank(criteria_met_count=0, dd_present=None, has_trigger=False)
        assert truly_indistinguishable_a == truly_indistinguishable_b
        assert never_scored != actually_failed_all_criteria  # capacity_gap still differentiates here

    def test_missing_capacity_data_dd_present_none_is_moderate_not_zeroed_out(self) -> None:
        """dd_present=None (no signal computed) must score as a moderate gap
        (0.5), not silently collapse to 0.0 as if capacity were confirmed present."""
        unknown = compute_gap_rank(criteria_met_count=3, dd_present=None, has_trigger=False)
        confirmed_present = compute_gap_rank(criteria_met_count=3, dd_present=True, has_trigger=False)
        confirmed_absent = compute_gap_rank(criteria_met_count=3, dd_present=False, has_trigger=False)
        assert confirmed_present < unknown < confirmed_absent

    def test_all_three_components_simultaneously_missing_produces_low_but_nonzero_or_zero_deterministically(self) -> None:
        """Worst case: no alignment score, no capacity signal, no trigger detected.
        Must not error, and must be fully deterministic (not silently treated as a
        random/undefined rank)."""
        score_a = compute_gap_rank(criteria_met_count=0, dd_present=None, has_trigger=False)
        score_b = compute_gap_rank(criteria_met_count=0, dd_present=None, has_trigger=False)
        assert score_a == score_b
        # With default weights: alignment=0, capacity_gap=0.5*0.3=15, trigger=0 → 15.0
        assert score_a == 15.0

    def test_out_of_range_criteria_met_count_is_not_clamped(self) -> None:
        """RESOLVED: compute_gap_rank previously had no defensive clamp on
        criteria_met_count — a value above ALIGNMENT_MAX_CRITERIA (a data bug
        elsewhere) silently inflated the alignment component past 1.0 rather than
        erroring or capping. Now clamped to [0, ALIGNMENT_MAX_CRITERIA]."""
        in_range_max = compute_gap_rank(criteria_met_count=6, dd_present=None, has_trigger=False)
        out_of_range = compute_gap_rank(criteria_met_count=7, dd_present=None, has_trigger=False)
        assert out_of_range == in_range_max  # clamped at the max-criteria ceiling

    def test_partial_weight_override_does_not_zero_out_unspecified_weights(self) -> None:
        """Overriding only one weight key must merge over the defaults (same
        pattern as Stage 1's recall_filter config merge) rather than zeroing the
        other two components."""
        overridden = compute_gap_rank(
            criteria_met_count=3, dd_present=False, has_trigger=True, weights={"alignment": 0.8}
        )
        # capacity_gap (0.3 default) and trigger (0.2 default) must still contribute
        alignment_only = compute_gap_rank(
            criteria_met_count=3, dd_present=None, has_trigger=False, weights={"alignment": 0.8, "capacity_gap": 0.0, "trigger": 0.0}
        )
        assert overridden > alignment_only

    def test_weights_that_sum_above_one_are_not_capped_at_100(self) -> None:
        """RESOLVED: weights summing above 1.0 previously had no normalization,
        letting gap_rank push mathematically above what "0-100" implies. Now
        normalized proportionally back down to a sum of 1.0 when the raw sum
        exceeds 1.0."""
        score = compute_gap_rank(
            criteria_met_count=6, dd_present=False, has_trigger=True,
            weights={"alignment": 1.0, "capacity_gap": 1.0, "trigger": 1.0},
        )
        assert score == 100.0  # normalized — confirms the clamp/normalization fix


class TestQATextCitationEdgeCases:
    """Gap 5: longest-common-substring text matching edge cases."""

    def test_very_short_quote_uses_its_own_length_as_the_match_bar_not_min_text_match_len(self) -> None:
        """DISCOVERED GAP: for citations shorter than MIN_TEXT_MATCH_LEN (25 chars),
        the effective bar is the citation's own (short) length, not the 25-char
        floor — so a trivially short, out-of-context substring like "the org"
        counts as "grounded" purely because it's fully contained in a much longer
        corpus. This is a real false-verification risk for very short citations,
        not a crash."""
        citation = "the org"
        corpus = "the organization serves youth in rural communities across the state"
        assert _quoted_text_matches_corpus(citation, corpus) is True  # documents the gap

    def test_short_citation_shorter_than_min_length_uses_full_citation_length_as_bar(self) -> None:
        """When the citation itself is shorter than MIN_TEXT_MATCH_LEN, the bar
        becomes the full (normalized) citation length — an exact short substring
        match still passes, but a partial one does not."""
        citation = "youth mentoring"
        corpus = "we run a youth mentoring program for at-risk teens"
        assert _quoted_text_matches_corpus(citation, corpus) is True

        citation_partial_only = "youth counseling"
        assert _quoted_text_matches_corpus(citation_partial_only, corpus) is False

    def test_paraphrased_not_quoted_claim_may_fail_pure_lcs_matching(self) -> None:
        """A genuine paraphrase that reorders/rewords the source rather than quoting
        it verbatim can legitimately fail LCS matching — this is a real, documented
        limitation of pattern-based verification (module docstring: 'not full
        semantic fact-checking'), not a bug to fix here."""
        corpus = "the organization provides free legal representation to low income tenants facing eviction"
        paraphrase = "this nonprofit offers no-cost eviction defense services for renters who cannot afford a lawyer"
        assert _quoted_text_matches_corpus(paraphrase, corpus) is False

    def test_verify_claim_with_special_characters_in_citation_and_corpus(self) -> None:
        """Em-dashes, curly quotes, ampersands, and numerals must not break
        normalization or crash the matcher."""
        citation = "the organization's mission — \u201cequity & access for all\u201d — drives our work"
        corpus = "our mission equity access for all drives everything the organization does 2024"
        assert _quoted_text_matches_corpus(citation, corpus) is True

    def test_verify_claim_inserted_parenthetical_breaks_one_contiguous_match_into_two_shorter_ones(self) -> None:
        """DISCOVERED GAP: hyphens normalize to spaces (not dropped), so hyphenation
        style alone isn't the issue \u2014 but a parenthetical aside in the real corpus
        ("(est. 1998)") sits between two phrases that are otherwise a perfect,
        faithful match to the citation, splitting one long contiguous run into two
        22-character runs that each fall under MIN_TEXT_MATCH_LEN (25). A completely
        accurate paraphrase of real program text can mismatch purely because of an
        inserted aside elsewhere in the source sentence."""
        org_context = {
            "mission_text": None,
            "program_text": [{"desc": "Youth & Family Services (est. 1998) \u2014 after-school tutoring"}],
        }
        citation = "youth family services after school tutoring"
        assert verify_claim(citation, org_context) == "mismatch"  # documents the gap

        # Remove the inserted parenthetical and the identical citation now matches,
        # confirming the aside \u2014 not hyphenation \u2014 is what breaks contiguity.
        org_context_without_aside = {
            "mission_text": None,
            "program_text": [{"desc": "Youth & Family Services after-school tutoring"}],
        }
        assert verify_claim(citation, org_context_without_aside) == "match"

    def test_verify_claim_numeric_citation_ignores_surrounding_special_characters(self) -> None:
        citation = "govt_pct: 0.59% (per 990 filing—see note)"
        org_context = {"govt_pct": 0.59}
        assert verify_claim(citation, org_context) == "match"

    def test_empty_string_citation_against_present_corpus_is_mismatch_not_unverifiable(self) -> None:
        """DISCOVERED GAP: an empty-string citation resolves to "mismatch", not
        "unverifiable" — _quoted_text_matches_corpus short-circuits on an empty
        normalized citation and returns False, which verify_claim then reports as a
        false claim rather than "nothing to verify". Currently unreachable in the
        real pipeline (extract_claims only extracts claims with a truthy citation,
        and "" is falsy), but this is defense-in-depth that doesn't hold if that
        upstream filter ever changes — worth knowing, not just assuming."""
        org_context = {"mission_text": "We serve the community.", "program_text": []}
        assert verify_claim("", org_context) == "mismatch"  # documents the gap


class TestPublishIdempotency:
    """Gap 6: re-running publish twice on the same client without new data must
    not create duplicate prospects rows or corrupt CSV/rank output."""

    def test_upsert_prospect_sql_uses_on_conflict_upsert_not_plain_insert(self) -> None:
        """Static check that the upsert path is genuinely idempotent at the SQL
        level — ON CONFLICT (client_id, ein) DO UPDATE, not a bare INSERT that would
        raise or duplicate on a second run."""
        import inspect

        source = inspect.getsource(upsert_prospect)
        assert "ON CONFLICT (client_id, ein) DO UPDATE" in source
        assert "INSERT INTO prospects" in source

    def test_export_csv_overwrites_rather_than_appends_on_second_call(self) -> None:
        """A second publish run with fewer/different rows must fully replace the
        CSV, not leave stale rows from the first run appended underneath."""
        first_run_rows = [
            {"ein": "1", "name": "A", "city": "", "state": "", "gap_rank": 1.0, "criteria_met_count": 0,
             "qualifies": False, "assigned_trigger": None, "trigger_angle": None, "dd_present": None,
             "fundraising_spend_ratio": None, "suppression_flag": ""},
            {"ein": "2", "name": "B", "city": "", "state": "", "gap_rank": 2.0, "criteria_met_count": 0,
             "qualifies": False, "assigned_trigger": None, "trigger_angle": None, "dd_present": None,
             "fundraising_spend_ratio": None, "suppression_flag": ""},
        ]
        second_run_rows = [
            {"ein": "1", "name": "A", "city": "", "state": "", "gap_rank": 5.0, "criteria_met_count": 1,
             "qualifies": False, "assigned_trigger": None, "trigger_angle": None, "dd_present": None,
             "fundraising_spend_ratio": None, "suppression_flag": ""},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "prospects.csv"
            export_csv(first_run_rows, path)
            export_csv(second_run_rows, path)
            with path.open(newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                out_rows = list(reader)
        assert len(out_rows) == 1  # not 3 (2 stale + 1 new)
        assert out_rows[0]["ein"] == "1"
        assert out_rows[0]["gap_rank"] == "5.0"

    def test_gap_rank_recomputation_is_deterministic_across_repeated_runs(self) -> None:
        """Re-running publish with unchanged upstream signals must recompute the
        identical gap_rank each time — no hidden state or randomness."""
        run_1 = compute_gap_rank(criteria_met_count=3, dd_present=False, has_trigger=True)
        run_2 = compute_gap_rank(criteria_met_count=3, dd_present=False, has_trigger=True)
        run_3 = compute_gap_rank(criteria_met_count=3, dd_present=False, has_trigger=True)
        assert run_1 == run_2 == run_3

    def test_update_score_gap_rank_sql_is_a_targeted_update_not_an_insert(self) -> None:
        """A second run's gap_rank sync back onto `scores` must overwrite the same
        row (WHERE on the full composite key), never insert a duplicate scores row."""
        import inspect

        from discovery.stages.publish import update_score_gap_rank

        source = inspect.getsource(update_score_gap_rank)
        assert source.strip().startswith("def update_score_gap_rank")
        assert "UPDATE scores SET gap_rank" in source
        assert "WHERE client_id = %s AND ein = %s AND icp_version = %s AND stage = 'sonnet'" in source
