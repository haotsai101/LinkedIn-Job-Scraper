"""nim_client.py — OpenAI-compatible client for the NIM job-relevance classifier.

Ticket T14 (part 1). ``OffsiteApply`` jobs are classified here against the free
NVIDIA NIM endpoint resolved from ``config.get_llm_config("classifier")``; Easy
Apply jobs go through the Claude Agent SDK instead (``llm.ClaudeSession``).
Routing lives in ``apply_jobs.JobAgent.classify``.

``openai`` is still a project dependency (removed in T14b) so the import is only
soft-guarded — enough that ``import nim_client`` does not explode in a stripped
environment, not a real fallback.
"""

from __future__ import annotations

import json
from typing import Any

try:
    from openai import OpenAI
except ImportError:  # pragma: no cover - openai is a declared dependency
    OpenAI = None  # type: ignore[assignment, misc]

from common import extract_json_object
from config import LLMConfig, get_llm_config

# Request-level wall-clock ceiling (seconds). classify_via_nim runs inside
# ``asyncio.to_thread`` (uncancellable), and JobAgent wraps each attempt in a
# ~40s ``asyncio.wait_for``. Keep this just above that per-attempt deadline so a
# timed-out call's worker thread dies right after we stop waiting on it, instead
# of orphaning it for the ~50s difference a 90s ceiling would leave.
_TIMEOUT_S = 45.0

_PROMPT_TEMPLATE = (
    "Review this job posting on two dimensions:\n"
    "1. Is it related to software engineering, AI/ML, or data "
    "(engineering/science/analytics)?\n"
    "2. Does it explicitly require US citizenship or an active US security "
    "clearance (e.g. 'must be a US citizen', 'TS/SCI required', 'active Secret "
    "clearance')?\n\n"
    "Title: {title}\n\n"
    "Description:\n{description}\n\n"
    'Respond with a JSON object: {{"relevant": true|false, '
    '"reason": "<one sentence>", "citizenship_required": true|false}}'
)


class NimConfigError(RuntimeError):
    """The classifier endpoint is not usable — no API key resolvable from
    ``CLASSIFIER_API`` / ``CLASSIFIER_LLM_API`` / ``LLM_API``, or ``openai`` is
    not installed."""


def resolve_classifier(cfg: LLMConfig | None = None) -> tuple[Any, str]:
    """Return ``(OpenAI client, model name)`` for the ``classifier`` role.

    Raises :class:`NimConfigError` when no API key can be resolved so the caller
    can surface a clear message instead of an opaque auth failure mid-run.
    """
    if OpenAI is None:  # pragma: no cover - openai is a declared dependency
        raise NimConfigError("the 'openai' package is not installed")
    cfg = cfg or get_llm_config("classifier")
    if not cfg.api_key:
        raise NimConfigError(
            "No classifier API key. Set CLASSIFIER_API (or the legacy alias "
            "CLASSIFIER_LLM_API / LLM_API) in .env."
        )
    client = OpenAI(api_key=cfg.api_key, base_url=cfg.base_url, timeout=_TIMEOUT_S)
    return client, cfg.model


def classify_via_nim(client: Any, model: str, title: str, description: str) -> dict:
    """One classification call. Returns the raw parsed JSON object.

    Asks for ``response_format={"type": "json_object"}``, but NIM's honouring of
    that for smaller / older models is model-dependent and historically spotty,
    so the reply is still salvaged (``json.loads`` → first/last-brace extract)
    before giving up. Coercion into the ``(relevant, reason, citizenship_required)``
    tuple (and the telemetry log line) is the caller's job so both classifier
    routes share one code path.
    """
    prompt = _PROMPT_TEMPLATE.format(
        title=title or "", description=(description or "")[:3000]
    )
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        max_tokens=300,
        temperature=0,
    )
    content = resp.choices[0].message.content or "{}"
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        return json.loads(extract_json_object(content))
