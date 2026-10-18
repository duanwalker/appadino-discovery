"""Customer-facing CSV export: header contract, new columns, blank-not-invented
behavior, and escaping of free-text citations. Exercises the real export_csv route
end to end (DB connection faked), so what's asserted is the exact bytes a customer
downloads, not just the intermediate dict."""

from __future__ import annotations

import codecs
import csv
import io
from decimal import Decimal
from typing import Any, Self

import azure.functions as func
import pytest
from discovery.stages.score import ALIGNMENT_CRITERIA
from discovery.stages.triggers import TRIGGER_PRIORITY
from test_function_app import SAMPLE_ROW

from function_app import (
    ALIGNMENT_CRITERIA_TOTAL,
    CSV_HEADERS,
    EXPORT_QUERY,
    PROSPECT_QUERY,
    TRIGGER_LABELS,
    export_csv,
    list_prospects,
)

EXPECTED_HEADERS = [
    "EIN",
    "Organization",
    "City",
    "State",
    "Revenue (most recent 990)",
    "Gap rank",
    "Alignment (n/6)",
    "Qualifies",
    "Trigger",
    "Trigger angle",
    "Values: Population served",
    "Values: Leadership",
    "Development director present",
    "Fundraising spend ratio",
    "Website",
    "Suppression flag",
    "Contact name",
    "Contact title",
    "Contact email",
    "Contact phone",
    "Contact status",
]


class _Cursor:
    def __init__(self, rows: list[dict[str, Any]], executed: list[str]) -> None:
        self._rows = rows
        self._executed = executed

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, sql: str, params: object = None) -> None:
        self._executed.append(sql)

    def fetchall(self) -> list[dict[str, Any]]:
        return self._rows


class _Connection:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.executed: list[str] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def cursor(self) -> _Cursor:
        return _Cursor(self._rows, self.executed)


def _export(monkeypatch: pytest.MonkeyPatch, rows: list[dict[str, Any]]) -> tuple[str, list[dict[str, str]]]:
    monkeypatch.setattr("function_app._conn", lambda: _Connection(rows))
    response = export_csv(
        func.HttpRequest(method="GET", url="/api/export.csv", params={"client_id": "2"}, body=b"")
    )
    assert response.status_code == 200
    text = response.get_body().decode("utf-8-sig")
    return text, list(csv.DictReader(io.StringIO(text)))


def _row(**overrides: Any) -> dict[str, Any]:
    return {**SAMPLE_ROW, **overrides}


def test_header_list_and_order_exact(monkeypatch: pytest.MonkeyPatch) -> None:
    text, _ = _export(monkeypatch, [])
    assert next(csv.reader(io.StringIO(text))) == EXPECTED_HEADERS
    assert CSV_HEADERS == EXPECTED_HEADERS


def test_alignment_denominator_is_derived_from_the_criteria_framework() -> None:
    assert ALIGNMENT_CRITERIA_TOTAL == len(ALIGNMENT_CRITERIA) == 6


def test_revenue_comes_from_most_recent_990_filing(monkeypatch: pytest.MonkeyPatch) -> None:
    # revenue_latest (BMF) deliberately differs, to pin which source the column uses.
    _, rows = _export(monkeypatch, [_row(filing_revenue_total=Decimal(824132), revenue_latest=Decimal(1))])
    assert rows[0]["Revenue (most recent 990)"] == "824132"


def test_alignment_shows_met_over_total(monkeypatch: pytest.MonkeyPatch) -> None:
    rows_in = [
        _row(alignment={"criteria_met_count": 4, "qualifies": True}),
        _row(alignment={"criteria_met_count": 0, "qualifies": False}),
    ]
    _, rows = _export(monkeypatch, rows_in)
    assert [r["Alignment (n/6)"] for r in rows] == ["4/6", "0/6"]


