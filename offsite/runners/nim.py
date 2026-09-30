"""NIM runner: OpenAI Agents SDK + NVIDIA NIM, driving guard-mcp (OA8).

Model ``OFFSITE_NIM_MODEL`` (default ``deepseek-ai/deepseek-v4.1-flash``) on
NIM's OpenAI-compatible endpoint (``OFFSITE_NIM_BASE_URL``, default
``https://integrate.api.nvidia.com/v1``). Key: ``NVIDIA_API_KEY``, falling back
to the existing ``LLM_API`` when ``LLM_URL`` points at NIM.

* Agents SDK **tracing is disabled** — it would upload prompts (the applicant
  profile) to OpenAI.
* MCP calls get a 300 s session timeout (the SDK default of 5 s is shorter than
  a navigation, and guard-mcp types a long answer at ~0.2 s per character).
* The run stops as soon as ``RunControl.outcome`` is set (``stop_when``), so a
  stubborn model doesn't burn turns on ``STOP`` replies.
* Each NIM request is retried up to **3** times on a timeout (also on 429 /
  5xx / connection errors) before the run fails.
* Errors map to ``RunResult(status="error", error=…)``: timeout, HTTP error,
  429, turn limit, and invalid tool calls / output after **one retry** (the
  retry continues on the same page — the page is the state).
"""
from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable
from dataclasses import dataclass

import openai
from agents import (
    Agent,
    ModelSettings,
    OpenAIChatCompletionsModel,
    RunHooks,
    Runner,
    set_tracing_disabled,
)
from agents.exceptions import MaxTurnsExceeded, ModelBehaviorError, ModelTimeoutError
from agents.mcp import MCPServerStreamableHttp

import config  # noqa: F401  (loads .env into os.environ on first config call)
from common import write_llm_log
from config import _load_dotenv
from offsite.runners import RunResult

DEFAULT_MODEL = "deepseek-ai/deepseek-v4.1-flash"
DEFAULT_BASE_URL = "https://integrate.api.nvidia.com/v1"
MAX_TURNS = 60          # > RunControl.BUDGET (40): the guard's budget ends runs first
REQUEST_TIMEOUT = 90    # seconds per NIM completion request attempt
# Owner decision: a timed-out NIM request is retried up to 3 times before the run
# fails (→ Claude fallback). The OpenAI client applies the same count to 429 / 5xx /
# connection errors. Worst case per model turn: 4 × 90 s.
REQUEST_RETRIES = 3
RUN_TIMEOUT = 30 * 60   # whole run (human-paced typing makes long forms take 10+ min)

set_tracing_disabled(True)


class MissingKeyError(RuntimeError):
    pass


@dataclass(frozen=True)
class NimConfig:
    model: str
    base_url: str
    api_key: str

    @classmethod
    def from_env(cls) -> NimConfig:
        _load_dotenv()
        env = lambda k: (os.environ.get(k) or "").strip()  # noqa: E731
        base_url = env("OFFSITE_NIM_BASE_URL") or DEFAULT_BASE_URL
        key = env("NVIDIA_API_KEY")
        if not key and "nvidia.com" in env("LLM_URL"):
            key = env("LLM_API")
        if not key:
            raise MissingKeyError(
                "no NIM API key — set NVIDIA_API_KEY in .env (an nvapi-… key from "
                "build.nvidia.com)")
        return cls(model=env("OFFSITE_NIM_MODEL") or DEFAULT_MODEL, base_url=base_url,
                   api_key=key)


class _StopRun(Exception):
    """Raised from the tool hook once RunControl has an outcome."""


class _RunTimeout(Exception):
    pass


class _Hooks(RunHooks):
    """Counts turns, logs each model call's latency, stops on a control outcome."""

    def __init__(self, stop_when: Callable[[], bool] | None, model: str = "") -> None:
        self.stop_when = stop_when
        self.model = model
        self.turns = 0
        self._t = 0.0

    async def on_llm_start(self, context, agent, system_prompt, input_items) -> None:
        self.turns += 1
        self._t = time.monotonic()

    async def on_llm_end(self, context, agent, response) -> None:
        write_llm_log({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "source": "nim_runner",
                       "model": self.model, "turn": self.turns,
                       "seconds": round(time.monotonic() - self._t, 1)})

    async def on_tool_end(self, context, agent, tool, result) -> None:
        if self.stop_when is not None and self.stop_when():
            raise _StopRun


