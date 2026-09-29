"""OA5: no agent submit — MCP-level checks + in-page lock, on every fixture.

The bar (docs/TICKETS.md OA5): with the lock on, no sequence of allowlisted tool
calls — nor a direct click / Enter / form.submit() on the shared browser —
increases the fixture server's ``/__submissions``. After ``unlock()`` a human
click submits normally.

Setup steps drive the page directly through the shared browser; the attack
steps go through guard-mcp over HTTP exactly as a model would.
"""
from __future__ import annotations

import json
import re
import urllib.request
from pathlib import Path

import pytest
from mcp import Client

from offsite.guard_mcp import BLOCKED_PREFIX
from offsite.guards import SubmitGuard, is_submit_label

RESUME = Path(__file__).resolve().parents[1] / "fixtures" / "offsite" / "dummy_resume.pdf"


# ── unit ──────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("label,expected", [
    ("Submit application", True),
    ("Submit Application", True),
    ("Submit", True),
    ("Send application", True),
    ("Send my application", True),
    ("Complete application", True),
    ("Save and submit", True),          # conservative: submit wording wins
    ("Review and Submit", True),
    ("Apply now", True),
    ("#submit", True),
    ("Next", False),
    ("Save and continue", False),
    ("Continue", False),
    ("Apply", False),                   # listing-page opener; the page lock judges it
    ("Apply for this job", False),
    ("Upload resume", False),
    ("Back", False),
    ("", False),
])
def test_is_submit_label(label, expected):
    assert is_submit_label(label) is expected


# ── live ──────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def lg(live_guard):
    sg = SubmitGuard(live_guard.browser, resume_path=RESUME)
    live_guard.run(sg.install(live_guard.guard))
    live_guard.sg = sg
    return live_guard


@pytest.fixture(autouse=True)
def _fresh(lg):
    _post(f"{lg.base}/__reset")
    lg.run(lg.sg.lock())
    yield
    lg.run(lg.sg.lock())


def _post(url):
    urllib.request.urlopen(urllib.request.Request(url, data=b"", method="POST")).read()


def _count(lg) -> int:
    with urllib.request.urlopen(f"{lg.base}/__submissions") as r:
        return json.loads(r.read())["count"]


def _text(result) -> str:
    return "".join(getattr(c, "text", "") or "" for c in result.content)


def _ref(snapshot: str, role: str, name: str) -> str:
    m = re.search(rf'{role} "{re.escape(name)}"[^\n]*?\[ref=([a-z0-9]+)\]', snapshot)
    assert m, f"{role} {name!r} not in snapshot:\n{snapshot[:2500]}"
    return m.group(1)


def mcp(lg, fn):
    async def go():
        async with Client(lg.guard.url) as c:
            return await fn(c)
    return lg.run(go())


def page(lg):
    return lg.browser.page


def _fill_multipage_to_step3(lg):
    p = page(lg)

    async def go():
        await p.fill("#first_name", "Test")
        await p.fill("#last_name", "Person")
        await p.fill("#email", "test@example.com")
        await p.select_option("#country", "United States")
        await p.set_input_files("#resume", str(RESUME))
        await p.click("#next")                    # Next still works while locked
        await p.fill("#years_python", "4")
        await p.check("input[name=work_authorized][value=yes]")
        await p.check("input[name=sponsorship][value=yes]")
        await p.click("#next")
        await p.check("#certify")
        assert await p.is_visible("#submit")

    lg.run(go())


def _blocked(r) -> bool:
    return r.is_error and _text(r).startswith(BLOCKED_PREFIX)


def test_page_is_locked_after_install_and_on_new_documents(lg):
    lg.open("multipage.html")
    assert lg.run(lg.sg.is_locked()) is True


def test_enter_is_blocked_at_both_layers(lg):
    lg.open("multipage.html")
    p = page(lg)
    lg.run(p.fill("#first_name", "Test"))
    lg.run(p.fill("#last_name", "Person"))
    lg.run(p.fill("#email", "test@example.com"))
    lg.run(p.select_option("#country", "United States"))
    lg.run(p.set_input_files("#resume", str(RESUME)))

    async def attacks(c):
        snap = _text(await c.call_tool("browser_snapshot", {}))
        email = _ref(snap, "textbox", "Email *")
        r1 = await c.call_tool("browser_press_key", {"key": "Enter"})
        r2 = await c.call_tool("browser_type", {"element": "Email", "target": email,
                                                "text": "x", "submit": True})
        return r1, r2

    r1, r2 = mcp(lg, attacks)
    assert _blocked(r1) and "Enter" in _text(r1)
    assert _blocked(r2) and "submit: true" in _text(r2)
    # bypass the MCP layer entirely: the in-page lock still swallows implicit submission
    lg.run(p.press("#email", "Enter"))
    lg.run(p.wait_for_timeout(300))
    assert _count(lg) == 0
    assert "BLOCKED by guard" in lg.run(p.inner_text("#__oa_guard_toast"))


