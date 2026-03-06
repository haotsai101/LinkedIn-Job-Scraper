# Asset Update Strategy

## Quick Answer

**No, assets don't auto-update.** They're snapshots of data at materialization time. But here's how to keep them fresh:

---

## How It Works 🔄

### Dataflow:
```
SQLite Database (live data) 
    ↓
Dagster Assets (snapshots taken at materialization time)
    ↓
Used for analysis/reporting
```

### Timeline:
1. **Database updates** - Your scheduled jobs update SQLite
2. **Assets stay stale** - They remain unchanged until you rematerialize
3. **Rematerialize** - Take a new snapshot from the database
4. **Assets refresh** - Now they have the latest data

---

## Update Strategy Options 

### **Option 1: Manual Refresh** (Simple & Direct)

After running a job that updates the database:

```bash
# Update database with new jobs
./run_jobs.sh search "data scientist" 5

# Take a fresh snapshot of all assets
dagster asset materialize -m scripts.definitions

# Or in Dagster UI: Assets → Materialize All
```

**When to use:** Small updates, testing, one-off refreshes

---

### **Option 2: Automated with Schedules** (Best for Production) ⭐

Your scheduled jobs automatically update the database on a schedule:

```
Schedule              Updates                Assets Stay
─────────────────────────────────────────────────────────
Search (6 hours)  → jobs_db table      → jobs_with_companies
Details (4 hours) → jobs table details → complete_job_data
```

**Setup:**
1. Dagster runs searches/details on schedule → updates SQLite
2. Manually rematerialize assets when needed, or
3. Create an automated asset materialization after jobs complete

**When to use:** Continuous data pipeline, production environment

---

### **Option 3: Hybrid Approach** (Recommended)

Use both strategies:

```
1. Scheduled jobs update database every 4-6 hours
2. Manually rematerialize assets after each job run
3. Or set up asset materialization trigger after jobs complete
```

---

## Best Practice Workflow 🎯

### Daily Workflow:

```bash
# 1. Run search jobs to update database
./run_jobs.sh search "software engineer" 3

# 2. Run details fetcher to enrich data
./run_jobs.sh details 50

# 3. Refresh all assets with new data
dagster asset materialize -m scripts.definitions

# 4. Your analyses now use fresh data
python analyze_jobs.py
```

### Weekly/Monthly:

Let schedules handle it automatically - assets get rematerialized when you query them.

---

## Asset Dependency Chain

When you materialize assets, they auto-refresh in dependency order:

```
jobs_db ──┐
          ├──> jobs_with_companies ──┐
companies_db ─┘                        ├──> complete_job_data
                                       ├──> company_job_summary
job_skills_db ──> jobs_with_skills ───┤
skills_db ────────────────────────────┘

salaries_db ──> jobs_with_salaries ────> complete_job_data
```

So materializing `complete_job_data` will automatically refresh all dependencies!

---

## Command Reference

### Refresh All Assets:
```bash
dagster asset materialize -m scripts.definitions
```

### Refresh Specific Assets:
```bash
dagster asset materialize -m scripts.definitions -a jobs_db companies_db
```

### In Dagster UI:
```
1. Go to http://127.0.0.1:3000
2. Assets tab → Click asset → Materialize
3. Or: Select multiple → Materialize All
```

---

## Monitoring Data Freshness

Check when assets were last updated:

```python
import pandas as pd
import sqlite3

# Check when jobs_db was last materialized
asset_path = Path(".dagster_home/storage/jobs_db")
if asset_path.exists():
    mtime = asset_path.stat().st_mtime
    print(f"Last updated: {pd.Timestamp.fromtimestamp(mtime)}")
```

---

## Summary

| Method | Freshness | Effort | Best For |
|--------|-----------|--------|----------|
| Manual after jobs | When you refresh | Low | Development/Testing |
| Scheduled only | Every 4-6 hours | Medium | Continuous pipeline |
| Triggered refresh | After each job | High | Production |

**Recommended:** Use scheduled jobs + manual rematerialization after important runs.
