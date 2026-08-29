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
    # combined_schedule,
    unscraped_jobs_sensor,
)
from .no_persist_io_manager import no_persist_io_manager

defs = Definitions(
    jobs=[search_jobs_only, fetch_details_only, search_and_fetch_jobs, apply_jobs_job],
    schedules=[search_schedule, details_schedule, apply_schedule],
    sensors=[unscraped_jobs_sensor],
    resources={
        # Op outputs (search/details/apply ops all return values) would otherwise be
        # pickled to $DAGSTER_HOME/storage on every run. This no-op manager keeps them
        # in memory only. Not asset-specific, so it stays after the SDA assets removal.
        "io_manager": no_persist_io_manager,
    },
)
