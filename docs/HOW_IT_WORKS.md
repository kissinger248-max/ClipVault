# How ClipVault works

A plain-language description of every part of ClipVault, what each part does, when it runs, and how the parts work together. There is no code in this document. For the exact technical rules see `SPEC.md`; for what has been built and checked see `PROGRESS.md`.

Last updated: 13 November 2026.

---

## 1. What ClipVault is

ClipVault is a program that runs on your computer and quietly builds a private library of the best moments from the Twitch streamers you choose. For every moment it saves, it also makes a **phone version**: a vertical video with a headline at the top and big word-by-word captions, ready to watch or save on a phone.

It finds moments in three ways:

1. **Viewer clips:** the clips that viewers made on Twitch, most-viewed first.
2. **Moments nobody clipped:** after a stream ends, it reads the stream's chat and finds the places where chat suddenly went wild.
3. **Live moments:** while a streamer is live, it reads chat as it happens and saves the moments a few minutes later.

You look at everything through the **control panel**, a website that only runs on your own computer (and, if you allow it, your phone on the same Wi-Fi).

ClipVault does **not** post anything anywhere. Posting to TikTok or YouTube was planned but you chose not to have it, so it is not built.

---

## 2. The life of a clip, start to finish

Every moment ClipVault saves goes through the same steps. Each step is done by a different part of the program, and the database (see section 15) remembers which step each clip has reached, so nothing is done twice and an interruption is never a problem.

1. **Found.** One of the three finders (viewer clips, finished-stream chat, live chat) notices a moment and writes it down as "found". Nothing is downloaded yet.
2. **Checked for repeats.** If two clips show the same moment of the same stream (they overlap by more than half), only the one with more views is kept. The other is marked as a repeat and never downloaded.
3. **Saved.** The video is downloaded into your library folder with a clear file name. Moments from chat are cut out of the stream recording (40 seconds) instead of downloading a whole stream. A small information file is saved next to each video.
4. **Transcribed.** ClipVault listens to the clip and writes down every word with its exact timing. This produces a subtitle file and makes the clip searchable by what was said.
5. **Edited for phones.** ClipVault makes the vertical phone version: picks the best layout, adds the headline and the captions, evens out the sound, and runs quality checks.
6. **On the dashboard.** As soon as the phone version exists, the clip appears on the control panel's Home and Clips screens.

Later, the daily cleanup may remove old, less popular clips to save space (section 11). Starred clips are never removed.

If a step fails (for example, the internet drops during a download), ClipVault tries again after 1 minute, then 5, then 30. After that the clip is marked as failed for good and counted in the daily summary, and the rest carries on.

---

## 3. Your streamer list

**What it is:** the Twitch channels ClipVault watches. Right now: Valkyrae, xQc and Kai Cenat.

**What you can set per streamer** (in the Streamers screen, or in a `streamers.txt` file):

- **Save their clips** on or off. Off means ClipVault stops looking for new clips but keeps the ones it has.
- **Best clips per day:** how many of each day's most-viewed clips to keep (default 5).
- **Or every clip with at least N views:** an alternative to "best per day".
- **Chat moments per stream:** how many "moments nobody clipped" to keep from each finished stream (default 3; 0 turns it off).
- **Phone layout:** Automatic, Whole picture, Facecam on top, or Twitch's own vertical version (section 9).

**How it connects:** every finder only looks at streamers with saving switched on. ClipVault also looks each streamer up on Twitch once a week to get their name and picture, and it follows them if they change their Twitch name.

Removing a streamer who has saved clips only switches saving off, so their clips stay. A streamer with no clips is removed completely.

---

## 4. Finding viewer clips

**What it does:** asks Twitch's official API for each streamer's clips, one day at a time, and keeps the most-viewed ones (or every clip above your view limit).

**When:** every 30 minutes while ClipVault runs (you can change this in Settings), right when it starts, and right after the computer wakes from sleep.

