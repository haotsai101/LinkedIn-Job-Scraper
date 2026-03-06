"""
Dagster ops and jobs for LinkedIn data retrieval and enrichment.
Converts search_retriever.py and details_retriever.py into schedulable tasks.
"""

import sqlite3
import random
from collections import deque
from pathlib import Path
import time

import pandas as pd
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
)

from scripts.create_db import create_tables
from scripts.database_scripts import insert_job_postings, insert_data
from scripts.fetch import JobSearchRetriever, JobDetailRetriever
from scripts.helpers import clean_job_postings

logger = get_dagster_logger()

# ============================================================================
# OPS - Individual tasks for data retrieval
# ============================================================================


@op(config_schema={"keywords": str, "pages_to_fetch": int})
def search_jobs_op(context) -> dict:
    """
    Search for LinkedIn jobs and insert new ones into database.
    
    Config:
        keywords: Search keywords (default: "software data")
        pages_to_fetch: Number of pages to search (default: 1)
    """
    keywords = context.op_config.get("keywords", "software data")
    pages_to_fetch = context.op_config.get("pages_to_fetch", 5)
    
    conn = sqlite3.connect("linkedin_jobs.db")
    cursor = conn.cursor()
    create_tables(conn, cursor)
    
    logger.info(f"🔍 Starting job search for keywords: {keywords}")
    
    job_searcher = JobSearchRetriever(keywords=keywords)
    total_new = 0
    total_new_non_sponsored = 0
    
    for page in range(1, pages_to_fetch + 1):
        logger.info(f"📄 Fetching page {page}...")
        
        try:
            all_results = job_searcher.get_jobs(page)
            
            # Check which jobs are already in DB
            query = "SELECT job_id FROM jobs WHERE job_id IN ({})".format(
                ','.join(['?'] * len(all_results))
            )
            cursor.execute(query, list(all_results.keys()))
            result = cursor.fetchall()
            result = [r[0] for r in result]
            
            # Insert only new jobs
            new_results = {
                job_id: job_info 
                for job_id, job_info in all_results.items() 
                if job_id not in result
            }
            insert_job_postings(new_results, conn, cursor)
            
            total_non_sponsored = len([x for x in all_results.values() if x['sponsored'] is False])
            new_non_sponsored = len([x for x in new_results.values() if x['sponsored'] is False])
            
            logger.info(
                f"✅ Page {page}: {len(new_results)}/{len(all_results)} NEW | "
                f"{new_non_sponsored}/{total_non_sponsored} NEW NON-PROMOTED"
            )
            
            total_new += len(new_results)
            total_new_non_sponsored += new_non_sponsored
            
        except Exception as e:
            logger.warning(f"⚠️ Error on page {page}: {e}")
            continue
    
    conn.close()
    
    result = {
        "total_new_jobs": total_new,
        "total_new_non_sponsored": total_new_non_sponsored,
        "pages_fetched": pages_to_fetch,
    }
    
    logger.info(f"✅ Job search complete: {result}")
    return result


@op(config_schema={"max_updates": int, "sleep_time": int})
def fetch_job_details_op(context) -> dict:
    """
    Fetch detailed information for jobs without details yet.
    
    Config:
        max_updates: Max jobs to update per run (default: 25)
        sleep_time: Sleep time between runs in seconds (default: 30)
    """
    max_updates = context.op_config.get("max_updates", 25)
    sleep_time = context.op_config.get("sleep_time", 30)
    
    conn = sqlite3.connect("linkedin_jobs.db")
    cursor = conn.cursor()
    create_tables(conn, cursor)
    
    logger.info(f"📋 Fetching details for up to {max_updates} jobs...")
    
    # Get jobs without details
    query = "SELECT job_id FROM jobs WHERE scraped = 0"
    cursor.execute(query)
    result = cursor.fetchall()
    result = [r[0] for r in result]
    
    if not result:
        logger.info("✅ All jobs have been scraped!")
        conn.close()
        return {
            "updated_count": 0,
            "remaining_jobs": 0,
            "status": "complete"
        }
    
    logger.info(f"Found {len(result)} jobs needing details")
    job_detail_retriever = JobDetailRetriever()

    # Fetch details for random sample
    total_size = len(result)
    while total_size > 0:
      sample_size = min(max_updates, total_size)
      
      try:
          details = job_detail_retriever.get_job_details(random.sample(result, sample_size))
          details = clean_job_postings(details)
          insert_data(details, conn, cursor)
          
          logger.info(f"✅ Updated {len(details)} jobs with details")
          
          # Remaining jobs
          cursor.execute("SELECT COUNT(*) FROM jobs WHERE scraped = 0")
          remaining = cursor.fetchone()[0]
          
          result_dict = {
              "updated_count": len(details),
              "remaining_jobs": remaining,
              "status": "in_progress" if remaining > 0 else "complete"
          }
          
      except Exception as e:
          logger.error(f"❌ Error fetching details: {e}")
          result_dict = {
              "updated_count": 0,
              "remaining_jobs": len(result),
              "status": "error",
              "error": str(e)
          }
      total_size -= sample_size
      logger.info(f"⏸️ Sleeping for {sleep_time} seconds before next run...")
      time.sleep(sleep_time)

    conn.close()
    return result_dict


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
