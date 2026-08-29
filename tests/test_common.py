"""Unit tests for ``common.py`` — the shared stdlib-only helpers extracted in T4.

Covers ``strip_code_fence`` (plain / ```json / bare ``` / surrounding whitespace /
no fence) and ``extract_json_object`` (clean / prose-wrapped / nested braces /
malformed / absent). ``write_llm_log`` is exercised for its append + error-swallow
contract.
"""

import json

import common

# ── strip_code_fence ──────────────────────────────────────────────────────────

def test_strip_code_fence_plain_passthrough():
    assert common.strip_code_fence('{"a": 1}') == '{"a": 1}'


def test_strip_code_fence_json_language_tag():
    raw = '```json\n{"a": 1}\n```'
    assert common.strip_code_fence(raw) == '{"a": 1}'


def test_strip_code_fence_bare_fence():
    raw = '```\n{"a": 1}\n```'
    assert common.strip_code_fence(raw) == '{"a": 1}'


def test_strip_code_fence_surrounding_whitespace():
    raw = '   \n```json\n{"a": 1}\n```   \n'
    assert common.strip_code_fence(raw) == '{"a": 1}'


def test_strip_code_fence_no_fence_but_whitespace_trimmed():
    assert common.strip_code_fence('  hello  ') == 'hello'


def test_strip_code_fence_mid_string_backticks_untouched():
    # Guard is on a leading fence only — inline backticks must survive.
    raw = 'note: use `page.locator` here'
    assert common.strip_code_fence(raw) == 'note: use `page.locator` here'


# ── extract_json_object ───────────────────────────────────────────────────────

def test_extract_json_object_clean():
    assert common.extract_json_object('{"a": 1}') == '{"a": 1}'


def test_extract_json_object_prose_wrapped():
    raw = 'Sure! Here is the result: {"relevant": true} — hope that helps.'
    assert common.extract_json_object(raw) == '{"relevant": true}'
    assert json.loads(common.extract_json_object(raw)) == {"relevant": True}


def test_extract_json_object_nested_braces():
    raw = 'prefix {"a": {"b": 2}} suffix'
    assert common.extract_json_object(raw) == '{"a": {"b": 2}}'


def test_extract_json_object_spans_to_last_brace_across_multiple_objects():
    # first '{' to last '}' — deliberately greedy; caller handles multi-object salvage.
    raw = '{"a": 1} {"b": 2}'
    assert common.extract_json_object(raw) == '{"a": 1} {"b": 2}'


def test_extract_json_object_no_braces_returns_input_unchanged():
    assert common.extract_json_object('no json here') == 'no json here'


def test_extract_json_object_only_open_brace_returns_input_unchanged():
    assert common.extract_json_object('oops {"a": 1') == 'oops {"a": 1'


# ── write_llm_log ─────────────────────────────────────────────────────────────

def test_write_llm_log_appends_jsonl(tmp_path, monkeypatch):
    log = tmp_path / "llm_debug.jsonl"
    monkeypatch.setattr(common, "LLM_LOG_PATH", str(log))
    common.write_llm_log({"type": "a"})
    common.write_llm_log({"type": "b"})
    lines = log.read_text().splitlines()
    assert [json.loads(x)["type"] for x in lines] == ["a", "b"]


def test_write_llm_log_swallows_unserializable_entry(tmp_path, monkeypatch):
    log = tmp_path / "llm_debug.jsonl"
    monkeypatch.setattr(common, "LLM_LOG_PATH", str(log))
    # A set is not JSON-serializable — must not raise, and must not write a line.
    common.write_llm_log({"bad": {1, 2, 3}})
    assert not log.exists() or log.read_text() == ""


def test_write_llm_log_swallows_bad_path(monkeypatch):
    monkeypatch.setattr(common, "LLM_LOG_PATH", "/nonexistent-dir/nope/llm.jsonl")
    common.write_llm_log({"type": "x"})  # must not raise
