# LinkedIn Job Scraper & Auto-Apply Agent

<img src="media/logo.jpg" width="530" height="267">

Scrapes a continuous stream of LinkedIn job postings, stores them in SQLite, and autonomously applies to matching jobs using a Playwright browser agent backed by any OpenAI-compatible LLM.

## Setup

```bash
pip install -r requirements.txt
playwright install chromium

# Copy and fill in credentials
cp .env.template .env
cp logins.csv.template logins.csv
```

**`logins.csv`** — LinkedIn account credentials. Columns: `emails`, `passwords`, `method`. Set `method` to `search`, `details`, or `apply`.

**`.env`** — LLM API key, endpoint URL, model name, and optional Gmail credentials (see `.env.template`).

**`user_profile.json`** — Applicant profile used to fill forms. Generate interactively:
```bash
python apply_jobs.py --setup
```

## Scraping Pipeline

Two-phase pipeline, orchestrated by [Dagster](https://dagster.io/) or run standalone.

**Phase 1 — Discovery** (`search_retriever.py`): Queries LinkedIn's Voyager API for new job IDs and inserts them with `scraped=0`.

**Phase 2 — Enrichment** (`details_retriever.py`): Fetches full attributes for every `scraped=0` job and sets `scraped=1`. Rate-limit-sensitive — use multiple accounts/proxies.

### Dagster (recommended)

```bash
DAGSTER_HOME=./.dagster_home dagster dev
# Then visit: http://localhost:3000
```

Schedules discovery every ~4 hours and enrichment every ~6 hours. A sensor triggers enrichment whenever new unenriched jobs appear.

### Standalone

```bash
python search_retriever.py   # Discover new job IDs
python details_retriever.py  # Enrich scraped=0 jobs
```

## Auto-Apply Agent

Reads pending jobs (`applied IS NULL`), classifies them with an LLM, and applies via Playwright.

```bash
python apply_jobs.py                              # Semi-auto: confirm before each submit
python apply_jobs.py --auto                       # Fully autonomous
python apply_jobs.py --stats                      # Print pending/applied/skipped/failed counts
python apply_jobs.py --verbose                    # Save debug screenshots on failures
python apply_jobs.py --limit 5                    # Review at most 5 jobs this session
python apply_jobs.py --max-apply 10              # Cap submissions this session
python apply_jobs.py --type OffsiteApply          # Filter by application type
python apply_jobs.py --type SimpleOnsiteApply,ComplexOnsiteApply  # LinkedIn Easy Apply only
python apply_jobs.py --reset-failed               # Reset auto-failed jobs back to pending
```

**Application types:**
- `SimpleOnsiteApply` / `ComplexOnsiteApply` — LinkedIn Easy Apply (in-modal multi-step form)
- `OffsiteApply` — External company career sites (LLM fills arbitrary HTML forms)

Results are written to `application_log.json` and emailed if Gmail credentials are set.

## Database

Single SQLite file: `linkedin_jobs.db`. Key `jobs` columns:

| Column | Values |
|---|---|
| `scraped` | `0` = discovered only, `>0` = fully enriched |
| `applied` | `NULL` = pending, `1` = applied, `-1` = skipped, `-2` = auto-failed |
| `application_type` | `SimpleOnsiteApply`, `ComplexOnsiteApply`, `OffsiteApply` |
| `remote_allowed` | Used to filter candidates for apply |

```bash
# Export to CSV
python to_csv.py --folder <dest> --database linkedin_jobs.db
```

[Full database structure](DatabaseStructure.md)
