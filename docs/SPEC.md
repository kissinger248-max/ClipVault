# ClipVault spec

Facts below were checked on 12 November 2026. External APIs change: before coding an integration, open the linked docs and follow them where they differ from this file.

## 1. Modes

| Mode | What it does | Where it runs |
| --- | --- | --- |
| Archive | Finds and saves clips for any streamer on the list to a local library. Optional transcripts and vertical copies. Never posts. | Laptop (catch-up after sleep) or server |
| Archive + Post | Archive, plus renders and posts Shorts/TikToks for permissioned streamers, plus a weekly 10-minute YouTube compilation | Server (24/7), optionally syncing the library to a laptop |

Each streamer has two switches: `save_enabled` and `post_enabled`. `post_enabled` is only allowed when permission is recorded (see hard rule 1 in CLAUDE.md).

Permission types:
- `campaign`: the streamer runs a paid clipping campaign (Whop Content Rewards, Vyro). `permission_ref` = campaign URL. Campaign footage is licensed only for that campaign's clips.
- `written`: the streamer said yes directly. `permission_ref` = where (for example "DM on X, 2026-11-20"). Ideally they also authorized the app on Twitch (`channel:manage:clips`), which enables official downloads.
- `none`: archive only.

## 2. Data model (SQLite)

```
streamers(
  id INTEGER PK, login TEXT UNIQUE, twitch_user_id TEXT, display_name TEXT, avatar_url TEXT,
  save_enabled INT DEFAULT 1, post_enabled INT DEFAULT 0,
  permission_type TEXT CHECK(permission_type IN ('none','campaign','written')) DEFAULT 'none',
  permission_ref TEXT, twitch_authorized INT DEFAULT 0,
  top_n INT DEFAULT 5, min_views INT, vod_moments INT DEFAULT 3,
  layout_override TEXT, cam_rect TEXT, content_rect TEXT, layout_checked_at TEXT,
  created_at TEXT)

clips(
  id TEXT PK,                     -- Twitch clip id, or 'vod:<vod_id>:<offset_s>' for VOD cuts
  streamer_id INT, source TEXT,   -- viewer | auto | vod_spike | live_spike
  title TEXT, created_at TEXT, view_count INT, duration REAL,
  vod_id TEXT, vod_offset INT, game_name TEXT, creator_name TEXT, url TEXT,
  state TEXT, reject_reason TEXT, fail_count INT DEFAULT 0, last_error TEXT,
  file_path TEXT, portrait_path TEXT, sidecar_path TEXT, transcript_path TEXT,
  music_score REAL, speech_wps REAL, score REAL, starred INT DEFAULT 0,
  downloaded_via TEXT,            -- official | unofficial
  updated_at TEXT)

renders(id PK, clip_id, layout, path, headline, post_caption, hashtags_json,
        qa_passed INT, qa_report_json, created_at)

posts(id PK, render_id, platform, account, scheduled_at, status,   -- queued|approved|posted|failed|skipped
      service_post_id, post_url, posted_at, views_1h, views_24h, views_7d,
      campaign_submitted_at, campaign_submission_id)

runs(job TEXT PK, last_started TEXT, last_success TEXT, cursor TEXT, last_error TEXT)
alerts(id PK, kind, message, fix_url, created_at, resolved_at)
settings(key TEXT PK, value TEXT)   -- non-secret settings only
```

## 3. Clip state machine

`found → fetched → filtered (or rejected) → transcribed → written → rendered → qa_passed (or qa_failed) → scheduled → posted → submitted`

- Archive-only clips stop at `fetched` (or `transcribed` if transcripts are on).
- Each transition is one job step. A failed step retries after 1, 5 and 30 minutes; after 3 failures the clip is `dead`, counted in the daily summary, and never blocks the queue.
- Steps must be idempotent: check the database and the file system before doing work.

## 4. Jobs and schedule

