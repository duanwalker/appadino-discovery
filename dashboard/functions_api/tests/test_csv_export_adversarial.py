"""Independent export probes (Copilot's adversarial suite).

Written so unmet safety expectations failed; after the 2026-10-17 hardening gate the
whole file passes. The one exception is the Arabic (rtl) citation case, a strict
xfail: the shared qa normalizer strips non-Latin text, so it is never grounded
(deferred; see STATUS.md "Deferred / post-v1").

Run from dashboard/functions_api:
    .venv\\Scripts\\python.exe -m pytest tests\\test_csv_export_adversarial.py -v -s

Only the database boundary is replaced. Parsed cells and response bytes come from
the real route. No provider, database, or spreadsheet application is contacted.
"""

from __future__ import annotations

import codecs
import csv
import io
import random
from copy import deepcopy
from decimal import Decimal
from itertools import product
from time import perf_counter
from typing import Any, Self

import azure.functions as func
import pytest
from test_function_app import SAMPLE_ROW

import function_app as api

HEADERS = [
    "EIN", "Organization", "City", "State", "Revenue (most recent 990)",
    "Gap rank", "Alignment (n/6)", "Qualifies", "Trigger", "Trigger angle",
    "Values: Population served", "Values: Leadership",
    "Development director present", "Fundraising spend ratio", "Website",
    "Suppression flag", "Contact name", "Contact title", "Contact email",
    "Contact phone", "Contact status",
]
SIGNALS = ("population_served", "leadership_composition")
VALUE_HEADERS = ("Values: Population served", "Values: Leadership")
QUOTE = "Our programs provide free community education and family support"
PREFIXES = ("=", "+", "-", "@", "\t", "\r")
NULLABLE_FIELDS = (
    "assigned_trigger", "trigger_angle", "trigger_evidence", "gap_rank",
    "suppression_flag", "notes", "updated_by", "city", "state", "revenue_latest",
    "filing_revenue_total", "filing_mission_text", "filing_program_text",
    "values_signals", "alignment", "capacity", "soft_flags", "disqualified",
    "dq_reason", "website", "officers", "icp_config", "enr_id",
    "enr_contact_name", "enr_contact_title", "enr_email", "enr_email_status",
    "enr_stale_detail", "enr_phone", "enr_provider_confidence",
    "enr_credits_spent", "enr_retention_expires_at",
)


class Connection:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.executed: list[tuple[str, object]] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def cursor(self) -> Self:
        return self

    def execute(self, sql: str, params: object = None) -> None:
        self.executed.append((sql, params))

    def fetchall(self) -> list[dict[str, Any]]:
        return self.rows


def row(**overrides: Any) -> dict[str, Any]:
    return {**deepcopy(SAMPLE_ROW), **overrides}


def signal(citation: Any = QUOTE, **overrides: Any) -> dict[str, Any]:
    return {
        "score": 80, "citation": citation, "needs_human_verification": False,
        **overrides,
    }


def download(
    monkeypatch: pytest.MonkeyPatch, rows: list[dict[str, Any]],
) -> tuple[func.HttpResponse, list[dict[str, str]], Connection]:
    conn = Connection(rows)
    monkeypatch.setattr(api, "_conn", lambda: conn)
    response = api.export_csv(func.HttpRequest(
        method="GET", url="/api/export.csv", params={"client_id": "2"}, body=b"",
    ))
    assert response.status_code == 200
    body = response.get_body()
    assert body.startswith(codecs.BOM_UTF8), "export must start with a UTF-8 BOM"
    text = body.decode("utf-8-sig", errors="strict")
    records = list(csv.reader(io.StringIO(text, newline="")))
    assert records[0] == HEADERS
    assert all(len(record) == 21 for record in records)
    return response, [dict(zip(HEADERS, r, strict=True)) for r in records[1:]], conn


@pytest.mark.parametrize("field", [*NULLABLE_FIELDS, "all"])
def test_nullable_fields_preserve_21_cells(
    monkeypatch: pytest.MonkeyPatch, field: str,
) -> None:
    fields = NULLABLE_FIELDS if field == "all" else (field,)
    _, cells, _ = download(monkeypatch, [row(**dict.fromkeys(fields))])
    assert len(cells) == 1
    if field == "all":
        expected = dict.fromkeys(HEADERS, "")
        expected.update({
            "EIN": str(SAMPLE_ROW["ein"]), "Organization": str(SAMPLE_ROW["org_name"]),
            "Values: Leadership": "needs human confirmation",
            "Contact email": "Enrich to unlock", "Contact phone": "Enrich to unlock",
            "Contact status": "Enrich to unlock",
        })
        assert cells[0] == expected


