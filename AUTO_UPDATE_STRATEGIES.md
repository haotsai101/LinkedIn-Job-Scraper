# Auto-Update Strategies for Dagster Assets

## Problem
Currently, assets are **manual snapshots**. After jobs update the database, you must manually run:
```bash
dagster asset materialize -m scripts.definitions --select "*"
```

## Solution: 3 Auto-Update Strategies

---

## Strategy 1: Auto-Materialize After Jobs (Recommended) ⭐

**Best for:** Your use case - automatically refresh assets after jobs complete

### Implementation

Create a new file: `scripts/auto_materialize.py`

```python
"""
Auto-materialize assets after job completion.
This ensures assets are always fresh after data updates.
"""

from dagster import (
    sensor,
    RunRequest,
    SkipReason,
    DefaultSensorStatus,
    DagsterEventType,
    get_dagster_logger,
)

logger = get_dagster_logger()


@sensor(
    job=None,  # Will materialize assets, not a job
    default_status=DefaultSensorStatus.RUNNING,
    name="auto_materialize_on_job_success"
)
def auto_materialize_sensor(context):
    """
    Automatically materialize all assets after search/fetch jobs complete.
    
    How it works:
    1. Listens for job completion events
    2. When search_jobs_only or fetch_details_only succeeds
    3. Triggers full asset materialization
    4. All 21 assets are refreshed with latest data
    """
    from dagster import DagsterInstance, DagsterEventType
    
    instance = DagsterInstance.get()
    
    # Get the last run in the instance
    all_runs = instance.get_runs(limit=1)
    
    if not all_runs:
        return SkipReason("No runs yet")
    
    last_run = all_runs[0]
    
    # Check if it's one of our data update jobs
    is_search_or_fetch = last_run.job_name in [
        "search_jobs_only",
        "fetch_details_only", 
        "search_and_fetch_jobs"
    ]
    
    # Check if it succeeded
    if is_search_or_fetch and last_run.status.name == "SUCCESS":
        # Check if we've already materialized for this run
        cursor = context.cursor or "0"
        
        if last_run.run_id != cursor:
            logger.info(f"🔄 Auto-materializing assets after job: {last_run.job_name}")
            
            # Materialize all assets
            from dagster import AssetSelection
            asset_selection = AssetSelection.all()
            
            context.update_cursor(last_run.run_id)
            
            return RunRequest(
                run_key=f"auto_materialize_{last_run.run_id}",
                tags={
                    "triggered_by": last_run.job_name,
                    "parent_run_id": last_run.run_id,
                    "auto_materialized": "true"
                }
            )
    
    return SkipReason("No successful job runs to trigger materialization")
```

### Update definitions.py

```python
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
    combined_schedule,
    unscraped_jobs_sensor,
)
from .auto_materialize import auto_materialize_sensor

defs = Definitions(
    assets=load_assets_from_modules([dagster_db_assets, dagster_relationships]),
    jobs=[search_jobs_only, fetch_details_only, search_and_fetch_jobs],
    schedules=[search_schedule, details_schedule, combined_schedule],
    sensors=[
        unscraped_jobs_sensor,
        auto_materialize_sensor,  # ← NEW: Auto-materialize after jobs
    ],
)
```

### How It Works

```
Timeline:
─────────────────────────────────────────────────────────────

[13:44:29] Run: search_and_fetch_jobs starts
   ├─ search_jobs_op: Find 474 new jobs
   └─ fetch_job_details_op: Enrich 100 jobs
   
[13:45:44] Job completes (SUCCESS) ✅

[13:45:45] auto_materialize_sensor detects success
   └─ Triggers asset materialization run
   
[13:46:00] Asset materialization starts
   ├─ jobs_db: 23,508 rows (was 23,034)
   ├─ companies_db: 7,339 rows (updated)
   ├─ database_stats: Updated counts
   └─ All linked assets recalculated
   
[13:46:30] Assets updated ✅
```

---

## Strategy 2: Scheduled Asset Materialization

**Best for:** Regular refresh cycles regardless of job execution

### Add to `scripts/definitions.py`

