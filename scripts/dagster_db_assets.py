"""
Dagster assets for LinkedIn Job Scraper SQLite database.
This module loads data directly from the SQLite database instead of CSV files.
"""

import sqlite3
from pathlib import Path

import pandas as pd
from dagster import asset, get_dagster_logger

# Get the project root
PROJECT_ROOT = Path(__file__).parent.parent
DB_PATH = PROJECT_ROOT / "linkedin_jobs.db"

logger = get_dagster_logger()


def get_db_connection():
    """Create a connection to the SQLite database."""
    if not DB_PATH.exists():
        raise FileNotFoundError(f"Database not found at {DB_PATH}")
    return sqlite3.connect(str(DB_PATH))


def get_table_as_dataframe(table_name: str) -> pd.DataFrame:
    """Load a table from the database as a DataFrame."""
    conn = get_db_connection()
    try:
        df = pd.read_sql_query(f"SELECT * FROM {table_name}", conn)
        logger.info(f"Loaded {len(df)} rows from {table_name}")
        return df
    finally:
        conn.close()


# ============================================================================
# DATABASE ASSETS - Load directly from SQLite
# ============================================================================

@asset
def jobs_db() -> pd.DataFrame:
    """Load jobs table from SQLite database."""
    logger.info(f"Loading jobs from {DB_PATH}")
    return get_table_as_dataframe("jobs")


@asset
def benefits_db() -> pd.DataFrame:
    """Load benefits table from SQLite database."""
    logger.info(f"Loading benefits from {DB_PATH}")
    return get_table_as_dataframe("benefits")


@asset
def companies_db() -> pd.DataFrame:
    """Load companies table from SQLite database."""
    logger.info(f"Loading companies from {DB_PATH}")
    return get_table_as_dataframe("companies")


@asset
def company_industries_db() -> pd.DataFrame:
    """Load company_industries table from SQLite database."""
    logger.info(f"Loading company_industries from {DB_PATH}")
    return get_table_as_dataframe("company_industries")


@asset
def company_specialities_db() -> pd.DataFrame:
    """Load company_specialities table from SQLite database."""
    logger.info(f"Loading company_specialities from {DB_PATH}")
    return get_table_as_dataframe("company_specialities")


@asset
def employee_counts_db() -> pd.DataFrame:
    """Load employee_counts table from SQLite database."""
    logger.info(f"Loading employee_counts from {DB_PATH}")
    return get_table_as_dataframe("employee_counts")


@asset
def industries_db() -> pd.DataFrame:
    """Load industries table from SQLite database."""
    logger.info(f"Loading industries from {DB_PATH}")
    return get_table_as_dataframe("industries")


@asset
def job_industries_db() -> pd.DataFrame:
    """Load job_industries table from SQLite database."""
    logger.info(f"Loading job_industries from {DB_PATH}")
    return get_table_as_dataframe("job_industries")


@asset
def job_skills_db() -> pd.DataFrame:
    """Load job_skills table from SQLite database."""
    logger.info(f"Loading job_skills from {DB_PATH}")
    return get_table_as_dataframe("job_skills")


@asset
def skills_db() -> pd.DataFrame:
    """Load skills table from SQLite database."""
    logger.info(f"Loading skills from {DB_PATH}")
    return get_table_as_dataframe("skills")


@asset
def salaries_db() -> pd.DataFrame:
    """Load salaries table from SQLite database."""
    logger.info(f"Loading salaries from {DB_PATH}")
    return get_table_as_dataframe("salaries")


# ============================================================================
# DATABASE STATS ASSET
# ============================================================================

@asset
def database_stats(
    jobs_db: pd.DataFrame,
    companies_db: pd.DataFrame,
    skills_db: pd.DataFrame,
    industries_db: pd.DataFrame,
    salaries_db: pd.DataFrame,
) -> dict:
    """
    Generate summary statistics about the database.
    Shows record counts and data freshness.
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    
    stats = {
        "total_jobs": len(jobs_db),
        "total_companies": len(companies_db),
        "total_skills": len(skills_db),
        "total_industries": len(industries_db),
        "total_salaries": len(salaries_db),
        "jobs_with_details": 0,
        "jobs_without_details": 0,
    }
    
    # Count scraped vs unscraped
    cursor.execute("SELECT COUNT(*) FROM jobs WHERE scraped = 1")
    stats["jobs_with_details"] = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM jobs WHERE scraped = 0")
    stats["jobs_without_details"] = cursor.fetchone()[0]
    
    conn.close()
    
    logger.info(f"Database Stats: {stats}")
    return stats
