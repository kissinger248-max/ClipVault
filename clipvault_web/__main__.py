"""Command line for the website: ``python -m clipvault_web serve | make | cleanup``."""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

from clipvault.logging_setup import setup_logging

from . import settings as web_settings
from . import store


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="clipvault_web", description="ClipVault Web")
    parser.add_argument(
        "--data", type=Path, help="data folder (default: CLIPVAULT_WEB_DATA or ./web-data)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the website and its worker")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8800)

    make = sub.add_parser("make", help="make clips from a video file, without the website")
    make.add_argument("file", type=Path)
    make.add_argument("--name", help="who is in the video (used in headlines)")

    sub.add_parser("cleanup", help="delete expired clips and abandoned uploads now")
    switch = sub.add_parser("downloads", help="the switch for server downloads of other channels")
    switch.add_argument("state", choices=["on", "off", "status"])
    reports = sub.add_parser("reports", help="problem reports and takedown requests")
    reports.add_argument("--all", action="store_true", help="include handled ones")
    reports.add_argument("--done", type=int, metavar="ID", help="mark a report handled")
    reports.add_argument("--outcome", default="handled", help="what was done (with --done)")
    remove = sub.add_parser("remove", help="delete a run's clips now (a takedown)")
    remove.add_argument("run_id")
    optout = sub.add_parser("optout", help="stop a channel from being clipped (by request)")
    optout.add_argument("login")
    optout.add_argument("--id", dest="user_id", help="the channel's Twitch user id")
    block = sub.add_parser("block", help="block whoever made a run from starting new ones")
    block.add_argument("run_id")
    block.add_argument("--reason", default="")
    stats = sub.add_parser("stats", help="what happened on the website (counted, no visitor data)")
    stats.add_argument("--days", type=int, default=7)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = web_settings.from_env(args.data)
    settings.data.mkdir(parents=True, exist_ok=True)
    setup_logging(settings.logs_dir)

    if args.command == "serve":
        import uvicorn

        from .server import create_app

        print(f"ClipVault Web on http://{args.host}:{args.port} (data: {settings.data})")
        uvicorn.run(create_app(settings), host=args.host, port=args.port, log_level="warning")
        return 0

    if args.command in ("downloads", "reports", "remove", "optout", "block"):
        return _admin(settings, args)

    if args.command == "stats":
        from . import events

        conn = store.open_store(settings)
        try:
            rows = events.summary(conn, args.days)
        finally:
            conn.close()
        print(f"Last {args.days} day(s):" if rows else "Nothing counted yet.")
        for name, detail, total in rows:
            print(f"  {name:16s} {detail or '-':12s} {total:>8,}")
        return 0

    if args.command == "cleanup":
        from .worker import Worker

        expired = Worker(settings).sweep()
        print(f"{expired} expired run(s) deleted")
        return 0

    return _make(settings, args.file, args.name)


def _admin(settings, args) -> int:
    """The commands a person running the website uses (takedowns, the download switch)."""
    from clipvault.timeutil import to_rfc3339, utc_now

    conn = store.open_store(settings)
    try:
        if args.command == "downloads":
            if args.state != "status":
                store.set_downloads(conn, args.state == "on", "set by hand")
            on = store.downloads_on(conn, settings)
            why = store.get_setting(conn, store.DOWNLOADS) or ("on" if on else "off") + " (default)"
            print(f"Server downloads of other channels: {'ON' if on else 'OFF'} ({why})")
            if on:
                print("Read docs/PROGRESS.md (W3 rules check) before leaving this on in public.")
        elif args.command == "reports":
            if args.done:
                conn.execute("UPDATE reports SET handled_at = ?, outcome = ? WHERE id = ?",
                             (to_rfc3339(utc_now()), args.outcome, args.done))  # fmt: skip
            where = "" if args.all else "WHERE handled_at IS NULL"
            rows = conn.execute(f"SELECT * FROM reports {where} ORDER BY created_at").fetchall()
            print(f"{len(rows)} report(s)")
            for r in rows:
                print(f"  #{r['id']} {r['created_at']} {r['reason']} run={r['run_id'] or '-'} "
                      f"contact={r['contact'] or '-'} {r['outcome'] or ''}")  # fmt: skip
                if r["details"]:
                    print(f"     {r['details'][:300]}")
        elif args.command == "remove":
            if store.get_run(conn, args.run_id) is None:
                print("no such run", file=sys.stderr)
                return 2
            store.remove_run(conn, settings, args.run_id)
            print(f"run {args.run_id}: files deleted")
        elif args.command == "optout":
            user_id = args.user_id
            if not user_id:
                from . import twitch_web

                client = twitch_web.build_client(settings)
                try:
                    users = client.get_users(logins=[args.login.lower()])
                finally:
                    client.close()
                if not users:
                    print("no such Twitch channel", file=sys.stderr)
                    return 2
                user_id = users[0]["id"]
            removed = store.opt_out(conn, settings, user_id, args.login, "takedown")
            print(f"{args.login} opted out; {removed} run(s) deleted")
        elif args.command == "block":
            run = store.get_run(conn, args.run_id)
            if run is None or not run["visitor"]:
                print("no such run (or no visitor recorded)", file=sys.stderr)
                return 2
            conn.execute("INSERT OR REPLACE INTO blocked (visitor, reason, created_at) "
                         "VALUES (?, ?, ?)", (run["visitor"], args.reason or f"run {run['id']}",
                                              to_rfc3339(utc_now())))  # fmt: skip
            store.remove_run(conn, settings, args.run_id)
            print("blocked, and the run's files deleted")
    finally:
        conn.close()
    return 0


def _make(settings, file: Path, name: str | None) -> int:
    """Run the whole website pipeline on a local file and say how long each part took."""
    from clipvault.render import ffmpeg

    from . import making

    if not file.is_file():
        print(f"no such file: {file}", file=sys.stderr)
        return 2
    info = ffmpeg.probe(file)
    run_id = store.new_run_id()
    folder = store.run_dir(settings, run_id)
    folder.mkdir(parents=True, exist_ok=True)
    source = folder / f"source{file.suffix.lower()}"
    shutil.copy2(file, source)
    conn = store.open_store(settings)
    try:
        store.create_run(conn, settings, run_id=run_id, kind="upload", source_name=file.name,
                         source=source, duration=info.duration, width=info.width,
                         height=info.height, name=name, visitor=None)  # fmt: skip
        store.claim_next(conn)
    finally:
        conn.close()
    print(f"{file.name}: {info.duration / 60:.1f} min, {info.width}x{info.height}")
    started = time.monotonic()
    status = making.make_run(settings, run_id)
    total = time.monotonic() - started
    conn = store.open_store(settings)
    try:
        run = store.get_run(conn, run_id)
        rows = store.clips(conn, run_id)
    finally:
        conn.close()
    print(f"{status} in {total:.0f} s | {run['more_found']} more moment(s) found")
    for row in rows:
        path = store.resolve(settings, row["path"])
        qa = "QA passed" if row["qa_passed"] else "QA FAILED"
        length = row["end"] - row["start"]
        print(f"  {row['n']}. {row['start']:7.1f}-{row['end']:7.1f} s ({length:.0f} s) "
              f"score {row['score']:5.1f} [{row['reasons']}] {row['layout'] or '-'} | {qa} | "
              f"{row['seconds'] or 0:.0f} s | \"{row['headline']}\"")  # fmt: skip
        print(f"     {path or row['error']}")
    if run["error"]:
        print(f"error: {run['error']}")
    return 0 if status == "done" else 1


if __name__ == "__main__":
    sys.exit(main())
