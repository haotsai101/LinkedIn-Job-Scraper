"""
common.py — Shared stdlib-only helpers for the apply pipeline.

Extracted (ticket T4) from ``apply_jobs.py`` / ``linkedin_apply.py``, where
``_write_llm_log``, the markdown-fence strip, and the JSON-object extraction
were copy-pasted. Centralizing them removes the drift risk and shrinks the
surface T14 (Agent SDK migration) has to touch.

Keep this module import-light: ``json`` / ``re`` / stdlib only — **no**
``playwright`` / ``openai`` / ``httpx``. The large modules import it, and so
does the test suite, sometimes in environments without the heavy deps installed.
"""

import json
import os
import re

# Single source of truth for the LLM debug-log path. All three modules append
# JSONL telemetry here; each previously defined its own identical constant
# (``_LLM_LOG_PATH`` / ``LLM_LOG_PATH``).
LLM_LOG_PATH = "llm_debug.jsonl"

# Directory where the apply flows dump per-step / per-failure PNGs
# (``linkedin_apply.py`` writes ``debug_screenshots/<session_ts>_...png``).
# keep in sync with the "debug_screenshots" literal in linkedin_apply.py
DEBUG_SCREENSHOT_DIR = "debug_screenshots"


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


def rotate_llm_log(max_bytes: int = 20_000_000, keep: int = 2) -> None:
    """Rotate :data:`LLM_LOG_PATH` once it grows past ``max_bytes``.

    Renames ``llm_debug.jsonl`` → ``llm_debug.jsonl.1``, shifting any existing
    ``.1`` → ``.2`` … and dropping the ``.{keep}`` generation, so at most
    ``keep`` rotated files survive alongside the fresh (now-empty) log.

    No-op when the file is absent or still at/under ``max_bytes``. Best-effort:
    every error is swallowed, matching :func:`write_llm_log` — rotation must
    never break an apply session.
    """
    try:
        keep = max(1, keep)
        path = LLM_LOG_PATH
        if not os.path.isfile(path) or os.path.getsize(path) <= max_bytes:
            return
        # Drop the oldest generation, then shift the rest up by one slot.
        oldest = f"{path}.{keep}"
        if os.path.exists(oldest):
            os.remove(oldest)
        for gen in range(keep - 1, 0, -1):
            src = f"{path}.{gen}"
            if os.path.exists(src):
                os.replace(src, f"{path}.{gen + 1}")
        os.replace(path, f"{path}.1")
    except Exception:
        pass


def prune_debug_screenshots(keep: int = 100, screenshot_dir: str = DEBUG_SCREENSHOT_DIR) -> None:
    """Delete the oldest files in ``screenshot_dir`` beyond the newest ``keep``.

    Recency is by mtime. No-op when the directory is absent or holds ``keep`` or
    fewer files. Subdirectories are ignored. Best-effort: errors are swallowed.
    """
    try:
        keep = max(0, keep)
        if not os.path.isdir(screenshot_dir):
            return
        files = [
            os.path.join(screenshot_dir, name)
            for name in os.listdir(screenshot_dir)
        ]
        files = [p for p in files if os.path.isfile(p)]
        if len(files) <= keep:
            return
        # Snapshot mtimes defensively: a concurrent apply run or external cleanup
        # could unlink a file between listdir and the sort, and a bare
        # ``key=os.path.getmtime`` would raise mid-sort and abort the prune.
        stamped = []
        for p in files:
            try:
                stamped.append((os.path.getmtime(p), p))
            except OSError:
                pass
        if len(stamped) <= keep:
            return
        stamped.sort()
        for _, p in stamped[: len(stamped) - keep]:
            try:
                os.remove(p)
            except OSError:
                pass
    except Exception:
        pass
