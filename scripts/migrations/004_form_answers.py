"""Migration 004 — ``form_answers`` table for the Easy Apply answer store (EA1).

One idempotent change: create ``form_answers`` (known Easy Apply questions and
their answers, see ``answer_store.py``). ``scripts.create_db.create_tables``
creates the same table on every startup, so a fresh DB already has it; this
migration brings existing DBs current and records the change in
``schema_migrations``.

Usage:
    python scripts/migrations/004_form_answers.py [path/to/linkedin_jobs.db]
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.create_db import FORM_ANSWERS_DDL  # noqa: E402

DEFAULT_DB_PATH = "linkedin_jobs.db"


def migrate(db_path: str | Path = DEFAULT_DB_PATH) -> None:
    """Apply migration 004 to the SQLite database at ``db_path``."""
    db_path = Path(db_path)
    if not db_path.exists():
        raise SystemExit(f"error: database not found: {db_path}")
    print(f"[004_form_answers] target database: {db_path}")

    conn = sqlite3.connect(str(db_path))
    try:
        cursor = conn.cursor()
        existed = cursor.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='form_answers'"
        ).fetchone()
        cursor.execute(FORM_ANSWERS_DDL)
        conn.commit()
        verb = "already present" if existed else "created"
        print(f"[004_form_answers] table form_answers: {verb}")
    finally:
        conn.close()
    print("[004_form_answers] done.")


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB_PATH
    migrate(path)
