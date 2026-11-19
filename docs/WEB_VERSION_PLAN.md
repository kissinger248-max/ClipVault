# ClipVault Web: plan and feature list (draft for review)

Status: **plan only, no code yet.** Written 2026-11-19 for the owner to review. Nothing
here is built until the decisions in section 9 are made.

## 1. What it is

A free, cut-down ClipVault that runs in the browser. Someone pastes a Twitch link, searches
for a streamer or drops in a video, and a few minutes later gets **about 5 finished
vertical clips**: 9:16, hook headline, word-by-word captions, ready to download.

Its job is to be a **gateway to the desktop app**. Each screen shows what the app does on
top: it watches streamers 24/7, clips live moments, keeps a searchable library and fills
the "Edited clips" folders. The web version is the free taste; the app is the real thing.

**Success looks like:** a first-time visitor gets 5 good clips in under 5 minutes without
signing up, and a meaningful share of them click "Download the app".

## 2. Who it's for

- **Clippers** who post other streamers' moments on TikTok and Shorts, trying a tool before
  installing anything.
- **Streamers** who want vertical clips of their own streams.
- **Anyone with a video** (a podcast, a YouTube upload, a recording) who wants short clips.

## 3. Feature list

### 3.1 Ways in (one smart box)

One input on the home page, "Paste a Twitch link, search a streamer, or drop a video". It
works out what was typed:

| Input | What happens |
|---|---|
| Twitch **clip** link | That clip becomes 1 edited clip, plus up to 4 more of the streamer's top clips from the same week ("more like this"). |
| Twitch **VOD** link | The best 5 moments of that stream, found from chat activity and audio loudness (the app's `vod_spikes` scoring). |
| Twitch **channel** link, or a **streamer name** | Search (official Twitch API), pick the streamer, and get their top 5 moments of the last 7 days. A LIVE badge shows when they're live, with "Clip live moments automatically → get the app". |
| **Uploaded video** (mp4/mov/mkv/webm, up to 60 min / 2 GB) | The best 5 moments, found from audio loudness, laughter and what is said (the transcript). |
| **Sign in with Twitch** (optional) | "My channel": your own recent streams and clips, downloaded the official way. |

### 3.2 Making the clips

- About **5 clips per run**, each **15–59 s** (hard rule 4), 720p.
- The app's **hook headline** system (`process/headlines.py`) and **word-by-word captions**,
  all text inside the safe box (hard rule 5).
- The app's **automatic layout**: face cam on top when one is found (YuNet), letterbox
  otherwise.
- **Live progress**: the same loading cards as the app's live view (Found → Saved →
  Subtitles → Phone version), each turning into the finished clip when it's ready.

### 3.3 Results

- 5 phone-sized previews that play in place.
- **Download** each clip, or **Download all** as a zip.
- **Change the headline**: pick from the other suggested headlines or type one; only that
  clip is made again (1 free change per clip).
- Links expire after **24 hours**; files are deleted then.

### 3.4 Deliberately left out (app only)

| | Web (free) | Desktop app |
|---|---|---|
| Clips per run | about 5 | every moment found |
| Watching streamers | no, one-off runs | 24/7, catches up after sleep |
| Live moments | no | clips chat spikes while they're live |
| Library | no, files gone after 24 h | searchable by what was said |
| Edited clips folders | no | per streamer and all streamers |
| Layouts and styles | automatic only | letterbox, split, native; headline styles and colours |
| Quality | 720p | up to 1080p |
| Watermark | small "Made with ClipVault" (decision 9.4) | none |
| Posting to TikTok, Shorts, Instagram | no | yes, for streamers who gave permission |
| Limits | 3 runs a day | none |

## 4. Nudges toward the app

Rules: **one nudge per screen**, always honest, never blocking a download that's ready.

1. **Home page:** under the box, "Want this for every stream, automatically? Get the free
   app →".
2. **While it works** (the 2–4 minute wait is the best moment): a card beside the loading
   cards. "The app does this on its own for every streamer you follow, even while you
   sleep." with a Download button for their OS (Windows or Mac, detected).
3. **Results:** a 6th card in the grid, the same size as a clip: "+14 more moments found in
   this stream. The app makes all of them." The number is real: the scoring finds more
   than 5, and the web version only makes the top 5.
4. **Streamer is live:** "xQc is live right now. The app clips live moments as they happen."
5. **Daily limit reached:** "That's today's 3 free runs. The app has no limits." plus when
   the limit resets.
6. **After a download:** a small toast. "Tip: the app saves every clip into a folder per
   streamer."
