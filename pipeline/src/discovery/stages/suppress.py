"""Stage 4 — Suppression (§4, §9 rule 7). EIN-resolvable entries suppress outright.
Every one of ARCHITECT's seed suppression entries (§4 Stage 4) is a name only, no
EIN — so this stage's fuzzy name matching is the path that actually matters in
practice, and its result is a flag, never a silent drop.
"""

from __future__ import annotations

import difflib

import psycopg

# difflib.SequenceMatcher ratio; chosen to catch near-identical names ("She Dreams In
# Color" vs "She Dreams in Color, Inc.") without over-matching unrelated orgs that
# happen to share a common word. Tunable — no data yet to calibrate against.
FUZZY_MATCH_THRESHOLD = 0.85


def normalize_name(name: str) -> str:
    return " ".join(name.strip().lower().split())


def fuzzy_match_score(name_a: str, name_b: str) -> float:
    return difflib.SequenceMatcher(None, normalize_name(name_a), normalize_name(name_b)).ratio()


def find_fuzzy_match(org_name: str, candidate_names: list[str], threshold: float = FUZZY_MATCH_THRESHOLD) -> str | None:
    """Returns the first candidate name scoring >= threshold, or None."""
    for candidate in candidate_names:
        if fuzzy_match_score(org_name, candidate) >= threshold:
            return candidate
    return None


def load_suppression_entries(conn: psycopg.Connection, client_id: int) -> list[dict[str, str | None]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ein, org_name, kind FROM suppression WHERE client_id = %s",
            (client_id,),
        )
        return [{"ein": row[0], "org_name": row[1], "kind": row[2]} for row in cur.fetchall()]


def apply_suppression(
    conn: psycopg.Connection, client_id: int, eins: list[str]
) -> tuple[list[str], dict[str, str]]:
    """§9 rule 7: returns (remaining_eins, fuzzy_flags). `remaining_eins` excludes only
    EIN-exact matches — true suppression. `fuzzy_flags` maps ein -> matched
    suppression org_name for name-only fuzzy matches; those EINs are NOT excluded
    from `remaining_eins` (flagged, not silently dropped).
    """
    entries = load_suppression_entries(conn, client_id)
    ein_suppressed = {e["ein"] for e in entries if e["ein"]}
    name_only_candidates = [e["org_name"] for e in entries if not e["ein"] and e["org_name"]]

    with conn.cursor() as cur:
        cur.execute("SELECT ein, name FROM organizations WHERE ein = ANY(%s)", (eins,))
        org_names = {row[0]: row[1] for row in cur.fetchall()}

    remaining: list[str] = []
    fuzzy_flags: dict[str, str] = {}
    for ein in eins:
        if ein in ein_suppressed:
            continue
        name = org_names.get(ein)
        if name and name_only_candidates:
            match = find_fuzzy_match(name, name_only_candidates)
            if match:
                fuzzy_flags[ein] = match
        remaining.append(ein)

    return remaining, fuzzy_flags
