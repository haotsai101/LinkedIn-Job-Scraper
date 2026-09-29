"""Record a real ATS application form as an offline fixture (OA3).

    python -m tests.fixtures.offsite.record <application url> <name> [--click "Apply"]

Renders the page in headless Chromium, waits for form fields, then saves the
*rendered DOM* to ``tests/fixtures/offsite/<name>.html`` with everything that
could reach the network or submit anywhere real removed:

* all ``<script>``, ``<iframe>``, ``<noscript>``, ``<link>`` and inline ``on*=``
  handlers are dropped (so custom React dropdowns become static markup);
* same-origin CSS is inlined into one ``<style>``; image / srcset URLs cleared;
* every ``<form>`` posts to ``/__submitted`` (``novalidate`` — the real site
  validated in JS); if the page has no ``<form>``
  (Ashby renders a bare ``<div>``), the body content is wrapped in one;
* submit-like buttons (Submit / Apply / Send application / Finish) become
  ``type="submit"``, every other button ``type="button"``.

The result looks like the real form, has the real labels / ids / names, and
the only thing its submit button can do is increment the fixture server's
``/__submissions`` counter.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from playwright.async_api import async_playwright

HERE = Path(__file__).resolve().parent

_SANITIZE_JS = r"""
() => {
  const SUBMIT = /\b(submit|apply|send application|finish|complete application)\b/i;
  const NEXT = /\b(next|continue|save)\b/i;
  let css = '';
  for (const sheet of document.styleSheets) {
    try {
      for (const r of sheet.cssRules) css += r.cssText + '\n';
    } catch (e) { /* cross-origin sheet: skipped */ }
  }
  document.querySelectorAll('script,iframe,noscript,link,base,meta[http-equiv]')
    .forEach(e => e.remove());
  document.querySelectorAll('style').forEach(e => e.remove());
  document.querySelectorAll('*').forEach(el => {
    for (const a of [...el.attributes]) {
      if (a.name.startsWith('on')) el.removeAttribute(a.name);
    }
    if (el.tagName === 'IMG' || el.tagName === 'SOURCE') {
      el.removeAttribute('src'); el.removeAttribute('srcset');
    }
    if (el.tagName === 'A' && el.href && !el.getAttribute('href').startsWith('#')) {
      el.setAttribute('href', '#');
    }
  });
  let forms = [...document.querySelectorAll('form')];
  if (!forms.length) {
    const f = document.createElement('form');
    while (document.body.firstChild) f.appendChild(document.body.firstChild);
    document.body.appendChild(f);
    forms = [f];
  }
  forms.forEach(f => {
    f.setAttribute('action', '/__submitted');
    f.setAttribute('method', 'post');
    f.setAttribute('enctype', 'multipart/form-data');
    f.setAttribute('novalidate', '');  // the real site validated in JS, which is gone
  });
  document.querySelectorAll('button, input[type=submit], input[type=button]').forEach(b => {
    const text = (b.innerText || b.value || b.getAttribute('aria-label') || '').trim();
    const isSubmit = SUBMIT.test(text) && !NEXT.test(text);
    if (b.tagName === 'BUTTON') b.setAttribute('type', isSubmit ? 'submit' : 'button');
    else b.setAttribute('type', isSubmit ? 'submit' : 'button');
  });
  const st = document.createElement('style');
  st.textContent = css.replace(/url\((['"]?)https?:[^)]*\)/g, 'none');  // fonts etc: stay offline
  document.head.appendChild(st);
  return '<!doctype html>\n' + document.documentElement.outerHTML;
}
"""


async def record(url: str, name: str, click: str | None = None) -> Path:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1280, "height": 1800})
        await page.goto(url, wait_until="networkidle", timeout=60_000)
        if click:
            await page.get_by_role("button", name=click).first.click()
            await page.wait_for_load_state("networkidle")
        await page.wait_for_selector("input, textarea, select", timeout=30_000)
        html = await page.evaluate(_SANITIZE_JS)
        await browser.close()
    out = HERE / f"{name}.html"
    out.write_text(
        f"<!-- recorded by tests/fixtures/offsite/record.py from {url.split('?')[0]} -->\n"
        + html,
        encoding="utf-8",
    )
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="python -m tests.fixtures.offsite.record")
    ap.add_argument("url")
    ap.add_argument("name", help="fixture name, e.g. greenhouse → greenhouse.html")
    ap.add_argument("--click", help="button to click first (e.g. 'Apply')")
    args = ap.parse_args(argv)
    out = asyncio.run(record(args.url, args.name, args.click))
    print(f"wrote {out} ({out.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
