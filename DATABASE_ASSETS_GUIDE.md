# SQLite Database Assets - Dagster Integration

## Overview

Your LinkedIn Job Scraper now uses **SQLite database assets** instead of CSV files. This is more efficient because:

✅ **Live data** - Always reads fresh data from the database  
✅ **No duplication** - No need to maintain separate CSV files  
✅ **Better relationships** - Preserves foreign key relationships  
✅ **Scalability** - Database queries are more efficient than file I/O  
✅ **Single source of truth** - `linkedin_jobs.db` is the only data source

---

## Database Assets (11 tables)

### Base Tables from SQLite

| Asset | Source Table | Purpose |
|-------|-------------|---------|
| `jobs_db` | jobs | Job postings with titles, descriptions, etc |
| `companies_db` | companies | Company profiles |
| `job_skills_db` | job_skills | Junction table: job_id + skill_abr |
| `skills_db` | skills | Master skills list |
| `job_industries_db` | job_industries | Junction table: job_id + industry_id |
| `industries_db` | industries | Master industries list |
| `salaries_db` | salaries | Salary data by job |
| `benefits_db` | benefits | Benefits by job |
| `company_industries_db` | company_industries | Company industry classifications |
| `company_specialities_db` | company_specialities | Company specialities |
| `employee_counts_db` | employee_counts | Company employee counts |

### Enhanced Linked Assets (9)

| Asset | Description |
|-------|-------------|
| `jobs_with_companies` | Jobs joined with company details |
| `jobs_with_skills` | Jobs with all their required skills |
| `jobs_with_industries` | Jobs with industry classifications |
| `jobs_with_salaries` | Jobs with salary information |
| `companies_with_industries` | Companies with their industries |
| `complete_job_data` | Full denormalized view: job + company + skills + salaries |
| `company_job_summary` | Aggregation: job count per company |
| `skill_demand_summary` | Aggregation: top skills by demand |
| `industry_job_summary` | Aggregation: job count per industry |

### Utility Assets

| Asset | Description |
|-------|-------------|
| `database_stats` | Summary statistics about the database |

---

## Usage Examples

### View Raw Database Assets

```bash
# View all jobs from database
python view_assets.py jobs_db

# View companies
python view_assets.py companies_db

# View skills
python view_assets.py skills_db
```

### View Linked Assets

```bash
# View jobs with their company details
python view_assets.py jobs_with_companies

# View complete job data (jobs + companies + skills + salaries)
python view_assets.py complete_job_data

# View job summary by company
python view_assets.py company_job_summary

# View top skills by demand
python view_assets.py skill_demand_summary
```

### Materialize in Dagster UI

1. Go to http://127.0.0.1:3000
2. Click **"Assets"** tab
3. Click **"Materialize All"**
4. Watch the lineage show how data flows

---

## Database Updates

When you run the scheduled jobs:

```bash
./run_jobs.sh search         # Adds new jobs to database
./run_jobs.sh details        # Fetches details for jobs
./run_jobs.sh both           # Both
```

The Dagster assets will **automatically pick up the new data** when materialized. No need to re-export CSVs!

---

## Architecture

```
linkedin_jobs.db (SQLite)
    ↓
    ├─ jobs table
    ├─ companies table
    ├─ job_skills table
    ├─ skills table
    ├─ industries table
    ├─ job_industries table
    ├─ salaries table
    ├─ benefits table
    └─ ...
    
    ↓
    
Dagster Database Assets (*_db)
    ├─ jobs_db
    ├─ companies_db
    ├─ job_skills_db
    ├─ skills_db
    └─ ...
    
    ↓
    
Linked/Enriched Assets
    ├─ jobs_with_companies
    ├─ complete_job_data
    ├─ skill_demand_summary
    └─ ...
    
    ↓
    
Materialized (pickled DataFrames)
    .dagster_home/storage/
```

---

## Next Steps

1. **Delete old CSV files** (optional):
   ```bash
   rm csv_files/*.csv  # Keep csv_files/ folder structure
   ```

2. **Update your analysis scripts** to use the new asset names:
   ```python
   # Old way
   jobs = pd.read_csv('csv_files/job_postings.csv')
   
   # New way - via Dagster
   jobs = load_dagster_asset('jobs_db')
   ```

3. **Schedule continuous updates**:
   ```bash
   # Enable schedules in Dagster UI, or run manually:
   ./run_jobs.sh search      # Every 6 hours
   ./run_jobs.sh details     # Every 4 hours
   ```

---

## Benefits

🚀 **Real-time data** - Database always has latest records  
🔄 **Automatic sync** - Scheduled jobs keep database fresh  
📊 **Better analysis** - No stale CSVs to worry about  
⚡ **Faster queries** - Database queries are optimized  
🔗 **Preserved relationships** - Foreign keys stay intact  
📈 **Scalability** - Handles millions of records efficiently

---

## Files Changed

- `scripts/dagster_db_assets.py` - NEW: Database asset definitions
- `scripts/dagster_relationships.py` - Updated: Uses database assets
- `scripts/definitions.py` - Updated: Loads database assets
- `scripts/dagster_assets.py` - DEPRECATED: Old CSV assets (can delete)

---

## Database Schema

Use this to understand the data structure:

```sql
-- View table schemas
.schema

-- Count records
SELECT 'jobs' as table_name, COUNT(*) as count FROM jobs
UNION ALL
SELECT 'companies', COUNT(*) FROM companies
UNION ALL
SELECT 'job_skills', COUNT(*) FROM job_skills
-- ... etc
```

Access SQLite directly:
```bash
sqlite3 linkedin_jobs.db
```
