"""OA2: OffsiteBrowser — CDP endpoint, persistent profile, profile lock.

Runs a real (headless) Chromium against a local http.server; skipped when the
Playwright Chromium binary isn't installed.
"""
from __future__ import annotations

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from playwright.async_api import async_playwright

from offsite.browser import OffsiteBrowser, ProfileInUseError, host_of


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = b"<html><body><h1>ats</h1></body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        if self.path == "/login":
            # stands in for an ATS setting a session cookie after a human login
            self.send_header("Set-Cookie", "ats_session=abc123; Max-Age=86400; Path=/")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(scope="module")
def site():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture(scope="module", autouse=True)
def _require_chromium():
    async def probe():
        async with async_playwright() as pw:
            b = await pw.chromium.launch(headless=True)
            await b.close()

    try:
        asyncio.run(probe())
    except Exception as e:  # pragma: no cover - environment dependent
        pytest.skip(f"Playwright Chromium not available: {e}")


def _run(coro):
    return asyncio.run(coro)


def test_host_of():
    assert host_of("https://www.Example.com/a?b=1") == "example.com"
    assert host_of("https://nvidia.wd5.myworkdayjobs.com/x") == "nvidia.wd5.myworkdayjobs.com"
    assert host_of("not a url") == ""


def test_cdp_endpoint_is_attachable_and_sees_the_same_page(tmp_path, site):
    async def go():
        async with OffsiteBrowser(tmp_path / "p", headless=True) as b:
            await b.open(f"{site}/apply")
            assert b.cdp_endpoint.startswith("http://127.0.0.1:")
            async with async_playwright() as pw:
                other = await pw.chromium.connect_over_cdp(b.cdp_endpoint)
                urls = [p.url for c in other.contexts for p in c.pages]
                assert f"{site}/apply" in urls
                # an action through the second handle is visible to the first
                page2 = next(p for c in other.contexts for p in c.pages
                             if p.url == f"{site}/apply")
                await page2.evaluate("document.title = 'touched via cdp'")
            assert await b.page.title() == "touched via cdp"

    _run(go())


def test_login_cookie_survives_restart(tmp_path, site):
    profile = tmp_path / "p"

    async def login():
        async with OffsiteBrowser(profile, headless=True) as b:
            await b.open(f"{site}/login")

    async def cookies_after_restart():
        async with OffsiteBrowser(profile, headless=True) as b:
            await b.open(f"{site}/apply")
            return {c["name"]: c["value"] for c in await b.context.cookies(site)}

    _run(login())
    assert _run(cookies_after_restart()).get("ats_session") == "abc123"


def test_second_browser_on_same_profile_is_refused(tmp_path):
    profile = tmp_path / "p"

    async def go():
        async with OffsiteBrowser(profile, headless=True):
            with pytest.raises(ProfileInUseError):
                async with OffsiteBrowser(profile, headless=True):
                    pass
        # released on close → a new browser can take the profile
        async with OffsiteBrowser(profile, headless=True):
            pass

    _run(go())


def test_open_before_start_raises(tmp_path):
    b = OffsiteBrowser(tmp_path / "p", headless=True)
    with pytest.raises(RuntimeError):
        _run(b.open("about:blank"))
    with pytest.raises(RuntimeError):
        _ = b.page
