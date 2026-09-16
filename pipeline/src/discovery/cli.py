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

    return 1


if __name__ == "__main__":
    sys.exit(main())
