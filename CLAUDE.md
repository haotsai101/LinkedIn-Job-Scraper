# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Dagster (recommended — runs all pipelines via UI at http://localhost:3000)
DAGSTER_HOME=./.dagster_home dagster dev

# Standalone scripts
python search_retriever.py          # Discover new job IDs
python details_retriever.py         # Enrich scraped=0 jobs with full attributes

# Job application agent
python apply_jobs.py                              # Semi-auto: confirm before each submit
python apply_jobs.py --auto                       # Fully autonomous
python apply_jobs.py --stats                      # Print pending/applied/skipped/failed counts
python apply_jobs.py --setup                      # Re-run profile interview
python apply_jobs.py --reset-failed               # Reset applied=-2 jobs back to pending
python apply_jobs.py --verbose                    # Save debug screenshots on failures
python apply_jobs.py --limit 5                    # Process at most 5 jobs this session
python apply_jobs.py --type OffsiteApply          # Filter by application_type
python apply_jobs.py --type SimpleOnsiteApply,ComplexOnsiteApply  # EasyApply only

# Export to CSV
python to_csv.py --folder <dest> --database linkedin_jobs.db
```

There is no test suite and no linter configured.

## Git workflow

After every code change, commit with a descriptive message and push to origin before moving on. Never leave modified source files uncommitted or unpushed at the end of a task.

## Development workflow

When tickets are created (e.g. by the log-bug-detector agent after a run), follow this pipeline:

1. **Triage & dependencies** — Before assigning any ticket, determine dependencies between tickets. Block a ticket on its prerequisites and order work accordingly. State the dependency graph explicitly before dispatching agents.

2. **Implementation — `senior-swe` agent** — Assign each ready (unblocked) ticket to the `senior-swe` agent. The SWE implements the fix end-to-end and opens a GitHub pull request. Brief the agent with: ticket title, root cause, affected files, and any blocking tickets that were already merged.

3. **Code review — `pr-code-reviewer` agent** — Once a PR is open, assign it to the `pr-code-reviewer` agent with the PR number. The reviewer reads the diff, leaves inline comments, and returns an **approve** or **request changes** verdict.

4. **Iterate** — If the reviewer requests changes, send the feedback back to the `senior-swe` agent (use `SendMessage` with the same agent ID to resume context). Repeat until approved, then merge.

**Rules:**
- Never merge a PR without a reviewer approval.
- Dispatch the SWE and reviewer as separate agents — the SWE must not review its own work.
- When multiple tickets are independent, dispatch their SWE agents in parallel (one `Agent` call per ticket in the same message).

## Architecture

### Two-phase scraping pipeline

**Phase 1 — Discovery** (`search_retriever.py`, `scripts/fetch.py:JobSearchRetriever`): Queries LinkedIn's internal Voyager API via authenticated `requests.Session` (cookies extracted by Selenium). Inserts new job IDs into the `jobs` table with minimal attributes and `scraped=0`.

**Phase 2 — Enrichment** (`details_retriever.py`, `scripts/fetch.py:JobDetailRetriever`): Fetches full job attributes for every `scraped=0` row and sets `scraped=1`. This is rate-limit-sensitive and is designed to run with multiple accounts/proxies.

### Dagster orchestration layer (`scripts/`)

All pipeline logic is wrapped as Dagster ops/jobs/schedules in `scripts/dagster_retrievers.py`. The entrypoint for `dagster dev` is `scripts/definitions.py` (declared in `pyproject.toml` under `[tool.dagster]`). Schedules run search every ~4 hours and details enrichment every ~6 hours. An `unscraped_jobs_sensor` triggers detail fetching whenever new unenriched jobs appear.

`scripts/dagster_db_assets.py` and `scripts/dagster_relationships.py` define software-defined assets that reflect the DB tables for lineage tracking. The `no_persist_io_manager` is used so Dagster doesn't write snapshot files to disk.

### Autonomous apply agent (`apply_jobs.py`, `linkedin_apply.py`, `script_engine.py`)

Reads jobs where `scraped=1 AND applied IS NULL` (filtered to remote/Utah). For each job:
1. **LLM classifier** (`JobAgent`) calls an OpenAI-compatible API to decide relevance.
2. **Playwright browser** logs into LinkedIn and runs one of two flows:
   - `EasyApplyFlow` — LinkedIn's native in-modal multi-step form (`SimpleOnsiteApply`, `ComplexOnsiteApply`)
   - `OffsiteApplyFlow` — external company career site; tries `ScriptApplyEngine` (generates a complete Playwright script per page) first, then falls back to a step-by-step LLM loop. Can auto-create accounts (saved to `created_accounts.json`).
3. `jobs.applied` is set to: `1`=applied, `-1`=skipped/irrelevant, `-2`=auto-failed.

The agent supports three separate LLM endpoints: `LLM_*` (default), `CLASSIFIER_LLM_*` (relevance scoring), `BROWSER_LLM_*` (form filling). All default to the same endpoint if not specified. Any OpenAI-compatible API works (NVIDIA NIM, OpenRouter, etc.).

### SQLite database (`linkedin_jobs.db`)

Single database file. Key `jobs` columns:
- `scraped`: 0 = discovered only, >0 = fully enriched
- `applied`: NULL = pending, 1 = applied, -1 = skipped, -2 = auto-failed
- `application_type`: `SimpleOnsiteApply`, `ComplexOnsiteApply`, `OffsiteApply`
- `remote_allowed`, `location`: used to filter apply candidates (remote or Utah)

## Configuration files

| File | Purpose |
|---|---|
| `.env` | LLM API keys, Gmail credentials, `MAX_AUTO_APPLY` — copy from `.env.template` |
| `logins.csv` | LinkedIn account credentials; `method` column: `search`, `details`, or `apply` |
| `user_profile.json` | Applicant profile used to fill forms; auto-generated by `--setup` |
| `created_accounts.json` | Career-site accounts created during offsite apply flows |
| `application_log.json` | Per-session apply results (appended each run) |

`logins.csv` must have columns: `emails`, `passwords`, `method`. Accounts with `method=apply` are preferred by the apply agent; it falls back to `search` accounts if none exist.

## graphify

This project has a knowledge graph at graphify-out/ with god nodes, community structure, and cross-file relationships.

Rules:
- For codebase questions, first run `graphify query "<question>"` when graphify-out/graph.json exists. Use `graphify path "<A>" "<B>"` for relationships and `graphify explain "<concept>"` for focused concepts. These return a scoped subgraph, usually much smaller than GRAPH_REPORT.md or raw grep output.
- If graphify-out/wiki/index.md exists, use it for broad navigation instead of raw source browsing.
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context.
- After modifying code, run `graphify update .` to keep the graph current (AST-only, no API cost).
