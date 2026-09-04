"""Unit tests for the unified ``linkedin_apply.verify_submission`` helper (T33).

Covers the shared logic behind both ``EasyApplyFlow._check_submission_result``
(modal semantics, stricter phrase subset, no navigation) and
``OffsiteApplyFlow._check_submission_result`` (URL-change analysis).

The helper only ever *grants* a success verdict, and a wrong ``True`` is
unrecoverable (``applied=1`` is not in the ``--reset-failed`` pool), so the
regression surface is the set of "looks-done-but-isn't" cases:
  - a server-side-rejected submit that bounces to a listing page
  - a stray LLM nav to a listing without a submit ever being clicked
  - an "Applied" element on a *company* careers page (Applied Materials, …)
  - a GDPR ``/consent`` interstitial after nav

No browser: ``page`` is a fake exposing ``.content()`` / ``.url`` / ``.locator()``.
"""

from __future__ import annotations

import asyncio

import linkedin_apply

_verify = linkedin_apply.verify_submission
_EASYAPPLY = linkedin_apply._EASYAPPLY_CONFIRM_PHRASES


class _FakeElement:
    def __init__(self, d: dict):
        self._d = d

    async def is_visible(self) -> bool:
        return self._d.get("visible", True)

    async def inner_text(self) -> str:
        return self._d.get("text", "")

    async def count(self) -> int:
        return 1


class _FakeLocator:
    def __init__(self, els: list[dict]):
        self._els = els

    async def count(self) -> int:
        return len(self._els)

    def nth(self, i: int) -> _FakeElement:
        return _FakeElement(self._els[i])

    @property
    def first(self) -> _FakeElement:
        return _FakeElement(self._els[0] if self._els else {"visible": False, "text": ""})


class _FakePage:
    def __init__(self, url: str, *, html: str = "<html><body>ok</body></html>",
                 alerts: tuple[str, ...] = (), buttons: tuple[str, ...] = (),
                 applied_element: bool = False):
        self.url = url
        self._html = html
        self._alerts = [{"visible": True, "text": t} for t in alerts]
        self._buttons = [{"visible": True, "text": t} for t in buttons]
        self._applied = applied_element

    async def content(self) -> str:
        return self._html

    def locator(self, selector: str) -> _FakeLocator:
        s = selector.lower()
        if "applied" in s:
            return _FakeLocator([{"visible": True, "text": "Applied"}] if self._applied else [])
        if any(k in s for k in ("alert", ".error", "field-error", "field_error",
                                "form-error", ".errors", "error-message")):
            return _FakeLocator(self._alerts)
        if "button" in s or "submit" in s:
            return _FakeLocator(self._buttons)
        return _FakeLocator([])


def _run(page, **kw):
    return asyncio.run(_verify(page, **kw))


# ── the Rippling regression (fixed, but only with corroboration) ─────────────

def test_rippling_redirect_to_listing_is_success_when_submit_was_clicked():
    page = _FakePage("https://ats.rippling.com/acme/jobs?page=0",
                     html="<html><body>Open Positions at Acme</body></html>")
    ok, signal = _run(
        page,
        url_before="https://ats.rippling.com/acme/jobs/1a2b-3c4d-5e6f/apply",
        submit_attempted=True,
    )
    assert ok is True
    assert "listing" in signal.lower() or "careers" in signal.lower()


def test_greenhouse_redirect_to_board_root_is_success():
    page = _FakePage("https://boards.greenhouse.io/acme",
                     html="<html><body>All jobs</body></html>")
    ok, _ = _run(
        page,
        url_before="https://boards.greenhouse.io/acme/jobs/4567890?token=abc",
        submit_attempted=True,
    )
    assert ok is True


# ── DANGEROUS: listing redirect that is NOT a submission ─────────────────────

def test_rejected_submit_bouncing_to_job_page_with_error_banner_is_failure():
    """(a) server-side-rejected submit → bounce to …/jobs/<id> with a validation
    banner that is NOT one of _SUBMIT_FAIL_PHRASES → must be (False, …)."""
    page = _FakePage(
        "https://acme.com/jobs/123",
        html="<html><body>Acme — Senior Engineer</body></html>",
        alerts=("Please correct the highlighted fields before continuing.",),
    )
    ok, signal = _run(page, url_before="https://acme.com/jobs/123/apply",
                      submit_attempted=True)
    assert ok is False
    assert "error banner" in signal


def test_stray_nav_to_listing_without_a_submit_click_is_failure():
    """(b) LLM navigated to /jobs and then said "done"; no submit ever happened."""
    page = _FakePage("https://acme.com/company/jobs",
                     html="<html><body>Careers</body></html>")
    ok, signal = _run(page, url_before="https://acme.com/company/jobs/123/apply",
                      submit_attempted=False)
    assert ok is False
    assert "no submit" in signal


