"""Unit tests for the pure row-transformation logic in function_app.py — the
part most likely to silently drift from the real JSONB shapes scores/publish.py
produces (see pipeline/src/discovery/stages/score.py's SONNET_OUTPUT_SCHEMA and
assemble_alignment/compute_capacity). No DB is exercised here; these fixtures
mirror exactly what psycopg's dict_row would hand back from PROSPECT_QUERY.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from function_app import _json_default, _prospect_row_to_csv_dict, _row_to_prospect

SAMPLE_ROW = {
    "id": 1,
    "ein": "010343943",
    "client_id": 2,
    "status": "new",
    "assigned_trigger": "new_ed",
    "trigger_angle": "first-100-days",
    "trigger_evidence": {"new_ed": {"prior": "Jane Doe", "current": "John Smith"}},
    "gap_rank": Decimal("66.67"),
    "suppression_flag": None,
    "notes": None,
    "updated_by": "pipeline",
    "updated_at": datetime(2026, 9, 16, 15, 37, 45, tzinfo=timezone.utc),
    "org_name": "SEXUAL ASSAULT RESPONSE SERVICES OF SOUTHERN MAINE",
    "city": "PORTLAND",
    "state": "ME",
    "revenue_latest": Decimal(612345),
    "values_signals": {
        "leadership_composition": {
            "score": 70,
            "rationale": "Board bios describe...",
            "citation": "mission_text: '...'",
            "needs_human_verification": False,
        },
        "population_served": {"score": 80, "rationale": "r", "citation": "c", "needs_human_verification": False},
        "mission_language": {"score": 60, "rationale": "r", "citation": "c", "needs_human_verification": False},
        "programming": {"score": 50, "rationale": "r", "citation": "c", "needs_human_verification": True},
        "funder_base": {"score": None, "rationale": "r", "citation": None, "needs_human_verification": True},
    },
    "alignment": {
        "criteria": {
            "priority_tier_metro": {"met": False, "rationale": "geography match", "citation": None},
            "mission_alignment": {"met": True, "rationale": "r", "citation": "c"},
        },
        "criteria_met_count": 2,
        "qualifies": False,
    },
    "capacity": {"dd_present": False, "fundraising_spend_ratio": 0.0, "note": "public criteria only"},
    "soft_flags": {"heavy_govt_funding": None},
    "disqualified": False,
    "dq_reason": None,
    "website": "https://sarssm.org",
    "officers": [{"name": "Erin K Flood", "title": "Executive Director", "is_officer": True}],
    "icp_config": {"enrichment_enabled": True, "fullenrich_subaccount_id": "sub-1"},
    "enr_id": None,
    "enr_contact_name": None,
    "enr_contact_title": None,
    "enr_email": None,
    "enr_email_status": None,
    "enr_stale_detail": None,
    "enr_phone": None,
    "enr_provider_confidence": None,
    "enr_credits_spent": None,
    "enr_retention_expires_at": None,
}


def test_row_to_prospect_converts_decimals_to_float() -> None:
    prospect = _row_to_prospect(SAMPLE_ROW)
    assert prospect["gap_rank"] == 66.67
    assert isinstance(prospect["gap_rank"], float)
    assert prospect["org"]["revenue_latest"] == 612345.0
    assert isinstance(prospect["org"]["revenue_latest"], float)


def test_row_to_prospect_nests_org_and_score() -> None:
    prospect = _row_to_prospect(SAMPLE_ROW)
    assert prospect["org"] == {
        "name": "SEXUAL ASSAULT RESPONSE SERVICES OF SOUTHERN MAINE",
        "city": "PORTLAND",
        "state": "ME",
        "revenue_latest": 612345.0,
    }
    assert prospect["score"]["alignment"]["criteria_met_count"] == 2
    assert prospect["score"]["values_signals"]["funder_base"]["score"] is None


def test_row_to_prospect_handles_null_gap_rank_and_revenue() -> None:
    row = dict(SAMPLE_ROW, gap_rank=None, revenue_latest=None)
    prospect = _row_to_prospect(row)
    assert prospect["gap_rank"] is None
    assert prospect["org"]["revenue_latest"] is None


def test_row_to_prospect_handles_missing_score_row() -> None:
    """A prospect with no matching Sonnet-stage score (LEFT JOIN LATERAL miss)."""
    row = dict(
        SAMPLE_ROW,
        values_signals=None,
        alignment=None,
        capacity=None,
        soft_flags=None,
        disqualified=None,
        dq_reason=None,
    )
    prospect = _row_to_prospect(row)
    assert prospect["score"] == {
        "values_signals": None,
        "alignment": None,
        "capacity": None,
        "soft_flags": None,
        "disqualified": None,
        "dq_reason": None,
    }


def test_prospect_row_to_csv_dict_matches_confirmed_column_set() -> None:
    csv_row = _prospect_row_to_csv_dict(SAMPLE_ROW)
    assert csv_row == {
        "ein": "010343943",
        "name": "SEXUAL ASSAULT RESPONSE SERVICES OF SOUTHERN MAINE",
        "city": "PORTLAND",
        "state": "ME",
        "gap_rank": Decimal("66.67"),
        "criteria_met_count": 2,
        "qualifies": False,
        "assigned_trigger": "new_ed",
        "trigger_angle": "first-100-days",
        "dd_present": False,
        "fundraising_spend_ratio": 0.0,
        "suppression_flag": "",
        "contact_name": "Erin K Flood",
        "contact_title": "Executive Director",
        "contact_email": "Enrich to unlock",
        "contact_phone": "Enrich to unlock",
        "contact_status": "Enrich to unlock",
    }


def test_prospect_row_to_csv_dict_shows_enriched_contact_once_enr_id_set() -> None:
    row = dict(
        SAMPLE_ROW,
        enr_id=7,
        enr_email="eflood@sarssm.org",
        enr_email_status="verified",
        enr_phone="+1 401-309-1953",
    )
    csv_row = _prospect_row_to_csv_dict(row)
    assert csv_row["contact_email"] == "eflood@sarssm.org"
    assert csv_row["contact_phone"] == "+1 401-309-1953"
    assert csv_row["contact_status"] == "verified"


def test_prospect_row_to_csv_dict_real_not_found_result_stays_blank_not_locked() -> None:
    """enr_id set but no email/phone means a real "not matched" result, distinct
    from "never enriched" — must not show the locked placeholder text."""
    row = dict(SAMPLE_ROW, enr_id=7, enr_email=None, enr_email_status="not_found", enr_phone=None)
    csv_row = _prospect_row_to_csv_dict(row)
    assert csv_row["contact_email"] == ""
    assert csv_row["contact_phone"] == ""
    assert csv_row["contact_status"] == "not_found"


def test_prospect_row_to_csv_dict_blank_contact_when_no_officers_on_file() -> None:
    row = dict(SAMPLE_ROW, officers=None)
    csv_row = _prospect_row_to_csv_dict(row)
    assert csv_row["contact_name"] == ""
    assert csv_row["contact_title"] == ""


def test_prospect_row_to_csv_dict_null_suppression_flag_becomes_empty_string() -> None:
    row = dict(SAMPLE_ROW, suppression_flag="Possible suppression match: 'X' (fuzzy, not auto-excluded)")
    csv_row = _prospect_row_to_csv_dict(row)
    assert csv_row["suppression_flag"] == "Possible suppression match: 'X' (fuzzy, not auto-excluded)"


def test_prospect_row_to_csv_dict_handles_missing_score() -> None:
    row = dict(SAMPLE_ROW, alignment=None, capacity=None)
    csv_row = _prospect_row_to_csv_dict(row)
    assert csv_row["criteria_met_count"] is None
    assert csv_row["qualifies"] is None
    assert csv_row["dd_present"] is None
    assert csv_row["fundraising_spend_ratio"] is None


def test_json_default_converts_decimal_and_datetime() -> None:
    assert _json_default(Decimal("1.5")) == 1.5
    assert _json_default(datetime(2026, 1, 1, tzinfo=timezone.utc)) == "2026-01-01T00:00:00+00:00"


class TestContactColumns:
    def test_name_and_title_always_present_regardless_of_enrichment_enabled(self) -> None:
        row = dict(SAMPLE_ROW, icp_config={"enrichment_enabled": False})
        contact = _row_to_prospect(row)["contact"]
        assert contact["name"] == "Erin K Flood"
        assert contact["title"] == "Executive Director"

    def test_none_when_no_officers_on_file(self) -> None:
        row = dict(SAMPLE_ROW, officers=None)
        contact = _row_to_prospect(row)["contact"]
        assert contact["name"] is None
        assert contact["title"] is None

    def test_not_enriched_when_no_enrichment_row_yet(self) -> None:
        contact = _row_to_prospect(SAMPLE_ROW)["contact"]
        assert contact["enriched"] is False
        assert contact["email"] is None

    def test_enriched_true_even_for_a_real_not_found_result(self) -> None:
        row = dict(SAMPLE_ROW, enr_id=7, enr_email_status="not_found")
        contact = _row_to_prospect(row)["contact"]
        assert contact["enriched"] is True
        assert contact["email_status"] == "not_found"

    def test_can_enrich_false_when_status_not_approved(self) -> None:
        contact = _row_to_prospect(SAMPLE_ROW)["contact"]  # SAMPLE_ROW status == "new"
        assert contact["can_enrich"] is False
        assert contact["cannot_enrich_reason"] == "prospect is not approved"

    def test_can_enrich_false_when_tenant_disabled(self) -> None:
        row = dict(SAMPLE_ROW, status="approved", icp_config={"enrichment_enabled": False})
        contact = _row_to_prospect(row)["contact"]
        assert contact["can_enrich"] is False
        assert contact["cannot_enrich_reason"] == "enrichment is not enabled for this tenant"

    def test_can_enrich_true_when_all_conditions_hold(self) -> None:
        row = dict(SAMPLE_ROW, status="approved")
        contact = _row_to_prospect(row)["contact"]
        assert contact["can_enrich"] is True
        assert contact["cannot_enrich_reason"] is None

    def test_retention_expired_flagged_when_past_window(self) -> None:
        row = dict(SAMPLE_ROW, enr_id=7, enr_retention_expires_at=datetime(2020, 1, 1, tzinfo=timezone.utc))
        contact = _row_to_prospect(row)["contact"]
        assert contact["retention_expired"] is True

    def test_retention_not_expired_when_in_future(self) -> None:
        row = dict(SAMPLE_ROW, enr_id=7, enr_retention_expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc))
        contact = _row_to_prospect(row)["contact"]
        assert contact["retention_expired"] is False

    def test_no_retention_date_is_not_expired(self) -> None:
        contact = _row_to_prospect(SAMPLE_ROW)["contact"]
        assert contact["retention_expired"] is False

    def test_contact_mismatch_false_when_never_enriched(self) -> None:
        contact = _row_to_prospect(SAMPLE_ROW)["contact"]
        assert contact["contact_mismatch"] is False

    def test_contact_mismatch_false_when_enriched_contact_matches_current_officer(self) -> None:
        row = dict(SAMPLE_ROW, enr_id=7, enr_contact_name="Erin K Flood")
        contact = _row_to_prospect(row)["contact"]
        assert contact["contact_mismatch"] is False

    def test_contact_mismatch_true_when_enrichment_is_for_a_since_departed_officer(self) -> None:
        # Real case found against live data: Day One's stored enrichment (E1 spike)
        # is for Gregory Bowers, a since-departed CEO; the current officer per the
        # latest filing is his successor. The dashboard must not imply the enriched
        # email/phone belong to whoever Name/Title currently shows.
        row = dict(SAMPLE_ROW, enr_id=7, enr_contact_name="Gregory Bowers", enr_contact_title="CEO (THRU MAR 2024)")
        contact = _row_to_prospect(row)["contact"]
        assert contact["contact_mismatch"] is True
        assert contact["enriched_contact_name"] == "Gregory Bowers"
        assert contact["enriched_contact_title"] == "CEO (THRU MAR 2024)"

    def test_contact_mismatch_case_insensitive(self) -> None:
        row = dict(SAMPLE_ROW, enr_id=7, enr_contact_name="erin k flood")
        contact = _row_to_prospect(row)["contact"]
        assert contact["contact_mismatch"] is False
