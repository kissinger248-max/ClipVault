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

    if args.command == "cleanup":
        from .worker import Worker

        expired = Worker(settings).sweep()
        print(f"{expired} expired run(s) deleted")
        return 0

    return _make(settings, args.file, args.name)


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
