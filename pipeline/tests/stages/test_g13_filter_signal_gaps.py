"""Independent G1.3 coverage review tests for Stage 1 filters + Stage 2 signals.

These tests avoid the already-audited foundation_code behavior and probe the actual
Stage 1 SQL builder plus Stage 2 parser/signal runner with synthetic boundary data.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any
from xml.etree.ElementTree import ParseError

import pytest

from discovery.stages import extract_signals
from discovery.stages.extract_signals import compute_signals, parse_990_xml
from discovery.stages.filter import build_survivor_query
from discovery.stages.score import compute_soft_flags
from discovery.stages.triggers import detect_triggers

EMPTY_PARSED = {
    "revenue_total": None,
    "contributions": None,
    "program_revenue": None,
    "govt_grants": None,
    "fundraising_expense": None,
    "officers": [],
    "mission_text": None,
    "program_text": [],
    "website": None,
    "significant_change_ind": None,
}


def test_ntee_exclude_prefixes_are_supplied_by_client_config() -> None:
    query, params = build_survivor_query({"exclude_ntee_prefixes": ["Z9", "Q"]})

    assert "o.ntee LIKE %(ntee_prefix_0)s" in query
    assert "o.ntee LIKE %(ntee_prefix_1)s" in query
    assert params["ntee_prefix_0"] == "Z9%"
    assert params["ntee_prefix_1"] == "Q%"
    assert "B4%" not in params.values()
    assert "B5%" not in params.values()
    assert "E2%" not in params.values()
    assert "Y%" not in params.values()


def test_empty_ntee_exclude_prefix_config_removes_ntee_filter() -> None:
    query, params = build_survivor_query({"exclude_ntee_prefixes": []})

    assert "o.ntee" not in query
    assert not any(key.startswith("ntee_prefix_") for key in params)


def test_revenue_floor_and_ceiling_defaults_are_inclusive() -> None:
    query, params = build_survivor_query({})

    assert "o.revenue_latest >= %(revenue_floor)s" in query
    assert "o.revenue_latest <= %(revenue_ceiling)s" in query
    assert params["revenue_floor"] == 500_000
    assert params["revenue_ceiling"] == 10_000_000


def test_revenue_floor_and_ceiling_custom_overrides_keep_inclusive_edges() -> None:
    query, params = build_survivor_query({"revenue_floor": 750_000, "revenue_ceiling": 3_500_000})

    assert "o.revenue_latest >= %(revenue_floor)s" in query
    assert "o.revenue_latest <= %(revenue_ceiling)s" in query
    assert params["revenue_floor"] == 750_000
    assert params["revenue_ceiling"] == 3_500_000


def test_geography_tiers_are_not_stage1_filter_walls() -> None:
    query, params = build_survivor_query(
        {
            "geography_tiers": {"priority_metros": ["Charlotte", "Cincinnati"]},
            "priority_metros": ["Charlotte", "Cincinnati"],
        }
    )

    assert "city" not in query.lower()
    assert "state" not in query.lower()
    assert "metro" not in query.lower()
    assert "geography" not in query.lower()
    assert all(value != ["Charlotte", "Cincinnati"] for value in params.values())


def test_parse_990ez_returns_nullable_empty_shape_not_fabricated_values() -> None:
    result = parse_990_xml(_return_xml("IRS990EZ", "<TotalRevenueAmt>123</TotalRevenueAmt>"))

    assert result == EMPTY_PARSED



def test_parse_990pf_returns_nullable_empty_shape_not_fabricated_values() -> None:
    result = parse_990_xml(_return_xml("IRS990PF", "<TotalRevenueAmt>123</TotalRevenueAmt>"))

    assert result == EMPTY_PARSED



def test_parse_990_missing_financial_parts_are_nullable() -> None:
    result = parse_990_xml(_return_xml("IRS990", "<MissionDesc>Community programs</MissionDesc>"))

    assert result["revenue_total"] is None
    assert result["contributions"] is None
    assert result["program_revenue"] is None
    assert result["govt_grants"] is None
    assert result["fundraising_expense"] is None
    assert result["mission_text"] == "Community programs"
    assert result["officers"] == []


def test_parse_990_truncated_xml_raises_for_orchestrator_to_count() -> None:
    with pytest.raises(ParseError):
        parse_990_xml(b"<Return><ReturnData><IRS990><MissionDesc>cut off")


def test_revenue_composition_with_missing_total_is_nullable_not_zero() -> None:
    signal = compute_signals(
        _current(revenue_total=None, contributions=10, program_revenue=20, govt_grants=30),
        previous=None,
        ruling_year=None,
        tax_year=2023,
    )

    assert signal["revenue_composition"] is None
    assert signal["gov_funding_pct"] is None


def test_revenue_composition_with_zero_total_avoids_division_by_zero() -> None:
    signal = compute_signals(
        _current(revenue_total=0, contributions=10, program_revenue=20, govt_grants=30),
        previous=None,
        ruling_year=None,
        tax_year=2023,
    )

    assert signal["revenue_composition"] is None
    assert signal["gov_funding_pct"] is None


def test_government_funding_soft_flag_threshold_is_inclusive_at_40_percent() -> None:
    signal = compute_signals(
        _current(revenue_total=1_000_000, contributions=600_000, program_revenue=0, govt_grants=400_000),
        previous=None,
        ruling_year=None,
        tax_year=2023,
    )

    assert signal["gov_funding_pct"] == 0.4
    assert compute_soft_flags(signal["gov_funding_pct"], govt_funding_heavy_pct=0.40) == {
        "heavy_govt_funding": True
    }


def test_government_funding_soft_flag_just_below_threshold_is_false() -> None:
    """RESOLVED: gov_funding_pct is no longer rounded to 4 decimals before the >=
    threshold comparison, so a raw ratio just under 40% (399,999 / 1,000,000 =
    0.399999 — which previously rounded UP to 0.4000 and incorrectly flagged as
    heavy government funding) now correctly compares as below threshold."""
    signal = compute_signals(
        _current(revenue_total=1_000_000, contributions=600_001, program_revenue=0, govt_grants=399_999),
        previous=None,
        ruling_year=None,
        tax_year=2023,
    )

    assert signal["gov_funding_pct"] == pytest.approx(0.399999)
    assert signal["gov_funding_pct"] < 0.4
    assert compute_soft_flags(signal["gov_funding_pct"], govt_funding_heavy_pct=0.40) == {
        "heavy_govt_funding": False
    }
    # revenue_composition["govt_pct"] is still rounded for display/citation — only
    # the top-level gov_funding_pct that feeds the threshold decision is raw.
    assert signal["revenue_composition"]["govt_pct"] == 0.4


def test_fundraising_expense_ratio_with_missing_total_is_nullable() -> None:
    signal = compute_signals(
        _current(revenue_total=None, fundraising_expense=25_000),
        previous=None,
        ruling_year=None,
        tax_year=2023,
    )

    assert signal["fundraising_spend_ratio"] is None


def test_fundraising_expense_ratio_with_zero_total_avoids_division_by_zero() -> None:
    signal = compute_signals(
        _current(revenue_total=0, fundraising_expense=25_000),
        previous=None,
        ruling_year=None,
        tax_year=2023,
    )

    assert signal["fundraising_spend_ratio"] is None


@pytest.mark.parametrize(
    "title",
    [
        "director of development",
        "Director of Development",
        "VP Development",
        "Chief Development Officer",
        "Vice President, Advancement",
        "Fundraising Manager",
    ],
)
def test_development_role_detection_handles_common_title_variants(title: str) -> None:
    signal = compute_signals(
        _current(officers=[{"name": "A", "title": title, "is_officer": True}]),
        previous=None,
        ruling_year=None,
        tax_year=2023,
    )

    assert signal["dd_present"] is True


def test_development_role_detection_now_catches_common_abbreviation() -> None:
    """RESOLVED: "VP Dev" — a real near-miss this review found — is now detected via
    word-boundary matching on "dev" (not a bare substring search, which would also
    false-positive on "IT Developer"/"Device Manager")."""
    signal = compute_signals(
        _current(officers=[{"name": "A", "title": "VP Dev", "is_officer": True}]),
        previous=None,
        ruling_year=None,
        tax_year=2023,
    )

    assert signal["dd_present"] is True


def test_development_abbreviation_match_does_not_over_match_unrelated_titles() -> None:
    """Confirms the word-boundary fix for "dev" doesn't turn into a loose substring
    match that would false-positive on unrelated titles containing "dev"."""
    for title in ("IT Developer", "Device Manager", "Devon Regional Coordinator"):
        signal = compute_signals(
            _current(officers=[{"name": "A", "title": title, "is_officer": True}]),
            previous=None,
            ruling_year=None,
            tax_year=2023,
        )
        assert signal["dd_present"] is False, title


@pytest.mark.parametrize("title", ["Chief Growth Officer", "Donor Relations Lead"])
def test_development_role_detection_still_misses_near_synonyms_left_open(title: str) -> None:
    """Left open by design (TEST-COVERAGE-GAPS.md G1.3 Gap 8 note): these are
    adjacent-fundraising-role synonyms, not abbreviations of the existing keywords —
    out of scope for the abbreviation-pattern fix, unlike "VP Dev" above."""
    signal = compute_signals(
        _current(officers=[{"name": "A", "title": title, "is_officer": True}]),
        previous=None,
        ruling_year=None,
        tax_year=2023,
    )

    assert signal["dd_present"] is False


def test_revenue_trend_growth_boundary_is_strictly_above_10_percent() -> None:
    at_boundary = compute_signals(_current(revenue_total=110_000), {"revenue_total": 100_000}, None, 2023)
    above_boundary = compute_signals(_current(revenue_total=110_001), {"revenue_total": 100_000}, None, 2023)

    assert at_boundary["revenue_trend"] == "stable"
    assert above_boundary["revenue_trend"] == "growth"


def test_revenue_trend_decline_boundary_is_strictly_below_10_percent() -> None:
    at_boundary = compute_signals(_current(revenue_total=90_000), {"revenue_total": 100_000}, None, 2023)
    below_boundary = compute_signals(_current(revenue_total=89_999), {"revenue_total": 100_000}, None, 2023)

    assert at_boundary["revenue_trend"] == "stable"
    assert below_boundary["revenue_trend"] == "decline"


def test_transformational_jump_boundary_lives_in_stage5_not_stage2_current_gap() -> None:
    signal = compute_signals(_current(revenue_total=150_000), {"revenue_total": 100_000}, None, 2023)
    triggers = detect_triggers(
        {"revenue_total": 150_000, "officers": []},
        {"revenue_total": 100_000, "officers": []},
        revenue_floor=500_000,
    )

    assert signal["revenue_trend"] == "growth"
    assert "transformational" not in str(signal).lower()
    assert any(trigger["type"] == "transformational_revenue_jump" for trigger in triggers)


def test_only_one_filing_on_record_has_nullable_comparison_signals() -> None:
    signal = compute_signals(_current(revenue_total=750_000), previous=None, ruling_year=2018, tax_year=2023)

    assert signal["revenue_trend"] is None
    assert signal["org_age"] == 5


def test_stage2_coverage_pct_is_now_survivor_based(monkeypatch: pytest.MonkeyPatch) -> None:
    """RESOLVED: coverage_pct's denominator is now len(eins) (Stage 1's survivor
    count), not filings_parsed + filings_failed. "333333333" is a Stage 1 survivor
    with zero filing rows at all — previously invisible to the old filing-based
    denominator, it now correctly drags coverage down: 1 survivor successfully
    signaled out of 3 total, not 2 filings parsed out of 3 filing attempts."""
    fake_connection = FakeConnection(
        filing_rows=[
            (1, "111111111", 2023, "ok-current", "https://apps.irs.gov/pub/epostcard/990/xml/2024/"),
            (2, "111111111", 2022, "ok-previous", "https://apps.irs.gov/pub/epostcard/990/xml/2023/"),
            (3, "222222222", 2023, "missing", "https://apps.irs.gov/pub/epostcard/990/xml/2024/"),
        ],
        ruling_years={"111111111": 2010, "222222222": 2012, "333333333": 2015},
    )
    monkeypatch.setattr(extract_signals.psycopg, "connect", lambda _database_url: fake_connection)
    monkeypatch.setattr(extract_signals.httpx, "Client", lambda **_kwargs: NullHttpClient())
    monkeypatch.setattr(extract_signals, "build_year_index", lambda _client, _year: {})
    monkeypatch.setattr(
        extract_signals,
        "fetch_filing_xml",
        lambda _year_index, object_id: b"<xml />" if str(object_id).startswith("ok") else None,
    )
    monkeypatch.setattr(
        extract_signals,
        "parse_990_xml",
        lambda _xml_bytes: {
            **EMPTY_PARSED,
            **_current(revenue_total=100_000, contributions=20_000, govt_grants=40_000),
        },
    )

    counts = extract_signals.extract_signals_for_survivors("postgres://example", ["111111111", "222222222", "333333333"])

    assert counts["filings_parsed"] == 2
    assert counts["filings_failed"] == 1
    assert counts["signals_computed"] == 1
    assert counts["coverage_pct"] == 0.3333
    assert counts["coverage_pct"] == round(counts["signals_computed"] / 3, 4)
    # The old (buggy) filing-based formula would have overstated coverage at 0.6667
    # — confirms this isn't just a coincidental match, the denominator actually changed.
    old_filing_based_formula = round(counts["filings_parsed"] / (counts["filings_parsed"] + counts["filings_failed"]), 4)
    assert counts["coverage_pct"] != old_filing_based_formula


def _return_xml(form_tag: str, inner_xml: str) -> bytes:
    return (
        f'<Return xmlns="http://www.irs.gov/efile"><ReturnData><{form_tag}>{inner_xml}'
        f"</{form_tag}></ReturnData></Return>"
    ).encode()


def _current(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "revenue_total": 100_000,
        "contributions": None,
        "program_revenue": None,
        "govt_grants": None,
        "fundraising_expense": None,
        "officers": [],
    }
    data.update(overrides)
    return data


class NullHttpClient(AbstractContextManager["NullHttpClient"]):
    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        return None


class FakeConnection(AbstractContextManager["FakeConnection"]):
    def __init__(self, filing_rows: list[tuple[Any, ...]], ruling_years: dict[str, int]) -> None:
        self.filing_rows = filing_rows
        self.ruling_years = ruling_years
        self.last_sql = ""
        self.last_params: Any = None

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        return None

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        return None


class FakeCursor(AbstractContextManager["FakeCursor"]):
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    def execute(self, sql: str, params: Any = None) -> None:
        self.connection.last_sql = sql
        self.connection.last_params = params

    def fetchall(self) -> list[tuple[Any, ...]]:
        if "FROM filings" in self.connection.last_sql and "object_id" in self.connection.last_sql:
            return self.connection.filing_rows
        return []

    def fetchone(self) -> tuple[int] | None:
        if "SELECT ruling_year" in self.connection.last_sql:
            ein = self.connection.last_params[0]
            ruling_year = self.connection.ruling_years.get(ein)
            return (ruling_year,) if ruling_year is not None else None
        return None

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        return None