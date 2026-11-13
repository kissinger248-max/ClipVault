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

**Twitch application registered (2026-11-12)**
- Name `Auto_Clipper`, category Application Integration, client type Confidential (a client secret is required for the client credentials flow).
- Registered redirect URL: `https://localhost:8765/auth/twitch/callback` (HTTPS, not the `http://` URL written in BUILD_PLAN.md, README.md and the `doctor` text).
- Why: the console kept showing "Redirect URIs must use HTTPS protocol". The real cause turned out to be a blank second URL row on the form, not the URL itself, so it is still unknown whether the console accepts `http://localhost:8765/auth/twitch/callback`.
- Nothing built so far uses the redirect URL. To settle before M9 (authorize-with-Twitch): try adding the `http://localhost...` URL to the application (with no blank row left on the form). If accepted, use it and keep the docs as they are. If refused, either serve the local callback over HTTPS or use a Twitch flow that needs no redirect, and update the URL in the three places above.

## 2026-11-12 (third session): M1 verified against the real Twitch API

**M1 "Done when": passed.** With real keys stored and `valkyrae`, `xqc`, `kaicenat` on the list:
- First `discover`: 120 new clips (40 per streamer = 5 per local day over 8 days), 0 failures, about 25 requests.
- Second `discover` straight after: 0 new clips for all three. Exit code 0 both times.
- Database afterwards: 120 clips in state `found`, all three streamers have Twitch id, display name and avatar, 11 game names cached, every clip has a game name, one cursor per streamer.
- Durations in the sample range from 6.2 s to 60.0 s, so the 15-59 s posting rule will trim or reject some.
- The log file for the day contains no token or secret.

**Auto Clips question (SPEC 18): not settled, box left unticked.**
- The live responses have exactly the 17 documented fields on every clip (2,255 clips across the three streamers), so nothing marks an Auto Clip.
- No clip was created by the broadcaster's own account or by an obviously Twitch-owned account; every `creator_name` looks like a viewer. `is_featured` was false on all of them.
- So either these three don't use Auto Clips, or Auto Clips aren't returned by Get Clips; this sample can't tell which. To settle it, run `python scripts/twitch_live_check.py LOGIN` for a streamer known to have Auto Clips on and look at the creator names.
- This does not block M2: whatever the answer, such clips can only be stored as `source = 'viewer'`, because the API gives no way to tell them apart.