def test_multipage_final_submit_blocked_every_way(lg):
    lg.open("multipage.html")
    _fill_multipage_to_step3(lg)
    p = page(lg)

    async def by_ref(c):
        snap = _text(await c.call_tool("browser_snapshot", {}))
        ref = _ref(snap, "button", "Submit Application")
        r_ref = await c.call_tool("browser_click", {"element": "Submit Application",
                                                    "target": ref})
        r_sel = await c.call_tool("browser_click", {"element": "the button",
                                                    "target": "#submit"})
        # MCP layer fooled: vague description + ref it has never seen a name for
        lg.sg._refs.clear()
        r_vague = await c.call_tool("browser_click", {"element": "the red button",
                                                      "target": ref})
        return r_ref, r_sel, r_vague

    r_ref, r_sel, r_vague = mcp(lg, by_ref)
    assert _blocked(r_ref) and "report_ready" in _text(r_ref)
    assert _blocked(r_sel)
    # forwarded, but the page lock ate the click and the model sees why
    assert not r_vague.is_error
    assert "BLOCKED by guard" in _text(r_vague)

    # (the form has <button id="submit">, which shadows form.submit — call via prototype)
    for js in ("HTMLFormElement.prototype.requestSubmit.call(document.forms[0])",
               "HTMLFormElement.prototype.submit.call(document.forms[0])"):
        lg.run(p.evaluate(js))
    lg.run(p.click("#submit"))
    lg.run(p.keyboard.press("Enter"))
    lg.run(p.wait_for_timeout(300))
    assert _count(lg) == 0
    assert "Acme Robotics" in lg.run(p.title())   # still on the form


@pytest.mark.parametrize("fixture,button", [
    ("greenhouse.html", "Submit application"),
    ("ashby.html", "Submit Application"),
])
def test_recorded_ats_submit_blocked(lg, fixture, button):
    lg.open(fixture)

    async def attack(c):
        snap = _text(await c.call_tool("browser_snapshot", {}))
        ref = _ref(snap, "button", button)
        r1 = await c.call_tool("browser_click", {"element": button, "target": ref})
        lg.sg._refs.clear()
        r2 = await c.call_tool("browser_click", {"element": "that one", "target": ref})
        return r1, r2

    r1, r2 = mcp(lg, attack)
    assert _blocked(r1)
    assert not r2.is_error and "BLOCKED by guard" in _text(r2)
    lg.run(page(lg).get_by_role("button", name=button, exact=True).click())
    lg.run(page(lg).wait_for_timeout(300))
    assert _count(lg) == 0


def test_uploads_resume_only_and_never_into_cover_letter(lg):
    lg.open("multipage.html")

    async def go(c):
        snap = _text(await c.call_tool("browser_snapshot", {}))
        # resume field → file chooser → resume: allowed
        await c.call_tool("browser_click", {"element": "Resume / CV",
                                            "target": _ref(snap, "button", "Resume / CV *")})
        other = await c.call_tool("browser_file_upload", {"paths": ["/etc/hosts"]})
        ok = await c.call_tool("browser_file_upload", {"paths": [str(RESUME)]})
        return other, ok

    other, ok = mcp(lg, go)
    assert _blocked(other) and "only file you may upload is the resume" in _text(other)
    assert not ok.is_error, _text(ok)
    assert lg.run(page(lg).evaluate("document.querySelector('#resume').files.length")) == 1

    # step 2 has the cover-letter upload
    p = page(lg)
    for sel, val in (("#first_name", "Test"), ("#last_name", "Person"),
                     ("#email", "test@example.com")):
        lg.run(p.fill(sel, val))
    lg.run(p.select_option("#country", "United States"))
    lg.run(p.click("#next"))

    async def cover(c):
        snap = _text(await c.call_tool("browser_snapshot", {}))
        await c.call_tool("browser_click", {
            "element": "Cover letter (optional)",
            "target": _ref(snap, "button", "Cover letter (optional)")})
        bad = await c.call_tool("browser_file_upload", {"paths": [str(RESUME)]})
        cancel = await c.call_tool("browser_file_upload", {"paths": []})
        return bad, cancel

    bad, cancel = mcp(lg, cover)
    assert _blocked(bad) and "cover-letter" in _text(bad)
    assert not cancel.is_error, _text(cancel)
    assert lg.run(p.evaluate("document.querySelector('#cover_letter_file').files.length")) == 0


def test_unlock_lets_the_human_submit_then_lock_restores(lg):
    lg.open("multipage.html")
    _fill_multipage_to_step3(lg)
    lg.run(lg.sg.unlock())
    assert lg.run(lg.sg.is_locked()) is False
    lg.run(page(lg).click("#submit"))
    lg.run(page(lg).wait_for_load_state())
    assert _count(lg) == 1
    assert "FIX-0001" in lg.run(page(lg).inner_text("body"))
    # the confirmation page is a same-tab navigation: still unlocked for the human
    assert lg.run(lg.sg.is_locked()) is False
    lg.run(lg.sg.lock())
    lg.open("multipage.html")
    assert lg.run(lg.sg.is_locked()) is True


def test_pages_the_model_opens_are_locked_too(lg):
    """Navigation and new tabs made through Playwright MCP get the init script."""
    async def go(c):
        await c.call_tool("browser_navigate", {"url": f"{lg.base}/ashby.html"})
        await c.call_tool("browser_tabs", {"action": "new", "url": f"{lg.base}/multipage.html"})

    mcp(lg, go)
    pages = lg.browser.context.pages
    for url_part in ("ashby.html", "multipage.html"):
        p = next(p for p in pages if url_part in p.url)
        assert lg.run(p.evaluate("() => window.__oaIsLocked()")) is True, url_part
    # tidy: close the extra tab so later tests see one page
    extra = [p for p in pages if "multipage.html" in p.url and p is not pages[0]]
    for p in extra:
        lg.run(p.close())
