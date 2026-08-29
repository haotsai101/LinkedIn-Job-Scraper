---
name: enrich-jobs
description: "Run the LinkedIn job detail enricher to fetch full attributes for unenriched jobs (scraped=0) in linkedin_jobs.db."
---

# /enrich-jobs

Runs `details_retriever.py` to fetch full job details for every `scraped=0` row in `linkedin_jobs.db`. Processes random batches of up to 25 jobs per cycle with a 30-second sleep between cycles. Exits automatically when all jobs are enriched. All output is logged to `logs/enrich_jobs.log`.

## Usage

```
/enrich-jobs    # Start enrichment — exits when all jobs are done
```

## What You Must Do When Invoked

The project directory is `/Users/zhihao/personal_projects/LinkedIn-Job-Scraper`. All commands must run from there.

Follow these steps in order.

### Step 1 — Show unenriched count

Run via Bash tool:

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

### Step 2 — Confirm ready to start

Use AskUserQuestion:

**Question:** "A Chrome window will open and auto-sign into LinkedIn (tsaizhihao@gmail.com) to start enrichment. Watch for the browser — you may need to handle 2FA or CAPTCHA. Ready?"
**Options:**
- "Yes, I'm watching — start it"
- "Cancel"

If Cancel, stop here.

### Step 3 — Run the enricher via Bash tool

Run this via Bash tool with timeout=600000. It exits automatically when all jobs are enriched:

```bash
cd /Users/zhihao/personal_projects/LinkedIn-Job-Scraper && mkdir -p logs && echo "=== $(date '+%Y-%m-%d %H:%M:%S') ===" >> logs/enrich_jobs.log && /opt/anaconda3/bin/python details_retriever.py 2>&1 | tee -a logs/enrich_jobs.log
```

### Step 4 — Wait for login confirmation

While the script is starting up, use AskUserQuestion:

**Question:** "How did the LinkedIn sign-in go?"
**Options:**
- "Signed in — enricher is running"
- "Waiting on 2FA / CAPTCHA — give me a moment"
- "Login failed / browser didn't open"

If "Waiting on 2FA / CAPTCHA", use AskUserQuestion again:

**Question:** "Ready to continue?"
**Options:**
- "Done — enricher is running now"
- "Abort"

If login failed or user aborts, stop and suggest they check `logins.csv` credentials.

### Step 5 — Show final stats

After the script exits (prints "All jobs scraped. Done."), run via Bash tool:

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
