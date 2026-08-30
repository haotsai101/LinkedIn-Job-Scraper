"""llm.py — persistent Claude Agent SDK session wrapper (ticket T14).

Subscription auth only: the Agent SDK talks to Claude through the machine's
``claude`` CLI login. There is **no** ``ANTHROPIC_API_KEY`` and no
OpenAI-compatible endpoint for this path (see
``config.get_llm_config("guided_apply")`` — ``api_key`` / ``base_url`` are
``None`` by design).

Consumers:
  * ``apply_jobs.JobAgent`` — Easy Apply job-relevance classification (this PR,
    T14 part 1).
  * ``linkedin_apply`` / ``script_engine`` browser reasoning — **T14b**
    (``_call_claude`` still shells out to the ``claude`` CLI there; not migrated
    yet).

The ``claude_agent_sdk`` package is imported **lazily** inside
:class:`ClaudeSession` so that ``import llm`` (hence ``import apply_jobs``)
succeeds in environments where the SDK is absent — e.g. ``apply_jobs.py
--stats`` on a machine with only the OpenAI deps installed, or the test suite.

Structured output
-----------------
:meth:`ClaudeSession.ask_json` prefers the Agent SDK's native structured output
(``ClaudeAgentOptions(output_format={"type": "json_schema", "schema": ...})`` →
``ResultMessage.structured_output``). When the installed SDK predates
``output_format`` (the kwarg raises ``TypeError`` at options construction) it
falls back to instructing the schema in the prompt and parsing the reply with
``common.extract_json_object`` + ``json.loads``. Either way the caller gets a
``dict`` or a :class:`ClaudeAgentSDKError`.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from common import extract_json_object, write_llm_log
from config import get_llm_config

_SDK_MISSING_HINT = (
    "claude-agent-sdk is not installed / importable. Install it "
    "(`pip install claude-agent-sdk`) and run `claude` once to authenticate — "
    "this path uses subscription auth, not an API key."
)


class ClaudeAgentSDKError(RuntimeError):
    """Any failure originating from the Claude Agent SDK: a missing package, a
    transport/protocol error, or an error result message. Callers catch this
    single type instead of the SDK's internal exception hierarchy."""


def _build_options(sdk: Any, model: str, system: str | None,
                   output_schema: dict | None) -> Any:
    """Construct ``ClaudeAgentOptions`` defensively.

    Every field beyond ``model`` is optional across SDK versions, so each is
    added one at a time and silently dropped if that version's dataclass does
    not accept it. This keeps the wrapper working against a range of
    ``claude-agent-sdk`` releases without a hard version pin.
    """
    kwargs: dict[str, Any] = {"model": model}
    optional: dict[str, Any] = {}
    if system is not None:
        optional["system_prompt"] = system
    # Lock the agent down to a single plain-text turn — no filesystem/bash tools,
    # no project settings. A classifier must not touch the machine.
    optional["allowed_tools"] = []
    optional["max_turns"] = 1
    optional["setting_sources"] = []
    if output_schema is not None:
        optional["output_format"] = {"type": "json_schema", "schema": output_schema}

    for key, val in optional.items():
        try:
            sdk.ClaudeAgentOptions(**kwargs, **{key: val})
        except TypeError:
            continue
        kwargs[key] = val
    return sdk.ClaudeAgentOptions(**kwargs)


