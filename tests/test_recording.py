import asyncio
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.services.browser import recording
from src.services.browser.recording import Recorder, RecordingError

WEBM = b"\x1a\x45\xdf\xa3fake-webm"
CLOSED = "Screencast.stop: Target page, context or browser has been closed"
NO_FFMPEG = "Screencast.start: Executable doesn't exist at /x/ffmpeg-1011/ffmpeg-linux"


class FakeArtifact:
    def __init__(self):
        self.saved = []

    async def save_as(self, path):
        self.saved.append(path)
        with open(path, "wb") as f:
            f.write(WEBM)


class FakeScreencast:
    def __init__(self, page):
        self._page = page
        self.start_errors: list[Exception] = []
        self.starts: list[tuple] = []
        self.stops = 0
        self._path = None
        self._artifact = None

    async def start(self, path=None, size=None):
        self.starts.append((path, size))
        if self.start_errors:
            raise self.start_errors.pop(0)
        self._path = path
        self._artifact = FakeArtifact()

    async def stop(self):
        self.stops += 1
        if self._page.closed:
            raise RuntimeError(CLOSED)
        if self._path:
            with open(self._path, "wb") as f:
                f.write(WEBM)
        self._path = None


class FakePage:
    def __init__(self, viewport=(1920, 964)):
        self.url = "https://p.test/"
        self.closed = False
        self.viewport = viewport
        self.screencast = FakeScreencast(self)

    def is_closed(self):
        return self.closed

    async def title(self):
        return "T"

    async def bring_to_front(self):
        pass

    async def close(self):
        self.closed = True

    async def evaluate(self, script):
        if self.viewport is None:
            raise RuntimeError("no window")
        return list(self.viewport)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def recorder(tmp_path):
    installs = []
    r = Recorder(str(tmp_path / "rec"), installer=lambda: installs.append(1), min_free_bytes=0)
    r.installs = installs
    return r


# --- names and paths ---------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "stem"),
    [("case-1", "case-1"), ("login_ok.v2", "login_ok.v2"), ("case1.webm", "case1"), ("A", "A")],
)
def test_valid_names(name, stem):
    assert recording.check_name(name) == stem


@pytest.mark.parametrize(
    "name",
    ["", ".webm", "../x", "a/b", "a\\b", ".hidden", "-x", "a b", "x" * 81, "CON", "nul.v1", "é"],
)
def test_invalid_names(name):
    with pytest.raises(RecordingError) as e:
        recording.check_name(name)
    assert e.value.status == 400


@pytest.mark.parametrize(
    ("file", "ok"),
    [
        ("case-1.webm", True),
        ("case-1", False),
        ("x.mp4", False),
        ("../x.webm", False),
        ("CON.webm", False),
    ],
)
def test_is_recording_file(file, ok):
    assert recording.is_recording_file(file) is ok


def test_default_name_is_a_timestamp():
    assert recording.default_name(datetime(2026, 1, 2, 3, 4, 5)) == "20260102-030405"


def test_unique_path_never_overwrites(tmp_path):
    first = recording.unique_path(str(tmp_path), "case")
    assert first == str(tmp_path / "case.webm")
    open(first, "wb").close()
    second = recording.unique_path(str(tmp_path), "case")
    assert second == str(tmp_path / "case-2.webm")
    open(second, "wb").close()
    assert recording.unique_path(str(tmp_path), "case") == str(tmp_path / "case-3.webm")


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        ((1920, 964), {"width": 1920, "height": 964}),
        ((3840, 2160), {"width": 1920, "height": 1080}),
        ((1295, 1307), {"width": 1070, "height": 1080}),
        ((801, 601), {"width": 800, "height": 600}),
    ],
)
def test_fit_keeps_ratio_within_bounds_and_even_sides(size, expected):
    assert recording.fit(*size) == expected


def test_recordings_dir_is_per_profile(monkeypatch, tmp_path):
    monkeypatch.setattr(recording, "RECORDINGS_DIR", str(tmp_path))
    assert recording.recordings_dir("shop-01") == os.path.join(str(tmp_path), "shop-01")