7. **"Open in the app"** (later, phase 4): if the app is installed, the streamer can be
   added to it in one click (a `clipvault://add?streamer=xqc` link the app registers).

Measured per nudge: shown, clicked, installer downloaded (see 7.4).

## 5. Twitch rules: what the web version may download

This decides the whole design. The desktop app's hard rule 3 says unofficial downloaders
(yt-dlp) are **only for the private archive** on the user's own computer, and SPEC.md notes
that "Twitch's terms restrict downloading and automated extraction except where Twitch
permits it". A website that downloads any streamer's video for anyone who asks is not a
private archive. It is the case those terms are written for, and it is a much bigger target
for takedowns and for Twitch revoking the API key.

Three ways to handle it:

**A. Official and owned sources only (recommended for launch)**
- **Generate** from: uploaded videos (the uploader confirms they have the rights), the
  signed-in streamer's own channel (official Get Clips Download), and streamers who
  authorized ClipVault on Twitch.
- **Find but don't download** for everyone else: search and paste-a-link still work.
  Discovery uses the official Helix API, and the top 5 moments show as Twitch embeds with
  their suggested headlines. Then: "Make these 5 into vertical clips in the free app." The
  app downloads them on the user's own computer, which is the private-archive case the
  app already handles.
- Upside: no legal grey zone, the API key is safe, and the strongest nudge to the app is
  built in.
- Downside: a clipper pasting someone else's link doesn't get finished files in the
  browser.

**B. Download any public Twitch link on the server (what the request describes literally)**
- Highest wow factor and conversion.
- Goes against hard rule 3 and the SPEC note above, so it needs the owner's explicit
  decision (hard rule 11: changing a hard rule).
- If chosen: a separate Twitch app and API key for the website, so a ban can't take the
  desktop app down with it; files kept 24 h at most and only for the requester; a public
  streamer opt-out list; a DMCA contact and takedown process; and a check of Twitch's
  current Developer Agreement first (hard rule 10). Getting legal advice is worth it.

**C. Middle ground:** like A, plus server downloads of **Twitch clips only** (short,
already public, made by viewers), never VODs, with the same safeguards as B. It is still an
unofficial download, so the same decision applies.

The rest of this plan works with A, and B or C can be added later as a switch, without a
redesign.

## 6. How a run works

```
browser ──► web API ──► job queue ──► worker (FFmpeg, the clipvault package)
   ▲            │                        │
   │            ▼                        ▼
   └── progress (polling, like /api/activity) ◄── job state ── file storage (24 h)
```

1. **Start:** the API checks the input, the daily limit and a CAPTCHA (Cloudflare
   Turnstile, free), creates a job and returns its id.
2. **Find moments:**
   - Clips and channels: Helix Get Clips, top by views.
   - VODs: chat replay (TwitchDownloaderCLI) plus loudness, scored by `vod_spikes`.
   - Uploads: loudness, laughter (the audio tagger) and transcript keywords, plus an
     optional AI pick of the 5 best stretches. **New code**: the app always has chat or
     view counts; an upload has neither.
3. **Fetch:** only the moments, never the whole VOD (as in the app: `--download-sections`).
4. **Subtitles:** Groq Whisper on each moment's audio only, so 5 × about 45 s, not the
   whole stream.
5. **Phone versions:** the app's `render/` pipeline and QA, run unchanged. 5 clips run side
   by side on one worker.
6. **Done:** results are stored with a 24 h expiry and the cards flip to finished.

Each step records its state, so a worker crash resumes the run instead of starting over
(hard rule 7), as in the app.

**Expected time per run:** 2–4 minutes (fetching 30 s, subtitles 15 s, rendering about
1–2 min on 8 cores). It needs to be measured in phase 1.

## 7. Technical choices

### 7.1 Reuse, don't fork
The web worker imports the existing `clipvault` package: `render/`, `process/headlines.py`,
`process/transcribe.py`, `process/hook_emoji.py`, `discover/vod_spikes.py`, `twitch/api.py`.
New code lives in a separate `web/` service folder, so the desktop app never ships it, and
fixes to the pipeline reach both automatically.

### 7.2 Hosting
- **Front end:** the existing static site on Vercel (`site/`) gets a `/try` page. It stays
  plain HTML with a little JavaScript, like the panel, with no front-end build.
- **API and workers:** Vercel can't run FFmpeg jobs that take minutes, so these go on one
  small Linux server using the Docker image the project already has. Start with a single
  8-core machine that runs the API, queue and 2 workers; add workers when it's busy.
- **Queue and state:** SQLite (WAL) on that server at first, the same as the app. Move to
  Postgres only when there's more than one server.
