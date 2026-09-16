from discovery.stages.filter import build_survivor_query


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
