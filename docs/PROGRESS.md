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
