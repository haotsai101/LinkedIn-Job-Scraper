"""Unit tests for ``apply_jobs.JobAgent.classify`` routing (T14 part 1).

Routing contract:
  * citizenship / clearance keyword in the description → immediate skip, no LLM
    call on either backend
  * application_type == "OffsiteApply"                  → NIM (nim_client)
  * SimpleOnsiteApply / ComplexOnsiteApply / other      → Claude Agent SDK
    (llm.ClaudeSession)

Both backends are mocked — no network, no ``claude`` CLI, no OpenAI calls.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import apply_jobs

_PROFILE = {"full_name": "Test User", "skills": ["Python", "PyTorch"]}


@pytest.fixture
def agent():
    return apply_jobs.JobAgent(_PROFILE)


class _FakeSession:
    """Stand-in for llm.ClaudeSession. Records construction kwargs and returns a
    canned classifier payload."""

    last = None

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.asked: list = []
        self.closed = False
        _FakeSession.last = self

    def _payload(self):
        return {"relevant": True, "reason": "Backend SWE role", "citizenship_required": False}

    async def ask_json(self, prompt, schema):
        self.asked.append((prompt, schema))
        return self._payload()

    async def aclose(self):
        self.closed = True


def _boom(*args, **kwargs):
    raise AssertionError("wrong classifier backend was invoked")


# ── keyword fast-path ─────────────────────────────────────────────────────────

def test_keyword_fast_path_short_circuits_both_backends(agent, monkeypatch):
    monkeypatch.setattr(apply_jobs.nim_client, "resolve_classifier", _boom)
    monkeypatch.setattr(apply_jobs.nim_client, "classify_via_nim", _boom)
    monkeypatch.setattr(apply_jobs, "ClaudeSession", _boom)

    desc = "Exciting team. Must be a US citizen. Relocation offered."
    relevant, reason, citizenship = asyncio.run(
        agent.classify("Software Engineer", desc, "OffsiteApply")
    )
    assert relevant is False
    assert citizenship is True
    assert "citizen" in reason.lower()
    assert agent._session is None  # no Agent SDK session ever created


def test_keyword_fast_path_logs_keyword_route(agent, monkeypatch):
    entries: list = []
    monkeypatch.setattr(apply_jobs, "_write_llm_log", entries.append)
    monkeypatch.setattr(apply_jobs, "ClaudeSession", _boom)
    monkeypatch.setattr(apply_jobs.nim_client, "resolve_classifier", _boom)

    asyncio.run(agent.classify("Eng", "Requires TS/SCI clearance.", "ComplexOnsiteApply"))
    assert entries[-1]["route"] == "keyword"
    assert entries[-1]["type"] == "classifier"


# ── OffsiteApply → NIM ────────────────────────────────────────────────────────

def test_offsite_apply_routes_to_nim(agent, monkeypatch):
    calls: dict = {}

    def fake_resolve(cfg=None):
        return ("NIM_CLIENT", "meta/llama-3.1-8b-instruct")

    def fake_classify(client, model, title, description):
        calls["args"] = (client, model, title)
        return {"relevant": True, "reason": "Data engineering role", "citizenship_required": False}

    monkeypatch.setattr(apply_jobs.nim_client, "resolve_classifier", fake_resolve)
    monkeypatch.setattr(apply_jobs.nim_client, "classify_via_nim", fake_classify)
    monkeypatch.setattr(apply_jobs, "ClaudeSession", _boom)  # Agent SDK must NOT be used

    relevant, reason, citizenship = asyncio.run(
        agent.classify("Data Engineer", "Build pipelines", "OffsiteApply")
    )
    assert relevant is True
    assert reason == "Data engineering role"
    assert citizenship is False
    assert calls["args"] == ("NIM_CLIENT", "meta/llama-3.1-8b-instruct", "Data Engineer")
    assert agent._session is None


def test_offsite_apply_nim_config_error_propagates(agent, monkeypatch):
    def raise_cfg(cfg=None):
        raise apply_jobs.nim_client.NimConfigError("no classifier key")

    monkeypatch.setattr(apply_jobs.nim_client, "resolve_classifier", raise_cfg)
    monkeypatch.setattr(apply_jobs, "ClaudeSession", _boom)

    with pytest.raises(apply_jobs.nim_client.NimConfigError):
        asyncio.run(agent.classify("X", "desc", "OffsiteApply"))


# ── Easy Apply → Claude Agent SDK ────────────────────────────────────────────

@pytest.mark.parametrize("app_type", ["SimpleOnsiteApply", "ComplexOnsiteApply", ""])
def test_easy_apply_routes_to_agent_sdk(agent, monkeypatch, app_type):
    monkeypatch.setattr(apply_jobs, "ClaudeSession", _FakeSession)
    monkeypatch.setattr(apply_jobs.nim_client, "resolve_classifier", _boom)
    monkeypatch.setattr(apply_jobs.nim_client, "classify_via_nim", _boom)

    relevant, reason, citizenship = asyncio.run(
        agent.classify("ML Engineer", "PyTorch, distributed training", app_type)
    )
    assert relevant is True
    assert reason == "Backend SWE role"
    session = _FakeSession.last
    assert session.kwargs["model"] == "claude-haiku-4-5"
    assert session.kwargs["log_type"] == "classifier"
    assert session.kwargs["log_calls"] is False
    assert len(session.asked) == 1


def test_agent_session_is_reused_and_closed(agent, monkeypatch):
    monkeypatch.setattr(apply_jobs, "ClaudeSession", _FakeSession)

    async def _run():
        await agent.classify("A", "desc one", "SimpleOnsiteApply")
        await agent.classify("B", "desc two", "ComplexOnsiteApply")
        first = agent._session
        await agent.aclose()
        return first

    session = asyncio.run(_run())
    assert session is _FakeSession.last  # same session object across both calls
    assert len(session.asked) == 2
    assert session.closed is True
    assert agent._session is None


# ── structured-output parsing / citizenship override ─────────────────────────

def test_structured_response_parsed_into_tuple(agent, monkeypatch):
    class _S(_FakeSession):
        def _payload(self):
            return {"relevant": True, "reason": "Great fit", "citizenship_required": False}

    monkeypatch.setattr(apply_jobs, "ClaudeSession", _S)
    out = asyncio.run(agent.classify("SWE", "desc", "SimpleOnsiteApply"))
    assert out == (True, "Great fit", False)


def test_citizenship_required_forces_irrelevant(agent, monkeypatch):
    class _S(_FakeSession):
        def _payload(self):
            return {"relevant": True, "reason": "Relevant but cleared", "citizenship_required": True}

    monkeypatch.setattr(apply_jobs, "ClaudeSession", _S)
    relevant, _reason, citizenship = asyncio.run(
        agent.classify("SWE", "desc", "SimpleOnsiteApply")
    )
    assert relevant is False
    assert citizenship is True


def test_telemetry_records_route_and_model(agent, monkeypatch):
    entries: list = []
    monkeypatch.setattr(apply_jobs, "_write_llm_log", entries.append)
    monkeypatch.setattr(apply_jobs, "ClaudeSession", _FakeSession)

    asyncio.run(agent.classify("SWE", "desc", "SimpleOnsiteApply"))
    entry = entries[-1]
    assert entry["type"] == "classifier"
    assert entry["route"] == "agent_sdk"
    assert entry["model"] == "claude-haiku-4-5"
    assert "duration_ms" in entry
    assert entry["result"]["relevant"] is True


def test_nim_telemetry_route(agent, monkeypatch):
    entries: list = []
    monkeypatch.setattr(apply_jobs, "_write_llm_log", entries.append)
    monkeypatch.setattr(apply_jobs.nim_client, "resolve_classifier",
                        lambda cfg=None: ("C", "m"))
    monkeypatch.setattr(apply_jobs.nim_client, "classify_via_nim",
                        lambda *a, **k: {"relevant": False, "reason": "unrelated",
                                         "citizenship_required": False})
    asyncio.run(agent.classify("Nurse", "desc", "OffsiteApply"))
    assert entries[-1]["route"] == "nim"
    assert entries[-1]["model"] == "m"


# ── acceptance guards ────────────────────────────────────────────────────────

def test_no_claude_subprocess_or_fence_salvage_in_apply_jobs():
    src = Path(apply_jobs.__file__).read_text()
    assert 'subprocess.run(["claude"' not in src
    assert "strip_code_fence" not in src
    assert "extract_json_object" not in src
    assert "classifier_client" not in src


def test_classify_signature_takes_application_type():
    import inspect
    params = list(inspect.signature(apply_jobs.JobAgent.classify).parameters)
    assert params == ["self", "title", "description", "application_type"]
