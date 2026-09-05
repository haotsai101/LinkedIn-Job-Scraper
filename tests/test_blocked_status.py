"""T34 — mid-flow blocked-ATS / no-credentials login-wall paths must end at
``"blocked"`` (``applied=-3``, excluded from ``--reset-failed``), the same
outcome T33 already wired up for the *pre-flight* domain/login checks in
``OffsiteApplyFlow._llm_guided_apply``. Two mid-flow checks previously fell
through to the generic ``"failed"`` (``applied=-2``, retried):

  1. a Step-0 deterministic Apply-button click that navigates into a domain
     on ``_blocked_auto_apply_domains`` (e.g. Workday) partway through the
     flow, instead of being caught by the pre-flight landing-domain check;
  2. a password field appearing mid-flow with no stored credentials for that
     domain at all.

The adjacent branch — stored credentials exist but the login attempt itself
fails — is a different, still-retryable case and must keep returning
``"failed"``; that is covered here as a regression guard.

No browser: ``page``/``context`` are minimal fakes. The bot-wall pre-check
and ``_get_page_snapshot`` are monkeypatched out so each test only exercises
``_llm_guided_apply``'s own control flow around the checks under test.
"""

from __future__ import annotations

import asyncio

import linkedin_apply

# ── llm.query / logging plumbing (unused on these paths, stubbed for safety) ─

class _QueryStub:
    async def __call__(self, prompt, *, model, system=None, timeout=None,
                       log_type="agent", log_calls=False):
        return "a job summary"


def _install_common(monkeypatch):
    """Stub out everything ``_llm_guided_apply`` touches before it reaches the
    step loop's mid-flow checks, so a test only exercises the logic under test."""
    monkeypatch.setattr(linkedin_apply, "_write_llm_log", lambda *_a, **_k: None)
    monkeypatch.setattr(linkedin_apply.llm, "query", _QueryStub())

    async def _no_bot_wall(self, page):
        return ""
    monkeypatch.setattr(linkedin_apply.OffsiteApplyFlow, "_detect_bot_wall", _no_bot_wall)

    async def _snapshot_stub(self, page):
        return {"fields": [], "buttons": [{"text": "placeholder"}], "visible_text": "x" * 30}
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


# ── fake Page / Locator / Context plumbing ──────────────────────────────────

class _FakeLocator:
    """One-shot locator: fixed count/visibility/text, no-op click."""

    def __init__(self, count=0, *, visible=True, text=""):
        self._count = count
        self._visible = visible
        self._text = text

    @property
    def first(self):
        return self

    async def count(self):
        return self._count

    async def is_visible(self):
        return self._visible

    async def inner_text(self):
        return self._text

    async def click(self):
        return None


class _FakePage:
    """Exposes just what ``_llm_guided_apply`` touches on its way to the
    checks under test: .url, .locator(), .evaluate(), .wait_for_load_state()."""

    def __init__(self, url, *, password_fields=0, apply_button=False):
        self.url = url
        self._password_fields = password_fields
        self._apply_button = apply_button

    def locator(self, selector):
        if "password" in selector:
            return _FakeLocator(self._password_fields)
        if self._apply_button and "Apply" in selector:
            return _FakeLocator(1, visible=True, text="Apply Now")
        return _FakeLocator(0)

    async def evaluate(self, _js):
        return ""

    async def wait_for_load_state(self, *_a, **_kw):
        return None


class _ExpectPageCM:
    """Fakes Playwright's ``async with context.expect_page() as info: ...``."""

    def __init__(self, new_page):
        self._new_page = new_page

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    @property
    def value(self):
        async def _get():
            return self._new_page
        return _get()


class _FakeContext:
    def __init__(self, new_page):
        self._new_page = new_page

    def expect_page(self, timeout=3000):
        return _ExpectPageCM(self._new_page)


# ── 1) post-navigation blocked ATS ──────────────────────────────────────────

