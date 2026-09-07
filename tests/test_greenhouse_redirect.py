"""T39 — two fixes for the "Greenhouse board URL redirects to a company careers
SPA" dead end (found in the T22/T38 QA run, job 11 — MongoDB).

Part 1 — Canonical embed retry: when the ORIGINAL ``application_url`` is a
``*.greenhouse.io`` board host but the page landed cross-host on a non-greenhouse
host, ``_llm_guided_apply`` retries once against
``job-boards.greenhouse.io/<slug>/jobs/<id>`` (the bare form, no company wrapper).

Part 2 — Correct terminal state: the "URL unchanged for 3 consecutive steps"
stuck guard returns ``"blocked"`` (``applied=-3``, no ``--reset-failed`` retry)
when the loop only ever scrolled, never saw a form field, and isn't on a known
ATS form host — a navigation dead end, not a transient mid-form stall. A stall
*after* the form was engaged still returns ``"failed"`` (``-2``, retryable).

No browser: ``page``/``context`` are minimal fakes; the LLM, bot-wall and
snapshot helpers are monkeypatched so each test exercises only the control flow
under test. Mirrors the fake-Page pattern in ``tests/test_blocked_status.py``.
"""

from __future__ import annotations

import asyncio

import linkedin_apply

GH = linkedin_apply


# ── pure URL helpers ──────────────────────────────────────────────────────

def test_host_is_greenhouse():
    for h in ("boards.greenhouse.io", "job-boards.greenhouse.io",
              "boards.eu.greenhouse.io", "job-boards.eu.greenhouse.io",
              "greenhouse.io", "grnh.se"):
        assert GH._host_is_greenhouse(h), h
    for h in ("www.mongodb.com", "jobs.acme.com", "greenhouse.io.evil.com", ""):
        assert not GH._host_is_greenhouse(h), h


def test_parse_greenhouse_job_url_shapes():
    cases = [
        ("http://boards.greenhouse.io/mongodb/jobs/8161512?gh_src=abc",
         ("mongodb", "8161512")),
        ("https://job-boards.greenhouse.io/acme/jobs/123", ("acme", "123")),
        ("https://boards.eu.greenhouse.io/foo-bar/jobs/999?t=x", ("foo-bar", "999")),
        ("https://boards.greenhouse.io/embed/job_app?token=456&for=acme",
         ("acme", "456")),
        # slug in path + gh_jid in query (company-listing style on a gh host)
        ("https://boards.greenhouse.io/acme/jobs/777?gh_jid=777", ("acme", "777")),
    ]
    for url, expected in cases:
        assert GH._parse_greenhouse_job(url) == expected, url


def test_parse_greenhouse_job_rejects_non_greenhouse_and_incomplete():
    assert GH._parse_greenhouse_job("https://www.mongodb.com/careers/jobs/8161512") is None
    assert GH._parse_greenhouse_job("https://jobs.acme.com/x/jobs/1") is None
    assert GH._parse_greenhouse_job("") is None
    # greenhouse host but no recoverable numeric id
    assert GH._parse_greenhouse_job("https://boards.greenhouse.io/acme") is None


# ── shared fakes / stubs ──────────────────────────────────────────────────

class _QueryStub:
    async def __call__(self, prompt, *, model, system=None, timeout=None,
                       log_type="agent", log_calls=False):
        return "a job summary"


class _FakeLocator:
    def __init__(self, count=0, *, visible=True, text=""):
        self._count, self._visible, self._text = count, visible, text

    @property
    def first(self):
        return self

    async def count(self):
        return self._count

    async def is_visible(self):
        return self._visible

    async def is_enabled(self):
        return True

    async def inner_text(self):
        return self._text

    async def click(self):
        return None


class _FakePage:
    def __init__(self, url, *, redirects=None):
        self.url = url
        self._redirects = redirects or {}
        self.goto_calls: list[str] = []

    async def goto(self, url, **_kw):
        self.goto_calls.append(url)
        self.url = self._redirects.get(url, url)

    def locator(self, _selector):
        return _FakeLocator(0)

    async def evaluate(self, _js):
        return ""

    async def wait_for_load_state(self, *_a, **_kw):
        return None

    @property
    def frames(self):
        return []


