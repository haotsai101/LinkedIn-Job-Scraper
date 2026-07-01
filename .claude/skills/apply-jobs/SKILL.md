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
/apply-jobs --haiku [--limit N]      # Haiku agent mode (see section below)
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
| `--haiku` | Use Haiku agent (chrome-in-chrome) for OffsiteApply; Playwright for EasyApply |

---

## Haiku Agent Mode (`--haiku`)

When `--haiku` is passed, skip the standard Playwright flow entirely and use this path instead. **Do NOT execute the standard Steps 1–6 below.** Execute Steps H1–H7 in this section.

**Architecture:**
- OffsiteApply jobs → Haiku subagent via Agent tool with chrome-in-chrome MCP tools.
  The `application_url` column in the DB already contains the direct external URL — no LinkedIn navigation needed.
- EasyApply jobs (SimpleOnsiteApply, ComplexOnsiteApply) → separate `apply_jobs.py` run (Playwright, login required).

### H1 — Stats

```bash
cd /Users/zhihao/personal_projects/LinkedIn-Job-Scraper && python apply_haiku.py stats
```

Print the JSON result to the user.

### H2 — Read profile

```bash
cd /Users/zhihao/personal_projects/LinkedIn-Job-Scraper && cat user_profile.json
```

Store this profile JSON in memory — you will inject it verbatim into every Haiku agent prompt.

### H3 — Confirm

Use AskUserQuestion:

**Question:** "Haiku agent mode will process OffsiteApply jobs using a Haiku subagent with Chrome tab automation. No Playwright login needed for external sites. Ready?"
**Options:**
- "Start — process OffsiteApply jobs"
- "Also process EasyApply jobs after (needs LinkedIn login)"
- "Cancel"

If Cancel, stop here. Note whether to include EasyApply.

### H4 — Get OffsiteApply job list

Extract `--limit N` from user's flags (default 5 if not specified).

```bash
cd /Users/zhihao/personal_projects/LinkedIn-Job-Scraper && python apply_haiku.py list --limit LIMIT_HERE --type OffsiteApply
```

Parse the JSON array. For each job, you will spawn one Haiku agent sequentially (one at a time).

### H5 — Process each OffsiteApply job via Haiku agent

For each job in the list, use the Agent tool to spawn a Haiku subagent. Process jobs **one at a time** (wait for each agent before spawning the next). Use the following prompt template, filling in job details and profile:

---

**Agent prompt template:**