**What real data showed that the docs didn't**
- Clip URLs come back as `https://www.twitch.tv/<login>/clip/<id>`, not `clips.twitch.tv/<id>`. Nothing depends on the form, but M2 should pass the URL to yt-dlp as given.
- Clip ids always have a suffix after a dash (`Slug-XXXXXXXXXXXXXXXX`), up to about 60 characters. File names in M2 must allow for that length.
- Many clips keep the stream title as their title (115 of xQc's 711 share one), and titles are full of emoji and symbols. The M2 file-name slug and the M8 headline writer can't rely on the title being meaningful.
- Big channels get 100+ clips a day (KaiCenat: 880 in 7 days), so "one page of 100 per day" is what keeps discovery cheap; the top of the page is what matters.
- A few clips (9 of 2,255) already had no VOD (`video_id` empty, `vod_offset` null) within a week.

**What changed**
- Test fixtures in `tests/fixtures/twitch/` are now real recorded responses (clip creators' handles replaced; the token fixture stays made up). See the README there.
- `secrets set NAME --window`: asks in a small pop-up window with a masked box, because pasting into the hidden terminal prompt was confusing. A value that is empty or contains control characters or spaces is refused instead of stored.
- `discover` prints the top 5 new clips per streamer and a count of the rest; `--all` lists everything.
- `streamers.txt` created locally with the three test streamers (git-ignored).

**Verified**
- `pytest -q`: 133 passed. `ruff check .` and `ruff format --check .`: clean.
- The pop-up window was opened for real and driven automatically (paste, save, cancel) before the user used it.

**Open issues**
- Auto Clips question, as above.
- Top-N growth over a day has not been measured yet; compare clip counts per streamer per day after discovery has run for a day or two (expected 5, more means growth).
- Redirect URL question for M9 (see the note above about the registered application).

## 2026-11-12 (fourth session): M2 downloads and the library folder (done)

**M2 "Done when": all four checks passed on real data.**
- First run for `valkyrae`, `xqc`, `kaicenat`: 118 clips saved to `C:\Users\oncho\Twitch Archive` (733 MB), 0 failures, about 5 s per clip. 2 further clips were rejected as repeats of a more-viewed clip of the same moment (checked by hand: 10 s and 4 s offsets on the same VOD).
- Second run straight after: 0 downloaded.
- One file taken out of the library by hand: `cleanup` marked it, `fetch` did not download it again, nothing crashed. After the file was put back, `cleanup` un-marked it.
- Disk guard with `min_free_space_pct: 90` (the disk is 64% free): `fetch` stopped before downloading, exit code 1, storage alert opened once; `cleanup` reported "TOO FULL". With the normal setting the two waiting clips downloaded and the alert closed.

**Measured file sizes (replaces the estimates in SPEC 13)**
- 720p: **5.3 MB per 30 s** on average (118 clips, 4,118 s of video). Per streamer: valkyrae 4.0, kaicenat 5.3, xqc 7.4. Smallest file 0.4 MB, largest 21.1 MB.
- 1080p: **9.6 MB per 30 s** on average (9-clip sample, 401 s), ranging from 2.2 to 17.5.
- SPEC guessed 10 MB (720p) and 25 MB (1080p) per 30 s clip, so real files are about half that. The reason is below.
- At the default 5 clips a day per streamer (average clip 35 s): roughly 30 MB per streamer per day at 720p, about 1 GB per streamer per month.

**What real downloads showed**
- Twitch serves clips as **HEVC (H.265)** at 60 fps, not H.264. FFmpeg reads it fine, so rendering (M7) is unaffected, but browser playback is not guaranteed: Chrome and Edge need hardware support and Firefox often can't play it. The control panel (M5) should test playback early and may need an H.264 preview copy.
- yt-dlp also lists `portrait-360/480/608` versions for some clips (seen on valkyrae): Twitch's own vertical cut. Only landscape versions are downloaded for now; the portrait ones are a ready-made input for the "native" layout in M7 (SPEC 10.2), though at 608 px wide they would need upscaling.
- Some clips come in 1440p as well; the quality setting caps at 720p or 1080p as configured.
- yt-dlp reports "This clip is no longer available" for deleted clips; those are rejected at once (`reject_reason = 'unavailable'`) without retries.

**What changed**
- `clipvault/fetch/ytdlp.py`: downloads by running `python -m yt_dlp` as a separate process (not imported), so the daily update takes effect without a restart and a hung download is killed after 5 minutes. Daily `pip install --upgrade yt-dlp`, at most one attempt per 24 h, tracked in `runs`.
- `clipvault/library/storage.py`: SPEC 13 naming, sidecar JSON, title slugs, path-length guard, library marker file, local cache for when the library drive is away.
- `clipvault/library/retention.py`: disk guard, retention plan and apply, detection of hand-deleted files.
- `clipvault/process/filter.py`: overlap dedupe as a pure function.
- `clipvault/jobs.py`: `run_fetch` (found -> fetched) and `record_failure` (retries, `dead`).
- `clipvault/alerts.py`: alerts table helpers (one open alert per kind and message) and settings table helpers.
- CLI: `fetch [--streamer] [--limit]`, `cleanup [--dry-run]`.
- Migration `0003_library`: `retry_at`, `fetched_at`, `file_size`, `removed_at`, `removed_reason`, an index for retries, and a unique index for open alerts.

**Decisions worth knowing**
- **Paths in the database are relative to the library root.** The library can move to another drive or sync to another computer (M13) without touching the database.
- **A removed file does not change the clip's state.** `removed_at` / `removed_reason` (`retention`, `missing`, `user`) record it instead, so a posted clip stays `posted` after its source is cleaned up. Every later worker step must skip clips where `removed_at` is set.
- **Retry policy:** a failed step is retried after 1, 5 and 30 minutes and the clip is `dead` when the third retry fails (four attempts). SPEC 3 says both "retries after 1, 5 and 30 minutes" and "after 3 failures the clip is dead"; using all three delays needs four attempts, so that reading was chosen. This was scheduled for M3 but the download step needed it.
- **Dedupe never deletes a saved clip.** If a more-viewed clip of the same moment arrives after one is already downloaded, the newcomer is rejected. Overlap is measured against the shorter clip; over 50% counts as the same moment.
- **Retention median covers every clip ever saved for the streamer,** including ones already removed. Otherwise each run would remove the lower half of what was left and the archive would shrink to nothing over time.
- **The file name is fixed before the download starts** (stored in `file_path` while the clip is still `found`), so a crash mid-download resumes into the same file even if the view count changed.
- **Library away:** the root counts as present only when its `.clipvault-library` marker file exists. A root set up earlier whose marker is gone is treated as an unplugged drive: downloads go to `library-cache/` and move over later; nothing is marked missing meanwhile.
- **Mass disappearance guard:** if 10 or more files and at least 20% of the library vanish at once, nothing is marked; an alert asks whether the folder was moved.
- After 3 download failures in a row the run stops (so a dead connection doesn't burn every clip's retries) and one "downloader may need an update" alert opens; the next success closes it.

**Verified**
- `pytest -q`: 189 passed. `ruff check .` and `ruff format --check .`: clean.
- Tests never start a real process or touch the real library: yt-dlp/pip calls are blocked and `~` points at a temp folder.
- The live checks above; the database upgraded in place to migration 0003.

**Open issues and things to watch**
- **Top-N growth is real:** about 4 hours after the first run, two streamers already had a day with 6 clips instead of 5. Left as is for now; measure again after a few days of scheduled runs (M3) and add a minimum clip age if it gets out of hand.
- Sidecar JSON files are a snapshot from download time; later view-count changes are in the database only.
- Unfinished `.part` files from failed downloads are left in place (yt-dlp resumes them); nothing cleans up ones that are never resumed.
- The free-space percentage is of the whole drive, not of a quota. Fine for a laptop; revisit for a shared server.
- M5: HEVC playback in browsers (see above). M7: decide whether to use Twitch's portrait versions.
- M13: the yt-dlp self-update uses pip, which won't exist inside a PyInstaller build; the installer will need to bundle the yt-dlp binary and use its own `-U`.
- The user asked that the docs cover editing, subtitles and phone-optimised formatting. They do: SPEC sections 8 (transcription), 9 (headline and caption), 10 (1080x1920 rendering, layouts, safe box, text styles, export) and 11 (automatic QA), built in M6 to M8. A plain-language roadmap was added to README.md so this is visible without reading the spec.

**User request (2026-11-12): moments from chat activity in LIVE streams are wanted, not optional**
- The plan has two versions of "find moments nobody clipped": M4 reads the chat replay of a finished stream (any streamer, no extra login, results some hours later), and M14 does it while the stream is live. M14 was marked optional and limited to permissioned streamers; the user wants the live version.
- BUILD_PLAN.md M14 is no longer marked optional and gained a box for archive-only streamers: listen to chat live, and when a spike is detected cut that stretch from the stream's in-progress recording with `yt-dlp --download-sections` a few minutes later (private archive only, hard rule 3). Create Clip From VOD stays for permissioned streamers only.
- M14 depends on M4 (the spike detector is shared) and on M3 (the app must be running continuously while a streamer is live), so it cannot come before those.
- To check against Twitch's docs before building (hard rule 10): which token and scope let the app read another channel's chat over EventSub (expected: the user's own Twitch login with `user:read:chat`), connection and subscription limits per account, and whether a live stream's recording can be cut while the stream is still running.
- Streamers who disable or subscriber-lock their recordings yield no footage this way; the spike is still detected and can be logged.

## 2026-11-13: M6 (transcription and search) and M7 (render engine and QA)

Built ahead of M3 to M5 at the user's request ("Phone-ready video"). Both milestones are complete except the parts that need the control panel (M5) or the user's own eyes; those boxes stay open.

**What changed**
- `clipvault/process/transcribe.py`: audio extraction (16 kHz mono FLAC), Groq `whisper-large-v3-turbo` with word timestamps, faster-whisper (small, int8) on this computer as the fallback, automatic switch when Groq fails, slur masking from `clipvault/data/mask_words.txt` plus an optional `mask_words.txt` in the ClipVault folder, `.srt` and `.words.json` next to each clip. Migration `0004_transcripts`: clip language, transcript provider, and an FTS5 `transcripts` table.
- `clipvault/library/search.py` and `python -m clipvault search WORDS...`: exact phrase first, then all words (the last one as a prefix).
- `clipvault/render/`: `template.py` (look YAML + real font metrics), `ass.py` (headline, credit, disclosure, karaoke captions, all positioned by our code), `layouts.py` (letterbox, split, native), `facecam.py` (YuNet), `ffmpeg.py` (two-pass loudness, H.264 export), `qa.py` (SPEC §11 checks and the preview still), `engine.py` (render one clip end to end). `python -m clipvault render`.
- `clipvault/process/filter.py` + `candidates.py`: posting-candidate rules and the SPEC §7 score. `render` with no clip id now renders the best-scoring clips, taking turns between streamers.
- Bundled fonts (Montserrat Black/ExtraBold/SemiBold, Anton, SIL OFL licences included) and the YuNet face model (`clipvault/data/`).

**Decisions worth knowing**
- **Transcription is local and free for now.** Groq is built and tested against a recorded response, but there is no Groq key (paid services need the user's OK), so `provider: auto` uses faster-whisper. About 17 s per clip on this laptop. With a key, Groq becomes the first choice with no code change.
- **faster-whisper is fed raw samples decoded by FFmpeg**, not a file: its PyAV 19 dependency fails on file input ("unexpected keyword argument 'metadata_errors'").
- **libass sizes fonts differently from "px".** Font size in the ASS file = wanted px x (winAscent + winDescent) / unitsPerEm (1.562 for Montserrat). Without this, text came out about 35% too small and every safe-box measurement was wrong.
- **Clips over 59 s keep their last 59 s** (the payoff is usually at the end); word timings are shifted to match.
- **Caption blocks never flash by:** each block gets at least 0.4 s; in very fast speech neighbouring blocks are joined (up to 5 words) and, if still needed, later blocks wait a moment. Only a single word too wide for the line makes its block smaller (72 px down to `min_size` 56).
- **Stand-in headlines** (from the clip title, until M8 writes real ones) lose words from the end until they fit 2 lines at 64 px or more.
- **Split layout needs a cam at least 320 px wide in the source.** Faces are detected once per streamer over several clips and stored in `streamers.cam_rect`; each clip gets a 3-frame check; 3 misses in a row trigger a fresh detection.
- **Twitch's own portrait versions** ("Dual Format") are used for the native layout when they exist (`download_portrait`).
- **Music tagger moved to M9.** Only posting needs it, and it needs a model file not yet chosen. Until then a clip with no music score counts as a candidate but is flagged "listen first" (`Verdict.music_unchecked`); M9 must not auto-post such clips.

**Verified**
- All 120 saved clips transcribed locally: 117 with speech, 3 without (marked "none"), 6,503 words, average 1.5 words per second, 119 English and 1 Portuguese.
- Search returns real clips from all 3 streamers with the matching words highlighted (spot-checked with a few words; a title that is not spoken, like "nose hairs", correctly finds nothing).
- 15 vertical renders across all 3 streamers (kaicenat 6, valkyrae 4, xqc 5), including a forced split layout: all pass QA. Render time 14 to 35 s each; average file 36 MB (10 Mbps target), about 7 times the size of the source clip.
- `pytest -q`: 243 passed. `ruff check .` and `ruff format --check .`: clean.

**Not done yet (needs the user or a later milestone)**
- M6 "Done when": the user searching for a phrase they remember; the network-cable fallback test only means something once there is a Groq key.
- M7 "Done when": the user watching renders on a real phone and confirming no text sits under the TikTok or Shorts buttons. The preview stills in `qa/` show the button zones.
- Search in the Clips screen and the "make vertical copy" button: with M5.

**Open issues and things to watch**
- In the split layout the facecam crop for xqc includes part of a chat overlay under the camera; the crop could be tighter. valkyrae's stored cam rectangle has never been confirmed by a clip check.
- Vertical copies are big (about 36 MB each). Retention (M2) only counts source clips; renders need to be covered once rendering runs on a schedule (M9 at the latest).
- The 3 rejected clips are duplicates from M2's overlap check, as expected.

## 2026-11-13: M3 (scheduler, catch-up and phone notifications)

**What changed**
- `clipvault/scheduler.py`: the scheduled steps (discover, worker = download then transcribe, cleanup, summary, notify) and `run_step`, which gives each step its own database connection, honours the pause switch, keeps a `step:<name>` row in `runs`, and raises one "broken" alert after 3 failures in a row (cleared by the next success).
- `clipvault/app.py`: APScheduler with discover every `discover_interval_min` (and right at start), worker every 2 min, notify every minute, and a 1-minute heartbeat that runs the daily steps when due (cleanup 04:00, summary at `notifications.summary_time`) and notices sleep. Lock file so only one copy runs.
- `clipvault/notify/ntfy.py`: outbox table, quiet hours, JSON publishing, alerts turned into notifications once each.
- `clipvault/power.py` (psutil, new dependency): transcription waits for mains power unless `heavy_jobs_on_battery: true`.
- Migration `0005_scheduler`: `runs.fail_streak`, `alerts.notified_at`, `notifications` outbox.
- CLI: `run`, `pause`, `resume`, `notify setup|test|status`. Config: `notifications.summary_time`, `notifications.server`.
- The faster-whisper model is now loaded once per process instead of once per round.

**Decisions worth knowing**
- **Catch-up needs no replay logic.** Discovery already works from per-streamer cursors, the worker takes whatever is waiting, and a daily step is due whenever its last success is older than its latest scheduled time. So "on start" and "on wake" are the same as any other round. Sleep is detected from a heartbeat gap over 3 minutes and triggers discover and worker at once; APScheduler's `coalesce` turns missed interval runs into one.
- **A failed daily step is retried every 30 minutes**, not on every heartbeat.
- **Notifications go through an outbox** (`notifications` table), sent by the notify step. Offline or rate-limited sends stay queued (and are not counted as "broken"); messages older than 48 hours are dropped. Quiet hours delay everything except `storage` (disk full) and `test`; `approve` (M9) will also skip them. ntfy docs checked (2026-11-13): JSON publishing to the server root avoids header-encoding trouble with emoji; ntfy.sh allows about 250 messages a day per visitor, so ClipVault sends one summary a day plus alerts, and never one message per clip.
- **Topic name** is `clipvault-` plus 32 random characters, kept in the keychain (`ntfy_topic`). Nothing is queued until `notify setup` has made one, so the outbox never fills up unseen.
- **Database backup** in the daily cleanup: SQLite's backup API into `<library>/.clipvault-backups/clipvault-<weekday>.db`. Seven files overwritten in turn, so nothing ever has to be deleted (hard rule 8), and the copy sits on the library drive. Skipped while the library drive is away.
- **Idle rounds are logged at debug level** only; otherwise the minute-by-minute steps would write about 2,000 lines a day.
- **Pause** is the `paused` setting in the database, so the panel (M5) and the tray icon can share it. Paused stops discover, worker and cleanup; the summary and notifications still go out.

**Verified**
- `pytest -q`: 266 passed. `ruff check .` and `ruff format --check .`: clean. Tests cover quiet hours, the outbox (sent, kept on failure, dropped when old, no network: `httpx.MockTransport`), alerts sent once, the broken alert after 3 failures and its clearing, pause, daily due times across midnight and sleep, heartbeat wake detection, battery skipping transcription, backup rotation, the summary text, the lock, and the real APScheduler starting discovery straight away.
- Live, 3 minutes of `App` against the real database: discovery found 12 new clips for the 3 streamers, the worker saved 11 (1 was a repeat of an existing moment) and transcribed 10, cleanup ran and wrote `clipvault-fri.db` to the library folder, the summary was skipped because notifications are not set up yet.

**Not done yet (needs the user)**
- "Done when" part 1: leave `python -m clipvault run` going, let the laptop sleep for a few hours, and see it catch up after waking. The logic is covered by tests; the real sleep test needs the user's laptop.
- "Done when" part 2: install ntfy on the phone, run `notify setup` and `notify test`. No message has been sent to ntfy.sh yet.

**Open issues and things to watch**
- `run` has to stay open in a terminal window until the tray icon (M5) and start-at-login (M13) exist.
- When the computer is shut down mid-transcription, Ctrl+C waits for the current batch (up to 5 clips, about a minute and a half).
- Windows can put the laptop to sleep while ClipVault is working; nothing keeps it awake. That is by design (it catches up), but long transcription backlogs only move while the laptop is awake and plugged in.

## 2026-11-13: M4 (VOD chat-spike moments), built; waiting for the chat tool

**What changed**
- `clipvault/discover/vod_spikes.py`: the SPEC 5.2 detector as pure functions: 10-second windows, messages + 2 for each message with a hype/laugh emote, spike = above mean + 3 standard deviations of the previous 10 minutes and at least twice that mean, cut from peak - 30 s to peak + 10 s, peaks under 20 s apart merged (capped at 60 s), moments overlapping a clip skipped, best `vod_moments` kept.
- `clipvault/twitch/chat_replay.py`: runs TwitchDownloaderCLI `chatdownload`, stores the chat gzipped in the library (`<login>/chat/`), reads its JSON (`comments[].content_offset_seconds`, `message.body`, falling back to `message.fragments[].text`).
- `clipvault/discover/vods.py`: `find_vods` (Helix Get Videos, type archive, last 7 days, a stream still live is left for later) and `process_vods` (chat -> spikes -> clips `vod:<vod_id>:<offset>` with source `vod_spike`, state `found`). Migration `0006_vods` (vods table with state, skip reason, retries, counts).
- `TwitchClient.get_archives` and `get_live_stream_ids` (Helix Get Streams).
- `ytdlp.download_section` (`--download-sections "*START-END"`), used by `run_fetch` for `vod_spike` clips. Cuts are heavy work: they wait for mains power on a laptop. A cut quieter than -50 LUFS is flagged `reject_reason = 'muted'` (stays `fetched`, never a posting candidate).
- File names of chat moments say `chat` instead of `<n>views`.
- Scheduler step `vods` every hour (3 VODs per round); CLI `python -m clipvault vods`.

**Decisions worth knowing**
- **"Already clipped" includes every clip Twitch lists for the broadcast,** not only the ones in the library: a moment someone clipped with 3 views was still clipped (Get Clips over the stream's time span, matched on `video_id` and `vod_offset`).
- **Long streams are read in 4-hour pieces.** Live check showed kaicenat streams of 35 and 48 hours; one chat file for those would be millions of messages. Each piece is downloaded with `--beginning`/`--ending`, saved as `_partNN.json.gz` (so an interrupted VOD resumes), turned into window scores and dropped from memory. If a piece's offsets turn out to count from the piece rather than the VOD (TwitchDownloader's docs don't say), they are shifted.
- **Extra guards beyond SPEC 5.2:** no spike in the first minute of a stream (not enough history), and a window must score at least 10 (so a near-silent chat going from 0 to 4 messages isn't a "moment"). Both are constants in `vod_spikes.py` for tuning.
- **Chat download failures** are retried after 30 min, 2 h and 8 h, then the VOD is skipped with the reason. "Subscriber-only", "not found", 403/404 skip straight away.
- **Twitch's `muted_segments` is always null with an app token** (docs, 2026-11-13), so muted audio is detected from the cut file itself.
- TwitchDownloaderCLI is MIT-licensed; its JSON layout is not documented, so the parser is tolerant and tested on a synthetic fixture (`tests/fixtures/chat/`).

**Verified**
- `pytest -q`: 287 passed. `ruff check .` and `ruff format --check .`: clean. Detector tests on the fixture chat: steady chat gives nothing, a quiet chat needs a real burst, the three bursts are found at 15, 25 and 35 minutes, the clipped one is skipped.
- Live, Helix: `find_vods` recorded 10 finished streams from the last 7 days for the 3 streamers (3.0 to 47.8 hours long).
- Live, yt-dlp: a 40-second cut from a real xqc VOD took 8 s and came out exactly 40.0 s, 1280x720 at 60 fps, H.264 + AAC, 16 MB. `--download-sections` works on Twitch VODs.

**Not done yet (needs the user)**
- TwitchDownloaderCLI is not installed, so no real chat has been read. Installing it means downloading a program from GitHub (github.com/lay295/TwitchDownloader/releases), which needs the user's OK.
- "Done when": the user watches the moments from 2 real VODs and judges at least half "worth keeping"; hit rate and tuning to be recorded here.

**Open issues and things to watch**
- The 10 VODs found are waiting in the `vods` table; they will be searched as soon as the tool is installed. VODs older than 7 days are not looked at, so after a long break some are missed.
- Chat volume differs hugely between streamers; the emote list (`DEFAULT_EMOTES`) and the minimum score may need tuning per channel once real results are in.

## 2026-11-13: M5 (control panel v1 and tray icon)

**What changed**
- `clipvault/web/server.py` + `settings_routes.py`: FastAPI app with Jinja2 pages. Home (status card green/yellow/red, today's counts, newest clips, pause/resume), Clips (grid, filters by streamer, kind, views, starred; transcript search with highlighted words; paging), clip page (play, star, download, delete, "make vertical copy" with layout choice, transcript), Streamers (add by login or link, Twitch avatar, LIVE badge, save switch, clips per day / min views / chat moments, remove), Settings (storage, schedule, Twitch keys, phone, password), Alerts (fix links, dismiss), setup wizard (mode, Twitch keys, streamers, storage with a GB/month estimate, phone with QR codes).
- `clipvault/web/auth.py`: first password (scrypt hash in the keychain), signed 30-day HttpOnly SameSite=Strict cookie, CSRF token on every form.
- `clipvault/tray.py` (pystray): status colour refreshed every 30 s, Open control panel, Pause/Resume, Quit.
- `run` now starts the panel and the tray icon and opens the browser (`--no-panel`, `--no-tray`, `--no-browser`). `panel reset-password`.
- `config.save_settings` (panel writes config.yaml after validating), `panel.host` / `panel.port` settings.
- `retention.remove_clip_files` shared by retention and the Delete button. **Bug fixed on the way:** retention used to leave a clip's `.words.json` and its vertical renders behind; now every file the app made for the clip goes.
- Web-app manifest and icons (SVG, 192 and 512 PNG) for "Add to Home Screen". New dependencies: `python-multipart`, `qrcode`; `pystray` in the `desktop` extra.

**Decisions worth knowing**
- **No HTMX after all:** plain HTML forms (POST, then redirect) plus a 30-line `app.js` for confirmations and auto-refresh. It works with no JavaScript and needed no extra download. CLAUDE.md lists HTMX in the stack; it can still be added later if a screen needs live updates.
- **The first password can only be set from the computer itself** (127.0.0.1), so binding the panel to the Wi-Fi can't let someone else on the network claim it.
- **CSRF is checked in a FastAPI dependency, not middleware.** Reading the form in middleware left the route with an empty form (a test caught settings silently not being saved).
- **Phones:** the panel listens on 127.0.0.1 unless the user presses "Allow phones on my Wi-Fi" (sets `panel.host: 0.0.0.0`; restart needed). The QR code shows the computer's LAN address. Tailscale works the same way through its own address.
- **Removing a streamer who has clips only switches saving off**, so their clips and history stay; a streamer with no clips is deleted.
- **Settings are written back to config.yaml**, which drops comments in that file (said on the Settings page). The scheduler reads config at start, so schedule changes need a restart (also said on the page).
- Thumbnails: made on first view with FFmpeg (frame at 1 s, 480 px) into `<home>/cache/thumbs/`, outside the library, never deleted; one lock per clip so a page of clips is done in parallel.
- On Windows the panel runs on a selector event loop: the default proactor loop prints a "connection reset" traceback whenever a browser drops a connection.

**Verified**
- `pytest -q`: 303 passed. `ruff check .` and `ruff format --check .`: clean. 16 panel tests: first password only from this computer, short passwords refused, login required, forged cookie refused, wrong password, open redirect blocked, session expiry and logout-everywhere on password change, CSRF refused, wizard pages, home/clips/search/clip page/media, star blocks delete, delete removes video + sidecar + srt + words + vertical, streamer add/update/validate/remove, settings validated and saved, Twitch keys never shown back, ntfy QR, GB estimate, vertical copy runs in the background and shows its error.
- In the browser at phone size (375 px), against a copy of the real database (test session key in environment variables, nothing written to the keychain): walked the wizard, Home showed 131 clips / 827.9 MB, Clips search for "chat" found 8 with highlights, a real HEVC clip played (1280x720), seeking works (HTTP 206), Streamers showed the three avatars, Settings rendered. Fixed from what I saw: the filter row was wider than the phone, buttons stacked without spacing, "1 views".
- `PanelServer` starts, answers, stops in 0.2 s, and reports a busy port instead of crashing.

**Not done yet (needs the user)**
- "Done when": the user does a fresh setup, adds 3 streamers and browses clips on the laptop and the phone without a terminal. Still needs a terminal to start `run` until the installer and start-at-login (M13).
- The tray icon was not seen on screen in this session (it runs in a detached thread; tested only that it is optional).

**Open issues and things to watch**
- HEVC playback: Chromium on this Windows laptop plays the clips; Firefox and some Android browsers may not. The clip page says so and offers Download and the vertical (H.264) copy.
- The Look editor and Queue screens come with M8 and M9.

## 2026-11-13: M8 (hook headline writer and the Look editor), built; waiting for an AI key

**What changed**
- `clipvault/process/write.py`: provider interface (`complete(messages) -> text`) with one implementation for any OpenAI-style chat endpoint, Groq by default. Prompt with every SPEC 9 rule; answers checked in code: JSON shape, headline at most 6 words, no emoji, no BREAKING/LEAKED/BANNED/EXPOSED/CONFIRMED/OFFICIAL unless said in the clip or title, mask-list words blocked, headline must fit the on-screen box (2 lines at 64 px or more), credit `@login on Twitch` added if missing, campaign disclosure put first, hashtags cleaned to 3-5 lowercase words. A rejected answer is asked for again with the reasons (3 tries).
- `jobs.write_for_clip` / `run_write`: headline, post caption and hashtags stored on the clip (migration `0007_copy`), state `transcribed -> written`. The posting pipeline (M9) will pass `reject_on_failure=True` (clip `rejected`, reason `headline`); for the private archive a clip without a usable answer keeps its stand-in headline and stays visible.
- Renders use the clip's written headline, then the stand-in from the title.
- `python -m clipvault headlines [--streamer] [--limit 20]` writes and lists them for review.
- Look editor (Settings and wizard step 5 of 6): headline style (white box / outlined text), highlight colour, caption size 56-96 px, with previews of the three layouts on a recent real clip, redrawn when a control changes. `engine.preview_frame` draws one frame with FFmpeg in about 0.5 s; previews are cached under `<home>/cache/look/`. Config: `look.caption_size`, `llm.provider/model/base_url`.

**Decisions worth knowing**
- **Default model `openai/gpt-oss-20b` on Groq**, docs checked 2026-11-13: production model, supports strict `json_schema` output (so answers are always valid JSON), $0.075 / $0.30 per million tokens in/out, about 500 tokens per headline, so roughly $0.0001 each. Other models get `json_object` mode. `max_completion_tokens` (Groq deprecated `max_tokens`). 429 answers are waited out using `retry-after` (at most 60 s, twice).
- **Key:** `llm_api_key`, or `groq_api_key` if only that one is stored (one Groq key covers transcription and headlines).
- A model that can't be reached stops the run (`LLMError`) instead of burning through every clip; it is not the clip's fault and the clip is not rejected.
- The written copy lives on the clip, not on a render, so every layout and every post of a clip uses the same words.
- **Look preview bug found and fixed:** the split preview picked valkyrae, whose stored cam rectangle had already failed 3 face checks (so it points at her chat). Real renders were never affected (each clip is checked); the preview now only uses confirmed cams and prefers a clip already rendered as split.

**Verified**
- `pytest -q`: 321 passed. `ruff check .` and `ruff format --check .`: clean. 18 writer tests (rule checks, retries with reasons, giving up, Groq request shape against a made-up recorded answer in `tests/fixtures/groq/chat_completion.json`, 429 wait, 401 message, storing, rejecting for posting, unreachable model) and a Look editor test (save, validation, placeholder, cache).
- Real clips: previews for letterbox and split in 0.5 s each; box and outline styles checked by eye; the Look section works in the browser and redraws on change.

**Not done yet (needs the user)**
- No AI key is stored (paid services need the user's OK), so no real headline has been written. "Done when": with a key, run `python -m clipvault headlines --limit 20`, the user reviews them (at most 2 rejected as misleading or dull), and prompt changes are recorded here.

**Open issues and things to watch**
- valkyrae's facecam detection is wrong (it found a box above her chat). Real renders fall back to letterbox, but split will not be used for her until a re-detection finds the real cam; the detector may need a better grow-to-border step for cams without a frame. xqc's split crop includes a strip of chat under his camera.
- "Twitch's own vertical" preview shows a placeholder: none of the 3 streamers' saved clips has a portrait version.

## 2026-11-13: M4 live check with TwitchDownloaderCLI

**What changed**
- With the user's OK, installed TwitchDownloaderCLI 1.56.5 for Windows x64 from the official GitHub release (52.2 MB zip; SHA-256 matched GitHub's published digest `8b1b0695...c813a0`) into `bin/` (gitignored): the program and its licence files, without the 23 MB debug-symbols file. `doctor` finds it.
- **Moments are now ranked by surge** (score divided by the normal level of the 10 minutes before) instead of raw score. On a real 6.7-hour Valkyrae stream, chat in the first hour was 3 to 5 times busier than later, so raw scores picked only moments from the first hour (the biggest of which viewers had clipped already). Surge spreads the picks over the whole stream and favours real surprises. New test for it.

**Verified live**
- Chat file layout matches the parser (`comments[].content_offset_seconds`, `message.body`, `message.fragments`). With `--beginning`/`--ending`, offsets still count from the start of the VOD, so the shift safeguard in `load_chat` isn't needed for this version.
- Valkyrae VOD 2926244338 (6.7 h): 31,022 messages downloaded in 2 pieces in 72 s, 63 spikes. Re-running the detector reused the saved chat pieces (3 s). The 6:31:45 spike (12.7 times normal) was skipped because a viewer had clipped it.
- Kept moments at 3:15:55, 3:18:15 and 5:03:15. All three cut from the VOD (17.4, 16.2, 17.3 MB, 39 s together), none muted, transcribed. From their transcripts and a frame: a story about a long-time regular outing himself in chat, a voice-changer joke with a friend, and a game moment (crushed next to a giant chess pawn at 7/100 health, laughing on camera). By that reading all three are real moments; the user's own viewing is still the "Done when" check.
- `pytest -q`: 322 passed; ruff clean.

**Still to do (user)**
- M4 "Done when": the user watches the moments from 2 real VODs and judges at least half worth keeping. The three are in `Twitch Archive/valkyrae/2026-11/` with `_chat_` in their names, and on the Clips screen under "Chat moments". Nine more finished VODs are waiting and will be searched by `run` (3 per hour) or `python -m clipvault vods`.

## 2026-11-13: M14 (live chat-spike moments), built; waiting for the user's Twitch login

The user chose not to post at all ("No, go to M14"), so M9 to M12 are skipped and marked so in BUILD_PLAN.md.

**What changed**
- `clipvault/twitch/user_auth.py`: user login with Twitch's device-code flow (code entered at twitch.tv/activate; no redirect URL or HTTPS certificate), scope `user:read:chat`, token in the keychain (`twitch_user_token`), validated for login and user id, refreshed 10 minutes before expiry (refresh tokens are single-use, so the new pair is stored at once; a refused refresh forgets the login).
- `clipvault/twitch/eventsub.py`: EventSub WebSocket session (welcome, keepalive, reconnect to the given URL, revocation), `channel.chat.message` v1 subscribe/unsubscribe through Helix, nanosecond timestamps parsed.
- `clipvault/discover/live_spikes.py`: rolling detector with exactly the M4 thresholds (10 s windows, emote weight, mean + 3 sd of the last 10 minutes and at least 2x, minimum score 10); a run of spiking windows is reported once, at its peak, when it ends (or when the stream ends).
- `clipvault/live.py`: `LiveWatcher` thread started by `run`: every 2 minutes asks Helix who is live, finds the in-progress recording, subscribes to chat, closes quiet windows every second, and saves each spike as clip `live:<vod_id>:<offset>` (source `live_spike`, state `found`, `retry_at` 3 minutes after the moment). The download step cuts it like a chat moment (`CUT_SOURCES`). Pause stops watching; without a login it waits quietly; a refused login raises a "relogin" alert with a link to the panel.
- CLI `twitch login|logout|status` and `live`; panel Settings "Live chat" with a Log in to Twitch button that shows the code and refreshes until approved; "live moment" badge; the Clips filter "Chat moments" includes live ones. New dependency: `websockets`.

**Decisions worth knowing**
- **EventSub, not IRC.** Twitch's docs (2026-11-13) call EventSub "the preferred method" and recommend it over IRC; anonymous IRC reading isn't documented. EventSub needs a user token, hence the one-time login.
- **The in-progress recording is listed by Get Videos while the stream runs** (not stated in the docs; checked live on 4 channels: an archive with the live stream's `stream_id` and a growing duration). Cutting from it works: 40 s from 5 minutes before the live edge of a running stream took 9.8 s, 1280x720, 39.7 s.
- **Cuts wait 3 minutes** after the moment so the recording contains it; the cut times are in recording time (peak minus the recording's `created_at`).
- **No duplicates with M4:** a live moment carries `vod_id` + `vod_offset`, so when the finished stream's chat is searched later, that stretch counts as already clipped; the same live moment found twice is refused by the overlap check.
- **One connection** for all live channels (Twitch allows 300 subscriptions per connection). When nobody is live it is closed, because Twitch closes unused connections itself (code 4003 after 10 seconds, seen live). Messages missed during a reconnect are not replayed (Twitch doesn't); a moment may be missed then, and M4 still finds it after the stream.
- Create Clip From VOD for permissioned streamers is not built (no posting, no permissions).

**Verified**
- `pytest -q`: 338 passed; ruff clean. 16 new tests: steady chat gives nothing, a burst is reported once after it ends, silence still closes windows, flush at stream end, session welcome/chat/keepalive/reconnect/errors, subscription request shape and 401, device login waiting then storing, refresh and refused refresh, watcher saving a moment with the right recording offset and 3-minute wait, no duplicate, stream end, no recording, pause, no login, refused login alert, connection closed when nobody is live; panel login flow.
- Live: the EventSub WebSocket welcomes in 0.34 s and closes an unused connection with 4003 as documented; in-progress recordings found and cut on a running stream (above).

**Not done yet (needs the user)**
- "Done when": the user logs in to Twitch once (Settings, Live chat, or `python -m clipvault twitch login`), leaves `run` going while one of the streamers is live, and at least one live moment is created and saved. Until then no real chat has been read through EventSub; subscribing to another channel's chat with a regular viewer's token is what the docs describe but is only proven by that first live run.

**2026-11-13, later: user logged in.** `python -m clipvault twitch login` (device code, approved by the user at twitch.tv/activate) stored a user token. With it, a `channel.chat.message` subscription to a channel the user has no relation to (a 42,000-viewer live stream) was accepted, and 27 chat messages arrived in 20 seconds, about 0.4 s after they were sent. So a regular viewer token with `user:read:chat` can read any channel's chat. Still to see: one of the saved streamers live with `run` going, and a live moment saved and cut ("Done when").

## 2026-11-13: the dashboard shows edited phone versions, made automatically

User request: "i should be seeing edited clips in mobile format not the raw downloads, maybe have a button for 'see original' ... just want the newest clips edited in the best way and ready for me to view or save in the dashboard."

**What changed**
- **Every saved clip gets its phone version automatically** (`jobs.run_render`, in the worker step after transcription): best layout, headline, word-by-word captions, loudness, newest clips first, 3 per round, on mains power only. A clip that fails to render is remembered (`clips.render_error`, migration `0008_auto_render`) and not retried every round. Clips wait for their transcript, since the captions come from it.
- **Dashboard shows only edited clips:** Home's newest clips and the Clips grid list clips that have a phone version, as 9:16 cards (2 columns on a phone, 4-5 on a computer) with a thumbnail taken from the phone version, so the headline and captions show. A line says how many are still being edited.
- **Clip page:** the phone version plays first, with **Save video** (downloads the phone version), **See original** (the Twitch download, with Download original) and Star. Re-making with another layout, On Twitch and Delete moved under "More". The headline is the page title; the Twitch title is shown under it.
- Storage estimate in Settings now includes the phone versions (about 36 MB each, so most of the space: about 10.5 GB a month per streamer at the defaults).
- **Bug fixed:** phone versions carried a 1216:1215 pixel shape (from scaling 1280x720 to 1080x608), so players showed them 1081 wide. Renders now end with `setsar=1`; the ones made before were re-rendered.
- Quieter console: APScheduler's "skipped: maximum number of running instances" (a long editing round still going) is no longer printed. The chat tool's error message no longer shows a progress line ("[STATUS] - Downloading 55%").

**Verified**
- `pytest -q`: 340 passed; ruff clean. New tests: newest-first render queue, failed render not retried, clip waiting for its transcript; dashboard lists only edited clips, counts the rest, phone version first, See original and back.
- Live: after restarting `run`, the first worker round made 3 phone versions with no failures (QA passed on 2 of 3; the third came out at -16.2 LUFS, 0.2 dB quieter than the loudness check allows; it plays fine). In the browser at phone size: the grid of vertical cards, a phone version playing at 1080x1920, See original playing the 1280x720 original.

**Open issues**
- Without an AI key the headlines are made from Twitch titles, and some are weak ("big", "smoke"). An AI key (`python -m clipvault secrets set llm_api_key --window`) would make them proper hooks.
- About 125 clips were waiting to be edited at the time of the change; at 3 per 2-minute round that is roughly an hour and a half on mains power.

## 2026-11-13: credit line off by default; Twitch's vertical versions as an option

**What changed**
- The "@login on Twitch" line under the headline is off by default (`look.show_credit`, checkbox in the Look editor with live preview). Existing phone versions were re-made without it.
- Twitch's own vertical version of a clip is now usable: per-streamer **Phone layout** setting on the Streamers screen (Automatic, Whole picture, Facecam on top, Twitch's own vertical version) and the same choice on a clip's "Make it again". When that layout is wanted, the renderer downloads Twitch's version on the spot (stored as `<clip>_portrait.mp4`, recorded in `portrait_path`; an empty value means Twitch has none). `download_portrait` was built in M7 but never called until now.
- **Automatic no longer prefers it.** SPEC 10.2 put the native layout first. Checked on one recent clip per streamer (all three streamers have vertical versions for every clip checked, 12 of 12): xqc's is his own Dual Format design (camera on top, game below) with his live captions burned in, so the headline covered his face and the captions doubled; valkyrae's was two stacked game crops, dark at the start (failed the black-frame check); kaicenat's was a centre crop with blurred bars, fine. Automatic now picks facecam-on-top, then whole picture; Twitch's version only by choice. This departs from SPEC 10.2 on purpose.
- `docs/HOW_IT_WORKS.md`: a plain-language description of every feature and how the parts interact, linked from the README.

**Verified**
- `pytest -q`: 342 passed; ruff clean. New tests: credit off unless switched on; layout choice saved per streamer (invalid values ignored); automatic choice ignores the vertical version.
- Live: choosing Twitch's version on a Kai Cenat clip with nothing downloaded fetched it and rendered with it. The three test renders and their downloads were removed afterwards, so nothing is left that the user didn't choose.

## 2026-11-13: Panel redesign

Request: replace the control panel's look with the design in `docs/design/` (DESIGN_HANDOFF.md plus the `.dc.html` mockups).

**What changed**
- New stylesheets in `clipvault/web/static/`: `tokens.css` (colour tokens as CSS custom properties; dark values under `prefers-color-scheme: dark`; the phone mockups' own page, card and fill colours below 600 px), `panel.css` (all components), and `motion.css` copied unchanged from the design folder. `style.css` and `_phone.html` were deleted; a test checks nothing links to them.
- The shared layout was rebuilt: a sidebar with stroke icons and a red problem badge on Home. Below 600 px it becomes a floating bottom tab bar with filled icons and the same badge. Shared `<head>` in `_head.html`; the setup and login pages use the centred card in `bare.html`.
- Home, Clips, a clip's page, Streamers, Settings, the Look editor, the problems page, the login page, the first-run password page and the six setup steps were rebuilt from their mockups.
  - Switches are real checkboxes.
  - Segmented controls and colour swatches are radio buttons.
  - Forms marked `data-autosubmit` save on change, with a Save button for browsers without scripts.
- It follows the system appearance; there is no theme setting.
- System font stack from the handoff; icons are the inline SVGs from the mockups. No SF fonts or SF Symbols are bundled.
- Routes, form field names and the hidden `csrf` field are unchanged. There were no HTMX attributes to keep.
  - The server changes are display data only: problem count, free space, per-streamer bytes and estimate, transcript lines with timings, and the phone version's id and time in thumbnail links so a re-made version shows a fresh picture.
  - One renderer fix: making a clip again in a layout that writes to the same file no longer leaves two records for one file. Deleting one record had removed the shared file; a test covers it.
- Clip page: "What was said" lists the transcript line by line. Clicking a line jumps the video there, and the line being spoken is highlighted.

**Verified, and how**
- The mockups were rendered in the built-in browser with a small stand-in for their template runtime (props from the URL, e.g. `?dark=true`, `?step=3`). They were compared with the real panel, run from a copy of the database, at the same window size: desktop 1280x1000, setup 1000x940, phone 390x844.
- **Layout.** A script measured the position, size, font size, line height, padding and corner radius of matching elements on both, and every difference was fixed to the mockup's number. Screenshots were compared for:
  - Home, Clips, a clip's page, Streamers and Settings (light and dark);
  - setup steps 1-6 and the password page (light; step 3 also dark);
  - phone Home and Clips (light and dark).
- **Colours.** A second script read each element's text colour and background on both versions:
  - identical on every page checked in dark (Home, Clips, clip page, Streamers, Settings, setup);
  - identical on phone in light and dark, after giving the phone pages the phone mockups' own colours.
- **Reduce Motion:** motion.css sets `animation: none; transition: none` on every element under `prefers-reduced-motion: reduce`. The two transitions in panel.css are covered by that rule, and no script animates anything.
- `pytest -q`: 344 passed. `ruff check` and `ruff format --check` are clean.
- The real ClipVault was restarted and serves the new panel at 127.0.0.1:8765.

**Differences from the mockups, on purpose**
- **Font.** The mockups load Inter from Google Fonts. The panel uses the handoff's system stack, which is Segoe UI on this Windows PC, so some text is a few pixels wider or narrower and a few lines wrap differently. Bundling Inter (open font licence) would close that gap.
- **Streamers.** Each row has an extra **Phone layout** choice (added before the redesign). It sits on the second row with Remove. "Keep" combines the old top-N and minimum-views settings into one choice; the per-day number is kept in a hidden field.
- **Setup and Settings.**
  - Step 6 shows a "Set Up Notifications" button until a notification topic exists. After that it shows the QR code and steps as drawn.
  - Settings has no restart or logout rows; those features aren't in the mockup either.
  - The Look colour row also shows the current colour when it isn't one of the four in the palette, so the existing choice isn't lost.
- **Phone width.** The chip and newest-clip rows scroll sideways like the mockup. Windows shows a thin scrollbar under them; phones don't.

**Open issues**
- None blocking. Optional: bundle Inter for exact text widths.
