"""Stage 2 regression snapshot — dumps the filings extracted columns and signals rows
for a given EIN set to deterministic, sorted JSON, so two runs (before/after a Stage 2
code change) can be diffed directly for "did this change what Stage 2 actually
produces."

Deliberately omits `extracted_at`/`computed_at`: those are real timestamps that differ
on every run by construction and would make every diff noisy regardless of whether the
underlying data changed. Everything else both tables hold is included as-is.

Use --eins-file for a regression diff — NOT --client-id. --client-id runs Stage 1's
*current* recall filter (select_survivor_eins) for that client, which is the client's
whole survivor set (could be hundreds or thousands of EINs) and can change between the
"before" and "after" snapshot if organizations/filings data changes in between or the
client's icp_config's recall_filter does — neither of which makes it "the same N orgs"
across two runs. It does NOT match any specific smaller cohort (e.g. a pilot client's
published-prospect count) — that's a downstream number (post Stage 2/3/suppression),
not Stage 1's raw survivor count. --client-id is for a quick broad sanity check, not
the regression diff itself.

For an actual before/after diff, write the exact EIN list you care about to a file
(one EIN per line) yourself and use --eins-file, so both snapshots are guaranteed to
cover identical EINs regardless of what else changes in the DB between them:

Run with (never touches DATABASE_URL itself beyond reading it — see pipeline/README.md
"Running against the deployed database" for how to set it):

    python pipeline/scripts/snapshot_stage2_outputs.py --eins-file eins.txt --out before.json

    ... deploy the Stage 2 change, run a --force-refresh-signals pass over the same
    EINs (so every one is actually recomputed, not skipped by the new resume check) ...

    python pipeline/scripts/snapshot_stage2_outputs.py --eins-file eins.txt --out after.json
    diff before.json after.json   # expect no output

Or, for a broad (large, slower-to-diff) sanity check against a client's entire current
Stage 1 survivor set instead of a specific EIN list:

    python pipeline/scripts/snapshot_stage2_outputs.py --client-id 2 --out before.json
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

import psycopg


def _load_eins(conn: psycopg.Connection, args: argparse.Namespace) -> list[str]:
    if args.client_id is not None:
        from discovery.stages.filter import select_survivor_eins

        return select_survivor_eins(conn, args.client_id)
    eins_text = Path(args.eins_file).read_text()
    return [line.strip() for line in eins_text.splitlines() if line.strip()]


def snapshot_filings(conn: psycopg.Connection, eins: list[str]) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ein, tax_year, object_id, revenue_total, contributions, program_revenue,
                   govt_grants, fundraising_expense, officers, mission_text, program_text,
                   significant_change_ind
            FROM filings
            WHERE ein = ANY(%s)
            ORDER BY ein, tax_year
            """,
            (eins,),
        )
        columns = [desc.name for desc in cur.description]
        rows = [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]
    return sorted(rows, key=lambda row: (row["ein"], row["tax_year"]))


def snapshot_signals(conn: psycopg.Connection, eins: list[str]) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ein, tax_year, signal FROM signals WHERE ein = ANY(%s) ORDER BY ein, tax_year",
            (eins,),
        )
        columns = [desc.name for desc in cur.description]
        rows = [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]
    return sorted(rows, key=lambda row: (row["ein"], row["tax_year"]))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    eins_source = parser.add_mutually_exclusive_group(required=True)
    eins_source.add_argument(
        "--eins-file",
        help="Path to a file with one EIN per line — use this for a before/after regression diff, "
        "so both snapshots cover exactly the same EINs regardless of other DB changes in between",
    )
    eins_source.add_argument(
        "--client-id",
        type=int,
        help="Snapshot whatever Stage 1's recall filter CURRENTLY selects for this client — the "
        "client's whole survivor set (can be large, and can change between two runs); a broad "
        "sanity check, NOT the same thing as a fixed EIN list for a regression diff",
    )
    parser.add_argument("--out", required=True, help="Output JSON path")
    args = parser.parse_args()

    database_url = os.environ["DATABASE_URL"]
    with psycopg.connect(database_url) as conn:
        eins = _load_eins(conn, args)
        snapshot = {
            "eins_count": len(eins),
            "filings": snapshot_filings(conn, eins),
            "signals": snapshot_signals(conn, eins),
        }

    Path(args.out).write_text(json.dumps(snapshot, indent=2, sort_keys=True, default=str))
    print(f"wrote {len(snapshot['filings'])} filings rows, {len(snapshot['signals'])} signals rows for "
          f"{snapshot['eins_count']} EINs -> {args.out}")


if __name__ == "__main__":
    main()
