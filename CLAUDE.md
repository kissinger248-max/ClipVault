# ClipVault (working name)

A personal program that watches a list of Twitch streamers, finds their best moments, saves them to a clip library, renders vertical 9:16 versions with a hook headline and word-by-word captions, and, only for streamers who gave permission, posts them to TikTok and YouTube Shorts. It runs unattended: 24/7 on a small server, or on a laptop that catches up after sleep. A built-in web control panel is the only UI.

## Read these first, every session

1. `docs/SPEC.md`: what to build, with exact numbers, API facts and data model.
2. `docs/BUILD_PLAN.md`: the build order. Work on **one milestone at a time**, the first one with unchecked boxes.
3. `docs/PROGRESS.md`: notes from earlier sessions (create it if missing; append to it at the end of every session).

## Hard rules (never break these)

1. **Posting is gated per streamer.** A post can only be created when the streamer has `post_enabled = true` AND `permission_type` is `campaign` or `written` with a non-empty `permission_ref`. Enforce this in the publish code path itself, not only in the UI. Add a test for it.
2. **Post only through the posting-service adapter** (Upload-Post first, Blotato as an alternative). Never use browser automation, scraped endpoints, or unofficial TikTok or YouTube upload libraries.
3. **Twitch discovery uses the official Helix API.** Official downloads (Get Clips Download) are used for streamers who authorized the app. Unofficial downloaders (yt-dlp, TwitchDownloaderCLI) are allowed only for the private archive, and files obtained that way are never posted unless the streamer is permissioned.
4. **Shorts are 15–59 seconds.** YouTube blocks Shorts over 1 minute that carry any Content ID claim.
5. **All burned-in text stays inside the safe box** x 150–930, y 270–1340 on the 1080×1920 frame. QA must check this.
6. **No secrets in code or git.** Store tokens and API keys with the `keyring` library (OS keychain). A `.env` file is allowed for local development only and must be in `.gitignore`.
7. **Every job is idempotent and resumable.** State lives in SQLite; a crash at any point resumes without duplicate downloads or duplicate posts.
8. **Never delete anything outside the library folder,** and retention only ever deletes files the app itself created (tracked in the database).
9. **No live API calls in unit tests.** Use recorded fixtures under `tests/fixtures/`. Live checks go in clearly named manual scripts under `scripts/`.
10. **Before writing an integration, check that service's current docs** (links in SPEC.md). These APIs change; if the docs disagree with SPEC.md, follow the docs and note it in PROGRESS.md.
11. **Ask the user before** adding any paid service, changing a hard rule, or doing anything that posts publicly for the first time.

## Stack

- Python 3.12, packaged with `pyproject.toml`
- Web: FastAPI + Jinja2 templates + HTMX (no separate front-end build)
- Database: SQLite in WAL mode, plain `sqlite3` with a small migrations folder
- Scheduling: in-process scheduler (APScheduler) with catch-up cursors stored in the `runs` table
- HTTP: `httpx` with `tenacity` retries
- Video: FFmpeg via subprocess; captions and headline burned in with the `subtitles` filter (libass, ASS files)
- Downloads: `yt-dlp` (Python module), TwitchDownloaderCLI binary for VOD chat replays
- Transcription: Groq `whisper-large-v3-turbo` API, fallback `faster-whisper` (small, int8) locally
- Face detection: OpenCV with the YuNet model
- Music detection: a small audio tagger (YAMNet or PANNs)
- Headline writer: provider-agnostic LLM interface (default: Groq-hosted open model)
- Notifications: ntfy (HTTP POST to a topic)
- Desktop: `pystray` tray icon; PyInstaller for installers; Docker image for servers
- Tests and lint: `pytest`, `ruff`

## Project layout

```
clipvault/
  __main__.py        # CLI entry: run, doctor, discover, fetch, render, publish --dry-run
  app.py             # starts web server + scheduler (+ tray on desktop)
  config.py          # settings loader (config.yaml + keyring secrets)
  db.py              # connection, migrations
  migrations/
  twitch/            # api.py, auth.py, chat_replay.py
  discover/          # viewer_clips.py, vod_spikes.py, live_spikes.py
  fetch/             # official.py, ytdlp.py
  process/           # filter.py, music.py, transcribe.py, write.py
  render/            # layouts.py, facecam.py, ass.py, ffmpeg.py, qa.py, longform.py
  publish/           # base.py, uploadpost.py, blotato.py, queue.py, campaigns.py, stats.py
  library/           # storage.py, retention.py
  notify/            # ntfy.py
  web/               # routes/, templates/, static/
  jobs.py            # job definitions and state transitions
  alerts.py          # alerts table + settings table helpers
  runs.py            # job cursors and last-run bookkeeping (runs table)
  secret_prompt.py   # asking for a key (hidden terminal prompt or pop-up window)
  streamers.py       # streamer list: streamers.txt import, Twitch profile cache
  doctor.py          # the doctor checks
  logging_setup.py   # JSON-lines logging
  timeutil.py        # UTC timestamps (one stored format), durations
tests/
  fixtures/
scripts/             # manual live checks
docs/
  SPEC.md
  BUILD_PLAN.md
  PROGRESS.md
assets/fonts/        # Montserrat (OFL) and any other bundled fonts
```

## Commands

```
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
python -m clipvault doctor        # checks ffmpeg, yt-dlp, TwitchDownloaderCLI, config, keyring
python -m clipvault run           # starts everything
python -m clipvault secrets set NAME [--window]   # store a key in the OS keychain; --window asks in a pop-up (also: secrets list / delete NAME)
python -m clipvault streamers import [FILE]   # add streamers from streamers.txt (also: streamers list)
python -m clipvault discover [--streamer LOGIN] [--since 7d] [--all]   # find new clips
python -m clipvault fetch [--streamer LOGIN] [--limit N]   # download found clips into the library
python -m clipvault cleanup [--dry-run]       # retention, disk check, files deleted by hand
python scripts/twitch_live_check.py LOGIN...  # manual live check of the Twitch API (needs real keys)
pytest -q
ruff check . && ruff format --check .
```

Keep these commands working. If you add one, document it here.

## How to work

- Start a session by reading the three docs above, then say which milestone you're on.
- Build the smallest working slice first, then extend it.
- A milestone is done only when its "Done when" checks in BUILD_PLAN.md pass, `pytest` passes and `ruff` is clean. Tick its boxes and append a dated entry to `docs/PROGRESS.md`: what changed, what was verified and how, open issues.
- Test with 2–3 real streamers before adding more.
- Prefer clear, boring code. Small modules, type hints, docstrings on public functions.
- Log as JSON lines to `logs/`. Every error log says which clip, streamer and job it concerns.
