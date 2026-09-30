"""Human-paced typing for guard-mcp.

Playwright MCP's ``browser_type`` / ``browser_fill_form`` set a text field's
value in one go. guard-mcp instead types like a person:

1. click the field (through Playwright MCP, so refs resolve exactly as usual) —
   that focuses it, as a person would;
2. clear it (select-all + Backspace — ``browser_type`` replaces the value);
3. type the text one character at a time from our own handle on the same
   browser, waiting ``Normal(mean, std)`` seconds between characters — default
   **mean 0.2 s, standard deviation 0.1 s**, clamped to ``[min_delay, max_delay]``
   (no negative / zero gaps);
4. answer with a fresh snapshot, like the tool it replaces.

``browser_fill_form``: its ``textbox`` fields are typed this way, one after the
other; the other field types (checkbox, radio, combobox, slider) are forwarded
unchanged, in their original order. Newlines are only typed into ``<textarea>``
/ contenteditable fields (in an ``<input>``, Enter could submit the form).

Settings: ``OFFSITE_TYPING_MEAN`` / ``OFFSITE_TYPING_STD`` (seconds).
"""
from __future__ import annotations

import asyncio
import os
import random
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import mcp_types as types
from playwright.async_api import Page

from offsite.browser import OffsiteBrowser

Upstream = Callable[[str, dict[str, Any]], Awaitable[types.CallToolResult]]

_PAGE_URL = re.compile(r"^- Page URL: (\S+)", re.M)


def _text(r: types.CallToolResult) -> str:
    return "".join(getattr(c, "text", "") or "" for c in r.content)


@dataclass
class HumanTyping:
    mean: float = 0.2
    std: float = 0.1
    min_delay: float = 0.03
    max_delay: float = 1.0
    rng: random.Random = field(default_factory=random.Random)
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep

    @classmethod
    def from_env(cls) -> HumanTyping:
        def f(name: str, default: float) -> float:
            try:
                return float(os.environ.get(name, "") or default)
            except ValueError:
                return default
        return cls(mean=f("OFFSITE_TYPING_MEAN", 0.2), std=f("OFFSITE_TYPING_STD", 0.1))

    def delay(self) -> float:
        """Seconds to wait before the next character."""
        return min(self.max_delay, max(self.min_delay, self.rng.gauss(self.mean, self.std)))

    # ── tool handlers ─────────────────────────────────────────────────────────
    async def handle(self, name: str, args: dict[str, Any], up: Upstream,
                     browser: OffsiteBrowser) -> types.CallToolResult:
        if name == "browser_type":
            done = await self._type_field(args.get("element", ""), args.get("target", ""),
                                          str(args.get("text", "")), up, browser)
            if isinstance(done, types.CallToolResult):
                return done
            return await self._with_snapshot(up, f"Typed {done} characters into "
                                                 f"{args.get('element') or args.get('target')}.")
        if name == "browser_fill_form":
            typed = 0
            for f in args.get("fields") or []:
                if f.get("type") == "textbox":
                    done = await self._type_field(f.get("element") or f.get("name", ""),
                                                  f.get("target", ""), str(f.get("value", "")),
                                                  up, browser)
                    if isinstance(done, types.CallToolResult):
                        return done
                    typed += done
                else:
                    r = await up("browser_fill_form", {"fields": [f]})
                    if r.is_error:
                        return r
            n = len(args.get("fields") or [])
            return await self._with_snapshot(up, f"Filled {n} fields ({typed} characters typed).")
        return await up(name, args)

    async def _type_field(self, element: str, target: str, text: str, up: Upstream,
                          browser: OffsiteBrowser) -> int | types.CallToolResult:
        r = await up("browser_click", {"element": element or "text field", "target": target})
        if r.is_error:
            return r
        page = _page_for(browser, _text(r))
        multiline = await page.evaluate(
            "() => { const a = document.activeElement;"
            " return !!a && (a.tagName === 'TEXTAREA' || a.isContentEditable); }")
        if not multiline:
            text = re.sub(r"\s*[\r\n]+\s*", " ", text)
        await page.keyboard.press("ControlOrMeta+A")
        await page.keyboard.press("Backspace")
        last = 0.0
        for i, ch in enumerate(text):
            if i:
                # the gap between keystrokes is the drawn delay: subtract the time the
                # previous keystroke itself took
                wait = self.delay() - (time.monotonic() - last)
                if wait > 0:
                    await self.sleep(wait)
            last = time.monotonic()
            await page.keyboard.type(ch)
        return len(text)

    async def _with_snapshot(self, up: Upstream, note: str) -> types.CallToolResult:
        snap = await up("browser_snapshot", {})
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=f"### Result\n{note}\n"), *snap.content],
            is_error=snap.is_error)


def _page_for(browser: OffsiteBrowser, result_text: str) -> Page:
    """The tab Playwright MCP just acted on (by the URL in its answer)."""
    m = _PAGE_URL.search(result_text)
    pages = browser.context.pages if browser.context else []
    if m:
        for p in reversed(pages):
            if p.url == m.group(1):
                return p
    return browser.page
