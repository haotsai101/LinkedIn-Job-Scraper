"""
Dagster ops and jobs for LinkedIn data retrieval and enrichment.
Converts search_retriever.py and details_retriever.py into schedulable tasks.
"""

import json
import sqlite3
import subprocess
from pathlib import Path

from dagster import (
    op,
    job,
    schedule,
    ScheduleDefinition,
    DefaultScheduleStatus,
    DefaultSensorStatus,
    sensor,
    RunRequest,
    SkipReason,
    get_dagster_logger,
    Field,
    Int,
    MetadataValue,
)

from scripts.create_db import ensure_db_ready
from scripts.retrieval import run_detail_enrichment, run_search
from scripts.search_config import SEARCH_KEYWORDS

logger = get_dagster_logger()

# ============================================================================
# OPS - Individual tasks for data retrieval
# ============================================================================


@op(
    config_schema={
        "keywords": Field(
            str,
            default_value=SEARCH_KEYWORDS,
            description="LinkedIn Voyager search query. Defaults to the shared "
            "SEARCH_KEYWORDS constant in scripts/search_config.py.",
        ),
        "pages_to_fetch": Field(
            Int, default_value=5,
            description="Max rounds to fetch. One round = one result page from "
            "each search config (remote + Utah).",
        ),
    }
)
def search_jobs_op(context) -> dict:
    """
    Search for LinkedIn jobs and insert new ones into the database.

    Thin wrapper over ``scripts.retrieval.run_search`` (shared with the
    standalone ``search_retriever.py``).

    Config:
        keywords: Search keywords (default: shared SEARCH_KEYWORDS)
        pages_to_fetch: Max rounds to fetch (default: 5)
    """
    keywords = context.op_config.get("keywords", SEARCH_KEYWORDS)
    pages_to_fetch = context.op_config.get("pages_to_fetch", 5)

    conn = sqlite3.connect("linkedin_jobs.db")
    cursor = conn.cursor()
    ensure_db_ready(conn, cursor)

    logger.info(f"🔍 Starting job search for keywords: {keywords}")

    # target=None: the op fetches a fixed number of rounds rather than chasing a
    # new-job count (that's the standalone script's mode).
    result = run_search(
        conn,
        cursor,
        keywords=keywords,
        target=None,
        max_rounds=pages_to_fetch,
        log=logger.info,
    )

    conn.close()
    logger.info(f"✅ Job search complete: {result}")
    return result


@op(
    config_schema={
        "max_updates": Field(
            Int,
            default_value=25,
            description="Max jobs to update per run.",
        ),
        "sleep_time": Field(
            Int,
            default_value=30,
            description="Sleep time between batches in seconds.",
        ),
    }
)
def fetch_job_details_op(context) -> dict:
    """
    Fetch detailed information for jobs without details yet.

    Thin wrapper over ``scripts.retrieval.run_detail_enrichment`` (shared with
    the standalone ``details_retriever.py``).

    Config:
        max_updates: Max jobs to update per batch (default: 25)
        sleep_time: Sleep time between batches in seconds (default: 30)
    """
    max_updates = context.op_config.get("max_updates", 25)
    sleep_time = context.op_config.get("sleep_time", 30)

    conn = sqlite3.connect("linkedin_jobs.db")
    cursor = conn.cursor()
    ensure_db_ready(conn, cursor)

    logger.info(f"📋 Enriching scraped=0 jobs, {max_updates} at a time...")

    try:
        result = run_detail_enrichment(
            conn,
            cursor,
            max_updates=max_updates,
            sleep_time=sleep_time,
            log=logger.info,
        )
    except Exception as e:
        logger.error(f"❌ Error fetching details: {e}")
        cursor.execute("SELECT COUNT(*) FROM jobs WHERE scraped = 0")
        remaining = cursor.fetchone()[0]
        conn.close()
        return {
            "updated_count": 0,
            "remaining_jobs": remaining,
            "status": "error",
            "error": str(e),
        }

    conn.close()
    logger.info(f"✅ Detail enrichment complete: {result}")
    return result


# ============================================================================
# JOBS - Combine ops into workflows
# ============================================================================


@job
def search_and_fetch_jobs():
    """Complete workflow: search for jobs and fetch their details."""
    search_jobs_op()
    fetch_job_details_op()


@job
def search_jobs_only():
    """Just search for jobs without fetching details."""
    search_jobs_op()


@job
def fetch_details_only():
    """Just fetch details for jobs that need them."""
    fetch_job_details_op()


# ============================================================================
# SCHEDULES - Run on a schedule
# ============================================================================

# ============================================================================
# SCHEDULES - Run on a schedule
# ============================================================================

