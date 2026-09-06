"""T37 — ``apply_jobs.write_session_log`` must not crash on a valid-but-wrong-shape
``application_log.json``.

The parse guard only catches ``json.loads`` errors, not shape errors: a file
containing a bare ``[]`` parses fine, then ``existing["sessions"].append(...)``
raised ``TypeError`` and killed the post-session bookkeeping of an otherwise
successful run.

No network, no browser: only ``apply_jobs`` + a tmp file.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import apply_jobs  # noqa: E402

_REPORT = {"started_at": "2026-09-06T00:00:00Z", "applied": 1, "failed": 0}


def _run(monkeypatch, tmp_path, initial_bytes: str | None):
    log = tmp_path / "application_log.json"
    if initial_bytes is not None:
        log.write_text(initial_bytes)
    monkeypatch.setattr(apply_jobs, "LOG_PATH", str(log))
    apply_jobs.write_session_log(_REPORT)
    return json.loads(log.read_text())


def test_write_session_log_recovers_from_bare_list_file(monkeypatch, tmp_path):
    out = _run(monkeypatch, tmp_path, "[]")
    assert out == {"sessions": [_REPORT]}


def test_write_session_log_recovers_from_dict_without_sessions_key(monkeypatch, tmp_path):
    out = _run(monkeypatch, tmp_path, '{"other": 1}')
    assert out == {"sessions": [_REPORT]}


def test_write_session_log_recovers_from_wrong_typed_sessions_value(monkeypatch, tmp_path):
    # T37 review: {"sessions": 5} / {"sessions": {}} also must not crash.
    assert _run(monkeypatch, tmp_path, '{"sessions": 5}') == {"sessions": [_REPORT]}
    assert _run(monkeypatch, tmp_path, '{"sessions": {}}') == {"sessions": [_REPORT]}


def test_write_session_log_appends_to_a_well_formed_file(monkeypatch, tmp_path):
    prior = {"ts": "earlier"}
    out = _run(monkeypatch, tmp_path, json.dumps({"sessions": [prior]}))
    assert out == {"sessions": [prior, _REPORT]}


def test_write_session_log_handles_missing_file(monkeypatch, tmp_path):
    out = _run(monkeypatch, tmp_path, None)
    assert out == {"sessions": [_REPORT]}


def test_write_session_log_handles_corrupt_json(monkeypatch, tmp_path):
    out = _run(monkeypatch, tmp_path, "{not json")
    assert out == {"sessions": [_REPORT]}
