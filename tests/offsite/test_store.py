"""OA1: offsite_applications table, migration 003, blocklist seed, offsite.store DAO.

No network, no browser: sqlite3 + the modules under test.
"""
from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

from offsite import store
from scripts.create_db import BLOCKED_ENTITIES_SEED, create_tables, ensure_db_ready
from scripts.migrations import runner

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MIGRATION_003 = _REPO_ROOT / "scripts" / "migrations" / "003_offsite_applications.py"

_SCAM_HOSTS = {"alignerr.com", "micro1.ai", "mercor.com", "jobright.ai", "dice.com", "yara.so"}
_ENTERPRISE_ATS = {"myworkdayjobs.com", "icims.com", "successfactors.com", "oraclecloud.com",
                   "taleo.net", "greenhouse.io", "ashbyhq.com"}


def _load_003():
    spec = importlib.util.spec_from_file_location("_m003", _MIGRATION_003)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _columns(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _pre_oa1_db(path: Path) -> None:
    """A DB as it looked before OA1: current jobs + blocked_entities, 001/002 recorded,
    no offsite_applications table, only the original 5 seed rows."""
    conn = sqlite3.connect(str(path))
    create_tables(conn, conn.cursor())
    conn.execute("DROP TABLE offsite_applications")
    conn.execute("DELETE FROM blocked_entities")
    conn.executemany(
        "INSERT INTO blocked_entities (kind, pattern, reason) VALUES (?, ?, ?)",
        BLOCKED_ENTITIES_SEED[:5],
    )
    conn.execute(runner.SCHEMA_MIGRATIONS_DDL)
    conn.executemany(
        "INSERT INTO schema_migrations (id, applied_at) VALUES (?, 0)",
        [("001_indexes",), ("002_schema",)],
    )
    conn.execute("INSERT INTO jobs (job_id, scraped, applied) VALUES (1, 1, NULL), (2, 1, -1)")
    conn.commit()
    conn.close()


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    create_tables(c, c.cursor())
    c.execute("INSERT INTO jobs (job_id, scraped) VALUES (42, 1), (43, 1)")
    c.commit()
    yield c
    c.close()


# ── schema / seed ─────────────────────────────────────────────────────────────

def test_fresh_db_has_offsite_applications(conn):
    assert _columns(conn, "offsite_applications") >= {
        "id", "job_id", "status", "ats_host", "model_used", "fallback_reason",
        "answers_json", "tool_calls", "account_email", "account_host",
        "confirmation", "error", "created_at", "updated_at", "submitted_at",
    }
    assert not any("password" in c for c in _columns(conn, "offsite_applications"))


def test_seed_blocks_scam_hosts_but_not_enterprise_ats(conn):
    blocked = {p for (p,) in conn.execute(
        "SELECT pattern FROM blocked_entities WHERE kind='ats_domain'")}
    assert _SCAM_HOSTS <= blocked
    assert not (_ENTERPRISE_ATS & blocked)


def test_migration_003_upgrades_pre_oa1_db_once(tmp_path):
    db = tmp_path / "linkedin_jobs.db"
    _pre_oa1_db(db)

    applied = runner.run_pending_migrations(db, logger=lambda *_: None)
    assert applied == ["003_offsite_applications"]
    backups = sorted(tmp_path.glob("linkedin_jobs.db.bak-*"))
    assert len(backups) == 1

    c = sqlite3.connect(str(db))
    assert "offsite_applications" in {
        r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    blocked = {p for (p,) in c.execute(
        "SELECT pattern FROM blocked_entities WHERE kind='ats_domain'")}
    assert _SCAM_HOSTS <= blocked
    # jobs untouched
    assert c.execute("SELECT job_id, applied FROM jobs ORDER BY job_id").fetchall() == [
        (1, None), (2, -1)]
    c.close()

    # second startup: no-op, no new backup
    assert runner.run_pending_migrations(db, logger=lambda *_: None) == []
    assert sorted(tmp_path.glob("linkedin_jobs.db.bak-*")) == backups


def test_migration_003_is_idempotent(tmp_path):
    db = tmp_path / "linkedin_jobs.db"
    _pre_oa1_db(db)
    m = _load_003()
    m.migrate(db)
    m.migrate(db)
    c = sqlite3.connect(str(db))
    n = c.execute("SELECT COUNT(*) FROM blocked_entities").fetchone()[0]
    assert n == len(BLOCKED_ENTITIES_SEED)
    c.close()


def test_ensure_db_ready_on_fresh_file(tmp_path):
    db = tmp_path / "linkedin_jobs.db"
    c = sqlite3.connect(str(db))
    applied = ensure_db_ready(c, c.cursor(), db_path=db)
    assert "003_offsite_applications" in applied
    store.start_attempt(c, 1, "boards.greenhouse.io")
    c.close()


# ── DAO ───────────────────────────────────────────────────────────────────────

def test_start_and_latest_attempt(conn):
    aid = store.start_attempt(conn, 42, "boards.greenhouse.io")
    row = store.latest_attempt(conn, 42)
    assert row["id"] == aid
    assert row["status"] == store.PREPARING
    assert row["ats_host"] == "boards.greenhouse.io"
    assert row["answers"] == []
    assert row["created_at"] == row["updated_at"] > 0
    assert store.latest_attempt(conn, 43) is None


def test_update_attempt_roundtrips_answers(conn):
    aid = store.start_attempt(conn, 42, "jobs.ashbyhq.com")
    answers = [{"field_label": "Need sponsorship?", "answer": "Yes", "source": "profile",
                "confidence": 1.0, "evidence": ["need_sponsorship"], "sensitive": True}]
    store.update_attempt(conn, aid, status=store.READY_FOR_REVIEW, model_used="nim",
                         tool_calls=17, answers=answers)
    row = store.latest_attempt(conn, 42)
    assert row["status"] == store.READY_FOR_REVIEW
    assert row["model_used"] == "nim"
    assert row["tool_calls"] == 17
    assert row["answers"] == answers


def test_update_attempt_accepts_pydantic_like_answers(conn):
    class A:
        def model_dump(self):
            return {"field_label": "x", "answer": "y"}

    aid = store.start_attempt(conn, 42, None)
    store.update_attempt(conn, aid, answers=[A()])
    assert store.latest_attempt(conn, 42)["answers"] == [{"field_label": "x", "answer": "y"}]


@pytest.mark.parametrize("fields", [
    {"password": "hunter2"},
    {"job_id": 43},
    {"created_at": 0},
    {"status": "APPLIED"},
])
def test_update_attempt_rejects_bad_fields(conn, fields):
    aid = store.start_attempt(conn, 42, None)
    with pytest.raises(ValueError):
        store.update_attempt(conn, aid, **fields)


def test_update_attempt_unknown_id(conn):
    with pytest.raises(ValueError):
        store.update_attempt(conn, 999, status=store.FAILED)


def test_latest_attempt_and_status_counts_use_newest_row(conn):
    first = store.start_attempt(conn, 42, None)
    store.update_attempt(conn, first, status=store.DEFERRED)
    second = store.start_attempt(conn, 42, None)
    store.update_attempt(conn, second, status=store.SUBMITTED, submitted_at=123)
    other = store.start_attempt(conn, 43, None)
    store.update_attempt(conn, other, status=store.DEFERRED)

    assert store.latest_attempt(conn, 42)["id"] == second
    assert store.status_counts(conn) == {store.SUBMITTED: 1, store.DEFERRED: 1}