def _install_common(monkeypatch, *, snapshot_fields=None):
    monkeypatch.setattr(linkedin_apply, "_write_llm_log", lambda *_a, **_k: None)
    monkeypatch.setattr(linkedin_apply.llm, "query", _QueryStub())

    async def _no_bot_wall(self, page):
        return ""
    monkeypatch.setattr(linkedin_apply.OffsiteApplyFlow, "_detect_bot_wall", _no_bot_wall)

    async def _snapshot_stub(self, page):
        return {"fields": list(snapshot_fields or []),
                "buttons": [{"text": "placeholder"}], "visible_text": "x" * 40}
    monkeypatch.setattr(linkedin_apply.OffsiteApplyFlow, "_get_page_snapshot", _snapshot_stub)

    async def _fast_sleep(*_a, **_k):
        return None
    monkeypatch.setattr(linkedin_apply.asyncio, "sleep", _fast_sleep)


def _offsite(**kw):
    return linkedin_apply.OffsiteApplyFlow(
        page=None, context=None, profile={}, auto_mode=True,
        callbacks={}, generated_password="x",
        company_name="ACME", job_title="Dev", **kw,
    )


def _always(action):
    async def _decide(self, *_a, **_k):
        return dict(action)
    return _decide


def _sequence(*actions):
    """Return each action once, then repeat the last one forever."""
    box = {"i": 0}

    async def _decide(self, *_a, **_k):
        i = box["i"]
        box["i"] = i + 1
        return dict(actions[min(i, len(actions) - 1)])
    return _decide


# ── Part 1 — canonical embed retry ────────────────────────────────────────

def test_greenhouse_cross_host_redirect_triggers_canonical_retry(monkeypatch):
    _install_common(monkeypatch)
    monkeypatch.setattr(linkedin_apply.OffsiteApplyFlow, "_decide_action",
                        _always({"action": "failed", "reason": "stop here"}))
    # Landed on the company careers SPA after the board host 30x-redirected.
    page = _FakePage("https://www.mongodb.com/careers/jobs/8161512")
    flow = _offsite(
        application_url="http://boards.greenhouse.io/mongodb/jobs/8161512?gh_src=x")
    out = asyncio.run(flow._llm_guided_apply(page))
    assert "https://job-boards.greenhouse.io/mongodb/jobs/8161512" in page.goto_calls
    assert out in ("failed", "skipped", "expired")


def test_greenhouse_canonical_retry_url_parsed_from_embed_shape(monkeypatch):
    _install_common(monkeypatch)
    monkeypatch.setattr(linkedin_apply.OffsiteApplyFlow, "_decide_action",
                        _always({"action": "failed", "reason": "stop"}))
    page = _FakePage("https://careers.acme.com/job/123")
    flow = _offsite(
        application_url="https://boards.greenhouse.io/embed/job_app?token=456&for=acme")
    asyncio.run(flow._llm_guided_apply(page))
    assert "https://job-boards.greenhouse.io/acme/jobs/456" in page.goto_calls


def test_greenhouse_no_cross_host_redirect_no_retry(monkeypatch):
    """Board host resolved to a greenhouse form host (normal) — no retry."""
    _install_common(monkeypatch)
    monkeypatch.setattr(linkedin_apply.OffsiteApplyFlow, "_decide_action",
                        _always({"action": "failed", "reason": "stop"}))
    page = _FakePage("https://job-boards.greenhouse.io/acme/jobs/123")
    flow = _offsite(application_url="https://boards.greenhouse.io/acme/jobs/123")
    asyncio.run(flow._llm_guided_apply(page))
    assert not any("job-boards.greenhouse.io" in u for u in page.goto_calls)


