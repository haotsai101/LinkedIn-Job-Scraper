"""OA9: Claude runner — lockdown options, stream handling, stop, errors.

``claude_agent_sdk.query`` is replaced by a fake message stream (the runner
checks message classes by name, like ``llm.py``). The live run is opt-in:
``OFFSITE_LIVE=1 pytest tests/offsite/test_runner_claude.py -k live``.
"""
from __future__ import annotations

import asyncio
import os
import shutil
from dataclasses import dataclass, field

import pytest

from offsite import runners
from offsite.runners import claude

# ── fake SDK messages (matched by class name) ─────────────────────────────────

@dataclass
class TextBlock:
    text: str


@dataclass
class ToolUseBlock:
    name: str
    id: str = "t1"
    input: dict = field(default_factory=dict)


@dataclass
class AssistantMessage:
    content: list
    error: str | None = None


@dataclass
class UserMessage:
    content: list = field(default_factory=list)


@dataclass
class ResultMessage:
    subtype: str = "success"
    is_error: bool = False
    result: str | None = "done"
    api_error_status: int | None = None
    errors: list | None = None


def _tool(name="browser_snapshot"):
    return AssistantMessage([ToolUseBlock(f"mcp__guard__{name}")])


class FakeQuery:
    """Stands in for claude_agent_sdk.query; records options and closing."""

    def __init__(self, messages, *, raise_at=None, hang=False):
        self.messages, self.raise_at, self.hang = messages, raise_at, hang
        self.options = None
        self.closed = False

    def __call__(self, *, prompt, options):
        self.prompt, self.options = prompt, options
        return self._gen()

    async def _gen(self):
        try:
            for i, m in enumerate(self.messages):
                if self.raise_at == i:
                    raise RuntimeError("CLI died")
                yield m
            if self.hang:
                await asyncio.sleep(10)
        finally:
            self.closed = True


def run(q, **kw):
    return asyncio.run(claude.run("SYSTEM", "TASK", "http://127.0.0.1:9/mcp",
                                  model="claude-test", query=q, **kw))


@pytest.fixture(autouse=True)
def _quiet_log(monkeypatch):
    monkeypatch.setattr(claude, "write_llm_log", lambda e: None)


# ── lockdown ──────────────────────────────────────────────────────────────────

def test_options_lock_the_model_to_guard_mcp():
    o = claude.build_options("SYS", "http://127.0.0.1:9/mcp", model="m")
    assert o.tools == []                                   # no built-in tools at all
    assert o.allowed_tools == ["mcp__guard"]
    assert {"Bash", "Read", "Write", "Edit", "WebFetch", "WebSearch", "Task"} <= set(
        o.disallowed_tools)
    assert o.mcp_servers == {"guard": {"type": "http", "url": "http://127.0.0.1:9/mcp"}}
    assert o.strict_mcp_config is True                     # none of the user's MCP servers
    assert o.setting_sources == []                         # no settings / CLAUDE.md
    assert o.permission_mode == "dontAsk"                  # never bypassPermissions
    assert o.system_prompt == "SYS" and o.model == "m"
    assert int(o.env["MCP_TOOL_TIMEOUT"]) >= 300_000       # human-paced typing


def test_model_name_override(monkeypatch):
    monkeypatch.setattr(claude, "_load_dotenv", lambda: None)
    monkeypatch.delenv("OFFSITE_CLAUDE_MODEL", raising=False)
    assert claude.model_name()                             # guided_apply default
    monkeypatch.setenv("OFFSITE_CLAUDE_MODEL", "claude-opus-5-5")
    assert claude.model_name() == "claude-opus-5-5"


# ── run() ─────────────────────────────────────────────────────────────────────

def test_finished_run():
    q = FakeQuery([_tool(), UserMessage(), AssistantMessage([TextBlock("all set")]),
                   ResultMessage(result="Reported ready.")])
    r = run(q)
    assert isinstance(r, runners.RunResult)
    assert (r.status, r.error, r.turns, r.model) == ("finished", None, 2, "claude:claude-test")
    assert r.final_text == "Reported ready."
    assert q.prompt == "TASK" and q.closed


def test_stops_as_soon_as_an_outcome_is_set():
    outcome = {"v": None}
    msgs = [_tool(), UserMessage(), _tool("report_ready"), UserMessage(), _tool("click"),
            UserMessage(), ResultMessage()]

    class Q(FakeQuery):
        async def _gen(self):
            try:
                for i, m in enumerate(self.messages):
                    if i == 3:
                        outcome["v"] = "ready"             # report_ready's result arrives
                    yield m
                    self.seen = i
            finally:
                self.closed = True

    q = Q(msgs)
    r = run(q, stop_when=lambda: outcome["v"] is not None)
    assert r.status == "finished" and q.closed
    assert q.seen < 4                                      # never reached the next click


@pytest.mark.parametrize("result,expected", [
    (ResultMessage(subtype="error_max_turns", is_error=True, result=None), "turn limit"),
    (ResultMessage(subtype="error_during_execution", is_error=True, api_error_status=429,
                   result=None), "429"),
    (ResultMessage(subtype="error_during_execution", is_error=True, result=None,
                   errors=["overloaded"]), "overloaded"),
])
def test_result_errors_are_fallback_triggers(result, expected):
    r = run(FakeQuery([_tool(), UserMessage(), result]))
    assert r.status == "error" and expected in r.error


def test_stream_exception_is_an_error_unless_an_outcome_was_set():
    r = run(FakeQuery([_tool(), UserMessage(), _tool()], raise_at=2))
    assert r.status == "error" and "CLI died" in r.error
    r = run(FakeQuery([_tool(), UserMessage(), _tool()], raise_at=2), stop_when=lambda: True)
    assert r.status == "finished"


def test_run_timeout():
    r = run(FakeQuery([_tool()], hang=True), run_timeout=0.05)
    assert r.status == "error" and "run exceeded" in r.error


def test_non_guard_tool_use_is_logged(monkeypatch):
    logged = []
    monkeypatch.setattr(claude, "write_llm_log", logged.append)
    run(FakeQuery([AssistantMessage([ToolUseBlock("Bash")]), _tool(), ResultMessage()]))
    assert logged[-1]["non_guard_tools"] == ["Bash"]
    assert logged[-1]["source"] == "claude_runner"


# ── live (opt-in) ─────────────────────────────────────────────────────────────

@pytest.mark.skipif(os.environ.get("OFFSITE_LIVE") != "1" or shutil.which("npx") is None
                    or shutil.which("claude") is None,
                    reason="live Claude run: set OFFSITE_LIVE=1 (uses the subscription, ~2 min)")
def test_live_claude_fills_multipage_without_submitting(tmp_path, monkeypatch):
    import json
    import urllib.request

    from offsite import run_agent
    from offsite.prompts import JobInfo, load_profile
    from tests.fixtures.offsite.serve import fixture_server

    monkeypatch.undo()
    logged = []
    monkeypatch.setattr(claude, "write_llm_log", logged.append)
    with fixture_server() as base:
        job = JobInfo(None, "Software Engineer", "Acme Robotics", f"{base}/multipage.html")
        res, control = asyncio.run(run_agent.run_one(
            "claude", job.url, job=job, profile=load_profile(), headless=True, budget=40,
            wait=False, profile_dir=str(tmp_path / "p")))
        count = json.loads(urllib.request.urlopen(f"{base}/__submissions").read())["count"]
    assert control.outcome == "ready", (res, control.stop_reason)
    assert res.status == "finished" and count == 0
    assert logged and logged[-1]["non_guard_tools"] == []   # only guard-mcp tools were used
