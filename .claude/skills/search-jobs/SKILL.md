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

### Step 2 — Confirm ready to start

Use AskUserQuestion with this exact question and options before doing anything else:

**Question:** "A Chrome window will open and auto-sign into LinkedIn (tsaizhihao@gmail.com). Watch for the browser — you may need to handle 2FA or CAPTCHA. Ready?"
**Options:**
- "Yes, I'm watching — start it" 
- "Cancel"

If the user selects Cancel, stop here.

### Step 3 — Tell user to run the scraper

Tell the user to run this command in their terminal:

```bash
cd /Users/zhihao/personal_projects/LinkedIn-Job-Scraper && mkdir -p logs && echo "=== $(date '+%Y-%m-%d %H:%M:%S') ===" >> logs/search_jobs.log && /opt/anaconda3/bin/python search_retriever.py 2>&1 | tee -a logs/search_jobs.log
```

### Step 4 — Wait for login confirmation

Use AskUserQuestion with this exact question and options:

**Question:** "How did the LinkedIn sign-in go?"
**Options:**
- "Signed in — scraper is running"
- "Waiting on 2FA / CAPTCHA — give me a moment"
- "Login failed / browser didn't open"

If "Waiting on 2FA / CAPTCHA", use AskUserQuestion again:

**Question:** "Ready to continue?"
**Options:**
- "Done — scraper is running now"
- "Abort"

If login failed or user aborts, stop and suggest they check `logins.csv` credentials.

### Step 5 — Wait for scraper to finish

Tell the user the scraper will stop automatically after 100 new jobs (or they can press Ctrl+C). Ask them to let you know when it's done.

### Step 6 — Show post-run stats

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

Report the delta (how many new jobs were discovered). Full log is at `logs/search_jobs.log`.