def test_non_greenhouse_redirect_no_greenhouse_retry(monkeypatch):
    """A non-greenhouse application_url that redirects cross-host must not
    trigger a spurious job-boards.greenhouse.io retry."""
    _install_common(monkeypatch)
    monkeypatch.setattr(linkedin_apply.OffsiteApplyFlow, "_decide_action",
                        _always({"action": "failed", "reason": "stop"}))
    page = _FakePage("https://www.acme.com/careers")
    flow = _offsite(application_url="https://jobs.acme.com/postings/9")
    asyncio.run(flow._llm_guided_apply(page))
    assert page.goto_calls == []


# ── Part 2 — stuck guard: blocked vs failed ───────────────────────────────
#
# _form_engaged is set ONLY by a non-scroll action executing — never by the
# page snapshot (see the flag's comment in linkedin_apply.py). So the snapshot
# a test hands the loop is irrelevant to the blocked/failed verdict; what
# matters is whether _decide_action ever returned something other than scroll.

def test_scroll_only_deadend_on_non_ats_host_returns_blocked(monkeypatch):
    """Loop only ever scrolled, never ran a fill/click, host is a company SPA →
    navigation dead end → blocked (-3, no auto-retry)."""
    _install_common(monkeypatch, snapshot_fields=[])
    monkeypatch.setattr(linkedin_apply.OffsiteApplyFlow, "_decide_action",
                        _always({"action": "scroll", "reason": "look for apply"}))
    page = _FakePage("https://www.mongodb.com/careers/jobs/8161512")
    flow = _offsite(application_url="https://www.mongodb.com/careers/jobs/8161512")
    out = asyncio.run(flow._llm_guided_apply(page))
    assert out == "blocked"


def test_scroll_only_deadend_with_lone_nav_input_returns_blocked(monkeypatch):
    """Regression for the review: a company careers SPA routinely has a lone
    non-form input (nav search box, footer "Job alerts" email signup). That
    makes the snapshot's `fields` truthy, but it is NOT the application form —
    a scroll-only run must still end blocked (-3), not failed (-2)."""
    _install_common(monkeypatch, snapshot_fields=[
        {"selector": "#site-search", "type": "search", "label": "Search"},
        {"selector": "#ja-email", "type": "email", "label": "Get job alerts"},
    ])
    monkeypatch.setattr(linkedin_apply.OffsiteApplyFlow, "_decide_action",
                        _always({"action": "scroll", "reason": "hunting for apply"}))
    page = _FakePage("https://www.mongodb.com/careers/jobs/8161512")
    flow = _offsite(application_url="https://www.mongodb.com/careers/jobs/8161512")
    out = asyncio.run(flow._llm_guided_apply(page))
    assert out == "blocked"


def test_scroll_only_stall_on_ats_form_host_stays_failed(monkeypatch):
    """Same scroll-only stall but ON a known ATS form host — keep the old
    retryable -2 (the form is presumably there, just not snapshotted)."""
    _install_common(monkeypatch, snapshot_fields=[])
    monkeypatch.setattr(linkedin_apply.OffsiteApplyFlow, "_decide_action",
                        _always({"action": "scroll", "reason": "scrolling"}))
    page = _FakePage("https://job-boards.greenhouse.io/acme/jobs/1")
    flow = _offsite(application_url="https://job-boards.greenhouse.io/acme/jobs/1")
    out = asyncio.run(flow._llm_guided_apply(page))
    assert out == "failed"


def test_stall_after_form_engaged_stays_failed(monkeypatch):
    """The loop engaged a control (a click executed) then stalled — a genuine
    transient mid-form failure, must stay -2 even on a non-ATS host."""
    _install_common(monkeypatch, snapshot_fields=[])
    monkeypatch.setattr(
        linkedin_apply.OffsiteApplyFlow, "_decide_action",
        _sequence({"action": "click", "selector": "#open-form-btn", "reason": "open form"},
                  {"action": "scroll", "reason": "scrolling"}))
    page = _FakePage("https://www.mongodb.com/careers/jobs/8161512")
    flow = _offsite(application_url="https://www.mongodb.com/careers/jobs/8161512")
    out = asyncio.run(flow._llm_guided_apply(page))
    assert out == "failed"
