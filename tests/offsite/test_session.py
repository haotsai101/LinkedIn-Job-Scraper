"""OA11: one supervised job — every key → exact DB state, pauses, skip, lock order.

The controller, browser and submit guard are fakes; the DB is a real SQLite
file with the current schema; the human is a scripted list of keypresses.
"""
from __future__ import annotations

import asyncio
import sqlite3

import pytest

from offsite import store
from offsite.controller import FillResult
from offsite.prompts import JobInfo
from offsite.schemas import GeneratedAnswer
from offsite.session import check_list, run_offsite_job
from scripts.create_db import create_tables

JOB = JobInfo(42, "SWE", "Acme", "https://boards.greenhouse.io/acme/jobs/42", "desc")
ANS = [GeneratedAnswer(field_label="Email", answer="a@x.com", source="profile",
                       confidence=1.0, evidence=["email"], sensitive=False),
       GeneratedAnswer(field_label="Sponsorship", answer="Yes", source="profile",
                       confidence=1.0, evidence=["need_sponsorship"], sensitive=True),
       GeneratedAnswer(field_label="Why us?", answer="…", source="generated",
                       confidence=0.5, evidence=[], sensitive=False)]


def ready(**kw):
    return FillResult("ready", "nim", tool_calls=12, answers=ANS, **kw)


class FakeController:
    def __init__(self, *results):
        self.results = list(results)
        self.calls: list[tuple] = []

    async def fill(self):
        self.calls.append(("fill",))
        return self.results.pop(0)

    async def resume(self, reason, detail=""):
        self.calls.append(("resume", reason))
        return self.results.pop(0)

    async def fix(self, instruction):
        self.calls.append(("fix", instruction))
        return self.results.pop(0)


class FakeLock:
    def __init__(self):
        self.events: list[str] = []

    async def lock(self):
        self.events.append("lock")

    async def unlock(self):
        self.events.append("unlock")


class FakeBrowser:
    def __init__(self):
        self.opened = []

    async def open(self, url):
        self.opened.append(url)


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "db.sqlite")
    create_tables(c, c.cursor())
    c.execute("INSERT INTO jobs (job_id, scraped, applied) VALUES (42, 1, NULL)")
    c.commit()
    yield c
    c.close()


def go(conn, controller, keys, *, page="Thank you — application received. Ref 123"):
    keys = list(keys)
    said: list[str] = []
    lock, browser = FakeLock(), FakeBrowser()

    async def ask(prompt):
        said.append(f"? {prompt}")
        return keys.pop(0)

    async def page_text():
        return page

    out = asyncio.run(run_offsite_job(JOB, conn=conn, browser=browser, submit_guard=lock,
                                      controller=controller, ask=ask, say=said.append,
                                      page_text=page_text))
    return out, lock, said, browser


def applied(conn):
    return conn.execute("SELECT applied, applied_at FROM jobs WHERE job_id = 42").fetchone()


def test_submitted(conn):
    out, lock, said, browser = go(conn, FakeController(ready()), ["s"])
    assert (out.key, out.applied, out.status) == ("s", 1, store.SUBMITTED)
    a, at = applied(conn)
    assert a == 1 and at > 0
    att = store.latest_attempt(conn, 42)
    assert att["status"] == store.SUBMITTED and att["model_used"] == "nim"
    assert att["tool_calls"] == 12 and len(att["answers"]) == 3
    assert "Thank you" in att["confirmation"] and att["submitted_at"] > 0
    assert browser.opened == [JOB.url]
    # page locked while agents work, unlocked only for the review, locked again after
    assert lock.events[0] == "lock" and lock.events.count("unlock") == 1
    assert lock.events[lock.events.index("unlock") + 1] == "lock"
    assert lock.events[-1] == "lock"


@pytest.mark.parametrize("key,db,status", [
    ("r", -1, store.REJECTED),
    ("b", -3, store.BLOCKED),
    ("l", None, store.DEFERRED),
])
def test_other_review_keys(conn, key, db, status):
    out, _, _, _ = go(conn, FakeController(ready()), [key])
    assert (out.applied, out.status) == (db, status)
    assert applied(conn)[0] == db
    assert store.latest_attempt(conn, 42)["status"] == status


