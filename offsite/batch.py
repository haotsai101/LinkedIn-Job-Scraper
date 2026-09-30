"""Offsite jobs inside an ``apply_jobs.py`` session (OA12).

``OffsiteBatch`` owns the offsite stack for a whole ``run_session``: the shared
``OffsiteBrowser`` (persistent profile — ATS logins survive), guard-mcp,
``RunControl`` and ``SubmitGuard``. It starts lazily on the first offsite job
(so a batch that never reaches one never opens a window) and is reused for
every later job. If the human closes the browser window, the next job starts
a fresh stack instead of failing.

Must be entered and exited in the same asyncio task (guard-mcp's MCP client
uses anyio task groups) — ``run_session`` does exactly that.
"""
from __future__ import annotations

import contextlib
import sqlite3
from typing import Any

from offsite.browser import OffsiteBrowser
from offsite.control import RunControl
from offsite.controller import FillController
from offsite.guard_mcp import GuardMCP
from offsite.guards import SubmitGuard
from offsite.prompts import JobInfo, needs_sponsorship
from offsite.session import JobOutcome, run_offsite_job


class OffsiteBatch:
    def __init__(self, profile: dict[str, Any], *, order: tuple[str, ...] = ("nim", "claude"),
                 browser_factory=OffsiteBrowser) -> None:
        self.profile = profile
        self.order = order
        self._browser_factory = browser_factory
        self._stack: contextlib.AsyncExitStack | None = None
        self.browser: OffsiteBrowser | None = None
        self.guard: GuardMCP | None = None
        self.control: RunControl | None = None
        self.submit_guard: SubmitGuard | None = None
        self.starts = 0

    async def __aenter__(self) -> OffsiteBatch:
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def close(self) -> None:
        if self._stack is not None:
            stack, self._stack = self._stack, None
            with contextlib.suppress(Exception):
                await stack.aclose()
        self.browser = self.guard = self.control = self.submit_guard = None

    async def _healthy(self) -> bool:
        """The stack is up and has an open page (re-opening a tab if the human closed
        the last one). False when the browser itself is gone."""
        ctx = self.browser.context if self.browser else None
        if self._stack is None or ctx is None:
            return False
        try:
            if not [p for p in ctx.pages if not p.is_closed()]:
                await ctx.new_page()
            return True
        except Exception:  # noqa: BLE001 - window / browser closed
            return False

    async def _ensure_started(self) -> None:
        if await self._healthy():
            return
        await self.close()
        stack = contextlib.AsyncExitStack()
        try:
            browser = await stack.enter_async_context(self._browser_factory())
            guard = await stack.enter_async_context(GuardMCP(browser))
            control = RunControl(sponsorship_skip=needs_sponsorship(self.profile))
            control.install(guard)
            submit_guard = SubmitGuard(browser, resume_path=self.profile.get("resume_path"))
            await submit_guard.install(guard)
        except BaseException:
            await stack.aclose()
            raise
        self._stack, self.browser, self.guard = stack, browser, guard
        self.control, self.submit_guard = control, submit_guard
        self.starts += 1

    async def run_job(self, conn: sqlite3.Connection | None, job: JobInfo, **session_kw
                      ) -> JobOutcome:
        """One supervised offsite application (OA11) on the shared stack."""
        await self._ensure_started()
        assert self.browser and self.guard and self.control and self.submit_guard
        controller = FillController(profile=self.profile, job=job, guard_url=self.guard.url,
                                    control=self.control, order=self.order)
        return await run_offsite_job(job, conn=conn, browser=self.browser,
                                     submit_guard=self.submit_guard, controller=controller,
                                     **session_kw)
