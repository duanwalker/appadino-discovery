from __future__ import annotations

import argparse
import logging
import sys

logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="discovery")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("ingest", help="Run Stage 0 — populate organizations and filings")
    filter_parser = subparsers.add_parser(
        "filter", help="Run Stages 1-2 — recall filter + 990 signal extraction for a client"
    )
    filter_parser.add_argument("client_id", type=int)
    score_parser = subparsers.add_parser("score", help="Run Stage 3 — Haiku + Sonnet scoring for a client")
    score_parser.add_argument("client_id", type=int)
    score_parser.add_argument(
        "--haiku-cut-n", type=int, default=None, help="Override the Haiku-to-Sonnet cut size (config default: 3000)"
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

        counts = run_filter_and_signals(args.client_id)
        logger.info("filter complete: %s", counts)
        return 0

    if args.command == "score":
        import os

        import psycopg

        from discovery.stages.filter import select_survivor_eins
        from discovery.stages.run_scoring import run_scoring

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
