---
name: search-jobs
description: "Run the LinkedIn job search scraper to discover new job postings and insert them into linkedin_jobs.db."
---

# /search-jobs

Runs `search_retriever.py` to scrape LinkedIn for new job postings (keywords: software engineer AI ML, locations: remote + Utah) and inserts them into `linkedin_jobs.db`. All output is logged to `logs/search_jobs.log`.

## Usage

```
/search-jobs    # Run the scraper with default config
```

## What You Must Do When Invoked

The project directory is `/Users/zhihao/personal_projects/LinkedIn-Job-Scraper`. All commands must run from there.

Follow these steps in order.

### Step 1 — Show pre-run stats

```bash
cd /Users/zhihao/personal_projects/LinkedIn-Job-Scraper && python3 -c "
import sqlite3
conn = sqlite3.connect('linkedin_jobs.db')
c = conn.cursor()
c.execute('SELECT COUNT(*) FROM jobs')
total = c.fetchone()[0]
c.execute('SELECT COUNT(*) FROM jobs WHERE scraped=0')
unenriched = c.fetchone()[0]
conn.close()
print(f'Before: {total} total jobs, {unenriched} unenriched (scraped=0)')
"
```

### Step 2 — Run the scraper

Tell the user the scraper is starting and output is being logged to `logs/search_jobs.log`. It will print progress as it finds jobs and will stop after 100 new insertions (TARGET=100) or when the user interrupts with Ctrl+C.

Note: `search_retriever.py` requires Selenium. If `python3` doesn't have it, use `/opt/anaconda3/bin/python`.

**Human input required:** the script opens a Chrome window for LinkedIn login. Prompt the user to run this themselves in their terminal:

```bash
cd /Users/zhihao/personal_projects/LinkedIn-Job-Scraper && mkdir -p logs && echo "=== $(date '+%Y-%m-%d %H:%M:%S') ===" >> logs/search_jobs.log && /opt/anaconda3/bin/python search_retriever.py 2>&1 | tee -a logs/search_jobs.log
```

### Step 3 — Show post-run stats

Once the user says it finished, run:

```bash
cd /Users/zhihao/personal_projects/LinkedIn-Job-Scraper && python3 -c "
import sqlite3
conn = sqlite3.connect('linkedin_jobs.db')
c = conn.cursor()
c.execute('SELECT COUNT(*) FROM jobs')
total = c.fetchone()[0]
c.execute('SELECT COUNT(*) FROM jobs WHERE scraped=0')
unenriched = c.fetchone()[0]
conn.close()
print(f'After: {total} total jobs, {unenriched} unenriched (scraped=0)')
"
```

Report the delta to the user (how many new jobs were discovered this run). Also tell them the full log is at `logs/search_jobs.log`.
