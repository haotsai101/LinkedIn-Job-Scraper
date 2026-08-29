# Wave 2 QA — 2026-08-29

Tickets: T6 (#9), T13 (#8), T9 (#10), T4 (#11), T23 (#12), T3 (#13). All merged.
Interpreter: `/opt/anaconda3/bin/python`. All DB checks against COPIES of the real `linkedin_jobs.db`.

## Regression found & fixed mid-wave

**T9 shipped a P1**: `create_tables()` crashed with `no such column: listed_epoch` on the unmigrated production DB, breaking `search_retriever.py`, `details_retriever.py`, and Dagster `search_jobs_op` / `fetch_job_details_op`. The apply path was unaffected (`_ensure_apply_schema` guard). **Fixed by T23** (`ensure_schema_current()` — one shared schema-modernization impl, called by `create_tables()` before `create_indexes()`).

## Results

| Check | Result |
|---|---|
| pytest tests/ | PASS — 76 passed |
| T9: `get_pending_jobs` old vs new, real DB, all 6 filter variants | Identical job set + identical order |
| T9: hot query plan | `SEARCH j USING INDEX idx_jobs_listed` — temp B-tree sort eliminated |
| T9: `002_schema.py` idempotent | PASS — 2× runs, no double-seed, no re-backfill |
| T9: `DatabaseStructure.md` vs fresh schema | Byte-identical — drift closed |
| T23: the P1 repro (`create_tables()` on unmigrated real-DB copy) | PASS — no exception (was `OperationalError`) |
| T23: schema self-heal | `listed_epoch` + 1263/1263 backfill, `blocked_entities` seeded, `idx_jobs_listed ON jobs(applied, listed_epoch DESC)` |
| T23: `ensure_schema_current()` 2nd call on migrated DB | No ALTER / UPDATE / DROP / ANALYZE / CREATE INDEX — idempotent |
| T23: apply path self-heal | `get_pending_jobs` → 78 pending (unchanged) |
| T23: pipeline unbroken | log-bug-detector traced all 4 entrypoints → `create_tables()` passes on current DB |
| T4: 6183 real LLM-response samples, old vs new parse logic | Byte-identical (3952 browser_action + 2231 classifier) |
| T4: `common.py` stdlib-only, helpers deduplicated, `LLM_LOG_PATH` unchanged | PASS |
| T4: log path for Dagster `apply_jobs_op` subprocess (`cwd=project_root`) | Same file — no CWD divergence |
| T13: `config.py` inert (no runtime imports), 3 role accessors resolve, legacy-alias fallback + DeprecationWarning | PASS |
| T6: single source of truth (`scripts/search_config.py`), fail-fast guard on DSL chars | PASS |
| T3: rotate/prune no-op against current repo, hook after all `main()` early returns | PASS |
| `apply_jobs.py --stats` | Pending 78 / Applied 156 / Skipped 1024 / Auto-failed 5 (== baseline) |
| Real `linkedin_jobs.db` mutated by any check | No |

## Deferred / manual

- **Real `linkedin_jobs.db` is still unmigrated** (no `listed_epoch`). It self-heals on the next apply run or first retriever run (post-T23), or run `python scripts/migrations/002_schema.py linkedin_jobs.db`.
- **T6 live verification** — whether Voyager honors the `OR` + quoted-phrase syntax and whether skip volume drops needs a real discovery scrape (LinkedIn auth). Deferred to the operator.
- `analysis/` (81 MB) physically moved to `../linkedin-job-scraper-analysis/` (was gitignored).

## New follow-up

- **T24** (P3): `ensure_schema_current` backfill gate can't distinguish unparseable from not-yet-done. Zero impact on current data. Fold into T19.

## Wave 2 tickets — CLOSED

T6, T13, T9, T4, T23, T3 — merged, reviewed, QA passed 2026-08-29. T9's regression resolved by T23.
