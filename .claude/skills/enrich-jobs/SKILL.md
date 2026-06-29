---
name: enrich-jobs
description: "Run the LinkedIn job detail enricher to fetch full attributes for unenriched jobs (scraped=0) in linkedin_jobs.db."
---

# /enrich-jobs

Runs `details_retriever.py` to fetch full job details for every `scraped=0` row in `linkedin_jobs.db`. Processes random batches of up to 25 jobs per cycle with a 30-second sleep between cycles. Runs indefinitely — the user interrupts with Ctrl+C when done. All output is logged to `logs/enrich_jobs.log`.

## Usage

```
/enrich-jobs    # Start enrichment loop (Ctrl+C to stop)
```

## What You Must Do When Invoked

The project directory is `/Users/zhihao/personal_projects/LinkedIn-Job-Scraper`. All commands must run from there.

Follow these steps in order.

### Step 1 — Show unenriched count

```bash
cd /Users/zhihao/personal_projects/LinkedIn-Job-Scraper && python3 -c "
import sqlite3
conn = sqlite3.connect('linkedin_jobs.db')
c = conn.cursor()
c.execute('SELECT COUNT(*) FROM jobs WHERE scraped=0')
unenriched = c.fetchone()[0]
c.execute('SELECT COUNT(*) FROM jobs WHERE scraped>0')
enriched = c.fetchone()[0]
conn.close()
print(f'{unenriched} jobs need enrichment, {enriched} already enriched')
"
```

If unenriched count is 0, tell the user there is nothing to enrich and stop.

### Step 2 — Run the enricher

Tell the user the enricher is starting, will run in a loop (30s sleep between batches of 25), and output is being logged to `logs/enrich_jobs.log`. They can press Ctrl+C when they want to stop.

```bash
cd /Users/zhihao/personal_projects/LinkedIn-Job-Scraper && mkdir -p logs && echo "=== $(date '+%Y-%m-%d %H:%M:%S') ===" >> logs/enrich_jobs.log && python3 details_retriever.py 2>&1 | tee -a logs/enrich_jobs.log
```

### Step 3 — Show final stats after interruption

Once the script exits (via Ctrl+C or natural completion):

```bash
cd /Users/zhihao/personal_projects/LinkedIn-Job-Scraper && python3 -c "
import sqlite3
conn = sqlite3.connect('linkedin_jobs.db')
c = conn.cursor()
c.execute('SELECT COUNT(*) FROM jobs WHERE scraped=0')
unenriched = c.fetchone()[0]
c.execute('SELECT COUNT(*) FROM jobs WHERE scraped>0')
enriched = c.fetchone()[0]
conn.close()
print(f'Done: {unenriched} remaining unenriched, {enriched} fully enriched')
"
```

Also tell the user the full log is at `logs/enrich_jobs.log`.