def test_row_missing_citations_stays_blank_not_invented(monkeypatch: pytest.MonkeyPatch) -> None:
    ungrounded_signals = {
        "leadership_composition": {
            "score": None,
            "rationale": "no self-description",
            "citation": None,
            "needs_human_verification": True,
        },
        # A citation string is present but enforce_hard_rules flagged it — still not grounded.
        "population_served": {
            "score": None,
            "rationale": "r",
            "citation": "serves families",
            "needs_human_verification": True,
        },
    }
    rows_in = [
        _row(
            values_signals=ungrounded_signals,
            filing_revenue_total=None,
            alignment=None,
            capacity=None,
            website=None,
            assigned_trigger=None,
            trigger_angle=None,
            trigger_evidence=None,
        ),
        # No score row at all (LEFT JOIN LATERAL miss).
        _row(values_signals=None),
    ]
    _, rows = _export(monkeypatch, rows_in)
    first = rows[0]
    for header in (
        "Revenue (most recent 990)",
        "Alignment (n/6)",
        "Qualifies",
        "Trigger",
        "Trigger angle",
        "Values: Population served",
        "Development director present",
        "Fundraising spend ratio",
        "Website",
    ):
        assert first[header] == "", header
    assert first["Values: Leadership"] == "needs human confirmation"
    assert rows[1]["Values: Population served"] == ""
    assert rows[1]["Values: Leadership"] == "needs human confirmation"


def test_grounded_leadership_citation_is_quoted_not_replaced(monkeypatch: pytest.MonkeyPatch) -> None:
    signals = {
        "leadership_composition": {
            "score": 80,
            "rationale": "r",
            "citation": "We are a Black-led organization serving Portland",
            "needs_human_verification": False,
        }
    }
    row = _row(
        values_signals=signals,
        filing_mission_text="We are a Black-led organization serving Portland since 1990.",
    )
    _, rows = _export(monkeypatch, [row])
    assert rows[0]["Values: Leadership"] == "We are a Black-led organization serving Portland"
    assert rows[0]["Values: Population served"] == ""


def test_flagged_verified_citation_not_found_in_990_text_stays_blank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rows scored before the 2026-09-18 grounding check carry
    needs_human_verification=false on citations nobody verified. The export
    re-checks the quote against the stored 990 text and blanks it if it isn't there."""
    signals = {
        "population_served": {
            "score": 85,
            "rationale": "r",
            "citation": "Serves over 10,000 veterans across New England each year",
            "needs_human_verification": False,
        },
        "leadership_composition": {
            "score": 70,
            "rationale": "r",
            "citation": "mission_text: '...'",
            "needs_human_verification": False,
        },
    }
    row = _row(
        values_signals=signals,
        filing_mission_text="Provides after-school tutoring to children in Lewiston.",
        filing_program_text=[{"desc": "Summer reading camp for elementary students."}],
    )
    _, rows = _export(monkeypatch, [row])
    assert rows[0]["Values: Population served"] == ""
    assert rows[0]["Values: Leadership"] == "needs human confirmation"


def test_population_citation_grounded_in_program_text(monkeypatch: pytest.MonkeyPatch) -> None:
    citation = "Summer reading camp for elementary students"
    signals = {
        "population_served": {"score": 85, "rationale": "r", "citation": citation, "needs_human_verification": False},
    }
    row = _row(
        values_signals=signals,
        filing_mission_text="Provides after-school tutoring to children in Lewiston.",
        filing_program_text=[{"desc": "Summer reading camp for elementary students.", "expense": 1000}],
    )
    _, rows = _export(monkeypatch, [row])
    assert rows[0]["Values: Population served"] == citation


def test_trigger_label_mapping_covers_every_trigger_type() -> None:
    assert TRIGGER_LABELS == {
        "new_ed": "New executive director",
        "dd_departure": "Development director departed",
        "transformational_revenue_jump": "Major revenue increase",
        "first_filing_above_floor": "First filing above revenue floor",
    }
    # Every code triggers.py can assign has a label (no silent fall-through to a code).
    assert set(TRIGGER_PRIORITY) == set(TRIGGER_LABELS)


