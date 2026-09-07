"""Standalone, idempotent SQLite migrations for linkedin_jobs.db.

Each module is numbered ``NNN_*.py`` and exposes ``migrate(db_path)``:

    def migrate(db_path: str | Path = "linkedin_jobs.db") -> None:
        # open your own connection to db_path, apply idempotent changes,
        # commit, close.

It can also be run directly:

    python scripts/migrations/001_indexes.py [path/to/linkedin_jobs.db]

Migrations must be safe to run repeatedly.

At startup every process that opens the database calls
``scripts.create_db.ensure_db_ready``, which invokes
``scripts.migrations.runner.run_pending_migrations``: it runs any migration whose
stem is not yet in the ``schema_migrations(id TEXT PRIMARY KEY, applied_at
INTEGER)`` table, recording each stem only after that migration commits, and
takes a one-time DB backup before the first pending migration of a run.
"""
