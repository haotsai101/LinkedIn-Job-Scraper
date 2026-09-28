"""Unit tests for browser_use_client.py (T54).

Mirrors the mocking style of tests/test_classifier_routing.py, but exercises
browser_use_client directly rather than through apply_jobs.JobAgent — there is
no classifier-style routing decision here, just resolve + call.

No network, no real OpenAI client — ``resolve_browser_use`` is tested against
a real ``config.LLMConfig`` (no monkeypatching of the OpenAI SDK needed since
we never actually call out), and ``call_with_tools`` is tested against a fake
client double.
"""

from __future__ import annotations

import pytest

import browser_use_client
import config

# ── resolve_browser_use ──────────────────────────────────────────────────────

def test_resolve_browser_use_no_key_raises_config_error():
    cfg = config.LLMConfig(model="some-model", api_key=None, base_url="https://example.com/v1")
    with pytest.raises(browser_use_client.BrowserUseConfigError) as exc_info:
        browser_use_client.resolve_browser_use(cfg)
    msg = str(exc_info.value)
    # The error message IS the "bring your own key" documentation — assert the
    # exact env var names are named, per the ticket's design requirement.
    assert "BROWSER_USE_API" in msg
    assert "BROWSER_USE_BASE_URL" in msg
    assert "BROWSER_USE_MODEL" in msg


def test_resolve_browser_use_success_returns_client_and_model():
    cfg = config.LLMConfig(model="my-model", api_key="sk-test-123", base_url="https://example.com/v1")
    client, model = browser_use_client.resolve_browser_use(cfg)
    assert model == "my-model"
    # Real OpenAI() client object — just confirm it was constructed, not None.
    assert client is not None
    assert client.api_key == "sk-test-123"
    assert str(client.base_url).rstrip("/") == "https://example.com/v1"


def test_resolve_browser_use_defaults_to_get_llm_config(monkeypatch):
    """No cfg passed -> resolves via config.get_llm_config('browser_use')."""
    calls = []

    def fake_get_llm_config(role):
        calls.append(role)
        return config.LLMConfig(model="default-model", api_key="k", base_url="https://x/v1")

    monkeypatch.setattr(config, "get_llm_config", fake_get_llm_config)
    # browser_use_client imported get_llm_config by name, so patch its own reference too.
    monkeypatch.setattr(browser_use_client, "get_llm_config", fake_get_llm_config)

    client, model = browser_use_client.resolve_browser_use()
    assert calls == ["browser_use"]
    assert model == "default-model"


def test_resolve_browser_use_openai_not_installed(monkeypatch):
    monkeypatch.setattr(browser_use_client, "OpenAI", None)
    cfg = config.LLMConfig(model="m", api_key="k", base_url="https://x/v1")
    with pytest.raises(browser_use_client.BrowserUseConfigError, match="openai"):
        browser_use_client.resolve_browser_use(cfg)


def test_no_anthropic_import_in_browser_use_client():
    """The whole point of this module: provider-agnostic, no Claude/Anthropic
    import anywhere (prose in the docstring explaining that IS allowed to say
    the word — this checks actual imports, not the vocabulary)."""
    import inspect

    src = inspect.getsource(browser_use_client)
    assert "import anthropic" not in src
    assert "from anthropic" not in src
    assert "claude_agent_sdk" not in src
    assert "ClaudeSession" not in src


# ── call_with_tools ──────────────────────────────────────────────────────────

class _FakeMessage:
    def __init__(self, content=None, tool_calls=None):
        self.role = "assistant"
        self.content = content
        self.tool_calls = tool_calls


class _FakeChoice:
    def __init__(self, message):
        self.message = message


class _FakeResponse:
    def __init__(self, message):
        self.choices = [_FakeChoice(message)]


class _FakeCompletions:
    def __init__(self, message, calls):
        self._message = message
        self._calls = calls

    def create(self, **kwargs):
        self._calls.append(kwargs)
        return _FakeResponse(self._message)


class _FakeChat:
    def __init__(self, message, calls):
        self.completions = _FakeCompletions(message, calls)


class _FakeClient:
    def __init__(self, message, calls):
        self.chat = _FakeChat(message, calls)


def test_call_with_tools_passes_args_through_and_returns_message():
    calls = []
    fake_message = _FakeMessage(content="hello")
    client = _FakeClient(fake_message, calls)

    messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "go"}]
    tools = [{"type": "function", "function": {"name": "click", "parameters": {}}}]

    result = browser_use_client.call_with_tools(client, "my-model", messages, tools)

    assert result is fake_message
    assert len(calls) == 1
    kwargs = calls[0]
    assert kwargs["model"] == "my-model"
    assert kwargs["messages"] is messages
    assert kwargs["tools"] is tools
    assert kwargs["tool_choice"] == "auto"


def test_call_with_tools_respects_explicit_tool_choice():
    calls = []
    client = _FakeClient(_FakeMessage(), calls)
    browser_use_client.call_with_tools(
        client, "m", [], [], tool_choice="none",
    )
    assert calls[0]["tool_choice"] == "none"