def test_trigger_codes_become_readable_labels(monkeypatch: pytest.MonkeyPatch) -> None:
    codes = ["new_ed", "transformational_revenue_jump", "first_filing_above_floor", "dd_departure"]
    _, rows = _export(monkeypatch, [_row(assigned_trigger=code) for code in codes])
    assert [r["Trigger"] for r in rows] == [
        "New executive director",
        "Major revenue increase",
        "First filing above revenue floor",
        "Development director departed",
    ]


def test_unknown_trigger_code_passes_through_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    _, rows = _export(monkeypatch, [_row(assigned_trigger="board_turnover"), _row(assigned_trigger=None)])
    assert rows[0]["Trigger"] == "board_turnover"
    assert rows[1]["Trigger"] == ""


def test_booleans_become_yes_no_and_blank_stays_blank(monkeypatch: pytest.MonkeyPatch) -> None:
    rows_in = [
        _row(alignment={"criteria_met_count": 3, "qualifies": True}, capacity={"dd_present": True}),
        _row(alignment={"criteria_met_count": 2, "qualifies": False}, capacity={"dd_present": False}),
        _row(alignment={"criteria_met_count": 2}, capacity={"dd_present": None}),
    ]
    _, rows = _export(monkeypatch, rows_in)
    assert [(r["Qualifies"], r["Development director present"]) for r in rows] == [
        ("Yes", "Yes"),
        ("No", "No"),
        ("", ""),
    ]


def test_quotes_commas_and_newlines_in_quoted_text_are_escaped(monkeypatch: pytest.MonkeyPatch) -> None:
    tricky = 'Serves "at-risk" youth, families,\nand elders'
    signals = {
        "population_served": {"score": 90, "rationale": "r", "citation": tricky, "needs_human_verification": False},
        "leadership_composition": {
            "score": 75,
            "rationale": "r",
            "citation": 'Our board is "women-led", by design',
            "needs_human_verification": False,
        },
    }
    row = _row(
        values_signals=signals,
        org_name='Smith, Jones & "Co"',
        filing_mission_text='Serves "at-risk" youth, families, and elders. Our board is "women-led", by design.',
    )
    text, rows = _export(monkeypatch, [row])
    assert '"Serves ""at-risk"" youth, families,\nand elders"' in text
    assert rows[0]["Values: Population served"] == tricky
    assert rows[0]["Values: Leadership"] == 'Our board is "women-led", by design'
    assert rows[0]["Organization"] == 'Smith, Jones & "Co"'
    assert len(rows) == 1
    assert len(rows[0]) == len(EXPECTED_HEADERS)


def test_locked_contact_cells_keep_enrich_to_unlock_text(monkeypatch: pytest.MonkeyPatch) -> None:
    _, rows = _export(monkeypatch, [_row(enr_id=None)])
    assert rows[0]["Contact email"] == rows[0]["Contact phone"] == rows[0]["Contact status"] == "Enrich to unlock"
    assert rows[0]["Contact name"] == "Erin K Flood"


_FILING_TEXT_MARKERS = ("mission_text", "program_text", "filing_", "filings f3", "rf ON true")


def _executed_sql(monkeypatch: pytest.MonkeyPatch, route: Any) -> str:
    conn = _Connection([])
    monkeypatch.setattr("function_app._conn", lambda: conn)
    response = route(func.HttpRequest(method="GET", url="/api/x", params={"client_id": "2"}, body=b""))
    assert response.status_code == 200
    assert len(conn.executed) == 1
    return conn.executed[0]


def test_list_endpoint_query_never_loads_990_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """The dashboard list runs on every page load; 990 mission/program text is
    export-only (it can be ~50 KB per org) and must not ride along."""
    sql = _executed_sql(monkeypatch, list_prospects)
    assert sql == PROSPECT_QUERY
    for marker in _FILING_TEXT_MARKERS:
        assert marker not in sql, marker


def test_export_query_loads_990_text_and_revenue(monkeypatch: pytest.MonkeyPatch) -> None:
    sql = _executed_sql(monkeypatch, export_csv)
    assert sql == EXPORT_QUERY
    for marker in _FILING_TEXT_MARKERS:
        assert marker in sql, marker
    assert "rf.revenue_total AS filing_revenue_total" in sql


