from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="canawler")
    commands = parser.add_subparsers(dest="command", required=True)

    build = commands.add_parser("build", help="rebuild processed and public data")
    build.add_argument(
        "--export",
        type=Path,
        help="Strava export directory (auto-detected when exactly one exists)",
    )

    audit = commands.add_parser("audit", help="rebuild and flag suspicious matches")
    audit.add_argument(
        "--export",
        type=Path,
        help="Strava export directory (auto-detected when exactly one exists)",
    )

    reference = commands.add_parser(
        "reference", help="inspect or rebuild NPS reference data"
    )
    reference_commands = reference.add_subparsers(
        dest="reference_command", required=True
    )
    reference_commands.add_parser("inspect", help="list CHOH trail names and labels")
    reference_commands.add_parser("build", help="rebuild the canonical towpath")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    from canawler.activities import ActivityBuildError
    from canawler.coverage import CoverageError
    from canawler.reference import ReferenceDataError

    args = _parser().parse_args(argv)
    try:
        if args.command == "reference":
            from canawler.reference import (
                build_reference,
                fetch_choh_geojson,
                inspect_reference,
            )

            source = fetch_choh_geojson()
            if args.reference_command == "inspect":
                inspect_reference(source)
            else:
                print(build_reference(source).format())
        elif args.command == "audit":
            from canawler.activities import audit_historical_activities

            print(audit_historical_activities(args.export))
        else:
            from canawler.activities import build_historical_activities

            print(build_historical_activities(args.export).format())
    except (ActivityBuildError, CoverageError, ReferenceDataError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


__all__ = ["main"]
