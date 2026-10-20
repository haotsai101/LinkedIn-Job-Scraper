"""OA12: ``apply_jobs.py --type OffsiteApply`` routes offsite jobs to the agent.

``run_session`` is driven with fakes: the classifier, the LinkedIn browser, and
``offsite.batch.OffsiteBatch`` (records each job it is given and answers with a
scripted ``JobOutcome``). No browser, no network.
"""
from __future__ import annotations

import asyncio
import sys

import pytest

import apply_jobs
import offsite.batch
from offsite.session import JobOutcome


def _run(coro):
    return asyncio.run(coro)


class _Agent:
    def __init__(self, verdicts=None):
        self.verdicts = verdicts or {}

    async def classify(self, title, description, application_type):
        return self.verdicts.get(title, (True, "fits", False))


class _Conn:
    def close(self):
        pass


class _FakeBatch:
    instances: list = []
    script: list = []

    def __init__(self, profile, **kw):
        self.profile, self.jobs, self.closed = profile, [], False
        _FakeBatch.instances.append(self)

    async def run_job(self, conn, job, **kw):
        self.jobs.append(job)
        key = _FakeBatch.script.pop(0) if _FakeBatch.script else "l"
        applied = {"s": 1, "skip": -1, "r": -1, "b": -3, "l": None, "failed": -2}[key]
        return JobOutcome(key, applied, key.upper(), 1)

    async def close(self):
        self.closed = True


class _NoLinkedIn:
    """async_playwright() stand-in: fine to enter, but launching a browser fails."""

    class _P:
        class chromium:  # noqa: N801
            @staticmethod
            async def launch(**kw):
                raise AssertionError("LinkedIn browser should not be launched")

    def __call__(self):
        return self

    async def __aenter__(self):
        return self._P()

    async def __aexit__(self, *a):
        return None


def _job(jid, title="Backend Engineer", app_type="OffsiteApply",
         url="https://boards.greenhouse.io/acme/jobs/1", domain="acme.com"):
    return (jid, title, f"https://li/{jid}", "Remote", "Mid", "desc", "Acme", app_type,
            domain, url)


@pytest.fixture
def env(monkeypatch):
    marks, reports = [], []
    _FakeBatch.instances, _FakeBatch.script = [], []
    monkeypatch.setattr(offsite.batch, "OffsiteBatch", _FakeBatch)
    monkeypatch.setattr(apply_jobs, "async_playwright", _NoLinkedIn())
    monkeypatch.setattr(apply_jobs, "JobAgent", lambda _p: _Agent())
    monkeypatch.setattr(apply_jobs, "load_session_blocked_domains", lambda _c: {"dice.com"})
    monkeypatch.setattr(apply_jobs, "_check_recent_session_health", lambda: True)
    monkeypatch.setattr(apply_jobs, "write_session_log", reports.append)
    monkeypatch.setattr(apply_jobs, "send_session_email", lambda *_a: None)
    monkeypatch.setattr(apply_jobs, "_write_llm_log", lambda _e: None)
    monkeypatch.setattr(apply_jobs, "mark_job", lambda _cn, _cu, jid, st: marks.append((jid, st)))

    async def _no_sleep(*_a, **_k):
        return None

    monkeypatch.setattr(apply_jobs.asyncio, "sleep", _no_sleep)
    return marks, reports


def session(jobs, *, offsite=True):
    _run(apply_jobs.run_session(jobs, len(jobs), {"name": "T"}, _Conn(), object(),
                                auto_mode=False, max_apply=10, offsite=offsite))


def test_offsite_jobs_go_to_the_agent_without_a_linkedin_browser(env):
    marks, reports = env
    _FakeBatch.script = ["s", "skip", "b", "l", "failed"]
    session([_job(i) for i in range(1, 6)])
    batch = _FakeBatch.instances[0]
    assert [j.job_id for j in batch.jobs] == [1, 2, 3, 4, 5]
    assert batch.jobs[0].url == "https://boards.greenhouse.io/acme/jobs/1"
    assert batch.jobs[0].title == "Backend Engineer" and batch.jobs[0].company == "Acme"
    assert batch.closed
    assert marks == []                          # the offsite session wrote the DB itself
    r = reports[-1]
    assert (r["applied_count"], r["skipped_count"], r["blocked_count"], r["deferred_count"],
            r["error_count"]) == (1, 1, 1, 1, 1)
    assert r["applications"][0]["offsite"] is True


