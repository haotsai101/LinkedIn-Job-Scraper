# Scheduled Jobs Guide - Dagster LinkedIn Job Scraper

## Overview

Your `search_retriever.py` and `details_retriever.py` have been converted into **Dagster ops and jobs** that can be scheduled to run automatically. This replaces the infinite `while True` loops with managed, observable workflows.

## 🚀 What's Available

### **Jobs** (Workflows)

1. **`search_jobs_only`** - Search for new jobs
   - Runs the job search logic from `search_retriever.py`
   - Returns: count of new jobs found
   - Config: keywords (default: "software data"), pages_to_fetch (default: 1)

2. **`fetch_details_only`** - Fetch details for unscraped jobs
   - Runs the details fetching from `details_retriever.py`
   - Returns: count of jobs updated, remaining jobs
   - Config: max_updates (default: 25), sleep_time (default: 30s)

3. **`search_and_fetch_jobs`** - Complete workflow
   - Runs search first, then fetch details
   - Combines both operations in sequence

### **Schedules** (Automatic Execution)

| Schedule | Frequency | Job | Status |
|----------|-----------|-----|--------|
| `search_schedule` | Every 6 hours | search_jobs_only | ✅ RUNNING |
| `details_schedule` | Every 4 hours | fetch_details_only | ✅ RUNNING |
| `combined_schedule` | Every 8 hours | search_and_fetch_jobs | ⏸️ STOPPED |

**Default timezone:** America/Denver

### **Sensors** (Data-Driven Triggers)

- **`unscraped_jobs_sensor`** - Automatically triggers `fetch_details_only` when:
  - More than 10 jobs without details exist
  - Monitors database state and decides whether to run

## 📋 How to Use

### **In Dagster UI (http://127.0.0.1:3000)**

#### View Jobs
1. Click the **"Jobs"** tab
2. Select any job to see:
   - Job runs history
   - Input/output logs
   - Configuration options

#### Run a Job Manually
1. Open a job (e.g., `search_jobs_only`)
2. Click **"Materialize"** button
3. (Optional) Edit config before running
4. Check the run status

#### View Schedules
1. Click **"Schedules"** tab
2. Toggle schedules ON/OFF
3. Click schedule to see:
   - Next run time
   - Past run history
   - Execution timezone

#### View Sensors
1. Click **"Sensors"** tab
2. See `unscraped_jobs_sensor` status
3. Check when it last triggered

### **Configuration Examples**

#### Search different keywords
In Dagster UI, when running `search_jobs_only`:
```yaml
ops:
  search_jobs_op:
    config:
      keywords: "data scientist machine learning"
      pages_to_fetch: 5
```

#### Fetch more details at once
When running `fetch_details_only`:
```yaml
ops:
  fetch_job_details_op:
    config:
      max_updates: 50
      sleep_time: 30
```

## ⏱️ Schedule Customization

Edit the cron schedules in `scripts/dagster_retrievers.py`:

```python
# Search for jobs every 6 hours (change to your preference)
search_schedule = ScheduleDefinition(
    job=search_jobs_only,
    cron_schedule="0 */6 * * *",  # Edit this
    ...
)
```

**Common cron patterns:**
- `0 * * * *` - Every hour
- `0 */4 * * *` - Every 4 hours
- `0 9,17 * * *` - 9 AM and 5 PM
- `0 0 * * 0` - Weekly on Sunday midnight
- `0 0 1 * *` - Monthly on the 1st

**Reference:** https://crontab.guru/

## 📊 Monitoring

### View Run History
1. Go to **"Runs"** tab
2. Filter by job name
3. Click on any run to see:
   - Duration
   - Success/failure status
   - Logs and output

### Check Database Growth
```bash
python -c "
import sqlite3
conn = sqlite3.connect('linkedin_jobs.db')
cursor = conn.cursor()

cursor.execute('SELECT COUNT(*) FROM jobs')
print(f'Total jobs: {cursor.fetchone()[0]}')

cursor.execute('SELECT COUNT(*) FROM jobs WHERE scraped = 1')
print(f'Scraped jobs: {cursor.fetchone()[0]}')
"
```

## 🔧 Advanced: Disable/Enable Schedules

### Via Dagster UI
1. Go to **"Schedules"** tab
2. Toggle the schedule ON/OFF

### Via Python/CLI
```bash
# List all schedules
dagster schedule list

# Start a schedule
dagster schedule start search_schedule

# Stop a schedule
dagster schedule stop search_schedule
```

## ⚠️ Important Notes

1. **Database**: All jobs use the same SQLite database (`linkedin_jobs.db`)
2. **Selenium**: Job details fetching requires Selenium/webdriver to be configured
3. **Rate Limiting**: Built-in sleep times prevent LinkedIn rate limiting
4. **Deduplication**: Jobs already in the database are skipped automatically

## 📈 Next Steps

1. **Start Dagster**: `DAGSTER_HOME=.dagster_home dagster dev`
2. **Enable schedules**: Go to Schedules tab, toggle ON
3. **Monitor runs**: Check Runs tab to see job execution
4. **Adjust configuration**: Edit cron schedules and op configs as needed

## 🐛 Troubleshooting

### Jobs not running on schedule?
- Check if schedule is enabled in Dagster UI
- Verify `dagster-daemon` is running (should be in background)
- Check timezone setting matches your location

### Jobs failing?
- Click the run to see logs
- Common issue: Selenium/LinkedIn credentials not configured
- Check `scripts/fetch.py` for authentication setup

### Database locked error?
- Close any other Python scripts using `linkedin_jobs.db`
- Only one script should write to database at a time

## 📚 Related Files
- `scripts/dagster_retrievers.py` - Job/schedule definitions
- `search_retriever.py` - Original search logic (archived)
- `details_retriever.py` - Original details logic (archived)
- `scripts/definitions.py` - Dagster entry point
