"""T14b — the offsite browser agent talks to Claude through one reused
``llm.ClaudeSession`` per apply, and closes it afterwards.

No real ``claude`` subprocess, no browser: ``llm.ClaudeSession`` is replaced
with a recording fake and the flows are exercised through their
``_ClaudeAgentMixin`` seam (``_claude_ask`` / ``_aclose_claude``) plus the
``run()`` / ``apply()`` ``finally`` guards.
"""

from __future__ import annotations

import asyncio

import pytest

import config
import linkedin_apply
import script_engine


class _FakeSession:
    instances: list = []
    slow: bool = False

    def __init__(self, *, model=None, system=None, output_schema=None,
                 log_type="agent", log_calls=True):
        self.model = model
        self.log_calls = log_calls
        self.asks: list[str] = []
        self.closed = 0
        _FakeSession.instances.append(self)

    async def ask(self, prompt: str, **_kw) -> str:
        self.asks.append(prompt)
        if _FakeSession.slow:
            await asyncio.sleep(10)
        return "ok"

    async def ask_json(self, prompt: str, schema, **_kw) -> dict:
        self.asks.append(prompt)
        return {}

    async def aclose(self) -> None:
        self.closed += 1


@pytest.fixture(autouse=True)
def _fake_session(monkeypatch):
    _FakeSession.instances = []
    _FakeSession.slow = False
    monkeypatch.setattr(linkedin_apply.llm, "ClaudeSession", _FakeSession)
    monkeypatch.setattr(script_engine.llm, "ClaudeSession", _FakeSession)
    return _FakeSession


def _offsite() -> linkedin_apply.OffsiteApplyFlow:
    return linkedin_apply.OffsiteApplyFlow(
        page=None, context=None, profile={}, auto_mode=True,
        callbacks={}, generated_password="x",
    )


# ── lazy construction ───────────────────────────────────────────────────────

def test_flow_does_not_open_a_session_at_construction():
    _offsite()
    linkedin_apply.EasyApplyFlow(page=None, profile={}, auto_mode=True, callbacks={})
    script_engine.ScriptApplyEngine(profile={}, context=None, company_name="", job_title="")
    assert _FakeSession.instances == []


# ── one session, reused, then closed ────────────────────────────────────────

def test_offsite_flow_reuses_one_session_then_closes_it():
    flow = _offsite()

    async def _go():
        await flow._claude_ask("a")
        await flow._claude_ask("b")
        await flow._claude_ask("c")
        await flow._aclose_claude()

    asyncio.run(_go())
    assert len(_FakeSession.instances) == 1
    sess = _FakeSession.instances[0]
    assert sess.asks == ["a", "b", "c"]
    assert sess.closed == 1
    # subscription-auth guided_apply model, not a NIM / browser model
    assert sess.model == config.get_llm_config("guided_apply").model == "claude-sonnet-5"


def test_easyapply_flow_reuses_one_session_then_closes_it():
    flow = linkedin_apply.EasyApplyFlow(page=None, profile={}, auto_mode=True, callbacks={})

    async def _go():
        await flow._claude_ask("one")
        await flow._claude_ask("two")
        await flow._aclose_claude()

    asyncio.run(_go())
    assert len(_FakeSession.instances) == 1
    assert _FakeSession.instances[0].asks == ["one", "two"]
    assert _FakeSession.instances[0].closed == 1


def test_script_engine_reuses_one_session_then_closes_it():
    eng = script_engine.ScriptApplyEngine(
        profile={}, context=None, company_name="ACME", job_title="Dev",
    )

    async def _go():
        await eng._claude_ask("p1")
        await eng._claude_ask("p2")
        await eng._aclose()

    asyncio.run(_go())
    assert len(_FakeSession.instances) == 1
    assert _FakeSession.instances[0].asks == ["p1", "p2"]
    assert _FakeSession.instances[0].closed == 1


# ── run()/apply() close the session even when the body raises ───────────────

def test_offsite_run_closes_session_on_exception(monkeypatch):
    flow = _offsite()

    async def _boom(_job_url):
        await flow._claude_ask("x")
        raise RuntimeError("mid-apply blow-up")

    monkeypatch.setattr(flow, "_run", _boom)
    with pytest.raises(RuntimeError):
        asyncio.run(flow.run("https://example.com/job"))
    assert _FakeSession.instances[0].closed == 1


def test_offsite_assist_from_page_closes_session(monkeypatch):
    flow = _offsite()

    async def _guided(_page):
        await flow._claude_ask("y")
        return "failed"

    # assist_from_page iterates context.pages then prints self.page.url
    flow.context = type("C", (), {"pages": []})()
    flow.page = type("P", (), {"url": "https://example.com/form"})()
    monkeypatch.setattr(flow, "_llm_guided_apply", _guided)
    assert asyncio.run(flow.assist_from_page()) == "failed"
    assert _FakeSession.instances[0].closed == 1


def test_easyapply_run_closes_session_on_exception(monkeypatch):
    flow = linkedin_apply.EasyApplyFlow(page=None, profile={}, auto_mode=True, callbacks={})

    async def _boom(_job_url):
        await flow._claude_ask("x")
        raise RuntimeError("boom")

    monkeypatch.setattr(flow, "_run", _boom)
    with pytest.raises(RuntimeError):
        asyncio.run(flow.run("u"))
    assert _FakeSession.instances[0].closed == 1


def test_script_engine_apply_closes_session_on_exception(monkeypatch):
    eng = script_engine.ScriptApplyEngine(
        profile={}, context=None, company_name="", job_title="",
    )

    async def _boom(_page):
        await eng._claude_ask("x")
        raise RuntimeError("boom")

    monkeypatch.setattr(eng, "_apply", _boom)
    with pytest.raises(RuntimeError):
        asyncio.run(eng.apply(None))
    assert _FakeSession.instances[0].closed == 1


def test_claude_ask_timeout_tears_down_session(_fake_session):
    """A slow call raises TimeoutError and drops the (possibly desynced)
    session so the next call starts a fresh subprocess."""
    _fake_session.slow = True
    flow = _offsite()

    async def _go():
        with pytest.raises(asyncio.TimeoutError):
            await flow._claude_ask("slow", timeout=0.01)
        _fake_session.slow = False
        await flow._claude_ask("fast")

    asyncio.run(_go())
    assert len(_fake_session.instances) == 2
    assert _fake_session.instances[0].closed == 1
    assert _fake_session.instances[1].asks == ["fast"]


def test_aclose_is_idempotent():
    flow = _offsite()

    async def _go():
        await flow._claude_ask("a")
        await flow._aclose_claude()
        await flow._aclose_claude()

    asyncio.run(_go())
    assert _FakeSession.instances[0].closed == 1


# ── no `claude` subprocess anywhere in the migrated modules ─────────────────

def test_no_claude_subprocess_in_browser_agent_modules():
    for mod in (linkedin_apply, script_engine):
        src = open(mod.__file__).read()
        assert 'subprocess.run(["claude"' not in src
        assert "import subprocess" not in src