**How it remembers where it got to:** for each streamer it stores the last time it checked. The next check starts a day before that point (clips keep getting views, so recent days are looked at again) and continues to now. The very first check looks back 7 days. This is also how it catches up after the laptop was asleep or switched off.

**Feeds into:** "found" clips for the download step.

---

## 5. Moments nobody clipped (finished streams)

**What it does:** after a stream ends, ClipVault downloads its chat replay, finds where chat exploded, and saves the best of those moments that no viewer turned into a clip.

**When:** every hour while ClipVault runs, up to 3 finished streams per hour. Only streams from the last 7 days are looked at.

**How a "moment" is decided:**

- Chat is counted in 10-second slices. A message counts 1, and a message with a laughing or hype emote (KEKW, LUL, Pog, W, LMAO and similar) counts 3.
- A slice is a spike when it is far above the normal level of the 10 minutes before it (well beyond the usual ups and downs, and at least double the average), and busy enough to matter.
- Chat reacts a few seconds after something happens, so each moment is cut from 30 seconds before the peak to 10 seconds after it (40 seconds). Peaks close together are joined into one moment.
- Moments that a viewer already clipped (even a clip with only a few views) are skipped.
- The ones kept are those that jumped highest above the normal level for that part of the stream. Ranking by plain message count would only ever pick the busy first hour of a stream.

**The chat tool:** reading a stream's chat uses a free program called TwitchDownloaderCLI, installed in the project's `bin` folder. Long streams (some run for two days) are read in 4-hour pieces so nothing gets too big, and an interrupted read continues where it stopped. The chat is kept, compressed, in the library next to the streamer's clips.

**What can go wrong:** streams whose recording is subscriber-only or switched off are skipped, with the reason noted. Twitch mutes copyrighted music in recordings; a moment with no sound is still kept but marked as muted.

**Feeds into:** "found" moments, which the download step cuts out of the recording.

---

## 6. Live moments (during a stream)