@pytest.mark.parametrize("prefix", PREFIXES, ids=["equals", "plus", "minus", "at", "tab", "cr"])
@pytest.mark.parametrize("header", [
    "Organization", *VALUE_HEADERS, "Contact name", "Contact title",
    "Contact email", "Website",
])
def test_formula_prefix_is_neutralized(
    monkeypatch: pytest.MonkeyPatch, prefix: str, header: str,
) -> None:
    payload = prefix + ("=SUM(1,2)" if prefix in ("\t", "\r") else "SUM(1,2)")
    source = row(
        org_name=payload, website=payload,
        officers=[{"name": payload, "title": payload, "is_officer": True}],
        enr_id=7, enr_contact_name=payload, enr_contact_title=payload, enr_email=payload,
        values_signals={key: signal(payload) for key in SIGNALS},
        filing_mission_text=payload,
    )
    response, cells, _ = download(monkeypatch, [source])
    actual = cells[0][header]
    print(f"FORMULA {header} prefix={prefix!r} cell={actual!r}")
    assert actual.lstrip(" \t\r\n")[:1] not in ("=", "+", "-", "@"), (
        f"Unsafe spreadsheet cell {header}: {actual!r}; "
        f"CSV record={response.get_body().decode('utf-8').splitlines()[-1]!r}"
    )
    assert actual[:1] not in PREFIXES, f"Unsafe leading control character: {actual!r}"


@pytest.mark.parametrize("number", [Decimal("-12.50"), Decimal(0), Decimal("12.50")])
def test_numeric_cells_are_not_formula_escaped(
    monkeypatch: pytest.MonkeyPatch, number: Decimal,
) -> None:
    _, cells, _ = download(monkeypatch, [row(
        filing_revenue_total=number, gap_rank=number,
        capacity={"fundraising_spend_ratio": number},
    )])
    for header in ("Revenue (most recent 990)", "Gap rank", "Fundraising spend ratio"):
        assert cells[0][header] == str(number)


UNICODE_TEXTS = [
    "Éducation à Montréal — familles et accès à la santé",
    "Community education 🌍 📚 and family support 🤝",
    "نقدم التعليم المجاني ودعم الأسر في المجتمع المحلي",
]


@pytest.mark.parametrize("text", UNICODE_TEXTS, ids=["accents", "emoji", "rtl"])
def test_names_and_contact_fields_round_trip_utf8(
    monkeypatch: pytest.MonkeyPatch, text: str,
) -> None:
    response, cells, _ = download(monkeypatch, [row(
        org_name=text, officers=[{"name": text, "title": text, "is_officer": True}],
    )])
    assert cells[0]["Organization"] == text
    assert cells[0]["Contact name"] == text
    assert cells[0]["Contact title"] == text
    assert text.encode("utf-8") in response.get_body()
    print(f"ENCODING charset={response.charset!r} BOM={response.get_body().startswith(bytes.fromhex('efbbbf'))}")
    assert response.charset.lower() == "utf-8"


UNICODE_CITATION_CASES = [
    pytest.param(UNICODE_TEXTS[0], id="accents"),
    pytest.param(UNICODE_TEXTS[1], id="emoji"),
    pytest.param(UNICODE_TEXTS[2], id="rtl", marks=pytest.mark.xfail(
        strict=True,
        reason="Deferred (STATUS.md post-v1): qa._normalize_for_match keeps only [a-z0-9 ], so a "
        "non-Latin (Arabic) citation normalizes to nothing and is never grounded. Fails safe "
        "(blank / 'needs human confirmation'); fixing it means changing the shared qa normalizer.",
    )),
]


@pytest.mark.parametrize("text", UNICODE_CITATION_CASES)
def test_unicode_citations_survive_grounding_and_encoding(
    monkeypatch: pytest.MonkeyPatch, text: str,
) -> None:
    _, cells, _ = download(monkeypatch, [row(
        filing_mission_text=text,
        values_signals={key: signal(text) for key in SIGNALS},
    )])
    assert [cells[0][header] for header in VALUE_HEADERS] == [text, text]


