"""OA4: guard-mcp pass-through — allowlist, refusal, forwarding, checks, logging.

Talks to guard-mcp over its real streamable-HTTP endpoint with an MCP client,
exactly as the model SDKs will. Needs npx + Playwright Chromium (else skipped).
"""
from __future__ import annotations

import re

import pytest
from mcp import Client

from offsite import guard_mcp
from offsite.guard_mcp import ALLOWED_TOOLS, BLOCKED_PREFIX


def _text(result) -> str:
    return "".join(getattr(c, "text", "") or "" for c in result.content)


def _ref(snapshot: str, role: str, name: str) -> str:
    m = re.search(rf'{role} "{re.escape(name)}" \[ref=([a-z0-9]+)\]', snapshot)
    assert m, f"{role} {name!r} not in snapshot:\n{snapshot[:3000]}"
    return m.group(1)


async def _remote(lg, fn):
    async with Client(lg.guard.url) as c:
        return await fn(c)


def test_remote_client_sees_exactly_the_allowlist(live_guard):
    async def go(c):
        return {t.name for t in (await c.list_tools()).tools}

    assert live_guard.run(_remote(live_guard, go)) == set(ALLOWED_TOOLS)
    assert live_guard.guard.url.startswith("http://127.0.0.1:")


@pytest.mark.parametrize("tool,args", [
    ("browser_evaluate", {"function": "() => document.forms[0].submit()"}),
    ("browser_run_code_unsafe", {"code": "async (page) => page.close()"}),
    ("browser_drop", {"target": "e1", "paths": ["/etc/hosts"]}),
    ("browser_close", {}),
    ("browser_resize", {"width": 10, "height": 10}),
    ("made_up_tool", {}),
])
def test_hidden_tools_are_refused_and_not_forwarded(live_guard, tool, args):
    live_guard.open("multipage.html")

    async def go(c):
        return await c.call_tool(tool, args)

    r = live_guard.run(_remote(live_guard, go))
    assert r.is_error
    assert _text(r).startswith(BLOCKED_PREFIX)
    # the page is still there and untouched
    assert live_guard.run(live_guard.browser.page.title()).startswith("Acme Robotics")


def test_allowed_tools_drive_the_shared_browser(live_guard):
    live_guard.open("multipage.html")

    async def go(c):
        snap = _text(await c.call_tool("browser_snapshot", {}))
        ref = _ref(snap, "textbox", "First name *")
        r = await c.call_tool("browser_type",
                              {"element": "First name", "target": ref, "text": "Ada"})
        assert not r.is_error, _text(r)
        r = await c.call_tool("browser_select_option",
                              {"element": "Country", "target": _ref(snap, "combobox", "Country *"),
                               "values": ["Canada"]})
        assert not r.is_error, _text(r)
        r = await c.call_tool("browser_click",
                              {"element": "Next", "target": _ref(snap, "button", "Next")})
        assert not r.is_error, _text(r)
        return _text(r)

    after_click = live_guard.run(_remote(live_guard, go))
    page = live_guard.browser.page
    assert live_guard.run(page.input_value("#first_name")) == "Ada"
    assert live_guard.run(page.input_value("#country")) == "Canada"
    # validation error from the page's own JS proves the click really happened
    # the post-action snapshot is inlined (not a file link), so the model sees the
    # page's own validation error — which also proves the click really happened
    assert "```yaml" in after_click and "[Snapshot](" not in after_click
    assert "Please fix" in after_click


def test_add_check_refuses_before_forwarding(live_guard):
    live_guard.open("multipage.html")
    seen = []

    async def no_clicks(name, args):
        seen.append(name)
        return "clicks disabled for this test" if name == "browser_click" else None

    live_guard.guard.add_check(no_clicks)
    try:
        async def go(c):
            snap = _text(await c.call_tool("browser_snapshot", {}))
            return await c.call_tool("browser_click",
                                     {"element": "Next", "target": _ref(snap, "button", "Next")})

        r = live_guard.run(_remote(live_guard, go))
    finally:
        live_guard.guard._checks.remove(no_clicks)
    assert r.is_error and _text(r) == BLOCKED_PREFIX + "clicks disabled for this test"
    assert seen == ["browser_snapshot", "browser_click"]
    assert live_guard.run(live_guard.browser.page.inner_text("#error-summary")) == ""


def test_every_call_is_logged(live_guard, monkeypatch):
    logged = []
    monkeypatch.setattr(guard_mcp, "write_llm_log", logged.append)

    async def go(c):
        await c.call_tool("browser_snapshot", {})
        await c.call_tool("browser_evaluate", {"function": "() => 1"})

    live_guard.run(_remote(live_guard, go))
    assert [e["tool"] for e in logged] == ["browser_snapshot", "browser_evaluate"]
    assert logged[0]["source"] == "guard_mcp" and not logged[0]["is_error"]
    assert logged[0]["result_chars"] > 0
    assert "not available" in logged[1]["blocked"]


@pytest.mark.parametrize("raw,expected", [
    ({"target": "[ref=e9]"}, {"target": "e9"}),
    ({"target": "ref=f1e26"}, {"target": "f1e26"}),
    ({"target": " [ ref = e3 ] "}, {"target": "e3"}),
    ({"target": "e9"}, {"target": "e9"}),
    ({"target": "#submit"}, {"target": "#submit"}),
    ({"target": "getByRole('button', { name: 'Next' })"},
     {"target": "getByRole('button', { name: 'Next' })"}),
    ({"fields": [{"target": "[ref=e1]", "name": "a"}, {"name": "b"}]},
     {"fields": [{"target": "e1", "name": "a"}, {"name": "b"}]}),
])
def test_wrapped_refs_are_unwrapped(raw, expected):
    assert guard_mcp.normalize_targets(raw) == expected


def test_wrapped_ref_works_over_http(live_guard):
    live_guard.open("multipage.html")

    async def go(c):
        snap = _text(await c.call_tool("browser_snapshot", {}))
        ref = _ref(snap, "button", "Next")
        return await c.call_tool("browser_click", {"element": "Next", "target": f"[ref={ref}]"})

    r = live_guard.run(_remote(live_guard, go))
    assert not r.is_error and "Please fix" in _text(r)