def test_post_navigation_into_blocked_domain_returns_blocked(monkeypatch):
    """A Step-0 deterministic Apply click can redirect into a domain on
    _blocked_auto_apply_domains (e.g. Workday) — same dead end the pre-flight
    check catches, so it must land on -3, not the retryable -2."""
    _install_common(monkeypatch)
    landing = _FakePage("https://jobs.acme.com/careers/123", apply_button=True)
    blocked_after_click = _FakePage("https://usbank.wd1.myworkdayjobs.com/en-US/apply/123")
    flow = _offsite()
    flow.context = _FakeContext(blocked_after_click)
    out = asyncio.run(flow._llm_guided_apply(landing))
    assert out == "blocked"


# ── 2) login wall, no stored credentials ────────────────────────────────────

def test_login_wall_with_no_stored_credentials_returns_blocked(monkeypatch):
    _install_common(monkeypatch)
    monkeypatch.setattr(linkedin_apply, "_find_account_for_domain", lambda _d: None)
    page = _FakePage("https://jobs.acme.com/careers/123/apply", password_fields=1)
    flow = _offsite()
    out = asyncio.run(flow._llm_guided_apply(page))
    assert out == "blocked"


# ── 3) login wall, stored credentials exist but login fails (regression) ───

def test_login_wall_with_failing_stored_credentials_stays_failed(monkeypatch):
    """The adjacent branch T34 must NOT touch: real credentials exist, the
    login attempt itself fails — a transient/credential problem, not "no
    human path exists" — so it stays -2 and is retried by --reset-failed."""
    _install_common(monkeypatch)
    monkeypatch.setattr(linkedin_apply, "_find_account_for_domain",
                        lambda _d: {"email": "a@b.com", "password": "pw"})

    async def _login_fails(self, page, email, password):
        return False
    monkeypatch.setattr(linkedin_apply.OffsiteApplyFlow, "_try_login", _login_fails)

    page = _FakePage("https://jobs.acme.com/careers/123/apply", password_fields=1)
    flow = _offsite()
    out = asyncio.run(flow._llm_guided_apply(page))
    assert out == "failed"


# ── 4)-5) T35 — pre-flight login-path check (:3212-3217), not the mid-loop
#            password-field check above. Reached when the page URL's *path*
#            matches a known login route (e.g. "/login") rather than via a
#            password field appearing mid-flow — this is the call site that
#            routes through ``_handle_auth_page`` itself, whose "credentials
#            exist but login failed" branch used to fall through into the
#            same ``return False`` as the true no-credentials branch,
#            collapsing both into "blocked" (-3, non-retryable). ────────────

def test_preflight_login_wall_with_no_stored_credentials_returns_blocked(monkeypatch):
    """No stored/discoverable credentials for the domain at all — still a
    genuine dead end, must stay -3."""
    _install_common(monkeypatch)
    monkeypatch.setattr(linkedin_apply, "_find_account_for_domain", lambda _d: None)
    page = _FakePage("https://jobs.acme.com/login")
    flow = _offsite()
    out = asyncio.run(flow._llm_guided_apply(page))
    assert out == "blocked"


def test_preflight_login_wall_with_failing_stored_credentials_returns_failed(monkeypatch):
    """Regression for T35: stored credentials exist for the domain but the
    login attempt itself fails (transient/2FA/rate-limit) — this must end
    -2 (retryable), not fall through to -3 like it did before the fix."""
    _install_common(monkeypatch)
    monkeypatch.setattr(linkedin_apply, "_find_account_for_domain",
                        lambda _d: {"email": "a@b.com", "password": "pw"})

    async def _login_fails(self, page, email, password):
        return False
    monkeypatch.setattr(linkedin_apply.OffsiteApplyFlow, "_try_login", _login_fails)

    page = _FakePage("https://jobs.acme.com/login")
    flow = _offsite()
    out = asyncio.run(flow._llm_guided_apply(page))
    assert out == "failed"
