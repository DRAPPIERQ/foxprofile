"""Record what a profile's tab shows as a WebM video.

Built on Playwright's `page.screencast` (Playwright 1.59+): the browser sends its
own frames and Playwright encodes them with its ffmpeg build, on both engines,
without touching the page. One recording per profile at a time, on the tab that
is active when it starts. Playwright's ffmpeg is downloaded from Playwright's
CDN on the first recording if it is missing.

A recording stops by itself at FOXPROFILE_RECORDING_MAX_MINUTES or when free disk
space drops below FOXPROFILE_RECORDING_MIN_FREE_MB, so a forgotten recording
cannot fill the disk.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePath
from typing import Any

from ...core.config import RECORDING_MAX_MINUTES, RECORDING_MIN_FREE_MB, RECORDINGS_DIR
from ...utils.validation import is_reserved_name

EXTENSION = ".webm"
NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
# Frames are scaled down to fit this box, keeping the tab's aspect ratio.
MAX_SIZE = (1920, 1080)
FFMPEG_INSTALL_TIMEOUT = 100
# After a failed ffmpeg download, later recordings fail fast for this long.
FFMPEG_RETRY_SECONDS = 300
# How long callers of the control server wait: a first start may download ffmpeg,
# and a stop copies the whole video into place.
START_TIMEOUT = FFMPEG_INSTALL_TIMEOUT + 30
STOP_TIMEOUT = 60
# Playwright calls are bounded so that a wedged browser cannot hold the recorder
# (and every later record call) past what the caller waits for.
SCREENCAST_START_TIMEOUT = 20
SCREENCAST_STOP_TIMEOUT = STOP_TIMEOUT - 10
# How often a running recording checks its time limit and the free disk space.
WATCH_SECONDS = 10
FFMPEG_INSTALL_COMMAND = "python -m playwright install ffmpeg"
NAME_RULES = "use 1-80 letters, digits, '.', '_' or '-', starting with a letter or digit"


class RecordingError(Exception):
    """A recording could not be started, stopped or saved; `status` is the HTTP status."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def recordings_dir(profile_name: str) -> str:
    """Folder of a profile's recordings; the name must already be validated."""
    return os.path.join(os.getcwd(), RECORDINGS_DIR, profile_name)


def public_path(path: str) -> str:
    """How a path is shown to API and MCP clients: relative to the folder FoxProfile
    runs from, so responses do not reveal the account name or directory layout.
    A path outside it (an absolute FOXPROFILE_RECORDINGS_DIR) is the operator's own
    setting and is shown as is."""
    try:
        relative = os.path.relpath(path, os.getcwd())
    except ValueError:  # another drive on Windows
        return path
    if relative == os.pardir or relative.startswith(os.pardir + os.sep):
        return path
    # Forward slashes, so API responses look the same on every OS.
    return PurePath(relative).as_posix()


def scrub_paths(text: str) -> str:
    """Replace the working directory with "." and the home directory with "~" in a
    message sent to API clients. Only whole path prefixes match ("/srv/app" is not
    replaced inside "/srv/app-old"); on Windows also with forward slashes and in any case."""
    for prefix, shown in ((os.getcwd(), "."), (os.path.expanduser("~"), "~")):
        prefix = prefix.rstrip("\\/")
        if len(prefix) < 2:
            continue
        variants = {prefix, prefix.replace("\\", "/")} if os.name == "nt" else {prefix}
        for variant in sorted(variants, key=len, reverse=True):
            pattern = re.escape(variant) + r"(?=[\\/]|$|[\s'\",;:)\]])"
            flags = re.IGNORECASE if os.name == "nt" else 0
            text = re.sub(pattern, lambda _m, shown=shown: shown, text, flags=flags)
    return text


def check_name(name: str) -> str:
    """The file stem for a user-given recording name ("case-1" or "case-1.webm")."""
    stem = name[: -len(EXTENSION)] if name.lower().endswith(EXTENSION) else name
    if not NAME_PATTERN.match(stem) or is_reserved_name(stem):
        raise RecordingError(f"Invalid recording name '{name}': {NAME_RULES}")
    return stem


def is_recording_file(file_name: str) -> bool:
    """True for a file name this module could have written, e.g. "case-1.webm"."""
    if not file_name.endswith(EXTENSION):
        return False
    try:
        return check_name(file_name) + EXTENSION == file_name
    except RecordingError:
        return False


def default_name(now: datetime | None = None) -> str:
    return (now or datetime.now()).strftime("%Y%m%d-%H%M%S")


