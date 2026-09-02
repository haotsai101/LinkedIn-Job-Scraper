"""Unit tests for ``OffsiteApplyFlow._detect_bot_wall`` (T27 / T28).

The method must skip a job it can't get past a bot wall for, but must NOT
pre-empt the legitimate email-verification step of an account-creation offsite
flow — ``_GREENHOUSE_SECURITY_SIGNALS`` ("verification code", "enter the code",
…) overlaps the step loop's ``_email_signals`` handler, which ``run_session``
supports on purpose via ``OffsiteApplyFlow.inbox``. Hence the host gate.

No browser: ``page`` is a tiny fake exposing only ``.url`` / ``.locator`` /
``.evaluate`` and the flow is a ``SimpleNamespace`` with just ``auto_mode``.
"""

from __future__ import annotations

import asyncio
import types

import linkedin_apply


class _FakeLocator:
    def __init__(self, count: int):
        self._count = count

    async def count(self) -> int:
        return self._count


class _FakePage:
    def __init__(self, url: str, *, text: str = "", captcha_widgets: int = 0):
        self.url = url
        self._text = text
        self._captcha_widgets = captcha_widgets

    def locator(self, _selector: str) -> _FakeLocator:
        return _FakeLocator(self._captcha_widgets)

    async def evaluate(self, _js: str) -> str:
        return self._text


def _detect(page: _FakePage, *, auto_mode: bool) -> str:
    flow = types.SimpleNamespace(auto_mode=auto_mode)
    return asyncio.run(linkedin_apply.OffsiteApplyFlow._detect_bot_wall(flow, page))


# ── the regression: verification text off greenhouse.io must NOT skip ──────────

def test_verification_text_on_non_greenhouse_page_is_not_a_bot_wall():
    page = _FakePage(
        "https://jobs.acme-ats.com/apply/step-2",
        text="Please enter the verification code we emailed you to continue.",
    )
    assert _detect(page, auto_mode=True) == ""


# ── greenhouse.io text challenge: skip only in --auto ─────────────────────────

def test_greenhouse_security_text_is_a_bot_wall_in_auto():
    page = _FakePage(
        "https://boards.greenhouse.io/acme/jobs/123",
        text="Security check — enter the code shown to prove you're human.",
    )
    assert _detect(page, auto_mode=True).startswith("Greenhouse")


def test_greenhouse_security_text_is_ignored_in_interactive_mode():
    page = _FakePage(
        "https://job-boards.greenhouse.io/acme/jobs/123",
        text="prove you are human — please enter the characters below",
    )
    assert _detect(page, auto_mode=False) == ""


# ── reCAPTCHA widget: unambiguous, any host, any mode ─────────────────────────

def test_recaptcha_widget_is_a_bot_wall_regardless_of_host_or_mode():
    page = _FakePage("https://jobs.acme-ats.com/apply", captcha_widgets=1)
    assert _detect(page, auto_mode=False) == "reCAPTCHA widget"


def test_clean_page_is_not_a_bot_wall():
    page = _FakePage(
        "https://boards.greenhouse.io/acme/jobs/123",
        text="First name, last name, resume. Submit application.",
    )
    assert _detect(page, auto_mode=True) == ""
