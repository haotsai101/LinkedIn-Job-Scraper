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


def test_detect_expired_uses_supplied_body_text_without_reading_page():
    """When the caller passes body_text, _detect_expired does no page.evaluate
    (the mid-loop path shares its Cloudflare read) and stays silent on a
    would-be read failure."""
    p = _Page(evaluate_raises=True)  # any internal read would blow up / warn
    assert _run(_offsite()._detect_expired(
        p, check_url=False, body_text="This job is no longer accepting applications")) == "expired"
    assert _run(_offsite()._detect_expired(
        p, check_url=False, body_text="")) is None          # empty = read failed upstream, silent


# ── _detect_terminal_state ────────────────────────────────────────────────

def test_terminal_state_step_gt_zero_reads_body_text_once():
    """One page.evaluate for step>0 (shared by the Cloudflare check and the
    expired check) — was 2 in the first PR-2 draft."""
    p = _Page(body_text="First name Last name")
    p._reads = 0
    _orig = p.evaluate

    async def _counting(js):
        p._reads += 1
        return await _orig(js)
    p.evaluate = _counting
    assert _run(_offsite()._detect_terminal_state(p, step=2)) is None
    assert p._reads == 1

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


# ══════════════════════════════════════════════════════════════════════════
# _execute_action / _StepState  (T16b PR 2)
# ══════════════════════════════════════════════════════════════════════════
#
# The scroll/upload/fill/select/click dispatch inside _execute_action is a
# byte-for-byte extraction of code that was inline in _llm_guided_apply (the
# one intended change: `_submit_clicked = True` -> `state.submit_clicked =
# True`). It has no pre-existing unit coverage — it needs a real browser. These
# tests target the *seam boundary*: dispatch routing, terminal-return
# propagation, and the mutable state plumbed via _StepState (page rebind on a
# new tab, #<digit>-id selector normalisation, submit_clicked latch,
# forced_filled shared-dict mutation).

_SS = linkedin_apply._StepState


class _ExecLoc:
    """Configurable fake Locator for the exec dispatch. Records .click() calls."""

    def __init__(self, *, count=1, visible=True, enabled=True, text="",
                 attrs=None, tag="button", checked=False, input_value=""):
        self._count = count
        self._visible = visible
        self._enabled = enabled
        self._text = text
        self._attrs = attrs or {}
        self._tag = tag
        self._checked = checked
        self._input_value = input_value
        self.clicks = []
        self.filled = []
        self.typed = []

    @property
    def first(self):
        return self

    def nth(self, _i):
        return self

    async def count(self):
        return self._count

    async def is_visible(self):
        return self._visible

    async def is_enabled(self):
        return self._enabled

    async def is_checked(self):
        return self._checked

    async def inner_text(self):
        return self._text

    async def input_value(self):
        return self._input_value

    async def get_attribute(self, name):
        return self._attrs.get(name)

    async def evaluate(self, js, *_a):
        if "tagName" in js:
            return self._tag
        if "getAttribute('type')" in js or "el.type" in js:
            return self._attrs.get("type", "")
        if "aria-disabled" in js:
            return self._attrs.get("aria-disabled") == "true"
        return ""

    async def click(self, **kw):
        self.clicks.append(kw)

    async def check(self):
        self._checked = True

    async def fill(self, v):
        self.filled.append(v)

    async def type(self, v, **_kw):
        self.typed.append(v)

    async def press(self, _key):
        pass

    async def select_option(self, **_kw):
        pass

    async def set_input_files(self, _p):
        pass

    async def wait_for(self, **_kw):
        pass

    async def scroll_into_view_if_needed(self):
        pass


class _ExecPage:
    def __init__(self, url="https://jobs.acme.com/apply", *, locators=None):
        self.url = url
        self._locators = locators or {}
        self.evaluated = []
        self.default_loc = _ExecLoc(count=0)

    def locator(self, selector):
        for key, loc in self._locators.items():
            if key in selector:
                return loc
        return self.default_loc

    def get_by_label(self, *_a, **_kw):
        return _ExecLoc(count=0)

    async def evaluate(self, js, *_a):
        self.evaluated.append(js)
        return ""

    async def wait_for_load_state(self, *_a, **_kw):
        pass


