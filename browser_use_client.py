"""browser_use_client.py — OpenAI-compatible tool-calling client for the
agentic OffsiteApply engine (T54).

``offsite_agentic.AgenticOffsiteApplyFlow`` drives external ATS forms with
standard OpenAI ``chat.completions.create(..., tools=[...])`` function calling
against the ``browser_use`` role resolved from
``config.get_llm_config("browser_use")``. That role was already provisioned in
T14 for exactly this ("form-filling / DOM reasoning during offsite apply") but
sat dormant until now — see ``config.py``'s ``_DEFAULTS`` docstring.

This module intentionally mirrors ``nim_client.py``'s shape (same resolve /
call split, same error-handling style) so both OpenAI-compatible entry points
in this codebase look and behave the same way. Any OpenAI-compatible provider
works here — NVIDIA NIM (the default, free tier), OpenRouter, a self-hosted
endpoint, or the user's own key against any other compatible API — which is
the whole point of building this on the standard function-calling protocol
instead of the Claude Agent SDK's MCP tool wiring: nothing here is Claude- or
Anthropic-specific.

``openai`` stays a project dependency for this path (and the existing NIM
classifier route in ``nim_client.py``); the import is soft-guarded only so
``import browser_use_client`` does not explode in a stripped environment —
not a real fallback.
"""

from __future__ import annotations

from typing import Any

try:
    from openai import OpenAI
except ImportError:  # pragma: no cover - openai is a declared dependency
    OpenAI = None  # type: ignore[assignment, misc]

from config import LLMConfig, get_llm_config

# Request-level wall-clock ceiling (seconds). call_with_tools runs inside
# ``asyncio.to_thread`` (uncancellable) from AgenticOffsiteApplyFlow's
# conversation loop, exactly like nim_client.classify_via_nim is called from
# apply_jobs.JobAgent._classify_nim. A tool-calling turn can involve more
# reasoning than a bare classification call, so this ceiling is more generous
# than nim_client's 45s.
_TIMEOUT_S = 60.0


class BrowserUseConfigError(RuntimeError):
    """The agentic browser-use endpoint is not usable — no API key resolvable
    from ``BROWSER_USE_API`` (or the legacy alias ``BROWSER_LLM_API``), or
    ``openai`` is not installed.

    This message IS the "bring your own key" documentation for
    ``OFFSITE_ENGINE=agentic``: set ``BROWSER_USE_API`` (your provider's API
    key), ``BROWSER_USE_BASE_URL`` (its OpenAI-compatible base URL — defaults
    to NVIDIA NIM at https://integrate.api.nvidia.com/v1), and
    ``BROWSER_USE_MODEL`` (a tool/function-calling-capable model on that
    endpoint) in ``.env``. Any OpenAI-compatible provider works here — NVIDIA
    NIM, OpenRouter, a self-hosted vLLM/Ollama endpoint, etc.
    """


def resolve_browser_use(cfg: LLMConfig | None = None) -> tuple[Any, str]:
    """Return ``(OpenAI client, model name)`` for the ``browser_use`` role.

    Raises :class:`BrowserUseConfigError` when no API key can be resolved so
    the caller (``AgenticOffsiteApplyFlow``) surfaces a clear, actionable
    message instead of an opaque auth failure mid-application.
    """
    if OpenAI is None:  # pragma: no cover - openai is a declared dependency
        raise BrowserUseConfigError("the 'openai' package is not installed")
    cfg = cfg or get_llm_config("browser_use")
    if not cfg.api_key:
        raise BrowserUseConfigError(
            "No browser_use API key. OFFSITE_ENGINE=agentic needs an "
            "OpenAI-compatible endpoint: set BROWSER_USE_API (your API key), "
            "BROWSER_USE_BASE_URL (base URL — defaults to NVIDIA NIM at "
            "https://integrate.api.nvidia.com/v1), and BROWSER_USE_MODEL (a "
            "tool-calling-capable model on that endpoint) in .env. Any "
            "OpenAI-compatible provider works here — NVIDIA NIM, OpenRouter, "
            "a self-hosted endpoint, or your own key against any other "
            "compatible API."
        )
    client = OpenAI(api_key=cfg.api_key, base_url=cfg.base_url, timeout=_TIMEOUT_S)
    return client, cfg.model


def call_with_tools(
    client: Any,
    model: str,
    messages: list[dict],
    tools: list[dict],
    *,
    tool_choice: str = "auto",
) -> Any:
    """One ``chat.completions.create`` round-trip with tool-calling enabled.

    Thin sync wrapper — called via ``asyncio.to_thread`` from
    ``AgenticOffsiteApplyFlow._agentic_guided_apply`` exactly as
    ``nim_client.classify_via_nim`` is already called from
    ``apply_jobs.JobAgent._classify_nim``. Returns
    ``response.choices[0].message`` (not the full response object) so the
    caller can hand it straight to its own message-dict conversion and append
    it to the running ``messages`` list for the next turn.
    """
    response = client.chat.completions.create(
        model=model,
        messages=messages,
        tools=tools,
        tool_choice=tool_choice,
    )
    return response.choices[0].message
