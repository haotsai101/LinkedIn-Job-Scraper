"""Centralized configuration for the LinkedIn-Job-Scraper.

Single source of truth for LLM model / endpoint selection and the handful of
non-LLM runtime settings that live in ``.env``.

Nothing imports this module yet -- T14 wires it into the apply agent. It exists
now so that model choices can be changed with one ``.env`` edit instead of
hunting for hardcoded strings across ``apply_jobs.py`` / ``linkedin_apply.py`` /
``script_engine.py``.

Three LLM *roles*, each with its own model plus (optionally) its own endpoint:

    classifier    -- job relevance scoring; a small / fast model on an
                     OpenAI-compatible endpoint (NVIDIA NIM by default).
    browser_use   -- form-filling / DOM reasoning during offsite apply; an
                     OpenAI-compatible endpoint (NVIDIA NIM by default).
    guided_apply  -- LinkedIn Easy Apply via the Claude Agent SDK, which uses
                     subscription auth and no explicit endpoint, so ``api_key``
                     and ``base_url`` are ``None`` for this role.

Legacy env var names (``LLM_*`` / ``CLASSIFIER_LLM_*`` / ``BROWSER_LLM_*``) are
still honored as fallback aliases for one release. Reading one emits a
``DeprecationWarning`` (once per alias per process).

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

Role = Literal["classifier", "browser_use", "guided_apply"]

_NIM_BASE_URL = "https://integrate.api.nvidia.com/v1"

# role -> resolved defaults when no env var (new or legacy) is set
_DEFAULTS: dict[str, dict[str, Optional[str]]] = {
    # meta/llama-3.1-8b-instruct hit end-of-life on NIM 2026-08-26 (HTTP 410);
    # google/gemma-4-31b-it replaced it (T25) but started timing out on the free
    # NIM tier (34-90s, or full timeout) during the T14 live run 2026-09-01.
    # meta/llama-3.2-11b-vision-instruct validated 2026-09-02: 8/8 correct on the
    # classification probe set (relevance + citizenship), p50 1.5s, clean
    # json_object output, 0 rate-limit errors at 10 rapid calls. See ticket T27.
    "classifier": {"model": "meta/llama-3.2-11b-vision-instruct", "base_url": _NIM_BASE_URL},
    # deepseek-ai/deepseek-v4-flash-0731: in the NIM catalog, but single calls
    # timed out (>7 min) on the free tier during T25 probing — the T15 browser-use
    # spike must confirm it (or pick another) before this default is trusted.
    "browser_use": {"model": "deepseek-ai/deepseek-v4-flash-0731", "base_url": _NIM_BASE_URL},
    "guided_apply": {"model": "claude-sonnet-5", "base_url": None},
}

# role -> (canonical var name, [legacy aliases in descending priority])
_MODEL_ENV: dict[str, tuple[str, list[str]]] = {
    "classifier": ("CLASSIFIER_MODEL", ["CLASSIFIER_LLM_MODEL", "LLM_MODEL"]),
    "browser_use": ("BROWSER_USE_MODEL", ["BROWSER_LLM_MODEL", "LLM_MODEL"]),
    "guided_apply": ("GUIDED_APPLY_MODEL", []),
}
_KEY_ENV: dict[str, tuple[str, list[str]]] = {
    "classifier": ("CLASSIFIER_API", ["CLASSIFIER_LLM_API", "LLM_API"]),
    "browser_use": ("BROWSER_USE_API", ["BROWSER_LLM_API", "LLM_API"]),
}
_URL_ENV: dict[str, tuple[str, list[str]]] = {
    "classifier": ("CLASSIFIER_BASE_URL", ["CLASSIFIER_LLM_URL", "LLM_URL"]),
    "browser_use": ("BROWSER_USE_BASE_URL", ["BROWSER_LLM_URL", "LLM_URL"]),
}

# aliases already warned about -- keeps the DeprecationWarning to once per process
_warned_legacy: set[str] = set()

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

    ``api_key`` and ``base_url`` are ``None`` for the ``guided_apply`` role,
    which runs through the Claude Agent SDK with no explicit endpoint.
    ``api_key`` is also ``None`` for ``classifier`` / ``browser_use`` whenever
    no key env var is set (there is a default ``model`` and ``base_url``, but no
    default key). T14 callers must check ``api_key is not None`` before
    constructing an OpenAI-compatible client and surface a clear error
    otherwise.
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


def _resolve(canonical: str, aliases: list[str], default: Optional[str]) -> Optional[str]:
    """Return the canonical env var if set, else the first set legacy alias
    (warning once), else ``default``."""
    val = _env(canonical)
    if val is not None:
        return val
    for alias in aliases:
        val = _env(alias)
        if val is not None:
            if alias not in _warned_legacy:
                _warned_legacy.add(alias)
                warnings.warn(
                    f"Environment variable {alias!r} is deprecated; use "
                    f"{canonical!r} instead. Legacy aliases will be removed in "
                    f"the next release.",
                    DeprecationWarning,
                    stacklevel=3,
                )
            return val
    return default


def get_llm_config(role: Role) -> LLMConfig:
    """Resolve the model / api_key / base_url for one LLM role.

    Resolution order per field: canonical env var -> legacy alias(es) ->
    hardcoded default.
    """
    if role not in _DEFAULTS:
        raise ValueError(
            f"Unknown LLM role {role!r}; expected one of "
            f"{', '.join(sorted(_DEFAULTS))}"
        )

    _load_dotenv()
    defaults = _DEFAULTS[role]

    model_canonical, model_aliases = _MODEL_ENV[role]
    model = _resolve(model_canonical, model_aliases, defaults["model"])
    assert model is not None  # every role has a default model

    api_key: Optional[str] = None
    base_url: Optional[str] = None
    if role in _KEY_ENV:
        key_canonical, key_aliases = _KEY_ENV[role]
        api_key = _resolve(key_canonical, key_aliases, None)
        url_canonical, url_aliases = _URL_ENV[role]
        base_url = _resolve(url_canonical, url_aliases, defaults["base_url"])

    return LLMConfig(model=model, api_key=api_key, base_url=base_url)


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
    for _role in ("classifier", "browser_use", "guided_apply"):
        print(f"{_role:>13}: {get_llm_config(_role)}")  # type: ignore[arg-type]
    print(f"{'app':>13}: {get_config()}")
