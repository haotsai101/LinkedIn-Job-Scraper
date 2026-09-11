"""T51 — Workday support via "Autofill with Resume" + the generic correction
loop (Option C, see docs/TICKETS.md T51).

Covers:
  * ``myworkdayjobs.com`` / ``myworkdaysite.com`` are no longer in
    ``OffsiteApplyFlow._BLOCKED_AUTO_APPLY_DOMAINS``.
  * ``_get_page_snapshot``'s label resolver falls back to
    ``data-automation-id`` when aria-label/name/placeholder/``<label for>``
    are all absent, without changing the existing fallback priority.
  * ``_prefer_workday_autofill`` clicks "Autofill with Resume" over "Apply
    Manually" when both are offered, is a no-op off Workday, and a no-op
    when only one of the two buttons is present.
  * ``_try_register``'s Workday-specific automation-id selectors
    (email/password/verifyPassword/createAccountCheckbox/
    createAccountSubmitButton) are tried and actually fill/check/click the
    right elements on a synthetic Workday create-account fixture.

Some of the above can only be proven against a *real* DOM — a mocked
``page.evaluate()`` (as ``test_offsite_seams.py`` uses for the other seams)
never actually runs the JS snapshot walk, and Playwright locator behavior
(``:has-text()``, force-click fallbacks, ``.check()``) isn't meaningfully
fakeable either. Those tests launch a local headless Chromium page via
``playwright`` (already a project dependency — the apply agent drives the
same browser) and set its content to a synthetic, clearly-fictional
Workday-shaped fixture via ``page.route()`` request interception, so a
Workday-looking URL (``https://acme-fixture.myworkdayjobs.com/...``) is
served entirely locally — no DNS lookup, no network egress, no real Workday
page. This is NOT a live Workday application; the T51 ticket explicitly
requires that to happen separately, later, as a manual live-QA run.

Every name/email/company in these fixtures is fictional test data scoped to
this file only — nothing here reads or writes ``user_profile.json``.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

import linkedin_apply

OFF = linkedin_apply.OffsiteApplyFlow

playwright_async = pytest.importorskip(
    "playwright.async_api",
    reason="playwright not installed — Workday DOM fixture tests need a real headless page",
)
from playwright.async_api import async_playwright  # noqa: E402


def _offsite(**kw):
    kw.setdefault("profile", {})
    kw.setdefault("callbacks", {})
    kw.setdefault("company_name", "Acme Fixture Co")
    kw.setdefault("job_title", "Test Engineer")
    return OFF(
        page=None, context=None, auto_mode=True,
        generated_password="Test-Fixture-Pw1!", **kw,
    )


def _run(coro):
    return asyncio.run(coro)


_DEFAULT_URL = "https://acme-fixture.myworkdayjobs.com/en-US/careers/job/TEST-1"


@asynccontextmanager
async def _fixture_page(html: str, url: str = _DEFAULT_URL):
    """Yield a real headless Chromium page whose content is ``html``, served
    under ``url`` via request interception (no network, no real Workday).
    Skips (not fails) the test when a Chromium binary isn't available in
    this environment, e.g. ``playwright install chromium`` was never run.
    """
    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch()
        except Exception as exc:
            pytest.skip(f"headless chromium unavailable in this environment: {exc}")
        page = await browser.new_page()
        try:
            async def _handler(route):
                await route.fulfill(status=200, content_type="text/html", body=html)

            await page.route("**/*", _handler)
            await page.goto(url)
            yield page
        finally:
            await browser.close()


# ══════════════════════════════════════════════════════════════════════════
# 1) Domain no longer blocked
# ══════════════════════════════════════════════════════════════════════════

def test_workday_domains_removed_from_blocked_list():
    off = _offsite()
    assert off._classify_domain("acme.myworkdayjobs.com") is None
    assert off._classify_domain("acme.myworkdaysite.com") is None


def test_workday_domains_constant_is_not_in_blocked_list():
    assert OFF._WORKDAY_DOMAINS == ("myworkdayjobs.com", "myworkdaysite.com")
    for d in OFF._WORKDAY_DOMAINS:
        assert d not in OFF._BLOCKED_AUTO_APPLY_DOMAINS


def test_other_ats_domains_still_blocked():
    # Regression guard: the removal must be scoped to Workday only.
    off = _offsite()
    assert off._classify_domain("acme.ultipro.com") == "blocked"
    assert off._classify_domain("careers.airbnb.com") == "blocked"


# ══════════════════════════════════════════════════════════════════════════
# 2) Snapshot: data-automation-id label fallback
# ══════════════════════════════════════════════════════════════════════════

def test_snapshot_surfaces_data_automation_id_label_when_no_other_label_source():
    html = """<!doctype html><html><body>
        <input type="text" data-automation-id="legalNameSection_firstName" />
    </body></html>"""

    async def _scenario():
        async with _fixture_page(html) as page:
            return await _offsite()._get_page_snapshot(page)

    snap = _run(_scenario())
    assert len(snap["fields"]) == 1
    field = snap["fields"][0]
    # No id/name/aria-label/placeholder/<label for> exists — data-automation-id
    # is the only usable label source, so it must be surfaced rather than the
    # field silently coming through as unlabeled (and getting filtered out
    # entirely, per the accompanying filter-inclusion assertion below).
    assert field["label"] == "legalNameSection_firstName"


def test_snapshot_still_prefers_aria_label_over_automation_id():
    # Fallback ORDER must be unchanged: label[for] > aria-label > placeholder
    # > name > data-automation-id (new, last). A field with both must resolve
    # to the aria-label, not the automation id.
    html = """<!doctype html><html><body>
        <input type="text" aria-label="First Name"
               data-automation-id="legalNameSection_firstName" />
    </body></html>"""

    async def _scenario():
        async with _fixture_page(html) as page:
            return await _offsite()._get_page_snapshot(page)

    snap = _run(_scenario())
    assert len(snap["fields"]) == 1
    assert snap["fields"][0]["label"] == "First Name"


def test_snapshot_automation_id_only_field_is_not_filtered_out():
    # Before T51 this field had none of hasId/hasName/hasLabel and would have
    # been dropped by the inclusion filter before lbl() ever ran (0 fields).
    html = """<!doctype html><html><body>
        <select data-automation-id="countryDropdown">
            <option>United States</option>
            <option>Canada</option>
        </select>
    </body></html>"""

    async def _scenario():
        async with _fixture_page(html) as page:
            return await _offsite()._get_page_snapshot(page)

    snap = _run(_scenario())
    assert len(snap["fields"]) == 1
    assert snap["fields"][0]["label"] == "countryDropdown"


# ══════════════════════════════════════════════════════════════════════════
# 3) Prefer "Autofill with Resume" over "Apply Manually"
# ══════════════════════════════════════════════════════════════════════════

_AUTOFILL_VS_MANUAL_HTML = """<!doctype html><html><body>
    <button type="button" onclick="window.__clicked='autofill'">Autofill with Resume</button>
    <button type="button" onclick="window.__clicked='manual'">Apply Manually</button>