def _describe(e: BaseException) -> str:
    if isinstance(e, _RunTimeout):
        return str(e)
    if isinstance(e, openai.RateLimitError):
        return "NIM rate limit (HTTP 429)"
    if isinstance(e, openai.APIStatusError):
        return f"NIM HTTP {e.status_code}: {str(e)[:200]}"
    if isinstance(e, (openai.APITimeoutError, ModelTimeoutError, asyncio.TimeoutError,
                      TimeoutError)):
        return "NIM request timed out"
    if isinstance(e, openai.APIConnectionError):
        return f"NIM connection error: {e}"
    if isinstance(e, MaxTurnsExceeded):
        return f"turn limit ({MAX_TURNS}) reached"
    if isinstance(e, ModelBehaviorError):
        return f"invalid tool call / output: {str(e)[:200]}"
    return f"{type(e).__name__}: {str(e)[:200]}"


async def run(
    system: str,
    task: str,
    guard_url: str,
    *,
    stop_when: Callable[[], bool] | None = None,
    cfg: NimConfig | None = None,
    max_turns: int = MAX_TURNS,
    run_timeout: float = RUN_TIMEOUT,
) -> RunResult:
    """One NIM run against guard-mcp at ``guard_url``. Never raises for model/API
    failures — they come back as ``RunResult(status="error")``."""
    cfg = cfg or NimConfig.from_env()
    t0 = time.monotonic()
    hooks = _Hooks(stop_when, cfg.model)
    client = openai.AsyncOpenAI(base_url=cfg.base_url, api_key=cfg.api_key,
                                timeout=REQUEST_TIMEOUT, max_retries=REQUEST_RETRIES)
    model = OpenAIChatCompletionsModel(model=cfg.model, openai_client=client)
    label = f"nim:{cfg.model}"

    def result(status, error=None, final=""):
        return RunResult(status=status, model=label, error=error, turns=hooks.turns,
                         seconds=round(time.monotonic() - t0, 1), final_text=final)

    try:
        async with MCPServerStreamableHttp(
            # long: human-paced typing of a free-text answer takes ~0.2 s per character
            params={"url": guard_url, "timeout": 60, "sse_read_timeout": 600},
            name="guard", cache_tools_list=True, client_session_timeout_seconds=300,
        ) as server:
            agent = Agent(
                name="offsite-nim", instructions=system, model=model, mcp_servers=[server],
                model_settings=ModelSettings(temperature=0.2, parallel_tool_calls=False),
            )
            for attempt in (1, 2):
                try:
                    try:
                        out = await asyncio.wait_for(
                            Runner.run(agent, task, max_turns=max_turns, hooks=hooks),
                            timeout=run_timeout)
                    except TimeoutError:
                        raise _RunTimeout(
                            f"run exceeded {int(run_timeout)} s ({hooks.turns} turns)") from None
                    return result("finished", final=str(out.final_output or ""))
                except ModelBehaviorError as e:
                    if attempt == 2:
                        return result("error", _describe(e))
                    # one retry on the same page: the model re-reads it and goes on
                    task = (task + "\n\nYour previous attempt produced an invalid tool "
                            "call. Take a browser_snapshot and continue.")
    except Exception as e:  # noqa: BLE001 - every failure is a fallback trigger
        # The SDK wraps exceptions raised in hooks (our _StopRun) in UserError:
        # once RunControl has an outcome, however the run unwound, it finished.
        if isinstance(e, _StopRun) or (stop_when is not None and stop_when()):
            return result("finished")
        return result("error", _describe(e))
    finally:
        await client.close()
    return result("error", "unreachable")  # pragma: no cover
