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
          applied INTEGER DEFAULT NULL
        );
    ''')

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

    create_indexes(conn, cursor)
    enable_wal(conn, cursor)

    return True


# Secondary indexes backing the apply-agent's hot get_pending_jobs query.
# Single source of truth: scripts/migrations/001_indexes.py imports this dict.
INDEX_STATEMENTS = {
    "idx_jobs_pending": "CREATE INDEX IF NOT EXISTS idx_jobs_pending ON jobs(applied, scraped)",
    "idx_jobs_company": "CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs(company_id)",
    "idx_jobs_listed": "CREATE INDEX IF NOT EXISTS idx_jobs_listed ON jobs(original_listed_time DESC)",
    "idx_jobs_apptype": "CREATE INDEX IF NOT EXISTS idx_jobs_apptype ON jobs(application_type)",
}


def create_indexes(conn, cursor):
    """Create the secondary indexes that back the apply-agent's hot pending-jobs
    query. Additive and idempotent (CREATE INDEX IF NOT EXISTS)."""
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