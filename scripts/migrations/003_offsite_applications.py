"""Migration 003 — OffsiteApply agent persistence (OA1).

Two changes, both idempotent:

1. **``offsite_applications`` table** (+ ``idx_offsite_applications_job``) — one
   row per OffsiteApply agent attempt: status, ATS host, model used, fallback
   reason, answers/evidence JSON, account email (never a password),
   confirmation, error, timestamps. ``jobs.applied`` stays the per-job outcome
   field. Design: ``docs/NEW_AGENTIC_APPLY_PLAN.md`` §7.
2. **Scam / gig-site ``blocked_entities`` rows** — the part of the pre-#82
   offsite skip list that is kept (``INSERT OR IGNORE`` of
   ``BLOCKED_ENTITIES_SEED``). Enterprise ATS hosts are intentionally not added.

``scripts.create_db.create_tables`` creates the same table on every startup, so
a fresh DB already has it; this migration brings existing DBs current and
records the change in ``schema_migrations``.

Usage:
    python scripts/migrations/003_offsite_applications.py [path/to/linkedin_jobs.db]
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.create_db import (  # noqa: E402
    BLOCKED_ENTITIES_DDL,
    BLOCKED_ENTITIES_SEED,
    OFFSITE_APPLICATIONS_DDL,
    OFFSITE_APPLICATIONS_INDEX_DDL,
)

DEFAULT_DB_PATH = "linkedin_jobs.db"


def migrate(db_path: str | Path = DEFAULT_DB_PATH) -> None:
    """Apply migration 003 to the SQLite database at ``db_path``."""
    db_path = Path(db_path)
    if not db_path.exists():
        raise SystemExit(f"error: database not found: {db_path}")
    print(f"[003_offsite_applications] target database: {db_path}")

    conn = sqlite3.connect(str(db_path))
    try:
        cursor = conn.cursor()

        # ── 1. offsite_applications table + index ─────────────────────────────
        existed = cursor.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='offsite_applications'"
        ).fetchone()
        cursor.execute(OFFSITE_APPLICATIONS_DDL)
        cursor.execute(OFFSITE_APPLICATIONS_INDEX_DDL)
        conn.commit()
        verb = "already present" if existed else "created"
        print(f"[003_offsite_applications] table offsite_applications: {verb}")

        # ── 2. scam / gig-site blocklist rows ─────────────────────────────────
        cursor.execute(BLOCKED_ENTITIES_DDL)
        before = cursor.execute("SELECT COUNT(*) FROM blocked_entities").fetchone()[0]
        cursor.executemany(
            "INSERT OR IGNORE INTO blocked_entities (kind, pattern, reason) VALUES (?, ?, ?)",
            BLOCKED_ENTITIES_SEED,
        )
        conn.commit()
        after = cursor.execute("SELECT COUNT(*) FROM blocked_entities").fetchone()[0]
        print(
            f"[003_offsite_applications] blocked_entities: {after - before} row(s) added, "
            f"{after} total"
        )
    finally:
        conn.close()
    print("[003_offsite_applications] done.")


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB_PATH
    migrate(path)
