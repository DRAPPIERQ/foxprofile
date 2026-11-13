import asyncio
import os

import pytest

from src.services.browser.control import ControlError, PageController, _check_url


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("example.com", "https://example.com"),
        ("http://a.vn/x", "http://a.vn/x"),
        ("about:blank", "about:blank"),
        ("localhost:8080/x", "https://localhost:8080/x"),
    ],
)
def test_allowed_urls(url, expected):
    assert _check_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "file:///C:/Windows/win.ini",
        "view-source:https://a.com",
        "javascript:alert(1)",
        "chrome://x",
        "about:config",
        "about:logins",
        "data:text/html,<script>alert(1)</script>",
    ],
)
def test_blocked_urls(url):
    with pytest.raises(ControlError):
        _check_url(url)


class FakePage:
    def __init__(self, url="about:blank"):
        self.url = url
        self.closed = False

    def is_closed(self):
        return self.closed

    async def title(self):
        return "T"

    async def goto(self, url, wait_until="load"):
        self.url = url

    async def close(self):
        self.closed = True

    async def bring_to_front(self):
        pass


class FakeContext:
    def __init__(self, n=1):
        self.pages = [FakePage(f"https://p{i}.test/") for i in range(n)]

    async def new_page(self):
        page = FakePage()
        self.pages.append(page)
        return page


def run(coro):
    return asyncio.run(coro)


def test_dispatch_rejects_unknown_action_and_bad_params():
    c = PageController(FakeContext())
    with pytest.raises(ControlError, match="Unknown action"):
        run(c.dispatch("rm_rf", {}))
    with pytest.raises(ControlError, match="Invalid parameters"):
        run(c.dispatch("navigate", {"nope": 1}))


def test_navigate_uses_active_tab():
    ctx = FakeContext(2)
    c = PageController(ctx)
    result = run(c.dispatch("navigate", {"url": "example.com"}))
    assert result == {"url": "https://example.com", "title": "T"}
    assert ctx.pages[-1].url == "https://example.com"


def test_tabs_select_and_close():
    ctx = FakeContext(2)
    c = PageController(ctx)
    tabs = run(c.dispatch("tab_select", {"index": 0}))["tabs"]
    assert [t["active"] for t in tabs] == [True, False]
    tabs = run(c.dispatch("tab_close", {"index": 0}))["tabs"]
    assert [t["url"] for t in tabs] == ["https://p1.test/"]
    with pytest.raises(ControlError, match="last tab"):
        run(c.dispatch("tab_close", {"index": 0}))
    with pytest.raises(ControlError, match="No tab"):
        run(c.dispatch("tab_select", {"index": 5}))


# ---------------------------------------------------------------------------
# Percent-encoded scheme bypass (fix 3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "%66ile:///C:/Windows/win.ini",  # decodes to file:///...
        "java%73cript:alert(1)",  # decodes to javascript:...
        "%76iew-source:https://a.com",  # decodes to view-source:...
    ],
)
def test_percent_encoded_blocked_schemes(url):
    """Percent-encoded disallowed schemes must be caught before scheme detection."""
    with pytest.raises(ControlError):
        _check_url(url)


@pytest.mark.parametrize("url", ["[", "[]", "[::1", "http://[::1"])
def test_malformed_url_is_a_control_error(url):
    with pytest.raises(ControlError):
        _check_url(url)


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------


class RecordingContext:
    """Two tabs that can be recorded (see tests/test_recording.py)."""

    def __init__(self):
        from tests.test_recording import FakePage as RecordablePage

        self.pages = [RecordablePage(), RecordablePage()]

    async def new_page(self):  # pragma: no cover - the context always has tabs
        raise AssertionError


def test_record_actions_need_a_recordings_dir():
    c = PageController(FakeContext())
    with pytest.raises(ControlError, match="not available"):
        run(c.dispatch("record_start", {}))


def test_recorded_tab_cannot_be_closed(tmp_path):
    c = PageController(RecordingContext(), str(tmp_path))
    run(c.dispatch("tab_select", {"index": 0}))
    started = run(c.dispatch("record_start", {"name": "case-1"}))
    assert started["file"] == "case-1.webm"
    with pytest.raises(ControlError, match="being recorded") as e:
        run(c.dispatch("tab_close", {"index": 0}))
    assert e.value.status == 409
    # Once the recording is stopped, the tab closes as usual.
    run(c.dispatch("tab_select", {"index": 0}))
    assert run(c.dispatch("record_status", {}))["recording"] is True
    assert run(c.dispatch("record_stop", {}))["file"] == "case-1.webm"
    run(c.dispatch("tab_close", {"index": 0}))


def _serve(controller, token="tok"):
    from aiohttp.test_utils import TestClient, TestServer

    from src.services.browser.control import build_app

    return TestClient(TestServer(build_app(controller, token)))


def test_control_server_answers_with_the_error_status(tmp_path):
    async def scenario():
        client = _serve(PageController(RecordingContext(), str(tmp_path)))
        await client.start_server()
        try:
            headers = {"X-Control-Token": "tok"}
            idle = await client.post(
                "/action", json={"action": "record_stop", "params": {}}, headers=headers
            )
            bad = await client.post(
                "/action",
                json={"action": "record_start", "params": {"name": "../x"}},
                headers=headers,
            )
            return idle.status, await idle.json(), bad.status
        finally:
            await client.close()

    status, body, bad_status = run(scenario())
    assert (status, body["ok"], bad_status) == (409, False, 400)
    assert body["error"] == "No recording is running"


def test_cleaning_up_the_control_server_saves_the_recording(tmp_path):
    controller = PageController(RecordingContext(), str(tmp_path))

    async def scenario():
        client = _serve(controller)
        await client.start_server()
        started = await controller.record_start("left-running")
        await client.close()  # what the runners do on shutdown: control_runner.cleanup()
        return started

    started = run(scenario())
    assert os.path.getsize(started["path"]) > 0
    assert run(controller.record_status()) == {"recording": False}


def test_screenshots_leave_the_caret_alone():
    """Playwright's default caret="hide" restyles the focused field, which pages can see."""
    calls = []

    class ShotPage(FakePage):
        async def screenshot(self, **kwargs):
            calls.append(kwargs)
            return b"\x89PNG"

    ctx = FakeContext()
    ctx.pages = [ShotPage()]
    run(PageController(ctx).dispatch("screenshot", {}))
    assert calls == [{"full_page": False, "type": "png", "caret": "initial"}]


def test_control_server_errors_do_not_reveal_local_paths(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    class FailingPage(FakePage):
        async def goto(self, url, wait_until="load"):
            raise RuntimeError(f"cannot write {tmp_path}/recordings/x.webm")

    ctx = FakeContext()
    ctx.pages = [FailingPage()]

    async def scenario():
        client = _serve(PageController(ctx))
        await client.start_server()
        try:
            r = await client.post(
                "/action",
                json={"action": "navigate", "params": {"url": "https://a.test"}},
                headers={"X-Control-Token": "tok"},
            )
            return r.status, (await r.json())["error"]
        finally:
            await client.close()

    status, error = run(scenario())
    assert status == 422
    assert str(tmp_path) not in error and "./recordings/x.webm" in error
