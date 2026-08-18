from __future__ import annotations

import argparse
from collections.abc import Sequence


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nextrip-pipeline",
        description="NexTrip automated data pipeline.",
    )

    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser(
        "health",
        help="Check that the pipeline package is available.",
    )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)

    if arguments.command == "health":
        print("NexTrip data pipeline is ready.")
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())