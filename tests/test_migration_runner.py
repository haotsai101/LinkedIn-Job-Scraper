"""Tests for the T19 startup migration runner and the T24 backfill re-run gate.

Covers:
  * scripts.migrations.runner.run_pending_migrations — discovery, ordering,
    schema_migrations tracking, one-time DB backup, no-op on an up-to-date DB,
    partial state (001 already recorded -> only 002 runs)
  * scripts.create_db.ensure_db_ready — the consolidated "DB is current" entry
    point used by every process that opens linkedin_jobs.db
  * T24: ensure_schema_current() runs the listed_epoch backfill at most once on
    a DB that contains a permanently-unparseable timestamp row

No network, no browser: only sqlite3 + the modules under test.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.create_db import (  # noqa: E402
    LISTED_EPOCH_BACKFILL_SQL,
    LISTED_EPOCH_PENDING_PROBE_SQL,
    create_tables,
    ensure_db_ready,
    ensure_schema_current,
)
from scripts.migrations import runner  # noqa: E402

_ALL_IDS = ["001_indexes", "002_schema"]


def _bare_db(path: Path) -> None:
    """A hand-written pre-migrations `jobs` table: no listed_epoch column, no
    blocked_entities table, no schema_migrations table, idx_jobs_listed still on
    the old TEXT column (mirrors tests/test_get_pending_jobs.py's pre-002 DB)."""
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE jobs ("
        "job_id INTEGER PRIMARY KEY, scraped INTEGER NOT NULL DEFAULT 0, "
        "company_id INTEGER, application_type TEXT, remote_allowed INTEGER, "
        "location TEXT, applied INTEGER DEFAULT NULL, "
        "original_listed_time TEXT, listed_time TEXT)"
    )
    conn.execute("CREATE INDEX idx_jobs_listed ON jobs(original_listed_time DESC)")
    conn.commit()
    conn.close()


def _current_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    create_tables(conn, conn.cursor())
    conn.close()


def _table_exists(path: Path, name: str) -> bool:
    conn = sqlite3.connect(str(path))
    try:
        return (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
            ).fetchone()
            is not None
        )
    finally:
        conn.close()


def _recorded_ids(path: Path) -> list[str]:
    conn = sqlite3.connect(str(path))
    try:
        return [
            r[0]
            for r in conn.execute("SELECT id FROM schema_migrations ORDER BY id")
        ]
    finally:
        conn.close()


def _backups(db_path: Path) -> list[Path]:
    return sorted(db_path.parent.glob(f"{db_path.name}.bak-*"))


# ── runner ──────────────────────────────────────────────────────────────────────

def test_runs_all_pending_and_records_them(tmp_path):
    db = tmp_path / "linkedin_jobs.db"
    _bare_db(db)

    applied = runner.run_pending_migrations(db)

    assert applied == _ALL_IDS
    assert _recorded_ids(db) == _ALL_IDS
    # migrations actually ran: 002 creates blocked_entities + listed_epoch
    assert _table_exists(db, "blocked_entities")
    cols = {r[1] for r in sqlite3.connect(str(db)).execute("PRAGMA table_info(jobs)")}
    assert "listed_epoch" in cols


def test_backup_taken_once_when_pending(tmp_path):
    db = tmp_path / "linkedin_jobs.db"
    _bare_db(db)

    runner.run_pending_migrations(db)

    backups = _backups(db)
    assert len(backups) == 1


def test_second_call_is_noop_no_reapply_no_backup(tmp_path):
    db = tmp_path / "linkedin_jobs.db"
    _bare_db(db)
    runner.run_pending_migrations(db)
    before = _backups(db)

    applied = runner.run_pending_migrations(db)

    assert applied == []
    assert _backups(db) == before  # no new backup


def test_partial_state_only_missing_migration_runs(tmp_path):
    db = tmp_path / "linkedin_jobs.db"
    _bare_db(db)
    # Pretend 001 was already applied by hand.
    conn = sqlite3.connect(str(db))
    conn.execute(runner.SCHEMA_MIGRATIONS_DDL)
    conn.execute(
        "INSERT INTO schema_migrations (id, applied_at) VALUES ('001_indexes', 0)"
    )
    conn.commit()
    conn.close()

    applied = runner.run_pending_migrations(db)

    assert applied == ["002_schema"]
    assert _recorded_ids(db) == _ALL_IDS


