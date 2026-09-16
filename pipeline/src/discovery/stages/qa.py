"""QA job (§7): "every run samples 20 scored claims into qa_samples, re-fetches each
citation, verdicts match/mismatch; mismatch rate >10% pages Duan" — the automated
stand-in for "Duan reads the output".

"Re-fetch" here means re-querying our own stored source data (the actual ground
truth in this system — mission_text/program_text/signals), not an HTTP fetch: our
citations (§4 Stage 3) are inline pointers/paraphrases, not URLs. This is
pattern-based verification, not full semantic fact-checking — it catches the
concrete failure mode of a citation pointing at data that doesn't actually exist
(fabricated grounding) or a specific figure that doesn't match the real computed
value, not subtler misrepresentations.
"""

from __future__ import annotations

import difflib
import random
import re
from typing import Any

# priority_tier_metro is computed deterministically in Python (§4 Stage 3,
# compute_priority_tier_metro), never by the model — sampling it would test our own
# arithmetic, not catch LLM hallucination, so it's excluded from the claim pool.
EXCLUDED_ALIGNMENT_KEYS = frozenset({"priority_tier_metro"})

TEXT_FIELD_POINTERS = ("mission_text", "program_text")
NUMERIC_FIELD_POINTERS = (
    "govt_pct",
    "gov_funding_pct",
    "contributions_pct",
    "program_pct",
    "fundraising_spend_ratio",
    "org_age",
)
NUMERIC_TOLERANCE = 0.02
BOOLEAN_FIELD_POINTERS = ("significant_change_ind",)
CATEGORICAL_FIELD_POINTERS = ("revenue_trend",)
CATEGORICAL_NULL_WORDS = frozenset({"null", "none", "n/a", "unavailable", "no data"})
# Minimum longest-common-substring length (after normalization) to call a quoted/
# paraphrased citation grounded in the actual source text — short coincidental
# overlaps ("the organization") shouldn't count as verification.
MIN_TEXT_MATCH_LEN = 25
_NON_ALNUM = re.compile(r"[^a-z0-9 ]")


def _normalize_for_match(text: str) -> str:
    return " ".join(_NON_ALNUM.sub(" ", text.lower()).split())


def _program_text_corpus(program_text: Any) -> str:
    if not program_text:
        return ""
    return " ".join(p.get("desc", "") for p in program_text if isinstance(p, dict))


def _quoted_text_matches_corpus(citation: str, corpus: str) -> bool:
    """Longest-common-substring check (after normalization) between a citation that
    quotes/paraphrases source text and the actual stored corpus. Catches both exact
    quotes and near-verbatim paraphrases without requiring perfect string equality."""
    citation_norm = _normalize_for_match(citation)
    corpus_norm = _normalize_for_match(corpus)
    if not citation_norm or not corpus_norm:
        return False
    matcher = difflib.SequenceMatcher(None, citation_norm, corpus_norm, autojunk=False)
    match = matcher.find_longest_match(0, len(citation_norm), 0, len(corpus_norm))
    return match.size >= min(MIN_TEXT_MATCH_LEN, len(citation_norm))


def extract_claims(ein: str, values_signals: dict[str, Any], alignment: dict[str, Any]) -> list[dict[str, Any]]:
    """Pulls every citable claim out of one org's Sonnet score. Only claims with a
    non-null citation are extracted — "insufficient evidence" claims aren't
    fact-checkable and aren't the failure mode this job looks for.
    """
    claims: list[dict[str, Any]] = []
    for key, sig in (values_signals or {}).items():
        if isinstance(sig, dict) and sig.get("citation"):
            claims.append(
                {"ein": ein, "field": f"values_signals.{key}", "claim": sig.get("rationale") or "", "citation": sig["citation"]}
            )
    for key, crit in (alignment or {}).get("criteria", {}).items():
        if key in EXCLUDED_ALIGNMENT_KEYS:
            continue
        if isinstance(crit, dict) and crit.get("citation"):
            claims.append(
                {"ein": ein, "field": f"alignment.{key}", "claim": crit.get("rationale") or "", "citation": crit["citation"]}
            )
    return claims


def sample_claims(all_claims: list[dict[str, Any]], sample_size: int = 20, seed: int | None = None) -> list[dict[str, Any]]:
    if len(all_claims) <= sample_size:
        return list(all_claims)
    rng = random.Random(seed)
    return rng.sample(all_claims, sample_size)


def verify_claim(citation: str, org_context: dict[str, Any]) -> str:
    """Returns 'match', 'mismatch', or 'unverifiable' (citation format not
    recognized — not forced into a binary verdict it doesn't fit).
    """
    citation_lower = citation.lower()

    for field in NUMERIC_FIELD_POINTERS:
        if field in citation_lower:
            match = re.search(re.escape(field) + r"\D{0,10}(-?\d+\.?\d*)", citation_lower)
            if not match:
                continue
            cited_number = float(match.group(1))
            actual_number = org_context.get(field)
            if actual_number is None:
                return "mismatch"  # cites a figure for a field we don't actually have
            return "match" if abs(float(actual_number) - cited_number) <= NUMERIC_TOLERANCE else "mismatch"

    for field in BOOLEAN_FIELD_POINTERS:
        if field in citation_lower:
            match = re.search(re.escape(field) + r"\D{0,5}(true|false)", citation_lower)
            if not match:
                continue
            cited_bool = match.group(1) == "true"
            actual_bool = org_context.get(field)
            if actual_bool is None:
                return "mismatch"
            return "match" if bool(actual_bool) == cited_bool else "mismatch"

    for field in CATEGORICAL_FIELD_POINTERS:
        if field in citation_lower:
            match = re.search(re.escape(field) + r"\D{0,5}([a-z/ ]+)", citation_lower)
            if not match:
                continue
            cited_category = match.group(1).strip().strip("'\".,;")
            actual_category = org_context.get(field)
            if cited_category in CATEGORICAL_NULL_WORDS or not cited_category:
                return "match" if actual_category is None else "mismatch"
            return "match" if actual_category == cited_category else "mismatch"

    for field in TEXT_FIELD_POINTERS:
        if field in citation_lower:
            value = org_context.get(field)
            has_content = bool(value) if field == "mission_text" else bool(value and len(value) > 0)
            return "match" if has_content else "mismatch"

    # No bare field-name pointer — check whether the citation is itself a quote or
    # close paraphrase of the org's actual stored mission/program text.
    corpus = (org_context.get("mission_text") or "") + " " + _program_text_corpus(org_context.get("program_text"))
    if corpus.strip():
        return "match" if _quoted_text_matches_corpus(citation, corpus) else "mismatch"

    return "unverifiable"


def compute_mismatch_rate(verdicts: list[str]) -> float | None:
    """Denominator is match+mismatch only — 'unverifiable' isn't a judgment either
    way, so it's excluded rather than silently counted as passing."""
    verified = [v for v in verdicts if v in ("match", "mismatch")]
    if not verified:
        return None
    mismatches = sum(1 for v in verified if v == "mismatch")
    return round(mismatches / len(verified), 4)
