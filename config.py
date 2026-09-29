"""Centralized configuration for the LinkedIn-Job-Scraper.

Single source of truth for LLM model / endpoint selection and the handful of
non-LLM runtime settings that live in ``.env``.

One LLM *role* remains:

    guided_apply  -- LinkedIn Easy Apply via the Claude Agent SDK, which uses
                     subscription auth and no explicit endpoint, so ``api_key``
                     and ``base_url`` are always ``None`` for this role.

(The ``classifier`` / ``browser_use`` roles, the opt-in NVIDIA NIM classifier
route, and the ``CLASSIFIER_LLM_*`` / ``BROWSER_LLM_*`` / ``BROWSER_USE_*``
legacy env aliases were removed with OffsiteApplyFlow — they existed solely to
support the NIM classifier route and a browser-use engine that never shipped
on this branch. A from-scratch OffsiteApply redesign will need to reintroduce
whatever endpoint config it needs.)

``.env`` is parsed with a tiny built-in reader (same approach as the existing
``apply_jobs.load_env``) so no new dependency is introduced. Real environment
variables always win over ``.env`` file values.
"""

from __future__ import annotations

import os
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional

Role = Literal["guided_apply"]

# role -> resolved default model when no env var is set
_DEFAULTS: dict[str, str] = {
    "guided_apply": "claude-sonnet-5",
}

# role -> canonical model env var name
_MODEL_ENV: dict[str, str] = {
    "guided_apply": "GUIDED_APPLY_MODEL",
}

_ENV_LOADED = False


# ── .env loading ───────────────────────────────────────────────────────────────

def _load_dotenv() -> None:
    """Populate ``os.environ`` from ``./.env``, without overriding existing
    values. Runs at most once per process. Mirrors ``apply_jobs.load_env``."""
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    _ENV_LOADED = True

    env_file = Path(".env")
    if not env_file.exists():
        return
    for raw in env_file.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())


# ── LLM config ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class LLMConfig:
    """Resolved model + endpoint for one LLM role.

    ``api_key`` and ``base_url`` are always ``None`` for the sole remaining
    role, ``guided_apply`` — it runs through the Claude Agent SDK with no
    explicit endpoint.
    """

    model: str
    api_key: Optional[str]
    base_url: Optional[str]


def _env(name: str) -> Optional[str]:
    val = os.environ.get(name)
    if val is None:
        return None
    val = val.strip()
    return val or None


def get_llm_config(role: Role) -> LLMConfig:
    """Resolve the model for one LLM role. ``api_key`` / ``base_url`` are
    always ``None`` (the only role left, ``guided_apply``, uses Claude Agent
    SDK subscription auth with no explicit endpoint)."""
    if role not in _DEFAULTS:
        raise ValueError(
            f"Unknown LLM role {role!r}; expected one of "
            f"{', '.join(sorted(_DEFAULTS))}"
        )

    _load_dotenv()
    model = _env(_MODEL_ENV[role]) or _DEFAULTS[role]

    return LLMConfig(model=model, api_key=None, base_url=None)


# ── Non-LLM config ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class AppConfig:
    max_auto_apply: int
    gmail_user: Optional[str]
    gmail_app_password: Optional[str]


def get_config() -> AppConfig:
    """Resolve the non-LLM runtime settings that live in ``.env``."""
    _load_dotenv()

    raw = os.environ.get("MAX_AUTO_APPLY", "").strip() or "10"
    try:
        max_auto = int(raw)
    except ValueError:
        warnings.warn(
            f"MAX_AUTO_APPLY={raw!r} is not an integer; falling back to 10",
            RuntimeWarning,
            stacklevel=2,
        )
        max_auto = 10

    return AppConfig(
        max_auto_apply=max_auto,
        gmail_user=_env("GMAIL_USER"),
        gmail_app_password=_env("GMAIL_APP_PASSWORD"),
    )


if __name__ == "__main__":  # pragma: no cover - manual smoke check
    print(f"{'guided_apply':>13}: {get_llm_config('guided_apply')}")
    print(f"{'app':>13}: {get_config()}")