</body></html>"""


def test_prefer_workday_autofill_clicks_autofill_over_manual():
    async def _scenario():
        async with _fixture_page(_AUTOFILL_VS_MANUAL_HTML) as page:
            await _offsite()._prefer_workday_autofill(page)
            return await page.evaluate("() => window.__clicked")

    assert _run(_scenario()) == "autofill"


def test_prefer_workday_autofill_noop_off_workday_domain():
    async def _scenario():
        async with _fixture_page(
            _AUTOFILL_VS_MANUAL_HTML,
            url="https://careers.non-workday-example.com/apply/TEST-1",
        ) as page:
            await _offsite()._prefer_workday_autofill(page)
            return await page.evaluate("() => window.__clicked")

    # Domain-gated: the exact same choice screen off a Workday host must be
    # left entirely to the generic LLM step loop.
    assert _run(_scenario()) is None


def test_prefer_workday_autofill_noop_when_only_manual_present():
    html = """<!doctype html><html><body>
        <button type="button" onclick="window.__clicked='manual'">Apply Manually</button>
    </body></html>"""

    async def _scenario():
        async with _fixture_page(html) as page:
            await _offsite()._prefer_workday_autofill(page)
            return await page.evaluate("() => window.__clicked")

    assert _run(_scenario()) is None


def test_prefer_workday_autofill_noop_when_only_autofill_present():
    # Autofill already used on a prior step (only one button left) — must not
    # re-click it.
    html = """<!doctype html><html><body>
        <button type="button" onclick="window.__clicked='autofill'">Autofill with Resume</button>
    </body></html>"""

    async def _scenario():
        async with _fixture_page(html) as page:
            await _offsite()._prefer_workday_autofill(page)
            return await page.evaluate("() => window.__clicked")

    assert _run(_scenario()) is None


# ══════════════════════════════════════════════════════════════════════════
# 4) Workday-specific create-account auth selectors
# ══════════════════════════════════════════════════════════════════════════

_CREATE_ACCOUNT_HTML = """<!doctype html><html><body>
    <input type="email" data-automation-id="email" />
    <input type="password" data-automation-id="password" />
    <input type="password" data-automation-id="verifyPassword" />
    <label>
        <input type="checkbox" data-automation-id="createAccountCheckbox" />
        I agree to the Terms and Conditions
    </label>
    <button type="button" data-automation-id="createAccountSubmitButton"
            onclick="window.__submitClicked=true; location.hash='account-created';">
        Create Account
    </button>
