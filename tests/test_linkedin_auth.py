"""T17 PR 2 — Playwright storage_state session bootstrap (scripts/linkedin_auth.py).

No network, no browser: ``_playwright_login`` is the only seam that would launch
Chromium and every test stubs or asserts-not-called on it.
"""

import json

import pytest

import scripts.linkedin_auth as linkedin_auth

FAKE_STATE = {
    "cookies": [
        {"name": "li_at", "value": "AQEDAT_token", "domain": ".linkedin.com",
         "path": "/", "secure": True},
        {"name": "JSESSIONID", "value": '"ajax:1234567890"',
         "domain": ".www.linkedin.com", "path": "/", "secure": True},
        {"name": "bcookie", "value": "v=2&abcdef", "domain": ".linkedin.com",
         "path": "/", "secure": True},
    ],
    "origins": [],
}


def _boom(*_a, **_k):
    raise AssertionError("a browser was launched when it must not have been")


# ── state_path_for ───────────────────────────────────────────────────────────

def test_state_path_for_is_stable_and_case_insensitive(tmp_path):
    a = linkedin_auth.state_path_for("Foo@Bar.com ", tmp_path)
    b = linkedin_auth.state_path_for("foo@bar.com", tmp_path)
    assert a == b
    assert a.parent == tmp_path
    assert a.name.startswith("storage_state_") and a.name.endswith(".json")
    # hash only — no raw email in the filename
    assert "bar.com" not in a.name


def test_state_path_for_honours_env_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("LINKEDIN_STATE_DIR", str(tmp_path))
    assert linkedin_auth.state_path_for("x@y.com").parent == tmp_path


# ── session_from_storage_state (cookie + header mapping) ──────────────────────

def test_session_from_storage_state_maps_cookies_and_headers(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps(FAKE_STATE))

    session = linkedin_auth.session_from_storage_state(p)

    by_name = {c.name: c for c in session.cookies}
    assert by_name["li_at"].value == "AQEDAT_token"
    assert by_name["li_at"].domain == ".linkedin.com"
    assert by_name["JSESSIONID"].domain == ".www.linkedin.com"

    # csrf-token is JSESSIONID with the surrounding quotes stripped
    assert session.headers["Csrf-Token"] == "ajax:1234567890"
    assert session.headers["User-Agent"] == linkedin_auth.USER_AGENT
    assert session.headers["X-Li-Track"] == linkedin_auth.X_LI_TRACK


def test_csrf_token_handles_multi_domain_jsessionid(tmp_path):
    # A real storage_state carries JSESSIONID on more than one domain;
    # RequestsCookieJar.get() would raise CookieConflictError, csrf_token must not.
    state = {"cookies": [
        {"name": "JSESSIONID", "value": '"ajax:dup"', "domain": ".linkedin.com", "path": "/"},
        {"name": "JSESSIONID", "value": '"ajax:dup"', "domain": ".www.linkedin.com", "path": "/"},
        {"name": "li_at", "value": "t", "domain": ".linkedin.com", "path": "/"},
    ]}
    p = tmp_path / "s.json"
    p.write_text(json.dumps(state))
    session = linkedin_auth.session_from_storage_state(p)
    assert linkedin_auth.csrf_token(session) == "ajax:dup"


def test_session_from_storage_state_rejects_missing_jsessionid(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"cookies": [
        {"name": "li_at", "value": "x", "domain": ".linkedin.com", "path": "/"},
    ]}))
    with pytest.raises(linkedin_auth.LinkedInLoginError):
        linkedin_auth.session_from_storage_state(p)


# ── get_session: cache hit vs. cold login ────────────────────────────────────

def test_get_session_uses_cache_and_never_launches_browser(tmp_path, monkeypatch):
    p = tmp_path / "storage_state_cached.json"
    p.write_text(json.dumps(FAKE_STATE))
    monkeypatch.setattr(linkedin_auth, "_playwright_login", _boom)
    monkeypatch.setattr(linkedin_auth, "login_and_save_state", _boom)

    session = linkedin_auth.get_session("a@x.com", "pw", p)

    assert linkedin_auth.csrf_token(session) == "ajax:1234567890"


def test_get_session_logs_in_when_state_missing(tmp_path, monkeypatch):
    p = tmp_path / "storage_state_new.json"
    seen = {}

    def fake_login(email, password, *, headless):
        seen["email"] = email
        seen["headless"] = headless
        return FAKE_STATE

    monkeypatch.setattr(linkedin_auth, "_playwright_login", fake_login)

    session = linkedin_auth.get_session("a@x.com", "pw", p)

    assert seen["email"] == "a@x.com"
    assert p.exists()
    assert json.loads(p.read_text())["cookies"][0]["name"] == "li_at"
    assert linkedin_auth.csrf_token(session) == "ajax:1234567890"


def test_get_session_reauths_when_cached_state_is_corrupt(tmp_path, monkeypatch):
    p = tmp_path / "storage_state_corrupt.json"
    p.write_text("{ not json")
    monkeypatch.setattr(linkedin_auth, "_playwright_login",
                        lambda *a, **k: FAKE_STATE)

    session = linkedin_auth.get_session("a@x.com", "pw", p)

    assert linkedin_auth.csrf_token(session) == "ajax:1234567890"
    assert json.loads(p.read_text())["cookies"][1]["name"] == "JSESSIONID"


def test_login_and_save_state_writes_file(tmp_path, monkeypatch):
    p = tmp_path / "s.json"
    monkeypatch.setattr(linkedin_auth, "_playwright_login", lambda *a, **k: FAKE_STATE)
    linkedin_auth.login_and_save_state("a@x.com", "pw", p)
    assert p.exists() and json.loads(p.read_text())["cookies"][0]["name"] == "li_at"


def test_login_and_save_state_rejects_login_with_no_li_at(tmp_path, monkeypatch):
    p = tmp_path / "s.json"
    no_li_at = {"cookies": [
        {"name": "bcookie", "value": "z", "domain": ".linkedin.com", "path": "/"},
    ]}
    monkeypatch.setattr(linkedin_auth, "_playwright_login", lambda *a, **k: no_li_at)
    with pytest.raises(linkedin_auth.LinkedInLoginError):
        linkedin_auth.login_and_save_state("a@x.com", "pw", p)
    assert not p.exists()
