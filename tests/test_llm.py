"""Unit tests for ``llm.py`` — the Claude Agent SDK helpers (T14).

The real ``claude_agent_sdk`` package is never imported: every test injects a
fake module into ``sys.modules`` before the lazy import inside ``llm``. No
network, no ``claude`` CLI.

Two entry points under test:
  * ``llm.query_json`` — one-shot, isolated-per-call (the classifier). Mocks
    ``claude_agent_sdk.query`` (the module-level function), NOT a persistent
    client — see issue #560.
  * ``llm.ClaudeSession`` — persistent client (T14b browser agent). Mocks
    ``ClaudeSDKClient``.
"""

from __future__ import annotations

import asyncio
import sys
import types

import pytest

import llm


def _make_fake_sdk(*, result=None, structured=None, is_error=False, subtype=None,
                   errors=None, text_blocks=None, connect_exc=None, query_exc=None):
    """Build a stand-in ``claude_agent_sdk`` module.

    ``mod._query_calls`` / ``mod.ClaudeSDKClient.created`` let tests inspect what
    happened.
    """
    mod = types.ModuleType("claude_agent_sdk")
    mod._query_calls = []

    class ClaudeAgentOptions:
        def __init__(self, **kw):
            self.kw = kw

    class TextBlock:
        def __init__(self, text):
            self.text = text

    class AssistantMessage:
        def __init__(self, content):
            self.content = content

    class ResultMessage:
        def __init__(self):
            self.result = result
            self.structured_output = structured
            self.is_error = is_error
            self.subtype = subtype
            self.errors = errors

    def _messages():
        for blk in (text_blocks or []):
            yield AssistantMessage([TextBlock(blk)])
        yield ResultMessage()

    async def query(*, prompt, options=None):
        mod._query_calls.append(prompt)
        if query_exc is not None:
            raise query_exc
        for msg in _messages():
            yield msg

    class ClaudeSDKClient:
        created: list = []

        def __init__(self, options=None):
            self.options = options
            self.connect_calls = 0
            self.disconnect_calls = 0
            self.queries: list = []
            ClaudeSDKClient.created.append(self)

        async def connect(self):
            self.connect_calls += 1
            if connect_exc is not None:
                raise connect_exc

        async def disconnect(self):
            self.disconnect_calls += 1

        async def set_model(self, model):
            pass

        async def query(self, prompt):
            self.queries.append(prompt)

        async def receive_response(self):
            for msg in _messages():
                yield msg

    mod.ClaudeAgentOptions = ClaudeAgentOptions
    mod.ClaudeSDKClient = ClaudeSDKClient
    mod.AssistantMessage = AssistantMessage
    mod.TextBlock = TextBlock
    mod.ResultMessage = ResultMessage
    mod.query = query
    return mod


def _install(monkeypatch, fake):
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake)


# ── query_json (classifier path) ─────────────────────────────────────────────

def test_query_json_returns_native_structured_output(monkeypatch):
    fake = _make_fake_sdk(structured={"relevant": True, "reason": "x",
                                      "citizenship_required": False})
    _install(monkeypatch, fake)
    out = asyncio.run(llm.query_json("classify", {"type": "object"},
                                     model="claude-haiku-4-5"))
    assert out == {"relevant": True, "reason": "x", "citizenship_required": False}


def test_query_json_salvages_fenced_text(monkeypatch):
    fake = _make_fake_sdk(result='```json\n{"a": 1, "b": 2}\n```', structured=None)
    _install(monkeypatch, fake)
    out = asyncio.run(llm.query_json("q", {"type": "object"}, model="m"))
    assert out == {"a": 1, "b": 2}


def test_query_json_non_json_raises(monkeypatch):
    fake = _make_fake_sdk(result="I cannot help with that", structured=None)
    _install(monkeypatch, fake)
    with pytest.raises(llm.ClaudeAgentSDKError):
        asyncio.run(llm.query_json("q", {"type": "object"}, model="m"))


def test_query_json_error_result_raises_with_detail(monkeypatch):
    fake = _make_fake_sdk(result="boom", is_error=True, subtype="error",
                          errors=["rate_limit"])
    _install(monkeypatch, fake)
    with pytest.raises(llm.ClaudeAgentSDKError) as excinfo:
        asyncio.run(llm.query_json("q", {"type": "object"}, model="m"))
    assert "rate_limit" in str(excinfo.value)


