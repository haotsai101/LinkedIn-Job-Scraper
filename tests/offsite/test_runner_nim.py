"""OA8: NIM runner — config, error mapping, stop-on-outcome, tracing off.

No network: ``Runner.run`` and the MCP server are stubbed. The live run against
the fixtures is a manual/QA step (``python -m offsite.run_agent --model nim``),
or ``OFFSITE_LIVE=1 pytest -k live``.
"""
from __future__ import annotations

import asyncio
import os
import shutil

import httpx
import openai
import pytest
from agents.exceptions import MaxTurnsExceeded, ModelBehaviorError, UserError

from offsite.runners import nim
from offsite.runners.nim import MissingKeyError, NimConfig

CFG = NimConfig(model="deepseek-ai/deepseek-v4.1-flash", base_url="https://nim.test/v1",
                api_key="nvapi-test")


@pytest.fixture(autouse=True)
def _no_dotenv(monkeypatch):
    monkeypatch.setattr(nim, "_load_dotenv", lambda: None)
    for k in ("NVIDIA_API_KEY", "LLM_API", "LLM_URL", "OFFSITE_NIM_MODEL",
              "OFFSITE_NIM_BASE_URL"):
        monkeypatch.delenv(k, raising=False)


# ── config ────────────────────────────────────────────────────────────────────

def test_config_defaults_and_overrides(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-a")
    c = NimConfig.from_env()
    assert (c.model, c.base_url, c.api_key) == (
        "deepseek-ai/deepseek-v4.1-flash", "https://integrate.api.nvidia.com/v1", "nvapi-a")
    monkeypatch.setenv("OFFSITE_NIM_MODEL", "other/model")
    assert NimConfig.from_env().model == "other/model"


def test_config_falls_back_to_llm_api_only_for_nim_urls(monkeypatch):
    monkeypatch.setenv("LLM_API", "nvapi-legacy")
    monkeypatch.setenv("LLM_URL", "https://api.openai.com/v1")
    with pytest.raises(MissingKeyError):
        NimConfig.from_env()
    monkeypatch.setenv("LLM_URL", "https://integrate.api.nvidia.com/v1")
    assert NimConfig.from_env().api_key == "nvapi-legacy"


def test_tracing_is_disabled():
    from agents.tracing import get_trace_provider
    provider = get_trace_provider()
    assert getattr(provider, "_disabled", None) is True


# ── run(): stubbed SDK ────────────────────────────────────────────────────────

class _FakeServer:
    def __init__(self, *a, **kw):
        self.kw = kw

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None


class _Out:
    final_output = "done"


def _req():
    return httpx.Request("POST", "https://nim.test/v1/chat/completions")


def _resp(code):
    return httpx.Response(code, request=_req())


@pytest.fixture
def stub(monkeypatch):
    calls = {"n": 0, "tasks": []}
    behaviour: list = []

    async def fake_run(agent, task, *, max_turns, hooks):
        calls["n"] += 1
        calls["tasks"].append(task)
        calls["agent"] = agent
        step = behaviour.pop(0) if behaviour else "ok"
        if isinstance(step, BaseException):
            raise step
        if callable(step):
            return await step(hooks)
        return _Out()

    monkeypatch.setattr(nim, "MCPServerStreamableHttp", _FakeServer)
    monkeypatch.setattr(nim.Runner, "run", fake_run)
    return calls, behaviour


def run(**kw):
    return asyncio.run(nim.run("SYSTEM", "TASK", "http://127.0.0.1:1/mcp", cfg=CFG, **kw))


def test_nim_requests_retry_three_times_on_timeout(stub, monkeypatch):
    seen = {}
    real = nim.openai.AsyncOpenAI

    def capture(**kw):
        seen.update(kw)
        return real(**kw)

    monkeypatch.setattr(nim.openai, "AsyncOpenAI", capture)
    run()
    assert nim.REQUEST_RETRIES == 3 and seen["max_retries"] == 3
    assert seen["timeout"] == nim.REQUEST_TIMEOUT


def test_finished(stub):
    r = run()
    assert r.status == "finished" and r.final_text == "done"
    assert r.model == "nim:deepseek-ai/deepseek-v4.1-flash"
    calls, _ = stub
    assert calls["agent"].instructions == "SYSTEM"
    assert calls["agent"].model_settings.parallel_tool_calls is False


@pytest.mark.parametrize("exc,expected", [
    (openai.RateLimitError("slow down", response=_resp(429), body=None), "429"),
    (openai.InternalServerError("boom", response=_resp(502), body=None), "HTTP 502"),
    (openai.APITimeoutError(request=_req()), "timed out"),
    (openai.APIConnectionError(request=_req()), "connection error"),
    (MaxTurnsExceeded("too many"), "turn limit"),
    (RuntimeError("weird"), "RuntimeError: weird"),
])
def test_errors_map_to_fallback_triggers(stub, exc, expected):
    _, behaviour = stub
    behaviour.append(exc)
    r = run()
    assert r.status == "error" and expected in r.error


def test_invalid_output_is_retried_once_then_error(stub):
    calls, behaviour = stub
    behaviour.extend([ModelBehaviorError("bad json"), "ok"])
    assert run().status == "finished"
    assert calls["n"] == 2 and "invalid tool call" in calls["tasks"][1]

    calls["n"] = 0
    behaviour.extend([ModelBehaviorError("bad"), ModelBehaviorError("bad again")])
    r = run()
    assert r.status == "error" and "invalid tool call" in r.error and calls["n"] == 2


def test_stop_when_outcome_is_set_even_if_sdk_wraps_the_stop(stub):
    _, behaviour = stub
    outcome = {"v": None}

    async def model_that_reports_then_keeps_going(hooks):
        outcome["v"] = "ready"             # report_ready was just recorded
        try:
            await hooks.on_tool_end(None, None, None, None)
        except Exception as e:             # the SDK wraps hook errors like this
            raise UserError("Error running tool report_ready: ") from e
        raise AssertionError("hook should have stopped the run")

    behaviour.append(model_that_reports_then_keeps_going)
    r = run(stop_when=lambda: outcome["v"] is not None)
    assert r.status == "finished" and r.error is None


def test_run_timeout(stub):
    _, behaviour = stub

    async def hang(hooks):
        await asyncio.sleep(5)

    behaviour.append(hang)
    r = run(run_timeout=0.05)
    assert r.status == "error" and "run exceeded" in r.error


# ── live (opt-in) ─────────────────────────────────────────────────────────────

@pytest.mark.skipif(os.environ.get("OFFSITE_LIVE") != "1" or shutil.which("npx") is None,
                    reason="live NIM run: set OFFSITE_LIVE=1 (uses the NIM API, ~2 min)")
def test_live_nim_fills_multipage_without_submitting(tmp_path, monkeypatch):
    import json
    import urllib.request

    from offsite import run_agent
    from offsite.prompts import JobInfo, load_profile
    from tests.fixtures.offsite.serve import fixture_server

    monkeypatch.undo()  # use the real .env key
    with fixture_server() as base:
        job = JobInfo(None, "Software Engineer", "Acme Robotics", f"{base}/multipage.html")
        res, control = asyncio.run(run_agent.run_one(
            "nim", job.url, job=job, profile=load_profile(), headless=True, budget=40,
            wait=False, profile_dir=str(tmp_path / "p")))
        count = json.loads(urllib.request.urlopen(f"{base}/__submissions").read())["count"]
    assert control.outcome == "ready", (res, control.stop_reason)
    assert res.status == "finished"
    assert count == 0
    labels = " ".join(a.field_label.lower() for a in control.answers)
    assert "sponsorship" in labels and "email" in labels
