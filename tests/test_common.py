"""Unit tests for ``common.py`` — the shared stdlib-only helpers extracted in T4.

Covers ``strip_code_fence`` (plain / ```json / bare ``` / surrounding whitespace /
no fence) and ``extract_json_object`` (clean / prose-wrapped / nested braces /
malformed / absent). ``write_llm_log`` is exercised for its append + error-swallow
contract. The final block characterizes the *composed* parse pipeline exactly as
``OffsiteApplyFlow._decide_action`` and ``JobAgent.classify`` run it — this is the
safety net T14 will lean on when it replaces the salvage code.
"""

import json
import os

import pytest

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


# ── composed parse pipeline (characterization) ────────────────────────────────
# Mirrors OffsiteApplyFlow._decide_action / JobAgent.classify exactly:
#   strip_code_fence -> record first-'{' index -> extract_json_object
#   -> json.loads, with a "first object only" fallback on JSONDecodeError.
# NOTE: `start` deliberately indexes the *pre-extraction* string, matching the
# live call sites (see linkedin_apply.py `_decide_action`).

def _parse_like_decide_action(raw):
    clean = common.strip_code_fence(raw)
    start = clean.find("{")
    clean = common.extract_json_object(clean)
    first_end = clean.find("}", start) + 1
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        return json.loads(clean[start:first_end])


# ── rotate_llm_log ────────────────────────────────────────────────────────────

def test_rotate_llm_log_noop_when_absent(tmp_path, monkeypatch):
    log = tmp_path / "llm_debug.jsonl"
    monkeypatch.setattr(common, "LLM_LOG_PATH", str(log))
    common.rotate_llm_log(max_bytes=10)  # must not raise
    assert not log.exists()


def test_rotate_llm_log_noop_below_threshold(tmp_path, monkeypatch):
    log = tmp_path / "llm_debug.jsonl"
    log.write_text("x" * 50)
    monkeypatch.setattr(common, "LLM_LOG_PATH", str(log))
    common.rotate_llm_log(max_bytes=1000)
    assert log.exists()
    assert log.read_text() == "x" * 50
    assert not (tmp_path / "llm_debug.jsonl.1").exists()


def test_rotate_llm_log_rotates_above_threshold(tmp_path, monkeypatch):
    log = tmp_path / "llm_debug.jsonl"
    log.write_text("current" * 100)
    monkeypatch.setattr(common, "LLM_LOG_PATH", str(log))
    common.rotate_llm_log(max_bytes=10, keep=2)
    # Live log moved to .1; a fresh session will recreate the base path.
    assert not log.exists()
    assert (tmp_path / "llm_debug.jsonl.1").read_text() == "current" * 100


def test_rotate_llm_log_shifts_generations_and_drops_oldest(tmp_path, monkeypatch):
    log = tmp_path / "llm_debug.jsonl"
    log.write_text("gen0" * 100)
    (tmp_path / "llm_debug.jsonl.1").write_text("gen1")
    (tmp_path / "llm_debug.jsonl.2").write_text("gen2-oldest")
    monkeypatch.setattr(common, "LLM_LOG_PATH", str(log))
    common.rotate_llm_log(max_bytes=10, keep=2)
    assert not log.exists()
    assert (tmp_path / "llm_debug.jsonl.1").read_text() == "gen0" * 100
    assert (tmp_path / "llm_debug.jsonl.2").read_text() == "gen1"
    # Only `keep` generations survive — the previous .2 is gone, not shifted to .3.
    assert not (tmp_path / "llm_debug.jsonl.3").exists()


# ── prune_debug_screenshots ───────────────────────────────────────────────────

def test_prune_debug_screenshots_noop_when_dir_absent(tmp_path):
    common.prune_debug_screenshots(keep=5, screenshot_dir=str(tmp_path / "nope"))


def test_prune_debug_screenshots_noop_when_under_keep(tmp_path):
    for i in range(3):
        (tmp_path / f"s{i}.png").write_text("x")
    common.prune_debug_screenshots(keep=5, screenshot_dir=str(tmp_path))
    assert len(list(tmp_path.iterdir())) == 3


def test_prune_debug_screenshots_keeps_newest_n(tmp_path):
    # Create 10 files with strictly increasing mtimes.
    paths = []
    for i in range(10):
        p = tmp_path / f"s{i:02d}.png"
        p.write_text("x")
        os.utime(p, (1_000_000 + i * 10, 1_000_000 + i * 10))
        paths.append(p)
    common.prune_debug_screenshots(keep=3, screenshot_dir=str(tmp_path))
    survivors = sorted(p.name for p in tmp_path.iterdir())
    assert survivors == ["s07.png", "s08.png", "s09.png"]


def test_prune_debug_screenshots_ignores_subdirectories(tmp_path):
    (tmp_path / "keep_me").mkdir()
    for i in range(5):
        p = tmp_path / f"s{i}.png"
        p.write_text("x")
        os.utime(p, (1_000_000 + i, 1_000_000 + i))
    common.prune_debug_screenshots(keep=1, screenshot_dir=str(tmp_path))
    remaining = sorted(p.name for p in tmp_path.iterdir())
    assert remaining == ["keep_me", "s4.png"]


@pytest.mark.parametrize(
    "raw,expected",
    [
        # plain, no fence
        ('{"action":"click"}', {"action": "click"}),
        # ```json fence
        ('```json\n{"action":"click"}\n```', {"action": "click"}),
        # bare ``` fence
        ('```\n{"action":"click"}\n```', {"action": "click"}),
        # two fenced objects -> multi-object JSONDecodeError fallback takes the first
        ('```json\n{"a":1}\n```\n```json\n{"b":2}\n```', {"a": 1}),
        # trailing prose after the fence
        ('```json\n{"a":1}\n```\n\nNote: done.', {"a": 1}),
        # leading prose before the fence (no leading-fence guard hit; brace span saves it)
        ('Here is the action: {"action":"fill","value":"x"}', {"action": "fill", "value": "x"}),
        # nested object survives intact
        ('```json\n{"a":"select","o":{"k":"v"}}\n```', {"a": "select", "o": {"k": "v"}}),
        # CRLF line endings from the CLI
        ('```json\r\n{"action":"click"}\r\n```', {"action": "click"}),
    ],
)
def test_decide_action_parse_path(raw, expected):
    assert _parse_like_decide_action(raw) == expected
