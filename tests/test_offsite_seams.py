"""T16b — unit coverage for the seams extracted out of the ~1,500-line
``OffsiteApplyFlow._llm_guided_apply`` step loop.

Each seam is exercised in isolation with a minimal fake ``Page`` (and the LLM /
auth helpers monkeypatched); no browser, no network, no ``claude`` subprocess.

Seams under test:
  * ``_page_snapshot``        — structured page representation (delegates to _get_page_snapshot)
  * ``_decide_action``        — the one-shot ``llm.query`` round-trip (delegates to _ask_llm_action)
  * ``_classify_domain``      — spam / blocked-ATS / dead-end hostname classification
  * ``_detect_expired``       — closed / removed / not-found job detection
  * ``_detect_terminal_state``— mid-loop reCAPTCHA / Cloudflare / Greenhouse walls
  * ``_handle_auth``          — SSO redirects, login-path pages, mid-form password gates
"""

from __future__ import annotations

import asyncio

import linkedin_apply

OFF = linkedin_apply.OffsiteApplyFlow


def _offsite(**kw):
    return OFF(
        page=None, context=None, profile={}, auto_mode=True,
        callbacks={}, generated_password="x",
        company_name="ACME", job_title="Dev", **kw,
    )


def _run(coro):
    return asyncio.run(coro)


# ── fake Page / Locator ────────────────────────────────────────────────────

class _Loc:
    def __init__(self, count=0):
        self._count = count

    @property
    def first(self):
        return self

    async def count(self):
        return self._count


class _Page:
    """Just what the seams touch: .url, .locator(sel).count(), .evaluate(js)."""

    def __init__(self, url="https://jobs.acme.com/careers/1", *,
                 body_text="", captcha=0, password_fields=0, evaluate_raises=False):
        self.url = url
        self._body_text = body_text
        self._captcha = captcha
        self._password_fields = password_fields
        self._evaluate_raises = evaluate_raises

    def locator(self, selector):
        s = selector.lower()
        if "password" in s:
            return _Loc(self._password_fields)
        if "captcha" in s or "recaptcha" in s or "g-recaptcha" in s:
            return _Loc(self._captcha)
        return _Loc(0)

    async def evaluate(self, _js):
        if self._evaluate_raises:
            raise RuntimeError("JS evaluation failed")
        return self._body_text


# ── _classify_domain ──────────────────────────────────────────────────────

def test_classify_domain_spam_returns_skipped():
    assert _offsite()._classify_domain("jobright.ai") == "skipped"
    assert _offsite()._classify_domain("www.dice.com") == "skipped"


def test_classify_domain_blocked_ats_returns_blocked():
    assert _offsite()._classify_domain("acme.myworkdayjobs.com") == "blocked"
    assert _offsite()._classify_domain("careers.airbnb.com") == "blocked"


def test_classify_domain_clean_returns_none():
    assert _offsite()._classify_domain("boards.greenhouse.io") is None
    assert _offsite()._classify_domain("jobs.lever.co") is None


def test_classify_domain_suffix_match_is_exact_not_substring():
    # list has "fetchjobs.co" — must NOT match "fetchjobs.com"
    assert _offsite()._classify_domain("www.fetchjobs.com") is None
    assert _offsite()._classify_domain("fetchjobs.co") == "skipped"


def test_classify_domain_dead_end_only_with_flag():
    off = _offsite()
    assert off._classify_domain("boards.my.greenhouse.io") is None
    assert off._classify_domain("boards.my.greenhouse.io", include_dead_end=True) == "blocked"


# ── _detect_expired ───────────────────────────────────────────────────────

def test_detect_expired_url_pattern():
    assert _run(_offsite()._detect_expired(_Page("https://x.com/job-expired/5"))) == "expired"


def test_detect_expired_body_text_phrase():
    p = _Page(body_text="Sorry, this job is no longer accepting applications.")
    assert _run(_offsite()._detect_expired(p)) == "expired"


def test_detect_expired_clean_page_returns_none():
    assert _run(_offsite()._detect_expired(_Page(body_text="Apply now! We are hiring."))) is None


def test_detect_expired_swallows_evaluate_error():
    assert _run(_offsite()._detect_expired(_Page(evaluate_raises=True))) is None


def test_detect_expired_check_url_false_ignores_url_pattern():
    p = _Page("https://x.com/404", body_text="great opportunity")
    assert _run(_offsite()._detect_expired(p, check_url=False)) is None


