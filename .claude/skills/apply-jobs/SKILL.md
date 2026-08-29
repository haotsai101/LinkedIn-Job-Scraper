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

---

## Apply flow

The project directory is `/Users/zhihao/personal_projects/LinkedIn-Job-Scraper`. All commands must run from there.

Capture any flags the user typed after `/apply-jobs` exactly as written — pass them through verbatim to the script. Always append `--verbose` to every command (unless the user explicitly passed it already, to avoid duplicates).

### Step 1 — Handle instant-exit flags

If the user passed `--stats` or `--reset-failed`, run via Bash tool and stop:

```bash
cd /Users/zhihao/personal_projects/LinkedIn-Job-Scraper && /opt/anaconda3/bin/python apply_jobs.py FLAGS_HERE --verbose
```

These flags exit immediately (no browser needed) — run via Bash tool. Do not proceed to Step 2.

### Step 2 — Show pending count (non-instant-exit only)

Run via Bash tool:

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

Use AskUserQuestion:

**Question:** "OffsiteApply jobs navigate directly to the company ATS (no LinkedIn click needed). EasyApply jobs still require a LinkedIn sign-in. Ready to start?"
**Options:**
- "Yes, start it"
- "Cancel"

If Cancel, stop here.

### Step 4 — Run the apply agent

**If `--auto` is present:** No interactive prompts — run via Bash tool with timeout=600000:

```bash
cd /Users/zhihao/personal_projects/LinkedIn-Job-Scraper && /opt/anaconda3/bin/python apply_jobs.py FLAGS_HERE --verbose
```

**If no `--auto` flag (semi-auto mode):** The script uses `input()` to pause at each job for confirmation. Claude's Bash tool has no connected keyboard stdin, so it must run in a real Terminal.app window. Launch via Bash tool with osascript:

```bash
osascript -e 'tell application "Terminal" to do script "cd /Users/zhihao/personal_projects/LinkedIn-Job-Scraper && python apply_jobs.py FLAGS_HERE --verbose"'
```

This opens a new Terminal.app window where the browser appears and the user can type ENTER (submit), `s` (skip), or `f` (LLM-fill focused field). Session results are written to `application_log.json` and `llm_debug.jsonl`.

### Step 5 — Monitor for LinkedIn sign-in (EasyApply only)

If the job queue contains EasyApply jobs (SimpleOnsiteApply / ComplexOnsiteApply), the browser will sign into LinkedIn. Use AskUserQuestion:

**Question:** "How did the LinkedIn sign-in go?"
**Options:**
- "Signed in — agent is running"
- "Waiting on 2FA / CAPTCHA — give me a moment"
- "Login failed / browser didn't open"
- "No EasyApply jobs in this run — skip"

If "Waiting on 2FA / CAPTCHA", use AskUserQuestion again:

**Question:** "Ready to continue?"
**Options:**
- "Done — agent is running now"
- "Abort"

If login failed or user aborts, stop and suggest they check `logins.csv` credentials.

### Step 6 — Session wrap-up

Tell the user the full log is at `logs/apply_jobs.log`. When the session finishes, offer to pull final stats.
