"""llm.py — Claude Agent SDK helpers (ticket T14).

Subscription auth only: the Agent SDK talks to Claude through the machine's
``claude`` CLI login. There is **no** ``ANTHROPIC_API_KEY`` and no
OpenAI-compatible endpoint for this path (see
``config.get_llm_config("guided_apply")`` — ``api_key`` / ``base_url`` are
``None`` by design).

Two entry points, deliberately different tools for different jobs:

* :func:`query_json` — a **fresh, fully isolated session per call**, built on the
  Agent SDK's one-shot ``query()`` function. Used by ``apply_jobs.JobAgent`` to
  classify each job posting independently. See the docstring for why this is
  *not* a persistent client.

* :class:`ClaudeSession` — **one persistent ``ClaudeSDKClient``** (hence one
  ``claude`` subprocess) reused across calls that are meant to share
  conversation history: the offsite browser-reasoning agent in T14b, which
  drives a single multi-step form-fill as one conversation. Not for
  independent per-item calls (see #560 note below).

The ``claude_agent_sdk`` package is imported **lazily** inside each entry point
so that ``import llm`` (hence ``import apply_jobs``) succeeds where the SDK is
absent — ``apply_jobs.py --stats``, the test suite, etc. A call actually
attempted without the package raises :class:`ClaudeAgentSDKError`.

Structured output
-----------------
``ClaudeAgentOptions(output_format={"type": "json_schema", "schema": ...})`` →
``ResultMessage.structured_output``. If that field is absent / not a dict, the
reply text is parsed with ``json.loads`` then ``common.extract_json_object``
salvage; a non-JSON reply raises :class:`ClaudeAgentSDKError`.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from typing import Any

from common import extract_json_object, write_llm_log
from config import get_llm_config

_SDK_MISSING_HINT = (
    "claude-agent-sdk is not installed / importable. Install it "
    "(`pip install claude-agent-sdk`) and run `claude` once to authenticate — "
    "this path uses subscription auth, not an API key."
)

# NOTE (issue #560, https://github.com/anthropics/claude-agent-sdk-python/issues/560,
# OPEN as of v0.2.x): with a persistent ClaudeSDKClient, passing a fresh
# session_id per query() call does NOT isolate context — earlier prompts stay
# visible to later calls. Anything that needs per-item isolation (the job
# classifier) must use the one-shot query() function, which starts a brand-new
# session each call. See query_json below.

_JSON_INSTRUCTION = (
    "\n\nReturn ONLY a single JSON object matching this schema, with no "
    "surrounding prose or code fence:\n"
)


class ClaudeAgentSDKError(RuntimeError):
    """Any failure originating from the Claude Agent SDK: a missing package, a
    transport/protocol error, or an error result message. Callers catch this
    single type instead of the SDK's internal exception hierarchy."""


# ── shared message-stream handling ───────────────────────────────────────────

async def _consume(message_iter: Any) -> tuple[str, Any, str | None]:
    """Drain an Agent SDK message stream.

    Returns ``(text, structured_dict_or_None, error_detail_or_None)``.
    """
    text_parts: list[str] = []
    result_text: str | None = None
    structured: Any = None
    err: str | None = None

    async for msg in message_iter:
        name = type(msg).__name__
        if name == "AssistantMessage":
            for block in getattr(msg, "content", None) or []:
                if type(block).__name__ == "TextBlock":
                    text_parts.append(getattr(block, "text", "") or "")
        elif name == "ResultMessage":
            result_text = getattr(msg, "result", None)
            # Native structured output has moved names across SDK versions.
            for attr in ("structured_output", "structured_result", "output"):
                val = getattr(msg, attr, None)
                if isinstance(val, dict):
                    structured = val
                    break
            else:
                meta = getattr(msg, "metadata", None) or {}
                if isinstance(meta, dict) and isinstance(
                    meta.get("structuredOutput"), dict
                ):
                    structured = meta["structuredOutput"]
            if (bool(getattr(msg, "is_error", False))
                    or str(getattr(msg, "subtype", "") or "").startswith("error")):
                detail = (getattr(msg, "errors", None)
                          or getattr(msg, "api_error_status", None))
                err = " ".join(
                    str(p) for p in (result_text, detail) if p
                ).strip() or "unknown Agent SDK error"

    text = (result_text if result_text is not None else "".join(text_parts)).strip()
    return text, structured, err


