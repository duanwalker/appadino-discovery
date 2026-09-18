"""Stage 3 — AI scoring (§4), two passes over the Message Batches API (§2, 50%
discount). Haiku cheap pass pre-screens on mission/program text; Sonnet deep pass
produces the five values signals (each cited) and the strategic-alignment framework.

Text inputs are 990 Part III mission/program narrative only (§4 Stage 3 originally
called for "990 + website title/description" — website fetching was explicitly ruled
out of scope this gate; see STATUS.md). NTEE is never included in any prompt (§9 rule
6). capacity is always computed in Python from `signals`, never by the model, so it
can never emit "fully qualified" (§9 rule 2) — the note is a fixed string.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import anthropic
from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.messages.batch_create_params import Request

from discovery.stages.qa import (
    BOOLEAN_FIELD_POINTERS,
    CATEGORICAL_FIELD_POINTERS,
    NUMERIC_FIELD_POINTERS,
    _program_text_corpus,
    _quoted_text_matches_corpus,
    verify_claim,
)

# Citations pointing at one of these fields get their actual cited value checked
# against the real computed signal by verify_claim() (no presence-only shortcut for
# these — see _citation_supports_claim). Anything else is checked directly against
# the real corpus text.
_VALUE_VERIFIED_FIELD_POINTERS = NUMERIC_FIELD_POINTERS + BOOLEAN_FIELD_POINTERS + CATEGORICAL_FIELD_POINTERS

logger = logging.getLogger(__name__)

HAIKU_MODEL = "claude-haiku-4-5"
SONNET_MODEL = "claude-sonnet-5"

DEFAULT_HAIKU_CUT_N = 3000

VALUES_SIGNAL_KEYS = (
    "leadership_composition",
    "population_served",
    "mission_language",
    "programming",
    "funder_base",
)

# 3-of-7 strategic alignment framework (Lauren's ICP spec, not carried into the
# implementation brief — provided directly by Duan during G1.4). Criterion 2
# ("potential GENESIS hiring partner") is EXCLUDED PENDING CONFIRMATION, not
# permanently dropped: GENESIS is a Track A-specific ARCHITECT service, Discovery is
# confirmed Track B-only, and there's no reliable public evidence to score it against
# without risking fabricated rationale. Unconfirmed with Lauren — flagged for the
# Sept 16 scoping call; may be reinstated once real evidence sourcing exists.
# Threshold is therefore 3-of-6 over the remaining criteria in the meantime.
ALIGNMENT_CRITERIA: tuple[dict[str, str], ...] = (
    {"id": "1", "key": "priority_tier_metro", "label": "In a priority-tier metro (Charlotte or Cincinnati)"},
    # id "2" (potential GENESIS hiring partner) intentionally omitted — excluded
    # pending confirmation, not forgotten. See the module-level comment above for why.
    {"id": "3", "key": "leadership_advances_equity", "label": "Leadership advances equity mission"},
    {"id": "4", "key": "mission_alignment", "label": "Mission alignment (community-centered, social equity)"},
    {"id": "5", "key": "case_study_potential", "label": "Case-study potential"},
    {"id": "6", "key": "connected_to_influencer_networks", "label": "Connected to influencer networks"},
    {"id": "7", "key": "at_inflection_point", "label": "At an inflection point"},
)
# Criterion 1 is computed deterministically in Python (geography match), not scored
# by the model — the remaining 5 are model-scored, each against real evidence.
MODEL_SCORED_ALIGNMENT_KEYS = tuple(c["key"] for c in ALIGNMENT_CRITERIA if c["key"] != "priority_tier_metro")
ALIGNMENT_QUALIFY_THRESHOLD = 3

FORBIDDEN_PHRASES = ("fully qualified",)

CAPACITY_NOTE = "qualified pending discovery conversation"  # §9 rule 2 — never "fully qualified"

HAIKU_SYSTEM_PROMPT = """You are a cheap pre-screening pass for a nonprofit prospect \
discovery pipeline. Given an organization's name and its own IRS Form 990 mission/program \
text, produce a 0-100 alignment pre-score against the supplied keywords, and flag only \
OBVIOUS disqualifiers (e.g. the organization's stated primary need is explicitly grant-\
writing services, not fundraising/development capacity building). When in doubt, do not \
flag a disqualifier — that judgment belongs to the deeper pass. Base the score only on \
the text provided; never invent facts about the organization."""

HAIKU_OUTPUT_SCHEMA: dict[str, Any] = {
    # output_config.format's JSON schema doesn't support minimum/maximum on integer
    # properties (confirmed via a live 400: "properties maximum, minimum are not
    # supported") — the 0-100 range is enforced by the system prompt instead.
    "type": "object",
    "properties": {
        "pre_score": {"type": "integer"},
        "obvious_disqualifier": {"type": "boolean"},
        "disqualifier_reason": {"type": ["string", "null"]},
    },
    "required": ["pre_score", "obvious_disqualifier", "disqualifier_reason"],
    "additionalProperties": False,
}

SONNET_SYSTEM_PROMPT = """You are the deep scoring pass for a nonprofit prospect \
discovery pipeline. You are given one organization's public IRS Form 990 data: its \
mission statement, its program service descriptions (each independently citable), and \
a handful of derived financial signals (revenue composition, development-capacity \
readables, filing-to-filing trend). You do not have and must not assume access to \
anything else — no website, no news, no outside knowledge about this specific \
organization beyond what is given.

