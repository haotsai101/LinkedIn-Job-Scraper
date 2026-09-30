"""OA6: report_ready / request_human + budget / loop accounting.

Unit tests drive ``RunControl`` directly with synthetic tool results; the live
tests go through guard-mcp over HTTP with the OA5 SubmitGuard installed too.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import mcp_types as types
import pytest
from mcp import Client

from offsite.control import BUDGET, REPORT_READY, REQUEST_HUMAN, STOP_PREFIX, RunControl
from offsite.guard_mcp import BLOCKED_PREFIX
from offsite.guards import SubmitGuard

RESUME = Path(__file__).resolve().parents[1] / "fixtures" / "offsite" / "dummy_resume.pdf"


def _answer(**kw):
    a = {"field_label": "Email", "answer": "test@example.com", "source": "profile",
         "confidence": 1.0, "evidence": ["email"], "sensitive": False}
    a.update(kw)
    return a


def _snap(url: str, body: str) -> types.CallToolResult:
    text = f"### Page\n- Page URL: {url}\n### Snapshot\n```yaml\n{body}\n```"
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)])


def run(coro):
    return asyncio.run(coro)


def _text(r) -> str:
    return "".join(getattr(c, "text", "") or "" for c in r.content)


# ── unit: accounting ──────────────────────────────────────────────────────────

def test_budget_stops_after_40_calls_then_everything_is_stop():
    rc = RunControl()
    for i in range(BUDGET):
        assert run(rc.check("browser_click", {"target": f"e{i}"})) is None
    reason = run(rc.check("browser_click", {"target": "e999"}))
    assert reason.startswith(STOP_PREFIX) and rc.outcome == "budget"
    assert run(rc.check("browser_snapshot", {})).startswith(STOP_PREFIX)
    assert rc.calls == BUDGET + 1


def test_same_action_on_unchanged_page_twice_is_a_loop():
    rc = RunControl()
    args = {"element": "Next", "target": "e26"}
    assert run(rc.check("browser_click", args)) is None
    assert run(rc.check("browser_click", args)).startswith(STOP_PREFIX)
    assert rc.outcome == "loop" and "browser_click" in rc.stop_reason


def test_same_action_after_page_changed_is_not_a_loop():
    rc = RunControl()
    args = {"element": "Next", "target": "e26"}
    assert run(rc.check("browser_click", args)) is None
    run(rc.observe("browser_click", args, _snap("http://x/form", "- paragraph: Step 2 of 3")))
    assert run(rc.check("browser_click", args)) is None
    run(rc.observe("browser_click", args, _snap("http://x/form", "- paragraph: Step 3 of 3")))
    assert run(rc.check("browser_click", args)) is None
    assert rc.outcome is None


def test_action_repeated_around_a_snapshot_is_still_a_loop():
    """click → snapshot → the same click on the same page: what a stuck model does."""
    rc = RunControl()
    page = _snap("http://x/form", "- button \"Submit\" [ref=e9]")
    args = {"element": "Submit", "target": "e9"}
    assert run(rc.check("browser_click", args)) is None
    assert run(rc.check("browser_snapshot", {})) is None
    run(rc.observe("browser_snapshot", {}, page))
    assert run(rc.check("browser_click", args)) is None      # page sig changed: first seen
    assert run(rc.check("browser_snapshot", {})) is None
    run(rc.observe("browser_snapshot", {}, page))
    assert run(rc.check("browser_click", args)).startswith(STOP_PREFIX)
    assert rc.outcome == "loop"


def test_guard_banner_is_not_part_of_the_page():
    rc = RunControl()
    body = "- button \"Submit\" [ref=e9]"
    run(rc.observe("x", {}, _snap("http://x/form", body)))
    sig = rc._page_sig
    banner = "\n- status [ref=e40]: BLOCKED by guard (#7): agent click refused"
    run(rc.observe("x", {}, _snap("http://x/form", body + banner)))
    assert rc._page_sig == sig
    run(rc.observe("x", {}, _snap("http://x/form", body + "\n- alert: Please fix 1 field(s).")))
    assert rc._page_sig != sig


def test_read_only_tools_get_one_more_repeat():
    rc = RunControl()
    assert run(rc.check("browser_snapshot", {})) is None
    assert run(rc.check("browser_snapshot", {})) is None
    assert run(rc.check("browser_snapshot", {})).startswith(STOP_PREFIX)
    assert rc.outcome == "loop"


def test_interleaved_calls_are_not_a_loop():
    rc = RunControl()
    for _ in range(5):
        assert run(rc.check("browser_click", {"target": "e1"})) is None
        assert run(rc.check("browser_type", {"target": "e2", "text": "a"})) is None
    assert rc.outcome is None


def test_reset_starts_a_fresh_count_for_the_next_model():
    rc = RunControl()
    run(rc.check("browser_click", {"target": "e1"}))
    run(rc.check("browser_click", {"target": "e1"}))
    assert rc.outcome == "loop"
    rc.reset(model="claude")
    assert rc.model == "claude" and rc.outcome is None and rc.calls == 0
    assert run(rc.check("browser_click", {"target": "e1"})) is None


# ── unit: control tools ───────────────────────────────────────────────────────

def test_report_ready_valid():
    rc = RunControl()
    r = run(rc._report_ready({"answers": [
        _answer(), _answer(field_label="Sponsorship", answer="Yes", sensitive=True),
        _answer(field_label="Why us?", answer="...", source="generated", confidence=0.5)]}))
    assert not r.is_error and rc.outcome == "ready"
    assert len(rc.answers) == 3 and rc.answers[1].sensitive
    assert "2 flagged" in _text(r) and "do not click submit" in _text(r)


@pytest.mark.parametrize("answers", [
    None,
    [],
    [_answer(confidence=2)],
    [_answer(source="guess")],
    [{k: v for k, v in _answer().items() if k != "sensitive"}],
    [_answer(extra="x")],
])
def test_report_ready_rejects_bad_answers(answers):
    rc = RunControl()
    r = run(rc._report_ready({"answers": answers}))
    assert r.is_error and "rejected" in _text(r)
    assert rc.outcome is None


def test_report_ready_warns_on_filled_cover_letter():
    rc = RunControl()
    r = run(rc._report_ready({"answers": [
        _answer(field_label="Cover Letter", answer="Dear hiring manager", source="generated"),
        _answer(field_label="Cover letter (optional)", answer="")]}))
    assert rc.outcome == "ready"
    assert len(rc.warnings) == 1 and "Cover Letter" in rc.warnings[0]
    assert "must stay blank" in _text(r)


def test_after_ready_browser_calls_stop_and_second_report_is_refused():
    rc = RunControl()
    run(rc._report_ready({"answers": [_answer()]}))
    assert run(rc.check("browser_click", {"target": "e1"})).startswith(STOP_PREFIX)
    assert "already called report_ready" in run(rc.check("report_ready", {}))


def test_report_ready_after_budget_stop_is_accepted():
    rc = RunControl(budget=2)
    for i in range(3):
        run(rc.check("browser_click", {"target": f"e{i}"}))
    assert rc.outcome == "budget"
    assert run(rc.check("report_ready", {})) is None
    run(rc._report_ready({"answers": [_answer()]}))
    assert rc.outcome == "ready"


def test_request_human():
    rc = RunControl()
    bad = run(rc._request_human({"reason": "bored", "detail": "x"}))
    assert bad.is_error and rc.outcome is None
    detail = "Sign in to the Workday tenant for NVIDIA"
    ok = run(rc._request_human({"reason": "login", "detail": detail}))
    assert not ok.is_error and rc.outcome == "human"
    assert (rc.human_reason, rc.human_detail) == ("login", detail)


def test_tool_schemas_are_flat_for_nim_tool_calling():
    for tool in (REPORT_READY, REQUEST_HUMAN):
        dumped = json.dumps(tool.input_schema)
        assert "$ref" not in dumped and "$defs" not in dumped
    item = REPORT_READY.input_schema["properties"]["answers"]["items"]
    assert set(item["required"]) >= {"field_label", "answer", "source", "confidence", "sensitive"}


# ── live: over guard-mcp HTTP, with the OA5 guards installed ──────────────────

@pytest.fixture(scope="module")
def lg(live_guard):
    rc = RunControl()
    rc.install(live_guard.guard)
    live_guard.run(SubmitGuard(live_guard.browser, resume_path=RESUME).install(live_guard.guard))
    live_guard.rc = rc
    return live_guard


def mcp(lg, fn):
    async def go():
        async with Client(lg.guard.url) as c:
            return await fn(c)
    return lg.run(go())


def test_control_tools_are_listed(lg):
    async def go(c):
        return {t.name for t in (await c.list_tools()).tools}

    names = mcp(lg, go)
    assert {"report_ready", "request_human", "browser_click"} <= names


def test_live_blocked_submit_repeated_counts_as_loop(lg):
    lg.open("multipage.html")
    lg.rc.reset(model="nim")

    async def go(c):
        await c.call_tool("browser_snapshot", {})
        args = {"element": "Submit Application", "target": "#submit"}
        first = await c.call_tool("browser_click", args)
        second = await c.call_tool("browser_click", args)
        after = await c.call_tool("browser_snapshot", {})
        return first, second, after

    first, second, after = mcp(lg, go)
    assert _text(first).startswith(BLOCKED_PREFIX) and "reserved for the human" in _text(first)
    assert STOP_PREFIX in _text(second) and lg.rc.outcome == "loop"
    assert STOP_PREFIX in _text(after)
    # snapshot + both refused submit clicks were counted; calls after the stop are not
    assert lg.rc.calls == 3


def test_live_refused_submit_around_snapshots_loops_and_banner_counts_each(lg):
    lg.open("multipage.html")
    lg.rc.reset(model="nim")

    def banner_no(snap):
        import re
        m = re.search(r"BLOCKED by guard \(#(\d+)\)", snap)
        return int(m.group(1)) if m else 0

    async def go(c):
        args = {"element": "Submit Application", "target": "#submit"}
        await c.call_tool("browser_snapshot", {})
        first = await c.call_tool("browser_click", args)
        s1 = _text(await c.call_tool("browser_snapshot", {}))
        second = await c.call_tool("browser_click", args)
        s2 = _text(await c.call_tool("browser_snapshot", {}))
        return first, s1, second, s2

    first, s1, second, s2 = mcp(lg, go)
    assert "reserved for the human" in _text(first)
    assert STOP_PREFIX in _text(second) and lg.rc.outcome == "loop"
    # the banner the human sees moved on at each refusal (no merging of quick repeats)
    n1 = banner_no(s1)
    assert n1 >= 1 and "agent click 'Submit Application' refused" in s1
    assert STOP_PREFIX in s2          # snapshots after the stop are refused too…
    toast = lg.run(lg.browser.page.inner_text("#__oa_guard_toast"))
    # …and each of those two refusals moved the banner on: stop (+1), snapshot (+1)
    assert banner_no(toast) == n1 + 2 and "agent stopped" in toast


def test_live_report_ready_ends_the_run(lg):
    lg.open("multipage.html")
    lg.rc.reset(model="claude")

    async def go(c):
        await c.call_tool("browser_snapshot", {})
        ready = await c.call_tool("report_ready", {"answers": [_answer()]})
        after = await c.call_tool("browser_click", {"element": "Next", "target": "#next"})
        return ready, after

    ready, after = mcp(lg, go)
    assert not ready.is_error and lg.rc.outcome == "ready"
    assert lg.rc.answers[0].field_label == "Email"
    assert STOP_PREFIX in _text(after) and "report_ready" in _text(after)


def test_live_request_human(lg):
    lg.rc.reset(model="nim")

    async def go(c):
        return await c.call_tool("request_human",
                                 {"reason": "captcha", "detail": "Solve the reCAPTCHA"})

    r = mcp(lg, go)
    assert not r.is_error and lg.rc.outcome == "human" and lg.rc.human_reason == "captcha"


# ── skip_application (sponsorship not offered) ────────────────────────────────

class _FakeGuard:
    def __init__(self):
        self.local = {}

    def add_check(self, *a, **k):
        pass

    def add_observer(self, *a):
        pass

    def add_local_tool(self, tool, handler):
        self.local[tool.name] = handler


def test_skip_tool_only_offered_when_applicant_needs_sponsorship():
    g = _FakeGuard()
    RunControl(sponsorship_skip=False).install(g)
    assert "skip_application" not in g.local
    g = _FakeGuard()
    RunControl(sponsorship_skip=True).install(g)
    assert {"report_ready", "request_human", "skip_application"} == set(g.local)


def test_skip_application_ends_the_run():
    rc = RunControl(sponsorship_skip=True)
    ev = "Please note that sponsorship is not allowed for this role."
    r = run(rc._skip_application({"reason": "sponsorship_not_offered", "evidence": ev}))
    assert not r.is_error and rc.outcome == "skip"
    assert (rc.skip_reason, rc.skip_evidence) == ("sponsorship_not_offered", ev)
    assert run(rc.check("browser_click", {"target": "e1"})).startswith(STOP_PREFIX)
    assert "already called skip_application" in run(rc.check("report_ready", {}))


@pytest.mark.parametrize("args", [
    {"reason": "not_a_fit", "evidence": "The salary is too low for this candidate."},
    {"reason": "sponsorship_not_offered", "evidence": "no"},
    {"reason": "sponsorship_not_offered"},
])
def test_skip_application_rejects_bad_calls(args):
    rc = RunControl(sponsorship_skip=True)
    r = run(rc._skip_application(args))
    assert r.is_error and rc.outcome is None


def test_skip_application_refused_when_not_offered():
    rc = RunControl(sponsorship_skip=False)
    r = run(rc._skip_application({"reason": "sponsorship_not_offered",
                                  "evidence": "We are unable to sponsor visas."}))
    assert r.is_error and rc.outcome is None
