"""Unit tests for ``config.py`` -- the centralized model / endpoint config (T13).

Covers: defaults resolve, canonical env override, legacy-alias fallback (with the
one-shot ``DeprecationWarning``), the ``LLM_*`` last-resort key, unknown-role
guard, and ``MAX_AUTO_APPLY`` int parsing.

All env manipulation is via ``monkeypatch.setenv`` / ``delenv`` -- no ``.env``
file is read (the repo ships none; CI has none).
"""

import pytest

import config

_ALL_VARS = [
    "CLASSIFIER_MODEL", "CLASSIFIER_API", "CLASSIFIER_BASE_URL",
    "BROWSER_USE_MODEL", "BROWSER_USE_API", "BROWSER_USE_BASE_URL",
    "GUIDED_APPLY_MODEL",
    "CLASSIFIER_LLM_MODEL", "CLASSIFIER_LLM_API", "CLASSIFIER_LLM_URL",
    "BROWSER_LLM_MODEL", "BROWSER_LLM_API", "BROWSER_LLM_URL",
    "LLM_MODEL", "LLM_API", "LLM_URL",
    "MAX_AUTO_APPLY", "GMAIL_USER", "GMAIL_APP_PASSWORD",
]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Start every test from a known-empty env and reset the once-per-process
    legacy-warning cache."""
    for var in _ALL_VARS:
        monkeypatch.delenv(var, raising=False)
    config._warned_legacy.clear()
    # Pretend .env was already loaded so _load_dotenv is a no-op even if a
    # developer has a real .env in the cwd while running the suite.
    monkeypatch.setattr(config, "_ENV_LOADED", True)


# ── defaults ───────────────────────────────────────────────────────────────────

def test_classifier_defaults():
    cfg = config.get_llm_config("classifier")
    assert cfg.model == "meta/llama-3.1-8b-instruct"
    assert cfg.base_url == "https://integrate.api.nvidia.com/v1"
    assert cfg.api_key is None


def test_browser_use_defaults():
    """Acceptance check: bare env resolves to the deepseek / NIM config."""
    cfg = config.get_llm_config("browser_use")
    assert cfg.model == "deepseek-ai/deepseek-v4-flash-0731"
    assert cfg.base_url == "https://integrate.api.nvidia.com/v1"
    assert cfg.api_key is None


def test_guided_apply_defaults_have_no_endpoint():
    cfg = config.get_llm_config("guided_apply")
    assert cfg.model == "claude-sonnet-5"
    assert cfg.api_key is None
    assert cfg.base_url is None


# ── canonical override ─────────────────────────────────────────────────────────

def test_canonical_env_override(monkeypatch):
    monkeypatch.setenv("BROWSER_USE_MODEL", "custom/model-x")
    monkeypatch.setenv("BROWSER_USE_API", "sk-xyz")
    monkeypatch.setenv("BROWSER_USE_BASE_URL", "https://example.test/v1")
    cfg = config.get_llm_config("browser_use")
    assert cfg.model == "custom/model-x"
    assert cfg.api_key == "sk-xyz"
    assert cfg.base_url == "https://example.test/v1"


def test_guided_apply_model_override(monkeypatch):
    monkeypatch.setenv("GUIDED_APPLY_MODEL", "claude-opus-9")
    assert config.get_llm_config("guided_apply").model == "claude-opus-9"


def test_blank_env_var_falls_through_to_default(monkeypatch):
    monkeypatch.setenv("CLASSIFIER_MODEL", "   ")
    assert config.get_llm_config("classifier").model == "meta/llama-3.1-8b-instruct"


# ── legacy aliases ─────────────────────────────────────────────────────────────

def test_legacy_alias_fallback(monkeypatch):
    monkeypatch.setenv("CLASSIFIER_LLM_MODEL", "legacy/classifier")
    monkeypatch.setenv("CLASSIFIER_LLM_API", "legacy-key")
    monkeypatch.setenv("CLASSIFIER_LLM_URL", "https://legacy.test/v1")
    with pytest.warns(DeprecationWarning):
        cfg = config.get_llm_config("classifier")
    assert cfg.model == "legacy/classifier"
    assert cfg.api_key == "legacy-key"
    assert cfg.base_url == "https://legacy.test/v1"


def test_browser_use_legacy_model_alias(monkeypatch):
    monkeypatch.setenv("BROWSER_LLM_MODEL", "x")
    with pytest.warns(DeprecationWarning):
        cfg = config.get_llm_config("browser_use")
    assert cfg.model == "x"


def test_canonical_wins_over_legacy(monkeypatch):
    monkeypatch.setenv("BROWSER_USE_MODEL", "new/model")
    monkeypatch.setenv("BROWSER_LLM_MODEL", "old/model")
    import warnings as _w
    with _w.catch_warnings():
        _w.simplefilter("error")  # no DeprecationWarning expected
        cfg = config.get_llm_config("browser_use")
    assert cfg.model == "new/model"


def test_llm_api_is_last_resort_key(monkeypatch):
    monkeypatch.setenv("LLM_API", "shared-key")
    monkeypatch.setenv("LLM_URL", "https://shared.test/v1")
    monkeypatch.setenv("LLM_MODEL", "shared/model")
    with pytest.warns(DeprecationWarning):
        cfg = config.get_llm_config("browser_use")
    assert cfg.api_key == "shared-key"
    assert cfg.base_url == "https://shared.test/v1"
    assert cfg.model == "shared/model"


def test_deprecation_warning_emitted_once(monkeypatch, recwarn):
    monkeypatch.setenv("BROWSER_LLM_API", "legacy-key")
    config.get_llm_config("browser_use")
    config.get_llm_config("browser_use")
    deprecations = [w for w in recwarn.list if issubclass(w.category, DeprecationWarning)]
    assert len(deprecations) == 1


# ── guards ─────────────────────────────────────────────────────────────────────

def test_unknown_role_raises():
    with pytest.raises(ValueError):
        config.get_llm_config("summarizer")  # type: ignore[arg-type]


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
