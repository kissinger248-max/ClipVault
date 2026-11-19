"""Load test for ClipVault Web (docs/WEB_VERSION_PLAN.md, W4): many runs at once.

    python scripts/web_load_test.py [--runs 50] [--workers 2] [--real VIDEO]

Starts the website on this computer (a temporary data folder, a free port) and sends
``--runs`` uploads at the same time through HTTP, then follows every run to the end.

- Default: FFmpeg and the speech model are stand-ins (each phone version "takes" a moment),
  so this tests the website itself: uploads, the queue, the workers, the database, the
  pages answering while everything runs.
- ``--real VIDEO``: the real pipeline on a short video (use a few runs: each takes minutes).

Limits are raised for the test (one visitor sends everything). Nothing leaves this computer.
Exit code 0 only when every upload was accepted, every run finished and no page failed.
"""

from __future__ import annotations

import argparse
import shutil
import socket
import statistics
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clipvault.render import engine, ffmpeg, qa  # noqa: E402
from clipvault.render.ffmpeg import MediaInfo  # noqa: E402
from clipvault_web import making  # noqa: E402
from clipvault_web import moments as finder  # noqa: E402
from clipvault_web.server import create_app  # noqa: E402
from clipvault_web.settings import WebSettings  # noqa: E402


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def stand_ins() -> None:
    """FFmpeg and speech replaced by quick fakes that still write real files."""
    ffmpeg.probe = lambda path: MediaInfo(1280, 720, 30.0, 600.0, True, "h264")
    peak = finder.Peak(100.0, 9.0, -18.0)

    def find(video, duration, count, providers, progress):
        progress("Reading what is said", 1, 2)
        time.sleep(0.5)
        return [finder.Moment(40.0 * n, 40.0 * n + 30, 90 - n, peak, "no way!", (), "en", [])
                for n in range(count)], 4  # fmt: skip

    def make_vertical(**kw):
        time.sleep(0.4)
        kw["dest"].write_bytes(b"phone version")
        report = qa.Report()
        report.add("duration", True, "30 s")
        return report

    def extract_frame(path, second, dest):
        dest.write_bytes(b"jpeg")
        return dest

    finder.find_moments = find
    engine.make_vertical = make_vertical
    ffmpeg.extract_frame = extract_frame
    making.find_cam = lambda source, info, look: None
    original = making.make_run
    making.make_run = lambda settings, run_id, **kw: original(
        settings, run_id, transcribers=[], find=find, **kw)  # fmt: skip


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runs", type=int, default=50)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--real", type=Path, help="a short real video (the real pipeline)")
    parser.add_argument("--timeout", type=float, default=900)
    args = parser.parse_args()

    if args.real is None:
        stand_ins()
    body = args.real.read_bytes() if args.real else b"not really a video" * 1000
    data = Path(tempfile.mkdtemp(prefix="clipvault-web-load-"))
    settings = WebSettings(data=data, workers=args.workers, runs_per_day=10_000,
                           max_queue=10_000, one_run_at_a_time=False)  # fmt: skip
    app = create_app(settings)
    if args.real is None:
        app.state.worker._make = making.make_run  # the stand-in version
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if httpx.get(f"{base}/healthz", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.1)

    errors: list[str] = []
    latencies: list[float] = []

    def send(i: int) -> str | None:
        started = time.monotonic()
        try:
            headers = {"X-File-Name": f"load{i}.mp4", "X-Rights": "yes"}
            reply = httpx.post(f"{base}/api/upload", content=body, timeout=120, headers=headers)
        except httpx.HTTPError as exc:
            errors.append(f"upload {i}: {exc}")
            return None
        latencies.append(time.monotonic() - started)
        if reply.status_code != 200:
            errors.append(f"upload {i}: HTTP {reply.status_code} {reply.text[:120]}")
            return None
        return reply.json()["id"]

    began = time.monotonic()
    with ThreadPoolExecutor(max_workers=min(args.runs, 32)) as pool:
        run_ids = [r for r in pool.map(send, range(args.runs)) if r]
    print(f"{len(run_ids)}/{args.runs} uploads accepted in {time.monotonic() - began:.1f} s "
          f"(median {statistics.median(latencies or [0]):.2f} s, "
          f"slowest {max(latencies or [0]):.2f} s)")  # fmt: skip

    finished: dict[str, str] = {}
    pages = 0
    deadline = time.monotonic() + args.timeout
    with httpx.Client(base_url=base, timeout=30) as client:
        while len(finished) < len(run_ids) and time.monotonic() < deadline:
            for run_id in run_ids:
                if run_id in finished:
                    continue
                reply = client.get(f"/api/runs/{run_id}")
                pages += 1
                if reply.status_code != 200:
                    errors.append(f"status {run_id}: HTTP {reply.status_code}")
                    finished[run_id] = "error"
                elif reply.json()["status"] in ("done", "failed"):
                    finished[run_id] = reply.json()["status"]
            if client.get("/").status_code != 200:  # the home page keeps answering
                errors.append("home page failed while busy")
            time.sleep(1)
    total = time.monotonic() - began
    done = sum(1 for s in finished.values() if s == "done")
    failed = [r for r, s in finished.items() if s != "done"]
    unfinished = len(run_ids) - len(finished)
    print(f"{done} runs done, {len(failed)} failed, {unfinished} unfinished, "
          f"{pages} status pages served, {total:.0f} s in all "
          f"({args.workers} worker(s))")  # fmt: skip
    for error in errors[:20]:
        print("  error:", error)
    server.should_exit = True
    app.state.worker.stop()
    shutil.rmtree(data, ignore_errors=True)
    ok = not errors and not failed and not unfinished and len(run_ids) == args.runs
    print("PASSED" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
