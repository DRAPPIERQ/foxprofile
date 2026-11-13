import threading

import pytest

from src.models.profile import Profile
from src.services.browser import launcher as launcher_mod
from src.services.browser.launcher import BrowserControlError, BrowserLauncher, ProfileBusyError
from src.services.browser.recording import STOP_TIMEOUT
from src.ui.actions.browser import launch_or_stop
from src.ui.state import AppState


class FakeProc:
    def poll(self):
        return None


@pytest.fixture
def bl(monkeypatch):
    spawned = []

    def spawn(profile):
        spawned.append(profile.name)
        return FakeProc()

    monkeypatch.setattr(launcher_mod, "spawn_browser", spawn)
    monkeypatch.setattr(launcher_mod, "wait_for_exit", lambda *a: None)
    b = BrowserLauncher()
    monkeypatch.setattr(b, "_monitor_process", lambda *a: None)
    b.spawned = spawned
    yield b
    # Fake processes must not reach terminate() at interpreter exit.
    b._active_sessions.clear()


def test_second_launch_returns_false_without_callbacks(bl):
    calls = []
    assert bl.start_thread(Profile(name="p"), lambda m: None) is True
    started = bl.start_thread(
        Profile(name="p"),
        lambda m: None,
        on_start=lambda: calls.append("start"),
        on_stop=lambda: calls.append("stop"),
    )
    assert started is False
    assert calls == []
    assert bl.spawned == ["p"]


