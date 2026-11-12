# Progress log

Append a dated entry at the end of every session: what changed, what was verified and how, open issues.

## 2026-11-12: M0 Skeleton (done)

**What changed**
- Moved `SPEC.md` and `BUILD_PLAN.md` from the repo root into `docs/` (where CLAUDE.md expects them) and created `docs/PROGRESS.md`. The original `clipvault-brief/` folder is untouched and now redundant.
- `git init` on branch `main`. Nothing committed yet.
- `pyproject.toml`: core dependencies plus `dev` extra (pytest, ruff). Heavy packages are in optional extras to keep the first install quick: `render` (OpenCV), `transcribe` (faster-whisper), `desktop` (pystray). Install them when their milestone arrives.
- Package `clipvault` with the CLAUDE.md layout (sub-packages are empty apart from a docstring).
- `clipvault/config.py`: loads `config.yaml` (all keys optional, defaults from SPEC §17), validates values with plain-language errors, reports unknown keys (typos) without failing. Secrets via `keyring` under service name `clipvault`; `CLIPVAULT_<NAME>` env vars override for local development.
- `clipvault/db.py` + `clipvault/migrations/0001_initial.sql`: WAL mode, foreign keys on, forward-only migrations recorded in `schema_migrations`, each applied in one transaction.
- `clipvault/logging_setup.py`: JSON lines to `logs/clipvault.jsonl`, daily rotation, 14 days kept; every line has `clip`, `streamer`, `job`.
- `clipvault/doctor.py` and the CLI (`python -m clipvault doctor`). Other commands (`run`, `discover`, `fetch`, `render`, `publish`) exist as stubs that say which milestone they arrive in (exit code 2).
- `.gitignore`, `README.md`, `config.example.yaml`.

**Decisions worth knowing**
- App home folder (holds `config.yaml`, `clipvault.db`, `logs/`, optional `bin/`) = current directory, or `CLIPVAULT_HOME` if set.
- Schema additions beyond SPEC §2: a CHECK on `streamers` so `post_enabled = 1` is impossible without `permission_type` campaign/written and a non-empty `permission_ref` (hard rule 1, second line of defense; the publish-path gate and its test still come in M9); `UNIQUE(render_id, platform, account)` on `posts` to make duplicate posts impossible; CHECKs on `clips.source`, `clips.downloaded_via`, `posts.status`; a few indexes. `clips.state` has no CHECK (states live in `clipvault/jobs.py`).
- `doctor` treats a missing TwitchDownloaderCLI and a missing `config.yaml` as warnings, not failures (the first is only needed from M4, the second is optional). It also checks the database, which creates `clipvault.db` if absent.
- `tzdata` is a dependency because Windows has no system time-zone database.

**Verified**
- `pytest -q`: 43 passed. `ruff check .` and `ruff format --check .`: clean.
- `python -m clipvault doctor` on this Windows 11 laptop: Python 3.12.10, FFmpeg 9.0.1, yt-dlp 2026.10.26, keyring (WinVaultKeyring), disk and database OK; warnings for TwitchDownloaderCLI (not installed) and config.yaml (not created). Exit code 0.
- Fresh venv + `pip install -e ".[dev]"` took 104 seconds here, so the README quick start fits in 5 minutes when Python and FFmpeg are already installed.

**Open issues**
- Not tested on macOS or Linux, and not from an actual fresh `git clone` (no commit exists yet).
- TwitchDownloaderCLI is not installed on this machine; needed by M4.
- For M1 the user needs a Twitch developer application (client ID and secret, redirect URL `http://localhost:8765/auth/twitch/callback`).

## 2026-11-12 (second session): M0 hardening, M1 built but not yet verified live

**Status of M1:** all code and tests are in place. The milestone is NOT done: its
"Done when" check (discover for 3 real streamers, then a second run adds nothing)
and the Auto Clips question both need real Twitch keys, which are not stored yet.
The Auto Clips box in BUILD_PLAN.md is left unticked.

**To finish M1 (user steps)**
1. Create a Twitch application at dev.twitch.tv/console (redirect URL `http://localhost:8765/auth/twitch/callback`).
2. `python -m clipvault secrets set twitch_client_id` and `... twitch_client_secret`.
3. Put 3 logins in `streamers.txt`, then `python -m clipvault streamers import`.
4. `python -m clipvault discover` twice; the second run must report 0 new clips.
5. `python scripts/twitch_live_check.py LOGIN --dump tests/fixtures/twitch/recorded` for a streamer known to use Auto Clips; record below whether they appear and how to tell them apart.

**Docs check (hard rule 10)**
Read on 2026-11-12: Get Clips, Get Users, Get Games, client credentials flow, API guide (rate limits, pagination), validate tokens. Everything matches SPEC.md. Details worth keeping:
- Get Clips has no documented field that marks an Auto Clip, so every clip is stored with `source = 'viewer'` for now.
- `vod_offset` is null for some minutes after a clip is created, and `video_id` is an empty string when the VOD is gone. Discovery stores NULL for both and fills them in on a later run.
- The rate-limit bucket size is not documented (the "800" in the docs is only an example), so the client relies on the `Ratelimit-Reset` header and never on a fixed number.
- Twitch asks apps that keep an OAuth session to call `/oauth2/validate` at start and hourly. That is aimed at user tokens; it is not built yet and belongs in the `token_refresh` job (M3 for the app token check, M9 for user tokens).
- The bad-client-secret error is not documented; any 4xx from the token endpoint except 429 is treated as "keys rejected".

