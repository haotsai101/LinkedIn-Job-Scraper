"""Shared fixtures for offsite tests that need a live browser + guard-mcp.

There is no pytest-asyncio here, so ``live_guard`` runs one asyncio event loop
in a background thread for the whole module: the fixture server, a headless
``OffsiteBrowser`` and a ``GuardMCP`` are started once (≈2 s) and every test
drives them with ``lg.run(coro)``.
"""
from __future__ import annotations

import asyncio
import shutil
import threading
from dataclasses import dataclass

import pytest

from offsite.browser import OffsiteBrowser
from offsite.guard_mcp import GuardMCP
from offsite.human_typing import HumanTyping
from tests.fixtures.offsite.serve import fixture_server


class _LoopThread:
    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

    def run(self, coro, timeout: float = 120):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)
        self.loop.close()


@dataclass
class LiveGuard:
    base: str                 # fixture server base URL
    browser: OffsiteBrowser
    guard: GuardMCP
    run: callable             # run(coro) on the shared loop

    def open(self, name: str) -> None:
        """Navigate the shared browser to a fixture page."""
        self.run(self.browser.open(f"{self.base}/{name}"))


@pytest.fixture(scope="module")
def live_guard(tmp_path_factory):
    if shutil.which("npx") is None:
        pytest.skip("npx (Node) not installed — Playwright MCP unavailable")
    lt = _LoopThread()
    profile = tmp_path_factory.mktemp("profile")
    with fixture_server() as base:
        # GuardMCP's stdio client uses anyio task groups, which must be entered and
        # exited in the same task — so one host task holds everything open.
        ready: asyncio.Future = lt.loop.create_future()
        stop = asyncio.Event()

        async def host():
            try:
                # fast typing keeps the suite quick; test_human_typing checks the real pacing
                fast = HumanTyping(mean=0.005, std=0.0, min_delay=0.0)
                async with OffsiteBrowser(profile, headless=True) as b, \
                        GuardMCP(b, typing=fast) as g:
                    ready.set_result((b, g))
                    await stop.wait()
            except BaseException as e:
                if not ready.done():
                    ready.set_exception(e)
                raise

        host_fut = asyncio.run_coroutine_threadsafe(host(), lt.loop)

        async def wait_ready():
            return await ready

        try:
            browser, guard = lt.run(wait_ready())
        except Exception as e:  # pragma: no cover - environment dependent
            lt.stop()
            pytest.skip(f"browser / Playwright MCP unavailable: {e}")
        try:
            yield LiveGuard(base, browser, guard, lt.run)
        finally:
            lt.loop.call_soon_threadsafe(stop.set)
            host_fut.result(60)
            lt.stop()