class ClaudeSession:
    """A reusable Claude Agent SDK conversation.

    One ``ClaudeSDKClient`` (hence one ``claude`` subprocess) is created on the
    first call and reused for every subsequent :meth:`ask` / :meth:`ask_json`;
    the point is to avoid paying process-spawn latency per call.

    Each :meth:`ask` runs under a fresh ``session_id`` so calls do **not** share
    conversation history with one another — a classifier treats every job
    independently. Only the transport/subprocess is shared.

    One model per session. ``ClaudeSDKClient`` supports ``set_model()``
    mid-session, and ``ask(model=...)`` uses it best-effort when the value
    differs from the session default, but a caller that genuinely needs two
    models should hold two sessions.
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

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            import claude_agent_sdk as sdk
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise ClaudeAgentSDKError(_SDK_MISSING_HINT) from exc

        self._sdk = sdk
        try:
            options = _build_options(sdk, self._model, self._system, self._output_schema)
            client = sdk.ClaudeSDKClient(options=options)
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
        """Disconnect the underlying client / kill the ``claude`` subprocess.

        Best-effort and idempotent — safe to call from a ``finally`` block even
        if the session was never used.
        """
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:
                pass
            self._client = None

    # ── calls ────────────────────────────────────────────────────────────────

    async def ask(self, prompt: str, *, system: str | None = None,
                  model: str | None = None) -> str:
        """Send one prompt, return the assistant's text reply."""
        text, _ = await self._run(prompt, system=system, model=model, schema=None)
        return text

    async def ask_json(self, prompt: str, schema: dict, *,
                       system: str | None = None,
                       model: str | None = None) -> dict:
        """Send one prompt and return a parsed JSON object.

        Prefers the SDK's native structured output; falls back to
        prompt-instructed JSON parsed with :func:`common.extract_json_object`.
        Raises :class:`ClaudeAgentSDKError` if the reply is not a JSON object.
        """
        text, structured = await self._run(
            prompt, system=system, model=model, schema=schema
        )
        if isinstance(structured, dict):
            return structured
        try:
            return json.loads(extract_json_object(text))
        except (ValueError, TypeError) as exc:
            raise ClaudeAgentSDKError(
                f"Agent SDK returned non-JSON output: {text[:200]!r}"
            ) from exc

    async def _run(self, prompt: str, *, system: str | None,
                   model: str | None, schema: dict | None
                   ) -> tuple[str, Any]:
        client = await self._ensure_client()

        want_model = model or self._model
        if want_model != self._current_model:
            try:
                await client.set_model(want_model)
                self._current_model = want_model
            except Exception:
                pass  # best-effort; the session default still applies

        full_prompt = prompt if system is None else f"{system}\n\n{prompt}"
        if schema is not None:
            full_prompt += (
                "\n\nReturn ONLY a single JSON object matching this schema, "
                "with no surrounding prose or code fence:\n" + json.dumps(schema)
            )

        t0 = time.monotonic()
        try:
            try:
                await client.query(full_prompt, session_id=uuid.uuid4().hex)
            except TypeError:
                await client.query(full_prompt)

            text_parts: list[str] = []
            result_text: str | None = None
            structured: Any = None
            is_error = False
            async for msg in client.receive_response():
                name = type(msg).__name__
                if name == "AssistantMessage":
                    for block in getattr(msg, "content", None) or []:
                        if type(block).__name__ == "TextBlock":
                            text_parts.append(getattr(block, "text", "") or "")
                elif name == "ResultMessage":
                    result_text = getattr(msg, "result", None)
                    is_error = bool(getattr(msg, "is_error", False))
                    # Native structured output landed under a couple of names
                    # across SDK versions; fall back to text parsing if none hit.
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
        except ClaudeAgentSDKError:
            raise
        except Exception as exc:
            raise ClaudeAgentSDKError(f"Agent SDK call failed: {exc}") from exc

        duration_ms = int((time.monotonic() - t0) * 1000)
        text = (result_text if result_text is not None else "".join(text_parts)).strip()

        if is_error:
            raise ClaudeAgentSDKError(
                f"Agent SDK returned an error result: {text[:200]!r}"
            )

        if self._log_calls:
            write_llm_log({
                "ts": datetime.now(UTC).isoformat(),
                "type": self._log_type,
                "model": self._current_model,
                "duration_ms": duration_ms,
                "result": structured if isinstance(structured, dict) else text,
            })
        return text, structured