Hard rules, non-negotiable:
1. For leadership_composition: NEVER infer race, ethnicity, or gender from a person's \
name. You have no photos. Only count this signal as scored if the provided text \
contains an explicit, published self-description of leadership composition (e.g. the \
organization describes itself as Black-led, women-led, etc., in its own words). If no \
such explicit self-description is present in the text you were given, set score to \
null and needs_human_verification to true — this is the expected, common case, not a \
failure.
2. Never use the phrase "fully qualified" or imply full qualification anywhere. \
Capacity assessment is not your job — do not comment on it.
3. A blank/null field beats a fabricated one. If the provided text does not support a \
judgment, say so — set needs_human_verification true (for values signals) or met false \
with rationale "insufficient evidence in the provided text" (for alignment criteria). \
Do not reach for outside knowledge or plausible-sounding inference.
4. Every non-null score or true/false alignment call must carry a citation: quote or \
closely paraphrase the specific sentence in the provided mission_text or program_text \
that supports it, or reference the specific provided financial signal (e.g. \
"govt_pct 0.59 from 990 filing data"). No citation, no score.
5. disqualified=true only if the organization's own stated primary need, in the text \
you were given, is explicitly grant-writing services rather than fundraising/\
development capacity building generally. This is a high bar — most organizations are \
not disqualified. If you are unsure, disqualified=false.