**What it does:** while one of your streamers is live, ClipVault reads their chat as it happens, using the same "chat went wild" rules as above. A moment is saved when chat gets at least **4 times busier than its usual level** (the last 10 minutes), and at most **10 per stream**. Big chats spike every few minutes; the bar keeps the real moments (one 9.5-hour stream had 67 spikes, 14 of them this strong). When a moment is over, ClipVault waits 3 minutes (so Twitch's recording has caught up) and then cuts those 40 seconds out of the recording right away, while the stream is still going. Cutting is a short download, so it happens on battery too. Moments caught live go first in the queue for downloading and subtitles.

**On or off:** Settings, Twitch, "Live moments" (on unless you switch it off). Each live moment shows how much busier chat was ("chat 5.1×").

**When:** all the time while ClipVault runs. Every 2 minutes it checks who is live and starts or stops reading their chat.

**What it needs:** your Twitch sign-in, done once (Settings, Twitch, or the `twitch login` command). You type a short code at twitch.tv/activate. The login is used only to read chat; ClipVault never writes in chat. The login renews itself; if Twitch ever stops accepting it, an alert asks you to log in again.

**How it connects:**

- It uses one connection to Twitch's chat service for all live streamers, and closes it when nobody is live.
- Its moments go into the same download step as everything else.
- When the stream ends and the finished-stream finder (section 5) reads its chat later, the moments already caught live count as "already clipped", so nothing is saved twice.
- If a streamer has recordings switched off, the moment is noticed but nothing can be cut; Home says so.
- The Home screen shows each live streamer with a chat meter (section 14).
- Pausing ClipVault also stops reading live chat.

---

## 7. Saving clips (the library folder)

**What it does:** downloads the found clips into your library folder, `Twitch Archive` in your user folder unless you choose another one.

**When:** every 2 minutes, up to 20 clips per round.

**How files are organised:**

- One folder per streamer, then one folder per month.
- File names start with the date, then the view count (or "chat" for moments found from chat), a short version of the title, and the clip's id.
- Next to each video: an information file with everything known about the clip, and later a subtitle file and a word-timing file.
- Phone versions go in a `vertical` folder inside the month folder.
- Chat replays go in a `chat` folder inside the streamer's folder.
- Anything you put in a folder called `Favorites` is never touched by ClipVault.

**Quality:** 720p by default (smaller files) or 1080p.

**Downloader:** viewer clips and cuts from recordings use a widely used free download tool (yt-dlp), which updates itself once a day because Twitch changes often. If downloads fail several times in a row, an alert says the downloader may need attention.

**If the library drive is unplugged:** new clips go into a temporary folder and are moved over when the drive is back. Nothing is marked as missing while it is away.

**If the disk gets too full:** below 15% free space, downloads pause and an alert is raised.

---

## 8. Subtitles and search (transcription)

**What it does:** turns the speech in each clip into text with the exact timing of every word.

**When:** every 2 minutes, up to 5 clips per round, only while the laptop is plugged in (unless you allow it on battery).

**How:** it runs a speech-recognition model on your computer, for free (about 17 seconds per clip). If you ever add a Groq key, it uses Groq's online service first and falls back to the computer's own model when Groq is unavailable.

**Word masking:** slurs from a built-in list (plus any words you add to your own list) are shown as stars in subtitles and captions.

**What it produces:**

- A subtitle file next to the video, usable by any video player.
- A word-timing file, which the phone-version captions are built from.
- A search index: on the Clips screen you can search for what was said. Exact phrases are found first, then clips containing all the words.
- Words per second, used to rank clips with more talking higher.

---

## 9. Phone versions (editing)

**What it does:** makes a vertical 1080×1920 video from each clip, the size and shape TikTok, YouTube Shorts and phone screens use.

**When:** automatically for every new clip once its subtitles are done, newest clips first, 3 per round every 2 minutes, while plugged in. About 15 to 35 seconds each. You can also re-make one by hand on its clip page.

**Layouts:**

- **Facecam on top:** the game in the top half and the streamer's camera, enlarged, underneath. Used automatically when ClipVault has found the streamer's camera and checked that their face is in it in this clip. The camera's position is found once per streamer and re-checked when it stops matching.
- **Whole picture (letterbox):** the full stream picture across the middle, with space above and below for the text. Used automatically when facecam-on-top doesn't fit.
- **Twitch's own vertical version:** Twitch offers a vertical version of every clip (for some streamers a design of their own, for others a crop of the middle of the picture). Used only when you choose it, because on real clips it was often worse: one streamer's design puts their face exactly where the headline goes and burns in their own captions, which then double up with ClipVault's. It is downloaded only when this layout is chosen.

**Choosing a layout:** "Automatic" (the default) picks facecam-on-top or whole picture as above. On the Streamers screen you can set a fixed **Phone layout** per streamer; on a clip's page, under "More", you can make that one clip again in another layout.

**What goes on the video:**

- **Headline** at the top, in a white rounded box (or as outlined text), with one key word in the highlight colour. It fits in two lines; long headlines are shortened.
- **Credit line** "@streamer on Twitch" under the headline: **off by default**, can be switched on in the Look editor.
- **Captions** in the lower middle, two to five words at a time, the word being spoken in the highlight colour and slightly bigger, in sync with the speech. Each group stays on screen long enough to read.
- All text stays inside a "safe area" so it is never hidden behind the buttons TikTok and YouTube Shorts put on screen.

**Sound:** the volume is evened out so every clip plays at the same comfortable loudness.

**Length:** clips longer than 59 seconds keep their last 59 seconds (the payoff is usually at the end).

**Quality checks** after every edit: the length, that all text is inside the safe area, that the headline fits, that the face is in place for the facecam layout, the loudness, no black or frozen stretches, captions on screen long enough, and no blocked words. A picture of the first second, with the TikTok and Shorts buttons drawn on top, is saved in the `qa` folder. A clip that fails a check is still shown on the dashboard; the checks matter most for posting, which is off.

**Look editor** (Settings, "Look of vertical videos", also a step of the setup): headline style, highlight colour, caption size, and the credit line on or off, with live previews of the three layouts drawn on one of your real clips.

---

## 10. Headlines

**Without an AI key (now):** the headline is made from the clip's Twitch title: emoji and symbols removed, at most 7 words, shortened until it fits two lines. Some come out weak ("big", "smoke").

**With an AI key (optional, a few cents a month):** an AI model writes a short hook headline from what was said in the clip, plus a post caption and hashtags. ClipVault checks every answer: at most 6 words, no emoji, nothing invented, no "BREAKING / LEAKED / BANNED" unless the streamer actually said it, no blocked words, and it must fit on screen. A bad answer is asked for again, up to 3 times; if none is usable, the title-based headline is kept. Headlines are written with the `headlines` command for the best clips; the phone version then uses them.

---

## 11. Cleanup, backups and disk space

**When:** once a day at 04:00, or as soon as possible afterwards if the computer was off or asleep.

**What it does:**

1. **Moves** clips from the temporary folder into the library if the library drive was away.
2. **Notices files you deleted by hand:** those clips are marked and never downloaded again. Files that come back are un-marked. If a large share of the library disappears at once, nothing is marked and an alert asks whether the folder was moved.
3. **Retention:** removes viewer clips older than 60 days (Settings, Storage) whose views are below that streamer's usual (median) views, together with all their files: the video, the information file, subtitles, word timings and phone version. Moments found from chat (live or after the stream) have no view count, so this rule leaves them alone. Starred clips and anything in `Favorites` are never removed. Only files ClipVault itself made and recorded are ever deleted, and never anything outside the library folder.
4. **Disk check:** pauses downloads below 15% free space.
5. **Database backup:** copies the database into the library folder (`.clipvault-backups`), one copy per weekday, so there are always seven days of backups and old ones are simply overwritten.

**Space use:** a clip is about 5 MB at 720p, but its phone version is about 36 MB. With the defaults, one streamer adds roughly 10 GB a month, mostly phone versions. Settings shows an estimate for your streamers.

---

## 12. Running unattended (the scheduler)

When you start ClipVault (`run`), a scheduler inside it does everything on its own:

| Task | How often | What it does |
| --- | --- | --- |
| Find new clips | every 30 minutes, at start, and after waking | Section 4 |
| Worker | every 2 minutes | Downloads (section 7), then subtitles (section 8), then phone versions (section 9) |
| Finished-stream chat | every hour | Section 5 |
| Live chat | all the time | Section 6 |
| Notifications | every minute | Sends waiting phone notifications (section 13) |
| Heartbeat | every minute | Starts the daily tasks when they are due, and notices when the computer slept |
| Cleanup | daily at 04:00 | Section 11 |
| Daily summary | daily at 09:00 | Section 13 |

**Catching up after sleep:** if the computer was asleep, the heartbeat notices the gap on waking and starts finding and downloading straight away. Daily tasks that were missed run as soon as possible.

**Battery:** on a laptop running on battery, downloads and cuts continue but the heavy work (subtitles, phone versions) waits for the charger. A setting allows it on battery too; Home offers it as a button when clips are waiting for the charger.

**Changing settings:** most settings count from the next round; only how often to look for clips needs a restart.

**Pause:** the Pause button (Home screen, tray icon, or the `pause` command) stops finding, downloading, editing and live chat. Notifications and the daily summary still go out. Resume carries on where it stopped.

**Only one copy:** ClipVault refuses to start a second time from the same folder, so work is never done twice.

**When something keeps failing:** if the same task fails 3 times in a row, a "something keeps failing" alert is raised; it clears itself when the task works again. Being offline does not count.

---

## 13. Phone notifications

**What it does:** sends messages to your phone through the free ntfy app.

**Setting up:** Settings, "Phone", "Set up notifications" creates a private, random topic name and shows a QR code; scan it in the ntfy app and send a test. Anyone who knows the topic name can read the messages, so keep it private.

**What you get:**

- A **daily summary** at 09:00: clips saved in the last day, library size, clips waiting, clips that failed for good, problems that need you, and whether ClipVault is paused.
- An **alert** when something needs you: a task that keeps failing, a full disk, downloads that stopped working, the library folder gone missing, a Twitch login that needs renewing.

**Quiet hours** (23:00 to 08:00 by default): messages wait until morning, except a disk-full warning and tests.

**Nothing gets lost:** messages wait in an outbox and are sent when the computer is online. Messages older than 2 days are dropped.

---

## 14. The control panel

A website that runs inside ClipVault at http://127.0.0.1:8765. It opens in your browser when ClipVault starts.

**Security:**

- The first time, you choose a password. This first password can only be set on the computer itself.
- Logging in keeps you logged in for 30 days on that device. Changing the password logs out every device.
- Every button and form carries a hidden token, so other websites can't trigger actions.
- Forgot the password? The `panel reset-password` command clears it, and you choose a new one on the computer.

**Setup** (first visit, six steps): what ClipVault should do, connect to Twitch (the app keys), which streamers, where to keep clips (with a monthly space estimate), the look of the phone videos, and your phone (notifications and opening the panel on the phone).

**Home:** a status card (green running, yellow paused, red something needs you; tap it to see the problems), "Right now", today's numbers, the newest edited clips, and Pause / Resume.

**Right now** (Home) shows ClipVault working, updated every few seconds:

- what it is doing ("Making the phone version of xQc's clip", "Looking for new clips…", "All caught up · next look in 12 min");
- each streamer who is live, with a chat meter: the last five minutes of chat in 10-second bars, a line for its usual level and a red line for the "4× busier" bar a moment needs, and how many moments were caught;
- a short list of what just happened (clips found, a live moment caught, chat spiking, a phone version ready).

**Clips on their way** appear as loading cards at the front of Newest clips (and on the Clips screen): a dark tile with a moving shine, its stage (downloading, cutting, writing subtitles, making the phone version, waiting for the stream to catch up with a countdown, waiting for the charger) and a four-part bar (found, saved, subtitles, phone version). When the phone version is ready, the card turns into the finished clip. The numbers on Home count up as it happens.

**Clips:** the edited phone versions as tall cards, newest first. Filter by streamer, by kind (viewer clips, or chat and live moments), by order (newest, most viewed, oldest), by minimum views, or starred only. Search finds clips by what was said, with the matching words highlighted. Clips on their way show as loading cards at the top.

**A clip's page:** the phone version plays first. Buttons: **Save video** (downloads the phone version), **See original** (the raw Twitch video, with its own download), and **Star** (starred clips are never deleted). Under "More": make the phone version again in another layout, open it on Twitch, or delete it (deleted clips are never downloaded again). The transcript is under "What was said".

**Streamers:** each streamer with their Twitch picture, a LIVE badge when they are live, how many clips are saved, and their settings (section 3). Add a streamer by name or channel link, or remove one.

**Settings:** storage (folder, quality, how long to keep less popular clips, the free-space limit, the monthly estimate), schedule (how often to look for clips, time zone, subtitles on or off, heavy work on battery), the look of the phone videos (section 9), Twitch app keys (stored securely, never shown again), live chat login (section 6), phone (notifications, quiet hours, summary time, and opening the panel on your phone), and the panel password.

**Alerts:** the open problems, newest first, each with a Fix link where there is one and a Dismiss button. A dismissed problem comes back if it happens again.

**On your phone:** Settings, "Allow phones on my Wi-Fi", then restart ClipVault and scan the QR code shown. The password still protects it. On the phone use "Add to Home Screen" and it behaves like an app.

---

## 15. Where everything is kept

| What | Where |
| --- | --- |
| Clips, phone versions, subtitles, chat replays | The library folder (`Twitch Archive` by default) |
| Database backups | `.clipvault-backups` inside the library folder |
| The database (the memory of every clip, streamer, step and setting) | `clipvault.db` in the ClipVault folder |
| Settings | `config.yaml` in the ClipVault folder (the Settings screen writes it for you) |
| Twitch keys, Twitch login, panel password, notification topic, AI keys | Your computer's secure keychain (Windows Credential Manager), never in a file |
| Activity log, one file per day | `logs` in the ClipVault folder |
| Quality-check pictures | `qa` in the ClipVault folder |
| Thumbnails and Look previews for the panel | `cache` in the ClipVault folder |
| The chat tool | `bin` in the ClipVault folder |

**The database is the hub.** Every part reads from and writes to it: the finders add clips, the worker moves each clip through its steps, the panel reads it to show clips and writes your changes, the scheduler records when each task last ran (which is how catching up works), alerts and notifications wait in it, and settings like Pause live in it so the panel, the tray icon and the commands all agree.

---

## 16. How the parts talk to each other

- **Twitch's official API** (with the app keys) tells ClipVault about streamers, clips, finished streams and who is live.
- **Twitch's chat service** (with your login) delivers live chat to the live watcher.
- **The chat tool** reads finished streams' chat for the finished-stream finder.
- **The download tool** downloads clips and cuts moments out of recordings, for the worker.
- **FFmpeg** (the video tool) reads audio for subtitles, makes the phone versions, measures loudness, and makes thumbnails and previews.
- **The speech model** turns audio into timed words.
- **ntfy** delivers notifications to your phone.
- **Your browser** shows the control panel; the panel asks ClipVault for pages, videos and pictures.

The flow, in one line per finder:

- Viewer clips: Twitch API, found, repeat check, download, subtitles, phone version, dashboard.
- Finished-stream moments: Twitch API (finished streams), chat tool, spike detection, found, cut from recording, subtitles, phone version, dashboard.
- Live moments: Twitch API (who is live), live chat, spike detection, found (waits 3 minutes), cut from recording, subtitles, phone version, dashboard.

---

## 17. The tray icon

A small icon next to the clock while ClipVault runs: green running, yellow paused, red something needs you (refreshed every 30 seconds). Right-click it to open the control panel, pause or resume, or quit ClipVault.

---

## 18. Commands (the terminal way)

Everything important can be done from the control panel. For completeness, these are the commands typed in a terminal in the ClipVault folder, each starting with `python -m clipvault`:

| Command | What it does |
| --- | --- |
| `run` | Starts everything: scheduler, control panel, tray icon, live chat |
| `pause` / `resume` | Stops or restarts the scheduled work |
| `doctor` | Checks that everything ClipVault needs is installed and set up |
| `streamers import` / `streamers list` | Adds streamers from `streamers.txt` / shows the list |
| `discover` | Looks for new viewer clips now |
| `fetch` | Downloads the found clips now |
| `vods` | Searches finished streams' chat for moments now |
| `live` | Watches live chat in the terminal |
| `transcribe` | Makes subtitles now |
| `search` | Finds clips by what was said |
| `render` | Makes phone versions now |
| `headlines` | Writes AI headlines (needs an AI key) |
| `cleanup` | Runs the daily cleanup now (`--dry-run` only shows what it would remove) |
| `notify setup` / `test` / `status` | Phone notifications |
| `twitch login` / `logout` / `status` | Your Twitch login for live chat |
| `secrets set` / `list` / `delete` | Stores keys in the secure keychain |
| `panel reset-password` | Forgets the panel password |

---

## 19. Ground rules ClipVault always follows

- It never posts anything. (Posting was designed to work only for streamers who gave written or campaign permission, and only through an official posting service; you chose not to have it.)
- It finds clips only through Twitch's official API.
- It never deletes anything outside the library folder, and only deletes files it made itself.
- Keys, passwords and logins live in the computer's secure keychain, never in files.
- Every step can be interrupted at any moment (crash, power cut, sleep) and continues without doing anything twice.

---

## 20. Not built, or waiting for you

- **AI headlines** need an AI key (section 10).
- **Posting** to TikTok and YouTube, campaigns, view statistics and weekly compilations: not built, by your choice.
- **Installer, start at login, server version:** not built yet; ClipVault is started with `run` from a terminal.
- **Checks only you can do:** watching phone versions on your phone, setting up ntfy, and judging whether the chat moments are worth keeping.