def test_offsite_applied_element_on_careers_page_is_not_success():
    """(d) an "Applied" aria-label on a company careers page (Applied Materials /
    Applied Intuition / an "Applied" filter chip) must NOT be trusted offsite."""
    page = _FakePage("https://jobs.appliedmaterials.com/apply/123", applied_element=True)
    ok, _ = _run(page, url_before="https://jobs.appliedmaterials.com/apply/123",
                 submit_attempted=True)
    assert ok is False
    # …but the very same signal IS trusted inside the Easy Apply modal.
    ok_easyapply, sig = _run(page, modal_open=True)
    assert ok_easyapply is True and "Applied" in sig


def test_redirect_to_gdpr_consent_interstitial_is_not_success():
    """(e) '/consent' used to match the bare 'sent' success substring."""
    page = _FakePage("https://acme.com/gdpr/consent",
                     html="<html><body>Manage your cookie preferences</body></html>")
    ok, _ = _run(page, url_before="https://acme.com/apply/job-123", submit_attempted=True)
    assert ok is False


# ── confirmation text / already-applied ─────────────────────────────────────

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


def test_already_applied_page_is_treated_as_success():
    """(c) a prior application is on file — retrying is pointless."""
    page = _FakePage("https://acme.com/jobs/123/apply",
                     html="<p>You have already applied to this position.</p>")
    ok, signal = _run(page, url_before="https://acme.com/jobs/123/apply",
                      submit_attempted=True)
    assert ok is True
    assert "already-applied" in signal


# ── failure end-states ─────────────────────────────────────────────────────

def test_redirect_to_login_page_is_not_success():
    page = _FakePage("https://acme.com/login?next=/apply",
                     html="<html><body>Please sign in to continue</body></html>")
    ok, signal = _run(page, url_before="https://acme.com/apply/123", submit_attempted=True)
    assert ok is False
    assert "auth/error" in signal


def test_redirect_to_error_page_is_not_success():
    page = _FakePage("https://acme.com/error",
                     html="<html><body>Something went wrong</body></html>")
    ok, _ = _run(page, url_before="https://acme.com/apply/123", submit_attempted=True)
    assert ok is False


def test_still_on_form_with_validation_text_is_not_success():
    page = _FakePage("https://acme.com/apply/123",
                     html="<form><span>This field is required</span></form>")
    ok, signal = _run(page, url_before="https://acme.com/apply/123", submit_attempted=True)
    assert ok is False
    assert "validation" in signal


def test_ambiguous_cross_site_redirect_is_not_success():
    page = _FakePage("https://some-unrelated-tracker.net/x/y/z",
                     html="<html><body>hello</body></html>")
    ok, signal = _run(page, url_before="https://acme.com/apply/123", submit_attempted=True)
    assert ok is False
    assert "ambiguous" in signal


def test_listing_redirect_with_submit_cta_still_present_is_not_success():
    page = _FakePage("https://acme.com/jobs",
                     buttons=("Submit application",))
    ok, signal = _run(page, url_before="https://acme.com/jobs/9/apply", submit_attempted=True)
    assert ok is False
    assert "submit button is still present" in signal


# ── EasyApply modal semantics ──────────────────────────────────────────────

def test_easyapply_modal_closed_is_success():
    page = _FakePage("https://www.linkedin.com/jobs/view/123/")
    ok, signal = _run(page, modal_open=False, confirm_phrases=_EASYAPPLY)
    assert ok is True
    assert "modal closed" in signal


def test_easyapply_modal_still_open_no_errors_is_not_success():
    page = _FakePage("https://www.linkedin.com/jobs/view/123/")
    ok, signal = _run(page, modal_open=True, confirm_phrases=_EASYAPPLY)
    assert ok is False
    assert "modal still open" in signal


def test_easyapply_uses_the_stricter_phrase_subset():
    """"we received your application" is in the offsite union but NOT the Easy
    Apply subset — LinkedIn's job-view DOM carries "you applied" chrome for other
    ("similar") jobs, so the modal check must stay strict."""
    page = _FakePage("https://www.linkedin.com/jobs/view/1/",
                     html="<p>We received your application and will be in touch.</p>")
    ok_strict, _ = _run(page, modal_open=True, confirm_phrases=_EASYAPPLY)
    assert ok_strict is False
    # The offsite flow (full union) does accept it.
    ok_union, _ = _run(page, modal_open=None)
    assert ok_union is True