# --- Recorder ----------------------------------------------------------------


def test_start_records_the_viewport_and_stop_saves_the_file(recorder):
    page = FakePage()
    started = run(recorder.start(page, "case-1"))
    path = started["path"]
    assert started["file"] == "case-1.webm"
    assert page.screencast.starts == [(path, {"width": 1920, "height": 964})]
    assert recorder.status()["recording"] is True
    assert recorder.is_recording(page)

    saved = run(recorder.stop())
    assert saved["path"] == path and saved["bytes"] == len(WEBM)
    assert saved["seconds"] >= 0
    assert recorder.status() == {"recording": False}


def test_default_name_and_size_fallback(recorder):
    page = FakePage(viewport=None)
    started = run(recorder.start(page))
    assert started["file"].endswith(".webm") and started["file"][0].isdigit()
    # Without a viewport Playwright picks the size.
    assert page.screencast.starts[0][1] is None


def test_one_recording_at_a_time(recorder):
    run(recorder.start(FakePage(), "a"))
    with pytest.raises(RecordingError) as e:
        run(recorder.start(FakePage(), "b"))
    assert e.value.status == 409


def test_concurrent_starts_cannot_both_record(recorder):
    async def both():
        return await asyncio.gather(
            recorder.start(FakePage(), "a"), recorder.start(FakePage(), "b"), return_exceptions=True
        )

    results = run(both())
    assert sum(isinstance(r, RecordingError) for r in results) == 1


def test_stop_when_idle(recorder):
    with pytest.raises(RecordingError) as e:
        run(recorder.stop())
    assert e.value.status == 409
    assert run(recorder.stop(if_active=True)) is None


def test_failed_start_resets_playwright_and_reports(recorder):
    page = FakePage()
    page.screencast.start_errors = [RuntimeError("boom")]
    with pytest.raises(RecordingError, match="boom") as e:
        run(recorder.start(page, "a"))
    assert e.value.status == 422
    # stop() clears Playwright's "already started" flag left by the failed start.
    assert page.screencast.stops == 1
    assert recorder.status() == {"recording": False}
    assert recorder.installs == []


def test_missing_ffmpeg_is_installed_once_then_recording_starts(recorder):
    page = FakePage()
    page.screencast.start_errors = [RuntimeError(NO_FFMPEG)]
    run(recorder.start(page, "a"))
    assert recorder.installs == [1]
    assert len(page.screencast.starts) == 2
    assert recorder.status()["recording"] is True


def test_failed_ffmpeg_install_is_reported(tmp_path):
    def broken():
        raise RecordingError("Could not download ffmpeg", status=503)

    r = Recorder(str(tmp_path / "rec"), installer=broken, min_free_bytes=0)
    page = FakePage()
    page.screencast.start_errors = [RuntimeError(NO_FFMPEG)]
    with pytest.raises(RecordingError) as e:
        run(r.start(page, "a"))
    assert e.value.status == 503
    assert r.status() == {"recording": False}


def test_closed_tab_is_saved_from_playwright_artifact(recorder):
    page = FakePage()
    started = run(recorder.start(page, "a"))
    page.closed = True
    assert recorder.status()["tab_closed"] is True
    with pytest.raises(RecordingError, match="its tab was closed"):
        run(recorder.start(FakePage(), "b"))
    saved = run(recorder.stop())
    assert saved["path"] == started["path"]
    assert page.screencast._artifact.saved == [started["path"]]


def test_closed_tab_without_artifact_is_reported_lost(recorder):
    page = FakePage()
    run(recorder.start(page, "a"))
    page.closed = True
    page.screencast._artifact = None
    with pytest.raises(RecordingError, match="could not be saved") as e:
        run(recorder.stop())
    assert e.value.status == 410
    assert recorder.status() == {"recording": False}