def test_query_json_subtype_error_without_is_error_flag(monkeypatch):
    fake = _make_fake_sdk(result="nope", is_error=False, subtype="error")
    _install(monkeypatch, fake)
    with pytest.raises(llm.ClaudeAgentSDKError):
        asyncio.run(llm.query_json("q", {"type": "object"}, model="m"))


def test_query_json_wraps_transport_exception(monkeypatch):
    fake = _make_fake_sdk(query_exc=RuntimeError("connection reset"))
    _install(monkeypatch, fake)
    with pytest.raises(llm.ClaudeAgentSDKError):
        asyncio.run(llm.query_json("q", {"type": "object"}, model="m"))


def test_query_json_missing_sdk_package(monkeypatch):
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)
    with pytest.raises(llm.ClaudeAgentSDKError) as excinfo:
        asyncio.run(llm.query_json("q", {"type": "object"}, model="m"))
    assert "claude-agent-sdk" in str(excinfo.value)


def test_query_json_logs_when_enabled(monkeypatch):
    fake = _make_fake_sdk(structured={"relevant": False})
    _install(monkeypatch, fake)
    logged: list = []
    monkeypatch.setattr(llm, "write_llm_log", logged.append)
    asyncio.run(llm.query_json("q", {"type": "object"}, model="claude-haiku-4-5",
                               log_type="classifier", log_calls=True))
    assert len(logged) == 1
    assert logged[0]["type"] == "classifier"
    assert logged[0]["model"] == "claude-haiku-4-5"
    assert "duration_ms" in logged[0]


def test_query_json_no_log_by_default(monkeypatch):
    fake = _make_fake_sdk(structured={"ok": True})
    _install(monkeypatch, fake)
    logged: list = []
    monkeypatch.setattr(llm, "write_llm_log", logged.append)
    asyncio.run(llm.query_json("q", {"type": "object"}, model="m"))
    assert logged == []


def test_query_json_each_call_is_a_fresh_query(monkeypatch):
    """Isolation intent: two classifications go through two independent query()
    invocations — nothing from call 1 is threaded into call 2."""
    fake = _make_fake_sdk(structured={"relevant": True})
    _install(monkeypatch, fake)

    async def _two():
        await llm.query_json("Title: ALPHA_ONE\nrate it", {"type": "object"}, model="m")
        await llm.query_json("Title: BETA_TWO\nrate it", {"type": "object"}, model="m")

    asyncio.run(_two())
    assert len(fake._query_calls) == 2
    assert "ALPHA_ONE" in fake._query_calls[0]
    assert "ALPHA_ONE" not in fake._query_calls[1]  # no leakage between calls
    assert "BETA_TWO" in fake._query_calls[1]
    # one-shot query(), never the persistent client
    assert fake.ClaudeSDKClient.created == []


def test_query_json_passes_output_format_and_lockdown_options(monkeypatch):
    captured: dict = {}
    fake = _make_fake_sdk(structured={"ok": True})

    _orig = fake.ClaudeAgentOptions

    def _spy(**kw):
        captured.update(kw)
        return _orig(**kw)

    fake.ClaudeAgentOptions = _spy
    _install(monkeypatch, fake)
    asyncio.run(llm.query_json("q", {"type": "object", "x": 1}, model="m",
                               system="sys"))
    assert captured["output_format"] == {"type": "json_schema",
                                         "schema": {"type": "object", "x": 1}}
    assert captured["allowed_tools"] == []
    # structured output needs a reasoning turn + an emit turn; max_turns=1
    # hard-failed ~15-40% of real classifications. Tools stay unavailable via
    # allowed_tools=[] regardless of turn count.
    assert captured["max_turns"] == llm._CLASSIFIER_MAX_TURNS
    assert llm._CLASSIFIER_MAX_TURNS >= 2
    assert captured["model"] == "m"
    assert captured["system_prompt"] == "sys"
    assert captured["setting_sources"] == []
    # deny, never bypass — the classifier reads attacker-controlled job text
    assert captured["permission_mode"] == "dontAsk"
    assert captured["permission_mode"] != "bypassPermissions"


