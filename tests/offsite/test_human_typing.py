"""Human-paced typing: Normal(0.2 s, 0.1 s) between characters, through guard-mcp."""
from __future__ import annotations

import random
import re
import statistics

import pytest
from mcp import Client

from offsite.human_typing import HumanTyping

# ── unit: the delay distribution ──────────────────────────────────────────────

def test_delays_are_normal_mean_0_2_std_0_1_and_never_negative():
    t = HumanTyping(rng=random.Random(7))
    xs = [t.delay() for _ in range(20_000)]
    assert 0.195 <= statistics.mean(xs) <= 0.215       # clamping the low tail nudges it up
    assert 0.085 <= statistics.stdev(xs) <= 0.102
    assert min(xs) >= 0.03 and max(xs) <= 1.0


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("OFFSITE_TYPING_MEAN", "0.3")
    monkeypatch.setenv("OFFSITE_TYPING_STD", "0.05")
    t = HumanTyping.from_env()
    assert (t.mean, t.std) == (0.3, 0.05)
    monkeypatch.setenv("OFFSITE_TYPING_MEAN", "fast")
    assert HumanTyping.from_env().mean == 0.2


# ── live: through guard-mcp over HTTP ─────────────────────────────────────────

_RECORD_KEYS = """() => {
  window.__keys = [];
  document.addEventListener('keydown', e => {
    // printable characters only — not the select-all shortcut used to clear the field
    if (e.key.length === 1 && !e.ctrlKey && !e.metaKey) window.__keys.push(performance.now());
  }, true);
}"""


def _t(r) -> str:
    return "".join(getattr(c, "text", "") or "" for c in r.content)


def _ref(snap: str, role: str, name: str) -> str:
    m = re.search(rf'{role} "{re.escape(name)}"[^\n]*?\[ref=([a-z0-9]+)\]', snap)
    assert m, f"{role} {name!r} not in snapshot"
    return m.group(1)


def mcp(lg, fn):
    async def go():
        async with Client(lg.guard.url) as c:
            return await fn(c)
    return lg.run(go())


@pytest.fixture
def real_pacing(live_guard):
    fast = live_guard.guard.typing
    live_guard.guard.typing = HumanTyping(rng=random.Random(3))
    yield live_guard
    live_guard.guard.typing = fast


def test_browser_type_is_paced_like_a_person(real_pacing):
    lg = real_pacing
    lg.open("multipage.html")
    page = lg.browser.page
    lg.run(page.fill("#email", "old@value.com"))
    lg.run(page.evaluate(_RECORD_KEYS))
    text = "ada.lovelace@example.com"                     # 24 characters

    async def go(c):
        snap = _t(await c.call_tool("browser_snapshot", {}))
        return await c.call_tool("browser_type", {"element": "Email",
                                                  "target": _ref(snap, "textbox", "Email *"),
                                                  "text": text})

    r = mcp(lg, go)
    assert not r.is_error and _t(r).startswith(f"### Result\nTyped {len(text)} characters")
    assert "```yaml" in _t(r)                              # answers with a snapshot
    assert lg.run(page.input_value("#email")) == text     # replaced, not appended
    stamps = lg.run(page.evaluate("() => window.__keys"))
    assert len(stamps) == len(text)                        # one real keystroke per character
    gaps = [(b - a) / 1000 for a, b in zip(stamps, stamps[1:], strict=False)]
    assert 0.15 <= statistics.mean(gaps) <= 0.26, gaps
    assert 0.03 <= statistics.stdev(gaps) <= 0.17, gaps
    assert min(gaps) >= 0.02


