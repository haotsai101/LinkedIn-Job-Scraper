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
import time
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
               model="meta/llama-3.2-11b-vision-instruct"):
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
    assert calls[0] == ("NIM_CLIENT", "meta/llama-3.2-11b-vision-instruct", "Data Engineer")


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
    assert params == ["self", "title", "description", "application_type", "prefer_agent_sdk"]


# ── T27: per-attempt timeout ────────────────────────────────────────────────

def test_run_with_retry_gives_each_attempt_its_own_deadline(agent, monkeypatch):
    """A slow call must be retried, not starved by one deadline over both attempts."""
    monkeypatch.setattr(apply_jobs.JobAgent, "_ATTEMPT_TIMEOUT_S", 0.05)
    calls = {"n": 0}

    def slow(client, model, title, description):
        calls["n"] += 1
        time.sleep(0.3)  # longer than one attempt's deadline
        return _agent_payload()

    monkeypatch.setattr(nim_client, "resolve_classifier", lambda cfg=None: ("C", "m"))
    monkeypatch.setattr(nim_client, "classify_via_nim", slow)

    with pytest.raises(TimeoutError):
        asyncio.run(agent.classify("T", "d", "OffsiteApply"))
    assert calls["n"] == 2  # each attempt got a fresh deadline


# ── T27: NIM-route circuit breaker ─────────────────────────────────────────

class _FakeAgent:
    """Stand-in for JobAgent: NIM route always times out, SDK route succeeds."""

    def __init__(self, sdk_result=(True, "ok", False), sdk_exc=None):
        self.routes: list[str] = []
        self._sdk_result = sdk_result
        self._sdk_exc = sdk_exc

    async def classify(self, title, description, application_type, *, prefer_agent_sdk=False):
        # Mirror JobAgent.classify routing: OffsiteApply → NIM unless forced.
        route = "nim" if application_type == "OffsiteApply" and not prefer_agent_sdk else "sdk"
        self.routes.append(route)
        if route == "nim":
            raise TimeoutError()
        if self._sdk_exc is not None:
            raise self._sdk_exc
        return self._sdk_result


@pytest.fixture(autouse=True)
def _silence_llm_log(monkeypatch):
    monkeypatch.setattr(apply_jobs, "_write_llm_log", lambda entry: None)


def test_circuit_breaker_routes_to_sdk_after_two_nim_timeouts():
    agent = _FakeAgent()
    breaker = apply_jobs._new_classifier_breaker()

    for _ in range(3):
        out = asyncio.run(apply_jobs.classify_with_circuit_breaker(
            agent, breaker, "T", "d", "OffsiteApply"))
        assert out == (True, "ok", False)

    # job1: nim→timeout→sdk ; job2: nim→timeout→sdk (streak hits 2, degraded) ;
    # job3: sdk only, no wasted nim attempt.
    assert agent.routes == ["nim", "sdk", "nim", "sdk", "sdk"]
    assert breaker["nim_route_degraded"] is True


def test_circuit_breaker_does_not_trip_on_single_timeout():
    agent = _FakeAgent()
    breaker = apply_jobs._new_classifier_breaker()

    out = asyncio.run(apply_jobs.classify_with_circuit_breaker(
        agent, breaker, "T", "d", "OffsiteApply"))
    assert out == (True, "ok", False)
    assert breaker["nim_timeout_streak"] == 1
    assert breaker["nim_route_degraded"] is False


def test_nim_timeout_then_sdk_success_does_not_raise():
    """So run_session does not count it as a classifier failure / fail-streak."""
    agent = _FakeAgent(sdk_result=(False, "not a fit", False))
    breaker = apply_jobs._new_classifier_breaker()

    out = asyncio.run(apply_jobs.classify_with_circuit_breaker(
        agent, breaker, "T", "d", "OffsiteApply"))
    assert out == (False, "not a fit", False)


def test_circuit_breaker_propagates_when_both_routes_fail():
    agent = _FakeAgent(sdk_exc=llm.ClaudeAgentSDKError("sdk down"))
    breaker = apply_jobs._new_classifier_breaker()

    with pytest.raises(llm.ClaudeAgentSDKError):
        asyncio.run(apply_jobs.classify_with_circuit_breaker(
            agent, breaker, "T", "d", "OffsiteApply"))


def test_circuit_breaker_ignores_easy_apply_jobs():
    agent = _FakeAgent()
    breaker = apply_jobs._new_classifier_breaker()

    out = asyncio.run(apply_jobs.classify_with_circuit_breaker(
        agent, breaker, "T", "d", "SimpleOnsiteApply"))
    assert out == (True, "ok", False)
    assert agent.routes == ["sdk"]
    assert breaker["nim_timeout_streak"] == 0


# ── T29 / T28: spam pre-filter ─────────────────────────────────────────────

def test_match_spam_domain_matches_posting_domain_and_url():
    assert apply_jobs._match_spam_domain("jobright.ai", "") == "jobright.ai"
    assert apply_jobs._match_spam_domain("", "https://www.dice.com/jobs/x") == "www.dice.com"
    assert apply_jobs._match_spam_domain("sub.crossover.com", "") == "sub.crossover.com"
    assert apply_jobs._match_spam_domain("example.com", "") is None
    assert apply_jobs._match_spam_domain("", "") is None


def test_greenhouse_is_not_pre_filtered():
    joined = " ".join(apply_jobs._OFFSITE_SPAM)
    assert "greenhouse" not in joined
    assert "grnh.se" not in apply_jobs._OFFSITE_SPAM
    assert apply_jobs._match_spam_domain(
        "job-boards.greenhouse.io",
        "https://job-boards.greenhouse.io/acme/jobs/1",
    ) is None
    assert apply_jobs._match_spam_domain("grnh.se", "") is None
