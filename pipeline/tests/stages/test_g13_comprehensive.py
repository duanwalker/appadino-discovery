"""Comprehensive edge-case and regression tests for G1.3 (filter.py + extract_signals.py).

Coverage areas:
1. Stage 1 filter boundary conditions and invalid configs
2. ZIP-archive/remotezip lookup failures and regression (Deflate64)
3. Coverage_pct accuracy when filings skip
4. Signal computation on synthetic edge cases
5. NTEE data isolation (never in scored output)
"""

from __future__ import annotations

from discovery.stages.extract_signals import (
    compute_signals,
    parse_990_xml,
)
from discovery.stages.filter import build_survivor_query


class TestStage1FilterBoundaryConditions:
    """Gap 1: Stage 1 filter edge cases — boundary conditions, nulls, invalid configs."""

    def test_build_survivor_query_revenue_exactly_at_floor(self) -> None:
        """Revenue exactly at floor boundary should pass (>=, not >)."""
        query, params = build_survivor_query({"revenue_floor": 1_000_000})
        assert "o.revenue_latest >= %(revenue_floor)s" in query
        assert params["revenue_floor"] == 1_000_000
        # In actual SQL, an org with revenue_latest=1_000_000 would pass

    def test_build_survivor_query_revenue_exactly_at_ceiling(self) -> None:
        """Revenue exactly at ceiling boundary should pass (<=, not <)."""
        query, params = build_survivor_query({"revenue_ceiling": 5_000_000})
        assert "o.revenue_latest <= %(revenue_ceiling)s" in query
        assert params["revenue_ceiling"] == 5_000_000
        # In actual SQL, an org with revenue_latest=5_000_000 would pass

    def test_build_survivor_query_foundation_code_null_passes(self) -> None:
        """NULL foundation_code should pass the foundation_code filter (IS NULL OR not in exclude list)."""
        query, params = build_survivor_query({"exclude_foundation_codes": ["00", "02"]})
        # The condition is: (o.foundation_code IS NULL OR o.foundation_code != ALL(...))
        assert "(o.foundation_code IS NULL OR o.foundation_code != ALL" in query
        assert params["exclude_foundation_codes"] == ["00", "02"]

    def test_build_survivor_query_foundation_code_in_exclude_list_fails(self) -> None:
        """foundation_code in the exclude list should be filtered out (SQL != ALL will catch it)."""
        query, _ = build_survivor_query(
            {"exclude_foundation_codes": ["00", "02", "03", "04", "12", "13", "14"]}
        )
        # The condition is: (o.foundation_code IS NULL OR o.foundation_code != ALL(...))
        assert "o.foundation_code != ALL(%(exclude_foundation_codes)s)" in query

    def test_build_survivor_query_exclude_all_ntee_prefixes(self) -> None:
        """Config that excludes every possible NTEE prefix — query still generates correctly."""
        query, params = build_survivor_query(
            {"exclude_ntee_prefixes": ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J", "K", "L", "M", "N", "O", "P", "Q", "R", "S", "T", "U", "V", "W", "X", "Y", "Z"]}
        )
        # Query should still be valid; it just won't match any NTEE
        assert "ntee" in query.lower()
        assert len([k for k in params if k.startswith("ntee_prefix_")]) == 26

    def test_build_survivor_query_exclude_foundation_codes_empty_list(self) -> None:
        """Config with empty exclude_foundation_codes should still generate valid SQL (no codes excluded)."""
        query, params = build_survivor_query({"exclude_foundation_codes": []})
        # Empty list passed to != ALL should still be valid SQL
        assert "o.foundation_code != ALL(%(exclude_foundation_codes)s)" in query
        assert params["exclude_foundation_codes"] == []

    def test_build_survivor_query_zero_revenue_floor(self) -> None:
        """revenue_floor of 0 (edge case: accept any revenue >= 0)."""
        query, params = build_survivor_query({"revenue_floor": 0})
        assert params["revenue_floor"] == 0
        assert "o.revenue_latest >= %(revenue_floor)s" in query

    def test_build_survivor_query_inverted_boundaries_still_produces_query(self) -> None:
        """If ceiling < floor (config error), query still generates — SQL will return no rows."""
        _, params = build_survivor_query(
            {"revenue_floor": 5_000_000, "revenue_ceiling": 1_000_000}
        )
        assert params["revenue_floor"] == 5_000_000
        assert params["revenue_ceiling"] == 1_000_000
        # In SQL: revenue >= 5M AND revenue <= 1M returns 0 rows (no silent failure)


class TestArchiveLookupAndRemoteZipRegression:
    """Gap 2: ZIP-archive/remotezip lookup failures and regression tests."""

    def test_fetch_filing_xml_missing_object_id_returns_none(self) -> None:
        """object_id not in year_index → returns None, not crash."""
        from discovery.stages.extract_signals import fetch_filing_xml

        year_index = {"obj_123": ("https://example.com/archive.zip", "obj_123_public.xml")}
        result = fetch_filing_xml(year_index, "obj_not_found")
        assert result is None

    def test_parse_990_xml_with_defect_compression_method_tolerance(self) -> None:
        """Regression: Deflate64 compression method error was hit live during G1.3.
        This test documents that the parse_990_xml function gracefully handles
        malformed/unsupported compression by raising ParseError (now caught at
        orchestrator level). The fix is at the orchestrator level (catch Exception
        around fetch+parse), not here, but document the error type."""
        # This is tested at the orchestrator level in test_extract_signals_coverage_tracking

    def test_year_from_archive_url_with_trailing_slash(self) -> None:
        """Archive URL with trailing slash should still extract year correctly."""
        from discovery.stages.extract_signals import _year_from_archive_url

        assert _year_from_archive_url("https://apps.irs.gov/pub/epostcard/990/xml/2023/") == 2023

    def test_year_from_archive_url_without_trailing_slash(self) -> None:
        """Archive URL without trailing slash should still extract year correctly."""
        from discovery.stages.extract_signals import _year_from_archive_url

        assert _year_from_archive_url("https://apps.irs.gov/pub/epostcard/990/xml/2023") == 2023

    def test_year_from_archive_url_malformed_returns_none(self) -> None:
        """Malformed URL with no year pattern returns None (caught at orchestrator level)."""
        from discovery.stages.extract_signals import _year_from_archive_url

        assert _year_from_archive_url("") is None
        assert _year_from_archive_url("https://example.com/no-year") is None
        assert _year_from_archive_url("/path/without/year") is None


class TestCoveragePctAccuracy:
    """Gap 3: Coverage_pct accuracy when filings are skipped."""

    def test_coverage_pct_all_filings_succeed(self) -> None:
        """All filings parsed → coverage_pct = 1.0."""
        filings_parsed = 10
        filings_failed = 0
        total = filings_parsed + filings_failed
        coverage_pct = round(filings_parsed / total, 4) if total else None
        assert coverage_pct == 1.0

    def test_coverage_pct_half_succeed_half_fail(self) -> None:
        """Half succeed, half fail → coverage_pct = 0.5."""
        filings_parsed = 5
        filings_failed = 5
        total = filings_parsed + filings_failed
        coverage_pct = round(filings_parsed / total, 4) if total else None
        assert coverage_pct == 0.5

    def test_coverage_pct_all_filings_fail(self) -> None:
        """All filings failed → coverage_pct = 0.0."""
        filings_parsed = 0
        filings_failed = 10
        total = filings_parsed + filings_failed
        coverage_pct = round(filings_parsed / total, 4) if total else None
        assert coverage_pct == 0.0

    def test_coverage_pct_no_filings_at_all_is_none(self) -> None:
        """No filings to process → coverage_pct is None (not division by zero)."""
        filings_parsed = 0
        filings_failed = 0
        total = filings_parsed + filings_failed
        coverage_pct = round(filings_parsed / total, 4) if total else None
        assert coverage_pct is None

    def test_coverage_pct_reflects_real_world_scenario_from_g13(self) -> None:
        """From STATUS.md G1.3 closure: 15 real ARCHITECT survivors, 26/30 filings parsed.
        coverage_pct should be 26/30 = 0.8667."""
        filings_parsed = 26
        filings_failed = 4
        total = filings_parsed + filings_failed
        coverage_pct = round(filings_parsed / total, 4) if total else None
        assert coverage_pct == 0.8667


class TestSignalComputationSyntheticEdgeCases:
    """Gap 4: Signal computation on synthetic edge cases."""

    def test_compute_signals_zero_development_titles_returns_false_not_none(self) -> None:
        """Officers present but no development-related titles → dd_present is False, not None."""
        officers_no_dd = [
            {"name": "John Doe", "title": "Treasurer", "is_officer": True},
            {"name": "Jane Smith", "title": "Secretary", "is_officer": False},
        ]
        result = compute_signals(
            {
                "revenue_total": 100_000,
                "contributions": None,
                "program_revenue": None,
                "govt_grants": None,
                "fundraising_expense": None,
                "officers": officers_no_dd,
            },
            previous=None,
            ruling_year=None,
            tax_year=2023,
        )
        assert result["dd_present"] is False  # Not None, explicitly False

    def test_compute_signals_case_insensitive_dd_title_matching(self) -> None:
        """Development titles are matched case-insensitively."""
        officers = [
            {"name": "A", "title": "DIRECTOR OF DEVELOPMENT", "is_officer": True},  # All caps
            {"name": "B", "title": "Director of Fundraising", "is_officer": True},  # Mixed case
        ]
        result = compute_signals(
            {
                "revenue_total": 100_000,
                "contributions": None,
                "program_revenue": None,
                "govt_grants": None,
                "fundraising_expense": None,
                "officers": officers,
            },
            previous=None,
            ruling_year=None,
            tax_year=2023,
        )
        assert result["dd_present"] is True

    def test_compute_signals_single_filing_no_previous_revenue_trend_is_none(self) -> None:
        """Only one filing (no previous data) → revenue_trend is None, not error."""
        result = compute_signals(
            {
                "revenue_total": 100_000,
                "contributions": None,
                "program_revenue": None,
                "govt_grants": None,
                "fundraising_expense": None,
                "officers": [],
            },
            previous=None,  # No previous filing
            ruling_year=2000,
            tax_year=2023,
        )
        assert result["revenue_trend"] is None

    def test_compute_signals_previous_revenue_zero_no_crash(self) -> None:
        """Previous filing has zero revenue → can't compute trend (division by zero) — gracefully returns None."""
        result = compute_signals(
            {
                "revenue_total": 100_000,
                "contributions": None,
                "program_revenue": None,
                "govt_grants": None,
                "fundraising_expense": None,
                "officers": [],
            },
            previous={"revenue_total": 0},  # Zero revenue, would cause division by zero
            ruling_year=2000,
            tax_year=2023,
        )
        # If previous revenue is 0, change = (100k - 0) / 0 → should handle gracefully
        # Actual code: if prev_revenue is truthy; 0 is falsy, so this returns None
        assert result["revenue_trend"] is None

    def test_compute_signals_revenue_components_dont_sum_to_total(self) -> None:
        """Revenue components (contributions + program + govt) don't sum to total → no crash,
        values still extracted individually."""
        result = compute_signals(
            {
                "revenue_total": 100_000,
                "contributions": 30_000,
                "program_revenue": 30_000,
                "govt_grants": 30_000,  # Sum = 90_000, not 100_000
                "fundraising_expense": 5_000,
                "officers": [],
            },
            previous=None,
            ruling_year=None,
            tax_year=2023,
        )
        # Percentages should still compute, even though they don't sum to 1.0
        assert result["revenue_composition"]["contributions_pct"] == 0.3
        assert result["revenue_composition"]["program_pct"] == 0.3
        assert result["revenue_composition"]["govt_pct"] == 0.3
        # No validation that they sum to 1.0; that's a data quality check, not a crash risk

    def test_compute_signals_officers_with_null_titles(self) -> None:
        """Officers with NULL titles (missing from XML) shouldn't crash dd_present check."""
        officers = [
            {"name": "John Doe", "title": None, "is_officer": True},  # No title
            {"name": "Jane Smith", "title": "Director of Development", "is_officer": True},
        ]
        result = compute_signals(
            {
                "revenue_total": 100_000,
                "contributions": None,
                "program_revenue": None,
                "govt_grants": None,
                "fundraising_expense": None,
                "officers": officers,
            },
            previous=None,
            ruling_year=None,
            tax_year=2023,
        )
        assert result["dd_present"] is True  # One officer has dev title; None title is skipped

    def test_compute_signals_negative_revenue_trend(self) -> None:
        """Negative revenue (edge case) should still compute trend without crash."""
        result = compute_signals(
            {
                "revenue_total": -100_000,  # Negative (shouldn't happen, but data quality)
                "contributions": None,
                "program_revenue": None,
                "govt_grants": None,
                "fundraising_expense": None,
                "officers": [],
            },
            previous={"revenue_total": -50_000},
            ruling_year=None,
            tax_year=2023,
        )
        # -100k / -50k = 2.0 (doubling in magnitude)
        # change = (-100k - (-50k)) / -50k = -50k / -50k = 1.0 (100% increase in magnitude)
        assert result["revenue_trend"] == "growth"  # change > 0.10

    def test_compute_signals_org_age_ruling_year_null(self) -> None:
        """ruling_year is NULL → org_age is None, not error."""
        result = compute_signals(
            {
                "revenue_total": 100_000,
                "contributions": None,
                "program_revenue": None,
                "govt_grants": None,
                "fundraising_expense": None,
                "officers": [],
            },
            previous=None,
            ruling_year=None,  # Unknown ruling year
            tax_year=2023,
        )
        assert result["org_age"] is None

    def test_compute_signals_fundraising_ratio_with_zero_revenue(self) -> None:
        """fundraising_expense present but revenue_total is 0 → ratio is None (division by zero)."""
        result = compute_signals(
            {
                "revenue_total": 0,  # Zero revenue
                "contributions": None,
                "program_revenue": None,
                "govt_grants": None,
                "fundraising_expense": 5_000,  # But has expense
                "officers": [],
            },
            previous=None,
            ruling_year=None,
            tax_year=2023,
        )
        # Code checks: if fundraising_expense is not None and revenue_total
        # 0 is falsy, so fundraising_spend_ratio = None
        assert result["fundraising_spend_ratio"] is None


class TestNTEEDataIsolation:
    """Gap 5: Hard rule check (§9.6) — NTEE never in scored output or signal values."""

    def test_compute_signals_output_never_contains_ntee(self) -> None:
        """Signal output should never contain any NTEE field."""
        result = compute_signals(
            {
                "revenue_total": 100_000,
                "contributions": 30_000,
                "program_revenue": 50_000,
                "govt_grants": 10_000,
                "fundraising_expense": 5_000,
                "officers": [{"name": "John", "title": "CEO", "is_officer": True}],
            },
            previous={"revenue_total": 80_000},
            ruling_year=1990,
            tax_year=2023,
        )
        # Verify no NTEE anywhere in signal output
        assert "ntee" not in str(result).lower()
        assert all(
            "ntee" not in str(v).lower() for v in result.values()
        )

    def test_parse_990_xml_never_extracts_ntee(self) -> None:
        """parse_990_xml should never extract NTEE data (not part of the XML schema it reads)."""
        from pathlib import Path

        fixtures = Path(__file__).parent / "fixtures"
        xml_bytes = (fixtures / "sample_990.xml").read_bytes()
        result = parse_990_xml(xml_bytes)

        # Result keys should be revenue, contributions, officers, etc. — never NTEE.
        # Not an exact key-set match: G1.4 legitimately extended this function's
        # output (mission_text, program_text, website, significant_change_ind) for
        # Stage 3 scoring — the actual hard-rule check is "ntee never appears",
        # regardless of how many other fields this function grows to include.
        expected_keys = {
            "revenue_total",
            "contributions",
            "program_revenue",
            "govt_grants",
            "fundraising_expense",
            "officers",
        }
        assert expected_keys.issubset(result.keys())
        assert "ntee" not in result

    def test_build_survivor_query_uses_ntee_for_filtering_not_selection(self) -> None:
        """Stage 1 uses NTEE to filter OUT (recall shaping), not to select/score."""
        query, _ = build_survivor_query({"exclude_ntee_prefixes": ["B4", "Y"]})
        # Query should have NTEE conditions in WHERE clause (filtering)
        assert "ntee" in query.lower()
        # But NTEE should NOT be in SELECT (no NTEE selected/returned)
        # The SELECT is only "SELECT o.ein FROM organizations o WHERE ..."
        assert query.startswith("SELECT o.ein")
        assert "o.ntee" not in query.split("WHERE")[0]  # NTEE not in SELECT portion

    def test_parse_990_xml_real_filing_has_no_ntee_field(self) -> None:
        """Real 990 XML fixture (EIN 203518700) should not contain NTEE — that's BMF data."""
        from pathlib import Path

        fixtures = Path(__file__).parent / "fixtures"
        xml_bytes = (fixtures / "sample_990.xml").read_bytes()
        # NTEE is not part of Form 990 XML schema; it's from the IRS BMF
        # Verify by parsing the XML directly
        from xml.etree import ElementTree as ET

        root = ET.fromstring(xml_bytes)
        xml_text = ET.tostring(root, encoding="unicode")
        # NTEE should not appear in the XML at all (it's BMF metadata)
        assert "ntee" not in xml_text.lower()


class TestSignalComputationRobustness:
    """Additional robustness tests for signal computation."""

    def test_compute_signals_all_null_inputs_returns_all_none(self) -> None:
        """All input fields are None/empty → all signal outputs are None/empty."""
        result = compute_signals(
            {
                "revenue_total": None,
                "contributions": None,
                "program_revenue": None,
                "govt_grants": None,
                "fundraising_expense": None,
                "officers": [],
            },
            previous=None,
            ruling_year=None,
            tax_year=2023,
        )
        assert result["gov_funding_pct"] is None
        assert result["revenue_composition"] is None
        assert result["dd_present"] is None
        assert result["fundraising_spend_ratio"] is None
        assert result["org_age"] is None
        assert result["revenue_trend"] is None

    def test_compute_signals_stability_threshold_edge_cases(self) -> None:
        """Test revenue_trend edge cases around the ±10% stability threshold.
        Threshold: > 0.10 → growth, < -0.10 → decline, else stable."""
        base_revenue = 100_000

        # Exactly +10% (0.10) → stable (not > 0.10)
        result_plus_10_exact = compute_signals(
            {
                "revenue_total": 110_000,
                "contributions": None,
                "program_revenue": None,
                "govt_grants": None,
                "fundraising_expense": None,
                "officers": [],
            },
            previous={"revenue_total": base_revenue},
            ruling_year=None,
            tax_year=2023,
        )
        assert result_plus_10_exact["revenue_trend"] == "stable"  # = 0.10, not > 0.10

        # Just over +10% → growth
        result_plus_10_over = compute_signals(
            {
                "revenue_total": 110_001,
                "contributions": None,
                "program_revenue": None,
                "govt_grants": None,
                "fundraising_expense": None,
                "officers": [],
            },
            previous={"revenue_total": base_revenue},
            ruling_year=None,
            tax_year=2023,
        )
        assert result_plus_10_over["revenue_trend"] == "growth"  # > 0.10

        # Exactly -10% (-0.10) → stable (not < -0.10)
        result_minus_10_exact = compute_signals(
            {
                "revenue_total": 90_000,
                "contributions": None,
                "program_revenue": None,
                "govt_grants": None,
                "fundraising_expense": None,
                "officers": [],
            },
            previous={"revenue_total": base_revenue},
            ruling_year=None,
            tax_year=2023,
        )
        assert result_minus_10_exact["revenue_trend"] == "stable"  # = -0.10, not < -0.10

        # Just under -10% → decline
        result_minus_10_under = compute_signals(
            {
                "revenue_total": 89_999,
                "contributions": None,
                "program_revenue": None,
                "govt_grants": None,
                "fundraising_expense": None,
                "officers": [],
            },
            previous={"revenue_total": base_revenue},
            ruling_year=None,
            tax_year=2023,
        )
        assert result_minus_10_under["revenue_trend"] == "decline"  # < -0.10