def unique_path(directory: str, stem: str) -> str:
    """A path in `directory` for `stem` that does not overwrite an existing video."""
    path = os.path.join(directory, stem + EXTENSION)
    n = 2
    # lexists: a dangling symlink must not be written through either.
    while os.path.lexists(path):
        path = os.path.join(directory, f"{stem}-{n}{EXTENSION}")
        n += 1
    return path


def make_private_dirs(directory: str) -> None:
    """Create a profile's recordings folder and its parent, readable by this user only.

    Videos can show logged-in accounts. Existing folders keep their permissions;
    Windows ignores the mode.
    """
    os.makedirs(os.path.dirname(directory), mode=0o700, exist_ok=True)
    os.makedirs(directory, mode=0o700, exist_ok=True)


def fit(width: int, height: int, bounds: tuple[int, int] = MAX_SIZE) -> dict[str, int]:
    """Scale a viewport down to fit `bounds`, keeping its aspect ratio and even sides."""
    scale = min(1.0, bounds[0] / width, bounds[1] / height)
    w, h = int(width * scale), int(height * scale)
    return {"width": max(2, w - w % 2), "height": max(2, h - h % 2)}


async def viewport_size(page: Any) -> dict[str, int] | None:
    """The tab's viewport, fitted to MAX_SIZE; None lets Playwright pick (800x800 box)."""
    try:
        width, height = await asyncio.wait_for(
            page.evaluate("() => [innerWidth, innerHeight]"), timeout=5
        )
        return fit(int(width), int(height))
    except Exception:
        return None


def disk_space(directory: str) -> tuple[int, int] | None:
    """(free, total) bytes of the disk holding `directory`, or None if it cannot be read."""
    try:
        usage = shutil.disk_usage(directory)
    except OSError:
        return None
    return usage.free, usage.total


def _compact(error: object, limit: int = 300) -> str:
    text = scrub_paths(" ".join(str(error).split()))
    text = text.split("Call log:", 1)[0].strip()
    return text[:limit] + ("..." if len(text) > limit else "")


def _missing_ffmpeg(exc: BaseException) -> bool:
    # Playwright: "Executable doesn't exist at .../ffmpeg-1011/ffmpeg-linux ...
    # Video rendering requires ffmpeg binary". The recording's own path (which can
    # contain "ffmpeg") is not enough on its own.
    text = str(exc).lower()
    return "ffmpeg" in text and ("executable doesn't exist" in text or "requires ffmpeg" in text)


# --- ffmpeg download ------------------------------------------------------------

_install_failure: tuple[float, str] | None = None


def install_ffmpeg() -> None:
    """Download Playwright's ffmpeg build, the version pinned by the installed Playwright.

    A failure is remembered for FFMPEG_RETRY_SECONDS so that each new recording
    does not wait for another download that is likely to fail the same way.
    """
    global _install_failure
    if _install_failure and time.monotonic() - _install_failure[0] < FFMPEG_RETRY_SECONDS:
        raise RecordingError(_install_failure[1], status=503)
    try:
        _run_ffmpeg_install()
    except RecordingError as e:
        _install_failure = (time.monotonic(), str(e))
        raise
    _install_failure = None


def _run_ffmpeg_install() -> None:
    command = [sys.executable, "-m", "playwright", "install", "ffmpeg"]
    # Its own process group, so a timeout also stops Playwright's Node driver child.
    group: dict[str, Any] = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        if os.name == "nt"
        else {"start_new_session": True}
    )
    try:
        proc = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **group
        )
    except OSError as e:
        raise RecordingError(
            f"Could not download ffmpeg ({_compact(e, 120)}); run: {FFMPEG_INSTALL_COMMAND}",
            status=503,
        ) from e
    try:
        out, err = proc.communicate(timeout=FFMPEG_INSTALL_TIMEOUT)
    except subprocess.TimeoutExpired as e:
        _kill_tree(proc)
        with contextlib.suppress(Exception):
            proc.communicate(timeout=5)
        raise RecordingError(
            f"Could not download ffmpeg (no answer after {FFMPEG_INSTALL_TIMEOUT} s); "
            f"run: {FFMPEG_INSTALL_COMMAND}",
            status=503,
        ) from e
    if proc.returncode != 0:
        detail = _compact(err or out, 200)
        raise RecordingError(
            f"Could not download ffmpeg: {detail}. Run: {FFMPEG_INSTALL_COMMAND}", status=503
        )


