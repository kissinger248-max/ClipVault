# Packaging: the installers and the download page

How ClipVault becomes one download that installs everything and starts it.

## What's in the installed app

| Part | Where it comes from |
| --- | --- |
| Python and every library (FastAPI, OpenCV, faster-whisper, yt-dlp, ...) | PyInstaller, `packaging/clipvault.spec` |
| FFmpeg and ffprobe, with libass for captions (GPL) | Windows: [BtbN's builds](https://github.com/BtbN/FFmpeg-Builds) (shared GPL, newest release branch). Mac: [Martin Riedl's builds](https://ffmpeg.martin-riedl.de) (Apple silicon) |
| TwitchDownloaderCLI, for chat replays (MIT) | [lay295/TwitchDownloader](https://github.com/lay295/TwitchDownloader) releases |
| Caption fonts (OFL) and panel fonts (Inter, OFL) | `assets/fonts`, `clipvault/web/static/fonts` |
| The speech model | Not bundled: faster-whisper downloads it on the first transcription |

`packaging/fetch_tools.py` downloads the tools into `packaging/tools/<platform>/`
with their licences, and refuses an FFmpeg without the `subtitles` filter.

## How the installed app behaves

- **Start:** `clipvault/desktop.py`. With no arguments it runs `clipvault run`:
  scheduler, panel, tray icon (menu bar on Mac), and the panel opens in the browser.
  Opening it again while it runs just opens the panel.
- **No console window.** Output goes to the log files. On Windows the tools it starts
  get no window either. `ClipVault-console.exe` (Windows) runs the other commands,
  for example `ClipVault-console doctor`.
- **Bundled tools** come first on PATH, so the same code finds them.
- **Data:** `%LOCALAPPDATA%\ClipVault` (Windows) or
  `~/Library/Application Support/ClipVault` (Mac) instead of the current folder.
  `CLIPVAULT_HOME` still wins.
- **First run:** switches on "Open when I log in" (Windows: the `Run` registry key;
  Mac: a LaunchAgent) and sets the computer's time zone. Both happen once; Settings
  can change them afterwards.
- **Twitch:** "Sign in with Twitch" uses the built-in public app ID in
  `clipvault/data/twitch_public_client_id.txt`. A user's own developer keys, if
  stored, still win. See `clipvault/twitch/__init__.py`.

## Building

```
pip install -e ".[build]"
python packaging/build.py --smoke
```

`build.py` draws the icons, fetches the tools if needed, runs PyInstaller, runs the
built app's `selftest` (tools, libraries, models, fonts, panel files, database), and
with `--smoke` runs `packaging/smoke_test.py`. The smoke test:

- starts the built app in a temporary home folder with its own keychain entries
  (`CLIPVAULT_KEYRING_SERVICE`);
- chooses the first password;
- opens the setup pages;
- has the bundled FFmpeg draw a caption preview;
- asks Twitch for a sign-in code.

Then it makes the installer:

- **Windows:** Inno Setup 6, `packaging/windows/ClipVault.iss`. It installs per user (no
  administrator prompt) and starts ClipVault at the end. Uninstalling keeps clips and
  settings.
- **Mac:** a `.dmg` with ClipVault.app and an Applications shortcut. The app is
  ad-hoc signed, not notarised, so the first open needs Privacy & Security, Open Anyway.

## Releasing

`.github/workflows/release.yml` runs on GitHub's Windows and Mac machines:

1. unit tests;
2. the build with `--smoke`;
3. for a tag `vX.Y.Z`, a GitHub release with both installers.

To release:

1. Raise the version in `pyproject.toml` and `clipvault/__init__.py`.
2. Commit, then `git tag vX.Y.Z` and `git push origin main vX.Y.Z`.

The download page links to `releases/latest/download/<file>`, so it never needs
editing for a new version.

## The download page

`site/index.html` is one static file. It detects Windows or Mac and links to the
latest release.

On Vercel: **Add New → Project**, import the GitHub repository, set **Root
Directory** to `site`, Framework Preset **Other**, no build command, then Deploy.

## Not done yet (M13)

- **Update check:** the app doesn't yet tell you a new version exists. Installing a
  newer release over the old one keeps everything.
- **Code signing:** a Windows certificate and an Apple Developer account cost money, so
  they were not set up.
- **Docker image, Tailscale and Syncthing instructions:** not done.
