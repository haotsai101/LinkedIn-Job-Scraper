# Dagster Data Versioning Guide

## Overview

Dagster tracks data through **asset definitions, materialization runs, and storage metadata**. Your LinkedIn Job Scraper uses a multi-layer versioning system combining:

1. **Asset versions** - Based on code/definition changes
2. **Materialization metadata** - Timestamped snapshots
3. **Database versioning** - SQLite as source of truth
4. **Run tracking** - Full execution history

---

## 1. Current Versioning Architecture

### A. Storage Backend
```yaml
# dagster.yaml - Where all data is stored
storage:
  sqlite:
    base_dir: /Users/zhihao/personal_projects/LinkedIn-Job-Scraper/.dagster_home
```

**Storage Structure:**
```
.dagster_home/
├── storage/
│   ├── benefits_db
│   ├── companies_db
│   ├── jobs_db
│   ├── [12 more database assets]
│   ├── jobs_with_companies
│   ├── jobs_with_skills
│   ├── [9 more relationship assets]
│   └── database_stats
└── logs/
    └── [execution logs]
```

**Each asset is a pickled DataFrame** stored with metadata about:
- Timestamp of materialization
- Asset key (path)
- Output type
- Lineage information

### B. Asset Definition Versioning

Your assets are defined in Python files. When code changes, Dagster detects it:

```python
# scripts/dagster_db_assets.py
@asset
def jobs_db() -> pd.DataFrame:
    """Load jobs table from SQLite database."""
    logger.info(f"Loading jobs from {DB_PATH}")
    return get_table_as_dataframe("jobs")
```

**How versioning works:**
- Asset key: `jobs_db`
- Source file: `scripts/dagster_db_assets.py`
- Code hash: Generated from the function definition
- When you modify the function → new version detected

### C. Run-Level Versioning

Every time you execute a job, Dagster creates a **Run** with:

```
Run ID: 29fbe908-9a76-4be8-946c-9bde6cdaa9ed
Job: search_and_fetch_jobs
Started: 2026-03-06 13:44:29
Status: SUCCESS
Steps: 2 (search_jobs_op, fetch_job_details_op)
Outputs: Stored in .dagster_home/storage/
```

---

## 2. How Your Data is Versioned

### Version 1: Asset Materialization (Snapshots)

When you run `dagster asset materialize -m scripts.definitions --select "*"`:

**Event Timeline:**
```
2026-03-06 12:36:47 - RUN_START: __ASSET_JOB started
2026-03-06 12:36:50 - benefits_db materialized (19,072 rows)
2026-03-06 12:36:50 - companies_db materialized (7,339 rows)
2026-03-06 12:36:51 - jobs_db materialized (23,034 rows)
... [all 21 assets]
2026-03-06 12:36:58 - RUN_SUCCESS: All assets up-to-date
```

**Storage created:**
```
.dagster_home/storage/benefits_db
.dagster_home/storage/companies_db
.dagster_home/storage/jobs_db
... (21 total asset outputs)
```

### Version 2: Job Execution Runs

Your jobs create timestamped runs:

```bash
# Run 1: Search + Fetch
Run ID: 29fbe908-9a76-4be8-946c-9bde6cdaa9ed
2026-03-06 13:44:29 - Started
2026-03-06 13:45:20 - search_jobs_op complete: 474 new jobs
2026-03-06 13:45:44 - fetch_job_details_op complete: 100 jobs enriched
Status: SUCCESS ✅

# Run 2: Would have a different Run ID
# Each run is independently tracked
```

### Version 3: SQLite Database Versioning

Your source of truth: `linkedin_jobs.db`

**Database contains:**
- `jobs` table - Master job listings
- `companies` table - Company information
- `job_skills` table - Job-skill relationships
- Assets load FROM this database (snapshot model)

**Current state:**
```sql
SELECT COUNT(*) FROM jobs;           -- 23,034 jobs
SELECT COUNT(*) FROM companies;      -- 7,339 companies
SELECT COUNT(*) FROM job_skills;     -- 37,598 relationships
```

---

## 3. Metadata Tracking

### A. Asset Metadata in Dagster

Each materialized asset stores:

```python
# Visible in Dagster UI or via API
Asset Key: ['jobs_db']
Timestamp: 2026-03-06T12:36:51.000Z
Data Type: DataFrame
Rows: 23,034
Columns: [id, job_url, title, company_id, ...]
Source: SQLite database (linkedin_jobs.db)
```

### B. Run Information

```
Run ID: 29fbe908-9a76-4be8-946c-9bde6cdaa9ed
User: system (dagster)
Status: SUCCESS
Duration: 1min 14sec
Assets Materialized: search_jobs_op, fetch_job_details_op
Outputs: 2 files stored
Errors: 0
```

### C. Lineage Tracking

Dagster automatically tracks **asset dependencies**:

```
jobs_db ──────────┐
companies_db ─────┼──> jobs_with_companies ──┐
                  │                           ├──> complete_job_data
job_skills_db ────┼──> jobs_with_skills ────┤
                  │                           │
job_industries_db ┴──> jobs_with_industries ─┘

salaries_db ──────────> jobs_with_salaries ──┐
                                              └──> complete_job_data
```

