"""Stage 5 — Triggers (§4). Phase 1 = filing-derived only (website/news-based
triggers are Phase 2). Diffs an org's latest vs. prior filing to detect: a new
ED/CEO, a development-director departure, a transformational revenue jump, or a
first filing above the client's revenue floor.

An org can trigger on more than one condition at once; only one becomes the
"assigned" trigger (priority order below) but every detected trigger is kept in
`trigger_evidence` for transparency — the angle table only names one type per org,
but nothing here throws away evidence.
"""

from __future__ import annotations

from typing import Any

EXECUTIVE_TITLE_KEYWORDS = ("executive director", "president", "chief executive")
DEVELOPMENT_TITLE_KEYWORDS = ("development", "fundraising", "advancement")

# Lower index = higher priority when more than one trigger fires for the same org.
TRIGGER_PRIORITY = (
    "new_ed",
    "dd_departure",
    "transformational_revenue_jump",
    "first_filing_above_floor",
)

DEFAULT_TRANSFORMATIONAL_JUMP_PCT = 0.50


def _normalize(text: str) -> str:
    return " ".join(text.strip().lower().split())


def _find_executive_name(officers: list[dict[str, Any]]) -> str | None:
    for officer in officers:
        title = (officer.get("title") or "").lower()
        if any(keyword in title for keyword in EXECUTIVE_TITLE_KEYWORDS):
            name = officer.get("name")
            if name:
                return str(name)
    return None


def _has_development_officer(officers: list[dict[str, Any]]) -> bool:
    return any(
        keyword in (officer.get("title") or "").lower()
        for officer in officers
        for keyword in DEVELOPMENT_TITLE_KEYWORDS
    )


def detect_triggers(
    current: dict[str, Any],
    previous: dict[str, Any] | None,
    revenue_floor: float,
    transformational_jump_pct: float = DEFAULT_TRANSFORMATIONAL_JUMP_PCT,
) -> list[dict[str, Any]]:
    """Pure function: given the current and (optional) prior filing's officers +
    revenue_total, returns every trigger that fired, each as {"type", "evidence"}.
    """
    triggers: list[dict[str, Any]] = []
    current_officers = current.get("officers") or []
    previous_officers = (previous or {}).get("officers") or []

    if previous is not None:
        current_exec = _find_executive_name(current_officers)
        previous_exec = _find_executive_name(previous_officers)
        if current_exec and previous_exec and _normalize(current_exec) != _normalize(previous_exec):
            triggers.append(
                {"type": "new_ed", "evidence": {"previous_name": previous_exec, "current_name": current_exec}}
            )

    if previous is not None:
        had_dd = _has_development_officer(previous_officers)
        has_dd = _has_development_officer(current_officers)
        if had_dd and not has_dd:
            triggers.append(
                {
                    "type": "dd_departure",
                    "evidence": {"previous_tax_year": previous.get("tax_year"), "current_tax_year": current.get("tax_year")},
                }
            )

    if previous is not None:
        prev_revenue = previous.get("revenue_total")
        curr_revenue = current.get("revenue_total")
        if prev_revenue and curr_revenue is not None and prev_revenue > 0:
            pct_change = (curr_revenue - prev_revenue) / prev_revenue
            if pct_change >= transformational_jump_pct:
                triggers.append(
                    {
                        "type": "transformational_revenue_jump",
                        "evidence": {
                            "previous_revenue": prev_revenue,
                            "current_revenue": curr_revenue,
                            "pct_change": round(pct_change, 4),
                        },
                    }
                )

    curr_revenue = current.get("revenue_total")
    if curr_revenue is not None and curr_revenue >= revenue_floor:
        prev_revenue = previous.get("revenue_total") if previous is not None else None
        if prev_revenue is None or prev_revenue < revenue_floor:
            triggers.append(
                {
                    "type": "first_filing_above_floor",
                    "evidence": {"current_revenue": curr_revenue, "revenue_floor": revenue_floor},
                }
            )

    return triggers


def select_primary_trigger(triggers: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not triggers:
        return None
    by_type = {t["type"]: t for t in triggers}
    for trigger_type in TRIGGER_PRIORITY:
        if trigger_type in by_type:
            return by_type[trigger_type]
    return triggers[0]


def resolve_angle(trigger_type: str, trigger_angles: dict[str, str | None]) -> str | None:
    """Looks up the client-config angle for a trigger type. Returns None (not a
    fabricated string) when the config has no mapping — e.g. first_filing_above_floor,
    a genuine gap in what Lauren provided, left unset deliberately rather than guessed.
    """
    return trigger_angles.get(trigger_type)
