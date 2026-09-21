"""Tests for the ``jobs.applied_at`` timestamp companion column.

Covers the write paths that set ``applied`` and must keep ``applied_at``
consistent with it:
  * ``apply_jobs.mark_job`` — stamps ``applied_at`` with "now" for every
    terminal status (1/-1/-2/-3)
  * ``apply_jobs.skip_ineligible_jobs`` — the bulk pre-filter UPDATE
  * ``apply_jobs.reset_failed_jobs`` — clears ``applied_at`` back to NULL
    alongside ``applied`` (covered in tests/test_get_pending_jobs.py)

No network, no browser: only sqlite3 + the modules under test.
"""
from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import apply_jobs  # noqa: E402
from scripts.create_db import create_tables  # noqa: E402


def _make_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    create_tables(conn, conn.cursor())
    return conn


def _add_job(conn, job_id, *, applied=None, applied_at=None, scraped=1,
             remote_allowed=1, location="Remote") -> None:
    conn.execute(
        "INSERT INTO jobs (job_id, scraped, applied, applied_at, remote_allowed, location, title) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (job_id, scraped, applied, applied_at, remote_allowed, location, f"Job {job_id}"),
    )


@pytest.fixture()
def conn(tmp_path):
    c = _make_db(tmp_path / "t.db")
    yield c
    c.close()


def _row(conn, job_id):
    return conn.execute(
        "SELECT applied, applied_at FROM jobs WHERE job_id = ?", (job_id,)
    ).fetchone()


@pytest.mark.parametrize("status", [1, -1, -2, -3])
def test_mark_job_stamps_applied_at_for_every_terminal_status(conn, status):
    _add_job(conn, 1)
    conn.commit()
    cur = conn.cursor()

    before = int(time.time())
    apply_jobs.mark_job(conn, cur, 1, status)
    after = int(time.time())

    applied, applied_at = _row(conn, 1)
    assert applied == status
    assert applied_at is not None
    assert before <= applied_at <= after


def test_mark_job_with_none_status_clears_applied_at(conn):
    """Defensive path: no current caller passes status=None, but a pending job
    should never carry a stale applied_at."""
    _add_job(conn, 1, applied=-2, applied_at=12345)
    conn.commit()
    cur = conn.cursor()

    apply_jobs.mark_job(conn, cur, 1, None)

    applied, applied_at = _row(conn, 1)
    assert applied is None
    assert applied_at is None


def test_skip_ineligible_jobs_stamps_applied_at(conn):
    _add_job(conn, 1, remote_allowed=0, location="San Francisco, CA")  # ineligible
    _add_job(conn, 2, remote_allowed=1, location="Remote")             # stays pending
    conn.commit()
    cur = conn.cursor()

    before = int(time.time())
    count = apply_jobs.skip_ineligible_jobs(conn, cur)
    after = int(time.time())

    assert count == 1
    applied, applied_at = _row(conn, 1)
    assert applied == -1
    assert applied_at is not None
    assert before <= applied_at <= after

    applied2, applied_at2 = _row(conn, 2)
    assert applied2 is None
    assert applied_at2 is None


def test_reset_failed_jobs_clears_applied_at(conn):
    _add_job(conn, 1, applied=-2, applied_at=12345)
    conn.commit()
    cur = conn.cursor()

    count = apply_jobs.reset_failed_jobs(conn, cur)

    assert count == 1
    applied, applied_at = _row(conn, 1)
    assert applied is None
    assert applied_at is None
