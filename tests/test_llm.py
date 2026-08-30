"""Unit tests for ``llm.ClaudeSession`` — the persistent Claude Agent SDK
wrapper introduced in T14 (part 1).

The real ``claude_agent_sdk`` package is never imported: every test injects a
fake module into ``sys.modules`` before the lazy ``import claude_agent_sdk``
inside ``ClaudeSession._ensure_client`` runs. No network, no ``claude`` CLI.

Covered:
  * native structured output (``ResultMessage.structured_output``) is returned as-is
  * text fallback: no structured output → parse the reply with extract_json_object
  * one client / one connect() across multiple calls (the whole point of the wrapper)
  * SDK errors (connect failure, error result) are wrapped in ClaudeAgentSDKError
  * a missing ``claude_agent_sdk`` package raises a clean ClaudeAgentSDKError
  * every call is logged via common.write_llm_log when log_calls=True
"""

from __future__ import annotations

import asyncio
import sys
import types

import pytest

import llm


def _make_fake_sdk(*, result=None, structured=None, is_error=False,
                   text_blocks=None, connect_exc=None):
    """Build a stand-in ``claude_agent_sdk`` module.

    ``receive_response`` yields an ``AssistantMessage`` per entry in
    ``text_blocks`` and then a single ``ResultMessage``.
    """
    mod = types.ModuleType("claude_agent_sdk")

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
        def __init__(self, result=None, structured_output=None, is_error=False):
            self.result = result
            self.structured_output = structured_output
            self.is_error = is_error

    class ClaudeSDKClient:
        created: list = []

        def __init__(self, options=None):
            self.options = options
            self.connect_calls = 0
            self.disconnect_calls = 0
            self.queries: list = []
            self.models: list = []
            ClaudeSDKClient.created.append(self)

        async def connect(self):
            self.connect_calls += 1
            if connect_exc is not None:
                raise connect_exc

        async def disconnect(self):
            self.disconnect_calls += 1

        async def set_model(self, model):
            self.models.append(model)

        async def query(self, prompt, session_id="default"):
            self.queries.append((prompt, session_id))

        async def receive_response(self):
            for blk in (text_blocks or []):
                yield AssistantMessage([TextBlock(blk)])
            yield ResultMessage(result=result, structured_output=structured,
                                is_error=is_error)

    mod.ClaudeAgentOptions = ClaudeAgentOptions
    mod.ClaudeSDKClient = ClaudeSDKClient
    mod.AssistantMessage = AssistantMessage
    mod.TextBlock = TextBlock
    mod.ResultMessage = ResultMessage
    return mod


def _install(monkeypatch, fake):
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake)


def test_ask_json_returns_native_structured_output(monkeypatch):
    fake = _make_fake_sdk(
        structured={"relevant": True, "reason": "x", "citizenship_required": False}
    )
    _install(monkeypatch, fake)
    session = llm.ClaudeSession(model="claude-haiku-4-5", log_calls=False)
    out = asyncio.run(session.ask_json("classify this", {"type": "object"}))
    assert out == {"relevant": True, "reason": "x", "citizenship_required": False}


def test_ask_json_falls_back_to_text_parse(monkeypatch):
    fake = _make_fake_sdk(result='```json\n{"a": 1, "b": 2}\n```', structured=None)
    _install(monkeypatch, fake)
    session = llm.ClaudeSession(model="claude-haiku-4-5", log_calls=False)
    out = asyncio.run(session.ask_json("q", {"type": "object"}))
    assert out == {"a": 1, "b": 2}


def test_ask_json_non_json_reply_raises(monkeypatch):
    fake = _make_fake_sdk(result="I cannot help with that", structured=None)
    _install(monkeypatch, fake)
    session = llm.ClaudeSession(model="claude-haiku-4-5", log_calls=False)
    with pytest.raises(llm.ClaudeAgentSDKError):
        asyncio.run(session.ask_json("q", {"type": "object"}))


def test_ask_returns_plain_text(monkeypatch):
    fake = _make_fake_sdk(result="hello world")
    _install(monkeypatch, fake)
    session = llm.ClaudeSession(model="claude-haiku-4-5", log_calls=False)
    assert asyncio.run(session.ask("hi")) == "hello world"


def test_session_reuses_one_client_across_calls(monkeypatch):
    fake = _make_fake_sdk(result="ok")
    _install(monkeypatch, fake)
    session = llm.ClaudeSession(model="claude-haiku-4-5", log_calls=False)

    async def _twice():
        await session.ask("one")
        await session.ask("two")

    asyncio.run(_twice())
    assert len(fake.ClaudeSDKClient.created) == 1
    client = fake.ClaudeSDKClient.created[0]
    assert client.connect_calls == 1
    assert len(client.queries) == 2
    # distinct session_ids so calls don't share conversation history
    assert client.queries[0][1] != client.queries[1][1]


def test_connect_failure_is_wrapped(monkeypatch):
    fake = _make_fake_sdk(connect_exc=RuntimeError("transport boom"))
    _install(monkeypatch, fake)
    session = llm.ClaudeSession(model="claude-haiku-4-5", log_calls=False)
    with pytest.raises(llm.ClaudeAgentSDKError):
        asyncio.run(session.ask("hi"))


def test_error_result_is_wrapped(monkeypatch):
    fake = _make_fake_sdk(result="quota exceeded", is_error=True)
    _install(monkeypatch, fake)
    session = llm.ClaudeSession(model="claude-haiku-4-5", log_calls=False)
    with pytest.raises(llm.ClaudeAgentSDKError):
        asyncio.run(session.ask("hi"))


def test_missing_sdk_package_raises_clean_error(monkeypatch):
    # sys.modules[name] = None makes `import name` raise ImportError.
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)
    session = llm.ClaudeSession(model="claude-haiku-4-5", log_calls=False)
    with pytest.raises(llm.ClaudeAgentSDKError) as excinfo:
        asyncio.run(session.ask("hi"))
    assert "claude-agent-sdk" in str(excinfo.value)


def test_calls_are_logged_when_enabled(monkeypatch):
    fake = _make_fake_sdk(result="hi", structured={"k": "v"})
    _install(monkeypatch, fake)
    logged: list = []
    monkeypatch.setattr(llm, "write_llm_log", logged.append)
    session = llm.ClaudeSession(model="claude-haiku-4-5", log_type="classifier")
    asyncio.run(session.ask("hi"))
    assert len(logged) == 1
    entry = logged[0]
    assert entry["type"] == "classifier"
    assert entry["model"] == "claude-haiku-4-5"
    assert "duration_ms" in entry
    assert entry["result"] == {"k": "v"}


def test_calls_not_logged_when_disabled(monkeypatch):
    fake = _make_fake_sdk(result="hi")
    _install(monkeypatch, fake)
    logged: list = []
    monkeypatch.setattr(llm, "write_llm_log", logged.append)
    session = llm.ClaudeSession(model="claude-haiku-4-5", log_calls=False)
    asyncio.run(session.ask("hi"))
    assert logged == []


def test_aclose_is_safe_before_use(monkeypatch):
    fake = _make_fake_sdk(result="hi")
    _install(monkeypatch, fake)
    session = llm.ClaudeSession(model="claude-haiku-4-5", log_calls=False)
    asyncio.run(session.aclose())  # never connected — must not raise
    assert fake.ClaudeSDKClient.created == []
