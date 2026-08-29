# Optimization Tickets

Derived from `docs/ARCHITECTURE_AND_OPTIMIZATION.md` §4. Each ticket ships as its own feature branch → `senior-swe` → `pr-code-reviewer` → QA (`log-bug-detector`). Never merge without a reviewer approval; never close without a passing QA run.

Baseline artifacts captured 2026-08-28 in `docs/baseline/` (application_log snapshot, llm_debug summary, DB state).

## Dependency graph

```
Wave 1 (independent, parallel now):
  T1  dependency & tooling hygiene
  T2  remove Haiku / chrome-in-chrome apply path
  T5  characterization tests for _get_profile_value / field matching
  T8  DB indexes + WAL
  T12 trim Dagster

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
- Defaults: classifier → `meta/llama-3.1-8b-instruct` @ NIM; browser-use → `deepseek-ai/deepseek-v4-flash-0731` @ NIM (`https://integrate.api.nvidia.com/v1`); `GUIDED_APPLY_MODEL` → `claude-sonnet-5`.
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

## T16a / T16b — Phase 4 (shape TBD by T15)

Ticket written after T15 reports. **16a (go):** browser-use = OffsiteApply primary, fallback to a *lightly* decomposed `_llm_guided_apply` (5 seams: `page_snapshot` / `decide_action` / `execute_action` / `detect_terminal_state` / `handle_auth`), triggers = {error|timeout|max-steps w/o verified submission} ∪ {NIM 429 after N retries} ∪ {claimed success but `verify_submission` false}; retire `ScriptApplyEngine`. **16b (no-go):** full decomposition of `_llm_guided_apply` as primary; browser-use shelved; retire `ScriptApplyEngine`.

---

## T17 — Scraper cleanup

**Phase:** 5 · **Risk:** medium · **Deps:** T12

- Replace the `while True: time.sleep()` bodies of `search_retriever.py` / `details_retriever.py` with thin wrappers over the existing Dagster ops; add `tenacity` retry/backoff around the Voyager calls in `scripts/fetch.py`.
- Move cookie extraction from Selenium to Playwright with a persisted `storage_state.json`, refreshed only on 401.
- Drop `selenium` from deps if nothing else uses it.

**Acceptance:** no `while True` in the standalone scripts; `tenacity` wraps the network calls; a discovery run works without launching Selenium when a valid `storage_state.json` exists.