# Search for jobs every 12 hours
search_schedule = ScheduleDefinition(
    job=search_jobs_only,
    cron_schedule="0 */12 * * *",  # Every 12 hours
    execution_timezone="America/Denver",
    default_status=DefaultScheduleStatus.RUNNING,
)

# Fetch job details every 12 hours but 10 mins after search to allow new jobs to be added
details_schedule = ScheduleDefinition(
    job=fetch_details_only,
    cron_schedule="10 */12 * * *",  # Every 12 hours at 10 mins past the hour
    default_status=DefaultScheduleStatus.RUNNING,
)

# # Combined workflow every 12 hours
# combined_schedule = ScheduleDefinition(
#     job=search_and_fetch_jobs,
#     cron_schedule="0 */12 * * *",  # Every 12 hours
#     execution_timezone="America/Denver",
#     default_status=DefaultScheduleStatus.STOPPED,  # Disabled by default, enable if you want
# )


# ============================================================================
# SENSOR - Trigger based on new jobs without details
# ============================================================================


@op(
    config_schema={
        "max_apply": Field(Int, default_value=10, description="Max applications to submit per session."),
        "limit":     Field(Int, default_value=100, description="Max jobs to classify per session."),
    }
)
def apply_jobs_op(context) -> dict:
    """
    Run apply_jobs.py --auto as a subprocess.

    Opens a real browser window (requires a display — works natively on macOS).
    Reads application_log.json afterward and exposes counts as asset metadata.
    """
    max_apply = context.op_config["max_apply"]
    limit     = context.op_config["limit"]

    project_root = Path(__file__).parent.parent
    cmd = [
        "python", "apply_jobs.py",
        "--auto",
        "--max-apply", str(max_apply),
        "--limit",     str(limit),
    ]

    logger.info(f"Starting apply_jobs.py --auto --max-apply {max_apply} --limit {limit}")
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(project_root))

    if result.stdout:
        logger.info(result.stdout)
    if result.returncode != 0 and result.stderr:
        logger.error(result.stderr)

    log_path = project_root / "application_log.json"
    report: dict = {}
    if log_path.exists():
        try:
            data = json.loads(log_path.read_text())
            sessions = data.get("sessions", [])
            if sessions:
                report = sessions[-1]  # most recent session
        except Exception as exc:
            logger.warning(f"Could not parse application_log.json: {exc}")

    context.add_output_metadata({
        "applied": MetadataValue.int(report.get("applied_count", 0)),
        "skipped": MetadataValue.int(report.get("skipped_count", 0)),
        "blocked": MetadataValue.int(report.get("blocked_count", 0)),
        "errors":  MetadataValue.int(report.get("error_count", 0)),
        "date":    MetadataValue.text(report.get("date", "")),
    })

    logger.info(
        f"Session complete — applied: {report.get('applied_count', 0)}, "
        f"skipped: {report.get('skipped_count', 0)}, "
        f"blocked: {report.get('blocked_count', 0)}, "
        f"errors: {report.get('error_count', 0)}"
    )
    return report


@job
def apply_jobs_job():
    """Autonomous job application: classify, generate cover letters, and submit."""
    apply_jobs_op()


# Daily schedule: runs apply_jobs.py at 10 AM Mountain every day
apply_schedule = ScheduleDefinition(
    job=apply_jobs_job,
    cron_schedule="0 10 * * *",
    execution_timezone="America/Denver",
    default_status=DefaultScheduleStatus.STOPPED,  # Enable manually when ready
    name="daily_apply_schedule",
)


@sensor(job=fetch_details_only)
def unscraped_jobs_sensor(context) -> RunRequest | SkipReason:
    """
    Trigger job detail fetching when there are unscraped jobs.
    Checks database for jobs with scraped=0.
    """
    try:
        conn = sqlite3.connect("linkedin_jobs.db")
        cursor = conn.cursor()
        
        cursor.execute("SELECT COUNT(*) FROM jobs WHERE scraped = 0")
        unscraped_count = cursor.fetchone()[0]
        conn.close()

        if unscraped_count > 0:  # Only trigger if more than 0 jobs need details
            logger.info(f"🔔 Found {unscraped_count} unscraped jobs, triggering fetch...")
            
            # Provide config for the job to avoid missing required config error
            run_config = {
                "ops": {
                    "fetch_job_details_op": {
                        "config": {
                            "max_updates": min(50, unscraped_count),  # Update up to 50 jobs per run
                            "sleep_time": 30
                        }
                    }
                }
            }
            
            return RunRequest(run_config=run_config)
        else:
            return SkipReason(f"Only {unscraped_count} unscraped jobs, waiting for more...")
            
    except Exception as e:
        logger.warning(f"Sensor error: {e}")
        return SkipReason("Database error")
