"""Updates: checking GitHub, the checksum, announcing once, installing at a quiet moment."""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from clipvault import __version__, alerts, lifecycle, updates
from clipvault import config as cfg
from clipvault.notify import ntfy
from clipvault.timeutil import to_rfc3339, utc_now
from tests.test_web import csrf, finish_setup, login

INSTALLER = b"pretend installer bytes" * 100
NEXT = "99.0.0"


def feed(tag: str = f"v{NEXT}", installer: bytes = INSTALLER, listed: bytes | None = None,
         asset: str = "ClipVault-Setup-Windows.exe") -> httpx.Client:  # fmt: skip
    """A stand-in for GitHub: the releases/latest JSON, the installer and SHA256SUMS.txt."""
    digest = hashlib.sha256(listed if listed is not None else installer).hexdigest()

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/releases/latest"):
            return httpx.Response(200, json={
                "tag_name": tag, "html_url": f"https://example.test/{tag}", "body": "Notes",
                "published_at": "2026-11-20T10:00:00Z", "draft": False, "prerelease": False,
                "assets": [
                    {"name": asset, "browser_download_url": f"https://example.test/{asset}",
                     "size": len(installer)},
                    {"name": "SHA256SUMS.txt", "browser_download_url": "https://example.test/sums"},
                ]})  # fmt: skip
        if request.url.path == "/sums":
            return httpx.Response(200, text=f"{digest}  {asset}\n")
        return httpx.Response(200, content=installer)

    return httpx.Client(transport=httpx.MockTransport(handle))


@pytest.fixture
def windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")


@pytest.fixture
def installable(monkeypatch: pytest.MonkeyPatch, windows) -> list[Path]:
    """This copy may install updates; installer launches are recorded, not run."""
    launched: list[Path] = []
    monkeypatch.setattr(updates, "can_install", lambda: True)
    monkeypatch.setattr(updates, "launch_installer", launched.append)
    return launched


def test_versions_compare_as_numbers() -> None:
    assert updates.version_tuple("v0.10.2") == (0, 10, 2)
    assert updates.is_newer("0.10.0", "0.9.9") and not updates.is_newer("0.2.0", "0.2.0")
    assert not updates.is_newer("v0.1.9", "0.2.0")


def test_a_newer_release_is_remembered_and_an_old_one_forgotten(conn, windows) -> None:
    release = updates.check(conn, feed())
    assert release.version == NEXT and release.asset_name == "ClipVault-Setup-Windows.exe"
    assert updates.available(conn) == release
    assert updates.check(conn, feed(tag=f"v{__version__}")) is None
    assert updates.available(conn) is None


def test_a_failed_check_keeps_what_was_known(conn, windows) -> None:
    updates.check(conn, feed())
    broken = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    assert updates.check(conn, broken).version == NEXT
    assert "503" in alerts.get_setting(conn, updates.ERROR)


