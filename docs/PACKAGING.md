# Packaging: the installers and the download page

How ClipVault becomes one download that installs everything and starts it.

## What's in the installed app

| Part | Where it comes from |
| --- | --- |
| Python and every library (FastAPI, OpenCV, faster-whisper, ...) | PyInstaller, `packaging/clipvault.spec` |
| yt-dlp, as its official standalone program (Unlicense) | [yt-dlp/yt-dlp](https://github.com/yt-dlp/yt-dlp) releases |
| FFmpeg and ffprobe, with libass for captions (GPL) | Windows: [BtbN's builds](https://github.com/BtbN/FFmpeg-Builds) (shared GPL, newest release branch). Mac: [Martin Riedl's builds](https://ffmpeg.martin-riedl.de) (Apple silicon) |
| TwitchDownloaderCLI, for chat replays (MIT) | [lay295/TwitchDownloader](https://github.com/lay295/TwitchDownloader) releases |
| Caption fonts (OFL) and panel fonts (Inter, OFL) | `assets/fonts`, `clipvault/web/static/fonts` |
| The speech model | Not bundled: faster-whisper downloads it on the first transcription |
| `LICENSES/` | Every shipped package's licence files, Python's, the fonts' and the tools' (`packaging/collect_licenses.py`) |

`packaging/fetch_tools.py` downloads the tools into `packaging/tools/<platform>/`
with their licences, and refuses an FFmpeg without the `subtitles` filter.

## How the installed app behaves

- **Start:** `clipvault/desktop.py`. With no arguments it runs `clipvault run`:
  scheduler, panel, tray icon (menu bar on Mac), and the panel opens in the browser.
  Opening it again while it runs just opens the panel.
- **App window** (`clipvault/window.py`): the panel opens in ClipVault's own window
  (pywebview: WebView2 on Windows, WebKit on macOS), which signs itself in with a token
  held only in the running app. Closing the window hides it, and opening ClipVault again
  brings it forward (`/app/show`). It falls back to the browser if the window can't
  start. From source, `run --window`.
- **No console window.** Output goes to the log files. On Windows the tools it starts
  get no window either. `ClipVault-console.exe` (Windows) runs the other commands,
  for example `ClipVault-console doctor`.
- **Bundled tools** come first on PATH, so the same code finds them.
- **yt-dlp:** there is no Python or pip to run `python -m yt_dlp`, so the app copies the
  bundled standalone yt-dlp to `<data folder>/bin` on first use and keeps it current
  with `yt-dlp -U` once a day (`clipvault/fetch/ytdlp.py`).
- **Data:** `%LOCALAPPDATA%\ClipVault` (Windows) or
  `~/Library/Application Support/ClipVault` (Mac) instead of the current folder.
  `CLIPVAULT_HOME` still wins.
- **First run:** switches on "Open when I log in" (Windows: the `Run` registry key;
  Mac: a LaunchAgent) and sets the computer's time zone. Both happen once; Settings
  can change them afterwards.
- **Start at login** registers `ClipVault.exe run --no-browser`, so it starts quietly
  in the background; each start refreshes the entry.
- **Updates** (`clipvault/updates.py`):
  - **Check:** the latest GitHub release, at start and every 6 hours.
  - **Announce:** a Home card, Settings › Updates, a tray menu item and one phone
    notification per version.
  - **Download:** the installer, refused unless it matches the release's `SHA256SUMS.txt`.
  - **Install:** Windows runs the installer with `/VERYSILENT`, and the installer starts
    ClipVault again. macOS swaps `ClipVault.app` after the app quits, then reopens it.
  - **When:** at a quiet moment (within 15 minutes of starting, or in the notification
    quiet hours), or at once with Update Now. Settings can turn off automatic installing
    or the check itself.
  - **Testing:** `CLIPVAULT_UPDATE_FEED` points the check at another address, and
    `CLIPVAULT_NO_UPDATES` turns it off for test copies.
- **Quitting** sets a shared "stopping" signal: download, subtitle and phone-version
  rounds stop after the current clip. A timed-out tool is killed with everything it
  started (`clipvault/procs.py`).
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
- checks the look preview and the panel fonts;
- asks Twitch for a sign-in code (when the build has the built-in app ID).

`selftest` itself burns a caption into a test video with the bundled FFmpeg and fonts
and reads it back with OpenCV, and runs the bundled yt-dlp. Test copies set
`CLIPVAULT_NO_AUTOSTART=1` so they never register to start at login.

Then it makes the installer:

- **Windows:** Inno Setup 6, `packaging/windows/ClipVault.iss`:
  - installs per user, with no administrator prompt;
  - adds Start menu and desktop shortcuts (the desktop one can be unticked);
  - starts ClipVault at the end, including after a silent update;
  - uninstalling keeps clips and settings.
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

`site/` is the website from `docs/website-design/`: static HTML, CSS and a little
JavaScript, with no build step. It has the home/download page, FAQ, About, Privacy,
Terms, Licences and a 404 page, plus sitemap and robots.

- **Behaviour:** `assets/site.js` detects the visitor's system and points the main
  button at that installer. After a download starts it shows what to do next, and
  sections rise in as you scroll (off under Reduce Motion).
- **Release details:** after each release, run
  `python packaging/update_site.py --macos-min <version>`. It fills the version, date,
  sizes and SHA-256 checksums from the GitHub release. Take the macOS version from the
  build's `macos-build-facts` artifact.
- **Site address:** pages use `[SITE URL]` for links shared on social media. Once the
  site has its address, run `python packaging/update_site.py --site-url https://…`.
- **Pictures:** `packaging/site_images.py` redraws the placeholder pictures, icons and
  share image.
- **Owner placeholders:** text in square brackets is left for the owner to fill in:
  name, emails, pricing, the Terms licence choice, jurisdiction and hosting details.

To put it on Vercel:

1. **Add New → Project**, and import the GitHub repository.
2. Set **Root Directory** to `site` and Framework Preset to **Other**, with no build command.
3. **Deploy**.

`site/vercel.json` gives clean addresses (`/faq`).

## Not done yet (M13)

- **Code signing:** a Windows certificate and an Apple Developer account cost money, so
  they were not set up.
- **Docker image, Tailscale and Syncthing instructions:** not done.
