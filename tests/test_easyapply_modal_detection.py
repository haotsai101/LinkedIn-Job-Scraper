"""EasyApplyFlow must find LinkedIn's dialog-less Easy Apply container.

LinkedIn's newer Easy Apply UI has no role="dialog" and only hashed class
names, so the legacy ``_MODAL_SELECTORS`` all miss. Before ``_tag_modal`` the
field scan fell back to the whole page (filling LinkedIn's search box and
language picker) and ``_get_modal_text()`` returned "", which disabled the
stuck-step detection — a form with an unanswerable question burned all 25
steps instead of failing after 3 stuck clicks.

The fixture mirrors markup captured from a live form (job 4475874407,
2026-10-05): hidden 0x0 radio inputs inside ``<div role="radio"
aria-label="<question>">`` wrappers with the option text in a ``<p>``.

Drives real headless Chromium against static HTML (no network); skipped when
the Playwright browser binary is not installed.
"""

from __future__ import annotations

import asyncio

import pytest

import linkedin_apply

_HTML = """
<html><body>
  <header>
    <input type="text" aria-label="I'm looking for…">
    <select aria-label="Select language"><option value="en_US">English</option></select>
  </header>
  <main>
    <div class="dj9alx">
      <div class="dj9aph">
        <h2>Apply to RedRiver Systems, LLC</h2>
        <div>
          <label for="yrs">How many years of work experience do you have with LangChain?</label>
          <input id="yrs" type="text" value="1">
        </div>
        <fieldset role="radiogroup" aria-describedby="error-message-_r_1b_">
          <div>
            <div role="radio" tabindex="0" aria-checked="false"
                 aria-label="Are you willing to undergo a background check?">
              <div><input id="_r_1c_" type="radio" name="radio-group-_r_1b_"
                          style="position:absolute;opacity:0;width:0;height:0">
                   <label for="_r_1c_"></label></div>
              <div><p>Yes</p></div>
            </div>
            <div role="radio" tabindex="0" aria-checked="false"
                 aria-label="Are you willing to undergo a background check?">
              <div><input id="_r_1d_" type="radio" name="radio-group-_r_1b_"
                          style="position:absolute;opacity:0;width:0;height:0">
                   <label for="_r_1d_"></label></div>
              <div><p>No</p></div>
            </div>
          </div>
        </fieldset>
        <footer><button>Back</button><button>Next</button></footer>
      </div>
    </div>
  </main>
</body></html>
"""


async def _with_flow(html: str, fn):
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(headless=True)
        except Exception as exc:  # browser binary not installed
            pytest.skip(f"chromium unavailable: {exc}")
        try:
            page = await browser.new_page()
            await page.set_content(html)
            flow = linkedin_apply.EasyApplyFlow(page, {}, auto_mode=True, callbacks={})
            return await fn(flow)
        finally:
            await browser.close()


def test_field_scan_is_scoped_to_dialogless_container():
    async def run(flow):
        return await flow._collect_fields_playwright()

    fields = asyncio.run(_with_flow(_HTML, run))
    labels = [f["label"] for f in fields]
    assert "I'm looking for…" not in labels
    assert "Select language" not in labels
    radio = next(f for f in fields if f["kind"] == "radio")
    assert radio["label"] == "Are you willing to undergo a background check?"
    assert radio["options"] == ["Yes", "No"]
    assert radio["current_value"] == ""


def test_modal_text_and_open_state_without_dialog():
    async def run(flow):
        return await flow._get_modal_text(), await flow._is_modal_open()

    text, is_open = asyncio.run(_with_flow(_HTML, run))
    assert text.startswith("Apply to RedRiver Systems, LLC")
    assert is_open is True


_RERENDER_JS = """
<script>
// Mimic LinkedIn: selecting an option re-renders the whole group with fresh
// input ids, so the id the scan captured no longer exists after the click.
let gen = 0;
document.addEventListener('click', e => {
  const w = e.target.closest('[role=radio]');
  if (!w) return;
  const fs = w.closest('fieldset');
  const pick = [...fs.querySelectorAll('[role=radio]')].indexOf(w);
  gen += 1;
  fs.querySelectorAll('[role=radio]').forEach((x, i) => {
    x.setAttribute('aria-checked', i === pick ? 'true' : 'false');
    const inp = x.querySelector('input');
    inp.id = 'regen' + gen + '_' + i;
    x.querySelector('label').setAttribute('for', inp.id);
  });
});
</script>
"""


def test_radio_fill_confirms_after_group_rerender():
    """The click re-renders the group: confirmation must not chase the stale id."""
    html = _HTML.replace("</body>", _RERENDER_JS + "</body>")

    async def run(flow):
        radio = next(f for f in await flow._collect_fields_playwright() if f["kind"] == "radio")
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        ok = await linkedin_apply._fill_field(flow.page, radio, "No")
        checked = await flow.page.locator("[role=radio]").evaluate_all(
            "ws => ws.map(w => w.getAttribute('aria-checked'))")
        return ok, loop.time() - t0, checked

    ok, elapsed, checked = asyncio.run(_with_flow(html, run))
    assert ok is True
    assert checked == ["false", "true"]
    assert elapsed < 10  # stale-id lookups used to wait out the 30s default timeout


def test_no_container_when_form_is_gone():
    """After submit the footer disappears: the modal must read as closed."""
    html = "<html><body><main><h2>Application submitted</h2></main></body></html>"

    async def run(flow):
        return await flow._get_modal_text(), await flow._is_modal_open()

    assert asyncio.run(_with_flow(html, run)) == ("", False)
