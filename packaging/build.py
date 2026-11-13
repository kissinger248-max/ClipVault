"""Build the installed ClipVault app and its installer for this computer's system.

    python packaging/build.py            # Windows: Setup.exe; macOS: .dmg
    python packaging/build.py --smoke    # also start the built app and use its panel

Steps: draw the icons, download the bundled tools (packaging/fetch_tools.py) unless
they are already there, build with PyInstaller (packaging/clipvault.spec), run the
built app's ``selftest``, then make the installer:

- Windows: Inno Setup 6 (``ISCC.exe``) makes packaging/build/ClipVault-Setup-Windows.exe
- macOS: ``hdiutil`` makes packaging/build/ClipVault-macOS.dmg (drag to Applications)

Needs ``pip install -e ".[build]"``.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
BUILD = HERE / "build"
DIST = BUILD / "dist"


def run(*command: str | Path, **kwargs) -> None:
    print("$", " ".join(str(c) for c in command), flush=True)
    subprocess.run([str(c) for c in command], check=True, **kwargs)


def version() -> str:
    sys.path.insert(0, str(ROOT))
    import clipvault

    return clipvault.__version__


def app_program() -> Path:
    """The built program that runs commands (``selftest``, ``run``)."""
    if sys.platform == "win32":
        return DIST / "ClipVault" / "ClipVault-console.exe"
    return DIST / "ClipVault.app" / "Contents" / "MacOS" / "ClipVault"


def find_iscc() -> Path | None:
    found = shutil.which("iscc")
    if found:
        return Path(found)
    for base in (
        os.environ.get("ProgramFiles(x86)"),
        os.environ.get("ProgramFiles"),
        os.environ.get("LOCALAPPDATA") and str(Path(os.environ["LOCALAPPDATA"]) / "Programs"),
    ):
        if base and (Path(base) / "Inno Setup 6" / "ISCC.exe").exists():
            return Path(base) / "Inno Setup 6" / "ISCC.exe"
    return None


def windows_installer() -> Path:
    iscc = find_iscc()
    if iscc is None:
        raise SystemExit("Inno Setup 6 is not installed (https://jrsoftware.org/isinfo.php); "
                         "the app folder is ready in " + str(DIST / "ClipVault"))  # fmt: skip
    run(iscc, f"/DAppVersion={version()}", f"/DSourceDir={DIST / 'ClipVault'}",
        f"/DOutputDir={BUILD}", f"/DIconFile={HERE / 'icons' / 'clipvault.ico'}",
        HERE / "windows" / "ClipVault.iss")  # fmt: skip
    return BUILD / "ClipVault-Setup-Windows.exe"


def mac_disk_image() -> Path:
    staging = BUILD / "dmg"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    run("ditto", DIST / "ClipVault.app", staging / "ClipVault.app")
    (staging / "Applications").symlink_to("/Applications")
    image = BUILD / "ClipVault-macOS.dmg"
    image.unlink(missing_ok=True)
    run("hdiutil", "create", "-volname", "ClipVault", "-srcfolder", staging, "-ov",
        "-format", "UDZO", image)  # fmt: skip
    return image


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fresh-tools", action="store_true", help="download the tools again")
    parser.add_argument("--no-installer", action="store_true", help="stop after the app folder")
    parser.add_argument("--smoke", action="store_true", help="start the built app and test it")
    args = parser.parse_args()

    run(sys.executable, HERE / "make_icons.py")
    platform = "windows" if sys.platform == "win32" else "macos"
    if args.fresh_tools or not (HERE / "tools" / platform).is_dir():
        run(sys.executable, HERE / "fetch_tools.py")
    run(sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--distpath", DIST, "--workpath", BUILD / "work", HERE / "clipvault.spec")  # fmt: skip
    with tempfile.TemporaryDirectory() as scratch:  # keep the check out of real app data
        env = {
            **os.environ,
            "CLIPVAULT_HOME": scratch,
            "CLIPVAULT_NO_AUTOSTART": "1",
            "CLIPVAULT_NO_UPDATES": "1",
        }
        run(app_program(), "selftest", env=env)
    if args.smoke:
        run(sys.executable, HERE / "smoke_test.py", app_program())
    if args.no_installer:
        return 0
    result = windows_installer() if sys.platform == "win32" else mac_disk_image()
    size = result.stat().st_size / 1e6
    print(f"\nBuilt {result} ({size:,.0f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
