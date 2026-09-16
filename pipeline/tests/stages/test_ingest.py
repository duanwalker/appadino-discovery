from discovery.stages.ingest import (
    latest_two_per_ein,
    transform_bmf_row,
    transform_index_row,
)


def test_transform_bmf_row_maps_fields() -> None:
    raw = {
        "EIN": "000019818",
        "NAME": "PALMER SECOND BAPTIST CHURCH",
        "STATE": "MA",
        "CITY": "PALMER",
        "NTEE_CD": "",
        "RULING": "195504",
        "REVENUE_AMT": "0",
        "FOUNDATION": "10",
    }
    result = transform_bmf_row(raw)
    assert result == {
        "ein": "000019818",
        "name": "PALMER SECOND BAPTIST CHURCH",
        "state": "MA",
        "city": "PALMER",
        "ntee": None,
        "ruling_year": 1955,
        "revenue_latest": 0,
        "foundation_code": "10",
    }


def test_transform_bmf_row_requires_ein() -> None:
    assert transform_bmf_row({"EIN": "", "NAME": "No EIN Org"}) is None


def test_transform_bmf_row_handles_missing_ruling_and_revenue() -> None:
    result = transform_bmf_row({"EIN": "123456789", "NAME": "New Org", "RULING": "", "REVENUE_AMT": ""})
    assert result is not None
    assert result["ruling_year"] is None
    assert result["revenue_latest"] is None


def test_transform_index_row_accepts_990_and_990ez() -> None:
    raw = {
        "EIN": "131624099",
        "TAX_PERIOD": "202306",
        "RETURN_TYPE": "990",
        "OBJECT_ID": "202340189349301104",
    }
    result = transform_index_row(raw, sub_year=2023)
    assert result == {
        "ein": "131624099",
        "tax_year": 2023,
        "form_type": "990",
        "object_id": "202340189349301104",
        "xml_object_url": "https://apps.irs.gov/pub/epostcard/990/xml/2023/",
    }


def test_transform_index_row_rejects_990pf_and_990t() -> None:
    base = {"EIN": "131624099", "TAX_PERIOD": "202306", "OBJECT_ID": "1"}
    assert transform_index_row({**base, "RETURN_TYPE": "990PF"}, 2023) is None
    assert transform_index_row({**base, "RETURN_TYPE": "990T"}, 2023) is None


def test_transform_index_row_rejects_incomplete_rows() -> None:
    assert transform_index_row({"EIN": "", "TAX_PERIOD": "202306", "RETURN_TYPE": "990", "OBJECT_ID": "1"}, 2023) is None
    assert transform_index_row({"EIN": "1", "TAX_PERIOD": "", "RETURN_TYPE": "990", "OBJECT_ID": "1"}, 2023) is None
    assert transform_index_row({"EIN": "1", "TAX_PERIOD": "202306", "RETURN_TYPE": "990", "OBJECT_ID": ""}, 2023) is None


def test_latest_two_per_ein_keeps_only_two_most_recent() -> None:
    rows = [
        {"ein": "1", "tax_year": 2021},
        {"ein": "1", "tax_year": 2023},
        {"ein": "1", "tax_year": 2022},
        {"ein": "2", "tax_year": 2020},
    ]
    result = latest_two_per_ein(rows)
    by_ein: dict[str, list[int]] = {}
    for row in result:
        by_ein.setdefault(row["ein"], []).append(row["tax_year"])
    assert sorted(by_ein["1"]) == [2022, 2023]
    assert by_ein["2"] == [2020]