def test_response_starts_with_a_single_utf8_bom(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("function_app._conn", lambda: _Connection([_row()]))
    response = export_csv(
        func.HttpRequest(method="GET", url="/api/export.csv", params={"client_id": "2"}, body=b"")
    )
    body = response.get_body()
    assert body.startswith(codecs.BOM_UTF8)
    assert body.count(codecs.BOM_UTF8) == 1
    assert response.charset.lower() == "utf-8"
    # Header row right after the BOM, so Excel shows "EIN" (not "﻿EIN") as the first header.
    assert body[len(codecs.BOM_UTF8) :].startswith(b"EIN,Organization,")


@pytest.mark.parametrize(
    "prefix", ["=", "+", "-", "@", "\t", "\r"], ids=["equals", "plus", "minus", "at", "tab", "cr"]
)
def test_each_formula_prefix_gets_a_single_quote(monkeypatch: pytest.MonkeyPatch, prefix: str) -> None:
    payload = f"{prefix}HYPERLINK(\"http://evil.example\",\"x\")"
    _, rows = _export(monkeypatch, [_row(org_name=payload, website=payload)])
    assert rows[0]["Organization"] == "'" + payload
    assert rows[0]["Website"] == "'" + payload


def test_plain_text_cells_are_not_quoted(monkeypatch: pytest.MonkeyPatch) -> None:
    _, rows = _export(monkeypatch, [_row(org_name="Day One", website="https://dayoneme.org")])
    assert rows[0]["Organization"] == "Day One"
    assert rows[0]["Website"] == "https://dayoneme.org"


@pytest.mark.parametrize("phone", ["+1 207-671-2868", "+1 (207) 671.2868", "207-671-2868", "-2076712868"])
def test_phone_number_starting_with_plus_or_dash_is_exempt(monkeypatch: pytest.MonkeyPatch, phone: str) -> None:
    _, rows = _export(monkeypatch, [_row(enr_id=7, enr_email="a@b.org", enr_email_status="verified", enr_phone=phone)])
    assert rows[0]["Contact phone"] == phone


@pytest.mark.parametrize("phone", ["+1 207 =cmd", "+SUM(1,2)", "+1\t207", "++", "-"])
def test_phone_cell_that_is_not_strictly_a_phone_number_is_neutralized(
    monkeypatch: pytest.MonkeyPatch, phone: str
) -> None:
    _, rows = _export(monkeypatch, [_row(enr_id=7, enr_email="a@b.org", enr_email_status="verified", enr_phone=phone)])
    assert rows[0]["Contact phone"] == "'" + phone


def test_phone_exemption_applies_only_to_the_phone_column(monkeypatch: pytest.MonkeyPatch) -> None:
    _, rows = _export(monkeypatch, [_row(org_name="+1 207-671-2868")])
    assert rows[0]["Organization"] == "'+1 207-671-2868"


def test_negative_numbers_are_not_quoted(monkeypatch: pytest.MonkeyPatch) -> None:
    row = _row(
        filing_revenue_total=Decimal("-48210.00"),
        gap_rank=Decimal("-1.5"),
        capacity={"dd_present": False, "fundraising_spend_ratio": -0.02},
    )
    _, rows = _export(monkeypatch, [row])
    assert rows[0]["Revenue (most recent 990)"] == "-48210.00"
    assert rows[0]["Gap rank"] == "-1.5"
    assert rows[0]["Fundraising spend ratio"] == "-0.02"


def test_grounded_citation_starting_with_formula_char_is_quoted(monkeypatch: pytest.MonkeyPatch) -> None:
    citation = "-Serves 1,100 Maine children whose families face hardship"
    signals = {
        "population_served": {"score": 80, "rationale": "r", "citation": citation, "needs_human_verification": False},
        "leadership_composition": {
            "score": 70,
            "rationale": "r",
            "citation": "@Our board is women-led by design",
            "needs_human_verification": False,
        },
    }
    row = _row(
        values_signals=signals,
        filing_mission_text="Serves 1,100 Maine children whose families face hardship. Our board is women-led by design.",
    )
    text, rows = _export(monkeypatch, [row])
    assert rows[0]["Values: Population served"] == "'" + citation
    assert rows[0]["Values: Leadership"] == "'@Our board is women-led by design"
    # Still a single, correctly escaped CSV field (the citation contains commas).
    assert f'"\'{citation}"' in text


@pytest.mark.parametrize(
    "count,expected",
    [(0, "0/6"), (6, "6/6"), (7, ""), (-1, ""), (100, ""), (True, ""), (2.0, ""), ("3", "")],
    ids=["zero", "max", "seven", "negative", "huge", "bool", "float", "string"],
)
def test_alignment_out_of_range_or_wrong_type_is_blank(
    monkeypatch: pytest.MonkeyPatch, count: Any, expected: str
) -> None:
    _, rows = _export(monkeypatch, [_row(alignment={"criteria_met_count": count, "qualifies": False})])
    assert rows[0]["Alignment (n/6)"] == expected


def test_quote_with_invented_tail_is_not_exported(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shared scoring check accepts any 25-character overlap; the export requires
    the whole normalized citation to be present in the 990 text."""
    real = "Provides after-school tutoring to children in Lewiston"
    signals = {
        "population_served": {
            "score": 85,
            "rationale": "r",
            "citation": real + " and guarantees every family a $10,000 grant",
            "needs_human_verification": False,
        },
    }
    row = _row(values_signals=signals, filing_mission_text=real + ".")
    _, rows = _export(monkeypatch, [row])
    assert rows[0]["Values: Population served"] == ""


def test_short_citation_fully_present_is_grounded(monkeypatch: pytest.MonkeyPatch) -> None:
    signals = {
        "population_served": {"score": 85, "rationale": "r", "citation": "rural veterans", "needs_human_verification": False},
    }
    row = _row(values_signals=signals, filing_mission_text="We serve rural veterans in Aroostook County.")
    _, rows = _export(monkeypatch, [row])
    assert rows[0]["Values: Population served"] == "rural veterans"


def _population_cell(monkeypatch: pytest.MonkeyPatch, citation: str, mission: str) -> str:
    signals = {
        "population_served": {"score": 85, "rationale": "r", "citation": citation, "needs_human_verification": False},
    }
    _, rows = _export(monkeypatch, [_row(values_signals=signals, filing_mission_text=mission)])
    return rows[0]["Values: Population served"]


@pytest.mark.parametrize(
    "citation,mission",
    [("men", "Shelter and counseling for women"), ("art", "A neighborhood block party every summer")],
    ids=["men-inside-women", "art-inside-party"],
)
def test_citation_matching_only_inside_a_word_is_not_grounded(
    monkeypatch: pytest.MonkeyPatch, citation: str, mission: str
) -> None:
    assert _population_cell(monkeypatch, citation, mission) == ""


def test_whole_word_citation_is_grounded(monkeypatch: pytest.MonkeyPatch) -> None:
    mission = "Shelter and counseling for women and men leaving incarceration"
    assert _population_cell(monkeypatch, "men leaving incarceration", mission) == "men leaving incarceration"


@pytest.mark.parametrize(
    "citation",
    ["Shelter and counseling", "women and men leaving incarceration."],
    ids=["very-start", "very-end"],
)
def test_citation_at_corpus_start_or_end_is_grounded(monkeypatch: pytest.MonkeyPatch, citation: str) -> None:
    mission = "Shelter and counseling for women and men leaving incarceration."
    assert _population_cell(monkeypatch, citation, mission) == citation


def test_citation_spanning_mission_and_program_text_boundary_is_word_aligned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mission and program text are joined with a space, so the last mission word and
    the first program word are separate words, not one glued token."""
    signals = {
        "population_served": {"score": 85, "rationale": "r", "citation": "elders", "needs_human_verification": False},
    }
    row = _row(
        values_signals=signals,
        filing_mission_text="Meals for elder",
        filing_program_text=[{"desc": "s and their caregivers"}],
    )
    _, rows = _export(monkeypatch, [row])
    assert rows[0]["Values: Population served"] == ""
