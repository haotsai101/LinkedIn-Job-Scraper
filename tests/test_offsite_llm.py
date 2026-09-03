"""T14b — the offsite / EasyApply browser agent talks to Claude through the
one-shot ``llm.query`` helper (fresh isolated session per call), not a
persistent conversation.

No real ``claude`` subprocess, no browser: ``llm.query`` is replaced with a
recording stub and the flow methods are exercised directly.
"""

from __future__ import annotations

import asyncio

import pytest

import config
import linkedin_apply
import script_engine


class _QueryStub:
    """Records every ``llm.query`` call; returns a canned reply or raises."""

    def __init__(self, reply="ok", *, exc=None, delay=0.0):
        self.reply = reply
        self.exc = exc
        self.delay = delay
        self.calls: list[dict] = []

    async def __call__(self, prompt, *, model, system=None, timeout=None,
                       log_type="agent", log_calls=False):
        self.calls.append({"prompt": prompt, "model": model, "timeout": timeout})
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.exc is not None:
            raise self.exc
        return self.reply


GUIDED_MODEL = config.get_llm_config("guided_apply").model


@pytest.fixture(autouse=True)
def _no_log(monkeypatch):
    monkeypatch.setattr(linkedin_apply, "_write_llm_log", lambda *_a, **_k: None)
    monkeypatch.setattr(script_engine, "_write_llm_log", lambda *_a, **_k: None)


def _install(monkeypatch, stub):
    monkeypatch.setattr(linkedin_apply.llm, "query", stub)
    monkeypatch.setattr(script_engine.llm, "query", stub)
    return stub


def _offsite(**kw):
    return linkedin_apply.OffsiteApplyFlow(
        page=None, context=None, profile={}, auto_mode=True,
        callbacks={}, generated_password="x", **kw,
    )


# ── model wiring ───────────────────────────────────────────────────────────

def test_flows_default_to_the_guided_apply_model():
    assert _offsite().model == GUIDED_MODEL == "claude-sonnet-5"
    assert linkedin_apply.EasyApplyFlow(
        page=None, profile={}, auto_mode=True, callbacks={}).model == GUIDED_MODEL
    assert script_engine.ScriptApplyEngine(
        profile={}, context=None, company_name="", job_title="").model == GUIDED_MODEL


def test_constructing_a_flow_makes_no_llm_call(monkeypatch):
    stub = _install(monkeypatch, _QueryStub())
    _offsite()
    linkedin_apply.EasyApplyFlow(page=None, profile={}, auto_mode=True, callbacks={})
    script_engine.ScriptApplyEngine(profile={}, context=None, company_name="", job_title="")
    assert stub.calls == []


# ── _summarize_job ─────────────────────────────────────────────────────────

def test_summarize_job_uses_one_shot_query(monkeypatch):
    stub = _install(monkeypatch, _QueryStub("A crisp three-sentence summary."))
    flow = _offsite(company_name="ACME", job_title="Data Eng",
                    job_description="Build pipelines. " * 20)
    out = asyncio.run(flow._summarize_job())
    assert out == "A crisp three-sentence summary."
    assert len(stub.calls) == 1
    assert stub.calls[0]["model"] == GUIDED_MODEL
    assert stub.calls[0]["timeout"] == 30


def test_summarize_job_falls_back_on_error(monkeypatch):
    _install(monkeypatch, _QueryStub(exc=linkedin_apply.llm.ClaudeAgentSDKError("boom")))
    flow = _offsite(company_name="ACME", job_title="Data Eng",
                    job_description="stuff")
    assert asyncio.run(flow._summarize_job()) == "Role: Data Eng at ACME."


# ── _ask_llm (field fill) ──────────────────────────────────────────────────

def test_ask_llm_field_fill_goes_through_query(monkeypatch):
    stub = _install(monkeypatch, _QueryStub("7"))
    out = asyncio.run(linkedin_apply._ask_llm(
        GUIDED_MODEL, {"years_experience": 7},
        {"label": "Years of experience", "kind": "number"},
    ))
    assert out == "7"
    assert stub.calls[0]["model"] == GUIDED_MODEL


# ── _ask_llm_action decide-action loop ─────────────────────────────────────

_SNAP = {"visible_text": "Apply now", "fields": [], "url": "https://ex.com/apply"}