**What changed: M1**
- `clipvault/twitch/auth.py`: app access token via client credentials, kept in the keychain (`twitch_app_token`), renewed 10 minutes before expiry or when Twitch answers 401. The secret goes in the form body, never the URL.
- `clipvault/twitch/api.py`: Helix client for users, games and clips. Retries network and 5xx errors with backoff, waits out 429 using `Ratelimit-Reset` (capped at 60 s), renews the token once on 401, follows pagination and drops repeated ids.
- `clipvault/streamers.py`: `streamers.txt` parser and import (adds and updates, never removes, cannot switch posting on); Twitch profile cache refreshed weekly by user id, which also follows login renames.
- `clipvault/discover/viewer_clips.py`: the discover job. `clipvault/runs.py`: cursor bookkeeping.
- CLI: `discover [--streamer] [--since]`, `streamers import|list`, `secrets set|list|delete`. `doctor` gained a "Twitch keys" check and prints the home folder.
- `scripts/twitch_live_check.py`: manual live check; `--dump` saves raw responses for fixtures.
- Migration `0002_discovery`: `clips.game_id`, `streamers.profile_checked_at`, `games` cache table, trigger that keeps `clips.updated_at` current.

**How discovery picks clips (decision to confirm)**
- Each run re-reads whole local days (config time zone) from the day before the streamer's cursor up to today, one request per streamer per day. Twitch sorts by views, so one page holds the day's top 100.
- Default mode keeps each day's `top_n` most-viewed clips. If `min_views` is set for a streamer it replaces `top_n`: every clip at or above it is kept.
- Cursors are per streamer (`runs.job = 'discover:<streamer id>'`), so a streamer added later gets their own 7-day first run and one streamer failing does not hold back the others.
- Known side effect: during a day the top-N changes as views grow, so over a day a streamer can collect more than `top_n` clips (each was in the top N at some run). An immediate second run still adds nothing. Measure this in the live check; if it is too many, options are a minimum clip age before picking, or only finalising a day after it ends.

**What changed: hardening of M0 (predicted problems, fixed before they bite)**
- Database connections are now autocommit with an explicit `db.transaction()` (BEGIN IMMEDIATE). Reason: the scheduler, web panel and CLI will write from different threads and processes; implicit transactions cause "database is locked" upgrades and half-written state. Rule written at the top of `db.py`: one connection per thread, never hold a transaction across a network call or FFmpeg run.
- Logging writes straight to `logs/clipvault-YYYY-MM-DD.jsonl` instead of renaming files at midnight. Reason: rename-based rotation fails on Windows when a second process (a CLI command while the app runs) has the file open. Old log files are no longer deleted automatically, because hard rule 8 says the app never deletes outside the library folder; they are small. Say if you want pruning (it needs a wording change to that rule).
- httpx/httpcore/apscheduler loggers are set to WARNING so request URLs don't flood the logs.
- `clipvault/timeutil.py`: one stored timestamp format (UTC, `YYYY-MM-DDTHH:MM:SSZ`) so timestamps compare correctly as text in SQLite; naive datetimes are refused.
- Tests block all non-loopback network connections (hard rule 9 is now enforced, not just a convention), and always use an in-memory keychain and a temp home folder.
- `.gitattributes` forces LF line endings, so files (and later the Docker entrypoint) behave the same on Windows and Linux.
- The CLI never crashes on a clip title the Windows console can't display, and turns unexpected errors into a one-line message plus a logged traceback.
- `streamers.txt` is read as `utf-8-sig`, so a file saved by Notepad works.

**Verified**
- `pytest -q`: 123 passed. `ruff check .` and `ruff format --check .`: clean.
- The existing M0 database upgraded in place from migration 0001 to 0002 (`doctor` reports 2 migrations).
- Real CLI smoke test in a scratch home folder: `streamers import`, `streamers list`, `secrets list`, and `discover` / the live-check script without keys (both explain how to set them, exit code 1).
- NOT verified: anything against the real Twitch API. The fixtures in `tests/fixtures/twitch/` follow the documented shapes but hold made-up values.

**Open issues and things to watch in later milestones**
- M1 live check and the Auto Clips answer (steps above). Replace the made-up fixtures with recorded ones afterwards.
- The top-N growth side effect described above.
- M2: clip titles need sanitising for Windows file names (reserved characters, trailing dots, 260-character paths); the library root may be a missing external drive.
- M3: the scheduler must open its own database connection per job thread; add the hourly token validation; only one `run` process should be active at a time (add a lock file or port check).
- M13: `keyring` has no backend in a plain Docker container, and PyInstaller must be told to bundle `clipvault/migrations/*.sql`. Both need a decision before packaging.
- The app home folder is the current directory unless `CLIPVAULT_HOME` is set, so starting the app from another folder would create a second, empty database. `doctor` now prints the folder; the installers (M13) should set it explicitly.
- `clipvault-brief/` is still in the repo as a redundant copy of the docs.
