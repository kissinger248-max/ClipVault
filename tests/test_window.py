"""ClipVault's own window: signs itself in, hides instead of closing, comes back."""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clipvault import window
from clipvault.web import auth
from tests.test_web import PASSWORD, finish_setup


class FakeEvent:
    def __init__(self) -> None:
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def fire(self) -> bool:
        return all(handler() is not False for handler in self.handlers)


class FakeWindow:
    def __init__(self, **options) -> None:
        self.options = options
        self.events = types.SimpleNamespace(closing=FakeEvent())
        self.visible = not options["hidden"]
        self.destroyed = False
        self.on_top = False

    def show(self) -> None:
        self.visible = True

    def hide(self) -> None:
        self.visible = False

    def restore(self) -> None:
        pass

    def destroy(self) -> None:
        if self.events.closing.fire():
            self.destroyed = True


@pytest.fixture
def fake_webview(monkeypatch: pytest.MonkeyPatch):
    """A stand-in for pywebview: ``start`` returns at once, the window stays inspectable."""
    made: list[FakeWindow] = []

    def create_window(title, url, **options):
        made.append(FakeWindow(title=title, url=url, **options))
        return made[-1]

    def start(func=None, **options):
        made[-1].start_options = options

    module = types.SimpleNamespace(settings={}, create_window=create_window, start=start)
    monkeypatch.setitem(sys.modules, "webview", module)
    yield module, made
    window._window = None


def test_the_window_opens_on_the_sign_in_address_with_downloads_on(
    fake_webview, tmp_path: Path
) -> None:
    module, made = fake_webview
    window.run("http://127.0.0.1:8765", tmp_path / "webview", visible=False)
    win = made[0]
    assert win.options["title"] == "ClipVault" and not win.visible
    assert win.options["url"].startswith("http://127.0.0.1:8765/app/enter?t=")
    assert window.token_ok(win.options["url"].split("t=", 1)[1])
    assert module.settings["ALLOW_DOWNLOADS"] and module.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"]
    assert win.start_options["private_mode"] is False  # the sign-in is remembered
    assert win.start_options["storage_path"] == str(tmp_path / "webview")


def test_closing_hides_it_and_quitting_really_closes_it(fake_webview, tmp_path: Path) -> None:
    _module, made = fake_webview
    window.run("http://127.0.0.1:8765", tmp_path / "webview", visible=True)
    win = made[0]
    window._window = win  # run() clears it when start returns; the fake returns at once
    assert win.events.closing.fire() is False  # ✕ is cancelled ...
    assert not win.visible  # ... and the window hidden
    assert window.show() and win.visible
    window.close()
    assert win.destroyed and not window.is_open()


def test_show_without_a_window_says_so() -> None:
    window._window = None
    assert window.show() is False


def test_only_the_windows_token_signs_in(client: TestClient, home: Path) -> None:
    auth.set_password(PASSWORD)
    finish_setup(home)
    wrong = client.get("/app/enter", params={"t": "guess"})
    assert wrong.status_code == 303 and wrong.headers["location"] == "/login"
    right = client.get("/app/enter", params={"t": window._token})
    assert right.headers["location"] == "/" and auth.COOKIE in right.cookies
    assert client.get("/").status_code == 200  # signed in, no password typed


def test_the_first_password_is_still_chosen_once(client: TestClient) -> None:
    reply = client.get("/app/enter", params={"t": window._token})
    assert reply.headers["location"] == "/setup-password"


def test_a_second_launch_can_bring_the_window_forward(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(window, "show", lambda: True)
    assert client.get("/app/show").json() == {"shown": True}


def test_the_tray_opens_the_window_or_else_the_browser(monkeypatch, conn, home: Path) -> None:
    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", opened.append)
    from clipvault import tray

    calls = []

    class Icon:
        def __init__(self, name, image, title, menu):
            self.menu = menu

        def run_detached(self):
            pass

        def stop(self):
            pass

    def item(text, action, **options):
        return types.SimpleNamespace(text=text, action=action)

    fake_pystray = types.SimpleNamespace(Icon=Icon, Menu=lambda *items: list(items), MenuItem=item)
    monkeypatch.setitem(sys.modules, "pystray", fake_pystray)
    icon = tray.start("http://127.0.0.1:8765", home / "clipvault.db", lambda: None,
                      on_open=lambda: calls.append("window") or False)  # fmt: skip
    open_item = next(item for item in icon.menu if item.text == "Open ClipVault")
    open_item.action(icon, open_item)
    assert calls == ["window"] and opened == ["http://127.0.0.1:8765"]  # no window: browser
