"""Unit tests for the unified ``linkedin_apply.verify_submission`` helper (T33).

Covers the shared logic behind both ``EasyApplyFlow._check_submission_result``
(modal semantics, no navigation) and ``OffsiteApplyFlow._check_submission_result``
(URL-change analysis). The headline fix: an ATS that redirects the tab back to
its own listing / careers page after a submit (Rippling ``.../jobs?page=0`` and
similar for Greenhouse / Ashby / Lever) now counts as success instead of the old
"URL changed but no confirmation text" false negative.

No browser: ``page`` is a tiny fake exposing ``.content()`` / ``.url`` /
``.locator()``.
"""

from __future__ import annotations

import asyncio

import linkedin_apply

_verify = linkedin_apply.verify_submission


class _FakeLocator:
    def __init__(self, count: int):
        self._count = count

    async def count(self) -> int:
        return self._count


class _FakePage:
    def __init__(self, url: str, *, html: str = "<html><body>ok</body></html>",
                 applied_indicator: int = 0):
        self.url = url
        self._html = html
        self._applied_indicator = applied_indicator

    async def content(self) -> str:
        return self._html

    def locator(self, _selector: str) -> _FakeLocator:
        return _FakeLocator(self._applied_indicator)


def _run(page, **kw):
    return asyncio.run(_verify(page, **kw))


# ── the Rippling regression ──────────────────────────────────────────────────

def test_rippling_redirect_back_to_listing_is_success():
    page = _FakePage("https://ats.rippling.com/acme/jobs?page=0",
                     html="<html><body>Open Positions at Acme</body></html>")
    ok, signal = _run(
        page,
        url_before="https://ats.rippling.com/acme/jobs/1a2b-3c4d-5e6f/apply",
    )
    assert ok is True
    assert "listing" in signal.lower() or "careers" in signal.lower()


def test_greenhouse_redirect_to_careers_root_is_success():
    page = _FakePage("https://boards.greenhouse.io/acme",
                     html="<html><body>All jobs</body></html>")
    ok, _ = _run(
        page,
        url_before="https://boards.greenhouse.io/acme/jobs/4567890?token=abc",
    )
    assert ok is True


# ── real confirmation text ───────────────────────────────────────────────────

def test_confirmation_text_is_success_even_without_navigation():
    page = _FakePage("https://jobs.acme.com/apply/123",
                     html="<h1>Thank you for applying!</h1><p>We received your application.</p>")
    ok, signal = _run(page, url_before="https://jobs.acme.com/apply/123")
    assert ok is True
    assert "confirmation text" in signal


def test_confirmation_text_wins_over_an_ambiguous_redirect():
    page = _FakePage("https://third-party.example/whatever",
                     html="<p>Your application was sent.</p>")
    ok, _ = _run(page, url_before="https://jobs.acme.com/apply/123")
    assert ok is True


# ── failure end-states ───────────────────────────────────────────────────────

def test_redirect_to_login_page_is_not_success():
    page = _FakePage("https://acme.com/login?next=/apply",
                     html="<html><body>Please sign in to continue</body></html>")
    ok, signal = _run(page, url_before="https://acme.com/apply/123")
    assert ok is False
    assert "auth/error" in signal


def test_redirect_to_error_page_is_not_success():
    page = _FakePage("https://acme.com/error",
                     html="<html><body>Something went wrong</body></html>")
    ok, _ = _run(page, url_before="https://acme.com/apply/123")
    assert ok is False


def test_still_on_form_with_validation_errors_is_not_success():
    page = _FakePage("https://acme.com/apply/123",
                     html="<form><span>This field is required</span></form>")
    ok, signal = _run(page, url_before="https://acme.com/apply/123")
    assert ok is False
    assert "validation" in signal


def test_ambiguous_cross_site_redirect_is_not_success():
    page = _FakePage("https://some-unrelated-tracker.net/x/y/z",
                     html="<html><body>hello</body></html>")
    ok, signal = _run(page, url_before="https://acme.com/apply/123")
    assert ok is False
    assert "ambiguous" in signal


# ── EasyApply modal semantics (no url_before) ────────────────────────────────

def test_easyapply_modal_closed_is_success():
    page = _FakePage("https://www.linkedin.com/jobs/view/123/")
    ok, signal = _run(page, modal_open=False)
    assert ok is True
    assert "modal closed" in signal


def test_easyapply_modal_still_open_no_errors_is_not_success():
    page = _FakePage("https://www.linkedin.com/jobs/view/123/")
    ok, signal = _run(page, modal_open=True)
    assert ok is False
    assert "modal still open" in signal


def test_easyapply_applied_indicator_is_success():
    page = _FakePage("https://www.linkedin.com/jobs/view/123/", applied_indicator=1)
    ok, signal = _run(page, modal_open=True)
    assert ok is True
    assert "Applied" in signal
