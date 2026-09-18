"""Independent G1.4 coverage review tests for code-level LLM guardrails.

These tests intentionally exercise the production scoring/publish code directly with
synthetic model outputs. When a guard is missing, the test documents the current
pass-through behavior so the gap is visible without changing implementation code.
"""

from __future__ import annotations

import inspect
import json
from types import SimpleNamespace
from typing import Any

from discovery.stages import run_scoring
from discovery.stages.publish import load_publishable
from discovery.stages.score import (
    CAPACITY_NOTE,
    MODEL_SCORED_ALIGNMENT_KEYS,
    build_haiku_request,
    build_org_context,
    build_sonnet_request,
    compute_capacity,
    enforce_hard_rules,
    redact_dq_reason,
)


def _org_context(**overrides: Any) -> dict[str, Any]:
    """A real org context (mission/program text + derived signals) to verify
    citations against — the shape enforce_hard_rules receives in production via
    run_scoring's `org_context=org`."""
    base: dict[str, Any] = {
        "mission_text": "We are a Black-led community organization providing youth mentorship and family support.",
        "program_text": [{"desc": "Community leadership cohort and after-school tutoring services."}],
        "revenue_composition": {"govt_pct": 0.59, "program_pct": 0.30, "contributions_pct": 0.11},
        "gov_funding_pct": 0.59,
        "dd_present": True,
        "fundraising_spend_ratio": 0.08,
        "org_age": 15,
        "revenue_trend": "growth",
        "significant_change_ind": True,
    }
    base.update(overrides)
    return base


def _signal(
    score: int | None = 80,
    citation: str | None = "mission_text: 'community-led services'",
    rationale: str = "Supported by the cited public text.",
    needs_human_verification: bool = False,
) -> dict[str, Any]:
    return {
        "score": score,
        "rationale": rationale,
        "citation": citation,
        "needs_human_verification": needs_human_verification,
    }


def _criterion(
    met: bool = True,
    citation: str | None = "program_text[0]: 'community leadership cohort'",
    rationale: str = "Supported by the cited public text.",
) -> dict[str, Any]:
    return {"met": met, "rationale": rationale, "citation": citation}


def test_demographic_inference_with_an_unrelated_citation_is_now_rejected() -> None:
    """RESOLVED: an officer-list citation that names a real field but doesn't
    actually contain a published self-description of leadership composition
    previously passed unchanged as long as *some* citation string was present. Now
    verified against the org's own mission/program text (bypassing verify_claim's
    "mission_text: ..." presence-only shortcut, see _citation_is_published_self_description)
    — an inference from a name, wrapped in a citation, is not grounded and is forced
    to needs_human_verification."""
    values = {
        "leadership_composition": _signal(
            rationale="The executive director appears to be Latina based on her name.",
            citation="officers list: 'Maria Alvarez, Executive Director'",
            needs_human_verification=False,
        )
    }

    sanitized, _, violations = enforce_hard_rules(values, {}, org_context=_org_context())

    assert sanitized["leadership_composition"]["score"] is None
    assert sanitized["leadership_composition"]["needs_human_verification"] is True
    assert len(violations) == 1
    assert "leadership_composition" in violations[0]


def test_leadership_score_without_citation_is_forced_to_human_verification() -> None:
    values = {
        "leadership_composition": _signal(
            citation=None,
            rationale="Leadership composition is scored without a source.",
            needs_human_verification=False,
        )
    }

    sanitized, _, violations = enforce_hard_rules(values, {}, org_context=_org_context())

    assert sanitized["leadership_composition"]["score"] is None
    assert sanitized["leadership_composition"]["needs_human_verification"] is True
    assert len(violations) == 1
    assert "leadership_composition" in violations[0]


def test_published_self_description_leadership_claim_verified_against_real_text_is_left_intact() -> None:
    """A citation that genuinely quotes/paraphrases the org's own stored
    mission_text is verified and left intact — the fix closes the demographic-
    inference gap without rejecting real, grounded self-descriptions."""
    values = {
        "leadership_composition": _signal(
            rationale="The organization's own mission text says it is Black-led.",
            citation="mission_text: 'a Black-led community organization providing youth mentorship'",
            needs_human_verification=False,
        )
    }

    sanitized, _, violations = enforce_hard_rules(values, {}, org_context=_org_context())

    assert sanitized == values
    assert violations == []


