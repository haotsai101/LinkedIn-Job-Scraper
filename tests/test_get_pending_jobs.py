"""Tests for apply_jobs.get_pending_jobs and the T9 schema migration.

Covers:
  * newest-first ordering on the new integer ``listed_epoch`` column
  * blocklist filtering via the ``blocked_entities`` table (no SQL interpolation)
  * ``apply_type`` / ``limit`` / ``include_failed`` behaviour
  * the acceptance guarantee that the function body interpolates no values
  * the query plan no longer needs a TEMP B-TREE sort on the hot path
  * migration 002 is idempotent and backfills every row

No network, no browser: only sqlite3 + the two modules under test.
"""
from __future__ import annotations

import importlib.util
import inspect
import sqlite3
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import apply_jobs  # noqa: E402
from scripts.create_db import BLOCKED_ENTITIES_SEED, create_tables  # noqa: E402


def _load_migration_002():
    path = _REPO_ROOT / "scripts" / "migrations" / "002_schema.py"
    spec = importlib.util.spec_from_file_location("migration_002", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    create_tables(conn, conn.cursor())
    return conn


def _add_company(conn, company_id: int, name: str) -> None:
    conn.execute("INSERT INTO companies (company_id, name) VALUES (?, ?)", (company_id, name))


def _add_job(conn, job_id, *, company_id=None, listed_epoch=None, applied=None,
             application_type="OffsiteApply", remote_allowed=1, location="Remote",
             scraped=1) -> None:
    conn.execute(
        "INSERT INTO jobs (job_id, scraped, applied, company_id, application_type, "
        "remote_allowed, location, listed_epoch, title) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (job_id, scraped, applied, company_id, application_type, remote_allowed,
         location, listed_epoch, f"Job {job_id}"),
    )


@pytest.fixture()
def conn(tmp_path):
    c = _make_db(tmp_path / "t.db")
    yield c
    c.close()


def test_orders_by_listed_epoch_desc(conn):
    _add_job(conn, 1, listed_epoch=1000)
    _add_job(conn, 2, listed_epoch=3000)
    _add_job(conn, 3, listed_epoch=2000)
    conn.commit()
    rows = apply_jobs.get_pending_jobs(conn.cursor())
    assert [r[0] for r in rows] == [2, 3, 1]


def test_null_listed_epoch_sorts_last(conn):
    _add_job(conn, 1, listed_epoch=None)
    _add_job(conn, 2, listed_epoch=5)
    conn.commit()
    rows = apply_jobs.get_pending_jobs(conn.cursor())
    assert [r[0] for r in rows] == [2, 1]


def test_only_pending_scraped_jobs(conn):
    _add_job(conn, 1, listed_epoch=10, applied=None, scraped=1)
    _add_job(conn, 2, listed_epoch=20, applied=1, scraped=1)      # already applied
    _add_job(conn, 3, listed_epoch=30, applied=-1, scraped=1)     # skipped
    _add_job(conn, 4, listed_epoch=40, applied=-2, scraped=1)     # failed
    _add_job(conn, 5, listed_epoch=50, applied=None, scraped=0)   # not enriched
    conn.commit()
    assert [r[0] for r in apply_jobs.get_pending_jobs(conn.cursor())] == [1]
    # include_failed pulls in the applied = -2 row
    got = {r[0] for r in apply_jobs.get_pending_jobs(conn.cursor(), include_failed=True)}
    assert got == {1, 4}


def test_blocked_company_excluded_via_table(conn):
    _add_company(conn, 100, "SynergisticIT Inc")     # matches seeded 'synergisticit'
    _add_company(conn, 101, "Acme Robotics")
    _add_job(conn, 1, company_id=100, listed_epoch=10)
    _add_job(conn, 2, company_id=101, listed_epoch=20)
    conn.commit()
    assert [r[0] for r in apply_jobs.get_pending_jobs(conn.cursor())] == [2]