</body></html>"""


def test_workday_auth_selectors_fill_and_submit_create_account_fixture():
    # Fictional test-profile data only — clearly scoped to this test, never
    # touches user_profile.json (see module docstring / T51 ticket constraint).
    profile = {
        "full_name": "Jamie Testperson",
        "email": "jamie.testperson@example.com",
        "phone": "",
    }
    captured: dict = {}

    async def _scenario():
        async with _fixture_page(
            _CREATE_ACCOUNT_HTML,
            url="https://acme-fixture.myworkdaysite.com/en-US/careers/job/TEST-1",
        ) as page:
            flow = _offsite(profile=profile, callbacks={"save_account": captured.update})
            ok = await flow._try_register(page, "acme-fixture.myworkdaysite.com")
            vals = await page.evaluate("""() => ({
                email: document.querySelector('[data-automation-id="email"]').value,
                password: document.querySelector('[data-automation-id="password"]').value,
                verify: document.querySelector('[data-automation-id="verifyPassword"]').value,
                checked: document.querySelector('[data-automation-id="createAccountCheckbox"]').checked,
                submitClicked: window.__submitClicked === true,
            })""")
            return ok, vals

    ok, vals = _run(_scenario())

    assert ok is True
    # The right value landed in the right element — proves the Workday
    # automation-id selectors (not a coincidental generic match) were used.
    assert vals["email"] == "jamie.testperson@example.com"
    assert vals["password"] == "Test-Fixture-Pw1!"
    assert vals["verify"] == "Test-Fixture-Pw1!"
    assert vals["checked"] is True
    assert vals["submitClicked"] is True
    # Credentials get saved for future logins on this domain (_find_account_for_domain).
    assert captured.get("email") == "jamie.testperson@example.com"
    assert captured.get("password") == "Test-Fixture-Pw1!"


def test_workday_verify_password_field_recognized_as_registration_page():
    # _try_register's "are we already on a registration page" probe must
    # recognize Workday's verifyPassword automation id, not just the generic
    # name*=confirm/repeat selectors — otherwise it would go hunting for a
    # "Create account" link that doesn't exist on this fixture and bail out.
    async def _scenario():
        async with _fixture_page(
            _CREATE_ACCOUNT_HTML,
            url="https://acme-fixture.myworkdaysite.com/en-US/careers/job/TEST-1",
        ) as page:
            confirm_field = page.locator(
                'input[name*="confirm" i], input[name*="repeat" i], '
                'input[placeholder*="confirm" i], [data-automation-id="verifyPassword"]'
            ).first
            return await confirm_field.count()

    assert _run(_scenario()) > 0
