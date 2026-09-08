"""T36 — session-level recovery from a Chromium tab / renderer crash.

``run_session`` shares ONE browser / context / page across every job in a run.
A renderer crash on a memory-heavy ATS SPA leaves that page (and, rarely, the
context) unusable; without a rebuild every subsequent job in the session fails
too. ``_recover_browser_if_crashed`` rebuilds only what actually died.

No browser, no network: fake browser / context / page objects only.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import apply_jobs  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


class _FakePage:
    def __init__(self, *, alive=True, closed=False):
        self._alive = alive
        self._closed = closed
        self.gotos = []

    def is_closed(self):
        return self._closed

    async def evaluate(self, _js):
        if not self._alive:
            raise RuntimeError("Target crashed")
        return 1

    async def goto(self, url):
        self.gotos.append(url)

    async def close(self):
        self._closed = True


class _FakeContext:
    def __init__(self, *, new_page_result=None, new_page_raises=False):
        self._new_page_result = new_page_result
        self._new_page_raises = new_page_raises
        self.closed = False
        self.pages = []

    async def new_page(self):
        if self._new_page_raises:
            raise RuntimeError("Target page, context or browser has been closed")
        p = self._new_page_result or _FakePage()
        self.pages.append(p)
        return p

    async def close(self):
        self.closed = True


class _FakeBrowser:
    def __init__(self, *, context_result=None):
        self._context_result = context_result
        self.new_contexts = 0

    async def new_context(self, **_kw):
        self.new_contexts += 1
        return self._context_result or _FakeContext()


def test_healthy_page_returned_unchanged_no_rebuild():
    page = _FakePage(alive=True)
    ctx = _FakeContext()
    br = _FakeBrowser()
    out_ctx, out_page = _run(
        apply_jobs._recover_browser_if_crashed(br, ctx, page, need_login=False)
    )
    assert out_ctx is ctx and out_page is page
    assert br.new_contexts == 0 and not ctx.closed


def test_crashed_page_replaced_with_fresh_tab_on_same_context():
    dead = _FakePage(alive=False)
    fresh = _FakePage(alive=True)
    ctx = _FakeContext(new_page_result=fresh)
    ctx.pages = [dead]
    br = _FakeBrowser()
    out_ctx, out_page = _run(
        apply_jobs._recover_browser_if_crashed(
            br, ctx, dead, need_login=False, suspect=True
        )
    )
    # context (and its cookies / LinkedIn login) preserved; only the tab is new
    assert out_ctx is ctx
    assert out_page is fresh
    assert br.new_contexts == 0
    assert dead.is_closed()  # stale tab cleaned up


def test_swallowed_crash_detected_by_probe_even_without_suspect():
    dead = _FakePage(alive=False)
    fresh = _FakePage(alive=True)
    ctx = _FakeContext(new_page_result=fresh)
    br = _FakeBrowser()
    out_ctx, out_page = _run(
        apply_jobs._recover_browser_if_crashed(br, ctx, dead, need_login=False)
    )
    assert out_page is fresh and out_ctx is ctx


def test_dead_context_triggers_rebuild_and_relogin(monkeypatch):
    logins = []

    async def _fake_login(pg):
        logins.append(pg)

    monkeypatch.setattr(apply_jobs, "login_linkedin_playwright", _fake_login)

    dead = _FakePage(alive=False)
    dead_ctx = _FakeContext(new_page_raises=True)
    rebuilt_page = _FakePage(alive=True)
    rebuilt_ctx = _FakeContext(new_page_result=rebuilt_page)
    br = _FakeBrowser(context_result=rebuilt_ctx)

    out_ctx, out_page = _run(
        apply_jobs._recover_browser_if_crashed(
            br, dead_ctx, dead, need_login=True, suspect=True
        )
    )
    assert out_ctx is rebuilt_ctx and out_page is rebuilt_page
    assert br.new_contexts == 1
    assert dead_ctx.closed
    assert logins == [rebuilt_page]


def test_dead_context_rebuild_skips_login_for_offsite_only_session(monkeypatch):
    called = []
    monkeypatch.setattr(
        apply_jobs, "login_linkedin_playwright",
        lambda pg: called.append(pg),  # would blow up if awaited; must not be called
    )
    dead = _FakePage(alive=False)
    dead_ctx = _FakeContext(new_page_raises=True)
    rebuilt_page = _FakePage(alive=True)
    rebuilt_ctx = _FakeContext(new_page_result=rebuilt_page)
    br = _FakeBrowser(context_result=rebuilt_ctx)

    out_ctx, out_page = _run(
        apply_jobs._recover_browser_if_crashed(
            br, dead_ctx, dead, need_login=False, suspect=True
        )
    )
    assert out_ctx is rebuilt_ctx and out_page is rebuilt_page
    assert called == []