# ── _detect_terminal_state ────────────────────────────────────────────────

def test_terminal_state_recaptcha_widget_returns_skipped():
    assert _run(_offsite()._detect_terminal_state(_Page(captcha=1), step=2)) == "skipped"


def test_terminal_state_cloudflare_text_only_after_step_zero():
    cf = _Page(body_text="Checking your browser — Ray ID: abc123. Enable JavaScript and cookies.")
    assert _run(_offsite()._detect_terminal_state(cf, step=0)) is None
    assert _run(_offsite()._detect_terminal_state(cf, step=1)) == "skipped"


def test_terminal_state_greenhouse_security_code_auto_mode_skips():
    gh = _Page(url="https://boards.greenhouse.io/embed/job_app?token=1",
               body_text="Please enter the code we sent to verify you are human.")
    assert _run(_offsite()._detect_terminal_state(gh, step=1)) == "skipped"


def test_terminal_state_clean_page_returns_none():
    p = _Page(body_text="First name Last name")
    assert _run(_offsite()._detect_terminal_state(p, step=3)) is None


# ── _handle_auth (phase="url") ───────────────────────────────────────────

def _patch(monkeypatch, name, fn):
    monkeypatch.setattr(OFF, name, fn)


def test_handle_auth_sso_success_continues(monkeypatch):
    async def _sso_ok(self, page):
        return True
    _patch(monkeypatch, "_handle_sso_page", _sso_ok)
    _real_sleep = asyncio.sleep
    monkeypatch.setattr(linkedin_apply.asyncio, "sleep", lambda *_a, **_k: _real_sleep(0))
    off = _offsite()
    off._auth_attempted = False
    out = _run(off._handle_auth(_Page("https://login.microsoftonline.com/x"), phase="url"))
    assert out == OFF._AUTH_CONTINUE
    assert off._auth_attempted is True


def test_handle_auth_sso_failure_returns_skipped(monkeypatch):
    async def _sso_bad(self, page):
        return False
    _patch(monkeypatch, "_handle_sso_page", _sso_bad)
    off = _offsite()
    off._auth_attempted = False
    out = _run(off._handle_auth(_Page("https://accounts.google.com/o/oauth2"), phase="url"))
    assert out == "skipped"


def test_handle_auth_sso_already_attempted_returns_skipped():
    off = _offsite()
    off._auth_attempted = True
    assert _run(off._handle_auth(_Page("https://acme.okta.com/login"), phase="url")) == "skipped"


def test_handle_auth_login_path_blocked(monkeypatch):
    async def _auth_page(self, page):
        return "blocked"
    _patch(monkeypatch, "_handle_auth_page", _auth_page)
    off = _offsite()
    off._auth_attempted = False
    assert _run(off._handle_auth(_Page("https://jobs.acme.com/login"), phase="url")) == "blocked"


def test_handle_auth_login_path_failed_stays_failed(monkeypatch):
    async def _auth_page(self, page):
        return "failed"
    _patch(monkeypatch, "_handle_auth_page", _auth_page)
    off = _offsite()
    off._auth_attempted = False
    assert _run(off._handle_auth(_Page("https://jobs.acme.com/signin"), phase="url")) == "failed"


def test_handle_auth_login_path_success_continues(monkeypatch):
    async def _auth_page(self, page):
        return True
    _patch(monkeypatch, "_handle_auth_page", _auth_page)
    _real_sleep = asyncio.sleep
    monkeypatch.setattr(linkedin_apply.asyncio, "sleep", lambda *_a, **_k: _real_sleep(0))
    off = _offsite()
    off._auth_attempted = False
    out = _run(off._handle_auth(_Page("https://jobs.acme.com/register"), phase="url"))
    assert out == OFF._AUTH_CONTINUE


def test_handle_auth_login_path_already_attempted_returns_failed():
    off = _offsite()
    off._auth_attempted = True
    assert _run(off._handle_auth(_Page("https://jobs.acme.com/login"), phase="url")) == "failed"


def test_handle_auth_non_auth_url_returns_none():
    off = _offsite()
    off._auth_attempted = False
    assert _run(off._handle_auth(_Page("https://jobs.acme.com/careers/123"), phase="url")) is None


# ── _handle_auth (phase="form") — T34/T35 blocked-vs-failed split ──────────

def test_handle_auth_form_no_password_field_returns_none():
    assert _run(_offsite()._handle_auth(_Page(password_fields=0), phase="form")) is None


