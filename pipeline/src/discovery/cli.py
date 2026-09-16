from __future__ import annotations

import argparse
import logging
import sys

logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="discovery")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("ingest", help="Run Stage 0 — populate organizations and filings")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.command == "ingest":
        from discovery.stages.ingest import run_ingest

        counts = run_ingest()
        logger.info("ingest complete: %s", counts)
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
