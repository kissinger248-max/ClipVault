"""Command-line entry point: ``python -m clipvault <command>``."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from clipvault import __version__, db, doctor
from clipvault import config as cfg
from clipvault.logging_setup import ctx, setup_logging

# Commands named in CLAUDE.md that arrive in later milestones.
_PLANNED = {
    "publish": "M9",
}
STREAMERS_FILENAME = "streamers.txt"
TOP_SHOWN = 5  # new clips listed per streamer by `discover` unless --all is given


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(prog="clipvault", description="ClipVault command line")
    parser.add_argument("--version", action="version", version=f"clipvault {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="check that everything ClipVault needs is installed")
    sub.add_parser("selftest", help="check that this build has every tool and file (offline)")

    discover = sub.add_parser("discover", help="find new clips for the streamers on the list")
    discover.add_argument("--streamer", metavar="LOGIN", help="only this streamer")
    discover.add_argument(
        "--since", metavar="AGE", help="look back this far instead of resuming (e.g. 12h, 7d)"
    )

    discover.add_argument("--all", action="store_true", help="list every new clip, not the top 5")

    fetch = sub.add_parser("fetch", help="download the clips that were found into the library")
    fetch.add_argument("--streamer", metavar="LOGIN", help="only this streamer")
    fetch.add_argument("--limit", metavar="N", type=int, help="download at most N clips")

    cleanup = sub.add_parser(
        "cleanup", help="tidy the library: retention, disk check, files deleted by hand"
    )
    cleanup.add_argument(
        "--dry-run", action="store_true", help="show what retention would delete; delete nothing"
    )

    transcribe = sub.add_parser(
        "transcribe", help="make subtitles and searchable transcripts for saved clips"
    )
    transcribe.add_argument("--streamer", metavar="LOGIN", help="only this streamer")
    transcribe.add_argument("--limit", metavar="N", type=int, help="at most N clips")

    search = sub.add_parser("search", help="find clips by what was said in them")
    search.add_argument("words", nargs="+", help="the words or phrase to look for")
    search.add_argument("--limit", metavar="N", type=int, default=10, help="results to show")

    headlines = sub.add_parser("headlines", help="write hook headlines for the best clips (AI)")
    headlines.add_argument("--streamer", metavar="LOGIN", help="only this streamer")
    headlines.add_argument("--limit", metavar="N", type=int, default=20, help="clips to write")

    render = sub.add_parser("render", help="make phone-ready vertical videos with captions")
    render.add_argument("clip_id", nargs="?", help="one clip; leave out to render the top clips")
    render.add_argument("--layout", choices=("letterbox", "split", "native"))
    render.add_argument("--headline", help="headline text (default: taken from the clip title)")
    render.add_argument("--streamer", metavar="LOGIN", help="only this streamer")
    render.add_argument("--limit", metavar="N", type=int, default=5, help="clips to render (5)")

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
        if action == "set":
            one.add_argument(
                "--window", action="store_true", help="ask in a pop-up window, not the terminal"
            )
    secrets_sub.add_parser("list", help="show which keys are stored (never their values)")

    vods = sub.add_parser("vods", help="search finished streams' chat for moments nobody clipped")
    vods.add_argument("--streamer", metavar="LOGIN", help="only this streamer")
    vods.add_argument("--limit", type=int, metavar="N", help="search at most N streams")
    run = sub.add_parser("run", help="keep running: schedule, control panel and tray icon")
    run.add_argument("--no-panel", action="store_true", help="no control panel (scheduler only)")
    run.add_argument("--no-tray", action="store_true", help="no tray icon")
    run.add_argument("--no-browser", action="store_true", help="don't open the panel on start")
    shown = run.add_mutually_exclusive_group()
    shown.add_argument(
        "--window",
        action="store_true",
        help="show the panel in ClipVault's own window (default for the installed app)",
    )
    shown.add_argument("--no-window", action="store_true", help="use the web browser instead")
    win = sub.add_parser("window", help="(started by 'run') ClipVault's own window for the panel")
    win.add_argument("--url", help="the running panel's address (default: from config.yaml)")
    twitch_cmd = sub.add_parser("twitch", help="your Twitch login (needed to read live chat)")
    twitch_sub = twitch_cmd.add_subparsers(dest="action", required=True)
    twitch_sub.add_parser("login", help="log in with a code at twitch.tv/activate")
    twitch_sub.add_parser("logout", help="forget the Twitch login")
    twitch_sub.add_parser("status", help="show who is logged in")
    sub.add_parser("live", help="watch live chat of saved streamers now (Ctrl+C stops)")
    panel = sub.add_parser("panel", help="control-panel helpers")
    panel_sub = panel.add_subparsers(dest="action", required=True)
    panel_sub.add_parser(
        "reset-password", help="forget the panel password; choose a new one on next open"
    )
    sub.add_parser("pause", help="pause scheduled work (a running 'run' finishes its current step)")
    sub.add_parser("resume", help="resume scheduled work")
    notify = sub.add_parser("notify", help="phone notifications through the free ntfy app")
    notify_sub = notify.add_subparsers(dest="action", required=True)
    notify_sub.add_parser("setup", help="make a private topic; shows how to subscribe on a phone")
    notify_sub.add_parser("test", help="send a test summary and a test alert to the phone now")
    notify_sub.add_parser("status", help="show what is waiting to be sent")

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
        "selftest": lambda _args: __import__("clipvault.selftest", fromlist=["main"]).main(),
        "discover": _cmd_discover,
        "fetch": _cmd_fetch,
        "cleanup": _cmd_cleanup,
        "transcribe": _cmd_transcribe,
        "render": _cmd_render,
        "search": _cmd_search,
        "headlines": _cmd_headlines,
        "streamers": _cmd_streamers,
        "secrets": _cmd_secrets,
        "run": _cmd_run,
        "window": _cmd_window,
        "panel": _cmd_panel,
        "twitch": _cmd_twitch,
        "live": _cmd_live,
        "vods": _cmd_vods,
        "pause": lambda _args: _set_paused(True),
        "resume": lambda _args: _set_paused(False),
        "notify": _cmd_notify,
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


def _say(line: str) -> None:
    """Print a progress line straight away, even when output goes to a file or pipe."""
    print(line, flush=True)


def _megabytes(size: int) -> str:
    return (
        f"{size / 1_048_576:,.1f} MB" if size < 1_073_741_824 else f"{size / 1_073_741_824:,.2f} GB"
    )


# --- fetch and cleanup ---------------------------------------------------------


def _cmd_fetch(args: argparse.Namespace) -> int:
    from clipvault import jobs
    from clipvault.alerts import open_alerts
    from clipvault.fetch import ytdlp
    from clipvault.streamers import normalize_login

    config = cfg.load_config()
    try:
        login = normalize_login(args.streamer) if args.streamer else None
    except ValueError as exc:
        print(exc)
        return 2
    conn = db.open_db(cfg.db_path())
    try:
        if ytdlp.update_if_due(conn):
            print("Checked for a newer downloader (yt-dlp).")
        summary = jobs.run_fetch(conn, config, login=login, limit=args.limit, on_progress=_say)
        alerts = [a["message"] for a in open_alerts(conn)]
    finally:
        conn.close()

    saved = summary.downloaded + summary.already_there
    print(f"\n{saved} clip(s) saved to {config.library_root} ({_megabytes(summary.bytes)}).")
    notes = [
        (summary.already_there, "were already on disk and only needed recording"),
        (summary.duplicates, "skipped as repeats of another clip"),
        (summary.unavailable, "no longer on Twitch"),
        (summary.failed, "failed and will be retried"),
        (summary.waiting, "waiting for their retry time"),
        (summary.remaining, "still to download"),
    ]
    for count, text in notes:
        if count:
            print(f"  {count} {text}")
    if summary.library_away:
        print("The library folder is away; new clips are in a temporary folder for now.")
    if summary.paused_for_disk:
        print("STOPPED: the disk is too full (see the storage setting in config.yaml).")
    if summary.stopped_on_failures:
        print("STOPPED: several downloads failed in a row. Check your connection, then run again.")
    for message in alerts:
        print(f"ALERT: {message}")
    problem = summary.failed or summary.paused_for_disk or summary.stopped_on_failures
    return 1 if problem else 0


def _cmd_cleanup(args: argparse.Namespace) -> int:
    from clipvault.alerts import open_alerts
    from clipvault.library import retention, storage

    config = cfg.load_config()
    conn = db.open_db(cfg.db_path())
    try:
        library = storage.open_library(conn, config)
        if not library.available:
            print(f"The library folder {library.root} can't be found; nothing was changed.")
            return 1
        moved = storage.relocate_cache(library)
        if moved:
            print(f"Moved {moved} file(s) from the temporary folder into the library.")
        missing = retention.mark_missing_files(conn, library)
        if missing:
            print(f"{missing} clip(s) were deleted by hand; they are marked and won't come back.")

        plan = retention.plan_retention(conn, config, library)
        size = _megabytes(sum(item.size for item in plan))
        days = config.retention.keep_days_below_median
        if not plan:
            print(f"Retention: nothing to remove (clips are kept at least {days} days).")
        elif args.dry_run:
            print(f"Retention would remove {len(plan)} clip(s), {size} (older than {days} days")
            print("and below the streamer's median views). Nothing was deleted:")
            for item in plan[:20]:
                median = f"median {item.median_views:,.0f}"
                print(f"  {item.view_count:>8,} views ({median})  {item.relpath}")
            if len(plan) > 20:
                print(f"  ... and {len(plan) - 20} more")
        else:
            removed = retention.apply_retention(conn, library, plan)
            print(f"Retention removed {removed} clip(s), {size}.")

        ok = retention.check_disk(conn, config, library)
        free = retention.free_space_pct(library.write_base)
        limit = config.retention.min_free_space_pct
        state = "OK" if ok else "TOO FULL, downloads are paused"
        print(f"Disk: {free:.0f}% free (downloads pause below {limit}%): {state}.")
        for alert in open_alerts(conn):
            print(f"ALERT: {alert['message']}")
    finally:
        conn.close()
    return 0 if ok else 1


def _cmd_transcribe(args: argparse.Namespace) -> int:
    from clipvault import jobs
    from clipvault.streamers import normalize_login

    config = cfg.load_config()
    if not config.transcription.enabled:
        print("Transcripts are switched off (transcription.enabled in config.yaml).")
        return 0
    try:
        login = normalize_login(args.streamer) if args.streamer else None
    except ValueError as exc:
        print(exc)
        return 2
    conn = db.open_db(cfg.db_path())
    try:
        summary = jobs.run_transcribe(conn, config, login=login, limit=args.limit, on_progress=_say)
    finally:
        conn.close()
    used = ", ".join(f"{count} by {name}" for name, count in summary.providers_used.items())
    total = summary.done + summary.already_there
    print(f"\n{total} clip(s) transcribed ({summary.words:,} words{'; ' + used if used else ''}).")
    notes = [
        (summary.no_speech, "had no speech in them"),
        (summary.failed, "failed and will be retried"),
        (summary.waiting, "waiting for their retry time"),
        (summary.remaining, "still to do"),
    ]
    for count, text in notes:
        if count:
            print(f"  {count} {text}")
    if summary.stopped_on_failures:
        print("STOPPED: several clips failed in a row; the last error was:")
        print(f"  {summary.errors[-1]}")
    return 1 if summary.failed else 0


def _cmd_render(args: argparse.Namespace) -> int:
    from clipvault.process import candidates
    from clipvault.render import engine
    from clipvault.render.ffmpeg import RenderError
    from clipvault.streamers import normalize_login

    config = cfg.load_config()
    conn = db.open_db(cfg.db_path())
    try:
        if args.clip_id:
            clip_ids = [args.clip_id]
        else:
            # Best-scoring posting candidates without a vertical version yet (SPEC §7),
            # taking turns between streamers.
            login = normalize_login(args.streamer) if args.streamer else None
            ranked = candidates.rank_candidates(conn, login=login)
            clip_ids = [c.clip_id for c in candidates.spread_across_streamers(ranked, args.limit)]
        if not clip_ids:
            print("Nothing to render: no saved clip of 15 s or more is waiting.")
            return 0
        failed = 0
        for index, clip_id in enumerate(clip_ids, start=1):
            try:
                result = engine.render_clip(
                    conn, config, clip_id, layout=args.layout, headline=args.headline
                )
            except RenderError as exc:
                failed += 1
                _say(f"[{index}/{len(clip_ids)}] {clip_id}: FAILED - {exc}")
                continue
            verdict = "QA passed" if result.report.passed else "QA FAILED"
            _say(
                f"[{index}/{len(clip_ids)}] {result.layout:<9} {verdict}  "
                f"{result.seconds:4.0f} s  {result.path}"
            )
            for check in result.report.failures():
                _say(f"      {check.name}: {check.detail}")
    finally:
        conn.close()
    print(f"\nPreview pictures with the phone buttons drawn in: {cfg.home_dir() / 'qa'}")
    return 1 if failed else 0


def _cmd_headlines(args: argparse.Namespace) -> int:
    from clipvault import jobs
    from clipvault.process import write
    from clipvault.streamers import normalize_login

    config = cfg.load_config()
    provider = write.build_provider(config)
    if provider is None:
        print("Headlines are written by an AI model, which needs an API key (Groq by default).")
        print("Store one with: python -m clipvault secrets set llm_api_key --window")
        print("Until then every vertical video uses a headline made from the clip's title.")
        return 1
    login = normalize_login(args.streamer) if args.streamer else None
    conn = db.open_db(cfg.db_path())
    try:
        summary = jobs.run_write(conn, config, provider, login=login, limit=args.limit)
    finally:
        conn.close()
    for clip_id, streamer, headline in summary.examples:
        _say(f"{streamer:<16} {headline}    ({clip_id})")
    print(f"\n{summary.written} headline(s) written; {summary.unusable} clip(s) got no usable one.")
    for error in summary.errors:
        print(f"Problem: {error}")
    return 1 if summary.errors else 0


def _cmd_search(args: argparse.Namespace) -> int:
    from clipvault.library.search import search_transcripts

    config = cfg.load_config()
    conn = db.open_db(cfg.db_path())
    try:
        rows = search_transcripts(conn, " ".join(args.words), args.limit)
    finally:
        conn.close()
    if not rows:
        print("No clip says that. (Only transcribed clips can be searched.)")
        return 1
    for row in rows:
        day = (row["created_at"] or "")[:10]
        print(f"{row['login']}  {day}  {row['view_count'] or 0:,} views  {row['title']}")
        print(f'    "{row["snippet"]}"')
        print(f"    {config.library_root / row['file_path']}")
    return 0


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
        ranked = sorted(result.new_clips, key=lambda c: -c.view_count)
        shown = ranked if args.all else ranked[:TOP_SHOWN]
        for clip in shown:
            day = clip.created_at.strftime("%Y-%m-%d")
            print(f"  {clip.view_count:>8,} views  {day}  {clip.duration:>4.0f}s  {clip.title}")
            print(f"  {'':>8}        {clip.url}")
        if len(ranked) > len(shown):
            print(f"  ... and {len(ranked) - len(shown)} more (add --all to list every one)")
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


def _cmd_vods(args: argparse.Namespace) -> int:
    from clipvault import twitch
    from clipvault.discover import vods
    from clipvault.streamers import normalize_login
    from clipvault.twitch.chat_replay import find_tool

    config = cfg.load_config()
    tool = find_tool(cfg.home_dir())
    if tool is None:
        print("TwitchDownloaderCLI is needed to read stream chat and isn't installed.")
        print("Run: python -m clipvault doctor   (it says where to get it and where to put it)")
        return 1
    login = normalize_login(args.streamer) if args.streamer else None
    conn = db.open_db(cfg.db_path())
    try:
        client = twitch.build_client()
    except twitch.MissingCredentials as exc:
        conn.close()
        print(exc)
        return 1
    try:
        found = vods.find_vods(conn, client, login=login)
        _say(f"{found} new finished stream(s) found. Reading chat (a long stream takes minutes)...")
        summary = vods.process_vods(
            conn, config, tool, client=client, login=login, limit=args.limit
        )
    except (twitch.TwitchAuthError, twitch.TwitchAPIError) as exc:
        print(f"Twitch problem: {exc}")
        return 1
    finally:
        client.close()
        conn.close()
    print(
        f"Searched {summary.processed} stream(s): {summary.moments} moment(s) nobody clipped. "
        "They are downloaded by: python -m clipvault fetch"
    )
    if summary.skipped:
        print(f"  {summary.skipped} skipped (chat not available, e.g. subscriber-only)")
    if summary.failed:
        print(f"  {summary.failed} failed and will be retried later")
    if summary.waiting:
        print(f"  {summary.waiting} waiting for their retry time")
    return 1 if summary.failed else 0


# --- running unattended ------------------------------------------------------


def _cmd_run(args: argparse.Namespace) -> int:
    import threading
    import webbrowser

    from clipvault import app
    from clipvault.alerts import is_paused

    config = cfg.load_config()
    from clipvault import desktop, lifecycle, updates

    conn = db.open_db(cfg.db_path())  # also brings the database up to date before jobs start
    try:
        paused = is_paused(conn)
        desktop.first_run(conn)  # installed app: start at login, local time zone (once)
        updates.after_start(conn)  # clear what an update left behind
    finally:
        conn.close()
    try:
        with app.single_instance(cfg.home_dir()):
            panel = None if args.no_panel else app.PanelServer(config)
            from clipvault import window

            # The panel opens in ClipVault's own window, which runs as a separate process
            # (window.py): nothing the window does can freeze the clip work in this one.
            # Set up before the panel answers, so an early "open ClipVault" finds it.
            wanted = (cfg.is_packaged() or args.window) and not args.no_window
            windows = None
            if panel is not None and wanted and window.available():
                windows = window.WindowManager(panel.local_url, cfg.home_dir())
                window.set_manager(windows)
            if panel is not None and not panel.start():
                print(
                    f"The control panel could not start on port {config.panel.port} "
                    "(is another program using it?). Change panel.port in config.yaml."
                )
                return 1
            runner = app.App(config)
            runner.start()
            quit_event = threading.Event()
            icon = None
            main_thread_icon = False

            def open_panel() -> bool:
                if windows is not None and windows.open():
                    return True
                webbrowser.open(panel.local_url)
                return True

            def stop_everything() -> None:
                quit_event.set()
                if icon is not None and main_thread_icon:
                    icon.stop()  # ends run_on_main_thread on macOS

            if panel is not None and not args.no_tray:
                from clipvault import tray

                # macOS draws menu-bar icons from the main thread only; the window has
                # a process (and a main thread) of its own, so nothing competes for it.
                main_thread_icon = tray.needs_main_thread()
                icon = tray.start(panel.local_url, cfg.db_path(), stop_everything,
                                  on_main_thread=main_thread_icon, on_open=open_panel)  # fmt: skip

            lifecycle.mark_started()
            lifecycle.stopping.clear()
            lifecycle.on_quit(stop_everything)  # an update or the tray can end the app
            _say("ClipVault is running. Leave this window open; press Ctrl+C to stop.")
            if panel is not None:
                _say(f"  Control panel: {panel.local_url}")
            _say(f"  New clips are looked for every {config.discover_interval_min} minutes.")
            if paused:
                _say("  It is PAUSED: nothing is downloaded until you resume (panel or command).")
            _say(f"  What it does is written to {cfg.logs_dir()}")
            # Never read the keychain on this (main) thread. macOS can make a keychain read
            # wait for the user's permission (it asks when a new version reads what an older
            # one saved), and a waiting main thread can't answer macOS: "ClipVault is not
            # responding" (0.3.3). Reading every key once here also brings any such
            # question up right at start, instead of in the middle of the night.
            threading.Thread(target=_read_keys_once, name="keychain", daemon=True).start()
            try:
                if panel is not None and not args.no_browser:
                    open_panel()
                if icon is not None and main_thread_icon:
                    tray.run_on_main_thread(icon)  # returns after Quit
                else:
                    while not quit_event.wait(1):
                        pass
            except KeyboardInterrupt:
                pass
            _say("Stopping; finishing the step that is running (this can take a minute)...")
            if windows is not None:
                windows.close()
                window.set_manager(None)
            if icon is not None:
                icon.stop()
            runner.stop()
            if panel is not None:
                panel.stop()
            lifecycle.on_quit(None)
    except app.AlreadyRunning as exc:
        if cfg.is_packaged() and not args.no_browser:
            # Opened again (desktop icon, Start menu): bring the running copy's window
            # forward, or open its panel in the browser when it has no window.
            port = cfg.panel_port(config)
            if not _show_running_window(port):
                webbrowser.open(f"http://127.0.0.1:{port}")
            return 0
        print(f"{exc}. Only one copy may run at a time.")
        return 1
    _say("Stopped.")
    return 0


def _read_keys_once() -> None:
    """Read every stored key once, off the main thread (see ``_cmd_run``)."""
    from clipvault.notify import ntfy

    log = logging.getLogger("clipvault.cli")
    for name in cfg.SECRET_NAMES:
        try:
            cfg.get_secret(name)
        except Exception as exc:  # a locked or missing keychain: the steps report it later
            log.warning("could not read %s from the keychain: %s", name, exc,
                        extra=ctx(job="keychain"))  # fmt: skip
            return
    if ntfy.get_topic() is None:
        _say("  Phone notifications are off; set them up in the panel's Settings.")


def _cmd_window(args: argparse.Namespace) -> int:
    """The window process: started by a running ClipVault, which passes its sign-in token."""
    import os
    import webbrowser

    from clipvault import window

    url = (args.url or f"http://127.0.0.1:{cfg.panel_port(cfg.load_config())}").rstrip("/")
    # Read the token and drop it, so the browser engine's own processes don't inherit it.
    token = os.environ.pop(window.TOKEN_ENV, "")
    if not token:
        print("This opens the window of a running ClipVault; start ClipVault with 'run' instead.")
        return 1
    try:
        return window.run_window(url, token, cfg.home_dir() / "webview")
    except Exception as exc:  # no usable window system (e.g. WebView2 missing): the browser
        logging.getLogger("clipvault.cli").warning(
            "app window unavailable, opening the browser: %s", exc, extra=ctx(job="window"),
            exc_info=True)  # fmt: skip
        webbrowser.open(url)
        return 1


def _show_running_window(port: int) -> bool:
    """Ask the ClipVault that is already running to open its window or bring it forward."""
    import json
    import urllib.request

    from clipvault import window

    window.allow_foreground()  # this launch was the user's click: the window may come to front
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/app/show", timeout=5) as reply:
            return bool(json.load(reply).get("shown"))
    except (OSError, ValueError):
        return False


def _cmd_twitch(args: argparse.Namespace) -> int:
    import webbrowser

    import httpx

    from clipvault.twitch import user_auth

    if args.action == "logout":
        user_auth.logout()
        print("Twitch login forgotten. Live chat is no longer read.")
        return 0
    if args.action == "status":
        token = user_auth.stored()
        print(f"Logged in to Twitch as {token.login}." if token else "Not logged in to Twitch.")
        return 0
    with httpx.Client(timeout=15.0) as http:
        try:
            login = user_auth.start_login(http)
        except user_auth.LoginNeeded as exc:
            print(f"Can't start the login: {exc}")
            return 1
        print("To let ClipVault read live chat, log in to Twitch:")
        print(f"  1. Open {login.verification_uri} (opening it in your browser now)")
        print(f"  2. Enter this code: {login.user_code}")
        print("Waiting for you to approve it... (Ctrl+C cancels)")
        webbrowser.open(login.verification_uri)
        try:
            token = user_auth.finish_login(http, login)
        except user_auth.LoginNeeded as exc:
            print(f"Login didn't work: {exc}")
            return 1
        except KeyboardInterrupt:
            print("Cancelled.")
            return 1
    print(f"Logged in as {token.login}. ClipVault reads live chat while it runs.")
    return 0


def _cmd_live(args: argparse.Namespace) -> int:
    import threading

    from clipvault.live import LiveWatcher
    from clipvault.twitch import user_auth

    if user_auth.stored() is None:
        print("Log in to Twitch first: python -m clipvault twitch login")
        return 1
    stop = threading.Event()
    watcher = LiveWatcher(cfg.load_config())
    thread = threading.Thread(target=watcher.run, args=(stop,), daemon=True)
    thread.start()
    _say("Watching live chat; moments are logged and cut a few minutes later by 'fetch' or 'run'.")
    _say("Press Ctrl+C to stop.")
    try:
        while thread.is_alive():
            thread.join(timeout=30)
            live = ", ".join(f"{c.login} ({c.spikes} spikes)" for c in watcher.channels.values())
            _say(f"  live now: {live or 'nobody'}")
    except KeyboardInterrupt:
        pass
    stop.set()
    thread.join(timeout=15)
    return 0


def _cmd_panel(args: argparse.Namespace) -> int:
    from clipvault.web import auth

    for name in ("panel_password_hash", "panel_session_key"):
        cfg.delete_secret(name)
    auth.forget_cache()  # a running app notices within auth.CACHE_S seconds
    print("Panel password forgotten and every device logged out.")
    print("Open the panel on this computer to choose a new one.")
    return 0


def _set_paused(paused: bool) -> int:
    from clipvault.alerts import set_setting

    conn = db.open_db(cfg.db_path())
    try:
        set_setting(conn, "paused", "1" if paused else "0")
    finally:
        conn.close()
    print("Paused: scheduled work waits until you resume." if paused else "Resumed.")
    return 0


def _cmd_notify(args: argparse.Namespace) -> int:
    from clipvault import scheduler
    from clipvault.notify import ntfy
    from clipvault.timeutil import utc_now

    config = cfg.load_config()
    if args.action == "setup":
        topic = ntfy.ensure_topic()
        url = ntfy.subscribe_url(config, topic)
        print("Phone notifications use the free ntfy app.")
        print("  1. Install 'ntfy' from the App Store or Google Play.")
        print("  2. In the app tap +, and subscribe to this topic name:")
        print(f"       {topic}")
        print(f"     (or open {url} on the phone)")
        print("  3. Then run: python -m clipvault notify test")
        print("Keep the topic name private: anyone who knows it can read the notifications.")
        return 0
    conn = db.open_db(cfg.db_path())
    try:
        if args.action == "status":
            if ntfy.get_topic() is None:
                print("Notifications are not set up. Run: python -m clipvault notify setup")
                return 0
            for status, count in conn.execute(
                "SELECT status, count(*) FROM notifications GROUP BY status ORDER BY status"
            ):
                print(f"{status:<8} {count}")
            last = conn.execute(
                "SELECT last_error FROM notifications WHERE last_error IS NOT NULL "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if last:
                print(f"Last problem: {last[0]}")
            return 0
        if ntfy.get_topic() is None:
            print("Notifications are not set up yet. Run first: python -m clipvault notify setup")
            return 1
        now = utc_now()
        summary = "This is what the daily summary looks like:\n" + scheduler.summary_text(conn, now)
        alert = "This is what a problem alert looks like. Nothing is wrong."
        # Test messages ignore quiet hours, so they go out straight away.
        ntfy.queue(conn, config, "test", "ClipVault test summary", summary, priority=2,
                   tags=("clipboard",), now=now)  # fmt: skip
        ntfy.queue(conn, config, "test", "ClipVault test alert", alert, priority=4,
                   tags=("warning",), now=now)  # fmt: skip
        result = ntfy.flush(conn, config, now=now)
    finally:
        conn.close()
    if result.failed:
        print("Could not send; check the internet connection.")
        print("Details: python -m clipvault notify status")
        return 1
    print(f"Sent {result.sent} test notification(s); they should reach the phone within a minute.")
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
    from clipvault import secret_prompt

    if args.window:
        entered = secret_prompt.ask_in_window(args.name)
        if entered is None:
            print("Cancelled; nothing stored.")
            return 1
    else:
        entered = secret_prompt.ask_in_terminal(args.name)
    value = secret_prompt.clean_secret(entered)
    if not value:
        print(
            "That doesn't look like a key (empty, or the paste didn't work); nothing stored. "
            f"Try: python -m clipvault secrets set {args.name} --window"
        )
        return 1
    cfg.set_secret(args.name, value)
    print(f"{args.name} stored in the OS keychain.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