def test_usual_checks_run_before_the_agent(env, monkeypatch):
    marks, _ = env
    monkeypatch.setattr(apply_jobs, "JobAgent",
                        lambda _p: _Agent({"Sales Rep": (False, "not SWE", False)}))
    session([
        _job(1, title="Staff Software Engineer"),          # title rule → -1
        _job(2, title="Sales Rep"),                         # classifier → -1
        _job(3, url="https://www.dice.com/apply/3"),        # blocklist → -3
        _job(4, url=""),                                    # no URL → -3
        _job(5),                                            # → agent
    ])
    assert (1, -1) in marks and (2, -1) in marks and (3, -3) in marks and (4, -3) in marks
    assert [j.job_id for j in _FakeBatch.instances[0].jobs] == [5]


def test_without_offsite_flag_offsite_jobs_are_left_alone(env, monkeypatch):
    marks, _ = env
    monkeypatch.setattr(apply_jobs, "async_playwright", lambda: (_ for _ in ()).throw(
        AssertionError("no browser for an all-offsite batch without --type OffsiteApply")))
    session([_job(1), _job(2)], offsite=False)
    assert marks == [] and _FakeBatch.instances == []


def test_mixed_batch_without_flag_skips_offsite_but_still_applies_easyapply(env, monkeypatch):
    # the LinkedIn side is exercised elsewhere (test_browser_crash_recovery); here we
    # only need to see the offsite job is passed over before any DB write
    marks, _ = env
    seen = []

    class _StopAtLinkedIn(Exception):
        pass

    class _PW(_NoLinkedIn):
        class _P:
            class chromium:  # noqa: N801
                @staticmethod
                async def launch(**kw):
                    seen.append("linkedin")
                    raise _StopAtLinkedIn

    monkeypatch.setattr(apply_jobs, "async_playwright", _PW())
    with pytest.raises(_StopAtLinkedIn):
        session([_job(1), _job(2, app_type="SimpleOnsiteApply")], offsite=False)
    assert seen == ["linkedin"] and _FakeBatch.instances == []


def test_main_turns_offsite_on_only_for_type_offsiteapply(monkeypatch, tmp_path):
    calls = []

    async def fake_run_session(*a, **kw):
        calls.append(kw)

    import sqlite3

    from scripts.create_db import create_tables

    db = tmp_path / "db.sqlite"
    c = sqlite3.connect(db)
    create_tables(c, c.cursor())   # main()'s legacy migrate_db needs an existing jobs table
    c.close()
    monkeypatch.setattr(apply_jobs, "DB_PATH", str(db))
    monkeypatch.setattr(apply_jobs, "run_session", fake_run_session)
    monkeypatch.setattr(apply_jobs, "load_profile", lambda: {"name": "T"})
    monkeypatch.setattr(apply_jobs, "load_env", lambda: (None, None, None, "", "", 5))
    monkeypatch.setattr(apply_jobs, "rotate_llm_log", lambda: None)
    monkeypatch.setattr(apply_jobs, "prune_debug_screenshots", lambda: None)
    monkeypatch.setattr(apply_jobs, "skip_ineligible_jobs", lambda *_a: 0)
    monkeypatch.setattr(apply_jobs, "get_pending_jobs", lambda *_a, **_k: [_job(1)])
    for argv, expected in ((["--type", "OffsiteApply"], True),
                           (["--type", "SimpleOnsiteApply,OffsiteApply", "--auto"], True),
                           (["--type", "SimpleOnsiteApply"], False),
                           ([], False)):
        monkeypatch.setattr(sys, "argv", ["apply_jobs.py", *argv])
        apply_jobs.main()
        assert calls[-1]["offsite"] is expected, argv