def test_leadership_citation_with_fabricated_quote_wrapped_as_mission_text_pointer_is_rejected() -> None:
    """The specific loophole rule 1's fix closes: a citation phrased as a
    "mission_text: '...'" field pointer previously would have passed verify_claim's
    presence-only shortcut merely because mission_text is non-empty, regardless of
    whether the quoted content is real. leadership_composition bypasses that
    shortcut and checks the actual quoted text against the real corpus."""
    values = {
        "leadership_composition": _signal(
            rationale="Org states it is Latina-led in its mission statement.",
            citation="mission_text: 'a proudly Latina-led organization'",
            needs_human_verification=False,
        )
    }

    sanitized, _, violations = enforce_hard_rules(values, {}, org_context=_org_context())

    assert sanitized["leadership_composition"]["score"] is None
    assert sanitized["leadership_composition"]["needs_human_verification"] is True
    assert len(violations) == 1


def test_leadership_grounding_check_is_skipped_when_org_context_omitted() -> None:
    """Backward-compatible default: omitting org_context (as every pre-fix caller
    does) falls back to the old structural-only check so tests of the other hard
    rules don't need a fabricated corpus fixture — not a production code path,
    since run_scoring always supplies org_context."""
    values = {
        "leadership_composition": _signal(
            rationale="The executive director appears to be Latina based on her name.",
            citation="officers list: 'Maria Alvarez, Executive Director'",
            needs_human_verification=False,
        )
    }

    sanitized, _, violations = enforce_hard_rules(values, {})

    assert sanitized["leadership_composition"]["score"] == 80
    assert violations == []


def test_capacity_output_is_fixed_pending_discovery_text_for_all_input_shapes() -> None:
    capacities = [
        compute_capacity(dd_present=True, fundraising_spend_ratio=0.02),
        compute_capacity(dd_present=False, fundraising_spend_ratio=0.0),
        compute_capacity(dd_present=None, fundraising_spend_ratio=None),
    ]

    assert {capacity["note"] for capacity in capacities} == {CAPACITY_NOTE}
    assert CAPACITY_NOTE == "qualified pending discovery conversation"
    assert all("fully qualified" not in capacity["note"].lower() for capacity in capacities)


def test_forbidden_phrase_is_redacted_from_model_authored_rationales() -> None:
    values = {
        "programming": _signal(rationale="This prospect is fully qualified for services."),
    }
    criteria = {
        "mission_alignment": _criterion(rationale="Fully qualified based on mission fit."),
    }

    sanitized_values, sanitized_criteria, violations = enforce_hard_rules(values, criteria)

    assert sanitized_values["programming"]["rationale"] == "[redacted: contained a disallowed phrase]"
    assert sanitized_criteria["mission_alignment"]["rationale"] == "[redacted: contained a disallowed phrase]"
    assert len(violations) == 2


def test_fully_qualified_ban_now_covers_disqualification_reason() -> None:
    """RESOLVED: dq_reason is persisted directly from Sonnet in run_scoring.py and
    was not covered by any forbidden-phrase redaction. redact_dq_reason() (a
    separate function, not folded into enforce_hard_rules — see its docstring for
    why) is now called on every dq_reason before persistence."""
    source = inspect.getsource(run_scoring.run_scoring)

    assert "redact_dq_reason(result[\"dq_reason\"])" in source
    assert '"dq_reason": dq_reason' in source


def test_redact_dq_reason_redacts_forbidden_phrase() -> None:
    sanitized, was_redacted = redact_dq_reason("Org is fully qualified except for one grant-writing need.")

    assert sanitized == "[redacted: contained a disallowed phrase]"
    assert was_redacted is True


def test_redact_dq_reason_case_insensitive() -> None:
    sanitized, was_redacted = redact_dq_reason("FULLY QUALIFIED aside from this one service gap.")

    assert sanitized == "[redacted: contained a disallowed phrase]"
    assert was_redacted is True


