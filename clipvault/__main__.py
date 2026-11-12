"""Command-line entry point: ``python -m clipvault <command>``."""

from __future__ import annotations

import argparse
import getpass
import logging
import sys
from pathlib import Path

from clipvault import __version__, db, doctor
from clipvault import config as cfg
from clipvault.logging_setup import ctx, setup_logging

# Commands named in CLAUDE.md that arrive in later milestones.
_PLANNED = {
    "run": "M3",
    "fetch": "M2",
    "render": "M7",
    "publish": "M9",
}
STREAMERS_FILENAME = "streamers.txt"


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(prog="clipvault", description="ClipVault command line")
    parser.add_argument("--version", action="version", version=f"clipvault {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="check that everything ClipVault needs is installed")

    discover = sub.add_parser("discover", help="find new clips for the streamers on the list")
    discover.add_argument("--streamer", metavar="LOGIN", help="only this streamer")
    discover.add_argument(
        "--since", metavar="AGE", help="look back this far instead of resuming (e.g. 12h, 7d)"
    )

    streamers = sub.add_parser("streamers", help="manage the streamer list")
    streamers_sub = streamers.add_subparsers(dest="action", required=True)
    imp = streamers_sub.add_parser("import", help="add streamers from a text file")
    imp.add_argument("file", nargs="?", help=f"default: {STREAMERS_FILENAME} in the home folder")
    streamers_sub.add_parser("list", help="show the streamer list")

    secrets = sub.add_parser("secrets", help="store API keys in the OS keychain")
    secrets_sub = secrets.add_subparsers(dest="action", required=True)
    for action, text in (("set", "store a key (asks for it, hidden)"), ("delete", "remove a key")):
        one = secrets_sub.add_parser(action, help=text)
        one.add_argument("name", choices=cfg.USER_SECRET_NAMES)
    secrets_sub.add_parser("list", help="show which keys are stored (never their values)")

    for name, milestone in _PLANNED.items():
        planned = sub.add_parser(name, help=f"(not built yet, arrives in {milestone})")
        planned.add_argument("args", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the CLI and return the process exit code."""
    _make_console_safe()
    args = build_parser().parse_args(argv)
    setup_logging(cfg.logs_dir())
    log = logging.getLogger("clipvault.cli")
    log.info("command started: %s", args.command, extra=ctx(job=args.command))
    handlers = {
        "doctor": lambda _args: doctor.main(),
        "discover": _cmd_discover,
        "streamers": _cmd_streamers,
        "secrets": _cmd_secrets,
    }
    try:
        if args.command in handlers:
            code = handlers[args.command](args)
        else:
            print(
                f"'{args.command}' isn't built yet; it arrives in milestone "
                f"{_PLANNED[args.command]} (see docs/BUILD_PLAN.md)."
            )
            code = 2
    except cfg.ConfigError as exc:
        print(f"Problem in config.yaml: {exc}")
        code = 1
    except Exception:
        log.exception("command crashed: %s", args.command, extra=ctx(job=args.command))
        print(f"'{args.command}' stopped with an unexpected error; details are in the log file.")
        code = 1
    log.info("command finished: %s", args.command, extra=ctx(job=args.command, exit_code=code))
    return code


def _make_console_safe() -> None:
    """Never crash on a clip title the console can't display (common on Windows)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


# --- discover ----------------------------------------------------------------


def _cmd_discover(args: argparse.Namespace) -> int:
    from clipvault import twitch
    from clipvault.discover.viewer_clips import discover_all
    from clipvault.streamers import normalize_login
    from clipvault.timeutil import parse_duration

    config = cfg.load_config()
    try:
        since = parse_duration(args.since) if args.since else None
        login = normalize_login(args.streamer) if args.streamer else None
    except ValueError as exc:
        print(exc)
        return 2

    conn = db.open_db(cfg.db_path())
    try:
        if (
            login
            and not conn.execute("SELECT 1 FROM streamers WHERE login = ?", (login,)).fetchone()
        ):
            print(f"{login} is not on the streamer list. Add them with 'streamers import' first.")
            return 1
        if not conn.execute("SELECT 1 FROM streamers").fetchone():
            print(
                "The streamer list is empty. Put one Twitch login per line in "
                f"{cfg.home_dir() / STREAMERS_FILENAME} and run: "
                "python -m clipvault streamers import"
            )
            return 1
        try:
            client = twitch.build_client()
        except twitch.MissingCredentials as exc:
            print(exc)
            return 1
        try:
            results = discover_all(conn, client, config, login=login, since=since)
        except (twitch.TwitchAuthError, twitch.TwitchAPIError) as exc:
            print(f"Twitch problem: {exc}")
            return 1
        finally:
            client.close()
    finally:
        conn.close()

    if not results:
        print("Nothing to do: saving is switched off for the streamer(s) asked for.")
        return 0
    for result in results:
        if result.error:
            print(f"{result.login}: FAILED - {result.error}")
            continue
        print(
            f"{result.login}: {len(result.new_clips)} new clip(s) "
            f"({result.seen} seen over {result.days} day(s))"
        )
        for clip in sorted(result.new_clips, key=lambda c: -c.view_count):
            day = clip.created_at.strftime("%Y-%m-%d")
            print(f"  {clip.view_count:>8,} views  {day}  {clip.duration:>4.0f}s  {clip.title}")
            print(f"  {'':>8}        {clip.url}")
    total = sum(len(r.new_clips) for r in results)
    failed = sum(1 for r in results if r.error)
    print(f"\n{total} new clip(s) across {len(results)} streamer(s); {failed} failed.")
    return 1 if failed else 0


# --- streamers ---------------------------------------------------------------


def _cmd_streamers(args: argparse.Namespace) -> int:
    from clipvault.streamers import StreamersFileError, import_streamers, parse_streamers_text

    conn = db.open_db(cfg.db_path())
    try:
        if args.action == "list":
            rows = conn.execute("SELECT * FROM streamers ORDER BY login").fetchall()
            if not rows:
                print("The streamer list is empty.")
            for row in rows:
                pick = (
                    f"min_views={row['min_views']}"
                    if row["min_views"] is not None
                    else f"top_n={row['top_n']}"
                )
                flags = f"save={'on' if row['save_enabled'] else 'off'}"
                flags += f" post={'on' if row['post_enabled'] else 'off'}"
                name = row["display_name"] or "(not looked up yet)"
                print(f"{row['login']:<26} {name:<26} {pick:<16} {flags}")
            return 0

        path = Path(args.file) if args.file else cfg.home_dir() / STREAMERS_FILENAME
        if not path.is_file():
            print(f"File not found: {path}\nCreate it with one Twitch login per line.")
            return 1
        try:
            # utf-8-sig: Notepad on Windows may add an invisible marker at the start.
            entries = parse_streamers_text(path.read_text(encoding="utf-8-sig"))
        except StreamersFileError as exc:
            print(f"Nothing imported; fix these in {path}:\n{exc}")
            return 1
        with db.transaction(conn):
            result = import_streamers(conn, entries)
    finally:
        conn.close()
    print(
        f"{len(result.added)} added, {len(result.updated)} updated, "
        f"{len(result.unchanged)} unchanged."
    )
    if result.added:
        print("Added: " + ", ".join(result.added))
    return 0


# --- secrets -----------------------------------------------------------------


def _cmd_secrets(args: argparse.Namespace) -> int:
    if args.action == "list":
        for name in cfg.USER_SECRET_NAMES:
            print(f"{name:<22} {'stored' if cfg.get_secret(name) else 'not set'}")
        return 0
    if args.action == "delete":
        cfg.delete_secret(args.name)
        print(f"{args.name} removed.")
        return 0
    value = getpass.getpass(f"Paste the value for {args.name} (it stays hidden), then Enter: ")
    value = value.strip()
    if not value:
        print("Nothing entered; nothing stored.")
        return 1
    cfg.set_secret(args.name, value)
    print(f"{args.name} stored in the OS keychain.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
