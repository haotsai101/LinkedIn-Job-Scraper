"""OA10: NIM → Claude fallback, in place — every trigger, with fake runners.

A fake runner plays a model: it records the task it was given and then does
what a script says — set a control outcome (as report_ready / request_human /
skip_application would), burn tool calls into a loop / budget stop, fail, or
just end its turn.
"""
from __future__ import annotations

import asyncio

import pytest

from offsite.control import RunControl
from offsite.controller import FillController
from offsite.prompts import JobInfo
from offsite.runners import RunResult
from offsite.schemas import GeneratedAnswer

JOB = JobInfo(7, "SWE", "Acme", "https://boards.greenhouse.io/acme/jobs/7", "desc")
PROFILE = {"full_name": "Ada", "need_sponsorship": "yes", "resume_path": "/r.pdf"}
ANSWER = GeneratedAnswer(field_label="Email", answer="a@x.com", source="profile",
                         confidence=1.0, evidence=["email"], sensitive=False)


class FakeModel:
    """One model. ``script`` is a list of actions, one per run of this model."""

    def __init__(self, name, control, script):
        self.name, self.control, self.script = name, control, list(script)
        self.tasks: list[str] = []

    async def __call__(self, system, task, guard_url, *, stop_when):
        self.tasks.append(task)
        action = self.script.pop(0)
        c = self.control
        for _ in range(3):                      # every run does a little work
            await c.check("browser_snapshot", {"n": len(self.tasks), "i": _})
        if action == "ready":
            c.answers, c.outcome = [ANSWER], "ready"
        elif action == "human":
            c.human_reason, c.human_detail, c.outcome = "login", "sign in", "human"
        elif action == "skip":
            c.skip_reason, c.skip_evidence, c.outcome = (
                "sponsorship_not_offered", "We are unable to sponsor.", "skip")
        elif action == "loop":
            await c.check("browser_click", {"target": "e1"})
            await c.check("browser_click", {"target": "e1"})
        elif action == "budget":
            for i in range(c.budget + 1):
                await c.check("browser_click", {"target": f"e{i}"})
        elif action == "error":
            return RunResult("error", self.name, "NIM request timed out")
        elif action == "raise":
            raise RuntimeError("no NIM API key")
        elif action == "quit":
            pass                                 # ends its turn, no control tool
        return RunResult("finished", self.name)


def make(nim_script, claude_script, **kw):
    control = RunControl(sponsorship_skip=True)
    nim, claude = FakeModel("nim", control, nim_script), FakeModel("claude", control,
                                                                   claude_script)
    ctl = FillController(profile=PROFILE, job=JOB, guard_url="http://g/mcp", control=control,
                         runners={"nim": nim, "claude": claude}, **kw)
    return ctl, nim, claude, control


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    import offsite.controller as c
    monkeypatch.setattr(c, "write_llm_log", lambda e: None)


def test_nim_ready_needs_no_claude():
    ctl, nim, claude, _ = make(["ready"], [])
    r = run(ctl.fill())
    assert (r.outcome, r.model_used, r.fallback_reason) == ("ready", "nim", None)
    assert r.answers == [ANSWER] and claude.tasks == []
    assert "is open in the browser" in nim.tasks[0]            # start_message


@pytest.mark.parametrize("nim_action,reason", [
    ("error", "NIM request timed out"),
    ("raise", "RuntimeError: no NIM API key"),
    ("loop", "loop — browser_click repeated"),
    ("budget", "budget — 40 tool calls used"),
    ("quit", "ended its turn without calling report_ready"),
])
def test_every_trigger_falls_back_to_claude_in_place(nim_action, reason):
    ctl, nim, claude, _ = make([nim_action], ["ready"])
    r = run(ctl.fill())
    assert r.outcome == "ready" and r.model_used == "nim→claude"
    assert r.fallback_reason.startswith(f"nim: {reason}")
    handoff = claude.tasks[0]
    assert "taking over" in handoff and "(nim)" in handoff and reason in handoff
    assert "do NOT retype" in handoff                           # continue in place


