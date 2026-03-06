"""
Dagster definitions and jobs for LinkedIn Job Scraper.
"""

from dagster import Definitions, load_assets_from_modules

from . import dagster_db_assets, dagster_relationships
from .dagster_retrievers import (
    search_jobs_only,
    fetch_details_only,
    search_and_fetch_jobs,
    search_schedule,
    details_schedule,
    # combined_schedule,
    unscraped_jobs_sensor,
)
from .auto_materialize import auto_materialize_sensor, asset_refresh_job
from .no_persist_io_manager import no_persist_io_manager

defs = Definitions(
    assets=load_assets_from_modules([dagster_db_assets, dagster_relationships]),
    jobs=[search_jobs_only, fetch_details_only, search_and_fetch_jobs, asset_refresh_job],
    schedules=[search_schedule, details_schedule],
    sensors=[unscraped_jobs_sensor, auto_materialize_sensor],
    resources={
        "io_manager": no_persist_io_manager,  # Don't persist snapshots to disk
    },
)