```
You are a job application assistant. Your task is to apply to one job on behalf of Zhi-Hao Tsai ("Ty").

## Your tools
First, load all browser tools by calling ToolSearch with:
  query: "select:mcp__claude-in-chrome__tabs_context_mcp,mcp__claude-in-chrome__navigate,mcp__claude-in-chrome__computer,mcp__claude-in-chrome__read_page,mcp__claude-in-chrome__tabs_create_mcp,mcp__claude-in-chrome__javascript_tool,mcp__claude-in-chrome__form_input,mcp__claude-in-chrome__get_page_text"

## Job details
- Job ID: JOB_ID_HERE
- Title: JOB_TITLE_HERE
- Company / domain: POSTING_DOMAIN_HERE
- Location: JOB_LOCATION_HERE
- Level: JOB_LEVEL_HERE
- Remote: JOB_REMOTE_HERE
- Application URL: APPLICATION_URL_HERE
- Description (truncated):
JOB_DESCRIPTION_HERE

## Applicant profile
PROFILE_JSON_HERE

## Step 1 — Classify relevance

Decide if this job is relevant to Ty's profile. It is relevant if:
- It involves software engineering, data engineering, ML engineering, or AI
- Requires skills Ty has (Go, Python, TypeScript, AWS, Kubernetes, LLM/RAG, etc.)
- Is remote or located in Utah
- He can do it on OPT (needs sponsorship later is OK)

It is NOT relevant if:
- Primarily a sales, marketing, finance, or operations role
- Requires active security clearance
- Requires 8+ years of experience in a very specific domain he lacks

If NOT relevant, end your response immediately with exactly:
  RESULT: skipped
  REASON: <one sentence why>

## Step 2 — Open the application URL

Create a new Chrome tab and navigate to APPLICATION_URL_HERE.

## Step 3 — Handle landing page

Read the page. Possible states:
- **Application form** — proceed to Step 4.
- **"Create account" / "Sign in" gate** — If you can create an account using Ty's email (tytsai26@gmail.com) and a password (use "Ty@secure2024!"), do so. Otherwise end with RESULT: failed REASON: login/account wall.
- **CAPTCHA or bot challenge** — end with RESULT: failed REASON: CAPTCHA blocked.
- **Job no longer available / 404** — end with RESULT: skipped REASON: posting removed.

## Step 4 — Fill the application form

Fill all visible form fields using the profile data above. Rules:
- Name: use "Ty Tsai" as preferred name, "Zhi-Hao Tsai" as legal name if asked separately.
- Email: tytsai26@gmail.com
- Phone: 2086007012 (format as 208-600-7012 if field requires dashes)
- Location: American Fork, Utah, 84003, United States
- Resume: upload from /Users/zhihao/personal_projects/LinkedIn-Job-Scraper/media/Resume-Zhi-Hao-Tsai.pdf if there is a file upload field (use the file_upload tool).
- LinkedIn: https://www.linkedin.com/in/zhi-hao-tsai-14619a141/
- GitHub: https://github.com/haotsai101
- Work authorization: OPT (no sponsorship needed currently, will need in the future — if forced yes/no on "authorized to work in US", answer Yes)
- Sponsorship: if asked "will you require sponsorship now", answer No. If asked "will you require sponsorship in the future", answer Yes.
- EEO fields (race, gender, disability, veteran): choose "prefer not to disclose" or equivalent.
- Yes/No experience questions: answer Yes if Ty has ≥1 year of the stated skill, else No.
- Salary: if asked, enter 110000 (or "110,000" as formatted).
- Open-ended "Why do you want to work here?" / cover letter: write 2-3 sentences about Ty's relevant skills and interest in the role based on his summary and the job description. Keep it honest and specific.
- If a required field is unclear, make a reasonable professional choice — do not leave required fields blank.

Use mcp__claude-in-chrome__form_input for text/select fields and mcp__claude-in-chrome__computer for clicking checkboxes, radio buttons, and file upload triggers.

After each page/step, read the page again to check for validation errors. Fix any errors before proceeding.

## Step 5 — Submit

Click the final submit button. Wait for a confirmation message ("Thank you", "Application submitted", "We've received your application", etc.). Take a screenshot via mcp__claude-in-chrome__computer if unsure.

## Step 6 — Report result

Before the final RESULT line, output a structured answers block so the record can be saved:

```
ANSWERS SUBMITTED:
- Full name: <value>
- Email: <value>
- Phone: <value>
- Location: <value>
- Resume uploaded: yes/no
- LinkedIn: <value>
- GitHub: <value>
- Work authorization answer: <value>
- Sponsorship now: <value>
- Sponsorship future: <value>
- Salary: <value>
- EEO fields: declined/answered
- Open-ended questions answered:
  Q: <question text>
  A: <your answer>
  ... (repeat for each)
- Any other notable fields: <field>: <value>
```

Then end with EXACTLY one of:
  RESULT: applied
  RESULT: skipped
  REASON: <one sentence>
  RESULT: failed
  REASON: <one sentence>
```

---

After each agent completes, parse its output for `RESULT: applied|skipped|failed`.

**If RESULT: applied**, extract the full `ANSWERS SUBMITTED:` block from the agent's response and run:

```bash
cd /Users/zhihao/personal_projects/LinkedIn-Job-Scraper && python apply_haiku.py log JOB_ID_HERE "JOB_TITLE_HERE" "COMPANY_HERE" "APPLICATION_URL_HERE" <<'ANSWERS'
<paste the full ANSWERS SUBMITTED block here>
ANSWERS
```

Then run:

```bash
cd /Users/zhihao/personal_projects/LinkedIn-Job-Scraper && python apply_haiku.py mark JOB_ID_HERE STATUS_HERE
```

Where STATUS_HERE is `applied`, `skipped`, or `failed`.

Report each result to the user as it completes: "✓ [Title] @ [Domain] → applied/skipped/failed"

### H6 — EasyApply jobs (if user chose "Also process EasyApply jobs")

After all OffsiteApply agents finish, handle EasyApply (Playwright required, LinkedIn login):

Determine remaining EasyApply limit (original limit minus jobs already processed):

```bash
cd /Users/zhihao/personal_projects/LinkedIn-Job-Scraper && python apply_haiku.py list --limit REMAINING_LIMIT --type SimpleOnsiteApply,ComplexOnsiteApply
```

If any jobs listed, ask user to confirm LinkedIn login, then launch via osascript:

```bash
osascript -e 'tell application "Terminal" to do script "cd /Users/zhihao/personal_projects/LinkedIn-Job-Scraper && python apply_jobs.py --auto --limit REMAINING_LIMIT --type SimpleOnsiteApply,ComplexOnsiteApply --verbose"'
```

### H7 — Summary

Run `python apply_haiku.py stats` and show updated counts. List each job processed with its outcome.

---

## Standard Playwright Mode (no `--haiku`)

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
