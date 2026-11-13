# ClipVault

Watches a list of Twitch streamers, saves their best clips to a local library and,
only for streamers who gave permission, posts vertical versions to TikTok and
YouTube Shorts. See [docs/SPEC.md](docs/SPEC.md) for what it does and
[docs/BUILD_PLAN.md](docs/BUILD_PLAN.md) for what is built so far.

## What works today, and what's coming

| Part | What it does | Status |
| --- | --- | --- |
| Find clips | Finds each streamer's most-viewed clips every day | Working |
| Save clips | Downloads them into a tidy library folder, skips repeats, cleans up old ones | Working |
| Run by itself | Schedule, catch-up after sleep, phone notifications | Next (M3) |
| Missed moments | Cuts moments nobody clipped, found from chat activity | M4 |
| Control panel | Browser and phone screens, tray icon | M5 |
| Subtitles | Transcripts with word-by-word timing, searchable | M6 |
| Phone-ready video | Vertical 9:16 versions with a headline and captions kept clear of TikTok and Shorts buttons | M7 |
| Headlines and look | Hook headline writer, style editor with live preview | M8 |
| Posting | TikTok and YouTube Shorts, only for streamers who gave permission | M9 onward |

The exact rules for the phone-ready video (sizes, fonts, caption timing, safe
area, loudness, automatic quality checks) are in [docs/SPEC.md](docs/SPEC.md),
sections 8 to 11.

## Quick start (about 5 minutes)

You need **Python 3.12+** and **FFmpeg**.

| System | Install FFmpeg |
| --- | --- |
| Windows | `winget install Gyan.FFmpeg` (or `scoop install ffmpeg`) |
| macOS | `brew install ffmpeg` |
| Linux | `sudo apt install ffmpeg` |

Then, in the project folder:

```
python -m venv .venv
.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"
python -m clipvault doctor
```

`doctor` checks Python, FFmpeg, yt-dlp, TwitchDownloaderCLI, the config file, the
OS keychain, free disk space and the database, and prints how to fix anything
that is missing. `[WARN]` lines are optional; `[FAIL]` lines need fixing.

## Settings

- **Non-secret settings** live in `config.yaml`. It is optional: copy
  `config.example.yaml` to `config.yaml` and change what you need. Anything you
  leave out uses the default.
- **Secrets** (Twitch keys, API keys, the ntfy topic) are stored in the OS
  keychain through the `keyring` library, never in files.
- `config.yaml`, the database (`clipvault.db`) and `logs/` live in the folder you
  run ClipVault from. Set the `CLIPVAULT_HOME` environment variable to use a
  different folder.
- **TwitchDownloaderCLI** (only needed for VOD chat moments): download it from
  <https://github.com/lay295/TwitchDownloader/releases> and put it in `bin/` or
  anywhere on PATH.

## Finding clips

1. **Twitch keys (free, once).** Go to <https://dev.twitch.tv/console>, register an
   application with redirect URL `http://localhost:8765/auth/twitch/callback`, and
   copy its client ID and a new client secret. Store them in the OS keychain.
   Each command opens a small window: paste the value and click Save.

   ```
   python -m clipvault secrets set twitch_client_id --window
   python -m clipvault secrets set twitch_client_secret --window
   ```

   Without `--window` the value is asked for in the terminal instead, hidden as
   you paste it.

2. **Streamer list.** Copy `streamers.example.txt` to `streamers.txt`, put one
   Twitch login per line (the example file explains the optional settings), then:

   ```
   python -m clipvault streamers import
   ```

   Importing again is safe: it only adds new logins and applies changed settings.

3. **Discover.**

   ```
   python -m clipvault discover
   ```

   The first run looks back 7 days; later runs continue from where the last one
   stopped, so a run after the laptop was asleep catches up. By default each
   streamer's 5 most-viewed clips per day are kept (`top_n`), or every clip above
   a view count if you set `min_views`. Running it twice in a row adds nothing new.

## Saving clips

```
python -m clipvault fetch
```

downloads every clip that was found into the library folder (`library_root` in
`config.yaml`, default `~/Twitch Archive`), most-viewed first:

```
<library>/<streamer>/YYYY-MM/YYYY-MM-DD_<views>views_<title>_<clip id>.mp4
                             ...same name....json    everything known about the clip
<library>/Favorites/         move a file here and ClipVault keeps it forever
```

- Running it again never downloads a clip twice, and a run that was interrupted
  picks up where it stopped.
- Clips that repeat another clip of the same moment are skipped (the one with more
  views is kept).
- A failed download is retried after 1, 5 and 30 minutes, then given up on.
- Downloads pause when less than 15% of the disk is free (`min_free_space_pct`).
- If the library is on a drive that isn't plugged in, new clips wait in
  `library-cache/` next to the database and move over when the drive is back.
- The downloader (yt-dlp) updates itself once a day, because Twitch changes
  regularly break old versions.

```
python -m clipvault cleanup --dry-run     # show what would be removed
python -m clipvault cleanup               # do it
```

removes clips older than 60 days that have fewer views than that streamer's
median. Starred clips and anything in `Favorites/` are never removed. It also
notices files you deleted by hand (they are not downloaded again) and reports
free disk space.

## Commands

```
python -m clipvault doctor                         # check the installation
python -m clipvault secrets set NAME [--window]    # store a key (also: secrets list, secrets delete NAME)
python -m clipvault streamers import [FILE]        # add streamers from a text file
python -m clipvault streamers list                 # show the streamer list
python -m clipvault discover [--streamer LOGIN] [--since 7d] [--all]
python -m clipvault fetch [--streamer LOGIN] [--limit N]    # download found clips
python -m clipvault cleanup [--dry-run]            # retention, disk check, hand-deleted files
python scripts/twitch_live_check.py LOGIN...       # manual check against the real Twitch API
pytest -q                                          # run the tests
ruff check . && ruff format --check .
```

`run`, `render` and `publish` arrive in later milestones.

## Logs

JSON lines in `logs/clipvault-YYYY-MM-DD.jsonl`, one file per day. Every line has
`clip`, `streamer` and `job` fields. Old log files are not removed automatically.
