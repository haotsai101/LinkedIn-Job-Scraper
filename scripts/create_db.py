import sqlite3

# ── blocked_entities: canonical store for apply-agent blocklist patterns ───────
# Single source of truth for the DDL — imported by scripts/migrations/002_schema.py
# and by apply_jobs._ensure_apply_schema. ``kind`` partitions the patterns:
#   'company'    — case-insensitive substring match against companies.name
#   'ats_domain' — substring match against an application/posting domain
BLOCKED_ENTITIES_DDL = (
    "CREATE TABLE IF NOT EXISTS blocked_entities ("
    "kind TEXT NOT NULL, "
    "pattern TEXT NOT NULL, "
    "reason TEXT, "
    "PRIMARY KEY (kind, pattern))"
)

# Seed rows migrated out of the old apply_jobs.py Python constants
# (BLOCKED_COMPANIES / BLOCKED_DOMAINS). This is the fallback seed source only —
# the live blocklist is the blocked_entities table. Patterns are stored lowercase
# and matched with a parameterized LIKE (never string-interpolated into SQL).
BLOCKED_ENTITIES_SEED = [
    ("company", "synergisticit", "paid bootcamp recruiter"),
    ("company", "ladders", "theladders.com — paid job board, requires Ladders account"),
    ("ats_domain", "theladders.com", "paid job board"),
    ("ats_domain", "ed.crossover.com", "Apply with Google/LinkedIn only — no form"),
    ("ats_domain", "rex.zone", "OAuth-only apply flow"),
]


def seed_blocked_entities(conn, cursor):
    """Insert the fallback blocklist seed rows. Idempotent (INSERT OR IGNORE)."""
    cursor.executemany(
        "INSERT OR IGNORE INTO blocked_entities (kind, pattern, reason) VALUES (?, ?, ?)",
        BLOCKED_ENTITIES_SEED,
    )
    conn.commit()
    return True


