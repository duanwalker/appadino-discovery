from pathlib import Path
from xml.etree import ElementTree as ET

from discovery.stages.extract_signals import (
    _extract_website,
    _strip_namespaces,
    _year_from_archive_url,
    compute_signals,
    parse_990_xml,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _irs990_from(inner_xml: str) -> ET.Element:
    root = ET.fromstring(
        f'<Return xmlns="http://www.irs.gov/efile"><ReturnData><IRS990>{inner_xml}'
        "</IRS990></ReturnData></Return>"
    )
    _strip_namespaces(root)
    irs990 = root.find("ReturnData/IRS990")
    assert irs990 is not None
    return irs990


def test_extract_website_filters_placeholders() -> None:
    for placeholder in ("NONE", "N/A", "n/a", "-", "NA"):
        assert _extract_website(_irs990_from(f"<WebsiteAddressTxt>{placeholder}</WebsiteAddressTxt>")) is None


def test_extract_website_keeps_real_domain() -> None:
    irs990 = _irs990_from("<WebsiteAddressTxt>EXAMPLE.ORG</WebsiteAddressTxt>")
    assert _extract_website(irs990) == "EXAMPLE.ORG"


def test_extract_website_missing_tag_returns_none() -> None:
    assert _extract_website(_irs990_from("<SomeOtherTag>x</SomeOtherTag>")) is None


def test_parse_990_xml_real_filing() -> None:
    """Real filing (EIN 203518700, tax year 2021, object_id 202340189349301104) —
    fetched during G1.3 to confirm the field mapping against actual IRS data, not
    just schema docs. Values below are read directly off that filing's Form 990."""
    xml_bytes = (FIXTURES / "sample_990.xml").read_bytes()
    result = parse_990_xml(xml_bytes)
    assert result["revenue_total"] == 110520
    assert result["contributions"] == 30035
    assert result["program_revenue"] == 80485
    assert result["govt_grants"] is None  # this org has none — tag absent, not zero
    assert result["fundraising_expense"] == 0
    assert {"name": "Diane Paque", "title": "Executive Director", "is_officer": True} in result[
        "officers"
    ]
    assert len(result["officers"]) == 3

    # G1.4 additions: mission/program text (the sole Stage 3 text source, §4) and the
    # significant-change flag (evidence for the "at an inflection point" alignment
    # criterion) — this filing has no WebsiteAddressTxt at all (only OwnWebsiteInd).
    assert result["mission_text"] is not None
    assert result["mission_text"].startswith("To facitate the integration")
    assert result["website"] is None
    assert result["significant_change_ind"] is True
    assert len(result["program_text"]) == 3
    assert result["program_text"][0] == {
        "desc": (
            "Held two one week long virtual training sessions for United States and "
            "International students specializing in the state between lives as view "
            "from a horizontal viewed of all lives rather than passing though each as "
            "lived in the past with emphasis on relief in the present life."
        ),
        "expense": 43655,
        "revenue": 55935,
    }


def test_parse_990_xml_missing_return_data_is_tolerated() -> None:
    xml_bytes = b'<?xml version="1.0"?><Return xmlns="http://www.irs.gov/efile"></Return>'
    result = parse_990_xml(xml_bytes)
    assert result["revenue_total"] is None
    assert result["officers"] == []


def test_parse_990_xml_raises_on_garbage_bytes() -> None:
    from xml.etree.ElementTree import ParseError

    import pytest

    with pytest.raises(ParseError):
        parse_990_xml(b"not xml at all")


def test_year_from_archive_url() -> None:
    assert _year_from_archive_url("https://apps.irs.gov/pub/epostcard/990/xml/2023/") == 2023
    assert _year_from_archive_url("https://apps.irs.gov/pub/epostcard/990/xml/2023") == 2023


def test_year_from_archive_url_unparseable() -> None:
    assert _year_from_archive_url("https://example.com/no-year-here/") is None


def test_compute_signals_revenue_composition() -> None:
    current = {
        "revenue_total": 100_000,
        "contributions": 40_000,
        "program_revenue": 50_000,
        "govt_grants": 10_000,
        "fundraising_expense": 5_000,
        "officers": [],
    }
    result = compute_signals(current, previous=None, ruling_year=2000, tax_year=2023)
    assert result["revenue_composition"] == {
        "contributions_pct": 0.4,
        "program_pct": 0.5,
        "govt_pct": 0.1,
    }
    assert result["gov_funding_pct"] == 0.1
    assert result["fundraising_spend_ratio"] == 0.05
    assert result["org_age"] == 23


def test_compute_signals_handles_zero_revenue() -> None:
    current = {
        "revenue_total": 0,
        "contributions": None,
        "program_revenue": None,
        "govt_grants": None,
        "fundraising_expense": None,
        "officers": [],
    }
    result = compute_signals(current, previous=None, ruling_year=None, tax_year=2023)
    assert result["revenue_composition"] is None
    assert result["gov_funding_pct"] is None
    assert result["org_age"] is None


def test_compute_signals_dd_present_true_and_false() -> None:
    with_dd = {
        "revenue_total": 1,
        "contributions": None,
        "program_revenue": None,
        "govt_grants": None,
        "fundraising_expense": None,
        "officers": [{"name": "A", "title": "Director of Development", "is_officer": True}],
    }
    without_dd = {**with_dd, "officers": [{"name": "B", "title": "Treasurer", "is_officer": True}]}
    no_officers = {**with_dd, "officers": []}

    assert compute_signals(with_dd, None, None, 2023)["dd_present"] is True
    assert compute_signals(without_dd, None, None, 2023)["dd_present"] is False
    assert compute_signals(no_officers, None, None, 2023)["dd_present"] is None


def test_compute_signals_revenue_trend() -> None:
    tax_year = 2023
    growth = compute_signals(
        {"revenue_total": 150_000, "contributions": None, "program_revenue": None,
         "govt_grants": None, "fundraising_expense": None, "officers": []},
        previous={"revenue_total": 100_000},
        ruling_year=None,
        tax_year=tax_year,
    )
    decline = compute_signals(
        {"revenue_total": 50_000, "contributions": None, "program_revenue": None,
         "govt_grants": None, "fundraising_expense": None, "officers": []},
        previous={"revenue_total": 100_000},
        ruling_year=None,
        tax_year=tax_year,
    )
    stable = compute_signals(
        {"revenue_total": 102_000, "contributions": None, "program_revenue": None,
         "govt_grants": None, "fundraising_expense": None, "officers": []},
        previous={"revenue_total": 100_000},
        ruling_year=None,
        tax_year=tax_year,
    )
    no_prior = compute_signals(
        {"revenue_total": 100_000, "contributions": None, "program_revenue": None,
         "govt_grants": None, "fundraising_expense": None, "officers": []},
        previous=None,
        ruling_year=None,
        tax_year=tax_year,
    )
    assert growth["revenue_trend"] == "growth"
    assert decline["revenue_trend"] == "decline"
    assert stable["revenue_trend"] == "stable"
    assert no_prior["revenue_trend"] is None
