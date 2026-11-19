# ClipVault Web: plan and feature list (draft for review)

Status: **plan only, no code yet.** Written 2026-11-19 for the owner to review.
**Decided 2026-11-19:** the owner chose **path B**: the website downloads any public
Twitch link itself (section 5), with the safeguards listed there. The other decisions in
section 9 have proposed defaults; nothing paid is bought without the owner's OK.

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
| Twitch **clip** link | That clip becomes 1 edited clip, plus up to 4 more of the streamer's top clips from the same week ("more like this"). Works for any streamer. |
| Twitch **VOD** link | The best 5 moments of that stream, found from chat activity and audio loudness (the app's `vod_spikes` scoring). Only those moments are downloaded, never the whole stream. |
| Twitch **channel** link, or a **streamer name** | Search (official Twitch API), pick the streamer, and get their top 5 clips of the last 7 days as finished vertical clips. A LIVE badge shows when they're live, with "Clip live moments automatically → get the app". |
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
7. **"Open in the app"** (later, W5): if the app is installed, the streamer can be
   added to it in one click (a `clipvault://add?streamer=xqc` link the app registers).

Measured per nudge: shown, clicked, installer downloaded (see 7.4).

## 5. Twitch downloads: path B (decided 2026-11-19)

**The decision.** The website downloads any public Twitch clip, or moments of a public VOD,
on its own server with yt-dlp, for whoever pastes the link. The owner chose this over
"official and owned sources only" (path A) and "Twitch clips only" (path C) on 2026-11-19.

**What it changes.** Hard rule 3 allowed unofficial downloads only for the private archive.
It now also allows them for the web version's one-off runs, with the safeguards below
(CLAUDE.md is updated to match). SPEC.md notes that "Twitch's terms restrict downloading
and automated extraction except where Twitch permits it". This path accepts that risk. The
risk is a takedown, or Twitch blocking the website's API key or its server. The
safeguards keep that risk small and make sure it can never reach the desktop app.

**Safeguards (all required before launch):**

1. **Its own Twitch key.** The website uses a separate Twitch developer app (its own
   client id and secret). The desktop app's keys are never used on the server, so if
   Twitch blocks the website, the app keeps working. The owner creates that Twitch app.
2. **Only the moments.** Clip files, or `--download-sections` cuts of a VOD (at most
   5 × 60 s per run), never a whole stream, and nothing behind a login: no cookies, no
   subscriber-only or deleted videos.
3. **Private and short-lived.** Results are reachable only through the requester's
   unguessable link, marked `noindex`, never listed in a public gallery, and deleted
   after 24 h. Source downloads are deleted as soon as the clips are made.
4. **Streamer opt-out.** A page where a streamer signs in with Twitch and blocks their
   channel; blocked channels are refused at the input box, with a polite message. Opt-outs
   are honoured immediately and kept forever.
5. **Takedowns.** A takedown/DMCA contact on every result page and in the terms, answered
   within 24 h, with a log of requests and a repeat-abuser block.
6. **Kill switch.** One server setting turns server-side Twitch downloads off at once.
   Twitch links then fall back to path A: the top 5 moments as Twitch embeds, plus "make
   these in the free app". It's for a warning from Twitch, a takedown wave, or a broken
   downloader.
7. **Check the rules before building it.** Before the Twitch milestone (W3), read Twitch's
   current Terms of Service and Developer Agreement and the yt-dlp project notes, and
   write down what they say in PROGRESS.md (hard rule 10). Legal advice before the public
   launch is strongly recommended.
8. **No posting, ever.** The website never posts anything anywhere. Files are only
   downloaded by the person who asked (hard rule 1 still holds for the app).
9. **Keep the downloader healthy.** yt-dlp is updated daily on the server. Repeated
   failures raise an alert and switch to the fallback above, instead of failing every run.

**Why this still sells the app:** the website gives 5 clips from one link; the app
watches streamers all day, catches live moments and keeps every clip. The nudges in
section 4 do that job.

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
3. **Fetch:** yt-dlp on the server, with the website's own Twitch key for lookups, and
   only the moments, never the whole VOD (as in the app: `--download-sections`). The
   opt-out list is checked first, and the kill switch is honoured (section 5).
4. **Subtitles:** Groq Whisper on each moment's audio only, so 5 × about 45 s, not the
   whole stream.
5. **Phone versions:** the app's `render/` pipeline and QA, run unchanged. 5 clips run side
   by side on one worker.
6. **Done:** results are stored with a 24 h expiry and the cards flip to finished.

Each step records its state, so a worker crash resumes the run instead of starting over
(hard rule 7), as in the app.

**Expected time per run:** 2–4 minutes (fetching 30 s, subtitles 15 s, rendering about
1–2 min on 8 cores). It needs to be measured in W1.

## 7. Technical choices

### 7.1 Reuse, don't fork
The web worker imports the existing `clipvault` package: `render/`, `process/headlines.py`,
`process/transcribe.py`, `process/hook_emoji.py`, `discover/vod_spikes.py`, `twitch/api.py`,
`fetch/ytdlp.py`.
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
| The website's own Twitch developer app | lookups and search (section 5) | free |

The biggest cost is **CPU time for rendering**. One 8-core server should manage roughly
20–40 runs an hour. That needs measuring in W1 before any promotion.

## 9. Decisions

1. **Which Twitch path:** **decided: B** (2026-11-19), with the section 5 safeguards.
2. **Monthly budget**, and approval of each paid service in section 8. *Still needed
   before anything is bought or deployed.* Building and testing on this computer needs no
   paid service.
3. **Where it lives:** proposed: a `/try` page on the current download site, with the
   workers on one small server.
4. **Watermark:** proposed: a small "Made with ClipVault" in a corner, inside the safe
   box.
5. **Free limits:** proposed: 3 runs a day without an account, 5 with Twitch sign-in,
   about 5 clips per run.
6. **Accounts:** proposed: none needed; optional Twitch sign-in.

Items 3–6 go ahead with the proposals unless the owner says otherwise.

## 10. Build order (after the decisions)

Same style as BUILD_PLAN.md: one milestone at a time, each with "Done when" checks.

**W1. Upload → 5 clips (the core, no Twitch).** ✅ built 2026-11-19 (`clipvault_web/`, local only)
Upload, moment finder for videos without chat, render 5, results page, 24 h expiry.
Done when: a 20-minute upload gives 5 clips that pass QA in under 5 minutes on the
server; files are gone after 24 h; tests use fixtures only.

**W2. Live progress and nudges.** ✅ built 2026-11-19 (local only)
Loading cards, the working-screen card, the "+N more moments" card, the daily-limit screen,
analytics events.
Done when: every screen shows at most one nudge, and events arrive for a test run.

**W3. Twitch links and streamer search (path B).** ✅ built 2026-11-19 (local only; server downloads of other channels OFF by default, see PROGRESS.md rules check)
Starts with the rules check in section 5 (item 7), written down in PROGRESS.md. Then:
- Search Channels, and clip, VOD and channel links, all with the website's own Twitch key;
- server downloads of only the moments (yt-dlp, with daily updates);
- the streamer opt-out page, the takedown contact and the kill switch with its path A
  fallback;
- Sign in with Twitch → "My channel".

Done when:
- pasting any public Twitch clip, VOD or channel link gives about 5 finished clips;
- an opted-out channel is refused;
- with the kill switch on, the same link shows embeds and "make them in the app", and
  nothing is downloaded (tests check all three);
- no request ever uses the desktop app's keys.

**W4. Hardening and launch.**
CAPTCHA, limits, queue cap, takedown link, privacy and terms text, load test, a
"busy" page.
Done when: a 50-run load test finishes without errors, and limits and the busy page work.

**W5 (later). "Open in the app"** links that add a streamer to the installed app, and a
"made N clips on the web" welcome in the app.

## 11. Risks

- **Twitch terms and API access** (section 5): the main risk, accepted with path B. Its
  own Twitch key keeps the desktop app safe. The opt-out, takedowns and 24 h deletion keep
  complaints small, and the kill switch turns server downloads off in one step if Twitch
  objects.
- **The downloader breaking:** Twitch changes often. Daily yt-dlp updates, plus the
  fallback to embeds.
- **Cost from popularity:** rendering is CPU-heavy. The daily limits, queue cap and busy page
  keep the bill flat.
- **Upload moment-finding quality:** without chat, picking the 5 best moments is harder.
  W1 needs real test videos and an honest check before launch.
- **Cannibalising the app:** low. Live clipping, the library, the folders and posting stay
  app-only.
- **Abuse:** uploads of content the user doesn't own. The rights checkbox, takedown link,
  24 h deletion and per-IP limits handle it.
