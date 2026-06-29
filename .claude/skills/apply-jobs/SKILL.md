---
name: apply-jobs
description: "Run the autonomous LinkedIn job application agent. Supports all apply_jobs.py flags passed after the slash command."
---

# /apply-jobs

Runs `apply_jobs.py` — the AI-powered job application agent. Reads enriched jobs (`scraped=1, applied IS NULL`), classifies relevance via LLM, and automates form-filling via Playwright for EasyApply and offsite career pages. All output is logged to `logs/apply_jobs.log`.

## Usage

```
/apply-jobs                          # Semi-auto: confirm before each submit
/apply-jobs --auto                   # Fully autonomous, no confirmation
/apply-jobs --auto --limit 5         # Autonomous, review at most 5 jobs
/apply-jobs --auto --max-apply 3     # Autonomous, submit at most 3 applications
/apply-jobs --stats                  # Print pending/applied/skipped/failed counts and exit
/apply-jobs --reset-failed           # Reset applied=-2 jobs back to pending and exit
/apply-jobs --setup                  # Re-run the user profile interview
/apply-jobs --type SimpleOnsiteApply,ComplexOnsiteApply  # EasyApply only
/apply-jobs --verbose                # Save debug screenshots + full LLM logs
```

## Flags reference

| Flag | Description |
|------|-------------|
| `--auto` | Submit without per-job confirmation |
| `--limit N` | Cap total jobs reviewed this session |
| `--max-apply N` | Cap total submissions this session (overrides MAX_AUTO_APPLY env var) |
| `--stats` | Print counts and exit immediately |
| `--reset-failed` | Mark all applied=-2 jobs back to NULL (pending) and exit |
| `--setup` | Re-run the interactive profile questionnaire |
| `--type TYPE` | Comma-separated filter: SimpleOnsiteApply, ComplexOnsiteApply, OffsiteApply |
| `--verbose` | Print full LLM prompts/responses; save per-step screenshots |

## What You Must Do When Invoked

The project directory is `/Users/zhihao/personal_projects/LinkedIn-Job-Scraper`. All commands must run from there.

Capture any flags the user typed after `/apply-jobs` exactly as written — pass them through verbatim to the script.

### Step 1 — Handle instant-exit flags

If the user passed `--stats` or `--reset-failed`, run immediately and stop after:

```bash
cd /Users/zhihao/personal_projects/LinkedIn-Job-Scraper && python3 apply_jobs.py FLAGS_HERE
```

Replace FLAGS_HERE with the flags the user provided. Do not proceed to Step 2.

### Step 2 — Show pending count (non-instant-exit only)

```bash
cd /Users/zhihao/personal_projects/LinkedIn-Job-Scraper && python3 -c "
import sqlite3
conn = sqlite3.connect('linkedin_jobs.db')
c = conn.cursor()
c.execute(\"SELECT COUNT(*) FROM jobs WHERE scraped>0 AND applied IS NULL\")
pending = c.fetchone()[0]
c.execute(\"SELECT COUNT(*) FROM jobs WHERE applied=1\")
applied = c.fetchone()[0]
c.execute(\"SELECT COUNT(*) FROM jobs WHERE applied=-1\")
skipped = c.fetchone()[0]
c.execute(\"SELECT COUNT(*) FROM jobs WHERE applied=-2\")
failed = c.fetchone()[0]
conn.close()
print(f'Pending: {pending} | Applied: {applied} | Skipped: {skipped} | Failed: {failed}')
"
```

### Step 3 — Confirm ready to start

Use AskUserQuestion with this exact question and options:

**Question:** "A Playwright browser will open and sign into LinkedIn (tsaizhihao@gmail.com). Watch for the browser — you may need to complete 2FA or CAPTCHA before the agent can proceed. Ready?"
**Options:**
- "Yes, I'm watching — start it"
- "Cancel"

If the user selects Cancel, stop here.

### Step 4 — Tell user to run the apply agent

Tell the user to run this in their terminal. In semi-auto mode the script will pause before each submit waiting for their input directly in the terminal.

```bash
cd /Users/zhihao/personal_projects/LinkedIn-Job-Scraper && mkdir -p logs && echo "=== $(date '+%Y-%m-%d %H:%M:%S') ===" >> logs/apply_jobs.log && python3 apply_jobs.py FLAGS_HERE 2>&1 | tee -a logs/apply_jobs.log
```

Replace FLAGS_HERE with the flags the user provided. If no flags were given, omit FLAGS_HERE (semi-auto mode).

### Step 5 — Wait for login confirmation

Use AskUserQuestion with this exact question and options:

**Question:** "How did the LinkedIn sign-in go?"
**Options:**
- "Signed in — agent is running"
- "Waiting on 2FA / CAPTCHA — give me a moment"
- "Login failed / browser didn't open"

If "Waiting on 2FA / CAPTCHA", use AskUserQuestion again:

**Question:** "Ready to continue?"
**Options:**
- "Done — agent is running now"
- "Abort"

If login failed or user aborts, stop and suggest they check `logins.csv` credentials.

### Step 6 — Session wrap-up

Tell the user the full log is at `logs/apply_jobs.log`. When they report the session is done, offer to pull final stats.