def test_releases_without_an_installer_for_this_system_are_ignored(conn, monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    assert updates.check(conn, feed()) is None


def test_the_download_must_match_its_published_checksum(conn, home: Path, windows) -> None:
    release = updates.check(conn, feed(listed=b"something else"))
    with pytest.raises(updates.UpdateError, match="checksum"):
        updates.download(conn, release, feed(listed=b"something else"))
    assert not list((home / "updates").glob("ClipVault-*"))
    path = updates.download(conn, release, feed())
    assert path.read_bytes() == INSTALLER
    assert updates.download(conn, release, feed()) == path  # downloaded once, reused


def test_a_new_version_is_announced_on_the_phone_once(conn, monkeypatch, windows) -> None:
    monkeypatch.setattr(ntfy, "get_topic", lambda: "topic")
    monkeypatch.setattr(updates, "can_install", lambda: False)
    config = cfg.Config()
    now = utc_now()
    assert "available" in updates.step_updates(conn, config, now, http=feed())
    updates.step_updates(conn, config, now + timedelta(hours=7), http=feed())
    rows = conn.execute("SELECT title FROM notifications WHERE kind = 'update'").fetchall()
    assert [r["title"] for r in rows] == [f"ClipVault {NEXT} is available"]


def test_it_installs_right_after_starting_and_then_quits(conn, installable, monkeypatch) -> None:
    quits: list[bool] = []
    lifecycle.on_quit(lambda: quits.append(True))
    monkeypatch.setattr(lifecycle, "started_at", utc_now())
    try:
        note = updates.step_updates(conn, cfg.Config(), utc_now(), http=feed())
    finally:
        lifecycle.on_quit(None)
    assert note == f"installing ClipVault {NEXT}"
    assert len(installable) == 1 and installable[0].read_bytes() == INSTALLER
    assert quits == [True]
    assert json.loads(alerts.get_setting(conn, updates.INSTALLED_FROM))["to"] == NEXT


def test_later_in_the_day_it_waits_for_quiet_hours(conn, installable, monkeypatch) -> None:
    now = utc_now()
    monkeypatch.setattr(lifecycle, "started_at", now - timedelta(hours=3))
    monkeypatch.setattr(ntfy, "quiet_until", lambda when, config: None)  # not quiet now
    assert "installs at a quiet moment" in updates.step_updates(
        conn, cfg.Config(), now, http=feed()
    )
    assert installable == []
    monkeypatch.setattr(ntfy, "quiet_until", lambda when, config: when + timedelta(hours=1))
    updates.step_updates(conn, cfg.Config(), now, http=feed())
    assert len(installable) == 1


def test_switched_off_means_no_automatic_install(conn, installable, monkeypatch) -> None:
    monkeypatch.setattr(lifecycle, "started_at", utc_now())
    alerts.set_setting(conn, updates.AUTO, "0")
    assert "is available" in updates.step_updates(conn, cfg.Config(), utc_now(), http=feed())
    assert installable == []
    alerts.set_setting(conn, updates.CHECKS, "0")
    alerts.set_setting(conn, updates.CHECKED, "")
    assert updates.step_updates(conn, cfg.Config(), utc_now(), http=feed()) == ""


def test_test_copies_never_check(conn, monkeypatch, windows) -> None:
    monkeypatch.setenv("CLIPVAULT_NO_UPDATES", "1")

    def no_network(request):
        raise AssertionError("a test copy asked GitHub")

    client = httpx.Client(transport=httpx.MockTransport(no_network))
    assert updates.step_updates(conn, cfg.Config(), utc_now(), http=client) == ""


def test_just_updated_shows_for_two_days(conn) -> None:
    marker = {"from": "0.0.1", "to": __version__, "at": to_rfc3339(utc_now())}
    alerts.set_setting(conn, updates.INSTALLED_FROM, json.dumps(marker))
    assert updates.just_updated(conn) == __version__
    assert updates.just_updated(conn, now=utc_now() + timedelta(days=3)) is None


def test_home_offers_the_update_and_settings_has_the_switches(
    client: TestClient, home: Path, installable
) -> None:
    login(client)
    finish_setup(home)
    from clipvault import db

    c = db.open_db(home / "clipvault.db")
    updates.check(c, feed())
    c.close()
    page = client.get("/").text
    assert f"ClipVault {NEXT} is available" in page and 'action="/update-now"' in page
    settings = client.get("/settings").text
    assert 'id="updates"' in settings and "Install updates automatically" in settings
    client.post("/settings/updates", data={"csrf": csrf(client), "checks": "1"})
    c = db.open_db(home / "clipvault.db")
    try:
        assert not updates.auto_install(c) and updates.checks_on(c)
    finally:
        c.close()


def test_a_source_copy_only_points_to_the_release(client: TestClient, home: Path, windows) -> None:
    login(client)
    finish_setup(home)
    from clipvault import db

    c = db.open_db(home / "clipvault.db")
    updates.check(c, feed())
    c.close()
    page = client.get("/").text
    assert "Download it from the release page" in page and 'action="/update-now"' not in page
    reply = client.post("/update-now", data={"csrf": csrf(client)})
    assert (
        "can%27t+update+itself" in reply.headers["location"]
        or "update+itself" in reply.headers["location"]
    )