def test_handle_auth_form_password_no_credentials_returns_blocked(monkeypatch):
    monkeypatch.setattr(linkedin_apply, "_find_account_for_domain", lambda _d: None)
    assert _run(_offsite()._handle_auth(_Page(password_fields=1), phase="form")) == "blocked"


def test_handle_auth_form_password_stored_creds_login_ok_proceeds(monkeypatch):
    monkeypatch.setattr(linkedin_apply, "_find_account_for_domain",
                        lambda _d: {"email": "a@b.com", "password": "pw"})

    async def _login_ok(self, page, email, password):
        return True
    _patch(monkeypatch, "_try_login", _login_ok)
    out = _run(_offsite()._handle_auth(_Page(password_fields=1), phase="form"))
    assert out == OFF._AUTH_PROCEED


def test_handle_auth_form_password_stored_creds_login_fails_stays_failed(monkeypatch):
    monkeypatch.setattr(linkedin_apply, "_find_account_for_domain",
                        lambda _d: {"email": "a@b.com", "password": "pw"})

    async def _login_bad(self, page, email, password):
        return False
    _patch(monkeypatch, "_try_login", _login_bad)
    assert _run(_offsite()._handle_auth(_Page(password_fields=1), phase="form")) == "failed"


def test_handle_auth_form_try_login_exception_is_swallowed(monkeypatch):
    """master wrapped the whole probe + login attempt in one broad
    ``try/except Exception: pass`` — a throw from ``_try_login`` (dead browser
    page mid-login) was swallowed and the step loop continued, so the job
    could still recover to "applied" later. A raising ``_try_login`` must NOT
    become a hard terminal -2; ``_handle_auth`` returns None (proceed)."""
    monkeypatch.setattr(linkedin_apply, "_find_account_for_domain",
                        lambda _d: {"email": "a@b.com", "password": "pw"})

    async def _login_boom(self, page, email, password):
        raise RuntimeError("Target page, context or browser has been closed")
    _patch(monkeypatch, "_try_login", _login_boom)
    assert _run(_offsite()._handle_auth(_Page(password_fields=1), phase="form")) is None


def test_handle_auth_form_find_account_exception_is_swallowed(monkeypatch):
    """Same broad-swallow contract for a throw from ``_find_account_for_domain``
    (e.g. a malformed logins.csv row) — proceed, don't hard-fail the job."""
    def _boom(_d):
        raise RuntimeError("bad credentials file")
    monkeypatch.setattr(linkedin_apply, "_find_account_for_domain", _boom)
    assert _run(_offsite()._handle_auth(_Page(password_fields=1), phase="form")) is None


# ── _handle_auth: _auth_attempted is initialised in __init__ ──────────────

def test_auth_attempted_initialised_so_seams_are_callable_standalone():
    """The ticket wants the seams independently callable. _handle_auth reads
    self._auth_attempted; a freshly constructed flow (no _llm_guided_apply
    run) must not AttributeError."""
    off = _offsite()
    assert off._auth_attempted is False
    out = _run(off._handle_auth(_Page("https://jobs.acme.com/careers/1"), phase="url"))
    assert out is None


# ── _page_snapshot / _decide_action delegation ───────────────────────────

def test_page_snapshot_delegates_to_get_page_snapshot(monkeypatch):
    seen = {}

    async def _impl(self, page):
        seen["page"] = page
        return {"url": page.url, "fields": [], "buttons": [], "visible_text": ""}
    _patch(monkeypatch, "_get_page_snapshot", _impl)
    p = _Page()
    out = _run(_offsite()._page_snapshot(p))
    assert seen["page"] is p
    assert out["url"] == p.url


def test_decide_action_delegates_to_ask_llm_action(monkeypatch):
    seen = {}

    async def _impl(self, snapshot, step, history=None, **kw):
        seen.update(snapshot=snapshot, step=step, history=history, kw=kw)
        return {"action": "done", "reason": "confirmation visible"}
    _patch(monkeypatch, "_ask_llm_action", _impl)
    snap = {"url": "https://x/apply", "fields": [], "visible_text": "hi"}
    out = _run(_offsite()._decide_action(snap, 4, ["click:#a"], job_summary="S"))
    assert out == {"action": "done", "reason": "confirmation visible"}
    assert seen["step"] == 4
    assert seen["history"] == ["click:#a"]
    assert seen["kw"]["job_summary"] == "S"