All times are the user's local time zone (default America/Chicago). Every job stores `last_success` in `runs`; on start it processes everything since that cursor, so missed runs (laptop asleep) are caught up.

| Job | Default | Notes |
| --- | --- | --- |
| discover | every 15 min (server), every 30 min + on wake (laptop) | Get Clips + Get Videos since cursor |
| worker | continuous | advances clips one state at a time; heavy steps (VOD cuts, transcription, render) only on AC power on laptops |
| publisher | every 10 min | posts due items, then campaign submission |
| stats | hourly | views at 1 h, 24 h, 7 d |
| token_refresh | hourly | Twitch refresh tokens; alert if revoked |
| longform | Sundays 02:00 | weekly compilation |
| cleanup | daily 04:00 | retention, disk guard, database backup |
| summary | daily 09:00 | one ntfy notification |

## 5. Discovery

### 5.1 Viewer clips and Auto Clips (primary)

- App access token (client credentials) is enough for public data.
- `GET /helix/users?login=` once per streamer; cache `twitch_user_id`.
- `GET /helix/clips?broadcaster_id=&started_at=&ended_at=&first=100`. Exactly one of `broadcaster_id`, `game_id`, `id` is required. `started_at`/`ended_at` are RFC3339, truncated to the minute; without `ended_at` the window is one week. For broadcaster queries, results are sorted by view count, highest first. Pagination stops around 1,000 results; shrink the window if needed.
- Select per streamer per day: top `top_n`, or every clip with `view_count >= min_views`. Skip ids already in `clips`.
- Twitch Auto Clips should appear in the same results. Verify in milestone M1 and record in PROGRESS.md.
- Docs: https://dev.twitch.tv/docs/api/reference/clips.md

### 5.2 VOD chat spikes (moments nobody clipped)