Score each of the five values signals (leadership_composition, population_served, \
mission_language, programming, funder_base) 0-100, or null with \
needs_human_verification=true if unsupported. Score each of the five alignment \
criteria you are given as met (true/false) with a rationale and citation, or \
met=false with rationale "insufficient evidence in the provided text" if unsupported."""


def _values_signal_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "score": {"type": ["integer", "null"]},  # 0-100 enforced by the system prompt (see HAIKU_OUTPUT_SCHEMA note)
            "rationale": {"type": "string"},
            "citation": {"type": ["string", "null"]},
            "needs_human_verification": {"type": "boolean"},
        },
        "required": ["score", "rationale", "citation", "needs_human_verification"],
        "additionalProperties": False,
    }


def _alignment_criterion_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "met": {"type": "boolean"},
            "rationale": {"type": "string"},
            "citation": {"type": ["string", "null"]},
        },
        "required": ["met", "rationale", "citation"],
        "additionalProperties": False,
    }


SONNET_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "values_signals": {
            "type": "object",
            "properties": {key: _values_signal_schema() for key in VALUES_SIGNAL_KEYS},
            "required": list(VALUES_SIGNAL_KEYS),
            "additionalProperties": False,
        },
        "alignment_criteria": {
            "type": "object",
            "properties": {key: _alignment_criterion_schema() for key in MODEL_SCORED_ALIGNMENT_KEYS},
            "required": list(MODEL_SCORED_ALIGNMENT_KEYS),
            "additionalProperties": False,
        },
        "disqualified": {"type": "boolean"},
        "dq_reason": {"type": ["string", "null"]},
    },
    "required": ["values_signals", "alignment_criteria", "disqualified", "dq_reason"],
    "additionalProperties": False,
}


def build_org_context(org: dict[str, Any]) -> dict[str, Any]:
    """Assembles the model input for one organization. Deliberately excludes `ntee`
    (§9 rule 6: NTEE is recall shaping only, never a scoring input) and any website
    content (out of scope this gate — 990 text only, see module docstring).
    """
    return {
        "name": org["name"],
        "city": org.get("city"),
        "state": org.get("state"),
        "mission_text": org.get("mission_text"),
        "program_text": org.get("program_text") or [],
        "signals": {
            "revenue_composition": org.get("revenue_composition"),
            "gov_funding_pct": org.get("gov_funding_pct"),
            "dd_present": org.get("dd_present"),
            "fundraising_spend_ratio": org.get("fundraising_spend_ratio"),
            "org_age": org.get("org_age"),
            "revenue_trend": org.get("revenue_trend"),
            "significant_change_ind": org.get("significant_change_ind"),
        },
    }


def build_haiku_request(ein: str, org: dict[str, Any], alignment_keywords: list[str]) -> Request:
    context = build_org_context(org)
    user_content = (
        f"Alignment keywords for this client: {alignment_keywords}\n\n"
        f"Organization data (from its own IRS Form 990):\n{json.dumps(context, indent=2)}"
    )
    return Request(
        custom_id=ein,
        params=MessageCreateParamsNonStreaming(
            model=HAIKU_MODEL,
            max_tokens=512,
            system=HAIKU_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
            output_config={"format": {"type": "json_schema", "schema": HAIKU_OUTPUT_SCHEMA}},
        ),
    )


def clamp_pre_score(value: int) -> int:
    """Same missing schema-level guarantee as values_signals scores (see
    enforce_hard_rules), but pre_score is only a ranking heuristic used to cut to the
    top N before Sonnet — not persisted as an evidentiary claim about the
    organization — so an out-of-range value is clamped rather than nulled: nulling
    would break the sort, and a value the model pushed past 100 or below 0 almost
    certainly means "very confident" at that extreme rather than a meaningless number.
    """
    return max(0, min(100, value))


def build_sonnet_request(ein: str, org: dict[str, Any]) -> Request:
    context = build_org_context(org)
    criteria_desc = "\n".join(
        f"- {c['key']}: {c['label']}" for c in ALIGNMENT_CRITERIA if c["key"] in MODEL_SCORED_ALIGNMENT_KEYS
    )
    user_content = (
        f"Alignment criteria to evaluate:\n{criteria_desc}\n\n"
        f"Organization data (from its own IRS Form 990):\n{json.dumps(context, indent=2)}"
    )
    return Request(
        custom_id=ein,
        params=MessageCreateParamsNonStreaming(
            model=SONNET_MODEL,
            max_tokens=8000,
            system=SONNET_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
            output_config={"format": {"type": "json_schema", "schema": SONNET_OUTPUT_SCHEMA}},
        ),
    )


def run_batch(client: anthropic.Anthropic, requests: list[Request], poll_interval: float = 10.0) -> dict[str, Any]:
    """Submits a batch, polls to completion, and returns {custom_id: parsed_json |
    None}. None means the request errored/expired/was canceled — tolerated per the
    same §10 philosophy as Stage 2 (one bad request doesn't fail the run)."""
    if not requests:
        return {}
    batch = client.messages.batches.create(requests=requests)
    logger.info("submitted batch %s (%d requests)", batch.id, len(requests))
    while True:
        batch = client.messages.batches.retrieve(batch.id)
        if batch.processing_status == "ended":
            break
        time.sleep(poll_interval)
    logger.info(
        "batch %s ended: succeeded=%d errored=%d",
        batch.id,
        batch.request_counts.succeeded,
        batch.request_counts.errored,
    )

    results: dict[str, Any] = {}
    for result in client.messages.batches.results(batch.id):
        if result.result.type == "succeeded":
            text = next((b.text for b in result.result.message.content if b.type == "text"), None)
            try:
                results[result.custom_id] = json.loads(text) if text else None
            except json.JSONDecodeError:
                logger.warning("unparseable JSON for custom_id=%s", result.custom_id)
                results[result.custom_id] = None
        else:
            detail = getattr(result.result, "error", None)
            logger.warning(
                "batch request failed for custom_id=%s: %s (%s)", result.custom_id, result.result.type, detail
            )
            results[result.custom_id] = None
    return results


def _flatten_org_context(org_context: dict[str, Any]) -> dict[str, Any]:
    """Same flattening run_publish.py applies before QA's verify_claim() — nested
    revenue_composition fields (govt_pct/program_pct/contributions_pct) are cited by
    their bare name (§9 rule 4's own example: "govt_pct 0.59 from 990 filing data"),
    so they need to be promoted to top level for verify_claim's field-pointer regexes
    to find them."""
    return {**org_context, **(org_context.get("revenue_composition") or {})}


def _citation_is_published_self_description(citation: Any, org_context: dict[str, Any]) -> bool:
    """§9 rule 1's specific bar for leadership_composition: only an explicit,
    published self-description in the org's own mission/program text counts — never
    a numeric/categorical field pointer (unlike the other four signals, identity
    self-description isn't the kind of claim a financial signal can support).
    Deliberately bypasses verify_claim()'s TEXT_FIELD_POINTERS shortcut (which
    matches "mission_text: ..." citations merely because mission_text is non-empty,
    without checking the quoted content is real) — that shortcut is exactly the
    demographic-inference-with-an-unrelated-citation gap this guard exists to close,
    so leadership citations are checked against the real corpus text directly."""
    if not isinstance(citation, str) or not citation.strip():
        return False
    corpus = f"{org_context.get('mission_text') or ''} {_program_text_corpus(org_context.get('program_text'))}"
    return _quoted_text_matches_corpus(citation, corpus)


def _citation_supports_claim(citation: Any, org_context: dict[str, Any]) -> bool:
    """§9 rule 4's general citation-grounding check for the other four values
    signals and every alignment criterion. A citation pointing at a numeric/boolean/
    categorical field (e.g. "govt_pct 0.59 from 990 filing data") has its actual
    cited value checked against the real computed signal via verify_claim() — no
    loophole there, the value either matches or it doesn't. Anything else (a bare
    text-field pointer like "mission_text: '...'" or a free-text quote/paraphrase)
    is checked directly against the real corpus, deliberately bypassing
    verify_claim()'s TEXT_FIELD_POINTERS branch, which matches a "mission_text: ..."
    citation merely because mission_text is non-empty without checking the quoted
    content is real — the same fabricated-grounding loophole
    _citation_is_published_self_description exists to close for
    leadership_composition, with no principled reason it should stay open here."""
    if not isinstance(citation, str) or not citation.strip():
        return False
    citation_lower = citation.lower()
    if any(field in citation_lower for field in _VALUE_VERIFIED_FIELD_POINTERS):
        return verify_claim(citation, org_context) == "match"
    corpus = f"{org_context.get('mission_text') or ''} {_program_text_corpus(org_context.get('program_text'))}"
    return _quoted_text_matches_corpus(citation, corpus)


def enforce_hard_rules(
    values_signals: dict[str, Any],
    alignment_criteria: dict[str, Any],
    org_context: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """Defense-in-depth enforcement of §9 rules 1, 2, and 4 — independent of the
    system prompt, applied to every Sonnet response before it's persisted. Returns
    (sanitized_values_signals, sanitized_alignment_criteria, violations_found).

    `org_context` (the same org dict passed to build_sonnet_request — mission_text/
    program_text/derived signals) is required, not optional: an earlier version of
    this function defaulted it to None and silently skipped citation-grounding
    verification when omitted, which would have quietly re-opened the exact
    demographic-inference loophole (rule 1) and citation-fabrication loophole
    (rule 4) this function exists to close for any future caller that forgot to
    pass it. `run_scoring.run_scoring()` (the only caller) always has real
    org_context available — there's no legitimate call shape that needs a
    fallback, so the type system enforces that instead of a runtime default.
    """
    violations: list[str] = []
    signals = json.loads(json.dumps(values_signals))  # deep copy without importing copy
    criteria = json.loads(json.dumps(alignment_criteria))
    context = _flatten_org_context(org_context)

    leadership = signals.get("leadership_composition")
    if (
        leadership
        and leadership.get("score") is not None
        and not leadership.get("needs_human_verification")
        and not _citation_is_published_self_description(leadership.get("citation"), context)
    ):
        violations.append(
            "leadership_composition: score not backed by a citation verified against the org's own "
            "published self-description, forced to needs_human_verification"
        )
        leadership["score"] = None
        leadership["needs_human_verification"] = True

    for key, sig in signals.items():
        if not isinstance(sig, dict):
            continue
        rationale = (sig.get("rationale") or "").lower()
        if any(phrase in rationale for phrase in FORBIDDEN_PHRASES):
            violations.append(f"values_signals.{key}: forbidden phrase in rationale, redacted")
            sig["rationale"] = "[redacted: contained a disallowed phrase]"

        # output_config.format's JSON schema can't constrain integer ranges (confirmed
        # via a live 400 — see HAIKU_OUTPUT_SCHEMA), so the 0-100 range is only a
        # system-prompt instruction at request time, not a mechanical guarantee. This
        # is the code-level backstop: an out-of-range score is nulled, not clamped —
        # clamping would insert a value the model never actually returned, which is
        # exactly the kind of fabricated-looking number §9 rule 3's "blank beats
        # wrong" principle rules out for evidentiary judgments (unlike Haiku's
        # pre_score, which is only a ranking heuristic — see clamp_pre_score).
        score = sig.get("score")
        if score is not None and not (isinstance(score, int) and 0 <= score <= 100):
            violations.append(f"values_signals.{key}: score {score!r} out of [0,100] range, nulled")
            sig["score"] = None
            sig["needs_human_verification"] = True

        if (
            key != "leadership_composition"
            and sig.get("score") is not None
            and not sig.get("needs_human_verification")
            and not _citation_supports_claim(sig.get("citation"), context)
        ):
            violations.append(
                f"values_signals.{key}: score not backed by a citation verified against the org's own "
                "stored source data, forced to needs_human_verification"
            )
            sig["score"] = None
            sig["needs_human_verification"] = True

    for key, crit in criteria.items():
        if not isinstance(crit, dict):
            continue
        rationale = (crit.get("rationale") or "").lower()
        if any(phrase in rationale for phrase in FORBIDDEN_PHRASES):
            violations.append(f"alignment_criteria.{key}: forbidden phrase in rationale, redacted")
            crit["rationale"] = "[redacted: contained a disallowed phrase]"

        if crit.get("met") and not _citation_supports_claim(crit.get("citation"), context):
            violations.append(
                f"alignment_criteria.{key}: met=true not backed by a citation verified against the org's own "
                "stored source data, forced to met=false"
            )
            crit["met"] = False

    return signals, criteria, violations


def redact_dq_reason(dq_reason: str | None) -> tuple[str | None, bool]:
    """§9 rule 2's forbidden-phrase ban, extended to dq_reason — enforce_hard_rules
    only ever received values_signals/alignment_criteria, so a Sonnet response with
    dq_reason containing "fully qualified" was persisted unchanged (the G1.4 coverage
    review's Gap 2 finding). A separate function rather than folding into
    enforce_hard_rules: dq_reason isn't part of either signals/criteria dict shape,
    and this keeps enforce_hard_rules's return arity stable for its many existing
    callers. Returns (sanitized_dq_reason, was_redacted).
    """
    if dq_reason and any(phrase in dq_reason.lower() for phrase in FORBIDDEN_PHRASES):
        return "[redacted: contained a disallowed phrase]", True
    return dq_reason, False


def compute_priority_tier_metro(city: str | None, priority_metros: list[str]) -> bool:
    """Criterion 1 — computed deterministically (geography match), never by the model."""
    if not city or not priority_metros:
        return False
    city_lower = city.strip().lower()
    return any(metro.strip().lower() in city_lower or city_lower in metro.strip().lower() for metro in priority_metros)


def compute_capacity(dd_present: bool | None, fundraising_spend_ratio: float | None) -> dict[str, Any]:
    """§4 Stage 2's 2 public capacity criteria ONLY. Always Python, never the model —
    the note is a fixed string, so "fully qualified" (§9 rule 2) can never appear here."""
    return {
        "dd_present": dd_present,
        "fundraising_spend_ratio": fundraising_spend_ratio,
        "note": CAPACITY_NOTE,
    }


def compute_soft_flags(gov_funding_pct: float | None, govt_funding_heavy_pct: float) -> dict[str, Any]:
    if gov_funding_pct is None:
        return {"heavy_govt_funding": None}
    return {"heavy_govt_funding": gov_funding_pct >= govt_funding_heavy_pct}


def assemble_alignment(
    priority_tier_metro: bool, model_criteria: dict[str, Any]
) -> dict[str, Any]:
    criteria_out = {"priority_tier_metro": {"met": priority_tier_metro, "rationale": "geography match", "citation": None}}
    criteria_out.update(model_criteria)
    met_count = sum(1 for c in criteria_out.values() if c.get("met"))
    return {
        "criteria": criteria_out,
        "criteria_met_count": met_count,
        "qualifies": met_count >= ALIGNMENT_QUALIFY_THRESHOLD,
    }