---

## 4. Versioning Best Practices (Current Setup)

### ✅ What's Working

1. **Immutable Snapshots** - Each materialization creates a new snapshot
2. **Timestamped Runs** - Every job execution has unique ID + timestamp
3. **Dependency Graph** - Lineage is automatically tracked
4. **SQL Source Control** - Database is version controlled through scraping ops
5. **Run History** - All past executions are queryable

### ⚠️ Limitations

1. **No explicit versioning tags** - Can't mark versions as "v1.0", "v2.0"
2. **No schema versioning** - If DataFrame columns change, no schema migration
3. **No data lineage replay** - Can't easily rollback to 2 weeks ago
4. **Manual asset refresh** - Must explicitly call materialize (no auto-versioning)

---

## 5. How to Query Versioning Information

### A. Check Asset History (CLI)

```bash
# View latest materialization
dagster asset list

# Check specific asset status
dagster asset show -m scripts.definitions

# See run history
dagster run list
```

### B. Programmatic Access

```python
from dagster import DagsterInstance

instance = DagsterInstance.get()

# Get all runs
runs = instance.get_runs(limit=10)
for run in runs:
    print(f"Run: {run.run_id}")
    print(f"Time: {run.start_time}")
    print(f"Status: {run.status}")

# Get specific asset
asset_events = instance.events_for_asset_key(['jobs_db'])
for event in asset_events:
    print(f"Materialized at: {event.timestamp}")
```

### C. Check Storage

```bash
# List all stored assets
ls -lah .dagster_home/storage/

# Check file sizes (data volume per version)
du -sh .dagster_home/storage/*

# View metadata
python -c "import pickle; data = pickle.load(open('.dagster_home/storage/jobs_db', 'rb')); print(f'Rows: {len(data)}')"
```

---

## 6. Versioning Timeline for Your Project

```
2026-03-06 12:36:47 - Initial asset materialization (21 assets)
├─ jobs_db v1: 23,034 rows
├─ companies_db v1: 7,339 rows
└─ ...

2026-03-06 13:44:29 - search_and_fetch_jobs Run #1
├─ Found 474 new jobs
├─ Updated 100 job details
└─ Database modified

[Next materialization needed to capture v2 of assets]
2026-03-06 XX:XX:XX - asset materialize (to capture new data)
├─ jobs_db v2: 23,508 rows (474 new)
├─ database_stats v2: updated counts
└─ All downstream assets recalculated
```

---

## 7. Enhanced Versioning Options (Optional)

### Option A: Add Version Tags
```python
from dagster import asset, AssetSelection

@asset
def jobs_db() -> pd.DataFrame:
    """Load jobs table from SQLite database."""
    df = get_table_as_dataframe("jobs")
    # Add version metadata
    row_count = len(df)
    print(f"jobs_db v{row_count // 1000}: {row_count} rows")
    return df
```

### Option B: Manual Versioning Snapshots
```bash
# Before running jobs, snapshot current state
mkdir -p version_snapshots/
cp -r .dagster_home/storage/ version_snapshots/v$(date +%Y%m%d_%H%M%S)/
# Run jobs
./run_jobs.sh both 'keywords' 5
# Optionally re-materialize and snapshot again
```

### Option C: Schema Versioning
Create a `data_schema.json` to track structure changes:
```json
{
  "version": "1.0",
  "timestamp": "2026-03-06T12:36:47Z",
  "assets": {
    "jobs_db": {
      "rows": 23034,
      "columns": ["id", "job_url", "title", "company_id", ...],
      "dtypes": {"id": "int64", "job_url": "object", ...}
    }
  }
}
```

---

## 8. Summary: Your Current Versioning

| Component | Current | How It Works |
|-----------|---------|------------|
| **Asset Versions** | ✅ Automatic | Code-based, stored snapshots |
| **Run Tracking** | ✅ Automatic | Every job execution tracked |
| **Data Snapshots** | ✅ Manual | Must call `dagster asset materialize` |
| **Database History** | ✅ Job ops | search/fetch jobs create audit trail |
| **Metadata** | ✅ Partial | Timestamps + lineage only |
| **Schema Versioning** | ❌ None | No schema change detection |
| **Rollback Capability** | ⚠️ Limited | Can only re-materialize or restore DB |

---

## 9. Recommended Workflow

**For production data versioning:**

```bash
# Step 1: Run jobs to update database
./run_jobs.sh both 'keywords' 5

# Step 2: Rematerialize assets to capture new data
dagster asset materialize -m scripts.definitions --select "*"

# Step 3: Optional - create version snapshot
cp -r .dagster_home/storage/ backups/snapshot_$(date +%Y%m%d)

# Step 4: Verify changes
python -c "from scripts.definitions import defs; stats = defs.assets[21]; print(stats)"  # database_stats
```

This approach ensures:
- ✅ Database is always updated
- ✅ Assets reflect latest data
- ✅ Historical snapshots available
- ✅ Lineage is traceable
- ✅ Runs are auditable