def test_concurrent_launches_spawn_one_browser(bl, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def slow_spawn(profile):
        entered.set()
        release.wait(5)
        bl.spawned.append(profile.name)
        return FakeProc()

    monkeypatch.setattr(launcher_mod, "spawn_browser", slow_spawn)
    first = threading.Thread(target=bl.start_thread, args=(Profile(name="p"), lambda m: None))
    first.start()
    assert entered.wait(5)
    try:
        with pytest.raises(ProfileBusyError):
            bl.start_thread(Profile(name="p"), lambda m: None)
    finally:
        release.set()
        first.join(5)
    assert bl.spawned == ["p"]
    assert bl.is_running("p")


def test_failed_spawn_releases_the_profile(bl, monkeypatch):
    def broken(profile):
        raise OSError("no runner")

    monkeypatch.setattr(launcher_mod, "spawn_browser", broken)
    stopped = []
    assert bl.start_thread(Profile(name="p"), lambda m: None, on_stop=lambda: stopped.append(1))
    assert stopped == [1]
    with bl.exclusive("p"):
        pass


class _Profiles:
    def __init__(self, *names):
        self.profiles = {n: Profile(name=n) for n in names}


def test_ui_click_does_not_stay_loading_when_launched_elsewhere(bl, monkeypatch):
    state = AppState()
    # The API launched the profile between the UI's is_running check and its
    # start_thread call.
    monkeypatch.setattr(bl, "is_running", lambda name: False)
    bl.start_thread(Profile(name="p"), lambda m: None)

    launch_or_stop("p", _Profiles("p"), bl, state, lambda m: None)

    assert not state.is_loading("p")
    assert "p" not in state.failed
    assert bl.spawned == ["p"]


def test_a_raising_callback_does_not_leave_the_profile_busy(bl):
    def boom():
        raise RuntimeError("UI gone")

    with pytest.raises(RuntimeError):
        bl.start_thread(Profile(name="p"), lambda m: None, on_start=boom)
    with bl.exclusive("p"):
        pass
    assert bl.spawned == []


def test_an_old_session_ending_keeps_the_newer_launch(bl, monkeypatch):
    exits = []
    monkeypatch.setattr(
        launcher_mod, "wait_for_exit", lambda proc, name, notify: exits.append(notify)
    )
    bl.start_thread(Profile(name="p"), lambda m: None)
    # The first browser is gone but its exit callback has not run yet.
    bl._active_sessions.clear()
    bl._stop_notifiers.clear()
    bl.start_thread(Profile(name="p"), lambda m: None)

    exits[0]()

    assert bl.is_running("p")
    assert bl.spawned == ["p", "p"]


def test_api_launch_answers_409_when_the_browser_appeared_meanwhile(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from src.api.app import create_app
    from src.core.container import Container

    monkeypatch.chdir(tmp_path)
    container = Container()
    client = TestClient(create_app(container), base_url="http://127.0.0.1")
    assert client.post("/api/v1/profiles", json={"name": "p"}).status_code in (200, 201)
    monkeypatch.setattr(container.browser_launcher, "is_running", lambda name: False)
    monkeypatch.setattr(container.browser_launcher, "start_thread", lambda *a, **k: False)

    r = client.post("/api/v1/browser/p/launch")

    assert r.status_code == 409
    assert r.json()["detail"] == "Browser already running"


# --- recordings --------------------------------------------------------------


def _with_control(bl, name="p"):
    """A running profile whose control channel answers through a fake `_send`."""
    bl.start_thread(Profile(name=name), lambda m: None)
    bl._controls[name] = (1, "tok")
    sent, responses = [], {}

    def send(port, token, action, params, timeout):
        sent.append((action, params, timeout))
        response = responses.get(action)
        if isinstance(response, Exception):
            raise response
        return response

    bl._send = send
    return sent, responses


def test_profiles_that_never_record_stop_without_asking_the_runner(bl, monkeypatch):
    sent, _ = _with_control(bl)
    monkeypatch.setattr(launcher_mod, "terminate", lambda *a, **k: None)
    assert bl.stop_profile("p") is True
    assert sent == []


def test_stop_saves_a_running_recording_before_terminating(bl, monkeypatch):
    sent, responses = _with_control(bl)
    responses["record_start"] = {"file": "a.webm"}
    responses["record_stop"] = {"file": "a.webm", "bytes": 10}
    bl.control("p", "record_start", {"name": "a"})
    order = []
    monkeypatch.setattr(launcher_mod, "terminate", lambda *a, **k: order.append("terminate"))
    send = bl._send
    bl._send = lambda *a: order.append(a[2]) or send(*a)
    assert bl.stop_profile("p") is True
    assert order == ["record_stop", "terminate"]
    assert sent[-1] == ("record_stop", {"if_active": True}, STOP_TIMEOUT)


def test_finish_recording_never_raises(bl):
    _, responses = _with_control(bl)
    responses["record_start"] = {"file": "a.webm"}
    bl.control("p", "record_start", {})
    responses["record_stop"] = BrowserControlError("Browser did not respond", status=504)
    assert bl.finish_recording("p") is None


def test_recording_tracking_follows_the_runner(bl):
    _, responses = _with_control(bl)

    def attempt(action, error):
        responses[action] = error
        with pytest.raises(BrowserControlError):
            bl.control("p", action, {})
        return "p" in bl._recording

    # A start that timed out may still have started (e.g. after downloading ffmpeg),
    # and a 409 means another recording is running: both keep the mark.
    assert attempt("record_start", BrowserControlError("did not respond", status=504))
    assert attempt("record_start", BrowserControlError("already running", status=409))
    # A stop that timed out may still be saving; any other answer means nothing records.
    assert attempt("record_stop", BrowserControlError("did not respond", status=504))
    assert not attempt("record_stop", BrowserControlError("produced no file", status=422))
    # A start the runner refused leaves nothing recording.
    assert not attempt("record_start", BrowserControlError("Invalid name", status=400))
    responses["record_status"] = {"recording": True}
    bl.control("p", "record_status", {})
    assert "p" in bl._recording
    with bl._lock:  # the session ending forgets it
        bl._forget_control("p")
    assert "p" not in bl._recording


def test_a_stop_racing_a_start_still_saves_the_recording(bl):
    _, responses = _with_control(bl)
    seen_during_start = []
    send = bl._send

    def slow_start(port, token, action, params, timeout):
        if action == "record_start":
            # stop_profile could run now, while the runner is starting the recording.
            seen_during_start.append("p" in bl._recording)
        return send(port, token, action, params, timeout)

    bl._send = slow_start
    responses["record_start"] = {"file": "a.webm"}
    bl.control("p", "record_start", {})
    assert seen_during_start == [True]