- `GET /helix/videos?user_id=&type=archive` since cursor; process VODs whose stream has ended.
- Download chat replay as JSON with TwitchDownloaderCLI (`chatdownload`). https://github.com/lay295/TwitchDownloader
- Bucket messages into 10-second windows. Per window: `count` + 2 × (messages containing a hype or laugh emote from a configurable list, for example KEKW, LUL, OMEGALUL, Pog, PogChamp, LMAO, W). Spike = window score > rolling mean of the previous 10 minutes + 3 standard deviations, and at least 2× that mean.
- Chat reacts 5–15 s after the event: cut from peak − 30 s to peak + 10 s (40 s). Merge spikes closer than 20 s.
- Skip spikes overlapping an existing viewer clip (compare against each clip's `vod_id` + `vod_offset` + `duration`).
- Keep the top `vod_moments` per VOD.
- VODs expire after 7–60 days depending on the streamer; subscriber-only or disabled VODs yield nothing (record why).
- Unit-test the spike detector with fixture chat JSON.

### 5.3 Live chat spikes (optional, later milestone)

- EventSub WebSocket `channel.chat.message` while the streamer is live.
- Same scoring on a live rolling window. For permissioned streamers: `POST /helix/videos/clips` (Create Clip From VOD, open beta) with `editor_id`, `broadcaster_id`, `vod_id`, `vod_offset`, `title`, `duration` 5–60 (default 30). The clip ends at `vod_offset` and starts at `vod_offset − duration`; `vod_offset` must be ≥ `duration`. Needs `editor:manage:clips` or `channel:manage:clips`. Pad 5 s each side.
- Plain `POST /helix/clips` (Create Clip) needs `clips:edit`, takes `duration` 5–60 s, is asynchronous (confirm via Get Clips; assume failure after 60 s), and the clip appears publicly under the user's name. Off by default.

## 6. Fetching footage

| Case | Method |
| --- | --- |
| Streamer authorized the app | `GET /helix/clips/downloads?editor_id=&broadcaster_id=&clip_id=` (1–10 ids). Needs a user token with `editor:manage:clips` or `channel:manage:clips`; `editor_id` must match the token's user. Returns `landscape_download_url` and `portrait_download_url` (either can be null). Rate limit 100 requests/min. URLs are temporary: download immediately. 403 = not an editor. |
| Archive-only streamer | `yt-dlp` on the clip URL (best quality available; optional 720p cap from settings) |
| VOD spike cut | `yt-dlp --download-sections "*START-END"` on the VOD URL (needs FFmpeg); never download the whole VOD |
| Vertical version of a clip (archive) | optional: `twitch-dlp` (Node) can fetch portrait versions |

Record `downloaded_via` on every clip. Update yt-dlp daily (`pip install -U yt-dlp` in the app's environment); after 3 consecutive download failures across streamers, alert "downloader may need an update" once.

## 7. Filter and score

- Dedupe: if two clips from the same VOD overlap by more than 50%, keep the higher `view_count`.
- Duration: posting candidates must be 15–59 s (trim from the start if longer, keeping the end).
- Music: run the audio tagger; if `music_score` > 0.5 (tune in testing), the clip is not posted as a Short.
- Speech density: `speech_wps` = words per second from the transcript; under 0.5 → low priority for posting.
- `score = view_velocity × (0.5 + min(speech_wps, 3)/3) × novelty`, where `view_velocity` = views per hour since creation and `novelty` = 1 − max similarity of the transcript to the last 20 posts (simple token Jaccard is fine).

## 8. Transcription

- Groq `whisper-large-v3-turbo`: $0.04 per audio hour, minimum 10 s billed per request, file limit 25 MB (free tier) / 100 MB (dev tier). Request word-level timestamps. Send audio only (extract with FFmpeg to 16 kHz mono).
- Fallback: `faster-whisper` small, int8, on CPU.
- Output: `.srt` next to the clip and word timings in JSON.
- Mask slurs from a configurable list as `****` in caption text only.

## 9. Hook headline and post caption

Input to the LLM: transcript with timestamps, streamer display name, game, top 10 chat words in the spike window (if known), clip title from Twitch.

Required JSON output:
```
{"headline": "...", "post_caption": "...", "hashtags": ["...", "..."]}
```

Rules (put in the prompt and enforce in code):
- `headline`: 6 words or fewer, describes only what happens in the transcript or clip title. No claims about real people beyond that, no fake news framing ("BREAKING", "LEAKED", "BANNED" unless said in the clip), no slurs, no emoji.
- `post_caption`: one or two short sentences + credit "@<twitch_login> on Twitch". For campaign clips the first line is the disclosure required by the campaign (default "#ad Paid partnership with @<login>").
- `hashtags`: 3–5, lowercase, no spaces.
- Validate: schema, word count, blocklist regex. On failure regenerate up to 3 times, then mark the clip `rejected` with reason `headline`.

## 10. Rendering (1080×1920)

### 10.1 Safe box

All burned-in text inside x 150–930, y 270–1340. Faces in split layout must also sit inside it. Published UI overlays: TikTok bottom 320–480 px and right 120–150 px; YouTube Shorts bottom 270–672 px depending on source. These numbers are deliberately conservative; verify on a real phone in M7 and make them config values.

### 10.2 Layout choice (first that fits)

1. **native**: `portrait_path` exists (streamer uses Twitch Dual Format).
2. **split** (default when possible): streamer has a valid `cam_rect` and the face check passes on 3 sampled frames.
3. **letterbox**: everything else.
`layout_override` on the streamer wins.

### 10.3 Geometry

| Layout | Element | Source | Placement |
| --- | --- | --- | --- |
| letterbox | video | whole 16:9 frame scaled to 1080×608 | y 600–1208; black above and below (option: blurred copy) |
| split | content | 1080×1080 crop of gameplay (centered or `content_rect`) | y 0–1080 |
| split | cam | `cam_rect` widened to 9:7, scaled to 1080×840 (Lanczos + light unsharp) | y 1080–1920, crop positioned so the face center lands near y 1250 |
| native | whole clip | portrait file scaled to 1080×1920 | full frame |

If `cam_rect` width < 320 px, don't use split (too much upscaling).

### 10.4 Facecam detection (once per streamer, cached)

1. Sample ~20 frames from that streamer's 3–5 most recent clips.
2. Run YuNet face detection. Keep faces with confidence > 0.8.
3. If most detections fall inside one stable rectangle in a corner region, that's the cam. Expand to the overlay border using edge detection (the cam usually has a hard rectangular edge).
4. Store `cam_rect` and `content_rect` (the rest of the frame, or center crop) as JSON `[x, y, w, h]` in source pixels.
5. Per clip: detect faces on 3 frames; if the face is outside `cam_rect`, use letterbox for that clip. Three consecutive misses → re-run detection.
6. If one large face is centered (full-cam "just chatting" scene), letterbox it; face-follow cropping is a later improvement.

### 10.5 Text styles (one ASS file per render)

| Style | Font | Size | Look | Position |
| --- | --- | --- | --- | --- |
| Headline | Montserrat Black (alt: Anton) | 88 px, auto-shrink to fit 780 px width, minimum 64 | Black text on white rounded box (radius 24, padding 28 × 16); one key word in accent color; max 2 lines | box centered at x 540, top at y 270; visible from frame 1 to the end |
| Credit | Montserrat SemiBold | 36 px | white, 80% opacity | centered just under the headline box |
| Disclosure | Montserrat SemiBold | 32 px | white | under the credit, first 3 s, campaign clips only |
| Caption | Montserrat ExtraBold | 72 px | white, 7 px black outline, soft shadow; active word in accent color (default yellow) at 110% scale | centered at x 540, max width 780; y center 1260 (letterbox), 1080 (split, on the seam), 1240 (native) |

Caption blocks: 2–5 words, split at pauses > 0.3 s and phrase boundaries, 1 line normally, 2 at most, each block on screen ≥ 0.4 s. Karaoke timing from word timestamps. No emoji burned in. Fonts ship in `assets/fonts/` (SIL Open Font License) and are passed to FFmpeg with `fontsdir`.

Style values live in a per-account template (YAML) so the look changes without code changes.

### 10.6 Export

- 1080×1920, source frame rate (30 or 60), H.264 High, yuv420p, target 8–15 Mbps
- AAC 48 kHz 192 kbps; loudness normalized to −14 LUFS integrated, true peak ≤ −1 dBTP (two-pass `loudnorm`)
- MP4 with `+faststart`
- Cover frame: 1.0 s

### 10.7 Long-form compilation (weekly)

- Per niche/account: top 12–20 clips of the past 7 days by views, ranked as a countdown, total 9–11 minutes (over 8 minutes allows mid-roll ads).
- 1920×1080. Before each clip: a 2-second card with rank and one line of context (streamer, game, what happens). Captions 64 px centered near y 940. Rank badge top-left during each clip.
- Description includes chapters (`00:00 #20 ...`) and credits for every streamer.
- Only include clips whose permission covers compilations; check each campaign's brief.

## 11. Automatic QA (all must pass before scheduling)

1. Duration 15–59 s (Shorts) or 9–11 min (long-form).
2. Every text box, measured with the real font metrics, inside the safe box.
3. Headline fits in 2 lines at ≥ 64 px.
4. Split layout: face inside the safe box on 3 sampled frames.
5. Integrated loudness within ±2 LU of target; no black or frozen segment > 1 s (FFmpeg `blackdetect`, `freezedetect`).
6. Every caption block on screen ≥ 0.4 s.
7. Headline and caption pass the blocklist.
8. Save a 1.0 s still with TikTok and Shorts UI overlays drawn on top to `qa/` (used in the daily summary and the Look editor).

## 12. Publishing

### 12.1 Posting service

> **Changed 2026-11-19 (owner's decision):** ClipVault posts through the platforms' own official
> APIs (`clipvault/posting/`, built in 0.6.0), not through Upload-Post or Blotato. Unaudited
> apps post privately, so public posting starts when TikTok's Content Posting audit and
> Google's verification pass (`docs/POSTING_AUDITS.md`). The Upload-Post notes below are kept
> for reference only.

- Upload-Post (primary). Docs: https://docs.upload-post.com. Upload endpoint `POST https://api.upload-post.com/api/upload` with `platform[]=tiktok` / `youtube`.
- Accounts are connected through Upload-Post's hosted connect link (`POST /api/uploadposts/users/generate-jwt`), so the app never handles TikTok or YouTube passwords.
- TikTok: before each post, read the account's allowed privacy levels (`GET /api/uploadposts/tiktok/settings` → `privacy_level_options`) and send one of them. Use direct-post mode, not draft/inbox mode (drafts need a manual publish in the app). Set the commercial-content/disclosure flags for campaign clips. Check the docs for exact field names.
- First real post of each account must be confirmed by the user (hard rule 11). Do a public test post and confirm it is visible to a logged-out viewer; unaudited apps silently post private.
- Blotato adapter implements the same interface (`publish(render, account) -> PostResult`) as a drop-in backup.

### 12.2 Limits and spacing

- TikTok allows about 15 API posts per creator account per day and 6 requests per minute. Schedule at most 10 per account per day, 90–120 minutes apart, ±10 minutes random jitter, inside configurable active hours (default 08:00–23:00).
- YouTube: uploads have their own quota bucket (about 100 per project per day); not a constraint at this volume.

### 12.3 Review mode

- Setting "Let me approve posts first" (default on until the user turns it off).
- When on: when a render is scheduled, send an ntfy notification with the still, headline and Approve / Skip action buttons. No answer within 2 hours → it posts anyway. Skip → `skipped`.

### 12.4 After posting

- Poll until the service reports the live URL; store `post_url`.
- Campaign clips: submit the URL to the campaign within 30 minutes. Try Whop's API (`POST /bounty_submissions` with a user credential; docs: https://docs.whop.com/developer/bounties/run-a-bounty-program.md). If Content Rewards campaigns don't accept API submissions, send an ntfy notification with the post URL and a one-tap link to the campaign's submit page instead. Record which path works in PROGRESS.md.
- Stats at 1 h, 24 h and 7 days feed the score and a simple per-account report of which headline styles and layouts get more views.

### 12.5 Auto-pause

- Any policy error, strike, or "account restricted" response pauses that account and raises an alert.
- A global pause flag stops all publishing (Pause button on Home).

## 13. Library and retention

```
<library_root>/
  <streamer_login>/
    YYYY-MM/
      YYYY-MM-DD_<views>views_<slug-of-title>_<clipid>.mp4
      ...same name....json      # sidecar: all clips-table fields + source URL + downloaded_via
      ...same name....srt
      vertical/...same name..._9x16.mp4
  archive.db (or the app data dir)
```

- Quality setting: 1080p (about 25 MB per 30 s clip) or 720p (about 10 MB); these are estimates to replace with measured values.
- Retention defaults: starred clips and anything in `Favorites/` kept forever; after 60 days delete clips below that streamer's median views; never delete posted renders' sources until 30 days after posting.
- Disk guard: if free space < 15%, pause downloads and alert. If an external drive is the library root and is missing, download to a local cache and move files when it returns.

## 14. Notifications (ntfy)

| Kind | Trigger | Tap opens |
| --- | --- | --- |
| summary | daily 09:00 | Home |
| approve | review mode, new scheduled post | Approve / Skip actions |
| relogin | 401/expired connection on Twitch, Upload-Post, TikTok, YouTube | Settings → Accounts → Reconnect |
| storage | free space < 15% | Settings → Storage with "Free up space" |
| broken | same step failed 3 times | Alerts |
| campaign | new matching campaign (optional) | campaign URL |

Topic name is random and stored in keyring. Quiet hours configurable (default 23:00–08:00, approvals excepted when review mode is on).

## 15. Control panel (FastAPI + HTMX)

Bottom tabs: Home, Clips, Streamers, Queue, Settings. Must work well on a phone (375 px wide) first.

| Screen | Shows | Actions |
| --- | --- | --- |
| Home | Status card (green/yellow/red; red when alerts are open, tap → Alerts), today's saved/posted/views, next post, newest clips | Pause/resume all; skip or edit next post |
| Clips | Thumbnail grid; filters: streamer, date, views, source; search transcripts | Play, star, download, delete, make vertical copy |
| Streamers | List with avatar, live status, Save/Post switches, permission badge | Add/remove, per-streamer settings, mark permission, authorize-with-Twitch link |
| Queue | Upcoming posts with video, headline, time; past posts with views | Edit headline, skip, post now |
| Settings | Storage, schedule, accounts, notifications, Look editor (live preview of the three layouts on a recent clip), updates | All configuration |
| Alerts | Open alerts, newest first | Fix button per alert |

Setup wizard on first launch, one question per screen:
1. Mode: "Save clips to this computer" / "Save and post to TikTok and YouTube"
2. Log in with Twitch
3. Add streamers (search by login, show avatar and live status; Save on by default; Post locked until permission is marked)
4. Storage folder, quality, keep rules, live GB/month estimate
5. Connect TikTok and YouTube (posting mode only): open Upload-Post connect link
6. Look: preview and choose headline style + accent color; review mode switch
7. Phone: QR for ntfy subscription; QR for the control panel URL

Security: bind to 127.0.0.1 by default; optional binding to the Tailscale interface; password set on first open; CSRF protection on forms.

## 16. Packaging

- Laptop: PyInstaller build per OS bundling FFmpeg, yt-dlp and TwitchDownloaderCLI; tray icon (green running / yellow paused / red needs you) with "Open control panel", "Pause", "Quit"; start at login (optional, on by default).
- Server: Docker image; `docker compose up -d` with a volume for library and database.
- Laptop ↔ server: the server's library syncs to the laptop with Syncthing (document setup; don't reimplement sync).
- Update check on start against GitHub releases; one-click update on desktop.

## 17. Configuration file (non-secret)

```yaml
mode: archive            # archive | archive_post
timezone: America/Chicago
library_root: ~/Twitch Archive
quality: 720p            # 720p | 1080p
discover_interval_min: 30
heavy_jobs_on_battery: false
retention:
  keep_days_below_median: 60
  min_free_space_pct: 15
posting:
  review_mode: true
  max_per_account_per_day: 10
  spacing_min: [90, 120]
  jitter_min: 10
  active_hours: ["08:00", "23:00"]
look:
  template: default
  accent: "#FFD400"
  headline_style: box     # box | outline
notifications:
  quiet_hours: ["23:00", "08:00"]
```

Secrets in keyring: Twitch client id/secret and tokens, Groq key, LLM key, Upload-Post key, ntfy topic, control-panel password hash.

## 18. Open questions to settle while building

- Do Twitch Auto Clips appear in Get Clips results? (M1)
- Does the Upload-Post account post publicly to TikTok from the first call? (M9)
- Can Content Rewards submissions be made through Whop's API? (M10)
- Are the safe-zone numbers right on a real phone for both apps? (M7)
- Measured file sizes and render times on the target machine (M2, M7)

## 19. Policy guardrails (summary)

- Reposting someone's stream content needs their permission; the posting gate enforces it.
- TikTok Creator Rewards and YouTube's reused-content rules generally don't pay for raw reposts; income comes from campaigns. Don't build features that disguise reposts as original content.
- Paid clips need disclosure at the start.
- Twitch's terms restrict downloading and automated extraction except where Twitch permits it; unofficial downloads are for the private archive only.
