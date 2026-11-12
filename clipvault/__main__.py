"""Command-line entry point: ``python -m clipvault <command>``."""

from __future__ import annotations

import argparse
import logging
import sys

from clipvault import __version__, doctor
from clipvault import config as cfg
from clipvault.logging_setup import ctx, setup_logging

# Commands named in CLAUDE.md that arrive in later milestones.
_PLANNED = {
    "run": "M3",
    "discover": "M1",
    "fetch": "M2",
    "render": "M7",
    "publish": "M9",
}


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(prog="clipvault", description="ClipVault command line")
    parser.add_argument("--version", action="version", version=f"clipvault {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="check that everything ClipVault needs is installed")
    for name, milestone in _PLANNED.items():
        planned = sub.add_parser(name, help=f"(not built yet, arrives in {milestone})")
        planned.add_argument("args", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the CLI and return the process exit code."""
    args = build_parser().parse_args(argv)
    setup_logging(cfg.logs_dir())
    log = logging.getLogger("clipvault.cli")
    log.info("command started: %s", args.command, extra=ctx(job=args.command))

    if args.command == "doctor":
        code = doctor.main()
    else:
        print(
            f"'{args.command}' isn't built yet; it arrives in milestone "
            f"{_PLANNED[args.command]} (see docs/BUILD_PLAN.md)."
        )
        code = 2
    log.info("command finished: %s", args.command, extra=ctx(job=args.command, exit_code=code))
    return code


if __name__ == "__main__":
    sys.exit(main())
