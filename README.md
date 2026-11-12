# ClipVault

Watches a list of Twitch streamers, saves their best clips to a local library and,
only for streamers who gave permission, posts vertical versions to TikTok and
YouTube Shorts. See [docs/SPEC.md](docs/SPEC.md) for what it does and
[docs/BUILD_PLAN.md](docs/BUILD_PLAN.md) for what is built so far.

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
   copy its client ID and a new client secret. Store them in the OS keychain (the
   command asks for the value and keeps it hidden):

   ```
   python -m clipvault secrets set twitch_client_id
   python -m clipvault secrets set twitch_client_secret
   ```

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

## Commands

```
python -m clipvault doctor                         # check the installation
python -m clipvault secrets set NAME               # store a key (also: secrets list, secrets delete NAME)
python -m clipvault streamers import [FILE]        # add streamers from a text file
python -m clipvault streamers list                 # show the streamer list
python -m clipvault discover [--streamer LOGIN] [--since 7d]
python scripts/twitch_live_check.py LOGIN...       # manual check against the real Twitch API
pytest -q                                          # run the tests
ruff check . && ruff format --check .
```

`run`, `fetch`, `render` and `publish` arrive in later milestones.

## Logs

JSON lines in `logs/clipvault-YYYY-MM-DD.jsonl`, one file per day. Every line has
`clip`, `streamer` and `job` fields. Old log files are not removed automatically.