def test_redact_dq_reason_leaves_clean_text_and_none_untouched() -> None:
    clean = "Organization's stated primary need is grant-writing services, not fundraising capacity."
    assert redact_dq_reason(clean) == (clean, False)
    assert redact_dq_reason(None) == (None, False)


def test_non_leadership_scored_signal_without_citation_is_now_rejected() -> None:
    values = {"programming": _signal(citation=None, needs_human_verification=False)}

    sanitized, _, violations = enforce_hard_rules(values, {}, org_context=_org_context())

    assert sanitized["programming"]["score"] is None
    assert sanitized["programming"]["needs_human_verification"] is True
    assert len(violations) == 1


def test_empty_string_citation_is_now_rejected_for_non_leadership_signal() -> None:
    values = {"population_served": _signal(citation="", needs_human_verification=False)}

    sanitized, _, violations = enforce_hard_rules(values, {}, org_context=_org_context())

    assert sanitized["population_served"]["score"] is None
    assert sanitized["population_served"]["needs_human_verification"] is True
    assert len(violations) == 1


def test_whitespace_citation_is_now_rejected_for_scored_signal() -> None:
    values = {"mission_language": _signal(citation="   ", needs_human_verification=False)}

    sanitized, _, violations = enforce_hard_rules(values, {}, org_context=_org_context())

    assert sanitized["mission_language"]["score"] is None
    assert sanitized["mission_language"]["needs_human_verification"] is True
    assert len(violations) == 1


def test_malformed_citation_object_is_now_rejected() -> None:
    values = {"funder_base": _signal(citation={"source": "mission_text"})}

    sanitized, _, violations = enforce_hard_rules(values, {}, org_context=_org_context())

    assert sanitized["funder_base"]["score"] is None
    assert sanitized["funder_base"]["needs_human_verification"] is True
    assert len(violations) == 1


def test_citation_with_a_hallucinated_number_is_rejected() -> None:
    """A citation that names a real field but cites the wrong value — the same
    fabricated-grounding failure mode the QA job's verify_claim() catches at
    sample-time — is now caught before persistence, not just after."""
    values = {"funder_base": _signal(citation="govt_pct: 0.91 from 990 filing data")}

    sanitized, _, violations = enforce_hard_rules(values, {}, org_context=_org_context())

    assert sanitized["funder_base"]["score"] is None
    assert sanitized["funder_base"]["needs_human_verification"] is True
    assert len(violations) == 1


def test_citation_grounded_in_a_real_derived_signal_is_left_intact() -> None:
    """The fix's positive case: a citation that accurately references a real
    derived signal (rule 4's own example phrasing) is verified and preserved."""
    values = {"funder_base": _signal(citation="govt_pct: 0.59 from 990 filing data")}

    sanitized, _, violations = enforce_hard_rules(values, {}, org_context=_org_context())

    assert sanitized["funder_base"]["score"] == 80
    assert violations == []


def test_citation_grounded_in_real_program_text_is_left_intact() -> None:
    values = {
        "programming": _signal(citation="program_text: 'Community leadership cohort and after-school tutoring services.'")
    }

    sanitized, _, violations = enforce_hard_rules(values, {}, org_context=_org_context())

    assert sanitized["programming"]["score"] == 80
    assert violations == []


def test_citation_enforcement_is_skipped_when_org_context_omitted() -> None:
    values = {"funder_base": _signal(citation={"source": "mission_text"})}

    sanitized, _, violations = enforce_hard_rules(values, {})

    assert sanitized["funder_base"]["score"] == 80
    assert sanitized["funder_base"]["citation"] == {"source": "mission_text"}
    assert violations == []


def test_alignment_met_true_without_citation_is_now_rejected() -> None:
    criteria = {"mission_alignment": _criterion(met=True, citation=None)}

    _, sanitized, violations = enforce_hard_rules({}, criteria, org_context=_org_context())

    assert sanitized["mission_alignment"]["met"] is False
    assert len(violations) == 1


def test_alignment_empty_citation_is_now_rejected() -> None:
    criteria = {"case_study_potential": _criterion(met=True, citation="")}

    _, sanitized, violations = enforce_hard_rules({}, criteria, org_context=_org_context())

    assert sanitized["case_study_potential"]["met"] is False
    assert len(violations) == 1