def _kill_tree(proc: subprocess.Popen) -> None:
    with contextlib.suppress(Exception):
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, timeout=10
            )
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(Exception):
        proc.kill()


# --- Playwright calls -------------------------------------------------------------


async def _start_screencast(page: Any, path: str, size: dict[str, int] | None) -> None:
    try:
        await asyncio.wait_for(
            page.screencast.start(path=path, size=size), timeout=SCREENCAST_START_TIMEOUT
        )
    except Exception:
        # After a failed start Playwright keeps the screencast marked as started
        # and refuses every later start on this page until stop() is called.
        with contextlib.suppress(Exception):
            await asyncio.wait_for(page.screencast.stop(), timeout=SCREENCAST_START_TIMEOUT)
        raise


async def _save_after_close(page: Any, path: str) -> bool:
    """Save a recording whose tab closed before stop().

    Playwright's public stop() needs the page, but the video it recorded up to
    the close survives as an artifact. It is only reachable through private
    attributes (checked with Playwright 1.62; tests/test_recording.py fails if
    they move), so any change there degrades to reporting the recording as lost.
    """
    impl = getattr(page.screencast, "_impl_obj", page.screencast)
    artifact = getattr(impl, "_artifact", None)
    if artifact is None:
        return False
    try:
        await asyncio.wait_for(artifact.save_as(path), timeout=SCREENCAST_STOP_TIMEOUT)
    except Exception:
        return False
    return os.path.exists(path)


@dataclass
class _Recording:
    page: Any
    path: str
    started: float


