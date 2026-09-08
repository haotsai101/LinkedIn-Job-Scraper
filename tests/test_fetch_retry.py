"""T17 — tenacity retry/backoff around the Voyager API calls in scripts/fetch.py.

Covers ``_voyager_get`` directly (retry on ConnectionError/Timeout/429/5xx, no
retry on 401) and its integration into ``JobDetailRetriever.get_job_details``.
The real backoff sleep is neutralised so the suite stays fast.
"""

import pytest
import requests

import scripts.fetch as fetch
import scripts.linkedin_auth as linkedin_auth
from scripts.fetch import VoyagerAuthError, VoyagerRetryableError


@pytest.fixture(autouse=True)
def _no_backoff_sleep(monkeypatch):
    # tenacity calls `<decorated>.retry.sleep` between attempts.
    monkeypatch.setattr(fetch._voyager_get.retry, "sleep", lambda *_: None)
    # get_job_details also has a fixed 0.3s pacing sleep.
    monkeypatch.setattr(fetch.time, "sleep", lambda *_: None)


class FakeResp:
    def __init__(self, status_code, text="", json_data=None):
        self.status_code = status_code
        self.text = text
        self._json = json_data if json_data is not None else {}

    def json(self):
        return self._json


class FakeSession:
    """Pops one outcome per .get() call; an Exception instance is raised."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def get(self, url, headers=None, timeout=None):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


# ── _voyager_get ─────────────────────────────────────────────────────────────

def test_retries_connection_error_then_succeeds():
    sess = FakeSession([
        requests.exceptions.ConnectionError("boom"),
        requests.exceptions.ConnectionError("boom"),
        FakeResp(200, json_data={"ok": True}),
    ])
    resp = fetch._voyager_get(sess, "https://x")
    assert resp.status_code == 200
    assert sess.calls == 3


def test_retries_on_429_then_succeeds():
    sess = FakeSession([FakeResp(429), FakeResp(200)])
    resp = fetch._voyager_get(sess, "https://x")
    assert resp.status_code == 200
    assert sess.calls == 2


def test_401_raises_immediately_without_retry():
    sess = FakeSession([FakeResp(401, text="unauthorized")])
    with pytest.raises(VoyagerAuthError):
        fetch._voyager_get(sess, "https://x")
    assert sess.calls == 1


def test_persistent_500_retries_four_times_then_raises():
    sess = FakeSession([FakeResp(500) for _ in range(4)])
    with pytest.raises(VoyagerRetryableError):
        fetch._voyager_get(sess, "https://x")
    assert sess.calls == 4


def test_persistent_connection_error_retries_four_times_then_raises():
    sess = FakeSession([requests.exceptions.Timeout("t") for _ in range(4)])
    with pytest.raises(requests.exceptions.Timeout):
        fetch._voyager_get(sess, "https://x")
    assert sess.calls == 4


def test_non_retryable_4xx_passes_through():
    sess = FakeSession([FakeResp(404, text="nope")])
    resp = fetch._voyager_get(sess, "https://x")
    assert resp.status_code == 404
    assert sess.calls == 1


# ── JobDetailRetriever.get_job_details integration ───────────────────────────

def _detail_retriever(session):
    r = fetch.JobDetailRetriever.__new__(fetch.JobDetailRetriever)
    r.error_count = 0
    r.job_details_link = "https://voyager/{}"
    r.emails = ["acct@example.com"]
    r.passwords = ["pw"]
    r.state_paths = ["/tmp/does-not-exist-storage_state.json"]
    r.sessions = [session]
    r.session_index = 0
    r.headers = [{}]
    return r


def test_get_job_details_maps_persistent_5xx_to_sentinel():
    sess = FakeSession([FakeResp(503) for _ in range(4)])
    r = _detail_retriever(sess)
    out = r.get_job_details([42])
    assert out == {42: -1}
    assert r.error_count == 1  # one failed job, below the abort threshold
    assert sess.calls == 4     # retried before giving up


def test_get_job_details_reauths_once_on_401_then_succeeds(monkeypatch):
    # First Voyager call 401s -> _reauth refreshes the session -> retry succeeds.
    sess = FakeSession([FakeResp(401, text="stale"), FakeResp(200, json_data={"ok": 1})])
    r = _detail_retriever(sess)

    calls = {"login": 0}
    monkeypatch.setattr(linkedin_auth, "login_and_save_state",
                        lambda *a, **k: calls.__setitem__("login", calls["login"] + 1))
    monkeypatch.setattr(linkedin_auth, "session_from_storage_state", lambda _p: sess)
    monkeypatch.setattr(fetch.JobDetailRetriever, "_make_headers", lambda self, idx: {})

    out = r.get_job_details([42])
    assert out == {42: {"ok": 1}}
    assert calls["login"] == 1
    assert sess.calls == 2


def test_get_job_details_propagates_401_after_second_consecutive_401(monkeypatch):
    sess = FakeSession([FakeResp(401, text="stale"), FakeResp(401, text="still stale")])
    r = _detail_retriever(sess)
    monkeypatch.setattr(linkedin_auth, "login_and_save_state", lambda *a, **k: None)
    monkeypatch.setattr(linkedin_auth, "session_from_storage_state", lambda _p: sess)
    monkeypatch.setattr(fetch.JobDetailRetriever, "_make_headers", lambda self, idx: {})
    with pytest.raises(VoyagerAuthError):
        r.get_job_details([42])
    assert sess.calls == 2


def test_get_job_details_happy_path():
    sess = FakeSession([FakeResp(200, json_data={"included": []})])
    r = _detail_retriever(sess)
    out = r.get_job_details([7])
    assert out == {7: {"included": []}}
    assert r.error_count == 0
