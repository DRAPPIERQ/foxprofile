"""Recording through the REST API and the MCP tools (the runner side is faked)."""

import asyncio
import os

import pytest
from fastapi.testclient import TestClient

from src.api import auth
from src.api.app import create_app
from src.core.container import Container
from src.mcp_server import server as mcp_server
from src.services.browser.launcher import BrowserControlError
from src.services.browser.recording import START_TIMEOUT, STOP_TIMEOUT

TOKEN = "t" * 32


@pytest.fixture
def api(tmp_path, monkeypatch):
    # profiles.json and recordings/ are relative to the working directory.
    monkeypatch.chdir(tmp_path)
    container = Container()
    client = TestClient(create_app(container), base_url="http://127.0.0.1")
    assert client.post("/api/v1/profiles", json={"name": "p"}).status_code == 201
    bl = container.browser_launcher
    calls, responses = [], {}

    def control(name, action, params=None, timeout=60):
        calls.append((name, action, params, timeout))
        response = responses.get(action)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(bl, "control", control)
    monkeypatch.setattr(bl, "is_running", lambda name: True)
    client.calls, client.responses, client.bl, client.dir = calls, responses, bl, tmp_path
    return client


def test_record_start_passes_the_name_and_returns_a_download_path(api):
    api.responses["record_start"] = {"file": "case-1.webm", "path": "/x/case-1.webm"}
    r = api.post("/api/v1/browser/p/page/record/start", json={"name": "case-1"})
    assert r.status_code == 200
    assert r.json()["download"] == "/api/v1/recordings/p/case-1.webm"
    assert api.calls == [("p", "record_start", {"name": "case-1"}, START_TIMEOUT)]


def test_record_errors_keep_their_status(api):
    api.responses["record_start"] = BrowserControlError(
        "A recording is already running; stop it first", status=409
    )
    r = api.post("/api/v1/browser/p/page/record/start", json={})
    assert r.status_code == 409
    assert r.json()["detail"].startswith("A recording is already running")


def test_record_on_an_unknown_profile_is_404(api):
    assert api.post("/api/v1/browser/nope/page/record/start", json={}).status_code == 404
    assert api.calls == []


def test_record_stop_and_status(api):
    api.responses["record_status"] = {"recording": True, "file": "a.webm", "seconds": 3.0}
    assert api.get("/api/v1/browser/p/page/record").json()["recording"] is True
    api.responses["record_stop"] = {"file": "a.webm", "path": "/x/a.webm", "bytes": 9, "seconds": 3}
    body = api.post("/api/v1/browser/p/page/record/stop").json()
    assert body["download"] == "/api/v1/recordings/p/a.webm"
    assert api.calls[-1] == ("p", "record_stop", {}, STOP_TIMEOUT)


@pytest.mark.parametrize(
    ("saved", "expected"),
    [({"file": "a.webm", "bytes": 9}, "/api/v1/recordings/p/a.webm"), (None, None)],
)
def test_stop_returns_the_recording_it_saved(api, monkeypatch, saved, expected):
    order = []
    monkeypatch.setattr(api.bl, "finish_recording", lambda name: order.append("finish") or saved)
    monkeypatch.setattr(api.bl, "stop_profile", lambda name: order.append("stop") or True)
    body = api.post("/api/v1/browser/p/stop").json()
    assert order == ["finish", "stop"]
    assert (body["recording"] or {}).get("download") == expected


def _video(api, profile="p", name="case-1.webm"):
    folder = api.dir / "recordings" / profile
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_bytes(b"\x1a\x45\xdf\xa3video")
    return folder / name


def test_download_serves_the_webm(api):
    _video(api)
    r = api.get("/api/v1/recordings/p/case-1.webm")
    assert r.status_code == 200
    assert r.headers["content-type"] == "video/webm"
    assert r.content == b"\x1a\x45\xdf\xa3video"


def test_download_outlives_the_profile(api):
    _video(api, profile="deleted-profile")
    assert api.get("/api/v1/recordings/deleted-profile/case-1.webm").status_code == 200


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/recordings/p/missing.webm",
        "/api/v1/recordings/p/case-1.mp4",
        "/api/v1/recordings/p/..%2F..%2Fprofiles.json",
        "/api/v1/recordings/p/..%2Fp%2Fcase-1.webm",
        "/api/v1/recordings/%2E%2E/case-1.webm",
        "/api/v1/recordings/CON/case-1.webm",
    ],
)
def test_download_refuses_anything_but_a_recording(api, path):
    _video(api)
    assert api.get(path).status_code == 404


def test_download_does_not_follow_a_link_out_of_the_folder(api):
    secret = api.dir / "secret.webm"
    secret.write_bytes(b"secret")
    link = api.dir / "recordings" / "p" / "link.webm"
    link.parent.mkdir(parents=True)
    try:
        os.symlink(secret, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks need privileges on this OS")
    assert api.get("/api/v1/recordings/p/link.webm").status_code == 404


def test_download_needs_the_token(api, monkeypatch):
    _video(api)
    monkeypatch.setattr(auth, "API_TOKEN", TOKEN)
    assert api.get("/api/v1/recordings/p/case-1.webm").status_code == 401
    headers = {"Authorization": f"Bearer {TOKEN}"}
    assert api.get("/api/v1/recordings/p/case-1.webm", headers=headers).status_code == 200


# --- MCP tools -----------------------------------------------------------------


@pytest.fixture
def mcp_calls(monkeypatch):
    calls = []

    async def fake(method, path, body=None, **kwargs):
        calls.append((method, path, body))
        return {"ok": True}

    monkeypatch.setattr(mcp_server, "_arequest", fake)
    return calls


def test_mcp_record_tools_call_the_record_routes(mcp_calls):
    asyncio.run(mcp_server.browser_record_start("shop 01", "case-1"))
    asyncio.run(mcp_server.browser_record_stop("shop 01"))
    assert mcp_calls == [
        ("POST", "/browser/shop%2001/page/record/start", {"name": "case-1"}),
        ("POST", "/browser/shop%2001/page/record/stop", None),
    ]


def test_mcp_lists_the_record_tools():
    names = {tool.name for tool in asyncio.run(mcp_server.mcp.list_tools())}
    assert {"browser_record_start", "browser_record_stop"} <= names