```python
from dagster import AssetSelection, define_asset_job, DefaultSensorStatus

# Define an asset materialization job
asset_refresh_job = define_asset_job(
    name="refresh_all_assets",
    selection=AssetSelection.all(),
    tags={"kind": "asset_refresh"}
)

# Schedule it to run every 2 hours
from croniter import croniter
from datetime import datetime

asset_refresh_schedule = schedule(
    job=asset_refresh_job,
    cron_schedule="0 */2 * * *",  # Every 2 hours
    default_status=DefaultSensorStatus.RUNNING,
)

# Update Definitions
defs = Definitions(
    assets=load_assets_from_modules([dagster_db_assets, dagster_relationships]),
    jobs=[
        search_jobs_only, 
        fetch_details_only, 
        search_and_fetch_jobs,
        asset_refresh_job,  # ← Add asset refresh job
    ],
    schedules=[
        search_schedule, 
        details_schedule, 
        combined_schedule,
        asset_refresh_schedule,  # ← Add asset refresh schedule
    ],
    sensors=[unscraped_jobs_sensor],
)
```

### Schedule Options

```
"0 */1 * * *"    # Every 1 hour
"0 */2 * * *"    # Every 2 hours
"0 */6 * * *"    # Every 6 hours (like search_schedule)
"0 0 * * *"      # Daily at midnight
"0 */4 * * *"    # Every 4 hours (like details_schedule)
```

---

## Strategy 3: Event-Driven with Custom Hook

**Best for:** Immediate updates with custom logic

### Create `scripts/hooks.py`

```python
"""
Dagster hooks for custom logic on job completion.
"""

from dagster import success_hook, get_dagster_logger
import subprocess
from datetime import datetime

logger = get_dagster_logger()


@success_hook
def refresh_assets_on_job_success(context):
    """
    Hook that runs when any job succeeds.
    Automatically refreshes assets if it was a data update job.
    """
    
    # Only refresh for data jobs
    data_jobs = ["search_jobs_only", "fetch_details_only", "search_and_fetch_jobs"]
    
    if context.job.name in data_jobs:
        logger.info(f"✅ Job '{context.job.name}' succeeded")
        logger.info(f"🔄 Auto-refreshing assets...")
        
        # Trigger asset materialization
        result = subprocess.run(
            ["dagster", "asset", "materialize", "-m", "scripts.definitions", "--select", "*"],
            capture_output=True,
            text=True
        )
        
        if result.returncode == 0:
            logger.info(f"✅ Assets refreshed successfully at {datetime.now()}")
        else:
            logger.error(f"❌ Asset refresh failed: {result.stderr}")
```

### Update jobs to use hook

```python
from .hooks import refresh_assets_on_job_success

@job(hooks={refresh_assets_on_job_success})
def search_jobs_only():
    """Search for jobs and auto-refresh assets."""
    search_jobs_op()

@job(hooks={refresh_assets_on_job_success})
def fetch_details_only():
    """Fetch job details and auto-refresh assets."""
    fetch_job_details_op()

@job(hooks={refresh_assets_on_job_success})
def search_and_fetch_jobs():
    """Search and fetch both, then auto-refresh."""
    details = fetch_job_details_op()
    search = search_jobs_op()
```

---

## Strategy 4: Advanced - Auto-Materialize Policy

**Best for:** Enterprise-level automatic freshness management (Dagster 1.1+)

### Update `scripts/dagster_db_assets.py`

```python
from dagster import asset, AutoMaterializePolicy, Backoff

@asset(
    auto_materialize_policy=AutoMaterializePolicy.eager(),
    backoff=Backoff(delay=300, max_retries=3)  # Retry every 5 minutes
)
def jobs_db() -> pd.DataFrame:
    """Load jobs table - automatically materialized when stale."""
    logger.info(f"Loading jobs from {DB_PATH}")
    return get_table_as_dataframe("jobs")

# Apply to all database assets
@asset(auto_materialize_policy=AutoMaterializePolicy.eager())
def companies_db() -> pd.DataFrame:
    """Load companies table - auto-refresh."""
    logger.info(f"Loading companies from {DB_PATH}")
    return get_table_as_dataframe("companies")
```

---

## Recommendation: Best Approach for You

**Combination of Strategy 1 + Strategy 2:**

