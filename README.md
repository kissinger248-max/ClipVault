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

## Commands

```
python -m clipvault doctor        # check the installation
pytest -q                         # run the tests
ruff check . && ruff format --check .
```

`run`, `discover`, `fetch`, `render` and `publish` arrive in later milestones.

## Logs

JSON lines in `logs/clipvault.jsonl`, one file per day, 14 days kept. Every line
has `clip`, `streamer` and `job` fields.
