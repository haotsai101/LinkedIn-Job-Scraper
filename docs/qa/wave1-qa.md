# Wave 1 QA — 2026-08-28

Tickets merged: T2 (#2), T12 (#3), T5 (#6), T8 (#4), T1 (#5).
No apply/LLM runtime path changed → smoke QA (not `apply_jobs.py --auto`).
Interpreter: /opt/anaconda3/bin/python

| Check | Result |
|---|---|
| Core module imports (apply_jobs, linkedin_apply, script_engine, scripts.definitions) | PASS |
| pytest tests/ | PASS — 16 passed in 0.20s |
| apply_jobs.py --stats | PASS — Pending 78 / Applied 156 / Skipped 1024 / Auto-failed 5 (== baseline) |
| apply_jobs.py --help | PASS — exit 0 |
| Fresh DB via scripts/create_db.py | PASS — idx_jobs_{pending,company,listed,apptype} + journal_mode=wal |
| scripts/migrations/001_indexes.py on real-DB copy | PASS — 4 indexes created, WAL set, full skip on 2nd run (idempotent) |
| EXPLAIN QUERY PLAN pending-jobs query (from T8 review) | SEARCH j USING INDEX idx_jobs_pending (was full SCAN); TEMP B-TREE ORDER BY remains → T9 |
| dagster: scripts.definitions loads | PASS — 4 jobs, 3 schedules, sensor unscraped_jobs_sensor; asset modules gone |
| Dangling refs to deleted code (apply_haiku, dagster_db_assets, dagster_relationships, auto_materialize) outside docs | PASS — none |
| ruff check . | N/A in anaconda env (ruff not installed there); config valid, verified by reviewers in a venv |

Baseline: docs/baseline/ (application_log, llm_debug summary, db_state).
Real linkedin_jobs.db NOT migrated — operator runs `python scripts/migrations/001_indexes.py` when ready.

## Independent verification — log-bug-detector

**VERDICT: PASS — Wave 1 safe to close. No regressions.**

- DB row integrity: 1263 rows; per-type/applied breakdown sums exactly to baseline. No drift.
- `get_pending_jobs` SQL byte-identical (`apply_jobs.py`) — T8 index is a pure read-path optimization.
- Query plan on real-DB copy after migration: `SCAN j` → `SEARCH j USING COVERING INDEX idx_jobs_pending`.
- Skipping the full `--auto` run is defensible: no ticket touches the browser loop, ScriptApplyEngine, classifier, or any LLM path.

Two P3 follow-ups raised (not bugs) → tickets T19, T20 in TICKETS.md.

## Wave 1 tickets — CLOSED

T1 (#5), T2 (#2), T5 (#6), T8 (#4), T12 (#3) — merged, reviewed, QA passed 2026-08-28.
