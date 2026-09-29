"""Unit tests for ``apply_jobs.JobAgent.classify`` (T14 part 1).

Contract (T38 — the Claude Agent SDK is the only backend now; the opt-in NIM
route and the NIM-timeout circuit breaker were removed along with
OffsiteApplyFlow, the only thing that ever routed to NIM):
  * citizenship / clearance keyword in the description → immediate skip, no
    LLM call
  * every job, regardless of ``application_type`` → the Claude Agent SDK
    (``llm.query_json``, one-shot isolated call)

The Agent SDK backend is mocked — no network, no ``claude`` CLI.
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

import pytest

import apply_jobs
import llm

_PROFILE = {"full_name": "Test User", "skills": ["Python", "PyTorch"]}


@pytest.fixture
def agent():
    return apply_jobs.JobAgent(_PROFILE)


@pytest.fixture(autouse=True)
def _fast_retry(monkeypatch):
    """Kill the 2 s retry sleep so failure-path tests are instant."""
    monkeypatch.setattr(apply_jobs.JobAgent, "_RETRY_DELAY_S", 0.0)


def _boom(*args, **kwargs):
    raise AssertionError("classifier backend should not have been invoked")


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


# ── keyword fast-path ─────────────────────────────────────────────────────────

def test_keyword_fast_path_short_circuits_the_llm_call(agent, monkeypatch):
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


# ── every application_type routes to the Agent SDK ────────────────────────────

@pytest.mark.parametrize(
    "app_type", ["OffsiteApply", "SimpleOnsiteApply", "ComplexOnsiteApply", "", None]
)
def test_every_application_type_routes_to_agent_sdk(agent, monkeypatch, app_type):
    calls: list = []
    _patch_agent_sdk(monkeypatch, calls=calls)

    relevant, reason, _cit = asyncio.run(
        agent.classify("Backend Engineer", "Go, Postgres, k8s", app_type)
    )
    assert relevant is True
    assert len(calls) == 1
    assert "Backend Engineer" in calls[0]


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


def test_offsite_apply_symbols_are_gone():
    """T-teardown acceptance guard: the removed OffsiteApplyFlow / NIM
    classifier plumbing is actually gone from apply_jobs, not just renamed.
    (Comments are allowed to still mention OffsiteApplyFlow historically —
    this checks live symbols, not raw source text.)"""
    for gone in (
        "OffsiteApplyFlow", "nim_client", "_classify_nim",
        "classify_with_circuit_breaker", "_new_classifier_breaker",
        "_NIM_CLASSIFIER_ENABLED", "_OFFSITE_SPAM", "_match_spam_domain",
    ):
        assert not hasattr(apply_jobs, gone), f"apply_jobs.{gone} should have been removed"

    import importlib.util
    assert importlib.util.find_spec("nim_client") is None, "nim_client.py should be deleted"


# ── T27: per-attempt timeout ────────────────────────────────────────────────

def test_classifier_timeout_is_not_retried(agent, monkeypatch):
    """A timed-out call fails fast — no second 40s attempt."""
    monkeypatch.setattr(apply_jobs.JobAgent, "_ATTEMPT_TIMEOUT_S", 0.05)
    calls = {"n": 0}

    async def slow(prompt, schema, *, model, system=None, log_type="classifier",
                   log_calls=False):
        calls["n"] += 1
        await asyncio.sleep(0.3)  # longer than the per-attempt deadline
        return _agent_payload()

    monkeypatch.setattr(llm, "query_json", slow)

    with pytest.raises(TimeoutError):
        asyncio.run(agent.classify("T", "d", "SimpleOnsiteApply"))
    assert calls["n"] == 1  # timeout → no retry


def test_transient_error_still_retried_under_per_attempt_deadline(agent, monkeypatch):
    """A non-timeout transient error IS retried, and attempt 2 gets its own
    fresh deadline (the wait_for is inside the loop)."""
    monkeypatch.setattr(apply_jobs.JobAgent, "_ATTEMPT_TIMEOUT_S", 0.5)
    calls = {"n": 0}

    async def flaky(prompt, schema, *, model, system=None, log_type="classifier",
                    log_calls=False):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient 503")
        return _agent_payload()

    monkeypatch.setattr(llm, "query_json", flaky)

    out = asyncio.run(agent.classify("T", "d", "SimpleOnsiteApply"))
    assert calls["n"] == 2
    assert out[0] is True


def test_deferred_jobs_are_not_counted_as_skipped():
    """run_session's classify except-block must bump deferred_count, never
    skipped_count, and must not mark_job (job stays pending)."""
    src = Path(apply_jobs.__file__).read_text()
    m = re_search_classify_except_block(src)
    assert m, "classify except-block not found"
    block = "\n".join(
        ln for ln in m.splitlines() if not ln.lstrip().startswith("#")
    )
    assert "deferred_count += 1" in block
    assert "skipped_count += 1" not in block
    assert "mark_job" not in block
    # and the two counters are distinct fields in the session report
    assert '"deferred_count": deferred_count' in src
    assert '"skipped_count": skipped_count' in src


def re_search_classify_except_block(src: str) -> str | None:
    import re
    m = re.search(
        r"try:\n\s*relevant, reason, citizenship_required = await agent\.classify\("
        r".*?\n(\s*except Exception as exc:.*?\n\s*continue\n)",
        src, re.S,
    )
    return m.group(1) if m else None