def test_alignment_met_true_with_hallucinated_numeric_citation_is_rejected() -> None:
    criteria = {"mission_alignment": _criterion(met=True, citation="govt_pct: 0.91 from 990 filing data")}

    _, sanitized, violations = enforce_hard_rules({}, criteria, org_context=_org_context())

    assert sanitized["mission_alignment"]["met"] is False
    assert len(violations) == 1


def test_alignment_met_true_grounded_in_real_text_is_left_intact() -> None:
    criteria = {
        "mission_alignment": _criterion(
            met=True, citation="mission_text: 'a Black-led community organization providing youth mentorship'"
        )
    }

    _, sanitized, violations = enforce_hard_rules({}, criteria, org_context=_org_context())

    assert sanitized["mission_alignment"]["met"] is True
    assert violations == []


def test_alignment_met_false_is_never_checked_for_citation() -> None:
    """met=false criteria are already the "safe" state (don't contribute to
    criteria_met_count/qualifies) — no grounding check applies to them regardless of
    citation shape."""
    criteria = {"mission_alignment": _criterion(met=False, citation=None)}

    _, sanitized, violations = enforce_hard_rules({}, criteria, org_context=_org_context())

    assert sanitized["mission_alignment"]["met"] is False
    assert violations == []


def test_alignment_enforcement_is_skipped_when_org_context_omitted() -> None:
    criteria = {"mission_alignment": _criterion(met=True, citation=None)}

    _, sanitized, violations = enforce_hard_rules({}, criteria)

    assert sanitized["mission_alignment"]["met"] is True
    assert violations == []


def test_grant_writing_disqualified_result_is_persisted_with_redacted_reason_by_orchestrator() -> None:
    source = inspect.getsource(run_scoring.run_scoring)

    assert '"disqualified": result["disqualified"]' in source
    assert 'redact_dq_reason(result["dq_reason"])' in source
    assert '"dq_reason": dq_reason' in source


def test_publishable_query_excludes_disqualified_and_unscored_sonnet_rows() -> None:
    """RESOLVED (Gap 6): load_publishable() now also excludes alignment IS NULL rows
    — the batch-partial-failure placeholder rows run_scoring persists (see
    test_batch_failure_is_persisted_as_a_durable_placeholder_row below) — so a
    failed-but-not-yet-rescored EIN is never mistaken for a publishable score."""
    source = inspect.getsource(load_publishable)

    assert "sc.stage = 'sonnet'" in source
    assert "sc.disqualified = false" in source
    assert "sc.alignment IS NOT NULL" in source


def test_grant_writing_as_one_of_several_needs_is_prompted_as_not_automatic_dq() -> None:
    system_prompt = build_sonnet_request("1", _org())["params"]["system"]

    assert "primary need" in system_prompt
    assert "not disqualified" in system_prompt
    assert "If you are unsure, disqualified=false" in system_prompt


def test_haiku_cutoff_ties_are_now_broken_deterministically_by_ein() -> None:
    """RESOLVED (Gap 5): the candidate sort now has an explicit, documented
    secondary key (ascending EIN) — re-runs on the same tied data always produce the
    same top-N set regardless of load order."""
    source = inspect.getsource(run_scoring.run_scoring)

    assert "candidates.sort(key=lambda pair: (-pair[1], pair[0]))" in source

    first_order = [("100000003", 90), ("100000001", 90), ("100000002", 90)]
    second_order = list(reversed(first_order))
    first_order.sort(key=lambda pair: (-pair[1], pair[0]))
    second_order.sort(key=lambda pair: (-pair[1], pair[0]))

    assert first_order == second_order == [("100000001", 90), ("100000002", 90), ("100000003", 90)]


def test_haiku_cutoff_tie_break_prefers_higher_score_before_ein() -> None:
    candidates = [("100000005", 70), ("100000001", 95), ("100000003", 95)]
    candidates.sort(key=lambda pair: (-pair[1], pair[0]))

    assert [ein for ein, _ in candidates[:2]] == ["100000001", "100000003"]


