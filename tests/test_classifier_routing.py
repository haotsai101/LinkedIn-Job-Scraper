"""Unit tests for ``apply_jobs.JobAgent.classify`` routing (T14 part 1).

Routing contract:
  * citizenship / clearance keyword in the description → immediate skip, no LLM
    call on either backend
  * application_type == "OffsiteApply"                  → NIM (nim_client)
  * SimpleOnsiteApply / ComplexOnsiteApply / other / None → Claude Agent SDK
    one-shot isolated call (llm.query_json)

Both backends are mocked — no network, no ``claude`` CLI, no OpenAI calls.
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

import pytest

import apply_jobs
import llm
import nim_client

_PROFILE = {"full_name": "Test User", "skills": ["Python", "PyTorch"]}


@pytest.fixture
def agent():
    return apply_jobs.JobAgent(_PROFILE)


@pytest.fixture(autouse=True)
def _fast_retry(monkeypatch):
    """Kill the 2 s retry sleep so failure-path tests are instant."""
    monkeypatch.setattr(apply_jobs.JobAgent, "_RETRY_DELAY_S", 0.0)


def _boom(*args, **kwargs):
    raise AssertionError("wrong classifier backend was invoked")


def _agent_payload(**over):
    d = {"relevant": True, "reason": "Backend SWE role", "citizenship_required": False}
    d.update(over)
    return d


def _patch_agent_sdk(monkeypatch, payload=None, *, calls=None, exc=None):
    """Patch llm.query_json with an async stub."""
    async def stub(prompt, schema, *, model, system=None, log_type="classifier",
                   log_calls=False):
        if calls is not None:
            calls.append(prompt)
        if exc is not None:
            raise exc
        return payload if payload is not None else _agent_payload()

    monkeypatch.setattr(llm, "query_json", stub)


def _patch_nim(monkeypatch, payload=None, *, calls=None, exc=None,
               model="google/gemma-4-31b-it"):
    def resolve(cfg=None):
        return ("NIM_CLIENT", model)

    def classify(client, m, title, description):
        if calls is not None:
            calls.append((client, m, title))
        if exc is not None:
            raise exc
        return payload if payload is not None else _agent_payload()

    monkeypatch.setattr(nim_client, "resolve_classifier", resolve)
    monkeypatch.setattr(nim_client, "classify_via_nim", classify)


# ── keyword fast-path ─────────────────────────────────────────────────────────

def test_keyword_fast_path_short_circuits_both_backends(agent, monkeypatch):
    monkeypatch.setattr(nim_client, "resolve_classifier", _boom)
    monkeypatch.setattr(nim_client, "classify_via_nim", _boom)
    monkeypatch.setattr(llm, "query_json", _boom)

    desc = "Exciting team. Must be a US citizen. Relocation offered."
    relevant, reason, citizenship = asyncio.run(
        agent.classify("Software Engineer", desc, "OffsiteApply")
    )
    assert relevant is False
    assert citizenship is True
    assert "citizen" in reason.lower()


def test_keyword_fast_path_logs_keyword_route(agent, monkeypatch):
    entries: list = []
    monkeypatch.setattr(apply_jobs, "_write_llm_log", entries.append)
    monkeypatch.setattr(llm, "query_json", _boom)

    asyncio.run(agent.classify("Eng", "Requires TS/SCI clearance.", "ComplexOnsiteApply"))
    assert entries[-1]["route"] == "keyword"
    assert entries[-1]["type"] == "classifier"


# ── OffsiteApply → NIM ────────────────────────────────────────────────────────

def test_offsite_apply_routes_to_nim(agent, monkeypatch):
    calls: list = []
    _patch_nim(monkeypatch, {"relevant": True, "reason": "Data engineering role",
                             "citizenship_required": False}, calls=calls)
    monkeypatch.setattr(llm, "query_json", _boom)  # Agent SDK must NOT be used

    relevant, reason, citizenship = asyncio.run(
        agent.classify("Data Engineer", "Build pipelines", "OffsiteApply")
    )
    assert (relevant, reason, citizenship) == (True, "Data engineering role", False)
    assert calls[0] == ("NIM_CLIENT", "google/gemma-4-31b-it", "Data Engineer")


def test_offsite_apply_nim_config_error_propagates(agent, monkeypatch):
    def raise_cfg(cfg=None):
        raise nim_client.NimConfigError("no classifier key")

    monkeypatch.setattr(nim_client, "resolve_classifier", raise_cfg)
    monkeypatch.setattr(llm, "query_json", _boom)

    with pytest.raises(nim_client.NimConfigError):
        asyncio.run(agent.classify("X", "desc", "OffsiteApply"))


def test_nim_config_error_is_not_retried(agent, monkeypatch):
    attempts = {"n": 0}

    def raise_cfg(cfg=None):
        attempts["n"] += 1
        raise nim_client.NimConfigError("no key")

    monkeypatch.setattr(nim_client, "resolve_classifier", raise_cfg)
    with pytest.raises(nim_client.NimConfigError):
        asyncio.run(agent.classify("X", "desc", "OffsiteApply"))
    assert attempts["n"] == 1  # deterministic — no retry


# ── Easy Apply → Claude Agent SDK (one-shot) ────────────────────────────────

@pytest.mark.parametrize("app_type", ["SimpleOnsiteApply", "ComplexOnsiteApply", "", None])
def test_easy_apply_routes_to_agent_sdk(agent, monkeypatch, app_type):
    calls: list = []
    _patch_agent_sdk(monkeypatch, calls=calls)
    monkeypatch.setattr(nim_client, "resolve_classifier", _boom)
    monkeypatch.setattr(nim_client, "classify_via_nim", _boom)

    relevant, reason, citizenship = asyncio.run(
        agent.classify("ML Engineer", "PyTorch, distributed training", app_type)
    )
    assert relevant is True
    assert reason == "Backend SWE role"
    assert len(calls) == 1
    assert "ML Engineer" in calls[0]


def test_agent_sdk_call_is_retried_once_then_succeeds(agent, monkeypatch):
    state = {"n": 0}

    async def flaky(prompt, schema, *, model, system=None, log_type="classifier",
                    log_calls=False):
        state["n"] += 1
        if state["n"] == 1:
            raise llm.ClaudeAgentSDKError("transient transport hiccup")
        return _agent_payload()

    monkeypatch.setattr(llm, "query_json", flaky)
    out = asyncio.run(agent.classify("SWE", "desc", "SimpleOnsiteApply"))
    assert state["n"] == 2
    assert out[0] is True


def test_agent_sdk_hard_failure_propagates_but_agent_still_usable(agent, monkeypatch):
    _patch_agent_sdk(monkeypatch, exc=llm.ClaudeAgentSDKError("still broken"))
    with pytest.raises(llm.ClaudeAgentSDKError):
        asyncio.run(agent.classify("SWE", "desc one", "SimpleOnsiteApply"))

    # A later good call on the same agent works — no poisoned state.
    _patch_agent_sdk(monkeypatch, _agent_payload(reason="recovered"))
    out = asyncio.run(agent.classify("SWE2", "desc two", "SimpleOnsiteApply"))
    assert out == (True, "recovered", False)


# ── structured-output parsing / citizenship override ─────────────────────────

def test_structured_response_parsed_into_tuple(agent, monkeypatch):
    _patch_agent_sdk(monkeypatch, {"relevant": True, "reason": "Great fit",
                                   "citizenship_required": False})
    out = asyncio.run(agent.classify("SWE", "d", "SimpleOnsiteApply"))
    assert out == (True, "Great fit", False)


def test_citizenship_required_forces_irrelevant(agent, monkeypatch):
    _patch_agent_sdk(monkeypatch, {"relevant": True, "reason": "Relevant but cleared",
                                   "citizenship_required": True})
    relevant, _reason, citizenship = asyncio.run(
        agent.classify("SWE", "d", "SimpleOnsiteApply")
    )
    assert relevant is False
    assert citizenship is True


def test_reason_says_not_relevant_overrides_relevant_true(agent, monkeypatch):
    _patch_agent_sdk(monkeypatch, {"relevant": True,
                                   "reason": "This is not relevant to the profile",
                                   "citizenship_required": False})
    relevant, _r, _c = asyncio.run(agent.classify("X", "d", "SimpleOnsiteApply"))
    assert relevant is False


# ── telemetry ───────────────────────────────────────────────────────────────

def test_agent_sdk_telemetry_records_route_and_model(agent, monkeypatch):
    entries: list = []
    monkeypatch.setattr(apply_jobs, "_write_llm_log", entries.append)
    _patch_agent_sdk(monkeypatch)

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
    _patch_nim(monkeypatch, {"relevant": False, "reason": "unrelated",
                             "citizenship_required": False}, model="m")
    asyncio.run(agent.classify("Nurse", "desc", "OffsiteApply"))
    assert entries[-1]["route"] == "nim"
    assert entries[-1]["model"] == "m"


# ── acceptance guards ───────────────────────────────────────────────────────

def test_no_claude_subprocess_or_fence_salvage_in_apply_jobs():
    src = Path(apply_jobs.__file__).read_text()
    assert 'subprocess.run(["claude"' not in src
    assert "strip_code_fence" not in src
    assert "extract_json_object" not in src
    assert "classifier_client" not in src


def test_classify_signature_takes_application_type():
    params = list(inspect.signature(apply_jobs.JobAgent.classify).parameters)
    assert params == ["self", "title", "description", "application_type"]