def _parse_json_object(text: str) -> dict:
    """``json.loads`` with a first→last-brace salvage fallback."""
    for candidate in (text, extract_json_object(text)):
        try:
            parsed = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    raise ClaudeAgentSDKError(
        f"Agent SDK returned output that is not a JSON object: {text[:200]!r}"
    )


def _import_sdk() -> Any:
    try:
        import claude_agent_sdk as sdk
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise ClaudeAgentSDKError(_SDK_MISSING_HINT) from exc
    return sdk


# ── one-shot isolated call (the classifier) ──────────────────────────────────

async def query_json(
    prompt: str,
    schema: dict,
    *,
    model: str,
    system: str | None = None,
    log_type: str = "classifier",
    log_calls: bool = False,
) -> dict:
    """One structured Agent SDK call in a **fresh, isolated session**.

    Built on the module-level ``query()`` function rather than
    ``ClaudeSDKClient``: every call starts a brand-new ``claude`` session, so no
    prompt from a previous call is visible to this one. That is a hard
    requirement for the job classifier — each posting must be judged on its own.

    Do **not** "optimize" this onto :class:`ClaudeSession`. A fresh ``session_id``
    on one persistent client does not isolate context (issue #560:
    https://github.com/anthropics/claude-agent-sdk-python/issues/560). The
    per-call subprocess spawn (~300 ms) is negligible next to the model
    round-trip, and the classifier already runs sequentially with a 1 s sleep
    between jobs.
    """
    sdk = _import_sdk()
    full_prompt = prompt + _JSON_INSTRUCTION + json.dumps(schema)

    t0 = time.monotonic()
    try:
        options = sdk.ClaudeAgentOptions(
            model=model,
            system_prompt=system,
            allowed_tools=[],
            max_turns=1,
            setting_sources=[],
            # "dontAsk": deny anything not in an allow rule (allowed_tools=[] ⇒
            # everything) without prompting. NOT "bypassPermissions" — the
            # classifier ingests attacker-controlled job text, and max_turns=1
            # still permits one tool round-trip, so the gate must stay shut.
            permission_mode="dontAsk",
            output_format={"type": "json_schema", "schema": schema},
        )
        text, structured, err = await _consume(
            sdk.query(prompt=full_prompt, options=options)
        )
    except ClaudeAgentSDKError:
        raise
    except TypeError as exc:
        # A kwarg the installed SDK doesn't accept — pin mismatch. Fail loudly
        # rather than silently dropping output_format and losing all structure.
        raise ClaudeAgentSDKError(
            f"ClaudeAgentOptions rejected a kwarg ({exc}); check the "
            f"claude-agent-sdk version pin in pyproject.toml"
        ) from exc
    except Exception as exc:
        raise ClaudeAgentSDKError(f"Agent SDK query failed: {exc}") from exc
    duration_ms = int((time.monotonic() - t0) * 1000)

    if err is not None:
        raise ClaudeAgentSDKError(f"Agent SDK returned an error result: {err}")

    data = structured if isinstance(structured, dict) else _parse_json_object(text)

    if log_calls:
        write_llm_log({
            "ts": datetime.now(UTC).isoformat(),
            "type": log_type,
            "model": model,
            "duration_ms": duration_ms,
            "result": data,
        })
    return data


# ── persistent session (T14b browser agent) ─────────────────────────────────

