"""Manual cleanup for archive_manifest rows (and their local cache files) whose year
has aged out of default_target_years()'s rolling window (Stage 2 archive cache).

Standalone, NOT wired into discovery.cli or any scheduled job — run by hand whenever
a year ages out (e.g. once 2027 starts and 2024 drops out of the 3-year window).
Deliberately manual for now: the cache itself assumes nothing about unbounded
retention (see sync_archive_manifest), but eviction isn't on a timer yet.

Run with:
    DATABASE_URL=... python pipeline/scripts/evict_stale_archives.py [--dry-run]
"""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

import psycopg

from discovery.stages.ingest import default_target_years

logger = logging.getLogger(__name__)


def find_stale_years(conn: psycopg.Connection, keep_years: set[int]) -> list[int]:
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT year FROM archive_manifest ORDER BY year")
        all_years = [row[0] for row in cur.fetchall()]
    return [year for year in all_years if year not in keep_years]


def evict_year(conn: psycopg.Connection, year: int, dry_run: bool) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT local_path FROM archive_manifest WHERE year = %s", (year,))
        local_paths = [row[0] for row in cur.fetchall()]

    for local_path in local_paths:
        path = Path(local_path)
        if dry_run:
            logger.info("[dry-run] would delete %s", path)
            continue
        path.unlink(missing_ok=True)

    if not dry_run:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM archive_manifest WHERE year = %s", (year,))
        conn.commit()

    return len(local_paths)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="List what would be deleted without deleting anything"
    )
    args = parser.parse_args()

    database_url = os.environ["DATABASE_URL"]
    keep_years = set(default_target_years())

    with psycopg.connect(database_url) as conn:
        stale_years = find_stale_years(conn, keep_years)
        if not stale_years:
            logger.info("No stale years in archive_manifest (keeping %s)", sorted(keep_years))
            return
        for year in stale_years:
            count = evict_year(conn, year, args.dry_run)
            verb = "would evict" if args.dry_run else "evicted"
            logger.info("%s %d archive(s) for year=%s", verb, count, year)


if __name__ == "__main__":
    main()
