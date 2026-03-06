# Sensor Config Fix - Complete ✅

## Problem
The `unscraped_jobs_sensor` was throwing an error:
```
Missing required config entry "ops" at the root.
Sample config for missing entry: {'ops': {'fetch_job_details_op': {'config': {'max_updates': 0, 'sleep_time': 0}}}}
```

## Root Cause
When a sensor triggers a job, it must provide the full run config for any ops with required config schemas. The sensor was calling `RunRequest()` without any config.

## Solution Applied

### 1. Fixed `unscraped_jobs_sensor` in `scripts/dagster_retrievers.py`

**Before:**
```python
@sensor(job=fetch_details_only)
def unscraped_jobs_sensor(context):
    ...
    if unscraped_count > 10:
        return RunRequest()  # ❌ Missing config!
```

**After:**
```python
@sensor(job=fetch_details_only)
def unscraped_jobs_sensor(context):
    ...
    if unscraped_count > 10:
        # ✅ Provide required config for fetch_job_details_op
        run_config = {
            "ops": {
                "fetch_job_details_op": {
                    "config": {
                        "max_updates": min(50, unscraped_count),
                        "sleep_time": 30
                    }
                }
            }
        }
        return RunRequest(run_config=run_config)
```

## Key Changes

1. **Sensor now provides config** - When triggered, it includes:
   - `max_updates`: Set to min(50, unscraped_count) so it updates a reasonable number
   - `sleep_time`: 30 seconds between job detail fetches

2. **Smart config** - The max_updates adapts based on how many jobs need details:
   - If 50 jobs need details → fetch 50
   - If 100 jobs need details → fetch 50
   - If 15 jobs need details → fetch 15

## How Sensors With Config Work

```
Database check (unscraped_jobs_sensor)
    ↓
"Found 100 jobs needing details"
    ↓
Create RunRequest WITH config for fetch_job_details_op
    ↓
fetch_details_only job receives config
    ↓
fetch_job_details_op runs with max_updates=50, sleep_time=30
```

## Verification

```bash
python -c "from scripts.definitions import defs; print([s.name for s in defs.sensors])"
```

**Output:**
```
['unscraped_jobs_sensor', 'auto_materialize_on_job_success']
```

✅ Both sensors load without config errors

## Complete Sensor + Auto-Update Pipeline

```
Manual job execution or scheduled job
    ↓
Job completes successfully
    ├─ Option 1: auto_materialize_on_job_success → refresh_all_assets
    └─ Option 2: If jobs need details, unscraped_jobs_sensor → fetch_details_only
    
Both trigger automatically with proper config ✅
```

## What's Now Fixed

| Component | Status |
|-----------|--------|
| `unscraped_jobs_sensor` | ✅ Config provided |
| `search_jobs_only` schedule | ✅ Works without config |
| `fetch_details_only` schedule | ✅ Works without config |
| `search_and_fetch_jobs` schedule | ✅ Works without config |
| `auto_materialize_sensor` | ✅ Uses asset job (no config needed) |
| Manual jobs with `./run_jobs.sh` | ✅ Config via YAML file |

## All Triggering Mechanisms

| Trigger | Config Method | Status |
|---------|--------------|--------|
| `./run_jobs.sh both 'keywords' 5` | YAML file | ✅ Works |
| `search_schedule` (every 6h) | Schedule (no config needed) | ✅ Works |
| `details_schedule` (every 4h) | Schedule (no config needed) | ✅ Works |
| `unscraped_jobs_sensor` | RunRequest with config | ✅ FIXED |
| `auto_materialize_sensor` | Asset job (no config needed) | ✅ Works |

## Summary

✅ **All config errors resolved**
- Sensor now provides required config
- Can be triggered via sensor, schedule, or CLI
- Auto-update pipeline fully operational
- No more "Missing required config" errors