class ClaudeSession:
    """A persistent Claude Agent SDK conversation.

    One ``ClaudeSDKClient`` (hence one ``claude`` subprocess) is created on the
    first call and reused for every subsequent :meth:`ask` / :meth:`ask_json`.
    **All calls on a session share conversation history** — that is the point:
    this is for a single multi-step task driven as one conversation (the T14b
    offsite form-filling agent).

    It is therefore the wrong tool for independent per-item work such as job
    classification: a fresh ``session_id`` per call would *not* give you
    isolation (issue #560). Use :func:`query_json` / the one-shot ``query()``
    for that.

    One model per session. ``ClaudeSDKClient`` exposes ``set_model()`` and
    :meth:`ask` uses it best-effort when ``model=`` differs, but a caller that
    needs two models should hold two sessions.
    """

    def __init__(
        self,
        *,
        model: str | None = None,
        system: str | None = None,
        output_schema: dict | None = None,
        log_type: str = "agent",
        log_calls: bool = True,
    ) -> None:
        self._model = model or get_llm_config("guided_apply").model
        self._system = system
        self._output_schema = output_schema
        self._log_type = log_type
        self._log_calls = log_calls
        self._client: Any = None
        self._sdk: Any = None
        self._current_model = self._model

    async def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        sdk = _import_sdk()
        self._sdk = sdk
        opts: dict[str, Any] = {
            "model": self._model,
            "system_prompt": self._system,
            "allowed_tools": [],
            "setting_sources": [],
            # deny (not bypass) — see query_json. No-hang without opening the gate.
            "permission_mode": "dontAsk",
        }
        if self._output_schema is not None:
            opts["output_format"] = {
                "type": "json_schema", "schema": self._output_schema,
            }
        try:
            client = sdk.ClaudeSDKClient(options=sdk.ClaudeAgentOptions(**opts))
            await client.connect()
        except ClaudeAgentSDKError:
            raise
        except Exception as exc:
            raise ClaudeAgentSDKError(
                f"failed to start Claude Agent SDK session: {exc}"
            ) from exc
        self._client = client
        return client

    async def aclose(self) -> None:
        """Disconnect the client / kill the ``claude`` subprocess. Best-effort
        and idempotent — safe to call from a ``finally`` even if never used."""
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:
                pass
            self._client = None

    async def ask(self, prompt: str, *, system: str | None = None,
                  model: str | None = None) -> str:
        """Send one prompt, return the assistant's text reply."""
        text, _ = await self._run(prompt, system=system, model=model, schema=None)
        return text

    async def ask_json(self, prompt: str, schema: dict, *,
                       system: str | None = None,
                       model: str | None = None) -> dict:
        """Send one prompt and return a parsed JSON object."""
        text, structured = await self._run(
            prompt, system=system, model=model, schema=schema
        )
        return structured if isinstance(structured, dict) else _parse_json_object(text)

    async def _run(self, prompt: str, *, system: str | None,
                   model: str | None, schema: dict | None) -> tuple[str, Any]:
        client = await self._ensure_client()

        want_model = model or self._model
        if want_model != self._current_model:
            try:
                await client.set_model(want_model)
                self._current_model = want_model
            except Exception:
                pass  # best-effort; session default still applies

        full_prompt = prompt if system is None else f"{system}\n\n{prompt}"
        if schema is not None:
            full_prompt += _JSON_INSTRUCTION + json.dumps(schema)

        t0 = time.monotonic()
        try:
            await client.query(full_prompt)
            text, structured, err = await _consume(client.receive_response())
        except ClaudeAgentSDKError:
            raise
        except Exception as exc:
            raise ClaudeAgentSDKError(f"Agent SDK call failed: {exc}") from exc
        duration_ms = int((time.monotonic() - t0) * 1000)

        if err is not None:
            raise ClaudeAgentSDKError(f"Agent SDK returned an error result: {err}")

        if self._log_calls:
            write_llm_log({
                "ts": datetime.now(UTC).isoformat(),
                "type": self._log_type,
                "model": self._current_model,
                "duration_ms": duration_ms,
                "result": structured if isinstance(structured, dict) else text,
            })
        return text, structured
