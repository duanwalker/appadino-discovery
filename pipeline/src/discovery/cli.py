from __future__ import annotations

import argparse
import logging
import re
import sys

logger = logging.getLogger(__name__)

_EIN_PATTERN = re.compile(r"\d{9}")


def parse_ein_list(raw: str) -> list[str]:
    """`score --eins` input: comma-separated, whitespace and empty segments ignored,
    each EIN exactly 9 ASCII digits (no dashes), de-duplicated preserving first-seen
    order. Raises ValueError on any malformed EIN or an empty result — a typo must
    fail loudly rather than silently shrink (or empty) a paid scoring run."""
    eins: list[str] = []
    for segment in raw.split(","):
        ein = segment.strip()
        if not ein:
            continue
        if not (ein.isascii() and _EIN_PATTERN.fullmatch(ein)):
            raise ValueError(f"invalid EIN {ein!r} (expected exactly 9 digits, no dashes)")
        if ein not in eins:
            eins.append(ein)
    if not eins:
        raise ValueError("no EINs given")
    return eins


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="discovery")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("ingest", help="Run Stage 0 — populate organizations and filings")
    filter_parser = subparsers.add_parser(
        "filter", help="Run Stages 1-2 — recall filter + 990 signal extraction for a client"
    )
    filter_parser.add_argument("client_id", type=int)
    filter_parser.add_argument(
        "--limit-eins",
        type=int,
        default=None,
        help="Bound Stage 2 to the first N post-suppression survivor EINs (~1-2 filings each) — "
        "for measuring throughput on a sample without running the full survivor set",
    )
    filter_parser.add_argument(
        "--force-refresh-signals",
        action="store_true",
        help="Reprocess every survivor EIN even if it already has a signals row, "
        "instead of Stage 2's default resume behavior of skipping already-signaled EINs",
    )
    filter_parser.add_argument(
        "--eins",
        default=None,
        help="Comma-separated EIN list that REPLACES Stage 1 + suppression entirely for this run "
        "(e.g. to run Stage 2 on an exact pilot org list) — logged as an override and recorded "
        "in runs.counts.eins_overridden, not combined with the client's normal survivor set",
    )
    score_parser = subparsers.add_parser("score", help="Run Stage 3 — Haiku + Sonnet scoring for a client")
    score_parser.add_argument("client_id", type=int)
    score_parser.add_argument(
        "--haiku-cut-n", type=int, default=None, help="Override the Haiku-to-Sonnet cut size (config default: 3000)"
    )
    score_parser.add_argument(
        "--eins",
        default=None,
        help="Comma-separated list of 9-digit EINs that REPLACES select_survivor_eins entirely for this "
        "run (e.g. a bounded, known-cost run for a QA tenant) — logged as an override and recorded in "
        "runs.counts.eins_overridden, not combined with the client's normal survivor set",
    )
    publish_parser = subparsers.add_parser(
        "publish", help="Run Stages 4-6 + QA — suppress, detect triggers, publish prospects for a client"
    )
    publish_parser.add_argument("client_id", type=int)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.command == "ingest":
        from discovery.stages.ingest import run_ingest

        counts = run_ingest()
        logger.info("ingest complete: %s", counts)
        return 0

    if args.command == "filter":
        from discovery.stages.run_filter_and_signals import run_filter_and_signals

        override_eins = (
            [ein.strip() for ein in args.eins.split(",") if ein.strip()] if args.eins is not None else None
        )
        counts = run_filter_and_signals(
            args.client_id,
            limit_eins=args.limit_eins,
            force_refresh_signals=args.force_refresh_signals,
            override_eins=override_eins,
        )
        logger.info("filter complete: %s", counts)
        return 0

    if args.command == "score":
        import os

        import psycopg

        from discovery.stages.filter import select_survivor_eins
        from discovery.stages.run_scoring import run_scoring

        if args.eins is not None:
            try:
                eins = parse_ein_list(args.eins)
            except ValueError as exc:
                score_parser.error(f"--eins: {exc}")
            # Never touches select_survivor_eins (or the DB) here: the explicit list
            # is the whole Stage 3 input, so the run's API spend is bounded by it.
            counts = run_scoring(args.client_id, eins, haiku_cut_n=args.haiku_cut_n, eins_overridden=True)
        else:
            with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
                eins = select_survivor_eins(conn, args.client_id)
            counts = run_scoring(args.client_id, eins, haiku_cut_n=args.haiku_cut_n)
        logger.info("score complete: %s", counts)
        return 0

    if args.command == "publish":
        from discovery.stages.run_publish import run_publish

        counts = run_publish(args.client_id)
        logger.info("publish complete: %s", counts)
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
