"""The one browser the offsite agent, guard-mcp and the human all share (OA2).

``OffsiteBrowser`` launches headed Chromium with:

* a **persistent profile** (``.offsite_browser_profile/`` by default) — cookies
  and local storage survive runs, so a login the human did by hand on an ATS
  host (Workday tenant, Greenhouse, ...) is still there next time;
* a **remote-debugging (CDP) port** on 127.0.0.1 — Playwright MCP (via
  ``--cdp-endpoint``, OA4) and the guard's own inspection handle (OA5) attach
  to the same browser the human is looking at.

Design: docs/NEW_AGENTIC_APPLY_PLAN.md §2.

CLI (manual check):
    python -m offsite.browser <url> [--profile-dir DIR] [--port N]
Opens the URL, prints the CDP endpoint, waits for Enter, closes.
"""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import socket
import sys
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import BrowserContext, Page, async_playwright

_REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROFILE_DIR = _REPO_ROOT / ".offsite_browser_profile"


class ProfileInUseError(RuntimeError):
    """Another Chromium already has this profile directory open."""


def free_port() -> int:
    """An unused TCP port on 127.0.0.1 (racy by nature, fine for a local tool)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def host_of(url: str) -> str:
    """Lower-cased host of ``url`` without ``www.`` — the per-ATS key used for
    ``offsite_applications.ats_host`` / ``account_host``."""
    host = (urlparse(url).hostname or "").lower()
    return host.removeprefix("www.")


def _cdp_ready(endpoint: str) -> bool:
    try:
        with urllib.request.urlopen(f"{endpoint}/json/version", timeout=1) as r:
            return "webSocketDebuggerUrl" in json.loads(r.read())
    except OSError:
        return False


class OffsiteBrowser:
    """Async context manager around a persistent, CDP-exposed Chromium.

    ``async with OffsiteBrowser() as b: await b.open(url); b.cdp_endpoint``
    """

    def __init__(
        self,
        profile_dir: str | Path = DEFAULT_PROFILE_DIR,
        *,
        headless: bool = False,
        port: int | None = None,
    ) -> None:
        self.profile_dir = Path(profile_dir)
        self.headless = headless
        self.port = port or free_port()
        self.cdp_endpoint = f"http://127.0.0.1:{self.port}"
        self._pw = None
        self._lock_file = None
        self.context: BrowserContext | None = None

    async def __aenter__(self) -> OffsiteBrowser:
        await self.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    def _acquire_profile_lock(self) -> None:
        # Our own lock: headless Chromium does not reliably take its
        # SingletonLock, and two browsers on one profile corrupt it.
        f = open(self.profile_dir / ".offsite.lock", "a")  # noqa: SIM115 - released in close()
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            f.close()
            raise ProfileInUseError(
                f"browser profile {self.profile_dir} is already open in another "
                "offsite browser — close that window (or the other offsite run) first"
            ) from None
        self._lock_file = f

    def _release_profile_lock(self) -> None:
        if self._lock_file is not None:
            try:
                fcntl.flock(self._lock_file, fcntl.LOCK_UN)
            finally:
                self._lock_file.close()
                self._lock_file = None

    async def start(self) -> None:
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._acquire_profile_lock()
        try:
            await self._launch()
        except BaseException:
            self._release_profile_lock()
            raise

    async def _launch(self) -> None:
        self._pw = await async_playwright().start()
        try:
            self.context = await self._pw.chromium.launch_persistent_context(
                str(self.profile_dir),
                headless=self.headless,
                no_viewport=not self.headless,  # headed: page follows the window size
                args=[
                    f"--remote-debugging-port={self.port}",
                    "--remote-debugging-address=127.0.0.1",
                ],
            )
        except Exception as e:
            await self._pw.stop()
            self._pw = None
            msg = str(e)
            if "ProcessSingleton" in msg or "SingletonLock" in msg:
                raise ProfileInUseError(
                    f"browser profile {self.profile_dir} is already open in another "
                    "Chromium — close that window (or the other offsite run) first"
                ) from e
            raise
        for _ in range(50):  # Chromium opens the CDP port shortly after launch
            if _cdp_ready(self.cdp_endpoint):
                break
            await asyncio.sleep(0.1)
        else:
            await self.close()
            raise RuntimeError(f"CDP endpoint {self.cdp_endpoint} never came up")

    async def close(self) -> None:
        if self.context is not None:
            try:
                await self.context.close()
            finally:
                self.context = None
        if self._pw is not None:
            try:
                await self._pw.stop()
            finally:
                self._pw = None
        self._release_profile_lock()

    @property
    def page(self) -> Page:
        """The active tab (the most recently opened page), creating none."""
        if self.context is None or not self.context.pages:
            raise RuntimeError("browser not started or no open page")
        return self.context.pages[-1]

    async def open(self, url: str) -> Page:
        """Navigate the active tab (opening one if needed) to ``url``."""
        if self.context is None:
            raise RuntimeError("browser not started")
        page = self.context.pages[-1] if self.context.pages else await self.context.new_page()
        await page.goto(url, wait_until="domcontentloaded")
        await page.bring_to_front()
        return page


async def _main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="python -m offsite.browser", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url")
    ap.add_argument("--profile-dir", default=str(DEFAULT_PROFILE_DIR))
    ap.add_argument("--port", type=int, default=None)
    args = ap.parse_args(argv)

    try:
        async with OffsiteBrowser(args.profile_dir, port=args.port) as b:
            await b.open(args.url)
            print(f"CDP endpoint : {b.cdp_endpoint}")
            print(f"Profile dir  : {b.profile_dir}")
            print(f"Host         : {host_of(args.url)}")
            await asyncio.get_running_loop().run_in_executor(
                None, input, "Browser open — press Enter to close. "
            )
    except ProfileInUseError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(sys.argv[1:])))