@pytest.mark.parametrize("key,header", list(zip(SIGNALS, VALUE_HEADERS, strict=True)))
def test_nullable_signal_cross_product_never_invents_or_raises(
    monkeypatch: pytest.MonkeyPatch, key: str, header: str,
) -> None:
    cases = list(product(
        (None, 0, 80), (None, "", QUOTE), (None, False, True),
        (None, "", QUOTE), (None, [], [{"desc": QUOTE}]),
    ))
    sources = [
        row(
            values_signals={key: signal(citation, score=score, needs_human_verification=verify)},
            filing_mission_text=mission, filing_program_text=program,
        )
        for score, citation, verify, mission, program in cases
    ]
    _, cells, _ = download(monkeypatch, sources)
    assert len(cells) == len(cases) == 243
    for case, record in zip(cases, cells, strict=True):
        score, citation, verify, mission, program = case
        grounded = score is not None and citation == QUOTE and not verify and (mission or program)
        expected = QUOTE if grounded else (
            "needs human confirmation" if key == "leadership_composition" else ""
        )
        assert record[header] == expected, case
        assert record["Contact email"] == "Enrich to unlock"


@pytest.mark.parametrize("signals", [
    None, {}, {"population_served": None, "leadership_composition": None},
    {"population_served": {}, "leadership_composition": {}},
])
def test_missing_score_and_null_values_signals_are_blank(
    monkeypatch: pytest.MonkeyPatch, signals: Any,
) -> None:
    _, cells, _ = download(monkeypatch, [row(
        values_signals=signals, alignment=None, capacity=None,
        officers=[{"name": "Alex Smith", "title": "Director"}],
        website="https://example.org",
    )])
    assert cells[0]["Values: Population served"] == ""
    assert cells[0]["Values: Leadership"] == "needs human confirmation"
    assert cells[0]["Alignment (n/6)"] == cells[0]["Qualifies"] == ""
    assert cells[0]["Development director present"] == ""
    assert cells[0]["Fundraising spend ratio"] == ""
    assert cells[0]["Contact email"] == "Enrich to unlock"