def test_ask_llm_action_success_returns_parsed_dict(monkeypatch):
    _install(monkeypatch, _QueryStub('{"action": "done", "reason": "confirmation visible"}'))
    flow = _offsite(company_name="ACME", job_title="Dev")
    out = asyncio.run(flow._ask_llm_action(_SNAP, 0))
    assert out == {"action": "done", "reason": "confirmation visible"}


def test_ask_llm_action_three_timeouts_then_failed(monkeypatch):
    """The reviewer's requested guard: 3 one-shot attempts all time out ->
    a clean {"action": "failed"} instead of an infinite hang."""
    stub = _install(monkeypatch, _QueryStub(exc=asyncio.TimeoutError()))
    flow = _offsite(company_name="ACME", job_title="Dev")
    out = asyncio.run(flow._ask_llm_action(_SNAP, 0))
    assert out["action"] == "failed"
    assert "timed out after 3" in out["reason"]
    assert len(stub.calls) == 3
    assert {c["timeout"] for c in stub.calls} == {120}


def test_ask_llm_action_non_retryable_sdk_error_fails_fast(monkeypatch):
    stub = _install(monkeypatch, _QueryStub(
        exc=linkedin_apply.llm.ClaudeAgentSDKError("malformed request")))
    flow = _offsite(company_name="ACME", job_title="Dev")
    out = asyncio.run(flow._ask_llm_action(_SNAP, 0))
    assert out["action"] == "failed"
    assert "ClaudeAgentSDKError" in out["reason"]
    assert len(stub.calls) == 1  # not retried


def test_ask_llm_action_rate_limit_is_retried(monkeypatch):
    """A ClaudeAgentSDKError whose message names a usage/rate limit is retried
    (was stdout string-matching in the old subprocess helper)."""
    _real_sleep = asyncio.sleep
    monkeypatch.setattr(linkedin_apply.asyncio, "sleep",
                        lambda *_a, **_k: _real_sleep(0))
    calls = {"n": 0}

    async def _flaky(prompt, *, model, system=None, timeout=None,
                     log_type="agent", log_calls=False):
        calls["n"] += 1
        if calls["n"] == 1:
            raise linkedin_apply.llm.ClaudeAgentSDKError("usage limit reached")
        return '{"action": "scroll", "reason": "reveal more"}'

    monkeypatch.setattr(linkedin_apply.llm, "query", _flaky)
    flow = _offsite(company_name="ACME", job_title="Dev")
    out = asyncio.run(flow._ask_llm_action(_SNAP, 0))
    assert out == {"action": "scroll", "reason": "reveal more"}
    assert calls["n"] == 2


# ── ScriptApplyEngine script generation ────────────────────────────────────

def test_script_engine_call_llm_uses_query_and_sanitizes(monkeypatch):
    stub = _install(monkeypatch, _QueryStub(
        "```python\nawait page.click('#submit')\n```"))
    eng = script_engine.ScriptApplyEngine(
        profile={}, context=None, company_name="ACME", job_title="Dev")
    script = asyncio.run(eng._call_llm("prompt", "https://ex.com", 1))
    assert "page.locator('#submit').click(timeout=5000)" in script
    assert stub.calls[0]["model"] == GUIDED_MODEL
    assert stub.calls[0]["timeout"] == 180


def test_script_engine_call_llm_timeout_returns_none(monkeypatch):
    _install(monkeypatch, _QueryStub(exc=asyncio.TimeoutError()))
    eng = script_engine.ScriptApplyEngine(
        profile={}, context=None, company_name="", job_title="")
    assert asyncio.run(eng._call_llm("p", "u", 0)) is None


# ── no persistent-session / subprocess plumbing left ──────────────────────

def test_no_claude_subprocess_or_session_in_browser_agent_modules():
    for mod in (linkedin_apply, script_engine):
        src = open(mod.__file__).read()
        assert 'subprocess.run(["claude"' not in src
        assert "import subprocess" not in src
        assert "ClaudeSession" not in src        # one-shot llm.query only
        assert "_ClaudeAgentMixin" not in src


def test_no_asyncopenai_in_browser_agent_modules():
    for mod in (linkedin_apply, script_engine):
        src = open(mod.__file__).read()
        assert "AsyncOpenAI" not in src
        assert "from openai" not in src