def test_no_backup_when_nothing_pending(tmp_path):
    db = tmp_path / "linkedin_jobs.db"
    _current_db(db)
    # First run records both against the already-current DB (one backup).
    runner.run_pending_migrations(db)
    for b in _backups(db):
        b.unlink()

    applied = runner.run_pending_migrations(db)

    assert applied == []
    assert _backups(db) == []


def test_discover_migrations_sorted_and_filtered():
    found = [p.stem for p in runner.discover_migrations()]
    assert found == _ALL_IDS  # runner.py / __init__.py excluded, numeric order


# ── ensure_db_ready ─────────────────────────────────────────────────────────────

def test_ensure_db_ready_brings_bare_db_current_and_migrates(tmp_path):
    db = tmp_path / "linkedin_jobs.db"
    _bare_db(db)
    conn = sqlite3.connect(str(db))

    applied = ensure_db_ready(conn, conn.cursor())

    assert applied == _ALL_IDS
    assert _recorded_ids(db) == _ALL_IDS
    assert _table_exists(db, "blocked_entities")
    conn.close()


def test_ensure_db_ready_skips_migrations_for_in_memory_db():
    conn = sqlite3.connect(":memory:")
    # Must not raise and must not try to reconnect by (empty) path.
    applied = ensure_db_ready(conn, conn.cursor())
    assert applied == []
    conn.close()


def test_ensure_db_ready_idempotent(tmp_path):
    db = tmp_path / "linkedin_jobs.db"
    _bare_db(db)
    conn = sqlite3.connect(str(db))
    ensure_db_ready(conn, conn.cursor())

    assert ensure_db_ready(conn, conn.cursor()) == []
    conn.close()


# ── T24: backfill re-run gate ───────────────────────────────────────────────────

class _SpyCursor:
    """Wraps a real cursor, records every SQL string passed to execute*."""

    def __init__(self, real):
        self._real = real
        self.executed: list[str] = []

    def execute(self, sql, *args):
        self.executed.append(sql)
        return self._real.execute(sql, *args)

    def executemany(self, sql, *args):
        self.executed.append(sql)
        return self._real.executemany(sql, *args)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _count(spy: _SpyCursor, sql: str) -> int:
    return spy.executed.count(sql)


def test_probe_sql_excludes_permanently_unparseable_rows(tmp_path):
    db = tmp_path / "linkedin_jobs.db"
    _current_db(db)
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO jobs (job_id, original_listed_time, listed_time, listed_epoch) "
        "VALUES (1, 'not-a-date', 'garbage', NULL)"
    )
    # A row the backfill *can* fix (13-digit epoch-millis string).
    conn.execute(
        "INSERT INTO jobs (job_id, original_listed_time, listed_time, listed_epoch) "
        "VALUES (2, '1700000000000', NULL, NULL)"
    )
    conn.commit()

    assert conn.execute(LISTED_EPOCH_PENDING_PROBE_SQL).fetchone() is not None  # row 2

    conn.execute(LISTED_EPOCH_BACKFILL_SQL)
    conn.commit()

    # Row 2 filled, row 1 still NULL — but now the probe finds nothing to do.
    assert conn.execute("SELECT listed_epoch FROM jobs WHERE job_id=2").fetchone()[0]
    assert conn.execute("SELECT listed_epoch FROM jobs WHERE job_id=1").fetchone()[0] is None
    assert conn.execute(LISTED_EPOCH_PENDING_PROBE_SQL).fetchone() is None
    conn.close()


def test_ensure_schema_current_backfills_at_most_once_with_unparseable_row(tmp_path):
    db = tmp_path / "linkedin_jobs.db"
    _current_db(db)
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO jobs (job_id, original_listed_time, listed_time, listed_epoch) "
        "VALUES (1, 'not-a-date', 'garbage', NULL)"
    )
    conn.execute(
        "INSERT INTO jobs (job_id, original_listed_time, listed_time, listed_epoch) "
        "VALUES (2, '1700000000000', NULL, NULL)"
    )
    conn.commit()

    spy1 = _SpyCursor(conn.cursor())
    ensure_schema_current(conn, spy1)
    spy2 = _SpyCursor(conn.cursor())
    ensure_schema_current(conn, spy2)

    # First call backfills (row 2 is fixable); second call must not — the
    # permanently-NULL row 1 no longer keeps the full-table UPDATE firing.
    assert _count(spy1, LISTED_EPOCH_BACKFILL_SQL) == 1
    assert _count(spy2, LISTED_EPOCH_BACKFILL_SQL) == 0
    conn.close()