class _ExecCtxNewTab:
    """context.expect_page(...) that yields a fresh tab (a click opened one)."""

    def __init__(self, new_page):
        self._new_page = new_page

    def expect_page(self, timeout=3000):
        np = self._new_page

        class _CM:
            async def __aenter__(self_):
                return self_

            async def __aexit__(self_, *_e):
                return False

            @property
            def value(self_):
                async def _get():
                    return np
                return _get()

        return _CM()


class _ExecCtxNoTab:
    def expect_page(self, timeout=3000):
        class _CM:
            async def __aenter__(self_):
                return self_

            async def __aexit__(self_, *_e):
                return False

            @property
            def value(self_):
                async def _get():
                    raise Exception("no new tab")
                return _get()

        return _CM()


def _exec(flow, action_type, state, *, text="", value=""):
    return _run(flow._execute_action(action_type, text, value, state))


# ── _StepState ───────────────────────────────────────────────────────────

def test_stepstate_holds_the_four_mutable_slots():
    ff = {"x": "1"}
    p = _ExecPage()
    st = _SS(p, "#sel", ff, False)
    assert st.page is p and st.selector == "#sel"
    assert st.forced_filled is ff and st.submit_clicked is False


# ── dispatch routing ─────────────────────────────────────────────────────

def test_execute_scroll_calls_scrollby_and_returns_none():
    page = _ExecPage()
    st = _SS(page, "", {}, False)
    assert _exec(_offsite(), "scroll", st) is None
    assert any("scrollBy" in js for js in page.evaluated)


def test_execute_unknown_action_type_is_a_noop():
    page = _ExecPage()
    st = _SS(page, "#whatever", {}, False)
    assert _exec(_offsite(), "wait", st) is None
    assert st.page is page and st.selector == "#whatever"


# ── new-tab page rebind (the sharp edge) ─────────────────────────────────

def test_execute_click_opening_new_tab_rebinds_state_page():
    landing = _ExecPage("https://jobs.acme.com/listing")
    new_tab = _ExecPage("https://ats.example.com/form")
    nav_btn = _ExecLoc(count=1, visible=True, text="View posting", tag="a")
    landing._locators = {"View posting": nav_btn, "a:has-text": nav_btn}
    flow = _offsite()
    flow.context = _ExecCtxNewTab(new_tab)
    st = _SS(landing, 'a:has-text("View posting")', {}, False)
    out = _exec(flow, "click", st, text="View posting")
    assert out is None
    assert st.page is new_tab            # orchestrator must see the new tab
    assert st.submit_clicked is False


def test_execute_click_no_new_tab_keeps_state_page():
    landing = _ExecPage("https://jobs.acme.com/listing")
    btn = _ExecLoc(count=1, visible=True, text="Show more", tag="button")
    landing._locators = {"Show more": btn, "button:has-text": btn}
    flow = _offsite()
    flow.context = _ExecCtxNoTab()
    st = _SS(landing, 'button:has-text("Show more")', {}, False)
    assert _exec(flow, "click", st, text="Show more") is None
    assert st.page is landing
    assert btn.clicks  # it was clicked


# ── submit-click: submit_clicked latch + _handle_submit propagation ──────

def test_execute_click_submit_button_latches_and_returns_handle_submit(monkeypatch):
    page = _ExecPage("https://ats.example.com/form")
    submit_btn = _ExecLoc(count=1, visible=True, text="Submit application",
                          tag="button", attrs={"aria-disabled": "false"})
    form_inputs = _ExecLoc(count=3)
    page._locators = {"Submit application": submit_btn,
                      'button:has-text("Submit application")': submit_btn,
                      "input:not(": form_inputs}

    async def _fake_handle_submit(self, pg, btn):
        return "applied"
    _patch(monkeypatch, "_handle_submit", _fake_handle_submit)

    flow = _offsite()
    st = _SS(page, 'button:has-text("Submit application")', {}, False)
    out = _exec(flow, "click", st, text="Submit application")
    assert out == "applied"          # terminal — propagates out of the loop
    assert st.submit_clicked is True  # latched for verify_submission corroboration


