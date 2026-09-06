"""Tests for apply_jobs.load_session_blocked_domains and its use in run_session.

T22: ``run_session``'s per-job application-URL check must honour ``ats_domain``
rows an operator adds to the ``blocked_entities`` table, not just the frozen
``BLOCKED_ENTITIES_SEED`` constant. ``get_pending_jobs`` already reads the table;
this closes the gap for the mid-session URL check.

No network, no browser: only sqlite3 + inspect against the module under test.
"""
from __future__ import annotations

import inspect
import sqlite3
import sys
from pathlib import Path
from urllib.parse import urlparse

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import apply_jobs  # noqa: E402
from scripts.create_db import BLOCKED_ENTITIES_SEED, create_tables  # noqa: E402


@pytest.fixture()
def cursor(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "t.db"))
    create_tables(conn, conn.cursor())
    yield conn.cursor()
    conn.close()


def _url_blocked(app_url: str, blocked: set[str]) -> bool:
    """Exact replica of the predicate in run_session (~line 1233)."""
    return any(bd in urlparse(app_url).netloc for bd in blocked)


def test_returns_seed_ats_domains_on_migrated_db(cursor):
    got = apply_jobs.load_session_blocked_domains(cursor)
    seed_ats = {p for kind, p, _ in BLOCKED_ENTITIES_SEED if kind == "ats_domain"}
    assert seed_ats <= got
    # company patterns must not leak into the domain set
    assert "synergisticit" not in got


def test_operator_added_row_is_honoured_with_no_code_change(cursor):
    app_url = "https://careers.acme-ats.com/apply/12345"
    # Before: the domain is not blocked.
    assert not _url_blocked(app_url, apply_jobs.load_session_blocked_domains(cursor))

    # Operator adds a row to the table — no code change, no restart semantics.
    cursor.execute(
        "INSERT INTO blocked_entities (kind, pattern, reason) VALUES (?, ?, ?)",
        ("ats_domain", "acme-ats.com", "operator: OAuth-only, no form"),
    )
    cursor.connection.commit()

    blocked = apply_jobs.load_session_blocked_domains(cursor)
    assert "acme-ats.com" in blocked
    assert _url_blocked(app_url, blocked)


def test_seed_domain_still_blocks_via_union(cursor):
    """Regression: the union always includes the module constant, so a seed
    ``ats_domain`` blocks even if the table read returned nothing."""
    blocked = apply_jobs.load_session_blocked_domains(cursor)
    assert apply_jobs.BLOCKED_DOMAINS <= blocked
    assert _url_blocked("https://www.theladders.com/job/9", blocked)


def test_falls_back_to_constant_when_table_missing(tmp_path):
    """Pre-002 DB (no ``blocked_entities`` table) → no crash, returns exactly
    the seed constant."""
    conn = sqlite3.connect(str(tmp_path / "pre002.db"))
    conn.execute("CREATE TABLE jobs (job_id INTEGER PRIMARY KEY)")
    conn.commit()
    got = apply_jobs.load_session_blocked_domains(conn.cursor())
    assert got == set(apply_jobs.BLOCKED_DOMAINS)
    conn.close()


def test_patterns_are_normalised_lowercase(cursor):
    cursor.execute(
        "INSERT INTO blocked_entities (kind, pattern, reason) VALUES (?, ?, ?)",
        ("ats_domain", "  MixedCase-ATS.com  ", "operator"),
    )
    cursor.connection.commit()
    assert "mixedcase-ats.com" in apply_jobs.load_session_blocked_domains(cursor)


def test_run_session_url_check_uses_session_blocked_domains():
    """The mid-session URL check must read the per-session set, not the frozen
    module constant."""
    src = inspect.getsource(apply_jobs.run_session)
    assert "session_blocked_domains = load_session_blocked_domains(cursor)" in src
    # the netloc check iterates the session set …
    assert "for bd in session_blocked_domains" in src
    # … and no longer iterates the raw constant anywhere in run_session
    assert "for bd in BLOCKED_DOMAINS" not in src