def test_finish_saves_and_never_raises(recorder, capsys):
    assert run(recorder.finish()) is None
    page = FakePage()
    run(recorder.start(page, "a"))
    assert run(recorder.finish())["file"] == "a.webm"
    run(recorder.start(page, "b"))
    page.closed = True
    page.screencast._artifact = None
    assert run(recorder.finish()) is None
    assert "Recording lost" in capsys.readouterr().out


# --- ffmpeg install ----------------------------------------------------------


@pytest.fixture
def popen(monkeypatch):
    """Fake the installer's subprocess. Set `.outcome` to (stdout, stderr, returncode),
    an OSError, or "hang"."""
    monkeypatch.setattr(recording, "_install_failure", None)
    state = SimpleNamespace(calls=[], outcome=("", "", 0), killed=[])

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            state.calls.append((cmd, kwargs))
            if isinstance(state.outcome, OSError):
                raise state.outcome
            self.pid = 4242
            self.returncode = None
            self.timed_out = False

        def communicate(self, timeout=None):
            if state.outcome == "hang":
                if not self.timed_out:
                    self.timed_out = True
                    raise subprocess.TimeoutExpired("playwright", timeout)
                self.returncode = -9
                return "", ""
            out, err, self.returncode = state.outcome
            return out, err

        def kill(self):
            state.killed.append("kill")

    monkeypatch.setattr(recording.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(
        recording.subprocess, "run", lambda cmd, **k: state.killed.append(("taskkill", cmd))
    )
    monkeypatch.setattr(
        recording.os, "killpg", lambda pid, sig: state.killed.append(("killpg", pid)), raising=False
    )
    return state


def test_install_ffmpeg_runs_playwright_install_in_its_own_process_group(popen):
    recording.install_ffmpeg()
    cmd, kwargs = popen.calls[0]
    assert cmd[1:] == ["-m", "playwright", "install", "ffmpeg"]
    assert kwargs.get("start_new_session") or kwargs.get("creationflags")


@pytest.mark.parametrize("outcome", [("", "Error: download failed", 1), OSError("no python")])
def test_install_ffmpeg_failure_names_the_manual_command(popen, outcome):
    popen.outcome = outcome
    with pytest.raises(RecordingError, match="playwright install ffmpeg") as e:
        recording.install_ffmpeg()
    assert e.value.status == 503


def test_install_ffmpeg_timeout_stops_the_whole_process_tree(popen):
    popen.outcome = "hang"
    with pytest.raises(RecordingError, match="no answer"):
        recording.install_ffmpeg()
    assert ("killpg", 4242) in popen.killed or any(k[0] == "taskkill" for k in popen.killed)


def test_a_failed_install_is_not_retried_for_a_while(popen, monkeypatch):
    popen.outcome = ("", "Error: offline", 1)
    with pytest.raises(RecordingError):
        recording.install_ffmpeg()
    popen.outcome = ("", "", 0)
    with pytest.raises(RecordingError, match="offline"):
        recording.install_ffmpeg()  # fails fast with the same message
    assert len(popen.calls) == 1
    # Once the wait is over, the next recording tries again and succeeds.
    failed_at, message = recording._install_failure
    monkeypatch.setattr(
        recording, "_install_failure", (failed_at - recording.FFMPEG_RETRY_SECONDS, message)
    )
    recording.install_ffmpeg()
    assert len(popen.calls) == 2
    assert recording._install_failure is None


# --- review fixes: bounded Playwright calls, narrower ffmpeg match, links ----------


class HangingScreencast(FakeScreencast):
    def __init__(self, page, hang_start=False, hang_stop=False):
        super().__init__(page)
        self.hang_start, self.hang_stop = hang_start, hang_stop

    async def start(self, path=None, size=None):
        if self.hang_start:
            await asyncio.sleep(30)
        await super().start(path, size)

    async def stop(self):
        if self.hang_stop:
            await asyncio.sleep(30)
        await super().stop()


@pytest.fixture
def short_timeouts(monkeypatch):
    monkeypatch.setattr(recording, "SCREENCAST_START_TIMEOUT", 0.05)
    monkeypatch.setattr(recording, "SCREENCAST_STOP_TIMEOUT", 0.05)


def test_a_hung_stop_frees_the_recorder(recorder, short_timeouts):
    page = FakePage()
    page.screencast = HangingScreencast(page, hang_stop=True)
    run(recorder.start(page, "a"))
    with pytest.raises(RecordingError, match="TimeoutError") as e:
        run(recorder.stop())
    assert e.value.status == 410
    page.screencast.hang_stop = False
    assert run(recorder.start(page, "b"))["file"] == "b.webm"


def test_a_hung_start_frees_the_recorder(recorder, short_timeouts):
    page = FakePage()
    page.screencast = HangingScreencast(page, hang_start=True)
    with pytest.raises(RecordingError) as e:
        run(recorder.start(page, "a"))
    assert e.value.status == 422
    assert recorder.status() == {"recording": False}
    assert recorder.installs == []


@pytest.mark.parametrize(
    ("message", "missing"),
    [
        (NO_FFMPEG, True),
        ("Screencast.start: Video rendering requires ffmpeg binary", True),
        ("Screencast.start: Target closed while writing /rec/ffmpeg-test.webm", False),
        ("boom", False),
    ],
)
def test_only_playwright_missing_ffmpeg_error_triggers_the_download(message, missing):
    assert recording._missing_ffmpeg(RuntimeError(message)) is missing


def test_unique_path_skips_a_dangling_link(tmp_path):
    try:
        os.symlink(tmp_path / "nowhere", tmp_path / "case.webm")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks need privileges on this OS")
    assert recording.unique_path(str(tmp_path), "case") == str(tmp_path / "case-2.webm")


def test_playwright_keeps_the_artifact_where_the_closed_tab_save_reads_it():
    """_save_after_close reads private Playwright attributes; fail here if they move."""
    import inspect

    from playwright._impl import _screencast
    from playwright._impl._impl_to_api_mapping import ImplWrapper
    from playwright.async_api import _generated

    # page.screencast is a public wrapper that keeps the implementation in _impl_obj.
    assert issubclass(_generated.Screencast, ImplWrapper)
    impl = object()
    assert ImplWrapper(impl)._impl_obj is impl
    assert "self._artifact" in inspect.getsource(_screencast.Screencast.__init__)
    stop = inspect.getsource(_screencast.Screencast.stop)
    # A stop that fails on a closed tab raises before the artifact is dropped.
    assert stop.index('"screencastStop"') < stop.index("self._artifact = None")


# --- limits, private folders, public paths -------------------------------------------


def test_time_limit_stops_and_saves_the_recording(tmp_path, capsys):
    r = Recorder(str(tmp_path / "rec"), max_seconds=0.1, min_free_bytes=0, watch_seconds=0.02)

    async def scenario():
        started = await r.start(FakePage(), "long")
        await _until(lambda: not r.status()["recording"])
        status = r.status()
        saved = await r.stop()
        return started, status, saved

    started, status, saved = run(scenario())
    assert started["max_minutes"] == round(0.1 / 60, 1)
    assert status["recording"] is False and status["stopped_by"].startswith("time limit")
    assert saved["file"] == "long.webm" and saved["stopped_by"].startswith("time limit")
    assert "stopped: time limit" in capsys.readouterr().out
    with pytest.raises(RecordingError):
        run(r.stop())  # the auto-stopped result is handed over once


def _disks(monkeypatch, tmp_path, recordings, temp):
    """Fake (free, total) for the recordings folder and the temporary folder."""
    space = {"rec": recordings, "tmp": temp}
    monkeypatch.setattr(
        recording,
        "disk_space",
        lambda d: space["rec"] if str(d).startswith(str(tmp_path)) else space["tmp"],
    )
    return space


def test_no_recording_starts_when_the_recordings_disk_is_nearly_full(tmp_path, monkeypatch):
    _disks(
        monkeypatch,
        tmp_path,
        (10 * 1024 * 1024, 100 * 1024 * 1024 * 1024),
        (50 * 1024 * 1024 * 1024, 100 * 1024 * 1024 * 1024),
    )
    r = Recorder(str(tmp_path / "rec"), min_free_bytes=1024 * 1024 * 1024)
    with pytest.raises(RecordingError, match="recordings folder has less than 1024 MB") as e:
        run(r.start(FakePage(), "a"))
    assert e.value.status == 507


def test_a_small_temporary_folder_is_fine_unless_nearly_full(tmp_path, monkeypatch):
    # A 512 MB RAM-backed /tmp with 300 MB free must not block recording.
    space = _disks(
        monkeypatch,
        tmp_path,
        (50 * 1024 * 1024 * 1024, 100 * 1024 * 1024 * 1024),
        (300 * 1024 * 1024, 512 * 1024 * 1024),
    )
    r = Recorder(str(tmp_path / "rec"), min_free_bytes=1024 * 1024 * 1024)
    run(r.start(FakePage(), "a"))
    run(r.stop())
    space["tmp"] = (20 * 1024 * 1024, 512 * 1024 * 1024)
    with pytest.raises(RecordingError, match="temporary folder is nearly full"):
        run(r.start(FakePage(), "b"))


def test_recording_stops_when_the_disk_fills_up(tmp_path, monkeypatch):
    space = _disks(
        monkeypatch,
        tmp_path,
        (50 * 1024 * 1024 * 1024, 100 * 1024 * 1024 * 1024),
        (50 * 1024 * 1024 * 1024, 100 * 1024 * 1024 * 1024),
    )
    r = Recorder(str(tmp_path / "rec"), min_free_bytes=1024 * 1024 * 1024, watch_seconds=0.02)

    async def scenario():
        await r.start(FakePage(), "a")
        space["rec"] = (100 * 1024 * 1024, 100 * 1024 * 1024 * 1024)
        await _until(lambda: not r.status()["recording"])
        return await r.stop(if_active=True)

    saved = run(scenario())
    assert saved["stopped_by"].startswith("low disk space: the recordings folder")


def test_disk_space_reads_free_and_total(tmp_path):
    assert recording.disk_space(str(tmp_path / "missing")) is None
    free, total = recording.disk_space(str(tmp_path))
    assert 0 < free <= total


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_recordings_folders_are_private(recorder, tmp_path):
    run(recorder.start(FakePage(), "a"))
    assert (tmp_path / "rec").stat().st_mode & 0o777 == 0o700


def test_paths_shown_to_clients_are_relative_to_the_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    inside = os.path.join(str(tmp_path), "recordings", "p", "a.webm")
    assert recording.public_path(inside) == "recordings/p/a.webm"
    outside = os.path.join(os.path.dirname(str(tmp_path)), "videos", "a.webm")
    assert recording.public_path(outside) == outside
    home = os.path.expanduser("~")
    message = recording._compact(f"failed at {inside} and {home}/.cache/x")
    assert str(tmp_path) not in message and home not in message


def test_start_returns_a_relative_path_by_default(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = Recorder(os.path.join(str(tmp_path), "recordings", "p"), min_free_bytes=0)
    started = run(r.start(FakePage(), "a"))
    assert started["path"] == "recordings/p/a.webm"


def test_a_tab_counts_as_recorded_while_its_recording_starts(recorder):
    page = FakePage()

    class SlowScreencast(FakeScreencast):
        async def start(self, path=None, size=None):
            await self.release.wait()
            await super().start(path, size)

    page.screencast = SlowScreencast(page)

    async def scenario():
        page.screencast.release = asyncio.Event()
        task = asyncio.create_task(recorder.start(page, "a"))
        await asyncio.sleep(0.05)
        during = recorder.is_recording(page)
        page.screencast.release.set()
        await task
        return during

    assert run(scenario()) is True
    assert recorder.is_recording(page)


async def _until(condition, timeout=5.0):
    """Wait for `condition()` without depending on how fast the machine is."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


# --- review round 3 ------------------------------------------------------------------


def test_a_failed_auto_stop_is_reported_by_the_next_stop(tmp_path):
    r = Recorder(str(tmp_path / "rec"), max_seconds=0.05, min_free_bytes=0, watch_seconds=0.01)
    page = FakePage()

    async def scenario():
        await r.start(page, "lost")
        page.closed = True
        page.screencast._artifact = None  # nothing left to save
        await _until(lambda: not r.status()["recording"])
        return r.status()

    status = run(scenario())
    assert status["stopped_by"].startswith("time limit") and "could not be saved" in status["error"]
    with pytest.raises(RecordingError, match="stopped .*time limit.* could not be saved") as e:
        run(r.stop(if_active=True))
    assert e.value.status == 410
    assert run(r.stop(if_active=True)) is None  # reported once


def test_a_manual_stop_before_the_limit_leaves_nothing_behind(tmp_path):
    r = Recorder(str(tmp_path / "rec"), max_seconds=0.1, min_free_bytes=0, watch_seconds=0.01)

    async def scenario():
        await r.start(FakePage(), "a")
        saved = await r.stop()
        await asyncio.sleep(0.2)  # past the limit: the cancelled watchdog must not act
        return saved, r.status()

    saved, status = run(scenario())
    assert "stopped_by" not in saved
    assert status == {"recording": False}


def test_the_tab_stays_protected_while_its_recording_is_saved(recorder):
    page = FakePage()

    class SlowStop(FakeScreencast):
        async def stop(self):
            await self.release.wait()
            await super().stop()

    page.screencast = SlowStop(page)

    async def scenario():
        page.screencast.release = asyncio.Event()
        await recorder.start(page, "a")
        task = asyncio.create_task(recorder.stop())
        await asyncio.sleep(0.05)
        during = (recorder.is_recording(page), recorder.status()["recording"])
        page.screencast.release.set()
        await task
        return during

    assert run(scenario()) == (True, True)
    assert not recorder.is_recording(page)


def test_public_path_keeps_folders_that_start_with_two_dots(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    inside = os.path.join(str(tmp_path), "..backup", "a.webm")
    assert recording.public_path(inside) == "..backup/a.webm"


def test_scrub_paths_only_replaces_whole_path_prefixes(tmp_path, monkeypatch):
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.chdir(project)
    out = recording.scrub_paths(f"open {project}/x and {tmp_path}/proj-old/y")
    assert out.startswith("open ./x and ")
    # "proj" is not cut out of "proj-old" (the home folder may still be, on Windows).
    assert out.endswith("proj-old/y") and "./-old" not in out


@pytest.mark.parametrize(
    ("value", "expected"), [("0.5", 0.5), ("-3", 0.0), ("abc", 60), ("", 60), ("90", 90.0)]
)
def test_recording_limits_read_from_the_environment(monkeypatch, value, expected):
    from src.core import config

    monkeypatch.setenv("FOXPROFILE_RECORDING_MAX_MINUTES", value)
    assert config._non_negative("RECORDING_MAX_MINUTES", 60) == expected


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
def test_kill_tree_stops_the_installer_and_its_children(tmp_path):
    import time

    marker = tmp_path / "child.pid"
    script = (
        "import subprocess, sys, time; "
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        f"open({str(marker)!r}, 'w').write(str(p.pid)); time.sleep(60)"
    )
    proc = subprocess.Popen([sys.executable, "-c", script], start_new_session=True)
    deadline = time.monotonic() + 10
    while not marker.exists() or not marker.read_text():
        assert time.monotonic() < deadline, "child did not start"
        time.sleep(0.05)
    child = int(marker.read_text())
    recording._kill_tree(proc)
    proc.wait(timeout=5)
    deadline = time.monotonic() + 5
    while True:
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break  # the grandchild is gone too
        # A killed child can linger as a zombie until init reaps it.
        stat = Path(f"/proc/{child}/stat")
        if not stat.exists() or stat.read_text().split()[2] == "Z":
            break
        assert time.monotonic() < deadline, "the installer's child survived"
        time.sleep(0.05)
