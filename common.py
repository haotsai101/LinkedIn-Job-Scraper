"""
common.py — Shared stdlib-only helpers for the apply pipeline.

Extracted (ticket T4) from ``apply_jobs.py`` / ``linkedin_apply.py`` /
``script_engine.py``, where ``_write_llm_log``, the markdown-fence strip, and the
JSON-object extraction were copy-pasted. Centralizing them removes the drift risk
and shrinks the surface T14 (Agent SDK migration) has to touch.

Keep this module import-light: ``json`` / ``re`` / stdlib only — **no**
``playwright`` / ``openai`` / ``httpx``. All three large modules import it, and so
does the test suite, sometimes in environments without the heavy deps installed.
"""

import json
import re

# Single source of truth for the LLM debug-log path. All three modules append
# JSONL telemetry here; each previously defined its own identical constant
# (``_LLM_LOG_PATH`` / ``LLM_LOG_PATH``).
LLM_LOG_PATH = "llm_debug.jsonl"


def write_llm_log(entry: dict) -> None:
    """Append one JSON line to :data:`LLM_LOG_PATH`.

    Best-effort telemetry: any IO or serialization error is swallowed so callers
    — especially ``except``-branch handlers that must always return ``None`` —
    are never disrupted by logging.
    """
    try:
        with open(LLM_LOG_PATH, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def strip_code_fence(raw: str) -> str:
    """Strip a leading + trailing markdown code fence (```` ``` ```` or
    ```` ```json ````) from an LLM response. No-op when the text does not start
    with a fence.

    Mirrors the canonical copy in ``apply_jobs.JobAgent.classify`` (the
    ``raw.startswith("```")``-guarded regex form).
    """
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*\n?", "", raw)
        raw = re.sub(r"\n?```\s*$", "", raw).strip()
    return raw


def extract_json_object(raw: str) -> str:
    """Return the substring from the first ``{`` to the last ``}`` inclusive,
    discarding any prose an LLM wrapped around a JSON object. Returns the input
    unchanged when no such span exists.

    The caller remains responsible for ``json.loads`` (and any multi-object
    salvage), since the two call sites parse the result differently.
    """
    start = raw.find("{")
    end = raw.rfind("}") + 1
    if start != -1 and end > start:
        return raw[start:end]
    return raw