def test_skip_is_terminal_no_fallback():
    ctl, nim, claude, _ = make(["skip"], ["ready"])
    r = run(ctl.fill())
    assert r.outcome == "skip" and r.model_used == "nim" and claude.tasks == []
    assert r.skip_evidence == "We are unable to sponsor."


def test_both_models_fail_needs_human():
    ctl, _, _, _ = make(["error"], ["loop"])
    r = run(ctl.fill())
    assert r.outcome == "needs_human" and r.model_used == "nim→claude"
    assert "nim: NIM request timed out" in r.fallback_reason
    assert "claude: loop" in r.fallback_reason


def test_human_pause_then_resume_same_model():
    ctl, nim, claude, _ = make(["human", "ready"], [])
    r = run(ctl.fill())
    assert (r.outcome, r.human_reason, r.human_detail) == ("human", "login", "sign in")
    r = run(ctl.resume("login", "sign in"))
    assert r.outcome == "ready" and r.model_used == "nim" and claude.tasks == []
    assert "has signed in" in nim.tasks[1]                     # resume_note


def test_claude_asks_for_human_then_resume_stays_on_claude():
    ctl, nim, claude, _ = make(["error"], ["human", "ready"])
    assert run(ctl.fill()).outcome == "human"
    r = run(ctl.resume("captcha"))
    assert r.outcome == "ready" and len(nim.tasks) == 1 and len(claude.tasks) == 2
    assert "solved the CAPTCHA" in claude.tasks[1]


def test_resume_that_fails_hands_off_with_human_actions():
    ctl, nim, claude, _ = make(["human", "error"], ["ready"])
    run(ctl.fill())
    r = run(ctl.resume("login"))
    assert r.outcome == "ready" and r.model_used == "nim→claude"
    assert "The human already did: signed in." in claude.tasks[0]


def test_fix_reruns_the_chain_from_the_first_model():
    ctl, nim, claude, _ = make(["ready", "error"], ["ready"])
    run(ctl.fill())
    r = run(ctl.fix("Salary → 110000"))
    assert r.outcome == "ready"
    assert "Salary → 110000" in nim.tasks[1] and "Salary → 110000" in claude.tasks[0]


def test_tool_calls_are_summed_across_runs():
    ctl, _, _, _ = make(["quit"], ["ready"])
    r = run(ctl.fill())
    assert r.tool_calls == 6 and len(r.runs) == 2


def test_force_fallback_after_cuts_only_the_first_run():
    ctl, nim, claude, control = make(["budget"], ["ready"], force_fallback_after=5)
    r = run(ctl.fill())
    assert r.outcome == "ready" and "5 tool calls used" in r.fallback_reason
    assert control.budget == 40                                 # restored for Claude


def test_custom_order_claude_only():
    ctl, nim, claude, _ = make([], ["ready"], order=("claude",))
    r = run(ctl.fill())
    assert r.model_used == "claude" and nim.tasks == []


# ── live (opt-in): NIM cut short, Claude finishes in place ───────────────────

@pytest.mark.skipif(__import__("os").environ.get("OFFSITE_LIVE") != "1",
                    reason="live NIM → Claude run: set OFFSITE_LIVE=1 (~5 min)")
def test_live_forced_fallback_continues_in_place(tmp_path):
    import json
    import urllib.request

    from offsite import run_agent
    from offsite.prompts import load_profile
    from tests.fixtures.offsite.serve import fixture_server

    with fixture_server() as base:
        job = JobInfo(None, "Software Engineer", "Acme Robotics", f"{base}/multipage.html")
        asyncio.run(run_agent.run_one(
            "auto", job.url, job=job, profile=load_profile(), headless=True, budget=40,
            wait=False, profile_dir=str(tmp_path / "p"), force_fallback_after=8))
        fill = run_agent.run_one.last_fill
        count = json.loads(urllib.request.urlopen(f"{base}/__submissions").read())["count"]
    assert fill.outcome == "ready", fill
    assert fill.model_used == "nim→claude" and "8 tool calls used" in fill.fallback_reason
    assert count == 0
