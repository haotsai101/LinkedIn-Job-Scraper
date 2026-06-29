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

### Step 2 — Confirm ready to start

Use AskUserQuestion with this exact question and options:

**Question:** "A Chrome window will open and auto-sign into LinkedIn (tsaizhihao@gmail.com) to start the enrichment loop. Watch for the browser — you may need to handle 2FA or CAPTCHA. Ready?"
**Options:**
- "Yes, I'm watching — start it"
- "Cancel"

If the user selects Cancel, stop here.

### Step 3 — Tell user to run the enricher

Tell the user to run this command in their terminal. It loops indefinitely with 30s sleeps between batches — press Ctrl+C to stop.

```bash
cd /Users/zhihao/personal_projects/LinkedIn-Job-Scraper && mkdir -p logs && echo "=== $(date '+%Y-%m-%d %H:%M:%S') ===" >> logs/enrich_jobs.log && python3 details_retriever.py 2>&1 | tee -a logs/enrich_jobs.log
```

### Step 4 — Wait for login confirmation

Use AskUserQuestion with this exact question and options:

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

### Step 5 — Wait for enrichment to finish

Tell the user to press Ctrl+C when they have enough jobs enriched, then let you know. Full log is at `logs/enrich_jobs.log`.

### Step 6 — Show final stats after interruption

Once the user says they stopped it:

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
