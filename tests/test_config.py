"""Unit tests for ``config.py`` -- the centralized model / endpoint config (T13).

Covers: the sole remaining LLM role (``guided_apply``), unknown-role guard, and
``MAX_AUTO_APPLY`` int parsing.

The ``classifier`` / ``browser_use`` roles, legacy-alias fallback, and
``get_classifier_route`` were removed along with OffsiteApplyFlow and
nim_client.py (T-teardown) — they existed solely to support the opt-in NIM
classifier route and a browser-use engine that never shipped on this branch.

All env manipulation is via ``monkeypatch.setenv`` / ``delenv`` -- no ``.env``
file is read (the repo ships none; CI has none).
"""

import pytest

import config

_ALL_VARS = [
    "GUIDED_APPLY_MODEL",
    "LLM_MODEL", "LLM_API", "LLM_URL",
    "MAX_AUTO_APPLY", "GMAIL_USER", "GMAIL_APP_PASSWORD",
]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Start every test from a known-empty env.

    Pretend .env was already loaded so _load_dotenv is a no-op even if a
    developer has a real .env in the cwd while running the suite.
    """
    for var in _ALL_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(config, "_ENV_LOADED", True)


# ── defaults / override ────────────────────────────────────────────────────────

def test_guided_apply_defaults_have_no_endpoint():
    cfg = config.get_llm_config("guided_apply")
    assert cfg.model == "claude-sonnet-5"
    assert cfg.api_key is None
    assert cfg.base_url is None


def test_guided_apply_model_override(monkeypatch):
    monkeypatch.setenv("GUIDED_APPLY_MODEL", "claude-opus-9")
    assert config.get_llm_config("guided_apply").model == "claude-opus-9"


def test_blank_env_var_falls_through_to_default(monkeypatch):
    monkeypatch.setenv("GUIDED_APPLY_MODEL", "   ")
    assert config.get_llm_config("guided_apply").model == "claude-sonnet-5"


# ── guards ─────────────────────────────────────────────────────────────────────

def test_unknown_role_raises():
    with pytest.raises(ValueError):
        config.get_llm_config("classifier")  # type: ignore[arg-type]


# ── non-LLM config ─────────────────────────────────────────────────────────────

def test_max_auto_apply_default_is_int():
    cfg = config.get_config()
    assert cfg.max_auto_apply == 10
    assert isinstance(cfg.max_auto_apply, int)


def test_max_auto_apply_parses_env(monkeypatch):
    monkeypatch.setenv("MAX_AUTO_APPLY", "42")
    cfg = config.get_config()
    assert cfg.max_auto_apply == 42
    assert isinstance(cfg.max_auto_apply, int)


def test_max_auto_apply_non_int_falls_back(monkeypatch):
    monkeypatch.setenv("MAX_AUTO_APPLY", "lots")
    with pytest.warns(RuntimeWarning):
        assert config.get_config().max_auto_apply == 10


def test_gmail_vars_surface(monkeypatch):
    monkeypatch.setenv("GMAIL_USER", "me@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "abcd efgh ijkl mnop")
    cfg = config.get_config()
    assert cfg.gmail_user == "me@gmail.com"
    assert cfg.gmail_app_password == "abcd efgh ijkl mnop"


def test_gmail_vars_default_none():
    cfg = config.get_config()
    assert cfg.gmail_user is None
    assert cfg.gmail_app_password is None
