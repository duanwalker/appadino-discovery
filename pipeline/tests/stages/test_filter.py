from __future__ import annotations

from typing import Any

from discovery.stages.filter import DEFAULT_RECALL_FILTER, build_survivor_query


def _survives_foundation_code_filter(foundation_code: str | None, params: dict[str, Any]) -> bool:
    """Mirrors the WHERE semantics of the foundation_code condition build_survivor_query
    emits ("IS NULL OR != ALL(excludes)") — lets a synthetic org be evaluated against a
    real generated params dict without needing a live Postgres to run the query."""
    if foundation_code is None:
        return True
    return foundation_code not in params["exclude_foundation_codes"]


def test_build_survivor_query_uses_defaults_when_recall_empty() -> None:
    query, params = build_survivor_query({})
    assert params["revenue_floor"] == 500_000
    assert params["revenue_ceiling"] == 10_000_000
    assert params["exclude_foundation_codes"] == ["00", "02", "03", "04", "12", "13", "14"]
    assert "o.revenue_latest >= %(revenue_floor)s" in query
    assert "EXISTS (SELECT 1 FROM filings f WHERE f.ein = o.ein)" in query


def test_build_survivor_query_overrides_merge_with_defaults() -> None:
    _query, params = build_survivor_query({"revenue_floor": 1_000_000})
    assert params["revenue_floor"] == 1_000_000
    assert params["revenue_ceiling"] == 10_000_000  # untouched default


def test_build_survivor_query_ntee_prefixes_produce_like_conditions() -> None:
    query, params = build_survivor_query({"exclude_ntee_prefixes": ["Y", "B4"]})
    assert params["ntee_prefix_0"] == "Y%"
    assert params["ntee_prefix_1"] == "B4%"
    assert "o.ntee LIKE %(ntee_prefix_0)s" in query
    assert "o.ntee LIKE %(ntee_prefix_1)s" in query


def test_build_survivor_query_empty_ntee_prefixes_omits_ntee_clause() -> None:
    query, _ = build_survivor_query({"exclude_ntee_prefixes": []})
    assert "ntee" not in query.lower()


def test_build_survivor_query_require_filing_false_omits_exists_clause() -> None:
    query, _ = build_survivor_query({"require_filing_on_record": False})
    assert "EXISTS" not in query


def test_excludes_are_read_per_tenant_from_icp_configs_not_hardcoded() -> None:
    """Two icp_configs rows differing only in whether "private_foundation" (BMF
    foundation_code 03) is in recall_filter.exclude_foundation_codes must produce
    different survivor sets — proving Stage 1's excludes are config-driven per
    client, not constants baked into build_survivor_query."""
    private_foundation_org_code = "03"

    icp_config_excludes_private_foundations = {
        "recall_filter": {"exclude_foundation_codes": ["00", "02", "03", "04", "12", "13", "14"]}
    }
    icp_config_without_that_exclude = {
        "recall_filter": {"exclude_foundation_codes": ["00", "12", "13", "14"]}
    }

    _, params_excluding = build_survivor_query(
        icp_config_excludes_private_foundations["recall_filter"]
    )
    _, params_allowing = build_survivor_query(icp_config_without_that_exclude["recall_filter"])

    assert _survives_foundation_code_filter(private_foundation_org_code, params_excluding) is False
    assert _survives_foundation_code_filter(private_foundation_org_code, params_allowing) is True

    # Confirm this genuinely came from the per-config value, not DEFAULT_RECALL_FILTER
    assert params_allowing["exclude_foundation_codes"] != DEFAULT_RECALL_FILTER["exclude_foundation_codes"]

