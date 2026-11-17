"""ClipVault's own window: a process of its own, signed in by the running app, raised on request."""

from __future__ import annotations

import sys
import threading
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clipvault import window
from clipvault.web import auth
from tests.test_web import PASSWORD, finish_setup


class FakeProcess:
    def __init__(self, command, **options) -> None:
        self.command = command
        self.options = options
        self.pid = 4242
        self.code = None
        self.terminated = False

    def poll(self):
        return self.code

    def terminate(self) -> None:
        self.terminated = True
        self.code = 0

    def wait(self, timeout=None):
        return self.code

    def kill(self) -> None:
        self.code = -9


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch, home: Path):
    """A window manager whose window process is a stand-in; it is the app's manager."""
    made: list[FakeProcess] = []

    def spawn(command, **options):
        made.append(FakeProcess(command, **options))
        return made[-1]

    manager = window.WindowManager("http://127.0.0.1:8765", home, spawn=spawn)
    window.set_manager(manager)
    yield manager, made
    window.set_manager(None)


def test_opening_starts_one_window_process_with_its_token(spawned, home: Path) -> None:
    manager, made = spawned
    assert window.show() is True
    assert len(made) == 1
    process = made[0]
    assert process.command[-3:] == ["window", "--url", "http://127.0.0.1:8765"]
    assert process.options["env"][window.TOKEN_ENV] == manager.token
    assert process.options["env"]["CLIPVAULT_HOME"] == str(home)
    assert manager.status() == {"running": True, "pid": 4242, "polled_s_ago": None}


def test_opening_again_brings_it_forward_instead_of_a_second_window(spawned) -> None:
    manager, made = spawned
    window.show()
    assert manager.poll(manager.token) == 0
    window.show()
    window.show()
    assert len(made) == 1  # still one window
    assert manager.poll(manager.token) == 2  # asked to come forward twice


def test_a_closed_window_is_started_again(spawned) -> None:
    manager, made = spawned
    window.show()
    made[0].code = 0  # the user closed the window: its process ended
    window.show()
    assert len(made) == 2


def test_quitting_closes_the_window_process(spawned) -> None:
    manager, made = spawned
    window.show()
    manager.close()
    assert made[0].terminated and not manager.running()


def test_a_window_that_cant_start_says_so(home: Path) -> None:
    def broken(command, **options):
        raise OSError("no such program")

    manager = window.WindowManager("http://127.0.0.1:8765", home, spawn=broken)
    assert manager.open() is False


def test_without_a_window_nothing_signs_in_or_shows() -> None:
    window.set_manager(None)
    assert window.show() is False
    assert window.token_ok("anything") is False


def test_the_installed_app_starts_itself_as_the_window(monkeypatch) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert window.window_command("http://x")[1:] == ["window", "--url", "http://x"]
    monkeypatch.setattr(sys, "frozen", False)
    assert window.window_command("http://x")[1:4] == ["-m", "clipvault", "window"]


# --- the panel's side ---------------------------------------------------------------


def test_only_the_windows_token_signs_in(client: TestClient, home: Path, spawned) -> None:
    manager, _made = spawned
    auth.set_password(PASSWORD)
    finish_setup(home)
    wrong = client.get("/app/enter", params={"t": "guess"})
    assert wrong.status_code == 303 and wrong.headers["location"] == "/login"
    right = client.get("/app/enter", params={"t": manager.token})
    assert right.headers["location"] == "/" and auth.COOKIE in right.cookies
    assert client.get("/").status_code == 200  # signed in, no password typed


def test_the_first_password_is_still_chosen_once(client: TestClient, spawned) -> None:
    manager, _made = spawned
    reply = client.get("/app/enter", params={"t": manager.token})
    assert reply.headers["location"] == "/setup-password"


def test_a_second_launch_opens_the_window(client: TestClient, spawned) -> None:
    _manager, made = spawned
    assert client.get("/app/show").json() == {"shown": True}
    assert len(made) == 1


def test_a_second_launch_without_a_window_gets_the_browser(client: TestClient) -> None:
    window.set_manager(None)
    assert client.get("/app/show").json() == {"shown": False}


def test_only_the_window_process_can_check_in(client: TestClient, spawned) -> None:
    manager, _made = spawned
    window.show()
    window.show()
    good = client.get(window.POLL_PATH, headers={window.TOKEN_HEADER: manager.token})
    assert good.json() == {"raise": 1}
    assert client.get(window.POLL_PATH, headers={window.TOKEN_HEADER: "guess"}).status_code == 403
    assert client.get(window.POLL_PATH).status_code == 403
    status = client.get("/app/window/status").json()
    assert status["running"] and status["polled_s_ago"] is not None


# --- the window process ---------------------------------------------------------------


class FakeWindow:
    def __init__(self, **options) -> None:
        self.options = options
        self.events = types.SimpleNamespace(shown=threading.Event())
        self.events.shown.set()
        self.destroyed = threading.Event()
        self.raised = 0

    def destroy(self) -> None:
        self.destroyed.set()


@pytest.fixture
def fake_webview(monkeypatch: pytest.MonkeyPatch):
    """A stand-in for pywebview: ``start`` waits until the window is destroyed."""
    made: list[FakeWindow] = []

    def create_window(title, url, **options):
        made.append(FakeWindow(title=title, url=url, **options))
        return made[-1]

    def start(func=None, **options):
        made[-1].start_options = options
        made[-1].destroyed.wait(10)

    module = types.SimpleNamespace(settings={}, create_window=create_window, start=start)
    monkeypatch.setitem(sys.modules, "webview", module)
    monkeypatch.setattr(window, "POLL_EVERY_S", 0.01)
    monkeypatch.setattr(window, "GIVE_UP_AFTER_S", 0.1)
    monkeypatch.setattr(window, "bring_forward", lambda win: setattr(win, "raised", win.raised + 1))
    return module, made


class Reply:
    def __init__(self, status: int, body: dict | None = None) -> None:
        self.status_code = status
        self.body = body or {}

    def json(self) -> dict:
        return self.body


def test_the_window_signs_in_comes_forward_and_closes_when_the_app_is_gone(
    fake_webview, tmp_path: Path
) -> None:
    module, made = fake_webview
    replies = [Reply(200, {"raise": 0}), Reply(200, {"raise": 0}), Reply(200, {"raise": 1})]
    asked: list[dict] = []

    def get(url, headers):
        asked.append(headers)
        return replies.pop(0) if replies else Reply(403)  # then: the app has gone

    code = window.run_window("http://127.0.0.1:8765", "secret", tmp_path / "webview", get=get)
    win = made[0]
    assert code == 0
    assert win.options["title"] == "ClipVault"
    assert win.options["url"] == "http://127.0.0.1:8765/app/enter?t=secret"
    assert "hidden" not in win.options  # never hidden: closing it ends the process
    assert module.settings["ALLOW_DOWNLOADS"] and module.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"]
    assert win.start_options == {"private_mode": False, "storage_path": str(tmp_path / "webview")}
    assert asked[0] == {window.TOKEN_HEADER: "secret"}
    assert win.raised == 1  # asked once to come forward
    assert win.destroyed.is_set()  # closed itself once the app stopped answering


def test_the_window_command_needs_the_apps_token(monkeypatch, capsys) -> None:
    from clipvault.__main__ import main

    monkeypatch.delenv(window.TOKEN_ENV, raising=False)
    assert main(["window", "--url", "http://127.0.0.1:8765"]) == 1
    assert "running ClipVault" in capsys.readouterr().out


# --- the tray -----------------------------------------------------------------------------


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
