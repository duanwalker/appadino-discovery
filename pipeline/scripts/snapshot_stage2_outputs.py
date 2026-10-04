"""Stage 2 regression snapshot — dumps the filings extracted columns and signals rows
for a given EIN set to deterministic, sorted JSON, so two runs (before/after a Stage 2
code change) can be diffed directly for "did this change what Stage 2 actually
produces."

Deliberately omits `extracted_at`/`computed_at`: those are real timestamps that differ
on every run by construction and would make every diff noisy regardless of whether the
underlying data changed. Everything else both tables hold is included as-is.

Run with (never touches DATABASE_URL itself beyond reading it — see pipeline/README.md
"Running against the deployed database" for how to set it):

    # EINs = whatever Stage 1 currently selects for a client (matches the regression
    # check's "client_id=2's N orgs" framing exactly, since it's the same function
    # Stage 1 itself calls):
    python pipeline/scripts/snapshot_stage2_outputs.py --client-id 2 --out before.json

    ... deploy the Stage 2 change, run a --force-refresh-signals pass over the same
    EINs (so every one is actually recomputed, not skipped by the new resume check) ...

    python pipeline/scripts/snapshot_stage2_outputs.py --client-id 2 --out after.json
    diff before.json after.json   # expect no output

Or against an explicit EIN list (one EIN per line) instead of a client's current
survivor set:

    python pipeline/scripts/snapshot_stage2_outputs.py --eins-file eins.txt --out before.json
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
    eins_source.add_argument("--client-id", type=int, help="Snapshot whatever Stage 1 currently selects for this client")
    eins_source.add_argument("--eins-file", help="Path to a file with one EIN per line")
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
