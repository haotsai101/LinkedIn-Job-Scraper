"""OA3: fixture forms + fixture server.

Each fixture renders offline (no request leaves 127.0.0.1), has the fields the
later tickets rely on, and its submit button only increments ``/__submissions``.
Skipped when the Playwright Chromium binary isn't installed.
"""
from __future__ import annotations

import asyncio
import json
import urllib.request
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from tests.fixtures.offsite.serve import FIXTURES, fixture_server

RESUME = Path(__file__).resolve().parents[1] / "fixtures" / "offsite" / "dummy_resume.pdf"


@pytest.fixture(scope="module")
def base():
    with fixture_server() as url:
        yield url


def _get(url):
    with urllib.request.urlopen(url) as r:
        return r.read().decode()


def _post(url):
    req = urllib.request.Request(url, data=b"", method="POST")
    with urllib.request.urlopen(req) as r:
        return r.read().decode()


def _count(base):
    return json.loads(_get(f"{base}/__submissions"))["count"]


@pytest.fixture(autouse=True)
def _reset(base):
    _post(f"{base}/__reset")


@pytest.fixture(scope="module", autouse=True)
def _require_chromium():
    async def probe():
        async with async_playwright() as pw:
            await (await pw.chromium.launch(headless=True)).close()

    try:
        asyncio.run(probe())
    except Exception as e:  # pragma: no cover - environment dependent
        pytest.skip(f"Playwright Chromium not available: {e}")


async def _with_page(fn, base):
    """Run ``fn(page, external_requests)`` in headless Chromium; every request to a
    host other than 127.0.0.1 is aborted and recorded."""
    external: list[str] = []

    async def gate(route):
        if route.request.url.startswith(base) or route.request.url.startswith("data:"):
            await route.continue_()
        else:
            external.append(route.request.url)
            await route.abort()

    async with async_playwright() as pw:
        b = await pw.chromium.launch(headless=True)
        page = await b.new_page()
        await page.route("**/*", gate)
        try:
            return await fn(page, external)
        finally:
            await b.close()


def test_index_and_counter(base):
    assert set(FIXTURES) >= {"greenhouse", "ashby", "multipage"}
    idx = _get(f"{base}/")
    for n in ("greenhouse", "ashby", "multipage"):
        assert f"/{n}.html" in idx
    assert _count(base) == 0


@pytest.mark.parametrize("name,labels,submit", [
    ("greenhouse", ["First Name", "Email", "Will you require sponsorship"], "Submit application"),
    ("ashby", ["Email", "Resume", "Will you now or in the future require sponsor"],
     "Submit Application"),
])
def test_recorded_fixture_offline_and_submits_only_locally(base, name, labels, submit):
    async def go(page, external):
        await page.goto(f"{base}/{name}.html")
        text = await page.inner_text("body")
        for label in labels:
            assert label in text, label
        assert await page.locator("script").count() == 0
        assert await page.locator("input[type=file]").count() >= 1
        await page.get_by_role("button", name=submit, exact=True).click()
        await page.wait_for_load_state()
        assert "application received" in await page.inner_text("body")
        return external

    assert asyncio.run(_with_page(go, base)) == []
    assert _count(base) == 1


def test_multipage_validation_steps_and_submit(base):
    async def go(page, external):
        await page.goto(f"{base}/multipage.html")
        # empty step 1 → Next is refused with errors
        await page.click("#next")
        assert "Please fix" in await page.inner_text("#error-summary")
        assert await page.is_visible("#first_name")
        # step 1
        await page.fill("#first_name", "Test")
        await page.fill("#last_name", "Person")
        await page.fill("#email", "test@example.com")
        await page.select_option("#country", "United States")
        await page.set_input_files("#resume", RESUME)
        await page.click("#next")
        assert await page.is_visible("#years_python")
        # non-numeric years → refused
        await page.fill("#years_python", "four")
        await page.check("input[name=work_authorized][value=yes]")
        await page.check("input[name=sponsorship][value=yes]")
        await page.click("#next")
        assert "Enter a number." in await page.inner_text("body")
        await page.fill("#years_python", "4")
        await page.click("#next")
        # step 3: submit hidden until here, certify required
        assert await page.is_visible("#submit")
        await page.get_by_role("button", name="Submit Application").click()
        assert "Please fix" in await page.inner_text("#error-summary")
        await page.check("#certify")
        await page.get_by_role("button", name="Submit Application").click()
        await page.wait_for_load_state()
        assert "FIX-0001" in await page.inner_text("body")
        return external

    assert asyncio.run(_with_page(go, base)) == []
    info = json.loads(_get(f"{base}/__submissions"))
    assert info["count"] == 1
    assert {"first_name", "resume", "sponsorship", "certify"} <= set(info["last"]["fields"])


def test_multipage_enter_in_text_field_submits(base):
    """Documents the hazard OA5's Enter guard exists for: implicit submission."""
    async def go(page, external):
        await page.goto(f"{base}/multipage.html")
        await page.fill("#first_name", "Test")
        await page.fill("#last_name", "Person")
        await page.fill("#email", "test@example.com")
        await page.select_option("#country", "United States")
        await page.set_input_files("#resume", RESUME)
        await page.press("#email", "Enter")
        await page.wait_for_load_state()
        return external

    asyncio.run(_with_page(go, base))
    assert _count(base) == 1
