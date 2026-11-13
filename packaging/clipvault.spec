# PyInstaller recipe for the installed ClipVault app (run through packaging/build.py).
#
# One folder build: Python, every library, the panel's templates and static files,
# the fonts, and packaging/tools/<platform>/ (FFmpeg, ffprobe, TwitchDownloaderCLI)
# under tools/. Windows: ClipVault.exe (no console) plus ClipVault-console.exe for
# `doctor` and the other commands. macOS: ClipVault.app, a menu-bar app (no Dock icon).
# ruff: noqa

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

ROOT = Path(SPECPATH).resolve().parent
PLATFORM = "windows" if sys.platform == "win32" else "macos"
TOOLS = ROOT / "packaging" / "tools" / PLATFORM
ICONS = ROOT / "packaging" / "icons"

if not TOOLS.is_dir():
    raise SystemExit(f"{TOOLS} is missing; run packaging/fetch_tools.py first")

sys.path.insert(0, str(ROOT / "packaging"))
from collect_licenses import licence_datas  # noqa: E402

datas = [
    *licence_datas(ROOT, TOOLS),  # LICENSES/: every shipped package, font and tool
    *collect_data_files("clipvault", excludes=["**/__pycache__/**"]),
    *collect_data_files("faster_whisper"),  # the voice-activity model
    *collect_data_files("certifi"),
    *collect_data_files("webview"),  # the app window's scripts and (Windows) WebView2 DLLs
    (str(ROOT / "assets" / "fonts"), "assets/fonts"),
    (str(TOOLS), "tools"),
]
binaries = [*collect_dynamic_libs("ctranslate2")]
if sys.platform == "win32":  # the app window drives WebView2 through .NET (pythonnet)
    binaries += [*collect_dynamic_libs("pythonnet"), *collect_dynamic_libs("clr_loader")]
    datas += [*collect_data_files("pythonnet"), *collect_data_files("clr_loader")]
hiddenimports = [
    *collect_submodules("clipvault"),
    *collect_submodules("uvicorn"),
    *collect_submodules("yt_dlp"),
    *collect_submodules("tzlocal"),
    *collect_submodules("webview"),
    "keyring.backends.Windows" if sys.platform == "win32" else "keyring.backends.macOS",
    "pystray._win32" if sys.platform == "win32" else "pystray._darwin",
    "multipart",
    "python_multipart",
]

a = Analysis(
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "pytest", "ruff", "IPython", "matplotlib", "torch"],
    noarchive=False,
)
# The tools' own DLLs (FFmpeg's) belong in tools/ only; PyInstaller would also copy
# them to the top level as dependencies of themselves (about 200 MB twice).
a.binaries = [entry for entry in a.binaries
              if not (Path(entry[1]).parent == TOOLS and Path(entry[0]).parent == Path("."))]
pyz = PYZ(a.pure)

icon = str(ICONS / ("clipvault.ico" if sys.platform == "win32" else "clipvault.icns"))
app_exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ClipVault",
    console=False,
    icon=icon,
    upx=False,
)
executables = [app_exe]
if sys.platform == "win32":
    # Same program with a console window, for `ClipVault-console doctor` and friends.
    executables.append(EXE(pyz, a.scripts, [], exclude_binaries=True,
                           name="ClipVault-console", console=True, icon=icon, upx=False))

coll = COLLECT(*executables, a.binaries, a.datas, name="ClipVault", upx=False)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="ClipVault.app",
        icon=icon,
        bundle_identifier="app.clipvault.desktop",
        info_plist={
            "CFBundleName": "ClipVault",
            "CFBundleDisplayName": "ClipVault",
            "CFBundleShortVersionString": __import__("clipvault").__version__,
            "LSUIElement": True,  # lives in the menu bar, no Dock icon
            "NSHighResolutionCapable": True,
        },
    )
