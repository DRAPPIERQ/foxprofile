import atexit
import contextlib
import json
import subprocess
import threading
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from typing import Any

from ...core.logging import get_logger
from ...core.strings import get_string
from ...models.profile import Profile
from .control import TOKEN_HEADER
from .process import spawn_browser, terminate, wait_for_exit
from .recording import STOP_TIMEOUT

logger = get_logger("browser.launcher")

_NOISY_PREFIXES = (
    "- [pid=",
    "console.error:",
    "Crash Annotation",
    "JavaScript error:",
    "WARNING: At least one completion condition",
)


class _LaunchResult:
    """Outcome of one launch, settled once the browser is ready or has failed."""

    def __init__(self) -> None:
        self.settled = threading.Event()
        self.ok = False
        self.error: str | None = None
        self._lock = threading.Lock()

    def settle(self, ok: bool, error: str | None = None) -> None:
        with self._lock:
            if self.settled.is_set():
                return
            self.ok = ok
            self.error = error
            self.settled.set()


class ProfileBusyError(Exception):
    """The profile's browser is running or another operation holds its data dir."""


class BrowserControlError(Exception):
    """A page action failed or the profile has no controllable browser."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class BrowserLauncher:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active_sessions: dict[str, subprocess.Popen] = {}
        self._stop_notifiers: dict[str, threading.Event] = {}
        self._launch_results: dict[str, _LaunchResult] = {}
        self._busy: set[str] = set()
        self._controls: dict[str, tuple[int, str]] = {}
        # Profiles whose runner may be recording a video, so that stopping one
        # saves it first while profiles that never record stop as before.
        self._recording: set[str] = set()
        atexit.register(self.shutdown_all)

    def _forget_control(self, profile_name: str) -> None:
        """Drop a finished session's control channel. Call with self._lock held."""
        self._controls.pop(profile_name, None)
        self._recording.discard(profile_name)

    @contextlib.contextmanager
    def exclusive(self, profile_name: str) -> Iterator[None]:
        """Hold a stopped profile's data dir; launches are refused meanwhile."""
        with self._lock:
            if profile_name in self._busy:
                raise ProfileBusyError(profile_name)
            proc = self._active_sessions.get(profile_name)
            if proc is not None and proc.poll() is None:
                raise ProfileBusyError(profile_name)
            self._busy.add(profile_name)
        try:
            yield
        finally:
            with self._lock:
                self._busy.discard(profile_name)

    def wait_for_launch(self, profile_name: str, timeout: float) -> tuple[bool, str | None]:
        """Block until the latest launch of a profile is ready or has failed.

        Returns (ok, error). A launch still pending after `timeout` reports
        (False, "timeout") while the browser keeps starting in the background.
        """
        with self._lock:
            result = self._launch_results.get(profile_name)
        if result is None:
            return False, "No launch in progress"
        if not result.settled.wait(timeout):
            return False, "timeout"
        return result.ok, result.error

    def shutdown_all(self) -> None:
        with self._lock:
            for name, proc in list(self._active_sessions.items()):
                notifier = self._stop_notifiers.pop(name, None)
                if notifier:
                    notifier.set()
                terminate(proc, name)
            self._active_sessions.clear()
        logger.info("All browser sessions terminated")

    def start_thread(
        self,
        profile: Profile,
        log_callback: Callable[[str], None],
        on_start: Callable[[], None] | None = None,
        on_ready: Callable[[], None] | None = None,
        on_stop: Callable[[], None] | None = None,
    ) -> bool:
        """Launch a profile's browser in the background.

        Returns False, without calling any callback, when the profile already
        has a browser. Otherwise returns True and later calls on_ready or
        on_stop, also when spawning fails. Raises ProfileBusyError while
        another launch or a stopped-profile operation holds the profile.
        """
        with self._lock:
            if profile.name in self._active_sessions:
                return False
            if profile.name in self._busy:
                raise ProfileBusyError(profile.name)
            # Reserve the profile until the process is registered, so two
            # concurrent launches cannot both spawn a browser.
            self._busy.add(profile.name)

        stop_event = threading.Event()
        notify_lock = threading.Lock()
        result = _LaunchResult()

        def notify_stopped() -> None:
            with notify_lock:
                if stop_event.is_set():
                    return
                stop_event.set()
                result.settle(False, "Browser exited before it was ready")
                with self._lock:
                    # A newer launch may already own this name; leave it alone.
                    if self._stop_notifiers.get(profile.name) is stop_event:
                        self._forget_control(profile.name)
                        self._active_sessions.pop(profile.name, None)
                        self._stop_notifiers.pop(profile.name, None)
                log_callback(get_string("session_ended", name=profile.name))
                logger.info(f"Session ended for profile: {profile.name}")
                if on_stop:
                    on_stop()

        registered = False
        try:
            with self._lock:
                self._launch_results[profile.name] = result
            log_callback(get_string("starting_profile", name=profile.name, os=profile.os_type))
            logger.info(f"Starting browser for profile: {profile.name}")
            if on_start:
                on_start()

            try:
                proc = spawn_browser(profile)
                with self._lock:
                    self._active_sessions[profile.name] = proc
                    self._stop_notifiers[profile.name] = stop_event
                    self._busy.discard(profile.name)
                    registered = True

                threading.Thread(
                    target=self._monitor_process,
                    args=(proc, profile.name, log_callback, on_ready, notify_stopped, result),
                    daemon=True,
                ).start()
                threading.Thread(
                    target=wait_for_exit,
                    args=(proc, profile.name, notify_stopped),
                    daemon=True,
                ).start()
            except Exception as e:
                logger.exception(f"Error starting browser for {profile.name}: {e}")
                result.settle(False, str(e))
                with self._lock:
                    self._busy.discard(profile.name)
                log_callback(get_string("error_starting", error=e))
                if on_stop:
                    on_stop()
        finally:
            # Never keep the reservation when a callback raised before the
            # process was registered; the profile would stay busy for good.
            if not registered:
                with self._lock:
                    self._busy.discard(profile.name)
        return True

    def stop_profile(self, profile_name: str, timeout: int = 2) -> bool:
        # Save a running recording while the browser can still finish it: the
        # terminate window below is short, and a hard kill on Windows.
        self.finish_recording(profile_name)
        with self._lock:
            if profile_name not in self._active_sessions:
                return False
            proc = self._active_sessions.pop(profile_name)
            notifier = self._stop_notifiers.pop(profile_name, None)
            self._forget_control(profile_name)
        if notifier:
            notifier.set()
        terminate(proc, profile_name, timeout)
        logger.info("Stopped browser for profile: %s", profile_name)
        return True

    def control(
        self,
        profile_name: str,
        action: str,
        params: dict[str, Any] | None = None,
        timeout: float = 60,
    ) -> Any:
        """Run a page action in a running profile's browser and return its result."""
        with self._lock:
            endpoint = self._controls.get(profile_name)
        if not self.is_running(profile_name):
            raise BrowserControlError("Browser is not running", status=409)
        if endpoint is None:
            # Process is up but has not announced its control channel yet.
            raise BrowserControlError("Browser is still starting", status=503)
        if action == "record_start":
            # Marked before sending, so a stop racing this start still saves it.
            self._track_recording(profile_name, True)
        try:
            result = self._send(*endpoint, action, params or {}, timeout)
        except BrowserControlError as e:
            # On a timeout (504) the runner may still be starting or stopping, so the
            # mark stays and a stop asks the runner. A 409 on start means another
            # recording is running. Any other error means nothing is recording.
            unknown = e.status == 504 or (action == "record_start" and e.status == 409)
            if action in ("record_start", "record_stop") and not unknown:
                self._track_recording(profile_name, False)
            raise
        if action == "record_stop":
            self._track_recording(profile_name, False)
        elif action == "record_status":
            self._track_recording(profile_name, bool(result.get("recording")))
        return result

    def finish_recording(self, profile_name: str) -> dict[str, Any] | None:
        """Stop and save the profile's recording if one may be running. Never raises."""
        with self._lock:
            if profile_name not in self._recording:
                return None
        try:
            return self.control(
                profile_name, "record_stop", {"if_active": True}, timeout=STOP_TIMEOUT
            )
        except BrowserControlError as e:
            logger.warning("Could not save the recording of %s: %s", profile_name, e)
            return None

    def _track_recording(self, profile_name: str, recording: bool) -> None:
        with self._lock:
            if recording and profile_name in self._controls:
                self._recording.add(profile_name)
            elif not recording:
                self._recording.discard(profile_name)

    def _send(
        self, port: int, token: str, action: str, params: dict[str, Any], timeout: float
    ) -> Any:
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/action",
            data=json.dumps({"action": action, "params": params}).encode(),
            headers={"Content-Type": "application/json", TOKEN_HEADER: token},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = json.loads(response.read())
        except urllib.error.HTTPError as e:
            body = json.loads(e.read() or b"{}")
            raise BrowserControlError(body.get("error", f"HTTP {e.code}"), status=e.code) from e
        except (urllib.error.URLError, TimeoutError) as e:
            raise BrowserControlError(f"Browser did not respond: {e}", status=504) from e
        return body["result"]

    def running_profile_names(self) -> set[str]:
        with self._lock:
            stale = [n for n, p in self._active_sessions.items() if p.poll() is not None]
            for n in stale:
                self._active_sessions.pop(n, None)
                self._stop_notifiers.pop(n, None)
                self._forget_control(n)
            return set(self._active_sessions.keys())

    def running_count(self) -> int:
        return len(self.running_profile_names())

    def is_running(self, profile_name: str) -> bool:
        with self._lock:
            if profile_name not in self._active_sessions:
                return False
            if self._active_sessions[profile_name].poll() is None:
                return True
            del self._active_sessions[profile_name]
            self._stop_notifiers.pop(profile_name, None)
            self._forget_control(profile_name)
            return False

    def _monitor_process(
        self,
        proc: subprocess.Popen,
        name: str,
        log_callback: Callable[[str], None],
        on_ready: Callable[[], None] | None,
        notify_stopped: Callable[[], None],
        result: _LaunchResult,
    ) -> None:
        ready_notified = False
        try:
            if proc.stdout is None:
                return
            for line in iter(proc.stdout.readline, ""):
                msg = line.strip()
                if not msg:
                    continue
                if msg.startswith("CONTROL:"):
                    # Holds the control token: never log this line.
                    _, port, token = msg.split(":", 2)
                    with self._lock:
                        self._controls[name] = (int(port), token)
                    continue
                if msg == "BROWSER_STARTED":
                    result.settle(True)
                    if not ready_notified:
                        ready_notified = True
                        if on_ready:
                            on_ready()
                    log_callback(get_string("browser_started"))
                    logger.info("Browser started for profile: %s", name)
                    continue
                if msg == "BROWSER_CLOSED":
                    logger.info("Browser close detected for profile: %s", name)
                    notify_stopped()
                    terminate(proc, name, timeout=1)
                    continue
                if msg.startswith("LAUNCH_FAILED:") or msg == "LAUNCH_CANCELLED":
                    result.settle(False, msg.removeprefix("LAUNCH_FAILED:").strip())
                    log_callback(f"[{name}] {msg}")
                    logger.warning("Launch failed for profile %s: %s", name, msg)
                    notify_stopped()
                    terminate(proc, name, timeout=1)
                    break
                if msg.startswith(_NOISY_PREFIXES):
                    logger.debug("[%s] %s", name, msg)
                    continue
                if len(msg) > 400:
                    msg = msg[:400] + "..."
                log_callback(f"[{name}] {msg}")
                logger.debug("[%s] %s", name, msg)
        except Exception as e:
            logger.exception("Monitor error for profile %s: %s", name, e)
            log_callback(f"[{name}] Monitor error: {e}")
        finally:
            if proc.stdout is not None:
                with contextlib.suppress(Exception):
                    proc.stdout.close()