```python
# scripts/definitions.py

from dagster import Definitions, load_assets_from_modules, define_asset_job, AssetSelection

from . import dagster_db_assets, dagster_relationships
from .dagster_retrievers import (
    search_jobs_only, fetch_details_only, search_and_fetch_jobs,
    search_schedule, details_schedule, combined_schedule,
    unscraped_jobs_sensor,
)
from .auto_materialize import auto_materialize_sensor

# Asset refresh job (runs every 2 hours as fallback)
asset_refresh_job = define_asset_job(
    name="refresh_all_assets",
    selection=AssetSelection.all(),
)

asset_refresh_schedule = schedule(
    job=asset_refresh_job,
    cron_schedule="0 */2 * * *",
    default_status=DefaultSensorStatus.RUNNING,
)

defs = Definitions(
    assets=load_assets_from_modules([dagster_db_assets, dagster_relationships]),
    jobs=[
        search_jobs_only,
        fetch_details_only,
        search_and_fetch_jobs,
        asset_refresh_job,
    ],
    schedules=[
        search_schedule,
        details_schedule,
        combined_schedule,
        asset_refresh_schedule,
    ],
    sensors=[
        unscraped_jobs_sensor,
        auto_materialize_sensor,  # Immediate refresh after jobs
    ],
)
```

### Behavior

```
Scenario 1: You run ./run_jobs.sh both 'keywords' 5
├─ Job completes at 13:45:44
├─ auto_materialize_sensor detects it
└─ Assets refresh immediately ✅

Scenario 2: Scheduled search job runs at 14:00
├─ Job completes (found 100 jobs)
├─ auto_materialize_sensor detects it
└─ Assets refresh immediately ✅

Scenario 3: No jobs ran recently
├─ asset_refresh_schedule runs every 2 hours
└─ Assets refresh on schedule ✅
```

---

## Implementation Steps

### Step 1: Create auto_materialize.py
Copy the Strategy 1 code into `scripts/auto_materialize.py`

### Step 2: Update definitions.py
Add the auto_materialize_sensor import and include in Definitions

### Step 3: Test It

```bash
# Start Dagster
dagster dev

# Run a job manually
./run_jobs.sh both 'test' 1

# Watch the UI - you should see:
# 1. search_and_fetch_jobs completes
# 2. auto_materialize_sensor triggers
# 3. Asset refresh job runs automatically
# 4. All 21 assets updated
```

### Step 4: Monitor

In Dagster UI:
- Go to **Jobs** → see `refresh_all_assets` running automatically
- Go to **Sensors** → see `auto_materialize_on_job_success` triggering runs
- Go to **Assets** → see materialization history

---

## Comparison Table

| Strategy | Trigger | Latency | Overhead | Best For |
|----------|---------|---------|----------|----------|
| **Manual** | `dagster asset materialize` | Manual | None | Dev/Testing |
| **Auto after jobs** | Job completion | ~10 sec | Low | Your use case ✅ |
| **Scheduled** | Cron (every 2h) | Up to 2h | Very low | Backup refresh |
| **Event hook** | Job success event | ~5 sec | Medium | Complex logic |
| **Auto-policy** | Staleness detection | ~1 min | High | Enterprise |

---

## Troubleshooting

### Assets not updating?
```bash
# Check sensor status
dagster sensor list

# Check if sensor is running
dagster dev  # Look for "auto_materialize_on_job_success" in logs

# Manually trigger
dagster asset materialize -m scripts.definitions --select "*"
```

### Too many materializations?
Adjust the sensor to only trigger on specific jobs:
```python
if last_run.job_name == "search_and_fetch_jobs":  # Only this job
    # Trigger materialization
```

### Assets materializing but not updating?
Ensure database is being updated by jobs:
```bash
# Check database directly
sqlite3 linkedin_jobs.db "SELECT COUNT(*) FROM jobs;"

# Check asset source code - make sure it reads from DB
cat scripts/dagster_db_assets.py
```

---

## Summary

**Yes! Auto-update is possible.** Choose the strategy based on your needs:

- 🚀 **Recommended:** Strategy 1 (Auto after jobs) + Strategy 2 (Scheduled fallback)
- ✅ **Immediate:** Assets refresh within 10 seconds of job completion
- 🔄 **Fallback:** Every 2 hours if jobs don't run
- 📊 **Monitoring:** All activities logged and visible in Dagster UI

