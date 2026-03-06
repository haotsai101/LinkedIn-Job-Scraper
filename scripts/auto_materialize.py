"""
Auto-materialize assets after job completion.
This ensures assets are always fresh after data updates.

This sensor doesn't trigger jobs - it creates asset materialization runs.
For asset materialization, we define a separate asset materialization job.
"""

from dagster import (
    sensor,
    RunRequest,
    SkipReason,
    DefaultSensorStatus,
    get_dagster_logger,
    AssetSelection,
    define_asset_job,
)

logger = get_dagster_logger()

# Define asset materialization job (runs all assets)
asset_refresh_job = define_asset_job(
    name="refresh_all_assets",
    selection=AssetSelection.all(),
    tags={"kind": "asset_refresh", "auto_triggered": "true"}
)


@sensor(
    job=asset_refresh_job,
    default_status=DefaultSensorStatus.RUNNING,
    name="auto_materialize_on_job_success"
)
def auto_materialize_sensor(context):
    """
    Automatically materialize all assets after data update jobs complete.
    
    How it works:
    1. Listens for completion of data update jobs
    2. When search_jobs_only, fetch_details_only, or search_and_fetch_jobs succeeds
    3. Triggers the asset_refresh_job to materialize all 21 assets
    4. All assets are refreshed with latest data from SQLite
    
    Benefits:
    - Assets always in sync with database
    - No manual materialization needed
    - Data freshness guaranteed within seconds of job completion
    """
    from dagster import DagsterInstance
    
    try:
        instance = DagsterInstance.get()
        
        # Get the most recent run
        all_runs = instance.get_runs(limit=1)
        
        if not all_runs:
            return SkipReason("No runs yet")
        
        last_run = all_runs[0]
        
        # Check if it's one of our data update jobs
        data_update_jobs = [
            "search_jobs_only",
            "fetch_details_only",
            "search_and_fetch_jobs"
        ]
        
        is_data_job = last_run.job_name in data_update_jobs
        
        # Check if it succeeded
        job_succeeded = last_run.status.name == "SUCCESS"
        
        if is_data_job and job_succeeded:
            # Use cursor to avoid re-triggering the same run
            cursor = context.cursor or "0"
            
            if last_run.run_id != cursor:
                logger.info(
                    f"🔄 Auto-materializing assets after job completion: {last_run.job_name}"
                )
                
                # Update cursor to this run ID
                context.update_cursor(last_run.run_id)
                
                # Trigger asset materialization
                return RunRequest(
                    run_key=f"auto_materialize_{last_run.run_id}",
                    tags={
                        "triggered_by": last_run.job_name,
                        "parent_run_id": last_run.run_id,
                        "auto_materialized": "true"
                    }
                )
        
        return SkipReason(
            f"Skipping: job_name={last_run.job_name}, status={last_run.status.name}"
        )
        
    except Exception as e:
        logger.warning(f"Sensor error: {e}")
        return SkipReason(f"Error checking runs: {e}")