def test_custom_blocked_entity_row_is_honoured(conn):
    _add_company(conn, 100, "Weird Staffing Co")
    _add_job(conn, 1, company_id=100, listed_epoch=10)
    conn.commit()
    assert [r[0] for r in apply_jobs.get_pending_jobs(conn.cursor())] == [1]
    conn.execute(
        "INSERT INTO blocked_entities (kind, pattern, reason) VALUES (?, ?, ?)",
        ("company", "weird staffing", "test"),
    )
    conn.commit()
    assert apply_jobs.get_pending_jobs(conn.cursor()) == []


def test_apply_type_filter_is_parameterized(conn):
    _add_job(conn, 1, listed_epoch=10, application_type="OffsiteApply")
    _add_job(conn, 2, listed_epoch=20, application_type="SimpleOnsiteApply")
    _add_job(conn, 3, listed_epoch=30, application_type="ComplexOnsiteApply")
    conn.commit()
    rows = apply_jobs.get_pending_jobs(conn.cursor(), apply_type="OffsiteApply,ComplexOnsiteApply")
    assert {r[0] for r in rows} == {1, 3}
    # a value that would break a naive f-string must simply match nothing
    weird = apply_jobs.get_pending_jobs(conn.cursor(), apply_type="'); DROP TABLE jobs;--")
    assert weird == []
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 3


def test_limit_is_parameterized(conn):
    for i in range(5):
        _add_job(conn, i, listed_epoch=i)
    conn.commit()
    assert len(apply_jobs.get_pending_jobs(conn.cursor(), limit=2)) == 2


def test_function_body_has_no_fstring_interpolated_values():
    src = inspect.getsource(apply_jobs.get_pending_jobs)
    # No f-strings at all in the function, and no %-interpolation of values.
    assert 'f"' not in src and "f'" not in src
    assert "format(" not in src
    # Every dynamic piece is a bound '?' placeholder.
    assert "cursor.execute(query, params)" in src


def test_hot_path_query_plan_has_no_temp_btree(conn):
    _add_company(conn, 1, "Acme")
    for i in range(50):
        _add_job(conn, i, company_id=1, listed_epoch=1_700_000_000 + i,
                 applied=(-1 if i % 3 else None))
    conn.commit()
    conn.execute("ANALYZE")
    conn.commit()

    # Reproduce the exact SQL get_pending_jobs issues on the --auto path.
    captured = {}
    real_cursor = conn.cursor()

    class Spy:
        def execute(self, q, p=()):
            captured["q"], captured["p"] = q, list(p)
            return real_cursor.execute(q, p)

        def executemany(self, q, seq):
            return real_cursor.executemany(q, seq)

        def fetchall(self):
            return real_cursor.fetchall()

        @property
        def connection(self):
            return conn

    apply_jobs.get_pending_jobs(Spy())
    plan = conn.execute("EXPLAIN QUERY PLAN " + captured["q"], captured["p"]).fetchall()
    plan_text = " ".join(row[-1] for row in plan)
    assert "TEMP B-TREE" not in plan_text, plan_text
    assert "idx_jobs_listed" in plan_text, plan_text