def test_execute_click_disabled_submit_latches_but_continues(monkeypatch):
    page = _ExecPage("https://ats.example.com/form")
    submit_btn = _ExecLoc(count=1, visible=True, text="Submit application",
                          tag="button", attrs={"aria-disabled": "true"})
    form_inputs = _ExecLoc(count=2)
    page._locators = {"Submit application": submit_btn,
                      'button:has-text("Submit application")': submit_btn,
                      "input:not(": form_inputs}

    async def _boom_handle_submit(self, pg, btn):
        raise AssertionError("_handle_submit must NOT be called for a disabled button")
    _patch(monkeypatch, "_handle_submit", _boom_handle_submit)

    flow = _offsite()
    st = _SS(page, 'button:has-text("Submit application")', {}, False)
    out = _exec(flow, "click", st, text="Submit application")
    assert out is None               # loop continues so the LLM sees validation errors
    assert st.submit_clicked is True


# ── select: #<digit>-id selector normalisation into state.selector ───────

def test_execute_select_normalises_digit_id_selector():
    page = _ExecPage("https://ats.example.com/form")
    page._locators = {'[id="12345"]': _ExecLoc(count=0)}  # not found -> no-op body
    flow = _offsite()
    st = _SS(page, "#12345", {}, False)
    _exec(flow, "select", st, value="United States")
    assert st.selector == '[id="12345"]'   # loop's history entry uses the normalised form


def test_execute_select_plain_selector_unchanged():
    page = _ExecPage("https://ats.example.com/form")
    page._locators = {"#country": _ExecLoc(count=0)}
    flow = _offsite()
    st = _SS(page, "#country", {}, False)
    _exec(flow, "select", st, value="United States")
    assert st.selector == "#country"


# ── fill: CAPTCHA-exception guard returns "skipped" ─────────────────────

def test_execute_fill_captcha_exception_returns_skipped():
    class _RaiseLoc(_ExecLoc):
        async def count(self):
            raise RuntimeError("hCaptcha challenge frame blocked the fill")

    page = _ExecPage("https://ats.example.com/form")
    page._locators = {"#name": _RaiseLoc()}
    flow = _offsite()
    st = _SS(page, "#name", {}, False)
    assert _exec(flow, "fill", st, value="Jordan") == "skipped"


# ── fill: forced_filled is the caller's dict, mutated in place ──────────

def test_execute_fill_shares_forced_filled_dict():
    # a React-Select combobox whose value commits internally -> forced_filled[id]
    combo = _ExecLoc(count=1, visible=True, text="Prefer not to say", tag="input",
                     attrs={"role": "combobox", "id": "eeo-gender"}, input_value="")
    option = _ExecLoc(count=1, visible=True, text="Prefer not to say")
    page = _ExecPage("https://ats.example.com/form")
    page._locators = {"#eeo-gender": combo, '[id="eeo-gender"]': combo,
                      'role="option"': option, "option": option}
    ff = {}
    flow = _offsite()
    st = _SS(page, "#eeo-gender", ff, False)
    _exec(flow, "fill", st, value="Prefer not to say")
    assert st.forced_filled is ff            # same object, no reassignment
    assert ff.get("eeo-gender") == "Prefer not to say"


# ── T31/T32 numeric coercion stays in the orchestrator, unchanged ───────

def test_coerce_numeric_answer_still_reduces_prose_to_bare_int():
    # _execute_action does NOT touch this — it runs in _llm_guided_apply before
    # the dispatch. Guard that the helper the orchestrator calls is intact.
    out = linkedin_apply._coerce_numeric_answer(
        "Rate your Python experience (1-10)",
        "I'd rate my Python experience about an 8 out of 10", "text", {},
    )
    assert out == "8"
