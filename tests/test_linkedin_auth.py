"""T17 PR 2 — Playwright storage_state session bootstrap (scripts/linkedin_auth.py).

No network, no browser: ``_playwright_login`` is the only seam that would launch
Chromium and every test stubs or asserts-not-called on it.
"""

import json

import pytest
import requests

import scripts.fetch as fetch
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

# A real Playwright storage_state carries several cookies on BOTH .linkedin.com
# and .www.linkedin.com.
DUAL_DOMAIN_STATE = {
    "cookies": [
        {"name": "JSESSIONID", "value": '"ajax:9"', "domain": ".linkedin.com", "path": "/"},
        {"name": "li_at", "value": "TOK", "domain": ".linkedin.com", "path": "/"},
        {"name": "bcookie", "value": "v=2&x", "domain": ".linkedin.com", "path": "/"},
        {"name": "JSESSIONID", "value": '"ajax:9"', "domain": ".www.linkedin.com", "path": "/"},
        {"name": "lidc", "value": "b=1", "domain": ".linkedin.com", "path": "/"},
        {"name": "lidc", "value": "b=1", "domain": ".www.linkedin.com", "path": "/"},
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

def test_session_from_storage_state_maps_cookies_and_leaves_headers_default(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps(FAKE_STATE))

    session = linkedin_auth.session_from_storage_state(p)

    by_name = {c.name: c for c in session.cookies}
    assert by_name["li_at"].value == "AQEDAT_token"
    assert by_name["li_at"].domain == ".linkedin.com"
    assert by_name["JSESSIONID"].domain == ".www.linkedin.com"

    # csrf-token is JSESSIONID with the surrounding quotes stripped
    assert linkedin_auth.csrf_token(session) == "ajax:1234567890"

    # session.headers must stay at requests' defaults: the retrievers build a
    # full per-request header dict, and seeding session defaults here reorders
    # the on-the-wire Voyager header keys (a known anti-bot fingerprint).
    assert session.headers == requests.utils.default_headers()
    assert "Csrf-Token" not in session.headers


def test_cookie_header_dedupes_multi_domain_crumbs(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps(DUAL_DOMAIN_STATE))
    session = linkedin_auth.session_from_storage_state(p)

    header = linkedin_auth.cookie_header(session)
    names = [crumb.split("=", 1)[0] for crumb in header.split("; ")]

    assert len(names) == len(set(names)), f"repeated cookie crumb in {header!r}"
    assert set(names) == {"JSESSIONID", "li_at", "bcookie", "lidc"}
    assert 'JSESSIONID="ajax:9"' in header


def test_voyager_request_header_order_and_cookie_string_match_master(tmp_path):
    """Wire parity: populating a fresh session from storage_state must not change
    the prepared Voyager request's header key ORDER, and the Cookie string must
    carry each cookie name once. Locks both regressions the reviewer found."""
    p = tmp_path / "s.json"
    p.write_text(json.dumps(DUAL_DOMAIN_STATE))

    r = fetch.JobDetailRetriever.__new__(fetch.JobDetailRetriever)
    r.sessions = [linkedin_auth.session_from_storage_state(p)]
    headers = r._make_headers(0)
    url = "https://www.linkedin.com/voyager/api/jobs/jobPostings/1"

    ours = r.sessions[0].prepare_request(
        requests.Request("GET", url, headers=headers)
    ).headers
    # master's Session carried only requests.default_headers() + a cookie jar.
    mirror = requests.Session().prepare_request(
        requests.Request("GET", url, headers=dict(headers))
    ).headers

    assert list(ours.keys()) == list(mirror.keys())
    assert list(ours.keys()) == [
        "User-Agent", "Accept-Encoding", "Accept", "Connection",
        "Authority", "Method", "Path", "Scheme",
        "Accept-Language", "Cookie", "Csrf-Token", "X-Li-Track",
    ]

    cookie_names = [c.split("=", 1)[0] for c in ours["Cookie"].split("; ")]
    assert len(cookie_names) == len(set(cookie_names))


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


def test_playwright_login_wraps_bare_browser_error(monkeypatch):
    sync_api = pytest.importorskip("playwright.sync_api")

    def _raise(*_a, **_k):
        raise sync_api.Error("Executable doesn't exist — run playwright install")

    monkeypatch.setattr(sync_api, "sync_playwright", _raise)
    with pytest.raises(linkedin_auth.LinkedInLoginError, match="playwright install chromium"):
        linkedin_auth._playwright_login("a@x.com", "pw", headless=True)


def test_login_and_save_state_rejects_login_with_no_li_at(tmp_path, monkeypatch):
    p = tmp_path / "s.json"
    no_li_at = {"cookies": [
        {"name": "bcookie", "value": "z", "domain": ".linkedin.com", "path": "/"},
    ]}
    monkeypatch.setattr(linkedin_auth, "_playwright_login", lambda *a, **k: no_li_at)
    with pytest.raises(linkedin_auth.LinkedInLoginError):
        linkedin_auth.login_and_save_state("a@x.com", "pw", p)
    assert not p.exists()
