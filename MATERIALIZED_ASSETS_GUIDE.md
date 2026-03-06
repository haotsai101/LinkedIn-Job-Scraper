# How to View Materialized Assets

## The Issue
You tried to view a materialized asset but got:
```
❌ File not found: /Users/zhihao/personal_projects/LinkedIn-Job-Scraper/csv_files/jobs_with_companies.csv
```

## The Solution
Materialized assets are **not CSV files**—they're stored in Dagster's storage system. The `view_assets.py` script has been updated to load them!

## Step-by-Step

### Step 1: Start Dagster
```bash
cd /Users/zhihao/personal_projects/LinkedIn-Job-Scraper
DAGSTER_HOME=.dagster_home dagster dev
```

### Step 2: Open the UI
Go to: `http://localhost:3000`

### Step 3: Materialize Assets
1. Click **Assets** tab
2. Select the assets you want (e.g., `jobs_with_companies`, `skill_demand_summary`)
3. Click **Materialize Selected**

**This will:**
- Load the data
- Join/process it
- Store in `.dagster_home/storage/`

### Step 4: View the Results
```bash
# In another terminal (keep Dagster running):
python view_assets.py jobs_with_companies
python view_assets.py skill_demand_summary
python view_assets.py complete_job_data
```

## Updated `view_assets.py` Capabilities

Now handles **both**:
- ✅ CSV files from `csv_files/`
- ✅ Materialized assets from `.dagster_home/storage/`

### List All Assets
```bash
python view_assets.py list
```

Shows:
- 📄 CSV files with sizes
- 🗄️ Materialized assets (if available)

### View CSV Asset
```bash
python view_assets.py job_postings    # Still works!
python view_assets.py companies
```

### View Materialized Asset
```bash
python view_assets.py jobs_with_companies     # Now works!
python view_assets.py skill_demand_summary    # Now works!
python view_assets.py complete_job_data       # Now works!
```

## Current Status

```
📄 CSV Assets (in csv_files/): ✅ Available
- job_postings (22,819 rows)
- companies (7,335 rows)
- ... 9 more

🗄️ Materialized Assets (in .dagster_home/storage/): ⏳ Not yet created
- Must materialize via Dagster UI first!
```

## Quick Workflow

```bash
# Terminal 1: Start Dagster
cd /Users/zhihao/personal_projects/LinkedIn-Job-Scraper
DAGSTER_HOME=.dagster_home dagster dev
# ↓ Keep this running!

# Terminal 2: View assets
cd /Users/zhihao/personal_projects/LinkedIn-Job-Scraper
python view_assets.py list                    # See what's available
python view_assets.py companies               # View CSV
# (Go materialize in UI, then:)
python view_assets.py jobs_with_companies     # View materialized
```

## What Happens When You Materialize

### Example: `jobs_with_companies`
```
1. You click "Materialize" in UI
2. Dagster:
   - Loads job_postings.csv
   - Loads companies.csv
   - Joins them: job.company_id = company.company_id
   - Stores result as pickle: .dagster_home/storage/jobs_with_companies
3. You run: python view_assets.py jobs_with_companies
4. Script:
   - Loads pickle from storage
   - Displays as pandas DataFrame
```

## Troubleshooting

### "Dagster storage not found"
- Run `DAGSTER_HOME=.dagster_home dagster dev` first
- Materialize at least one asset in the UI

### "ModuleNotFoundError: No module named 'scripts'"
- Make sure you're in project root: `pwd` shows `LinkedIn-Job-Scraper`
- Run from there

### "Asset not found"
- Asset hasn't been materialized yet
- Go to Dagster UI and click "Materialize"
- Dagster stores it in `.dagster_home/storage/`

## Files Changed

- `view_assets.py` - Updated to handle both CSV and materialized assets

## Next Steps

1. **Start Dagster**: `DAGSTER_HOME=.dagster_home dagster dev`
2. **Materialize some assets** in the UI (try `jobs_with_companies` first!)
3. **View results**: `python view_assets.py jobs_with_companies`
4. **Explore more**: Try other linked assets!

---

**Key Insight**: Materialized assets are stored as pickled DataFrames in Dagster's storage, not as CSV files. The updated script handles loading them automatically! 🚀