def test_later_leaves_the_job_pending(conn):
    go(conn, FakeController(ready()), ["l"])
    assert applied(conn) == (None, None)


def test_submitted_without_confirmation_asks_again(conn):
    out, _, said, _ = go(conn, FakeController(ready()), ["s", "n", "l"], page="Apply for SWE")
    assert out.key == "l" and applied(conn)[0] is None
    assert any("No confirmation message found" in s for s in said)
    out, _, _, _ = go(conn, FakeController(ready()), ["s", "y"], page="Apply for SWE")
    assert out.key == "s" and applied(conn)[0] == 1


def test_fix_reruns_the_agent_then_reviews_again(conn):
    ctl = FakeController(ready(), ready())
    out, lock, _, _ = go(conn, ctl, ["e", "Salary → 110000", "s"])
    assert ctl.calls == [("fill",), ("fix", "Salary → 110000")]
    assert out.key == "s"
    # locked again before the agent touched the page for the fix
    first_unlock = lock.events.index("unlock")
    assert lock.events[first_unlock + 1] == "lock"
    assert lock.events.count("unlock") == 2


def test_invalid_keys_are_asked_again(conn):
    out, _, _, _ = go(conn, FakeController(ready()), ["", "x", "submit", ])
    assert out.key == "s"


def test_skip_is_recorded_without_review(conn):
    fill = FillResult("skip", "nim", skip_reason="sponsorship_not_offered",
                      skip_evidence="We are unable to sponsor.")
    out, lock, said, _ = go(conn, FakeController(fill), [])
    assert (out.key, out.applied, out.status) == ("skip", -1, store.SKIPPED)
    assert applied(conn)[0] == -1
    assert store.latest_attempt(conn, 42)["error"] == "We are unable to sponsor."
    assert "unlock" not in lock.events                         # no review, never unlocked


def test_human_pause_records_email_then_resumes(conn):
    human = FillResult("human", "nim", human_reason="login", human_detail="Sign in to Workday")
    ctl = FakeController(human, ready())
    out, _, said, _ = go(conn, ctl, ["ada@example.com", "", "s"])
    assert ctl.calls == [("fill",), ("resume", "login")]
    att = store.latest_attempt(conn, 42)
    assert att["account_email"] == "ada@example.com"
    assert att["account_host"] == "boards.greenhouse.io"
    assert "password" not in " ".join(att.keys())
    assert out.key == "s"
    assert any("Sign in to Workday" in s for s in said)


def test_human_pause_can_end_as_blocked(conn):
    human = FillResult("human", "nim", human_reason="captcha", human_detail="Solve it")
    out, _, _, _ = go(conn, FakeController(human), ["b"])
    assert (out.applied, out.status) == (-3, store.BLOCKED) and applied(conn)[0] == -3


def test_needs_human_still_goes_to_review(conn):
    fill = FillResult("needs_human", "nim→claude", fallback_reason="nim: timeout; claude: loop")
    out, _, said, _ = go(conn, FakeController(fill), ["s"])
    assert out.key == "s" and applied(conn)[0] == 1
    assert any("could not finish" in s for s in said)


def test_crash_is_recorded_as_failed(conn):
    class Boom(FakeController):
        async def fill(self):
            raise RuntimeError("browser died")

    out, lock, said, _ = go(conn, Boom(), [])
    assert (out.applied, out.status) == (-2, store.FAILED)
    assert applied(conn)[0] == -2
    assert "browser died" in store.latest_attempt(conn, 42)["error"]
    assert lock.events[-1] == "lock"


def test_no_db_mode_works(tmp_path):
    out, _, _, _ = go(None, FakeController(ready()), ["s"])
    assert out.key == "s" and out.attempt_id is None


def test_check_list():
    line = check_list(ready(warnings=["cover-letter field 'Cover' was filled"]))
    assert "Sponsorship: 'Yes'" in line and "Why us?" in line and "confidence 0.5" in line
    assert "Email" not in line and "⚠ cover-letter" in line
    assert check_list(FillResult("ready", "nim")) == "nothing flagged"