def test_create_tables_is_migration_safe_on_pre_002_db(tmp_path):
    """T23 regression: create_tables() must migrate an existing pre-002 ``jobs``
    table in place.

    T9 pointed ``idx_jobs_listed`` at ``jobs(applied, listed_epoch DESC)`` but
    ``CREATE TABLE IF NOT EXISTS jobs`` is a no-op on an existing table, so
    ``listed_epoch`` never got added before ``create_indexes()`` ran ->
    ``OperationalError: no such column: listed_epoch`` on every
    ``search_retriever`` / ``details_retriever`` / Dagster op import.
    """
    db = tmp_path / "pre002.db"
    conn = sqlite3.connect(str(db))
    # Hand-written pre-002 schema: no listed_epoch column, no blocked_entities
    # table, idx_jobs_listed still on the old TEXT column.
    conn.execute(
        "CREATE TABLE jobs (job_id INTEGER PRIMARY KEY, scraped INTEGER NOT NULL DEFAULT 0, "
        "company_id INTEGER, application_type TEXT, remote_allowed INTEGER, location TEXT, "
        "applied INTEGER DEFAULT NULL, original_listed_time TEXT, listed_time TEXT)"
    )
    conn.execute("CREATE INDEX idx_jobs_listed ON jobs(original_listed_time DESC)")
    conn.executemany(
        "INSERT INTO jobs (job_id, scraped, original_listed_time) VALUES (?, 1, ?)",
        [(1, "1718939799000"), (2, "1700000000")],
    )
    conn.commit()

    # Must not raise.
    create_tables(conn, conn.cursor())

    cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    assert "listed_epoch" in cols

    epochs = dict(conn.execute("SELECT job_id, listed_epoch FROM jobs").fetchall())
    assert epochs[1] == 1718939799   # 13-digit epoch-millis / 1000
    assert epochs[2] == 1700000000   # already epoch-seconds

    seeded = conn.execute("SELECT COUNT(*) FROM blocked_entities").fetchone()[0]
    assert seeded >= len(BLOCKED_ENTITIES_SEED)

    idx_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_jobs_listed'"
    ).fetchone()[0]
    assert "listed_epoch" in idx_sql and "applied" in idx_sql

    # Second call is a pure no-op and must not raise.
    create_tables(conn, conn.cursor())
    conn.close()


def test_ensure_schema_current_skips_analyze_when_already_current(tmp_path):
    """ensure_schema_current() must not ANALYZE on a database that is already
    current — that write-lock used to hit every retriever startup (T23)."""
    import scripts.create_db as cdb

    db = tmp_path / "fresh.db"
    conn = sqlite3.connect(str(db))
    create_tables(conn, conn.cursor())          # first call migrates + builds
    conn.commit()

    executed: list[str] = []

    class RecordingCursor:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, *a):
            executed.append(sql)
            return self._inner.execute(sql, *a)

        def executemany(self, sql, seq):
            executed.append(sql)
            return self._inner.executemany(sql, seq)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    # Second call on an already-current DB: no schema change -> no ANALYZE.
    changed = cdb.ensure_schema_current(conn, RecordingCursor(conn.cursor()))
    assert changed is False
    assert not any(s.strip().upper().startswith("ANALYZE") for s in executed)
    conn.close()


def test_migration_002_backfill_and_idempotency(tmp_path):
    mig = _load_migration_002()
    db = tmp_path / "old.db"

    # Build a pre-T9 'jobs' table: no listed_epoch, epoch-millis TEXT timestamps.
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE jobs (job_id INTEGER PRIMARY KEY, scraped INTEGER DEFAULT 0, "
        "applied INTEGER, original_listed_time TEXT, listed_time TEXT)"
    )
    conn.execute("CREATE INDEX idx_jobs_listed ON jobs(original_listed_time DESC)")
    conn.executemany(
        "INSERT INTO jobs (job_id, scraped, original_listed_time, listed_time) VALUES (?, ?, ?, ?)",
        [
            (1, 1, "1718939799000", None),          # epoch millis
            (2, 1, "", "1734010693000"),            # falls back to listed_time
            (3, 1, "1700000000", None),             # already epoch seconds
            (4, 1, None, None),                     # unparseable -> stays NULL
        ],
    )
    conn.commit()
    conn.close()

    mig.migrate(db)
    mig.migrate(db)  # second run must not raise

    conn = sqlite3.connect(str(db))
    rows = dict(conn.execute("SELECT job_id, listed_epoch FROM jobs").fetchall())
    assert rows[1] == 1718939799        # /1000
    assert rows[2] == 1734010693        # from listed_time
    assert rows[3] == 1700000000        # unchanged
    assert rows[4] is None

    idx_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_jobs_listed'"
    ).fetchone()[0]
    assert "listed_epoch" in idx_sql

    seeded = conn.execute("SELECT COUNT(*) FROM blocked_entities").fetchone()[0]
    assert seeded >= 5
    conn.close()
