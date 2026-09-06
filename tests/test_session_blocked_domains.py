"""Tests for the T22 ats_domain block path in run_session.

T22: an operator adding an ``ats_domain`` row to the ``blocked_entities`` table
must actually block that ATS on the next apply session. The gap this closes:

  * ``load_session_blocked_domains`` reads the table (seed + operator rows),
    unioned with the frozen seed constant, degrading to the constant when the
    table is missing;
  * ``run_session``'s per-job check matches those patterns against
    ``posting_domain`` / ``application_url`` (the ATS host) — NOT ``job_url``,
    which is always a ``linkedin.com/jobs/view`` link;
  * host-suffix matching (``rex.zone`` ≠ ``forex.zone``), whitespace-only
    operator rows can't become a match-everything wildcard;
  * an operator block routes to ``applied = -3`` (excluded from --reset-failed).

No network, no browser: sqlite3 + inspect against the module under test.
"""
from __future__ import annotations

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


@pytest.fixture()
def cursor(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "t.db"))
    create_tables(conn, conn.cursor())
    yield conn.cursor()
    conn.close()


# ── load_session_blocked_domains ─────────────────────────────────────────────

def test_returns_seed_ats_domains_on_migrated_db(cursor):
    got = apply_jobs.load_session_blocked_domains(cursor)
    seed_ats = {p for kind, p, _ in BLOCKED_ENTITIES_SEED if kind == "ats_domain"}
    assert seed_ats <= got
    assert apply_jobs.BLOCKED_DOMAINS <= got          # union always keeps the constant
    assert "synergisticit" not in got                # company patterns don't leak
    assert "" not in got


def test_operator_added_row_appears_in_the_set(cursor):
    cursor.execute(
        "INSERT INTO blocked_entities (kind, pattern, reason) VALUES (?, ?, ?)",
        ("ats_domain", "acme-ats.com", "operator: OAuth-only, no form"),
    )
    cursor.connection.commit()
    assert "acme-ats.com" in apply_jobs.load_session_blocked_domains(cursor)


def test_patterns_are_normalised_lowercase_and_stripped(cursor):
    cursor.execute(
        "INSERT INTO blocked_entities (kind, pattern, reason) VALUES (?, ?, ?)",
        ("ats_domain", "  MixedCase-ATS.com  ", "operator"),
    )
    cursor.connection.commit()
    assert "mixedcase-ats.com" in apply_jobs.load_session_blocked_domains(cursor)


def test_whitespace_only_row_never_yields_empty_pattern(cursor):
    """Regression: a bare '   ' operator row must NOT normalise to '' (which any
    substring/prefix check would treat as match-everything)."""
    cursor.execute(
        "INSERT INTO blocked_entities (kind, pattern, reason) VALUES (?, ?, ?)",
        ("ats_domain", "   ", "operator typo"),
    )
    cursor.connection.commit()
    got = apply_jobs.load_session_blocked_domains(cursor)
    assert "" not in got
    # and the matcher fed that set blocks nothing unrelated
    assert apply_jobs._match_blocked_domain(got, "careers.example.com", "") is None


def test_falls_back_to_constant_when_table_missing(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "pre002.db"))
    conn.execute("CREATE TABLE jobs (job_id INTEGER PRIMARY KEY)")
    conn.commit()
    got = apply_jobs.load_session_blocked_domains(conn.cursor())
    assert got == set(apply_jobs.BLOCKED_DOMAINS)
    conn.close()


# ── _match_blocked_domain — realistic run_session inputs ─────────────────────

def _blocked_set(*extra):
    return set(apply_jobs.BLOCKED_DOMAINS) | set(extra)


def test_matches_ats_host_from_posting_domain():
    """The real column values: posting_domain carries the ATS host, job_url does
    not. A test that fed job_url (a linkedin.com link) would pass vacuously."""
    blocked = _blocked_set("myworkdayjobs.com")
    posting_domain = "alteryx.wd108.myworkdayjobs.com"
    application_url = "https://alteryx.wd108.myworkdayjobs.com/External/job/123"
    job_url = "https://www.linkedin.com/jobs/view/4055123456/"

    assert apply_jobs._match_blocked_domain(blocked, posting_domain, application_url) \
        == "alteryx.wd108.myworkdayjobs.com"
    # the linkedin job_url must never trip the check
    assert apply_jobs._match_blocked_domain(blocked, job_url) is None


def test_matches_ats_host_from_application_url_when_posting_domain_blank():
    blocked = _blocked_set("greenhouse.io")
    assert apply_jobs._match_blocked_domain(
        blocked, "", "https://job-boards.greenhouse.io/acme/jobs/9"
    ) == "job-boards.greenhouse.io"


def test_host_suffix_match_not_substring():
    """`rex.zone` (a seed pattern) must match rex.zone / sub.rex.zone but not
    forex.zone — the old `bd in netloc` substring test over-blocked."""
    blocked = _blocked_set()
    assert "rex.zone" in blocked                                   # seed
    assert apply_jobs._match_blocked_domain(blocked, "rex.zone", "") == "rex.zone"
    assert apply_jobs._match_blocked_domain(blocked, "apply.rex.zone", "") == "apply.rex.zone"
    assert apply_jobs._match_blocked_domain(blocked, "forex.zone", "") is None
    assert apply_jobs._match_blocked_domain(blocked, "notrex.zone", "") is None


def test_operator_row_blocks_end_to_end(cursor):
    """Full path: operator adds a row → helper picks it up → matcher blocks a job
    whose posting_domain matches. No code change between the two asserts."""
    posting_domain = "careers.weird-ats.io"
    before = apply_jobs.load_session_blocked_domains(cursor)
    assert apply_jobs._match_blocked_domain(before, posting_domain, "") is None

    cursor.execute(
        "INSERT INTO blocked_entities (kind, pattern, reason) VALUES (?, ?, ?)",
        ("ats_domain", "weird-ats.io", "operator"),
    )
    cursor.connection.commit()

    after = apply_jobs.load_session_blocked_domains(cursor)
    assert apply_jobs._match_blocked_domain(after, posting_domain, "") == posting_domain


def test_seed_domain_still_blocks(cursor):
    blocked = apply_jobs.load_session_blocked_domains(cursor)
    assert apply_jobs._match_blocked_domain(
        blocked, "theladders.com", "https://www.theladders.com/job/9"
    ) is not None


# ── run_session wiring guard ────────────────────────────────────────────────

def test_run_session_feeds_ats_columns_not_job_url_and_marks_blocked():
    """Lock the wiring an earlier revision got wrong: the check must pass
    posting_domain / application_url into _match_blocked_domain (not `url`) and
    mark the job -3, incrementing blocked_count."""
    src = inspect.getsource(apply_jobs.run_session)
    assert "session_blocked_domains = load_session_blocked_domains(cursor)" in src
    assert "_match_blocked_domain(" in src
    assert "session_blocked_domains, posting_domain, application_url" in src
    # the old, broken predicate is gone
    assert "for bd in session_blocked_domains" not in src
    assert "for bd in BLOCKED_DOMAINS" not in src
    # blocked, not skipped
    block = src.split("_match_blocked_domain(")[1].split("continue")[0]
    assert "mark_job(conn, cursor, job_id, -3)" in block
    assert "blocked_count += 1" in block
