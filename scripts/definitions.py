"""
Dagster definitions and jobs for LinkedIn Job Scraper.
"""

from dagster import Definitions

from .dagster_retrievers import (
    search_jobs_only,
    fetch_details_only,
    search_and_fetch_jobs,
    search_schedule,
    details_schedule,
    apply_jobs_job,
    apply_schedule,
    unscraped_jobs_sensor,
)
from .no_persist_io_manager import no_persist_io_manager

defs = Definitions(
    jobs=[search_jobs_only, fetch_details_only, search_and_fetch_jobs, apply_jobs_job],
    schedules=[search_schedule, details_schedule, apply_schedule],
    sensors=[unscraped_jobs_sensor],
    resources={
        # The search/details/apply ops all return values that the default io_manager
        # would pickle to $DAGSTER_HOME/storage on every run. This no-op manager
        # discards op outputs instead. Not asset-specific, so it stays after the
        # SDA assets removal.
        "io_manager": no_persist_io_manager,
    },
)
