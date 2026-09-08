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
import linkedin_apply  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


def _mk_async(fn):
    async def _inner(*a, **kw):
        return fn(*a, **kw)
    return _inner


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
        self.new_page_calls = 0

    async def new_page(self):
        self.new_page_calls += 1
        if self._new_page_raises:
            raise RuntimeError("Target page, context or browser has been closed")
        # a callable factory makes a fresh page each call (run_session opens the
        # initial page here too, then the recovery opens another)
        if callable(self._new_page_result):
            p = self._new_page_result()
        else:
            p = self._new_page_result or _FakePage()
        self.pages.append(p)
        return p

    async def close(self):
        self.closed = True


class _FakeBrowser:
    def __init__(self, *, context_result=None):
        self._context_result = context_result
        self.new_contexts = 0
        self.closed = False

    async def new_context(self, **_kw):
        self.new_contexts += 1
        return self._context_result or _FakeContext()

    async def close(self):
        self.closed = True


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


def test_suspect_but_shared_page_alive_is_kept_crash_was_in_a_child_tab():
    # A crash caught this job may have been in a child tab while the shared page
    # is fine — a probe still runs under suspect=True, and a healthy page is
    # returned untouched instead of being needlessly dropped.
    page = _FakePage(alive=True)
    ctx = _FakeContext()
    br = _FakeBrowser()
    out_ctx, out_page = _run(
        apply_jobs._recover_browser_if_crashed(
            br, ctx, page, need_login=False, suspect=True
        )
    )
    assert out_ctx is ctx and out_page is page
    assert ctx.new_page_calls == 0 and br.new_contexts == 0


def test_unusable_fresh_tab_is_closed_before_context_rebuild():
    dead = _FakePage(alive=False)
    # context.new_page() succeeds but the returned tab is dead too
    stillborn = _FakePage(alive=False)
    dead_ctx = _FakeContext(new_page_result=stillborn)
    rebuilt_page = _FakePage(alive=True)
    rebuilt_ctx = _FakeContext(new_page_result=rebuilt_page)
    br = _FakeBrowser(context_result=rebuilt_ctx)

    out_ctx, out_page = _run(
        apply_jobs._recover_browser_if_crashed(
            br, dead_ctx, dead, need_login=False, suspect=True
        )
    )
    assert out_ctx is rebuilt_ctx and out_page is rebuilt_page
    assert stillborn.is_closed()   # the orphan tab was not leaked
    assert br.new_contexts == 1


# ── end-to-end cascade: a job-1 crash must not poison job 2 ────────────────
#
# This is the PR's central claim. It drives the real run_session loop (fake
# Playwright + fake flows): job 1's flow crashes the shared page mid-run, and
# job 2's flow must be constructed with — and run on — a live page.

_CASCADE: dict = {}


class _FakePlaywrightCM:
    async def __aenter__(self):
        browser = _CASCADE["browser"]

        class _P:
            chromium = type("_C", (), {"launch": _mk_async(lambda self, **_k: browser)})()

        return _P()

    async def __aexit__(self, *_e):
        return False


class _CascadeFlow:
    """OffsiteApplyFlow stand-in. Job 1 crashes the shared page; job 2 records
    the page it was handed and asserts it is alive."""

    def __init__(self, *, page, **_kw):
        self.page = page
        self.unanswered_fields = []
        self._browser_crashed = False
        _CASCADE["flows"].append(self)

    async def run(self, _url):
        idx = len(_CASCADE["flows"])
        _CASCADE["pages_seen"].append(self.page)
        if idx == 1:
            # simulate a Chromium renderer crash on the shared page mid-fill
            self.page._alive = False
            raise linkedin_apply._TargetClosedError("Target crashed")
        # job 2+: the page we were handed must be a live one
        assert await apply_jobs._page_is_alive(self.page), "job 2 got a dead page!"
        return "applied"


class _DummyConn:
    def close(self):
        pass


def test_job1_crash_does_not_cascade_into_job2(monkeypatch):
    # every context.new_page() call (initial + recovery) yields a fresh live tab
    ctx = _FakeContext(new_page_result=lambda: _FakePage(alive=True))
    browser = _FakeBrowser(context_result=ctx)

    _CASCADE.clear()
    _CASCADE.update(browser=browser, flows=[], pages_seen=[])

    marks: list[tuple] = []
    monkeypatch.setattr(apply_jobs, "async_playwright", lambda: _FakePlaywrightCM())
    monkeypatch.setattr(apply_jobs, "OffsiteApplyFlow", _CascadeFlow)
    monkeypatch.setattr(apply_jobs, "JobAgent", lambda _p: object())
    monkeypatch.setattr(apply_jobs, "_new_classifier_breaker", lambda: {})
    monkeypatch.setattr(apply_jobs, "load_session_blocked_domains", lambda _c: set())
    monkeypatch.setattr(apply_jobs, "_check_recent_session_health", lambda: True)
    monkeypatch.setattr(apply_jobs, "_match_spam_domain", lambda *_a: None)
    monkeypatch.setattr(apply_jobs, "_match_blocked_domain", lambda *_a: None)
    monkeypatch.setattr(apply_jobs, "write_session_log", lambda _r: None)
    monkeypatch.setattr(apply_jobs, "send_session_email", lambda *_a: None)
    monkeypatch.setattr(apply_jobs, "_write_llm_log", lambda _e: None)
    monkeypatch.setattr(apply_jobs, "mark_job",
                        lambda _cn, _cu, jid, st: marks.append((jid, st)))

    async def _fake_classify(*_a, **_kw):
        return (True, "relevant", False)

    monkeypatch.setattr(apply_jobs, "classify_with_circuit_breaker", _fake_classify)

    async def _fast_sleep(*_a, **_kw):
        return None

    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)

    jobs = [
        (1, "Backend Engineer", "https://li/1", "Remote", "Mid", "d", "Acme",
         "OffsiteApply", "acme.com", "https://acme.com/apply"),
        (2, "Platform Engineer", "https://li/2", "Remote", "Mid", "d", "Beta",
         "OffsiteApply", "beta.com", "https://beta.com/apply"),
    ]

    _run(apply_jobs.run_session(
        jobs, len(jobs), {"name": "T"}, _DummyConn(), object(),
        auto_mode=True, max_apply=10,
    ))

    # two flows built, one per job
    assert len(_CASCADE["flows"]) == 2
    job1_page, job2_page = _CASCADE["pages_seen"]
    # job 2 was handed a DIFFERENT, live page — not job 1's crashed one
    assert job2_page is not job1_page
    assert job2_page._alive is True
    assert job1_page._alive is False
    # outcomes: job 1 auto-failed (-2, retryable), job 2 applied (1)
    assert (1, -2) in marks
    assert (2, 1) in marks