def test_fill_form_types_textboxes_and_forwards_the_rest(live_guard):
    lg = live_guard                                        # fast pacing
    lg.open("multipage.html")
    page = lg.browser.page
    lg.run(page.evaluate(_RECORD_KEYS))

    async def go(c):
        snap = _t(await c.call_tool("browser_snapshot", {}))
        return await c.call_tool("browser_fill_form", {"fields": [
            {"name": "First name", "type": "textbox", "value": "Ada",
             "target": _ref(snap, "textbox", "First name *")},
            {"name": "Country", "type": "combobox", "value": "Canada",
             "target": _ref(snap, "combobox", "Country *")},
            {"name": "Last name", "type": "textbox", "value": "Lovelace",
             "target": _ref(snap, "textbox", "Last name *")},
        ]})

    r = mcp(lg, go)
    assert not r.is_error and "Filled 3 fields (11 characters typed)" in _t(r)
    assert lg.run(page.input_value("#first_name")) == "Ada"
    assert lg.run(page.input_value("#last_name")) == "Lovelace"
    assert lg.run(page.input_value("#country")) == "Canada"
    assert len(lg.run(page.evaluate("() => window.__keys"))) == 11


def test_newlines_only_go_into_textareas(live_guard):
    lg = live_guard
    lg.open("multipage.html")
    page = lg.browser.page
    for sel, val in (("#first_name", "T"), ("#last_name", "P"), ("#email", "t@example.com")):
        lg.run(page.fill(sel, val))
    lg.run(page.select_option("#country", "United States"))
    lg.run(page.set_input_files("#resume", "tests/fixtures/offsite/dummy_resume.pdf"))
    lg.run(page.click("#next"))

    async def go(c):
        snap = _t(await c.call_tool("browser_snapshot", {}))
        salary = _ref(snap, "textbox", "Desired annual salary (USD)")
        why = _ref(snap, "textbox", "Why do you want to work at Acme Robotics?")
        r1 = await c.call_tool("browser_type", {"element": "salary", "target": salary,
                                                "text": "100000\n- 120000"})
        r2 = await c.call_tool("browser_type", {"element": "why", "target": why,
                                                "text": "Line one.\nLine two."})
        return r1, r2

    r1, r2 = mcp(lg, go)
    assert not r1.is_error and not r2.is_error
    assert lg.run(page.input_value("#salary")) == "100000 - 120000"
    assert lg.run(page.input_value("#why")) == "Line one.\nLine two."
    # the newline in the <input> was not typed as Enter: still on step 2, nothing submitted
    assert "Step 2 of 3" in lg.run(page.inner_text(".step-indicator"))


def test_parallel_typing_calls_are_serialized(real_pacing):
    """A model may send two browser_type calls at once; both must land intact."""
    import asyncio

    lg = real_pacing
    lg.open("multipage.html")
    page = lg.browser.page

    async def go(c):
        snap = _t(await c.call_tool("browser_snapshot", {}))
        first = _ref(snap, "textbox", "First name *")
        last = _ref(snap, "textbox", "Last name *")
        return await asyncio.gather(
            c.call_tool("browser_type", {"element": "first", "target": first, "text": "Augusta"}),
            c.call_tool("browser_type", {"element": "last", "target": last, "text": "Lovelace"}))

    r1, r2 = mcp(lg, go)
    assert not r1.is_error and not r2.is_error
    assert lg.run(page.input_value("#first_name")) == "Augusta"
    assert lg.run(page.input_value("#last_name")) == "Lovelace"


def test_cancelled_call_is_logged_and_reported(real_pacing, monkeypatch):
    import asyncio

    from offsite import guard_mcp

    lg = real_pacing
    lg.open("multipage.html")
    logged, cancelled = [], []
    monkeypatch.setattr(guard_mcp, "write_llm_log", logged.append)
    lg.guard.add_cancel_listener(lambda name, args: cancelled.append(name))
    snap = _t(lg.run(lg.guard.call("browser_snapshot", {})))
    target = _ref(snap, "textbox", "Email *")

    async def go():
        task = asyncio.create_task(lg.guard.call(
            "browser_type", {"element": "Email", "target": target, "text": "x" * 40}))
        await asyncio.sleep(1.0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return True
        return False

    try:
        assert lg.run(go()) is True
    finally:
        lg.guard._cancel_listeners.clear()
    assert cancelled == ["browser_type"]
    assert logged[-1]["tool"] == "browser_type" and logged[-1].get("cancelled") is True
