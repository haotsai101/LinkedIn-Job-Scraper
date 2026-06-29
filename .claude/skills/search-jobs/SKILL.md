---
name: search-jobs
description: "Run the LinkedIn job search scraper to discover new job postings and insert them into linkedin_jobs.db."
---

# /search-jobs

Runs `search_retriever.py` to scrape LinkedIn for new job postings (keywords: software engineer AI ML, locations: remote + Utah) and inserts them into `linkedin_jobs.db`.

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

Tell the user the scraper is starting. It will print progress as it finds jobs and will stop after 100 new insertions (TARGET=100) or when the user interrupts with Ctrl+C.

```bash
cd /Users/zhihao/personal_projects/LinkedIn-Job-Scraper && python3 search_retriever.py
```

### Step 3 — Show post-run stats

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

Report the delta to the user (how many new jobs were discovered this run).