def create_tables(conn, cursor):
    cursor.execute('''
          CREATE TABLE IF NOT EXISTS jobs (
          job_id INTEGER PRIMARY KEY,
          scraped INTEGER NOT NULL DEFAULT 0,
          company_id INTEGER,
          work_type TEXT,
          formatted_work_type TEXT,
          location TEXT,
          job_posting_url TEXT,
          applies INTEGER,
          original_listed_time TEXT,
          remote_allowed INTEGER,
          application_url TEXT,
          application_type TEXT,
          expiry TEXT,
          inferred_benefits TEXT,
          closed_time TEXT,
          formatted_experience_level TEXT,
          years_experience INTEGER,
          description TEXT,
          title TEXT,
          skills_desc TEXT,
          views INTEGER,
          job_region TEXT,
          listed_time TEXT,
          degree TEXT,
          posting_domain TEXT,
          sponsored INTEGER,
          applied INTEGER DEFAULT NULL,
          listed_epoch INTEGER
        );
    ''')

    cursor.execute(BLOCKED_ENTITIES_DDL)

    cursor.execute('''
      CREATE TABLE IF NOT EXISTS skills (
          skill_abr TEXT PRIMARY KEY,
          skill_name TEXT
      )
  ''')

    # cursor.execute('''
    #   CREATE TABLE IF NOT EXISTS job_skills (
    #       job_id INTEGER,
    #       skill_abr TEXT,
    #       skill_name TEXT,
    #       FOREIGN KEY (job_id) REFERENCES jobs(job_id),
    #       FOREIGN KEY (skill_abr) REFERENCES skills(skill_abr),
    #       FOREIGN KEY (skill_name) REFERENCES skills(skill_name),
    #       PRIMARY KEY (job_id)
    #   )
    # ''')

    cursor.execute('''
      CREATE TABLE IF NOT EXISTS job_skills (
          job_id INTEGER,
          skill_abr TEXT,
          FOREIGN KEY (job_id) REFERENCES jobs(job_id),
          FOREIGN KEY (skill_abr) REFERENCES skills(skill_abr),
          PRIMARY KEY (job_id, skill_abr)
      )
    ''')

    cursor.execute('''
      CREATE TABLE IF NOT EXISTS industries (
          industry_id INTEGER PRIMARY KEY,
          industry_name TEXT
      )
    ''')

    # cursor.execute('''
    #   CREATE TABLE IF NOT EXISTS job_industries (
    #       job_id INTEGER,
    #       industry_id INTEGER,
    #       industry_name TEXT,
    #       FOREIGN KEY (job_id) REFERENCES jobs(job_id),
    #       FOREIGN KEY (industry_id) REFERENCES industries(industry_id),
    #       FOREIGN KEY (industry_name) REFERENCES industries(industry_name),
    #       PRIMARY KEY (job_id)
    #   )
    # ''')

    cursor.execute('''
      CREATE TABLE IF NOT EXISTS job_industries (
          job_id INTEGER,
          industry_id INTEGER,
          FOREIGN KEY (job_id) REFERENCES jobs(job_id),
          FOREIGN KEY (industry_id) REFERENCES industries(industry_id),
          PRIMARY KEY (job_id, industry_id)
      )
    ''')

    cursor.execute('''
      CREATE TABLE IF NOT EXISTS salaries (
          salary_id INTEGER PRIMARY KEY,
          job_id INTEGER NOT NULL,
          max_salary FLOAT,
          med_salary FLOAT,
          min_salary FLOAT,
          pay_period TEXT,
          currency TEXT,
          compensation_type TEXT,
          FOREIGN KEY (job_id) REFERENCES job_postings (job_id)
      )
    ''')

    cursor.execute('''
      CREATE TABLE IF NOT EXISTS benefits (
          job_id INTEGER NOT NULL,
          inferred INTEGER NOT NULL,
          type TEXT NOT NULL,
          FOREIGN KEY (job_id) REFERENCES job_postings (job_id),
          PRIMARY KEY (job_id, type)
      )
    ''')

    # Create the "companies" table

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS companies (
            company_id INTEGER PRIMARY KEY,
            name TEXT,
            description TEXT,
            company_size INTEGER,
            state TEXT,
            country TEXT,
            city TEXT,
            zip_code TEXT,
            address TEXT,
            url TEXT
        )
    ''')

#           record_id INTEGER PRIMARY KEY AUTOINCREMENT,
    cursor.execute('''
      CREATE TABLE IF NOT EXISTS employee_counts (
          company_id INTEGER NOT NULL,
          employee_count INTEGER,
          follower_count INTEGER,
          time_recorded INTEGER NOT NULL,
          FOREIGN KEY (company_id) REFERENCES companies (company_id)
          PRIMARY KEY ( employee_count, company_id)
      )
    ''')
    cursor.execute('''
      CREATE TABLE IF NOT EXISTS company_specialities (
          company_id INTEGER NOT NULL,
          speciality INTEGER NOT NULL,
          FOREIGN KEY (company_id) REFERENCES companies (company_id),
          PRIMARY KEY (company_id, speciality)

      )
    ''')


    cursor.execute('''
      CREATE TABLE IF NOT EXISTS company_industries (
          company_id INTEGER NOT NULL,
          industry INTEGER NOT NULL,
          FOREIGN KEY (company_id) REFERENCES companies (company_id),
          PRIMARY KEY (company_id, industry)
      )
    ''')


    conn.commit()

    # ``CREATE TABLE IF NOT EXISTS jobs`` above is a no-op on a database whose
    # ``jobs`` table predates a later column (e.g. ``listed_epoch``, T9). Bring
    # such a database up to the current schema *before* create_indexes() runs —
    # otherwise ``CREATE INDEX ... ON jobs(applied, listed_epoch DESC)`` fails
    # with ``no such column: listed_epoch`` (T23 regression fix).
    ensure_schema_current(conn, cursor)
    create_indexes(conn, cursor)
    enable_wal(conn, cursor)

    return True


def _index_sql(cursor, name: str):
    """Return the stored ``CREATE INDEX`` SQL for ``name``, or ``None``."""
    row = cursor.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (name,)
    ).fetchone()
    return row[0] if row else None


def ensure_schema_current(conn, cursor):
    """Bring an existing database up to the current schema baseline.

    This is the **one** home for schema-modernization logic. It is invoked by
    ``create_tables()`` (every retriever / Dagster op entry point) and, via a thin
    wrapper, by ``apply_jobs._ensure_apply_schema`` (the apply path). The
    standalone migrations in ``scripts/migrations/`` predate this consolidation
    and keep their own copies for detailed logging / offline use.

    Idempotent and cheap — on an already-current database every step is a no-op
    (the backfill is gated behind a ``LIMIT 1`` probe, and index rebuild +
    ``ANALYZE`` run only when something changed).
    Steps:
      1. ``blocked_entities`` table + seed rows (``INSERT OR IGNORE``).
      2. ``jobs.listed_epoch INTEGER`` added if absent; the shared
         ``LISTED_EPOCH_BACKFILL_SQL`` runs only when the column was just added
         or a probe finds ``listed_epoch IS NULL`` rows still outstanding.
      3. A stale ``idx_jobs_listed`` (built on the old TEXT
         ``original_listed_time`` column) is dropped.
      4. On an actual schema change only: ``create_indexes()`` rebuilds every
         secondary index (``CREATE INDEX IF NOT EXISTS`` — so ``idx_jobs_listed``
         comes back on ``jobs(applied, listed_epoch DESC)`` even for callers that
         never go through ``create_tables()``), then ``ANALYZE`` refreshes
         planner stats. Neither runs on an already-current DB, so this is not a
         write lock on every retriever startup.

    Returns ``True`` if a schema change was applied this call, else ``False``.
    """
    schema_changed = False

    # ── 1. blocked_entities table + seed ─────────────────────────────────────
    cursor.execute(BLOCKED_ENTITIES_DDL)
    cursor.executemany(
        "INSERT OR IGNORE INTO blocked_entities (kind, pattern, reason) VALUES (?, ?, ?)",
        BLOCKED_ENTITIES_SEED,
    )

    # ── 2. jobs.listed_epoch column + backfill ───────────────────────────────
    # The backfill (``UPDATE ... WHERE listed_epoch IS NULL``) must NOT run
    # unconditionally: rows whose timestamps are permanently unparseable stay
    # NULL and keep matching that predicate, so an unguarded call would take a
    # write lock and full-scan on every retriever / Dagster startup. Run it only
    # when the column was just added, or when a cheap ``LIMIT 1`` probe shows
    # there is still work to do.
    #
    # T24: the probe is ``LISTED_EPOCH_PENDING_PROBE_SQL``, not a bare
    # ``listed_epoch IS NULL`` — it also requires that at least one source column
    # is actually parseable by the backfill ``CASE``. A row whose
    # ``original_listed_time`` *and* ``listed_time`` are both permanently
    # unparseable stays NULL forever; the bare predicate would keep matching it
    # and re-run the full-table UPDATE on every startup and never converge. With
    # T19 running this at every entrypoint, that non-convergence matters.
    cols = {row[1] for row in cursor.execute("PRAGMA table_info(jobs)").fetchall()}
    if "listed_epoch" not in cols:
        cursor.execute("ALTER TABLE jobs ADD COLUMN listed_epoch INTEGER")
        schema_changed = True
        cursor.execute(LISTED_EPOCH_BACKFILL_SQL)
    elif cursor.execute(LISTED_EPOCH_PENDING_PROBE_SQL).fetchone():
        cursor.execute(LISTED_EPOCH_BACKFILL_SQL)

    # ── 3. drop a stale idx_jobs_listed so it is rebuilt on the new shape ────
    stale = _index_sql(cursor, "idx_jobs_listed")
    if stale is not None and "listed_epoch" not in stale:
        cursor.execute("DROP INDEX IF EXISTS idx_jobs_listed")
        schema_changed = True

    conn.commit()

    # ── 4. on an actual schema change: (re)build indexes + refresh stats ────
    # Recreating the indexes here (not just in create_tables) is what makes this
    # the single home for schema modernization — an apply-only clone that never
    # runs a retriever still ends up with idx_jobs_listed on the new shape, so
    # get_pending_jobs's _has_index() check is a safety net, not load-bearing.
    if schema_changed:
        create_indexes(conn, cursor)   # CREATE INDEX IF NOT EXISTS — idempotent
        cursor.execute("ANALYZE")
        conn.commit()

    return schema_changed


# Secondary indexes backing the apply-agent's hot get_pending_jobs query.
# Single source of truth: scripts/migrations/001_indexes.py imports this dict.
INDEX_STATEMENTS = {
    "idx_jobs_pending": "CREATE INDEX IF NOT EXISTS idx_jobs_pending ON jobs(applied, scraped)",
    "idx_jobs_company": "CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs(company_id)",
    # Backs get_pending_jobs: filters `applied IS NULL` and orders by
    # `listed_epoch DESC`. Composite (applied, listed_epoch DESC) so one index
    # serves both the predicate and the sort — with plain `(listed_epoch DESC)`
    # the planner picks idx_jobs_pending for the predicate and still needs a
    # TEMP B-TREE for the ORDER BY. Migration 002 drops the old idx_jobs_listed
    # (on the TEXT original_listed_time column) and recreates it here.
    "idx_jobs_listed": (
        "CREATE INDEX IF NOT EXISTS idx_jobs_listed ON jobs(applied, listed_epoch DESC)"
    ),
    "idx_jobs_apptype": "CREATE INDEX IF NOT EXISTS idx_jobs_apptype ON jobs(application_type)",
}


def _epoch_case(col: str) -> str:
    """SQL CASE arms mapping a TEXT timestamp column to epoch *seconds*.

    ``col`` is a hard-coded column name (never user input) — this builds SQL
    structure, not interpolated values. Handles the shapes seen / plausible in
    the ``jobs`` table: 13-digit epoch-millis strings (the live format),
    10-digit epoch-seconds strings, and ISO-8601 date strings.
    """
    return (
        f"WHEN {col} IS NOT NULL AND {col} <> '' AND {col} NOT GLOB '*[^0-9]*' THEN "
        f"    CASE WHEN length({col}) >= 12 THEN CAST({col} AS INTEGER) / 1000 "
        f"         ELSE CAST({col} AS INTEGER) END "
        f"WHEN {col} LIKE '____-__-__%' THEN CAST(strftime('%s', {col}) AS INTEGER) "
    )


# Backfill jobs.listed_epoch from original_listed_time, falling back to
# listed_time. Only touches rows where listed_epoch IS NULL, so it is safe to
# re-run. Shared by scripts/migrations/002_schema.py and
# ensure_schema_current() (which the apply path calls via
# apply_jobs._ensure_apply_schema).
LISTED_EPOCH_BACKFILL_SQL = (
    "UPDATE jobs SET listed_epoch = CASE "
    + _epoch_case("original_listed_time")
    + _epoch_case("listed_time")
    + "ELSE NULL END "
    "WHERE listed_epoch IS NULL"
)


def _epoch_fixable(col: str) -> str:
    """SQL boolean — true for a row the ``listed_epoch`` backfill CASE can turn
    non-NULL from ``col``.

    Mirrors the two ``WHEN`` arms of ``_epoch_case`` exactly, *including* the
    fact that ``strftime('%s', <invalid-iso>)`` evaluates to NULL — so a row
    that matches the ISO ``LIKE`` shape but holds an unparseable date
    (``'1234-56-78'``) is correctly reported as **not** fixable. ``col`` is a
    hard-coded column name, never user input.
    """
    return (
        f"(({col} IS NOT NULL AND {col} <> '' AND {col} NOT GLOB '*[^0-9]*') "
        f"OR ({col} LIKE '____-__-__%' AND strftime('%s', {col}) IS NOT NULL))"
    )


# T24: re-run gate for the listed_epoch backfill. A row is "pending" only if it
# is still NULL *and* at least one source column is parseable by the backfill
# CASE. Rows whose original_listed_time and listed_time are both permanently
# unparseable are excluded, so they can never keep the full-table backfill
# UPDATE firing on every ensure_schema_current() call.
LISTED_EPOCH_PENDING_PROBE_SQL = (
    "SELECT 1 FROM jobs WHERE listed_epoch IS NULL AND ("
    + _epoch_fixable("original_listed_time")
    + " OR "
    + _epoch_fixable("listed_time")
    + ") LIMIT 1"
)


def create_indexes(conn, cursor):
    """Create the secondary indexes that back the apply-agent's hot pending-jobs
    query. Additive and idempotent (CREATE INDEX IF NOT EXISTS).

    Does **not** run ANALYZE — that took a write lock on every
    ``search_retriever`` / ``details_retriever`` startup (T23). ``ANALYZE`` now
    runs only in ``ensure_schema_current()`` right after an actual schema change,
    and in the standalone migrations. Callers that need the ``jobs`` table to
    exist first should go through ``create_tables()``, which invokes
    ``ensure_schema_current()`` before this function."""
    for stmt in INDEX_STATEMENTS.values():
        cursor.execute(stmt)
    conn.commit()
    return True


def enable_wal(conn, cursor):
    """Switch the database to WAL journal mode (persists on the DB file).
    Self-contained: commits first because PRAGMA journal_mode is a no-op
    inside an open transaction."""
    conn.commit()
    mode = cursor.execute("PRAGMA journal_mode=WAL").fetchone()
    return mode[0] if mode else None


def _db_path_of(conn):
    """Return the on-disk path of ``conn``'s main database, or ``None`` for an
    in-memory / temp database (``PRAGMA database_list`` reports an empty file
    for those)."""
    try:
        for _seq, name, filename in conn.execute("PRAGMA database_list").fetchall():
            if name == "main":
                return filename or None
    except sqlite3.Error:
        pass
    return None


def ensure_db_ready(conn, cursor, *, db_path=None, run_migrations=True):
    """The single "bring linkedin_jobs.db fully up to date" entry point.

    Every process that opens the database — ``search_retriever``,
    ``details_retriever``, the Dagster ops, and ``apply_jobs`` — calls this once
    at startup instead of wiring up schema setup piecemeal (T19). It does two
    things, in order:

    1. ``create_tables(conn, cursor)`` — the fresh-DB DDL (all
       ``CREATE TABLE IF NOT EXISTS``), then ``ensure_schema_current()``
       (``jobs.listed_epoch`` + backfill, ``blocked_entities`` + seed, stale
       index cleanup), ``create_indexes()`` and ``enable_wal()``. All idempotent.
    2. ``run_pending_migrations(db_path)`` — discovers ``scripts/migrations/
       NNN_*.py``, runs any whose id is not yet in the ``schema_migrations``
       tracking table, and records each after it commits. Takes a one-time
       backup of the DB file before the first pending migration of a run; a
       startup where every migration is already applied does no backup and no
       work.

    ``db_path`` defaults to ``conn``'s own file. An in-memory / temp DB with no
    file path skips step 2 (the numbered migration scripts reconnect by path and
    cannot see an in-memory DB) — step 1 already brought such a DB current.
    ``run_migrations=False`` also skips step 2, for callers that only need the
    DDL.

    Returns the list of migration ids applied this call (empty on a no-op).
    """
    create_tables(conn, cursor)

    if not run_migrations:
        return []

    if db_path is None:
        db_path = _db_path_of(conn)
    if not db_path:
        return []

    # Deferred import: scripts/migrations/002_schema.py imports from this module,
    # so importing the runner at module load would be circular.
    from scripts.migrations.runner import run_pending_migrations

    return run_pending_migrations(db_path)