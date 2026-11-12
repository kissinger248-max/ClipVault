# Build plan

Work top to bottom, one milestone per session (or a few sessions for the big ones). A milestone is done when every box is ticked, every "Done when" check has been run, `pytest` passes and `ruff` is clean. Then append a dated entry to `docs/PROGRESS.md`.

To start each session, the user will say something like: "Read CLAUDE.md, docs/SPEC.md, docs/BUILD_PLAN.md and docs/PROGRESS.md, then continue with the next unchecked milestone."

Accounts and keys the user needs, by milestone:

| Needed by | What | Cost |
| --- | --- | --- |
| M1 | Twitch developer application (dev.twitch.tv/console): client ID and secret; redirect URL `http://localhost:8765/auth/twitch/callback` | Free |
| M3 | ntfy app on the phone | Free |
| M6 | Groq API key | About $1/month at this volume |
| M8 | LLM API key (Groq works) | A few dollars a month at most |
| M9 | Upload-Post paid plan (TikTok posting isn't on the free plan); TikTok and YouTube accounts for the niche | $16–24/month |
| M13 | Tailscale on laptop and phone (free Personal plan is non-commercial); a server (Oracle Always Free or a small VPS) if posting 24/7 | $0–15/month |

---

## Part 1: Archive mode (private clip library)

### M0. Skeleton

- [x] `pyproject.toml` with dependencies and a `dev` extra (pytest, ruff); package `clipvault` with the layout from CLAUDE.md
- [x] Config loader: `config.yaml` (non-secret, see SPEC §17) + keyring secrets; sensible defaults
- [x] SQLite connection in WAL mode; migrations folder; tables from SPEC §2
- [x] JSON-lines logging to `logs/`
- [x] CLI: `python -m clipvault doctor` checks Python version, FFmpeg, yt-dlp, TwitchDownloaderCLI, config file, keyring access, free disk space; prints clear fix instructions for anything missing
- [x] `.gitignore` (venv, logs, library, `.env`, database)

**Done when:** `doctor` runs and reports each check; `pytest` runs (even with one trivial test); fresh clone → working in under 5 minutes following the README.

### M1. Twitch connection and viewer-clip discovery

- [ ] App access token (client credentials) with automatic renewal
- [ ] `GET /helix/users` lookup and caching of `twitch_user_id`, display name, avatar
- [ ] `GET /helix/clips` by broadcaster since the `discover` cursor, with pagination (SPEC §5.1)
- [ ] Selection: `top_n` per streamer per day or `min_views`; skip known ids; insert as `found`
- [ ] `streamers.txt` import (one login per line, optional `key=value` settings)
- [ ] CLI: `python -m clipvault discover [--streamer LOGIN] [--since 7d]`
- [ ] Unit tests with recorded API responses
- [ ] Check whether Twitch Auto Clips show up in results for a streamer known to use them; record the answer in PROGRESS.md

**Done when:** running `discover` for 3 real streamers lists their top recent clips; running it again adds nothing new.

### M2. Downloads and the library folder

- [ ] Clip download with yt-dlp (quality from config) → `fetched`; `downloaded_via = unofficial`
- [ ] File naming, folders and JSON sidecar exactly as SPEC §13
- [ ] Dedupe of overlapping clips (SPEC §7, first rule)
- [ ] Retention rules and disk guard (SPEC §13)
- [ ] Daily yt-dlp update; "downloader may need an update" alert after 3 consecutive failures
- [ ] Measure and record average MB per 30 s at 720p and 1080p in PROGRESS.md

**Done when:** a first run for 3 streamers fills the library correctly; a second run downloads nothing twice; deleting a file by hand doesn't crash anything; disk guard pauses downloads when tested with a tiny fake threshold.

### M3. Scheduler, catch-up and phone notifications

- [ ] In-process scheduler with the jobs in SPEC §4 that exist so far (discover, worker, cleanup, summary)
- [ ] Cursors in `runs`; on start and on wake, catch up from `last_success`
- [ ] Laptop power awareness: heavy steps only on AC power (configurable)
- [ ] ntfy client; daily summary; "broken" alert after 3 failures of the same step
- [ ] Retry policy (1, 5, 30 min) and `dead` state (SPEC §3)
- [ ] CLI: `python -m clipvault run`

**Done when:** with the app running, a laptop asleep for several hours catches up within one cycle of waking; the phone receives a test summary and a test failure alert.

### M4. VOD chat-spike moments

- [ ] `GET /helix/videos` (archives) since cursor
- [ ] Chat replay download via TwitchDownloaderCLI
- [ ] Spike detector exactly as SPEC §5.2, as a pure function with unit tests on fixture chat logs
- [ ] Skip spikes covered by viewer clips; keep top `vod_moments`
- [ ] Section download with `yt-dlp --download-sections`; ids `vod:<vod_id>:<offset>`
- [ ] Handle sub-only/disabled VODs and muted audio (flag near-silent files)

**Done when:** on 2 real recent VODs, the saved moments are watched by the user and at least half are judged "worth keeping"; record the hit rate and any tuning in PROGRESS.md.

### M5. Control panel v1 and tray icon

- [ ] FastAPI app bound to 127.0.0.1:8765; password on first open; CSRF on forms
- [ ] Phone-first layout; bottom tabs Home, Clips, Streamers, Settings (Queue arrives in M9)
- [ ] Home: status card, today's counts, newest clips, Pause/Resume
- [ ] Clips: grid, filters, play, star, delete, download
- [ ] Streamers: add (search by login, avatar, live status), remove, Save switch, per-streamer settings
- [ ] Settings: storage folder, quality, retention, schedule, notifications
- [ ] Setup wizard steps 1–4 and 7 (SPEC §15)
- [ ] Alerts screen with Fix links
- [ ] Tray icon (pystray): status color, Open, Pause, Quit
- [ ] Web-app manifest and icons so "Add to Home Screen" works

**Done when:** on a fresh install, the user completes setup, adds 3 streamers, and browses saved clips without touching a terminal, on both laptop and phone (phone over Tailscale or the local network for now).

**Gate: archive mode complete.** The user uses it for a week before Part 2 starts. Fix anything annoying first.

---

## Part 2: Vertical rendering

### M6. Transcription and search

- [ ] Audio extraction (16 kHz mono) and Groq transcription with word timestamps (SPEC §8)
- [ ] faster-whisper fallback; switch automatically when Groq fails
- [ ] `.srt` and word-timing JSON next to each clip
- [ ] Transcript search in the Clips screen
- [ ] Slur masking list

**Done when:** 20 clips transcribed, search finds a phrase the user remembers hearing, and pulling the network cable switches to the local fallback.

### M7. Render engine and QA

- [ ] Bundle Montserrat (Black, ExtraBold, SemiBold) and Anton in `assets/fonts/`
- [ ] ASS writer for headline, credit, disclosure and karaoke captions with styles from a YAML template (SPEC §10.5)
- [ ] Letterbox layout first, then native vertical, then split
- [ ] Facecam detection and caching (SPEC §10.4); 320 px minimum cam width rule
- [ ] Export settings and two-pass loudness (SPEC §10.6)
- [ ] All QA checks in SPEC §11, with the UI-overlay preview still
- [ ] Music tagger and the posting-candidate filter (SPEC §7)
- [ ] CLI: `python -m clipvault render CLIP_ID [--layout ...]`
- [ ] Optional "make vertical copy" button in Clips

**Done when:** 10 renders across 3 streamers pass QA; the user views them on a real phone (send via any method) and confirms text is never under TikTok or Shorts buttons; safe-zone numbers adjusted if needed and recorded.

### M8. Hook headline writer and the Look editor

- [ ] LLM provider interface; Groq default
- [ ] Prompt and validation rules from SPEC §9; regenerate up to 3 times
- [ ] Look editor in Settings: live preview of the three layouts on a recent clip; headline style, accent color, caption size
- [ ] Wizard step 6

**Done when:** 20 generated headlines are reviewed by the user; at most 2 rejected as misleading or dull, and prompt changes recorded.

---

## Part 3: Posting (permissioned streamers only)

### M9. Publishing with review mode

- [ ] Permission fields and badges in Streamers; Post switch locked until permission is recorded; authorize-with-Twitch flow (user token, `channel:manage:clips`) for official downloads via Get Clips Download (SPEC §6)
- [ ] Posting gate in the publish path + unit test proving a non-permissioned streamer can never be posted
- [ ] Publisher interface; Upload-Post adapter (SPEC §12.1); `--dry-run` that logs exactly what would be sent
- [ ] Wizard step 5: Upload-Post connect link for TikTok and YouTube
- [ ] Queue with caps, spacing, jitter, active hours (SPEC §12.2)
- [ ] Review mode with ntfy Approve/Skip actions and the 2-hour auto-post (SPEC §12.3)
- [ ] Queue screen
- [ ] Auto-pause on policy errors; global pause
- [ ] **Ask the user** before the first real post on each account; first post public, checked from a logged-out browser

**Done when:** 20 posts go out in review mode with no manual fixes (Gate 1 in the plan doc).

### M10. Campaigns and stats

- [ ] Campaign settings per streamer: campaign URL, disclosure text, allowed platforms
- [ ] Disclosure line and commercial-content flag on campaign posts
- [ ] Whop API submission attempt; fallback ntfy one-tap submit (SPEC §12.4); record which works
- [ ] Stats polling at 1 h, 24 h, 7 d; per-account report of layouts and headline styles vs views
- [ ] Optional: daily check of the public Content Rewards directory for new Twitch campaigns in the user's niche

**Done when:** one real campaign clip is posted with disclosure and its link is submitted within 30 minutes; stats appear in Queue.

### M11. Unattended hardening

- [ ] Review mode can be switched off; everything else unchanged
- [ ] Layout drift detection re-runs facecam detection automatically
- [ ] Nightly database backup; restore script tested
- [ ] Chaos checks: kill the process mid-render, mid-upload, mid-download; restart; confirm no duplicates and no stuck clips
- [ ] Token expiry paths produce "relogin" alerts with working Fix links

**Done when:** 7 days unattended with no missed posting slots (Gate 2 in the plan doc).

### M12. Weekly long-form compilation

- [ ] Selection, countdown cards, captions and chapters (SPEC §10.7)
- [ ] Respect permission scope (compilations allowed?) per streamer
- [ ] Auto thumbnail: highest-motion frame of #1 + 3–4 words from the template
- [ ] Schedule Sunday 02:00; posts to YouTube through the same adapter

**Done when:** one compilation is reviewed by the user and posted.

---

## Part 4: Packaging and extras

### M13. Installers, server and sync

- [ ] PyInstaller builds for Windows and macOS with bundled tools; start at login
- [ ] Docker image and `docker-compose.yml` for the server
- [ ] Tailscale-bound control panel option; instructions for phone access
- [ ] Syncthing instructions for server → laptop library sync
- [ ] Update check and one-click update

**Done when:** a non-technical person can install the laptop app from the installer alone, and the server comes up with one command.

### M14. Live chat-spike clipping (optional)

- [ ] EventSub chat listener for live permissioned streamers
- [ ] Create Clip From VOD with padding (SPEC §5.3)

**Done when:** during one live stream, at least one live-spike clip is created and saved.