def test_batch_failures_are_returned_as_none_same_shape_as_unparseable_absence() -> None:
    source = inspect.getsource(run_scoring.run_scoring)

    assert 'counts["sonnet_failed"] = sum(1 for v in sonnet_results.values() if v is None)' in source
    assert "if result is None:" in source


def test_batch_failure_is_persisted_as_a_durable_placeholder_row() -> None:
    """RESOLVED (Gap 6): a failed batch item now gets a real `scores` row
    (values_signals/alignment NULL) instead of no row at all — durably
    distinguishing "selected for Sonnet, scoring failed" from "never selected" at
    the row level, and recoverable via the existing ON CONFLICT upsert once the
    item is rescored successfully."""
    source = inspect.getsource(run_scoring.run_scoring)

    assert 'counts["sonnet_persisted_as_failed"]' in source
    assert '"values_signals": None,\n                            "alignment": None,' in source


def test_run_batch_preserves_successes_and_marks_failed_items_none() -> None:
    class FakeBatches:
        def create(self, requests: list[dict[str, Any]]) -> SimpleNamespace:
            assert len(requests) == 2
            return SimpleNamespace(id="batch-1")

        def retrieve(self, batch_id: str) -> SimpleNamespace:
            assert batch_id == "batch-1"
            return SimpleNamespace(
                id=batch_id,
                processing_status="ended",
                request_counts=SimpleNamespace(succeeded=1, errored=1),
            )

        def results(self, batch_id: str) -> list[SimpleNamespace]:
            assert batch_id == "batch-1"
            return [
                SimpleNamespace(
                    custom_id="scored-ein",
                    result=SimpleNamespace(
                        type="succeeded",
                        message=SimpleNamespace(
                            content=[SimpleNamespace(type="text", text='{"pre_score": 91}')],
                        ),
                    ),
                ),
                SimpleNamespace(
                    custom_id="failed-ein",
                    result=SimpleNamespace(type="errored", error=SimpleNamespace(message="timeout")),
                ),
            ]

    client = SimpleNamespace(messages=SimpleNamespace(batches=FakeBatches()))

    results = run_scoring.run_batch(client, [{}, {}], poll_interval=0)

    assert results == {"scored-ein": {"pre_score": 91}, "failed-ein": None}


def test_batch_results_docstring_defines_none_as_failed_request_without_persistence_state() -> None:
    docstring = run_scoring.run_batch.__doc__ or ""

    assert "None means the request errored/expired/was canceled" in docstring
    assert "doesn't fail the run" in docstring


def test_ntee_is_excluded_from_org_context_even_when_source_org_has_it() -> None:
    context = build_org_context({**_org(), "ntee": "P20", "ein": "123456789"})

    serialized = json.dumps(context)

    assert "ntee" not in context
    assert "P20" not in serialized
    assert "123456789" not in serialized


def test_ntee_is_excluded_from_haiku_and_sonnet_request_payloads() -> None:
    org = {**_org(), "ntee": "P20 Youth Development"}
    requests = [build_haiku_request("123456789", org, ["equity"]), build_sonnet_request("123456789", org)]

    for request in requests:
        payload = json.dumps(request["params"]["messages"])
        assert "ntee" not in payload.lower()
        assert "P20" not in payload
        assert "Youth Development" not in payload


def test_sonnet_schema_requires_citations_but_allows_null_citations() -> None:
    request = build_sonnet_request("123456789", _org())
    schema = request["params"]["output_config"]["format"]["schema"]

    for key in MODEL_SCORED_ALIGNMENT_KEYS:
        criterion_schema = schema["properties"]["alignment_criteria"]["properties"][key]
        assert "citation" in criterion_schema["required"]
        assert criterion_schema["properties"]["citation"]["type"] == ["string", "null"]


def _org() -> dict[str, Any]:
    return {
        "name": "Sample Community Fund",
        "city": "Charlotte",
        "state": "NC",
        "mission_text": "We build community power through youth and family programs.",
        "program_text": ["Community leadership cohort and family support services."],
        "revenue_composition": {"govt_pct": 0.2},
        "gov_funding_pct": 0.2,
        "dd_present": False,
        "fundraising_spend_ratio": 0.01,
        "org_age": 12,
        "revenue_trend": "stable",
        "significant_change_ind": None,
    }