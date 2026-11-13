"""Record a real Camoufox tab. Skipped unless Camoufox and Playwright's ffmpeg are installed
(`python -m camoufox fetch`, `python -m playwright install ffmpeg`), so CI skips it."""

import asyncio
import glob
import os
import sys

import pytest

from src.services.browser.recording import Recorder

EBML = b"\x1a\x45\xdf\xa3"  # first bytes of every WebM file

PAGE = """<body style="margin:0;font:40px sans-serif"><div id=n>0</div>
<script>let i = 0; setInterval(() => n.textContent = ++i, 40)</script></body>"""


def _camoufox_installed() -> bool:
    try:
        from camoufox.pkgman import launch_path

        return os.path.exists(launch_path())
    except Exception:
        return False


def _playwright_ffmpeg_installed() -> bool:
    root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if not root:
        if sys.platform == "win32":
            root = os.path.join(os.environ.get("LOCALAPPDATA", ""), "ms-playwright")
        elif sys.platform == "darwin":
            root = os.path.expanduser("~/Library/Caches/ms-playwright")
        else:
            root = os.path.expanduser("~/.cache/ms-playwright")
    return bool(glob.glob(os.path.join(root, "ffmpeg-*", "INSTALLATION_COMPLETE")))


pytestmark = pytest.mark.skipif(
    not (_camoufox_installed() and _playwright_ffmpeg_installed()),
    reason="needs Camoufox and Playwright's ffmpeg",
)


def _assert_webm(saved: dict) -> None:
    with open(saved["path"], "rb") as f:
        assert f.read(4) == EBML
    assert saved["bytes"] > 1000


def test_record_a_tab_and_a_tab_that_closes(tmp_path):
    from camoufox.async_api import AsyncCamoufox

    async def scenario():
        recorder = Recorder(str(tmp_path / "rec"), installer=lambda: None)
        async with AsyncCamoufox(
            headless=True, user_data_dir=str(tmp_path / "profile"), persistent_context=True
        ) as context:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.set_content(PAGE)
            started = await recorder.start(page, "case-1")
            assert started["size"]["width"] > 0
            await asyncio.sleep(1.5)
            first = await recorder.stop()

            # The recorded tab closes before the stop: the video up to the close is kept.
            other = await context.new_page()
            await other.set_content(PAGE)
            await recorder.start(other, "closed")
            await asyncio.sleep(1.5)
            await other.close()
            second = await recorder.stop()
        return first, second

    first, second = asyncio.run(scenario())
    assert first["file"] == "case-1.webm"
    _assert_webm(first)
    assert second["file"] == "closed.webm"
    _assert_webm(second)
