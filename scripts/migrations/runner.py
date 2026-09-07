"""Forward-only migration runner for ``linkedin_jobs.db`` (T19).

Discovers ``scripts/migrations/NNN_*.py`` in sorted order, runs any whose id is
not recorded in the ``schema_migrations`` tracking table, and records each id
*after* its migration has committed. Every entry point that opens the database
calls this once at startup via ``scripts.create_db.ensure_db_ready`` — an
unattended agent should never need a manual migration step.

Migration module contract
-------------------------
Each ``NNN_*.py`` exposes ``migrate(db_path)`` — it opens its own connection to
``db_path``, applies its (idempotent) changes, commits, and closes. The runner
never shares a connection or transaction with a migration: it only records the
id afterward, so a migration that raises leaves ``schema_migrations`` untouched
and the failure is loud.

The migration id is the file stem (``001_indexes``, ``002_schema``).

Concurrency
-----------
The discover → backup → apply → record critical section is serialised with an
advisory file lock (``<db>.migrate.lock``, ``fcntl.flock``). Dagster's
multiprocess executor can start ``search_jobs_op`` and ``fetch_job_details_op``
concurrently, and both call this on a not-yet-migrated DB. Without the lock the
two racing ``002_schema`` runs hit ``duplicate column name: listed_epoch`` (its
check-then-``ALTER`` is not atomic). With the lock the loser blocks, then
re-reads ``schema_migrations`` inside the lock, finds nothing pending, and
returns clean.

Backups
-------
Before the first pending migration of a run, the DB is snapshotted to
``<name>.bak-<epoch>`` with the SQLite online-backup API (consistent even with
other open connections). ``.gitignore`` covers ``linkedin_jobs.db*``. A startup
where every migration is already applied does no backup and no work.

Usage:
    python scripts/migrations/runner.py [path/to/linkedin_jobs.db]
"""
from __future__ import annotations

import contextlib
import importlib.util
import sqlite3
import sys
import threading
import time
from pathlib import Path
from types import ModuleType

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX (Windows); this project is macOS
    fcntl = None

# In-process serialisation. ``fcntl.flock`` guards *cross-process* races
# (Dagster's multiprocess executor runs search + details ops in separate
# processes); flock's semantics for multiple threads of one process sharing a
# path are murky, so this lock covers that case unambiguously.
_INPROC_LOCK = threading.Lock()

_MIGRATIONS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _MIGRATIONS_DIR.parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

DEFAULT_DB_PATH = "linkedin_jobs.db"

SCHEMA_MIGRATIONS_DDL = (
    "CREATE TABLE IF NOT EXISTS schema_migrations ("
    "id TEXT PRIMARY KEY, "
    "applied_at INTEGER)"
)

# Migration files: exactly three leading digits, an underscore, a name, ".py".
# Excludes runner.py, __init__.py and any helper module.
_MIGRATION_GLOB = "[0-9][0-9][0-9]_*.py"


def discover_migrations(migrations_dir: Path | None = None) -> list[Path]:
    """Return the ``NNN_*.py`` migration files, sorted by name (== by number)."""
    migrations_dir = migrations_dir or _MIGRATIONS_DIR
    return sorted(Path(migrations_dir).glob(_MIGRATION_GLOB))


def _applied_ids(db_path: Path) -> set[str]:
    """Read the recorded migration ids, creating ``schema_migrations`` if absent.

    Uses its own short-lived connection that is fully closed before any
    migration runs, so a migration's ``PRAGMA journal_mode=WAL`` never contends
    with an open transaction here.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(SCHEMA_MIGRATIONS_DDL)
        conn.commit()
        return {row[0] for row in conn.execute("SELECT id FROM schema_migrations")}
    finally:
        conn.close()


def _pending(db_path: Path, migrations: list[Path]) -> list[Path]:
    applied = _applied_ids(db_path)
    return [p for p in migrations if p.stem not in applied]


def _record_applied(db_path: Path, migration_id: str) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT OR REPLACE INTO schema_migrations (id, applied_at) VALUES (?, ?)",
            (migration_id, int(time.time())),
        )
        conn.commit()
    finally:
        conn.close()


@contextlib.contextmanager
def _migration_lock(db_path: Path):
    """Hold an exclusive advisory lock for the discover+apply critical section.

    No-op (best effort) if ``fcntl`` is unavailable. The lock file
    (``<db>.migrate.lock``) is created next to the DB and left in place — it
    carries no state, only the flock.
    """
    with _INPROC_LOCK:
        if fcntl is None:
            yield
            return
        lock_path = db_path.with_name(db_path.name + ".migrate.lock")
        lock_file = open(lock_path, "a")  # noqa: SIM115 - released in finally
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(lock_file, fcntl.LOCK_UN)
            finally:
                lock_file.close()


def _backup(db_path: Path, log) -> Path:
    """Snapshot the DB to ``<name>.bak-<epoch>`` via the SQLite online-backup API.

    Consistent even if another connection is mid-write — unlike a plain file
    copy, which can catch a torn WAL / an incomplete checkpoint.
    """
    backup_path = db_path.with_name(f"{db_path.name}.bak-{int(time.time())}")
    src = sqlite3.connect(str(db_path))
    dst = sqlite3.connect(str(backup_path))
    try:
        with dst:
            src.backup(dst)
    finally:
        dst.close()
        src.close()
    log(f"[migrations] backed up {db_path.name} -> {backup_path.name}")
    return backup_path


def _load_migration(path: Path) -> ModuleType:
    """Import a ``NNN_*.py`` file as a module (the leading digit makes it
    un-importable by normal ``import``)."""
    spec = importlib.util.spec_from_file_location(f"_t19_migration_{path.stem}", path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ImportError(f"cannot load migration module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_pending_migrations(
    db_path: str | Path = DEFAULT_DB_PATH,
    *,
    migrations_dir: Path | None = None,
    logger=None,
) -> list[str]:
    """Apply every discovered migration whose id is not in ``schema_migrations``.

    Returns the list of ids applied this call (empty when the DB is already
    current — the common case, and a fast no-op with no backup).
    """
    log = logger or print
    db_path = Path(db_path)
    migrations = discover_migrations(migrations_dir)
    if not migrations:
        return []

    # Cheap unlocked pre-check: the overwhelmingly common startup is "all
    # applied", and there is no point taking the lock for it.
    if not _pending(db_path, migrations):
        return []

    with _migration_lock(db_path):
        # Re-read inside the lock — a concurrent runner may have applied
        # everything while we were blocked on flock().
        pending = _pending(db_path, migrations)
        if not pending:
            return []

        log(f"[migrations] {len(pending)} pending: "
            + ", ".join(p.stem for p in pending))
        _backup(db_path, log)

        done: list[str] = []
        for path in pending:
            module = _load_migration(path)
            migrate_fn = getattr(module, "migrate", None)
            if not callable(migrate_fn):
                raise RuntimeError(
                    f"migration {path.name} has no callable migrate(db_path) entrypoint"
                )
            log(f"[migrations] applying {path.stem} ...")
            migrate_fn(str(db_path))          # owns its own connection + commit
            _record_applied(db_path, path.stem)  # only after the migration committed
            done.append(path.stem)
            log(f"[migrations] recorded {path.stem}")

        return done


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB_PATH
    result = run_pending_migrations(target)
    if result:
        print(f"Applied: {', '.join(result)}")
    else:
        print("No pending migrations.")
