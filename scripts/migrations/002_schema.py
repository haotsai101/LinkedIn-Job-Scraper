"""Migration 002 — schema modernization (T9).

Three changes, all idempotent:

1. **``jobs.listed_epoch INTEGER``** — a numeric mirror of the TEXT
   ``original_listed_time`` (fallback ``listed_time``). Added if missing and
   backfilled. ``apply_jobs.get_pending_jobs`` now sorts on this column, and
   ``idx_jobs_listed`` is rebuilt to cover it, so the hot query's ORDER BY is
   index-driven instead of ``USE TEMP B-TREE FOR ORDER BY``. The TEXT columns are
   left in place (a later ticket drops them).

2. **``blocked_entities`` table** — the canonical store for the apply-agent
   blocklist patterns that used to be Python constants (``BLOCKED_COMPANIES`` /
   ``BLOCKED_DOMAINS``) interpolated into SQL. Created and seeded with
   ``INSERT OR IGNORE``.

3. **``idx_jobs_listed``** — dropped (it covered the TEXT ``original_listed_time``)
   and recreated as ``(applied, listed_epoch DESC)`` from the shared
   ``INDEX_STATEMENTS``, then ``ANALYZE`` refreshes planner stats. The composite
   shape lets one index satisfy both ``WHERE applied IS NULL`` and
   ``ORDER BY listed_epoch DESC`` with no TEMP B-TREE sort.

Re-running this migration is a no-op: the column check is guarded, the backfill
only touches ``listed_epoch IS NULL`` rows, the index swap + ANALYZE are
unconditional but cheap, and the seed uses ``INSERT OR IGNORE``.

Usage:
    python scripts/migrations/002_schema.py [path/to/linkedin_jobs.db]
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

# Allow running as a bare script: add the repo root to sys.path so `scripts.*`
# imports resolve.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.create_db import (  # noqa: E402
    BLOCKED_ENTITIES_DDL,
    BLOCKED_ENTITIES_SEED,
    INDEX_STATEMENTS,
    LISTED_EPOCH_BACKFILL_SQL,
)

DEFAULT_DB_PATH = "linkedin_jobs.db"


def _column_exists(cursor: sqlite3.Cursor, table: str, column: str) -> bool:
    rows = cursor.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == column for r in rows)


def migrate(db_path: str | Path = DEFAULT_DB_PATH) -> None:
    """Apply migration 002 to the SQLite database at ``db_path``."""
    db_path = Path(db_path)
    if not db_path.exists():
        raise SystemExit(f"error: database not found: {db_path}")

    print(f"[002_schema] target database: {db_path}")

    conn = sqlite3.connect(str(db_path))
    try:
        cursor = conn.cursor()

        # ── 1. jobs.listed_epoch column ───────────────────────────────────────
        if _column_exists(cursor, "jobs", "listed_epoch"):
            print("[002_schema] column jobs.listed_epoch: already present")
        else:
            cursor.execute("ALTER TABLE jobs ADD COLUMN listed_epoch INTEGER")
            print("[002_schema] column jobs.listed_epoch: added")
        conn.commit()

        # ── 2. backfill listed_epoch ──────────────────────────────────────────
        cursor.execute(LISTED_EPOCH_BACKFILL_SQL)
        filled = cursor.rowcount
        conn.commit()

        total, remaining = cursor.execute(
            "SELECT COUNT(*), SUM(CASE WHEN listed_epoch IS NULL THEN 1 ELSE 0 END) FROM jobs"
        ).fetchone()
        remaining = remaining or 0
        print(
            f"[002_schema] backfill: {filled} row(s) updated this run; "
            f"{total - remaining}/{total} rows now have listed_epoch"
        )
        if remaining:
            print(
                f"[002_schema] warning: {remaining} row(s) still have NULL listed_epoch "
                "(unparseable original_listed_time/listed_time) — get_pending_jobs sorts "
                "these last"
            )

        # ── 3. rebuild idx_jobs_listed on listed_epoch ────────────────────────
        cursor.execute("DROP INDEX IF EXISTS idx_jobs_listed")
        cursor.execute(INDEX_STATEMENTS["idx_jobs_listed"])
        conn.commit()
        print("[002_schema] index idx_jobs_listed: rebuilt on jobs(applied, listed_epoch DESC)")

        # ANALYZE so the planner has stats to pick idx_jobs_listed for the
        # ORDER BY instead of idx_jobs_pending + a TEMP B-TREE sort.
        cursor.execute("ANALYZE")
        conn.commit()
        print("[002_schema] ANALYZE: refreshed query-planner statistics")

        # ── 4. blocked_entities table + seed ──────────────────────────────────
        existed = cursor.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='blocked_entities'"
        ).fetchone()
        cursor.execute(BLOCKED_ENTITIES_DDL)
        cursor.executemany(
            "INSERT OR IGNORE INTO blocked_entities (kind, pattern, reason) VALUES (?, ?, ?)",
            BLOCKED_ENTITIES_SEED,
        )
        conn.commit()
        seeded = cursor.execute("SELECT COUNT(*) FROM blocked_entities").fetchone()[0]
        verb = "already present" if existed else "created"
        print(f"[002_schema] table blocked_entities: {verb}; {seeded} row(s) after seed")

        # ── WAL check (warning only, matches 001) ─────────────────────────────
        mode = cursor.execute("PRAGMA journal_mode").fetchone()[0]
        if mode.lower() != "wal":
            print(
                f"[002_schema] warning: journal_mode is '{mode}', not 'wal' — "
                "run migration 001 (WAL often can't be enabled on network filesystems)"
            )
    finally:
        conn.close()

    print("[002_schema] done.")


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB_PATH
    migrate(path)
