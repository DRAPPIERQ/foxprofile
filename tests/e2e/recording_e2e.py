"""End-to-end: record test cases as WebM videos through a real MCP stdio client.

Needs a running FoxProfile (FOXPROFILE_URL, FOXPROFILE_API_TOKEN) started with a scratch
FOXPROFILE_DATA_DIR / FOXPROFILE_PROFILES_FILE / FOXPROFILE_RECORDINGS_DIR. It serves a small
site on 127.0.0.1, then for each engine in E2E_ENGINES (default "camoufox"; "camoufox,chrome"
for both): launch, record a case across two pages, refuse to close the recorded tab, download
the WebM, and check that stop_profile saves a recording left running. Videos are copied to
E2E_OUT (default e2e-output).
"""

import asyncio
import functools
import http.server
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

BASE = os.getenv("FOXPROFILE_URL", "http://127.0.0.1:8000").rstrip("/")
TOKEN = os.environ.get("FOXPROFILE_API_TOKEN", "")
ENGINES = [e.strip() for e in os.getenv("E2E_ENGINES", "camoufox").split(",") if e.strip()]
OUT = pathlib.Path(os.getenv("E2E_OUT", "e2e-output"))
ROOT = str(pathlib.Path(__file__).resolve().parents[2])
HOST_OS = {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")
EBML = b"\x1a\x45\xdf\xa3"
results = []

PAGES = {
    "index.html": """<title>Case start</title><body style="font:32px sans-serif">
<h1>Case start</h1><p>Tick <span id=n>0</span></p><a id=next href=form.html>Next page</a>
<script>let i = 0; setInterval(() => n.textContent = ++i, 100)</script></body>""",
    "form.html": """<title>Form</title><body style="font:32px sans-serif">
<h1>Form</h1><input id=q style="font:32px sans-serif"> <button id=go
onclick="out.textContent = 'Hello, ' + q.value">Go</button><p id=out></p></body>""",
}


def check(name, cond, detail=""):
    results.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name + (f"  [{str(detail)[:200]}]" if detail else ""))


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def serve_site() -> str:
    root = tempfile.mkdtemp(prefix="foxprofile-e2e-site-")
    for name, html in PAGES.items():
        pathlib.Path(root, name).write_text(html, encoding="utf-8")
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(_QuietHandler, directory=root)
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_address[1]}/"


def http_get(path: str) -> tuple[int, bytes]:
    request = urllib.request.Request(BASE + path)
    if TOKEN:
        request.add_header("Authorization", f"Bearer {TOKEN}")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def frames(path: str) -> int:
    """Frame count via ffprobe when it is installed, else -1."""
    if not shutil.which("ffprobe"):
        return -1
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0", "-show_entries",
         "stream=nb_read_frames", "-of", "csv=p=0", path],
        capture_output=True, text=True,
    ).stdout.strip()  # fmt: skip
    return int(out) if out.isdigit() else 0


async def run_engine(call, engine: str, site: str) -> None:
    name = f"e2e-rec-{engine}"
    page = {"profile": name}
    await call("stop_profile", name=name)
    await call("create_profile", name=name, os_type=HOST_OS, engine=engine)
    launched, text = await call("launch_profile", name=name)
    check(f"[{engine}] launch_profile", launched and "started" in str(text), text)
    if not launched:
        return

    await call("browser_navigate", **page, url=site + "index.html")
    ok, start = await call("browser_record_start", **page, name="case-1")
    check(f"[{engine}] record_start", ok and start.get("file") == "case-1.webm", start)
    ok, again = await call("browser_record_start", **page, name="case-2")
    check(f"[{engine}] second recording refused", not ok and "already running" in again, again)
    status, body = http_get(f"/api/v1/browser/{name}/page/record")
    check(f"[{engine}] REST record status", status == 200 and json.loads(body)["recording"], body)

    await asyncio.sleep(1.5)
    await call("browser_click", **page, selector="#next")
    await call("browser_type", **page, selector="#q", text="recording")
    await asyncio.sleep(0.5)
    await call("browser_click", **page, selector="#go")
    await asyncio.sleep(1.5)

    await call("browser_tab_new", **page, url=site + "index.html")
    ok, refused = await call("browser_tab_close", **page, index=0)
    check(f"[{engine}] recorded tab cannot be closed", not ok and "being recorded" in refused)

    ok, stop = await call("browser_record_stop", **page)
    check(f"[{engine}] record_stop", ok and stop.get("bytes", 0) > 0, stop)
    if ok:
        check(f"[{engine}] duration covers the case", stop["seconds"] >= 3.5, stop["seconds"])
        # `path` is relative to FoxProfile's folder unless FOXPROFILE_RECORDINGS_DIR is absolute.
        if os.path.isabs(stop["path"]):
            check(f"[{engine}] file on the FoxProfile machine", os.path.isfile(stop["path"]))
        status, video = http_get(stop["download"])
        check(
            f"[{engine}] download serves the WebM",
            status == 200 and video[:4] == EBML and len(video) == stop["bytes"],
            f"{status} {len(video)} bytes",
        )
        OUT.mkdir(parents=True, exist_ok=True)
        copy = OUT / f"{engine}-case-1.webm"
        copy.write_bytes(video)
        n = frames(str(copy))
        check(f"[{engine}] video has frames", n == -1 or n >= 50, f"{n} frames")

    await call("browser_record_start", **page, name="left-running")
    await asyncio.sleep(1.5)
    ok, stopped = await call("stop_profile", name=name)
    recording = (stopped or {}).get("recording") if ok else None
    check(
        f"[{engine}] stop_profile saves the running recording",
        recording and recording["file"] == "left-running.webm" and recording["bytes"] > 0,
        stopped,
    )


async def main():
    site = serve_site()
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "src.mcp_server"],
        cwd=ROOT,
        env={**os.environ, "FOXPROFILE_URL": BASE, "FOXPROFILE_API_TOKEN": TOKEN},
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        tools = {t.name for t in (await session.list_tools()).tools}
        check(
            "MCP lists the record tools", {"browser_record_start", "browser_record_stop"} <= tools
        )

        async def call(tool, **args):
            result = await session.call_tool(tool, args)
            text = "".join(c.text for c in result.content if getattr(c, "type", "") == "text")
            if result.isError:
                return False, text
            try:
                return True, json.loads(text)
            except ValueError:
                return True, text

        for engine in ENGINES:
            await run_engine(call, engine, site)


asyncio.run(main())
print(f"\n{sum(results)}/{len(results)} checks passed")
sys.exit(0 if all(results) else 1)
