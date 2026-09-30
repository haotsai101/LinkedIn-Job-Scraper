"""Claude runner: Claude Agent SDK (subscription auth), driving guard-mcp (OA9).

Same contract as the NIM runner (``run(system, task, guard_url, stop_when=…)
-> RunResult``), so OA10 can swap one for the other on the same page.

Locked down — the model can only reach the browser through guard-mcp:

* ``tools=[]``: no Claude Code built-in tools (Bash, Read, Write, WebFetch, …),
  plus an explicit ``disallowed_tools`` list as a second layer;
* ``mcp_servers={"guard": http}`` with ``strict_mcp_config=True``: none of the
  user's own MCP servers are loaded;
* ``setting_sources=[]``: no user / project settings or CLAUDE.md;
* ``permission_mode="dontAsk"`` + ``allowed_tools=["mcp__guard"]``: only
  guard-mcp tools may run, nothing prompts;
* ``MCP_TOOL_TIMEOUT`` = 300 s (guard-mcp types long answers at human pace).

The run stops as soon as ``RunControl.outcome`` is set. Model: the
``guided_apply`` model from ``config`` (``OFFSITE_CLAUDE_MODEL`` overrides).
"""
from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable
from typing import Any

from common import write_llm_log
from config import _load_dotenv, get_llm_config
from offsite.runners import RunResult

MAX_TURNS = 60
RUN_TIMEOUT = 30 * 60
GUARD = "guard"
BUILTIN_TOOLS = ["Bash", "Read", "Write", "Edit", "MultiEdit", "NotebookEdit", "Glob", "Grep",
                 "WebFetch", "WebSearch", "Task", "Agent", "TodoWrite", "KillShell",
                 "BashOutput", "Skill", "SlashCommand", "ExitPlanMode"]


def model_name() -> str:
    _load_dotenv()
    return (os.environ.get("OFFSITE_CLAUDE_MODEL") or "").strip() or \
        get_llm_config("guided_apply").model


def build_options(system: str, guard_url: str, *, model: str, max_turns: int = MAX_TURNS):
    import claude_agent_sdk as sdk

    return sdk.ClaudeAgentOptions(
        model=model,
        system_prompt=system,
        tools=[],
        allowed_tools=[f"mcp__{GUARD}"],
        disallowed_tools=list(BUILTIN_TOOLS),
        mcp_servers={GUARD: {"type": "http", "url": guard_url}},
        strict_mcp_config=True,
        setting_sources=[],
        permission_mode="dontAsk",
        max_turns=max_turns,
        env={"MCP_TOOL_TIMEOUT": "300000", "MCP_TIMEOUT": "60000"},
    )


async def run(
    system: str,
    task: str,
    guard_url: str,
    *,
    stop_when: Callable[[], bool] | None = None,
    model: str | None = None,
    max_turns: int = MAX_TURNS,
    run_timeout: float = RUN_TIMEOUT,
    query: Any = None,
) -> RunResult:
    """One Claude run against guard-mcp. Never raises for model/SDK failures.
    ``query`` is injectable for tests (defaults to ``claude_agent_sdk.query``)."""
    t0 = time.monotonic()
    model = model or model_name()
    label = f"claude:{model}"
    state: dict[str, Any] = {"turns": 0, "tools": [], "final": "", "error": None,
                             "stopped": False}

    if query is None:
        try:
            import claude_agent_sdk as sdk
        except ImportError as e:  # pragma: no cover - dependency is pinned
            return RunResult("error", label, f"claude-agent-sdk not importable: {e}")
        query = sdk.query

    async def consume() -> None:
        options = build_options(system, guard_url, model=model, max_turns=max_turns)
        stream = query(prompt=task, options=options)
        try:
            async for msg in stream:
                kind = type(msg).__name__
                if kind == "AssistantMessage":
                    state["turns"] += 1
                    if getattr(msg, "error", None):
                        state["error"] = f"assistant error: {msg.error}"
                    for block in getattr(msg, "content", None) or []:
                        bname = type(block).__name__
                        if bname == "ToolUseBlock":
                            state["tools"].append(block.name)
                        elif bname == "TextBlock":
                            state["final"] = getattr(block, "text", "") or state["final"]
                elif kind == "ResultMessage":
                    if getattr(msg, "is_error", False) or str(
                            getattr(msg, "subtype", "")).startswith("error"):
                        state["error"] = _result_error(msg)
                    elif getattr(msg, "result", None):
                        state["final"] = msg.result
                if stop_when is not None and stop_when():
                    state["stopped"] = True
                    break
        finally:
            aclose = getattr(stream, "aclose", None)
            if aclose is not None:
                try:
                    await aclose()
                except Exception:  # noqa: BLE001 - closing a killed CLI can complain
                    pass

    try:
        await asyncio.wait_for(consume(), timeout=run_timeout)
    except TimeoutError:
        state["error"] = f"run exceeded {int(run_timeout)} s ({state['turns']} turns)"
    except Exception as e:  # noqa: BLE001 - every failure is a fallback trigger / human
        if not (stop_when is not None and stop_when()):
            state["error"] = f"{type(e).__name__}: {str(e)[:200]}"

    non_guard = sorted({t for t in state["tools"] if not t.startswith(f"mcp__{GUARD}__")})
    write_llm_log({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "source": "claude_runner",
                   "model": model, "turns": state["turns"], "tools": len(state["tools"]),
                   "non_guard_tools": non_guard, "error": state["error"],
                   "seconds": round(time.monotonic() - t0, 1)})
    finished = state["stopped"] or state["error"] is None
    if stop_when is not None and stop_when():
        finished = True  # an outcome is an outcome, however the stream ended
    return RunResult(
        status="finished" if finished else "error",
        model=label,
        error=None if finished else state["error"],
        turns=state["turns"],
        seconds=round(time.monotonic() - t0, 1),
        final_text=str(state["final"] or "")[:2000],
    )


def _result_error(msg: Any) -> str:
    sub = str(getattr(msg, "subtype", "") or "")
    if sub == "error_max_turns":
        return f"turn limit ({MAX_TURNS}) reached"
    status = getattr(msg, "api_error_status", None)
    if status == 429:
        return "Claude rate limit (HTTP 429)"
    errs = getattr(msg, "errors", None) or []
    detail = "; ".join(str(e) for e in errs)[:200] or str(getattr(msg, "result", "") or "")[:200]
    return f"{sub or 'error'}" + (f" (HTTP {status})" if status else "") + \
        (f": {detail}" if detail else "")