# ── ClaudeSession (persistent — T14b) ────────────────────────────────────────

def test_session_reuses_one_client_across_calls(monkeypatch):
    fake = _make_fake_sdk(result="ok")
    _install(monkeypatch, fake)
    session = llm.ClaudeSession(model="claude-sonnet-5", log_calls=False)

    async def _twice():
        await session.ask("one")
        await session.ask("two")

    asyncio.run(_twice())
    assert len(fake.ClaudeSDKClient.created) == 1
    client = fake.ClaudeSDKClient.created[0]
    assert client.connect_calls == 1
    assert client.queries == ["one", "two"]


def test_session_ask_json_structured(monkeypatch):
    fake = _make_fake_sdk(structured={"k": "v"})
    _install(monkeypatch, fake)
    session = llm.ClaudeSession(model="claude-sonnet-5", log_calls=False)
    assert asyncio.run(session.ask_json("q", {"type": "object"})) == {"k": "v"}


def test_session_connect_failure_wrapped(monkeypatch):
    fake = _make_fake_sdk(connect_exc=RuntimeError("transport boom"))
    _install(monkeypatch, fake)
    session = llm.ClaudeSession(model="claude-sonnet-5", log_calls=False)
    with pytest.raises(llm.ClaudeAgentSDKError):
        asyncio.run(session.ask("hi"))


def test_session_error_result_wrapped(monkeypatch):
    fake = _make_fake_sdk(result="quota exceeded", is_error=True)
    _install(monkeypatch, fake)
    session = llm.ClaudeSession(model="claude-sonnet-5", log_calls=False)
    with pytest.raises(llm.ClaudeAgentSDKError):
        asyncio.run(session.ask("hi"))


def test_session_missing_sdk_package(monkeypatch):
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)
    session = llm.ClaudeSession(model="claude-sonnet-5", log_calls=False)
    with pytest.raises(llm.ClaudeAgentSDKError) as excinfo:
        asyncio.run(session.ask("hi"))
    assert "claude-agent-sdk" in str(excinfo.value)


def test_session_logs_when_enabled(monkeypatch):
    fake = _make_fake_sdk(result="hi", structured={"k": "v"})
    _install(monkeypatch, fake)
    logged: list = []
    monkeypatch.setattr(llm, "write_llm_log", logged.append)
    session = llm.ClaudeSession(model="claude-sonnet-5", log_type="agent")
    asyncio.run(session.ask("hi"))
    assert len(logged) == 1
    assert logged[0]["type"] == "agent"
    assert logged[0]["result"] == {"k": "v"}


def test_session_aclose_safe_before_use(monkeypatch):
    fake = _make_fake_sdk(result="hi")
    _install(monkeypatch, fake)
    session = llm.ClaudeSession(model="claude-sonnet-5", log_calls=False)
    asyncio.run(session.aclose())
    assert fake.ClaudeSDKClient.created == []


def test_session_reuses_one_client_across_many_calls(monkeypatch):
    """T14b browser agent fires many prompts through one session/subprocess."""
    fake = _make_fake_sdk(result="ok")
    _install(monkeypatch, fake)
    session = llm.ClaudeSession(model="claude-sonnet-5", log_calls=False)

    async def _many():
        for i in range(5):
            await session.ask(f"q{i}")

    asyncio.run(_many())
    assert len(fake.ClaudeSDKClient.created) == 1
    assert fake.ClaudeSDKClient.created[0].queries == [f"q{i}" for i in range(5)]


def test_session_aclose_idempotent_after_use(monkeypatch):
    """Two aclose() calls after use disconnect the subprocess exactly once —
    flows call it from a ``finally`` that can run after run() already closed."""
    fake = _make_fake_sdk(result="hi")
    _install(monkeypatch, fake)
    session = llm.ClaudeSession(model="claude-sonnet-5", log_calls=False)

    async def _go():
        await session.ask("one")
        await session.aclose()
        await session.aclose()

    asyncio.run(_go())
    assert fake.ClaudeSDKClient.created[0].disconnect_calls == 1