- **Files:** S3-compatible object storage with no download (egress) fees, so popular clips
  don't cost bandwidth. Files expire after 24 h.

### 7.3 Accounts
- **No account** for the first run: limited by IP plus CAPTCHA.
- **Sign in with Twitch** (optional) for "My channel" and a higher limit. It is the same
  OAuth the app already has.
- No passwords stored and no email list at launch.

### 7.4 Measuring (privacy-friendly)
- Cookie-free analytics (page views and the events below), no ad trackers.
- Events: run started (by input type), run finished, clip downloaded, nudge shown or
  clicked, installer download started.
- `privacy.html` and `terms.html` on the site get a web-version section (uploads, 24 h
  deletion, takedown contact).

### 7.5 Safety
- Uploads: size and length checked before they're stored, then probed with `ffprobe`. Only
  FFmpeg and ffprobe ever touch them, inside a locked-down container without network
  access.
- Limits: 3 runs a day without an account, 5 with Twitch sign-in, 1 run at a time, and a
  queue cap (the "busy right now, try in a few minutes" page instead of slowing everyone
  down).
- A report or takedown link on every result page, honoured within 24 h.
- Secrets: API keys live in the server's secret store or environment, injected at start,
  never in git (hard rule 6's intent; the OS keychain is desktop-only).

## 8. Costs (rough; to confirm before buying anything)

Each of these is a paid service, so **each needs the owner's OK first** (hard rule 11).
Prices change, so check them before deciding.

| Item | What for | Rough size |
|---|---|---|
| Linux server, 8 cores | API, queue, FFmpeg workers | a modest monthly fee for one small cloud server |
| Object storage | 24 h result files | small: about 75 MB per run, deleted daily |
| Groq Whisper | subtitles | very small: about 4 minutes of audio per run |
| AI headlines (optional) | better hooks | small; the built-in writer works without it |
| Domain (optional) | a nicer address than the Vercel one | yearly |
| Cloudflare Turnstile, analytics | abuse protection, measuring | free tiers |

The biggest cost is **CPU time for rendering**. One 8-core server should manage roughly
20–40 runs an hour. That needs measuring in phase 1 before any promotion.

## 9. Decisions needed from the owner

1. **Which Twitch path:** A (recommended), B or C from section 5.
2. **Monthly budget**, and approval of each paid service in section 8.
3. **Where it lives:** a `/try` page on the current download site, or its own domain.
4. **Watermark:** a small "Made with ClipVault" in the corner (inside the safe box), or
   none.
5. **Free limits:** 3 runs a day and 5 clips per run, or other numbers.
6. **Accounts:** none, Twitch sign-in only (recommended), or email too.

## 10. Build order (after the decisions)

Same style as BUILD_PLAN.md: one milestone at a time, each with "Done when" checks.

**W1. Upload → 5 clips (the core, no Twitch).**
Upload, moment finder for videos without chat, render 5, results page, 24 h expiry.
Done when: a 20-minute upload gives 5 clips that pass QA in under 5 minutes on the
server; files are gone after 24 h; tests use fixtures only.

**W2. Live progress and nudges.**
Loading cards, the working-screen card, the "+N more moments" card, the daily-limit screen,
analytics events.
Done when: every screen shows at most one nudge, and events arrive for a test run.

**W3. Streamer search and links (path A).**
Search Channels, paste clip, VOD and channel links, top 5 moments as embeds, "make them in
the app", and Sign in with Twitch → "My channel" with official downloads.
Done when: pasting any Twitch link gives the right top 5; your own channel gives finished
clips; no unofficial download runs on the server (a test checks this).

**W4. Hardening and launch.**
CAPTCHA, limits, queue cap, takedown link, privacy and terms text, load test, a
"busy" page.
Done when: a 50-run load test finishes without errors, and limits and the busy page work.

**W5 (only if B or C is chosen). Server-side Twitch downloads** behind a switch, with the
separate API key, opt-out list and takedown process.

**W6 (later). "Open in the app"** links that add a streamer to the installed app, and a
"made N clips on the web" welcome in the app.

## 11. Risks

- **Twitch terms and API access** (section 5): the main risk. Path A avoids it.
- **Cost from popularity:** rendering is CPU-heavy. The daily limits, queue cap and busy page
  keep the bill flat.
- **Upload moment-finding quality:** without chat, picking the 5 best moments is harder.
  W1 needs real test videos and an honest check before launch.
- **Cannibalising the app:** low. Live clipping, the library, the folders and posting stay
  app-only.
- **Abuse:** uploads of content the user doesn't own. The rights checkbox, takedown link,
  24 h deletion and per-IP limits handle it.
