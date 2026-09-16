"""Stage 1 — Cheap SQL recall filter (§4). No AI, no per-org cost. Wide net; err
toward recall. Every threshold is read from the client's active icp_config — nothing
tenant-variable is hardcoded here (§9's "config, not code" principle).
"""

from __future__ import annotations

from typing import Any

import psycopg

# Sensible starting values, used only when a client's config omits a key. Real values
# live in icp_configs.config and can be tuned per client without a deploy.
DEFAULT_RECALL_FILTER: dict[str, Any] = {
    "revenue_floor": 500_000,
    "revenue_ceiling": 10_000_000,
    # IRS EO BMF "FOUNDATION CODE": 00 = not 501(c)(3); 02-04 = private foundations
    # (confirmed private-foundation codes per IRS eo-info.pdf); 12 = hospital/medical
    # research org; 13 = college/university benefit org owned by a govt unit;
    # 14 = governmental unit. Together these implement "501(c)(3) public charities
    # only" and the hospitals/universities/government hard excludes (§4 Stage 1) from
    # a single BMF field.
    "exclude_foundation_codes": ["00", "02", "03", "04", "12", "13", "14"],
    # NTEE major/sub-group prefixes — recall shaping only, never a scoring input
    # (§9 rule 6). B4x/B5x = higher education; E2x = hospitals (belt-and-suspenders
    # with foundation_code 12); Y = mutual/membership benefit organizations.
    "exclude_ntee_prefixes": ["B4", "B5", "E2", "Y"],
    # Fiscally sponsored orgs (no own 990) never appear in the 990 index (§4 Stage 1) —
    # this just makes that exclusion explicit and ensures Stage 2 has something to parse.
    "require_filing_on_record": True,
}


def get_active_icp_config(conn: psycopg.Connection, client_id: int) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT config FROM icp_configs WHERE client_id = %s AND active = true "
            "ORDER BY version DESC LIMIT 1",
            (client_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise ValueError(f"no active icp_config for client_id={client_id}")
    config: dict[str, Any] = row[0]
    return config


def build_survivor_query(recall: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Pure query-builder for Stage 1's recall filter — split out from
    select_survivor_eins so the SQL it produces is unit-testable without a database.
    """
    recall = {**DEFAULT_RECALL_FILTER, **recall}

    conditions = [
        "o.revenue_latest >= %(revenue_floor)s",
        "o.revenue_latest <= %(revenue_ceiling)s",
        "(o.foundation_code IS NULL OR o.foundation_code != ALL(%(exclude_foundation_codes)s))",
    ]
    params: dict[str, Any] = {
        "revenue_floor": recall["revenue_floor"],
        "revenue_ceiling": recall["revenue_ceiling"],
        "exclude_foundation_codes": recall["exclude_foundation_codes"],
    }

    ntee_conditions = []
    for i, prefix in enumerate(recall["exclude_ntee_prefixes"]):
        key = f"ntee_prefix_{i}"
        ntee_conditions.append(f"o.ntee LIKE %({key})s")
        params[key] = f"{prefix}%"
    if ntee_conditions:
        conditions.append(f"(o.ntee IS NULL OR NOT ({' OR '.join(ntee_conditions)}))")

    if recall["require_filing_on_record"]:
        conditions.append("EXISTS (SELECT 1 FROM filings f WHERE f.ein = o.ein)")

    query = "SELECT o.ein FROM organizations o WHERE " + " AND ".join(conditions)
    return query, params


def select_survivor_eins(conn: psycopg.Connection, client_id: int) -> list[str]:
    """Stage 1 (§4): returns the survivor EINs for the client's active ICP. Geography
    tiers are ranking weights, not walls (§4), so they're intentionally not applied
    here — this filter is national and config-driven for every other criterion.
    """
    config = get_active_icp_config(conn, client_id)
    query, params = build_survivor_query(config.get("recall_filter", {}))
    with conn.cursor() as cur:
        cur.execute(query, params)
        return [row[0] for row in cur.fetchall()]