@pytest.mark.parametrize("enabled", [False, True])
def test_absent_enrichment_email_is_never_guessed_or_fetched(
    monkeypatch: pytest.MonkeyPatch, enabled: bool,
) -> None:
    def forbidden_provider_call(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Export must not initiate or poll enrichment")

    for name in ("FullEnrichProvider", "start_enrichment", "check_enrichment"):
        monkeypatch.setattr(api, name, forbidden_provider_call)
    _, cells, _ = download(monkeypatch, [row(
        status="approved",
        icp_config={"enrichment_enabled": enabled, "fullenrich_subaccount_id": "sub-1"},
        officers=[{"name": "Alex Smith", "title": "Executive Director"}],
        website="https://example.org", enr_id=7, enr_email=None,
        enr_phone=None, enr_email_status="not_found",
    )])
    assert cells[0]["Contact email"] == cells[0]["Contact phone"] == ""
    assert cells[0]["Contact status"] == "not_found"


GROUNDING_CASES = [
    pytest.param("Free lessons and assistance for local households", QUOTE, None, False,
                 id="semantic-paraphrase"),
    pytest.param(QUOTE, "An unrelated mission.", [{"desc": QUOTE}], True,
                 id="program-only"),
    # Corrected from Copilot's original expectation (rejected): a citation fully present in
    # the corpus is grounded regardless of length — the export requires the whole quote.
    pytest.param("community support", "We provide community support every day.", None, True,
                 id="shorter-than-25-fully-present-is-grounded"),
    pytest.param("abcdefghijklmnopqrstuvwxAAAA", "abcdefghijklmnopqrstuvwxBBBB", None, False,
                 id="only-24-characters-overlap"),
    pytest.param("OUR PROGRAMS,\n provide FREE community education; and family support!",
                 QUOTE, None, True, id="normalized-whitespace-case-punctuation"),
    pytest.param(QUOTE + "; every participant receives a guaranteed $100000 grant",
                 QUOTE, None, False, id="matching-prefix-invented-suffix"),
]


@pytest.mark.parametrize("citation,mission,program,expected_grounded", GROUNDING_CASES)
def test_grounding_rejects_unsubstantiated_quotes(
    monkeypatch: pytest.MonkeyPatch, citation: str, mission: str,
    program: Any, expected_grounded: bool,
) -> None:
    _, cells, _ = download(monkeypatch, [row(
        filing_mission_text=mission, filing_program_text=program,
        values_signals={key: signal(citation) for key in SIGNALS},
    )])
    expected = [citation, citation] if expected_grounded else ["", "needs human confirmation"]
    actual = [cells[0][header] for header in VALUE_HEADERS]
    print(f"GROUNDING citation={citation!r} cells={actual!r}")
    assert actual == expected
    corpus = f"{mission} {' '.join(p['desc'] for p in program or [])}"
    for key in SIGNALS:
        assert api._grounded_citation(
            {key: signal(citation)}, key, corpus,
        ) == (citation if expected_grounded else None)


@pytest.mark.parametrize("route", ["list", "export"])
def test_queries_remain_separate_with_nonempty_result(
    monkeypatch: pytest.MonkeyPatch, route: str,
) -> None:
    conn = Connection([row()])
    monkeypatch.setattr(api, "_conn", lambda: conn)
    handler = api.list_prospects if route == "list" else api.export_csv
    response = handler(func.HttpRequest(
        method="GET", url=f"/api/{route}", params={"client_id": "2"}, body=b"",
    ))
    assert response.status_code == 200
    expected = api.PROSPECT_QUERY if route == "list" else api.EXPORT_QUERY
    forbidden = api.EXPORT_QUERY if route == "list" else api.PROSPECT_QUERY
    assert expected != forbidden
    assert conn.executed == [(expected, {"client_id": "2"})]
    assert all(sql != forbidden for sql, _ in conn.executed)
    for field in ("mission_text", "program_text"):
        assert (field in expected.lower()) is (route == "export")


def test_500_rows_with_20kib_corpus_are_complete(monkeypatch: pytest.MonkeyPatch) -> None:
    rng = random.Random(20261017)
    sources = []
    quotes = []
    for index in range(500):
        text = "".join(rng.choices("abcdefghijklmnopqrstuvwxyz ", k=20 * 1024))
        mission, program = text[:8192], text[8192:]
        # ~100-char citations extended to whole-word boundaries: grounding is word-aligned,
        # so a citation cut mid-word (the original fixed [:100] / [-100:] slices) is
        # correctly not a quote. Start/end of the corpus count as boundaries.
        end = mission.find(" ", 100)
        population = mission[: end if end != -1 else len(mission)]
        start = program.rfind(" ", 0, len(program) - 100)
        leadership = program[start + 1 :]
        sources.append(row(
            ein=f"{index:09d}", org_name=f"Organization {index}",
            filing_mission_text=mission, filing_program_text=[{"desc": program}],
            values_signals={
                "population_served": signal(population),
                "leadership_composition": signal(leadership),
            },
        ))
        quotes.append((population, leadership))
        assert len(mission.encode()) + len(program.encode()) == 20 * 1024
    started = perf_counter()
    response, cells, _ = download(monkeypatch, sources)
    elapsed = perf_counter() - started
    assert len(cells) == 500
    for index, (record, citations) in enumerate(zip(cells, quotes, strict=True)):
        assert record["EIN"] == f"{index:09d}"
        assert record["Organization"] == f"Organization {index}"
        assert tuple(record[header] for header in VALUE_HEADERS) == citations
    print(f"LARGE rows={len(cells)} corpus_bytes={500 * 20 * 1024} "
          f"response_bytes={len(response.get_body())} elapsed_seconds={elapsed:.3f}")


@pytest.mark.parametrize("count,expected", [
    (0, "0/6"), (6, "6/6"), (7, ""), (-1, ""), ("3", ""), (None, ""),
], ids=["zero", "six", "above-six-safety-expectation", "negative-safety-expectation", "string", "null"])
def test_alignment_does_not_publish_impossible_counts(
    monkeypatch: pytest.MonkeyPatch, count: Any, expected: str,
) -> None:
    _, cells, _ = download(monkeypatch, [row(alignment={"criteria_met_count": count})])
    actual = cells[0]["Alignment (n/6)"]
    print(f"ALIGNMENT input={count!r} cell={actual!r}")
    assert actual == expected
