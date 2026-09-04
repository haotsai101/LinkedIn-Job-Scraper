# Optimization Tickets

Derived from `docs/ARCHITECTURE_AND_OPTIMIZATION.md` §4. Each ticket ships as its own feature branch → `senior-swe` → `pr-code-reviewer` → QA (`log-bug-detector`). Never merge without a reviewer approval; never close without a passing QA run.

Baseline captured 2026-08-28. `docs/baseline/db_state.baseline.txt` holds the DB-state snapshot; the application-log and llm-debug snapshots were kept only locally (they contain application history) — their headline numbers are quoted inline in the QA docs.

## Status

| Wave | Tickets | State |
|---|---|---|
| 1 | T1 #5, T2 #2, T5 #6, T8 #4, T12 #3 | ✅ **CLOSED** — QA passed 2026-08-28 (`docs/qa/wave1-qa.md`) |
| 2 | T6 #9, T13 #8, T9 #10, T4 #11, T23 #12, T3 #13 | ✅ **CLOSED** — QA passed 2026-08-29 (`docs/qa/wave2-qa.md`). T9 shipped a P1 regression (`create_tables()` crash on unmigrated DB); fixed by T23 hotfix. |
| 3 — Phase 2 | T14 #15, T26 #19, T27 #20, T14b #22 | ✅ **CLOSED** 2026-09-03. **The entire LLM stack is off the `claude` subprocess.** T14: Agent SDK wrapper + classifier routing (validated with 2 real applications). T26: `max_turns` fix. T27: NIM-tier hardening (model → `meta/llama-3.2-11b-vision-instruct`, circuit breaker, spam reorder, Greenhouse un-block; bundled T28+T29). T14b: browser agent (`_llm_guided_apply`, EEO pickers, `ScriptApplyEngine`) → one-shot `llm.query`/`query_json`; all dead `AsyncOpenAI` plumbing + the `load_env` `LLM_API` hard-exit removed. 154 tests. |
| 4 — Phase 3 | ~~T15 browser-use spike~~ | **DROPPED** 2026-09-03 (owner: "skip all NIM-specific tasks, keep going with Agent SDK"). browser-use needs a working free NIM model; the whole tier is unreliable. `verify_submission` unification (T15's non-NIM half) folds into T33. |
| 4/5 | **T33** — OffsiteApply flow reliability | **next** — unified `verify_submission` (fix Rippling `/jobs?page=0` false-negative), blocked-domain jobs → skip not auto-fail, T31 (prose in numeric fields), T32 (undersold answers). |
| 5 — Phase 4 | **T16b** — decompose `_llm_guided_apply` on the Agent SDK (primary OffsiteApply path) + retire `ScriptApplyEngine` | after T33 |
| 6 — Phase 5 | T17 — scraper cleanup | needs T12 ✓ |
| Follow-ups | T19 T20 T21 T22 T24 · T30 **parked** (NIM — owner: leave it, circuit breaker handles bad days) | not started (P2–P3) |

**Direction change (2026-09-03):** T14b live-QA runs confirmed the Agent SDK classifier is 100% reliable where NIM's model isn't (T30), but NIM stays for OffsiteApply classification with the circuit breaker as the safety net. The browser-use spike (T15) is dropped — the free NIM tier can't host an agentic browser model reliably. Remaining apply-agent work goes straight to hardening + decomposing `_llm_guided_apply` on the Agent SDK.

### Follow-ups from the T27/T14b live apply runs (2026-09-01/02)

| # | Sev | Summary |
|---|---|---|
| T30 | P2 | New classifier `meta/llama-3.2-11b-vision-instruct` returned non-JSON ~1/3 of NIM-route calls in the real flow, and was 8-12s (not the ~1.5s probe). Contained (deferred, not lost) but the NIM route's value (free) is thin vs Agent SDK (reliable, same latency). Watch; revisit routing. |
| T31 | P2 | Agent fills free-text "Rate your experience (1-10)" fields with prose (`"I would rate my experience at an 8 out of 10…"`) instead of a number. |
| T32 | P2 | Agent answered "years of Data Engineering experience = 0" for a data-focused applicant (undersells). Also a Playwright tab crash mid-fill → `applied=-2` auto-fail (browser stability). |
| — | P3 | (T14b reviewer note) Watch for orphaned `claude` processes after timeout-heavy runs — `asyncio.wait_for` on `llm.query` cancels the SDK generator mid-iteration; subprocess cleanup then depends on the SDK's `GeneratorExit` handling. |

**T27 .env:** classifier model must be `meta/llama-3.2-11b-vision-instruct` (via `CLASSIFIER_MODEL` or the legacy `CLASSIFIER_LLM_MODEL`) — done 2026-09-02.

**Optional (T28):** the ~42 rows commit `76cc97e` pre-skipped as Greenhouse are still `applied=-1`. To reconsider them:
```sql
UPDATE jobs SET applied = NULL
WHERE applied = -1
  AND (posting_domain LIKE '%greenhouse.io' OR application_url LIKE '%grnh.se%'
       OR application_url LIKE '%greenhouse.io%');
```

**T14 was split**: part 1 (#15) = Agent SDK wrapper (`llm.py`) + classifier routing + classifier-side OpenAI plumbing removal. **T14b** = `linkedin_apply._call_claude` / `script_engine._call_claude` → `ClaudeSession` + remove remaining OpenAI plumbing. Part 1's review caught [claude-agent-sdk#560](https://github.com/anthropics/claude-agent-sdk-python/issues/560) (persistent client doesn't isolate context) — classifier uses one-shot `query()`.

**T14 was split** during implementation: part 1 (#15, this) = Agent SDK wrapper (`llm.py`) + classifier routing (NIM ↔ Agent SDK by `application_type`) + classifier-side OpenAI plumbing removal. **T14b** = migrate `linkedin_apply._call_claude` / `script_engine._call_claude` to `ClaudeSession` + remove the remaining OpenAI plumbing. Part 1's review found and fixed a real bug: the persistent Agent SDK client does not isolate context ([claude-agent-sdk#560](https://github.com/anthropics/claude-agent-sdk-python/issues/560)) — the classifier now uses one-shot `query()`.

### Follow-up backlog (raised during Waves 1–2)

| # | Sev | Summary |
|---|---|---|
| T19 | P2 | Auto-run pending migrations at *every* entrypoint (not just apply). Consolidate `_ensure_apply_schema` / `migrate_db` / `ensure_schema_current`. Add DB backup before migration. |
| T20 | P3 | `ruff` not in the interpreter that runs the agent — document/bootstrap lint. |
| T21 | P2 | `fetch_job_details_op` has the same required-config bug T6 fixed for `search_jobs_op` — `details_schedule` is `RUNNING` with no run_config → scheduled enrichment fails every 12h. |
| T22 | P2 | `blocked_entities.ats_domain` rows are seeded but `run_session` still reads `BLOCKED_DOMAINS` from the Python constant — table not wired for domain blocks. |
| T24 | P3 | `ensure_schema_current` backfill gate can't distinguish "unparseable" from "not yet done" — a permanently-NULL `listed_epoch` row would re-trigger the full-table backfill every startup. Zero impact on current data. Fold into T19. |

## Dependency graph

```
Wave 1 — DONE:
  T1 T2 T5 T8 T12

Wave 2a (parallel, in progress):
  T6  narrow search keywords
  T9  DB schema modernization       (needs: T8 ✓)
  T13 config.py model config         (needs: T1 ✓)

Wave 2 (after wave 1 merges):
  T3  log/artifact rotation + move analysis/ out of tree      (needs: T1)
  T4  extract shared helpers to common.py                     (needs: T5 for safety net)
  T6  narrow search keywords                                   (needs: nothing, held for keyword decision)
  T9  DB schema modernization (epoch timestamps, blocklist table, doc regen)  (needs: T8)

Wave 3 — Phase 2:
  T13 config.py centralized model config                      (needs: T1)
  T14 Claude Agent SDK migration + classifier routing         (needs: T13, T4, T3)

Wave 4 — Phase 3:
  T15 browser-use spike + unified verify_submission           (needs: T13, T14)

Wave 5 — Phase 4 (shape decided by T15 outcome):
  T16a  (spike passed)  browser-use primary + light _llm_guided_apply decomposition + retire ScriptApplyEngine
  T16b  (spike failed)  full _llm_guided_apply decomposition as primary + retire ScriptApplyEngine

Wave 6 — Phase 5:
  T17 scraper cleanup: Dagster-owned loops + tenacity + Playwright cookies
```

---

## T1 — Dependency & tooling hygiene

**Phase:** 1a · **Risk:** low · **Deps:** none

- Make `pyproject.toml` `[project.dependencies]` the single source of truth: add `playwright`, `httpx`; remove `openai` (dead after Phase 2 — but flag it deprecated now with a comment, don't delete the import yet); keep `selenium`, `requests`, `pandas`, `numpy`, `dagster*`. Add a `[project.optional-dependencies] dev = ["ruff", "pytest"]`.
- Regenerate `requirements.txt` from `pyproject.toml` (or replace it with a one-line `-e .[dev]` pointer + a note).
- `git rm --cached jobs.db` (empty stray file); add `/jobs.db` to `.gitignore` if not covered.
- Add `[tool.ruff]` config to `pyproject.toml` — line length 100, target py311, select `E,F,I,UP,B`, ignore nothing aggressive. Do **not** run `ruff --fix` across the repo in this ticket (that's noise for the reviewer); just land the config + fix anything in files this ticket already touches.
- Tidy `.gitignore` (it has duplicate/overlapping db rules).

**Acceptance:** `pip install -e .[dev]` works from a clean venv; `ruff check` runs (may report findings — that's fine); `git status` shows `jobs.db` untracked.

---

## T2 — Remove the Haiku / chrome-in-chrome apply path

**Phase:** 1a · **Risk:** low · **Deps:** none

Per the decided plan, OffsiteApply consolidates on browser-use (primary) + decomposed `_llm_guided_apply` (fallback). The `--haiku` chrome-in-chrome path is dead weight.

- Delete `apply_haiku.py`.
- Rewrite `.claude/skills/apply-jobs/SKILL.md`: remove the `--haiku` flag row, the entire "Haiku Agent Mode (`--haiku`)" section and Steps H1–H7, and the `python apply_haiku.py …` invocations. Keep the Standard Playwright Mode as the only mode.
- `application_answers.jsonl` — check whether anything but `apply_haiku.py` writes/reads it; if not, delete it and any references.
- Grep for other `apply_haiku` / `--haiku` / "chrome-in-chrome" references (README, CLAUDE.md) and remove.
- Do **not** touch `linkedin_apply.py:824` (that comment mentions "haiku" as a Claude model fallback — unrelated, leave it).

**Acceptance:** no `apply_haiku` references remain outside `docs/`; `python apply_jobs.py --help` unaffected; SKILL.md describes one coherent flow.

---

## T5 — Characterization tests for `_get_profile_value` / field matching

**Phase:** 1a · **Risk:** low · **Deps:** none

`linkedin_apply.py:_get_profile_value` is 315 lines of pure matching logic with zero tests. It must be locked down before the `common.py` extraction (T4) and the Agent SDK migration (T14).

- Create `tests/` with `tests/test_profile_value.py`.
- Import `_get_profile_value` (and `_degree_rank` if useful) from `linkedin_apply.py`. If importing the module has heavy side effects (playwright/openai), add lazy imports or an `if __name__` guard in `linkedin_apply.py` — minimally.
- Write **characterization** tests: feed a representative `user_profile.json`-shaped dict + a spread of field labels/kinds (name, email, phone, years-experience, salary, work auth, sponsorship, EEO/demographic, degree, LinkedIn URL, address, cover letter, arbitrary unknown) and assert the **current** return values. Goal is a regression net, not "correct" behavior.
- Use `user_profile.json` from the repo if present, else a fixture dict in the test file (no real PII — use `Jane Doe` / `jane@example.com`).
- Add `pytest` to dev deps if T1 hasn't landed yet (coordinate — safe to duplicate).

**Acceptance:** `pytest tests/` green; ≥15 assertions covering the branch spread; no network, no browser.

---

## T8 — DB indexes + WAL

**Phase:** 1b · **Risk:** low (additive only) · **Deps:** none

- Add to `scripts/create_db.py` (so fresh DBs get them) **and** ship a standalone idempotent migration `scripts/migrations/001_indexes.py` (or a `migrate()` helper) that runs `CREATE INDEX IF NOT EXISTS` against an existing `linkedin_jobs.db`:
  - `idx_jobs_pending ON jobs(applied, scraped)`
  - `idx_jobs_company ON jobs(company_id)`
  - `idx_jobs_listed ON jobs(original_listed_time DESC)`
  - `idx_jobs_apptype ON jobs(application_type)`
- Set `PRAGMA journal_mode=WAL` on the DB (in `create_db.py` and the migration).
- Verify with `EXPLAIN QUERY PLAN` on the `get_pending_jobs` query before/after; paste both into the PR description.
- Back up `linkedin_jobs.db` → `linkedin_jobs.db.bak` before running the migration locally (gitignored).

**Acceptance:** `EXPLAIN QUERY PLAN` for the pending-jobs query shows index usage; `PRAGMA journal_mode` returns `wal`; migration is re-runnable with no error.

---

## T12 — Trim Dagster

**Phase:** 1c · **Risk:** medium (touches orchestration wiring) · **Deps:** none

The SDA lineage assets produce a graph nothing consumes.

- Delete `scripts/dagster_db_assets.py`, `scripts/dagster_relationships.py`, `scripts/auto_materialize.py`, `DAGSTER_COMPLETE_GUIDE.md`.
- Rewrite `scripts/definitions.py`: drop `load_assets_from_modules`, `asset_refresh_job`, `auto_materialize_sensor`. Keep `jobs=[search_jobs_only, fetch_details_only, search_and_fetch_jobs, apply_jobs_job]`, `schedules=[search_schedule, details_schedule, apply_schedule]`, `sensors=[unscraped_jobs_sensor]`.
- If `no_persist_io_manager` was only needed because of the assets, remove it and the `resources={"io_manager": ...}` block; if unsure, keep it (harmless).
- Verify the Dagster code object still loads: `DAGSTER_HOME=./.dagster_home dagster definitions validate` (or `dagster dev` briefly) — paste output into the PR.
- Update `README.md` / `CLAUDE.md` references to the deleted guide.

**Acceptance:** `dagster` loads `scripts.definitions` with no error; the 4 jobs + 3 schedules + 1 sensor are present; no import of the deleted modules anywhere.

---

## T3 — Log/artifact rotation + move `analysis/` out of tree

**Phase:** 1a · **Risk:** low · **Deps:** T1

- `scripts/rotate_logs.py` (or a `common.py` helper): rename `llm_debug.jsonl` → `llm_debug.jsonl.1` when it exceeds ~20 MB (keep 2 generations); on apply-session start, delete `debug_screenshots/*` older than the last N sessions (or keep newest ~100 files).
- Call it once from `apply_jobs.py:main()` entry (one line) — coordinate with T14 which also touches `main()`.
- Move `analysis/` outside the repo working tree (document the new location in `README.md`); it's already gitignored.
- `.gitignore`: keep `analysis/` ignored in case it's recreated.

**Acceptance:** oversized `llm_debug.jsonl` rotates; `analysis/` no longer in the repo dir; apply run still starts.

---

## T4 — Extract shared helpers to `common.py`

**Phase:** 1a · **Risk:** medium (touches all 3 large files) · **Deps:** T5

- New `common.py`: `write_llm_log(entry)`, `strip_json_fence(raw) -> str`, `extract_json_object(raw) -> str`, any other verbatim-duplicated helper across `apply_jobs.py` / `linkedin_apply.py` / `script_engine.py`.
- Replace the copies with imports. Do **not** touch `_call_claude` (T14 owns it) or `_get_profile_value`.
- Run `pytest tests/` (from T5) — must stay green.

**Acceptance:** no duplicated helper bodies across the 3 files; `pytest` green; `python -c "import apply_jobs, linkedin_apply, script_engine"` works.

---

## T6 — Narrow search keywords

**Phase:** 1a · **Risk:** low (changes what gets scraped going forward) · **Deps:** keyword decision from owner

- `search_retriever.py:KEYWORDS` and the Dagster `search_jobs_op` config default currently `"software engineer AI ML"` → 786 OffsiteApply skips vs 45 applied. Tighten to a query that better matches the profile (candidate: `"software engineer" OR "ML engineer" OR "AI engineer"` style — **confirm exact string with owner before implementing**).
- Make the keyword string a single config point shared by both the standalone script and the Dagster op.

**Acceptance:** one place defines the search query; documented in `README.md`.

---

## T9 — DB schema modernization

**Phase:** 1b · **Risk:** medium (touches live data + hot query) · **Deps:** T8

- Add `listed_epoch INTEGER` to `jobs`; backfill by parsing `original_listed_time` / `listed_time`; switch `get_pending_jobs` `ORDER BY` to `listed_epoch DESC`. Keep the TEXT columns for now (drop in a later ticket).
- `blocked_entities(kind TEXT, pattern TEXT, reason TEXT)` table; migrate the `BLOCKED_COMPANIES` / blocked-ATS Python constants into it; replace the f-string `LIKE` SQL in `get_pending_jobs` with a JOIN or a parameterized filter (no string interpolation of patterns).
- Regenerate `DatabaseStructure.md` from `PRAGMA table_info` via a small script (`scripts/dump_schema.py`), or delete the doc and point at `scripts/create_db.py`.
- Ship as an idempotent migration `scripts/migrations/002_schema.py`. Back up the DB first.

**Acceptance:** `get_pending_jobs` contains no f-string-interpolated values; `listed_epoch` populated for all rows; migration re-runnable; `DatabaseStructure.md` matches live schema (or is gone).

---

## T13 — `config.py` centralized model config

**Phase:** 2 · **Risk:** low · **Deps:** T1

- `config.py` reading env (with `.env` load): `CLASSIFIER_MODEL` / `CLASSIFIER_API` / `CLASSIFIER_BASE_URL`, `BROWSER_USE_MODEL` / `BROWSER_USE_API` / `BROWSER_USE_BASE_URL`, `GUIDED_APPLY_MODEL`, plus existing `MAX_AUTO_APPLY`, Gmail vars.
- Defaults: classifier → `meta/llama-3.2-11b-vision-instruct` @ NIM (was `google/gemma-4-31b-it` — timing out on the free tier, see T27; originally `meta/llama-3.1-8b-instruct` — EOL'd, see T25); browser-use → `deepseek-ai/deepseek-v4-flash-0731` @ NIM (`https://integrate.api.nvidia.com/v1`); `GUIDED_APPLY_MODEL` → `claude-sonnet-5`.
- Typed accessor (`get_llm_config(role: Literal["classifier","browser_use","guided_apply"])`).
- Update `.env.template` to the new var names; keep reading the old `LLM_*` / `CLASSIFIER_LLM_*` / `BROWSER_LLM_*` names as fallback aliases for one release, with a deprecation note.
- No behavior change yet — nothing imports it until T14.

**Acceptance:** `python -c "import config; print(config.get_llm_config('browser_use'))"` prints the NIM/deepseek config; `.env.template` documents every var.

---

## T14 — Claude Agent SDK migration + classifier routing

**Phase:** 2 · **Risk:** high · **Deps:** T13, T4, T3

- Add `claude-agent-sdk` to deps. Replace `linkedin_apply._call_claude`, `script_engine._call_claude`, and `JobAgent.classify`'s subprocess with a persistent Agent SDK session (subscription auth — **no API key**), one session per apply run, reused across calls.
- Classifier routing on `application_type`: `OffsiteApply` → NIM (`config` classifier client, OpenAI-compatible), `Simple/ComplexOnsiteApply` → Agent SDK. Keep the citizenship keyword fast-path.
- Classifier → structured output; delete the regex / JSON-fence salvage code.
- Delete all remaining dead `AsyncOpenAI` / `openai` / `llm_client` / `classifier_client` plumbing threaded through `run_session`, `EasyApplyFlow.__init__`, `OffsiteApplyFlow.__init__`. Each call site builds its own client.
- `_ask_llm` / `_ask_llm_action` / EEO option pickers now go through the Agent SDK session.
- `pytest tests/` green.

**Acceptance:** no `subprocess.run(["claude"` anywhere; no `AsyncOpenAI` import outside the NIM classifier/browser-use paths; a dry classify of 3 pending jobs works for both routes; `llm_debug.jsonl` still records calls with `usage`.

---

## T15 — browser-use spike + unified `verify_submission`

**Phase:** 3 · **Risk:** high / exploratory · **Deps:** T13, T14

- Add `browser-use` to deps. New `offsite_browser_use.py`: entry `apply_offsite_browser_use(job, profile) -> ApplyResult`, LLM via `ChatOpenAI(base_url=<NIM>, model=<BROWSER_USE_MODEL>)` with `add_schema_to_system_prompt` / `remove_min_items_from_schema` as needed. DOM mode. Feed it the profile + `EmailInbox` verification as a tool + `created_accounts.json`.
- Build `verify_submission(page, job) -> bool` in `common.py` (or `verification.py`) — unify the two `_check_submission_result` implementations. Used by the spike metric and (later) the fallback trigger.
- Pick 10 `job_id`s that are currently `applied = -2` or known-failing OffsiteApply. Run each through browser-use serially. Record: verified-submission? wall-clock? #LLM calls? threw / max-steps / 429?
- Write `docs/spike-browser-use-results.md`: the table + a **go/no-go call against the gate** (≥5/10 verified, median <5 min/job, ≤1 hard failure from 40 RPM).

**Acceptance:** results doc with the 10-job table and an explicit go/no-go; `verify_submission` has unit coverage; spike code on a branch, not merged to master until the Phase 4 decision.

---

## T33 — OffsiteApply flow reliability

**Phase:** 4 · **Risk:** medium · **Deps:** T14b ✓ · From the T14b live-QA runs (2026-09-01/03).

The Agent-SDK apply flow fills forms well (résumé upload, React Select, EEO decline all worked live) but loses jobs to bad end-state handling:

1. **Unified `verify_submission(page, job_or_url) -> (bool, str)`** — pull the two `_check_submission_result` impls (`linkedin_apply.py:1779` EasyApply, `:4157` OffsiteApply) into one place. **Fix the Rippling false-negative:** the agent fills + submits, Rippling redirects to `.../jobs?page=0`, and `:4197` returns `"URL changed but no confirmation text or success URL pattern"` → `applied=-2`. That redirect-to-listing IS the success signal for Rippling (and several ATSes) — recognize it, or re-check the application state. 3 likely false-negatives across the QA runs (Aalyria ×3).
2. **Blocked-domain jobs → skip, not auto-fail.** `linkedin_apply.py:2695` marks Workday / `applytojob.com` / other un-automatable ATSes `applied=-2` ("marking failed for manual retry"). `-2` is in the `--reset-failed` retry pool, so they churn forever. Give them `applied=-1` (or a distinct `-3` "blocked, needs human") so they don't retry automatically. Same for the `_dead_end_domains` / login-wall cases (`:2889`).
3. **T31 — numeric fields get prose.** "Rate your experience (1-10) …" gets `"I would rate my experience at an 8 out of 10…"` instead of `8`. Detect numeric/range fields in `_fill_field` / `_get_profile_value` and coerce the LLM answer to just the number.
4. **T32 — undersold answers.** "years of Data Engineering experience = 0" filled for a data-focused applicant. The profile has `years_experience: "4"` and data skills — the fallback for an unmapped "years of X" field should use the general experience figure or a sensible floor, not `0`. Review the `_get_profile_value` "years of <skill>" branch.

**Acceptance:** `verify_submission` unit-tested with the Rippling redirect case + a real confirmation case; a Workday job ends `-1`/`-3` not `-2`; a "Rate 1-10" field gets a bare number; no `= 0` for a skill the applicant plausibly has.

---

## T16b — Decompose `_llm_guided_apply` on the Agent SDK

**Phase:** 4 · **Risk:** medium-high · **Deps:** T33 · (T15 browser-use spike dropped — this is now the OffsiteApply primary, not a fallback.)

Split the ~1,500-line `_llm_guided_apply` into testable seams: `page_snapshot` · `decide_action` (the `llm.query` call) · `execute_action` · `detect_terminal_state` (applied / dead-end / needs-human — uses `verify_submission` from T33) · `handle_auth` (login + account creation + `EmailInbox` verification). Retire `ScriptApplyEngine` (the LLM-writes-a-Playwright-script path) — the step loop is the single OffsiteApply engine. Page representation → accessibility-tree / structured field list where practical (fewer tokens than raw DOM).

**Acceptance:** each seam independently unit-tested; `ScriptApplyEngine` gone; a live OffsiteApply run against 5 known-failing jobs completes without the old function.

---

## T19 — Auto-run pending DB migrations on startup

**Phase:** P3 · **Risk:** low · **Deps:** T8, T9 · Raised by log-bug-detector during Wave 1 QA.

T8's indexes + WAL and T9's schema changes only take effect when the operator manually runs the migration scripts. For an unattended agent that's a footgun. Add a lightweight "run all `scripts/migrations/NNN_*.py` that haven't been applied" step to `apply_jobs.py` startup (and/or a Dagster op), tracked via a `schema_migrations(id TEXT PRIMARY KEY, applied_at INTEGER)` table. Each migration is already idempotent, so worst case is a fast no-op.

**Acceptance:** a fresh checkout + first `apply_jobs.py` run leaves `linkedin_jobs.db` fully migrated with no manual step.

---

## T20 — Pin `ruff` into the interpreter that runs the agent

**Phase:** P3 · **Risk:** trivial · **Deps:** T1 · Raised by log-bug-detector during Wave 1 QA.

`ruff` is in `[project.optional-dependencies].dev` but the `/opt/anaconda3/bin/python` env that actually runs the agent doesn't have it, so `ruff check .` only works in a fresh venv. Either document that lint runs in the venv, add a `make lint` / `scripts/lint.sh` that bootstraps it, or install it into the anaconda env and note that in `CLAUDE.md`.

**Acceptance:** `ruff check .` runs from the documented dev setup with one obvious command.

---

## T21 — `fetch_job_details_op` required-config bug

**Phase:** follow-up · **Risk:** low · **Deps:** none · Raised by the T6 reviewer.

`scripts/dagster_retrievers.py:fetch_job_details_op` has `config_schema={"max_updates": int, "sleep_time": int}` with both fields required, but `details_schedule` (`default_status=RUNNING`) supplies no `run_config` — so scheduled enrichment fails config validation every 12h (only `unscraped_jobs_sensor` provides config). Same class of bug T6 fixed for `search_jobs_op`. Apply the same `Field(default_value=...)` treatment: `max_updates=25`, `sleep_time=30` (match the existing `.get()` fallbacks + the sensor's values). `search_and_fetch_jobs` (unscheduled) also benefits.

**Acceptance:** `details_schedule` produces a valid run from the launchpad with no config; the sensor path is unaffected.

---

## T22 — Wire `blocked_entities.ats_domain` to `run_session`

**Phase:** follow-up · **Risk:** low · **Deps:** T9 (done) · Raised by the T9 reviewer.

T9 created `blocked_entities` and seeds `ats_domain` rows, but `run_session`'s URL check still reads `BLOCKED_DOMAINS` (derived from the frozen `BLOCKED_ENTITIES_SEED` Python constant), so an operator adding an `ats_domain` row to the table is silently ignored. Have `run_session` load `ats_domain` patterns from the table once per session (mirror the `get_pending_jobs` approach), making the table authoritative for domain blocks too.

**Acceptance:** adding an `ats_domain` row to `blocked_entities` blocks that domain on the next apply session with no code change.

---

## T24 — Backfill re-run gate can't detect permanently-unparseable rows

**Phase:** follow-up (fold into T19) · **Risk:** low · **Occurrence:** 0 in current data · Raised by log-bug-detector during Wave 2 QA.

`ensure_schema_current()` gates the `listed_epoch` backfill behind `SELECT 1 FROM jobs WHERE listed_epoch IS NULL LIMIT 1`. Rows whose `original_listed_time`/`listed_time` are both unparseable stay `NULL` after the `CASE ... ELSE NULL` backfill, so they keep satisfying the probe → the full-table backfill `UPDATE` (scan + write lock) runs on every `ensure_schema_current()` call and never converges. Current `linkedin_jobs.db`: 1263/1263 parse, so zero impact. Fix: narrow the probe to rows the backfill *can* fix (mirror `_epoch_case` conditions), or sentinel-mark unfixable rows. Fold into T19.

**Acceptance:** on a DB with an unparseable-timestamp row, `ensure_schema_current()` runs the backfill at most once.

---

## T25 — NIM classifier model EOL

**Phase:** T14 follow-up · **Risk:** low · **Status:** ✅ fixed (PR pending) · Found during T14 live QA 2026-08-31.

`meta/llama-3.1-8b-instruct` (the `config.py` classifier default + `.env.template` + the `.env`) reached end-of-life on NVIDIA NIM 2026-08-26 → HTTP 410. NVIDIA also purged much of the small-model catalog (many IDs now 410 or 404). Live-tested replacements: `google/gemma-4-31b-it` works (clean `json_object`, correct on all probe cases, ~15s/call free tier); `openai/gpt-oss-20b` is a reasoning model with intermittent `None` content; `deepseek-v4-flash` (the `browser_use` default) never returned in >7 min on the free tier.

**Fix (this PR):** classifier default → `google/gemma-4-31b-it` in `config.py` + `.env.template` + tests. **Operator must also edit `.env`:** `CLASSIFIER_LLM_MODEL=meta/llama-3.1-8b-instruct` → `CLASSIFIER_MODEL=google/gemma-4-31b-it`.

**Open:** the `browser_use` default (`deepseek-v4-flash`) is unverified on the free tier — **T15 (browser-use spike) must confirm it or pick another** before that default is trusted.

**Superseded (T27, 2026-09-02):** `google/gemma-4-31b-it` began timing out on the free NIM tier (34–90s, or full timeout) during the T14 live run. Classifier default is now `meta/llama-3.2-11b-vision-instruct` (validated 8/8 on the probe set, p50 1.5s).

---

## T17 — Scraper cleanup

**Phase:** 5 · **Risk:** medium · **Deps:** T12

- Replace the `while True: time.sleep()` bodies of `search_retriever.py` / `details_retriever.py` with thin wrappers over the existing Dagster ops; add `tenacity` retry/backoff around the Voyager calls in `scripts/fetch.py`.
- Move cookie extraction from Selenium to Playwright with a persisted `storage_state.json`, refreshed only on 401.
- Drop `selenium` from deps if nothing else uses it.

**Acceptance:** no `while True` in the standalone scripts; `tenacity` wraps the network calls; a discovery run works without launching Selenium when a valid `storage_state.json` exists.

---

## T27 — OffsiteApply per-job loop hardening

**Phase:** T14 follow-up · **Risk:** medium · **Status:** ✅ fixed (PR) · From the T14 live run 2026-09-01 + NIM model survey 2026-09-02. Bundles the classifier-model swap + T28 + T29 (all `run_session` per-job loop).

1. **Classifier model swap.** `google/gemma-4-31b-it` timed out on the free NIM tier (34–90s, or full timeout); ~40 other small models are down/404/EOL. `meta/llama-3.2-11b-vision-instruct` validated 2026-09-02: 8/8 correct (relevance + citizenship), p50 1.5s, 0 rate-limit errors, clean `json_object`. Now the `config.py` classifier default + `.env.template` + the `LLM_MODEL` fallback. **Operator must edit `.env`:** `CLASSIFIER_LLM_MODEL` / `CLASSIFIER_MODEL` → `meta/llama-3.2-11b-vision-instruct`.

2. **Per-attempt timeout + per-route circuit breaker.** `JobAgent._run_with_retry` now gives *each* classify attempt its own 40s deadline (was one 90s `asyncio.wait_for` wrapping both attempts). A `TimeoutError` is **not** retried — it fails fast so the circuit breaker can fall back to the other route rather than waiting another 40s. `NimConfigError` still fails fast too. `nim_client._TIMEOUT_S` lowered 90 → 45s so a timed-out `asyncio.to_thread` worker doesn't outlive the wait by ~50s. New session-scoped circuit breaker (`classify_with_circuit_breaker`): after 2 consecutive NIM-route timeouts, every remaining `OffsiteApply` job is classified via the Agent SDK for the rest of that session (logged as `classifier_route_degraded`). A bad NIM tier no longer strands the OffsiteApply queue.

3. **Fail-streak per-route fix.** A NIM timeout that the Agent SDK then classifies successfully is no longer a `classify_fail_streak` increment — so interleaved EasyApply successes can't mask a dead NIM route. A genuine failure (both routes fail) still counts and still breaks at 3. Deferred jobs (classifier failed, job left pending, no `mark_job`) are now tracked as `deferred_count`, not `skipped_count` — "Skipped: N" no longer over-reports.

4. **T29 — spam check before classification.** `_OFFSITE_SPAM` / aggregator domains are now matched against `posting_domain` / `application_url` *before* `agent.classify()`. A spam listing costs 0 classifier calls (a jobright.ai job previously burned a 68s call before being spam-skipped).

5. **T28 — narrowed Greenhouse block.** Removed `job-boards.greenhouse.io` / `boards.greenhouse.io` / `grnh.se` from `_OFFSITE_SPAM` entirely (option (a)). `grnh.se` is only a link shortener and `*.greenhouse.io` boards host real per-company forms; the blanket block permanently skipped legitimate direct employers (e.g. MasterControl's "Marketing Operations AI Engineer"). `OffsiteApplyFlow` already has Greenhouse iframe-embed handling plus reCAPTCHA / bot-wall detectors that return `"skipped"` at runtime, so a genuinely CAPTCHA-walled Greenhouse form is still skipped — as a runtime outcome, not a blind pre-filter.
   - Only `linkedin_apply.py` reference to a `greenhouse.io` host in a block list is `my.greenhouse.io` in `_dead_end_domains` (an SSO login-wall subdomain) — it does not match the `job-boards` / `boards` form hosts, so it is a harmless clean skip for a different case.
   - Hardened the Greenhouse text "security code" bot wall while here: it was a bare `input()` with no `auto_mode` guard, which in an interactive `--auto` run blocks the event loop (and the outer `asyncio.wait_for(flow.run(), 600)`) forever. Now: `--auto` → `return "skipped"`; interactive → pause for the human as before. Added a cheap `_detect_bot_wall()` probe (reCAPTCHA widget + Greenhouse security text) *before* the `ScriptApplyEngine` / `_summarize_job` LLM calls so a walled job is skipped without burning them.

**Operator recovery — un-skip the ~42 Greenhouse rows commit `76cc97e` pre-skipped:**
```sql
UPDATE jobs SET applied = NULL
WHERE applied = -1
  AND (posting_domain LIKE '%greenhouse.io'
       OR application_url LIKE '%grnh.se%'
       OR application_url LIKE '%greenhouse.io%');
```
Run once against `linkedin_jobs.db` after this PR merges; the next apply session will re-evaluate them through the real flow. (Not automated here — the DB layer is out of scope for this PR.)

**Acceptance:** `config.get_llm_config("classifier").model` default is `meta/llama-3.2-11b-vision-instruct`; spam domains skip with 0 classifier calls; a Greenhouse job gets a real apply attempt; a `--auto` run never blocks on `input()`; circuit-breaker state is session-scoped and resets each `run_session`; `pytest tests/` green.

---

## T34 — Mid-flow blocked-ATS/login-wall paths still use `-2` instead of T33's `-3`

**Phase:** T33 follow-up · **Risk:** low · **Status:** ✅ fixed, merged (PR #26, 2026-09-04) · Found during T33 live validation run 2026-09-02/04.

T33 added a `"blocked"` outcome (→ `applied=-3`, excluded from `--reset-failed`) for jobs that hit a known un-automatable ATS domain — but only wired it into the **pre-flight** domain check in `linkedin_apply.py` (~lines 3018-3019, 3169, 3178, 3216: `"marking blocked (no auto-retry)"`). Three mid-flow checks that are the same category of "needs a human, don't auto-retry" situation still `return "failed"` (→ `applied=-2`, which **is** retried by `--reset-failed`):

1. **Post-navigation blocked ATS** (~line 3282): a deterministic click mid-flow redirects into a domain on `_blocked_auto_apply_domains` (same list the pre-flight check uses). Currently: `"Post-navigation blocked ATS (...) — marking failed for manual review"` → `"failed"`.
2. **Login wall, no stored credentials** (~line 3320): a password field appears mid-flow and `_find_account_for_domain` finds nothing. Currently: `"Login wall detected (password field) on ... — marking failed for manual login"` → `"failed"`.
3. **No stored credentials** (~line 4679): same class of check, different call site — grep `"marking failed for manual"` in `linkedin_apply.py` to enumerate all remaining instances precisely (line numbers may have drifted since T33 merged).

Note the **login-wall-with-credentials-that-fail-to-log-in** branch (also ~3316-3319: `"Login with stored credentials failed ... — marking failed"`) is a *different* case — a real transient/credential failure, not "no human path exists" — and should stay `-2` (retryable). Only the "no credentials exist for this domain" and "domain is on the blocked list" branches are the T33 gap; don't blanket-convert every `return "failed"` in these functions.

**Reproduced live:** in the T33 validation run, Job 8 (U.S. Bank) reached Workday via a mid-flow deterministic click and printed `"Post-navigation blocked ATS (usbank.wd1.myworkdayjobs.com) — marking failed for manual review"` → `applied=-2`, while Job 3 (Motorola Solutions, same underlying Workday-is-unautomatable reason, but caught pre-flight) correctly went to `-3`. Since U.S. Bank's `-2` is retryable, the next `--reset-failed` run will burn another cycle re-discovering the same dead end.

**Fix (this PR):** confirmed the pre-flight check signals via a literal `return "blocked"`, which `run_session` (`apply_jobs.py:1383-1389`) already maps to `applied=-3`. Converted the two live call sites to match: post-navigation blocked-domain check (`linkedin_apply.py:3281-3284`) and the mid-loop login-wall-with-no-stored-credentials branch (`:3320-3323`), both now `return "blocked"` with `"marking blocked (no auto-retry)"` wording. The third grep hit (`_handle_auth_page`, `:4679`) returns a `bool`, not a status string, and its sole caller (the pre-flight login-path check at `:3212-3217`) already collapses any `False` into `"blocked"` regardless of cause — so only its log message text was updated for wording consistency; no behavior change was needed or made there. The adjacent "stored credentials exist but login failed" branch (`:3317-3319`) is untouched and still returns `"failed"`. New `tests/test_blocked_status.py` drives `OffsiteApplyFlow._llm_guided_apply` through fake Page/Context objects for all three cases (post-nav blocked → blocked, no-creds login wall → blocked, failing-stored-creds login wall → failed, as a regression guard) — verified each new test fails against the pre-fix code before confirming green. `pytest tests/` (184 tests) and `ruff check` on the changed files are clean.

**Known pre-existing quirk, not touched (flagged for the reviewer):** inside `_handle_auth_page` (`:4671-4680`), the "credentials exist but login failed" fallthrough has no `return` after its own print, so it falls into the same final `print(...); return False` as the true no-credentials case — meaning that specific pre-flight login-path call site (`:3212-3217`, not the mid-loop one this ticket targets) already always maps a failed stored-credential login to `blocked`/`-3` rather than the `failed`/`-2` the mid-loop check gives it. This predates T34 and is a separate, narrower gap from what this ticket's acceptance criteria cover — out of scope here.

**Acceptance:** a job that hits the post-navigation blocked-ATS check or a login wall with no stored/discoverable credentials for that domain ends the run with `applied=-3`, not `-2`; `--reset-failed` does not pick these up; a job whose stored-credential login attempt genuinely fails still ends `-2` (unchanged, retryable); `pytest tests/` green.

---

## T35 — `_handle_auth_page` fallthrough over-blocks failed stored-credential logins (live since T33, ~2 months)

**Phase:** T33/T34 follow-up · **Risk:** medium (silent, already live in production) · **Status:** ✅ fixed (PR pending) · Found by the T34 SWE agent, confirmed independently by the T34 PR reviewer, 2026-09-04.

`_handle_auth_page` (`linkedin_apply.py:~4671-4680`) has two failure branches: "no stored credentials for this domain" and "stored credentials exist but the login attempt failed" (transient/2FA/rate-limit — should be retryable). The second branch's `print(...)` has no `return` statement after it, so execution falls through into the same final `return False` the true no-credentials case uses — collapsing both into one signal. Its caller, the **pre-flight** login-path check at `linkedin_apply.py:3212-3217`, maps any `False` to `"blocked"` → `applied=-3` (excluded from `--reset-failed`). Net effect: since T33 merged (`c962fef0`, 2026-07-03), **any offsite job whose stored credentials exist but whose login fails for a transient reason has been permanently written off as `-3` instead of retried as `-2`** — the exact mis-classification T33/T34 exist to prevent, just at a third call site neither ticket's scope covered. (T34's mid-loop equivalent, `_handle_auth_page`'s sibling check at `:3320-3323`, does NOT have this bug — it correctly distinguishes the two cases; only the pre-flight call site inherits the fallthrough.)

**Fix:** add the missing `return` (or an explicit distinct sentinel, e.g. a 3-way return / exception, if `:3212-3217` needs to tell the two cases apart — check what that call site currently does with the `bool` and whether a signature change ripples further) after the "stored credentials exist but login failed" print in `_handle_auth_page`, so that branch reaches its own outcome instead of falling through to the no-credentials `return False`. Confirm `:3212-3217` still correctly maps the true no-credentials case to `"blocked"`/`-3` and the login-failed case to `"failed"`/`-2` (mirroring the mid-loop check T34 already got right).

**Operator recovery (after this PR merges):** some current `applied=-3` rows may actually be transient login failures wrongly stuck as non-retryable. Consider auditing/reconsidering `-3` rows going back to 2026-07-03 whose blocked reason traces to this call site (vs. a genuine blocked-domain hit) — not automated here, DB layer out of scope for this ticket. **Flagged for the operator, not done in this PR.**

**Acceptance:** a job where `_handle_auth_page` is reached via the pre-flight login-path check, stored credentials exist for the domain, and the login attempt itself fails, ends the run `applied=-2` (retryable), not `-3`. A job with no stored/discoverable credentials at all still correctly ends `-3`. Regression test covering both branches of `_handle_auth_page`'s caller at `:3212-3217` (not just the mid-loop `:3320-3323` one T34 covered). `pytest tests/` green.

**Fix (this PR):** `_handle_auth_page` (`linkedin_apply.py:4628`) changed its return type from a plain `bool` to `bool | str`: `True` on successful auth, `"failed"` when stored credentials exist for the domain but the login attempt itself fails, `"blocked"` when no stored/discoverable credentials exist at all — mirroring the `"blocked"`/`"failed"` string idiom `run_session` and the mid-loop check already use. The missing `return` after the "login failed with stored credentials" print (`:4679`) is now explicit (`return "failed"`) instead of falling through into the no-credentials branch's `return "blocked"` a few lines later. Its sole caller — the pre-flight login-path check (`:3212-3217`) — now branches on `ok is not True` and inspects the string: `"failed"` → `return "failed"` (`applied=-2`, retryable), anything else → `return "blocked"` (`applied=-3`, unchanged). `_handle_auth_page` has exactly one call site in the codebase (confirmed by grep), so no other caller needed updating. Added two regression tests to `tests/test_blocked_status.py` targeting this pre-flight call site specifically (T34's existing tests only covered the mid-loop `:3320-3323` check, which never had this bug): a login-path URL (e.g. `/login`) with no stored credentials → `"blocked"`, and the same URL with stored credentials that fail to log in → `"failed"`. `pytest tests/` (186 tests) green; `ruff check` on the two changed files clean (the pre-existing ~400 lint findings elsewhere in `linkedin_apply.py` are untouched debt, not introduced by this change).