class Recorder:
    """The recording of one profile's tab. Used by the runner's control server."""

    def __init__(
        self,
        directory: str,
        installer: Any = install_ffmpeg,
        max_seconds: float = RECORDING_MAX_MINUTES * 60,
        min_free_bytes: int = int(RECORDING_MIN_FREE_MB * 1024 * 1024),
        watch_seconds: float = WATCH_SECONDS,
    ) -> None:
        self._dir = directory
        self._install = installer
        self._max_seconds = max_seconds
        self._min_free = min_free_bytes
        self._watch_seconds = watch_seconds
        self._active: _Recording | None = None
        self._starting: Any = None
        self._watchdog: asyncio.Task | None = None
        # A recording the watchdog stopped, returned by the next stop().
        self._auto_stopped: dict[str, Any] | None = None
        self._lock = asyncio.Lock()

    def is_recording(self, page: Any) -> bool:
        """True while `page` is being recorded or a recording on it is starting."""
        return page is self._starting or (self._active is not None and self._active.page is page)

    def status(self) -> dict[str, Any]:
        rec = self._active
        if rec is None:
            pending = {k: v for k, v in (self._auto_stopped or {}).items() if k != "status"}
            return {"recording": False, **pending}
        return {
            "recording": True,
            "file": os.path.basename(rec.path),
            "path": public_path(rec.path),
            "seconds": round(time.monotonic() - rec.started, 1),
            "tab_closed": rec.page.is_closed(),
        }

    def _disk_problem(self) -> str | None:
        """What lacks space for a recording, or None. Blocking: run it in a thread."""
        if not self._min_free:
            return None
        mb = self._min_free // (1024 * 1024)
        space = disk_space(self._dir)
        if space and space[0] < self._min_free:
            return f"the recordings folder has less than {mb} MB free"
        # Playwright writes the video to the temporary folder first and copies it at the
        # end. That folder is often a small RAM disk, so it only has to be not nearly full.
        space = disk_space(tempfile.gettempdir())
        if space and space[0] < min(self._min_free, space[1] // 10):
            return "the temporary folder is nearly full"
        return None

    async def start(self, page: Any, name: str | None = None) -> dict[str, Any]:
        async with self._lock:
            if self._active is not None:
                hint = " (its tab was closed)" if self._active.page.is_closed() else ""
                raise RecordingError(
                    f"A recording is already running{hint}; stop it first", status=409
                )
            stem = check_name(name) if name else default_name()
            make_private_dirs(self._dir)
            problem = await asyncio.to_thread(self._disk_problem)
            if problem:
                raise RecordingError(f"Not enough disk space to record: {problem}", status=507)
            path = unique_path(self._dir, stem)
            self._starting = page
            try:
                size = await viewport_size(page)
                await self._start_with_ffmpeg(page, path, size)
            finally:
                self._starting = None
            rec = _Recording(page, path, time.monotonic())
            self._active = rec
            self._auto_stopped = None
            self._watchdog = asyncio.create_task(self._watch(rec))
            return {
                "file": os.path.basename(path),
                "path": public_path(path),
                "url": page.url,
                "size": size,
                "max_minutes": round(self._max_seconds / 60, 1) if self._max_seconds else None,
            }

    async def _start_with_ffmpeg(self, page: Any, path: str, size: dict[str, int] | None) -> None:
        try:
            await _start_screencast(page, path, size)
            return
        except Exception as e:
            if not _missing_ffmpeg(e):
                raise RecordingError(f"Could not start recording: {_compact(e)}", status=422) from e
        print("Video recording needs Playwright's ffmpeg; downloading it once...", flush=True)
        await asyncio.to_thread(self._install)
        print("ffmpeg installed", flush=True)
        try:
            await _start_screencast(page, path, size)
        except Exception as e:
            raise RecordingError(f"Could not start recording: {_compact(e)}", status=422) from e

    async def stop(self, if_active: bool = False) -> dict[str, Any] | None:
        """Stop and save the recording. When idle, return what the watchdog stopped,
        else None if `if_active`, else an error."""
        async with self._lock:
            rec = self._active
            if rec is None:
                if self._auto_stopped:
                    result, self._auto_stopped = self._auto_stopped, None
                    if "error" in result:
                        raise RecordingError(
                            f"The recording was stopped ({result['stopped_by']}) but could not "
                            f"be saved: {result['error']}",
                            status=result["status"],
                        )
                    return result
                if if_active:
                    return None
                raise RecordingError("No recording is running", status=409)
            return await self._stop_locked(rec)

    async def _stop_locked(self, rec: _Recording) -> dict[str, Any]:
        watchdog, self._watchdog = self._watchdog, None
        if watchdog is not None and watchdog is not asyncio.current_task():
            watchdog.cancel()
        seconds = round(time.monotonic() - rec.started, 1)
        # The recording stays active (its tab protected, status "recording") until it
        # is saved, and is cleared however the save ends.
        try:
            try:
                await asyncio.wait_for(rec.page.screencast.stop(), timeout=SCREENCAST_STOP_TIMEOUT)
            except Exception as e:
                # A recorded tab that was closed (by the page, the user or the browser)
                # can still be saved; a stop that timed out on an open tab cannot.
                saved = rec.page.is_closed() and await _save_after_close(rec.page, rec.path)
                if not saved:
                    reason = _compact(e) or type(e).__name__
                    raise RecordingError(
                        f"The recording could not be saved: {reason}", status=410
                    ) from e
            try:
                size = os.path.getsize(rec.path)
            except OSError as e:
                raise RecordingError("The recording produced no file", status=422) from e
        finally:
            self._active = None
        return {
            "file": os.path.basename(rec.path),
            "path": public_path(rec.path),
            "bytes": size,
            "seconds": seconds,
        }

    async def _watch(self, rec: _Recording) -> None:
        """Stop `rec` at the time limit, or when free disk space runs low."""
        while True:
            wait = self._watch_seconds
            if self._max_seconds:
                left = rec.started + self._max_seconds - time.monotonic()
                wait = max(0.0, min(wait, left))
            await asyncio.sleep(wait)
            if self._active is not rec:
                return
            if self._max_seconds and time.monotonic() - rec.started >= self._max_seconds:
                reason = f"time limit ({self._max_seconds / 60:g} min)"
            else:
                problem = await asyncio.to_thread(self._disk_problem)
                if not problem:
                    continue
                reason = f"low disk space: {problem}"
            async with self._lock:
                if self._active is not rec:
                    return
                try:
                    result = await self._stop_locked(rec)
                except Exception as e:
                    # Reported by the next stop(), so the client learns it was lost.
                    error = str(e) if isinstance(e, RecordingError) else _compact(e)
                    status = e.status if isinstance(e, RecordingError) else 422
                    self._auto_stopped = {
                        "file": os.path.basename(rec.path),
                        "stopped_by": reason,
                        "error": error,
                        "status": status,
                    }
                    print(f"Recording stopped ({reason}) but not saved: {error}", flush=True)
                    return
                self._auto_stopped = {**result, "stopped_by": reason}
            print(f"Recording {result['file']} stopped: {reason}", flush=True)
            return

    async def finish(self) -> dict[str, Any] | None:
        """Best-effort stop for shutdown: saves a running recording, never raises."""
        try:
            result = await self.stop(if_active=True)
        except Exception as e:
            print(f"Recording lost on shutdown: {_compact(e, 200)}", flush=True)
            return None
        if result:
            print(f"Recording saved: {result['file']}", flush=True)
        return result
