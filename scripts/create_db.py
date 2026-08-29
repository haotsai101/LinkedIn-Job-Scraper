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

    seed_blocked_entities(conn, cursor)
    create_indexes(conn, cursor)
    enable_wal(conn, cursor)

    return True


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
# apply_jobs._ensure_apply_schema.
LISTED_EPOCH_BACKFILL_SQL = (
    "UPDATE jobs SET listed_epoch = CASE "
    + _epoch_case("original_listed_time")
    + _epoch_case("listed_time")
    + "ELSE NULL END "
    "WHERE listed_epoch IS NULL"
)


def create_indexes(conn, cursor):
    """Create the secondary indexes that back the apply-agent's hot pending-jobs
    query. Additive and idempotent (CREATE INDEX IF NOT EXISTS). Runs ANALYZE so
    the planner has the stats to choose idx_jobs_listed for the ORDER BY rather
    than falling back to a TEMP B-TREE sort."""
    for stmt in INDEX_STATEMENTS.values():
        cursor.execute(stmt)
    conn.commit()
    cursor.execute("ANALYZE")
    conn.commit()
    return True


def enable_wal(conn, cursor):
    """Switch the database to WAL journal mode (persists on the DB file).
    Self-contained: commits first because PRAGMA journal_mode is a no-op
    inside an open transaction."""
    conn.commit()
    mode = cursor.execute("PRAGMA journal_mode=WAL").fetchone()
    return mode[0] if mode else None