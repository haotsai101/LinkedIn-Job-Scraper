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
| 4/5 | **T33** — OffsiteApply flow reliability | ✅ **CLOSED** — merged (PR #25). Unified `verify_submission` (Rippling `/jobs?page=0` false-negative), blocked-domain jobs → `-3` not auto-fail. First pass at T31/T32; the rest split to their own PR (see `## T31 / T32`). |
| 5 — Phase 4 | **T16b** — decompose `_llm_guided_apply` on the Agent SDK (primary OffsiteApply path) + retire `ScriptApplyEngine` | ✅ **CLOSED** 2026-09-05 — PR 1 (#30) + PR 2 (#31) merged, QA passed (live run: 3 real applications inc. multi-step Rippling, 2 correct `-3` blocks, 0 errors). `ScriptApplyEngine` gone; OffsiteApply is a single decomposed step-loop engine. |
| 6 — Phase 5 | T17 — scraper cleanup | needs T12 ✓ |
| Follow-ups | T19 ✅ + T24 ✅ (PR pending) · T22 ✅ (PR pending) · T21 **CLOSED** (#33) · T20 ✅ (PR pending) · T30 **CLOSED — superseded by T38** (NIM classifier now opt-in; Agent SDK is the default) | P2–P3 |

**Direction change (2026-09-03):** T14b live-QA runs confirmed the Agent SDK classifier is 100% reliable where NIM's model isn't (T30), but NIM stays for OffsiteApply classification with the circuit breaker as the safety net. The browser-use spike (T15) is dropped — the free NIM tier can't host an agentic browser model reliably. Remaining apply-agent work goes straight to hardening + decomposing `_llm_guided_apply` on the Agent SDK.

### Follow-ups from the T27/T14b live apply runs (2026-09-01/02)

| # | Sev | Summary |
|---|---|---|
| T30 | P2 | ✅ **CLOSED — superseded by T38** (#38, QA 2026-09-06). New classifier `meta/llama-3.2-11b-vision-instruct` returned non-JSON ~1/3 of NIM-route calls in the real flow, and was 8-12s (not the ~1.5s probe). **Root cause found (T38):** NIM returns an *empty/whitespace* body for job descriptions over ~4–5K chars — reproduced directly (short desc → clean JSON; a real 6.7K-char posting → `JSONDecodeError` every time). Real postings are routinely 6–8K, so the NIM route fails on most real jobs; 3 in a row trips `run_session`'s `_MAX_CLASSIFY_FAIL_STREAK` and aborts the session, and the T27 breaker only catches `TimeoutError`, not parse failures. **Fix (T38):** the Agent SDK is now the default classifier for *all* jobs (reliable, same latency, rides the Claude subscription = free). NIM stays in the tree as an opt-in route behind `CLASSIFIER_ROUTE=nim`. |
| T31 | P2 | ✅ **CLOSED** (#34 + #37, QA 2026-09-06) — see `## T31 / T32` below. Numeric/scale free-text fields got prose instead of a bare number. Residual T40 fixed (PR pending). |
| T32 | P2 | ✅ **CLOSED** (#34, QA 2026-09-06) — see `## T31 / T32` below. Unmapped "years of &lt;skill&gt;" fields undersold to `0`. Residual T40 (catch-all branch order) fixed (PR pending). |
| T36 | P3 | (split out of T32) Playwright tab crash mid-fill → `applied=-2` auto-fail. Browser-stability concern, not form-answer quality — needs its own ticket. **Not started.** |
| T37 | P2 | ✅ **CLOSED** (#36, QA 2026-09-06) — see `## T37` below. Live QA of PR #34 found `_ask_llm`'s local numeric detection out of sync with `_coerce_numeric_answer` — long-labelled "Rate … (1-10)" scale fields still got prose. |
| T38 | P1 | ✅ **CLOSED** (#38, QA 2026-09-06) — see `## T38` below. Supersedes/closes T30. NIM classifier returns an empty body for descriptions over ~4–5K chars → most real OffsiteApply jobs fail to classify → session aborts. Agent SDK is now the default classifier for all jobs; NIM is opt-in (`CLASSIFIER_ROUTE=nim`). |
| T40 | P3 | ✅ **fixed (PR pending)** — (found by the T22/T38 QA run 2026-09-06) `_get_profile_value`'s `"years of experience"` catch-all ran *before* the T32 tiered "years of &lt;skill/role&gt;" branch, so `"How many years of experience do you have as a Lead?"` returned full `years_experience` (`4`) — asserted a whole career in a role the applicant has never held. Fix: new `_years_label_names_a_foreign_role_or_skill(l, profile)` guard on the three overall-experience catch-alls. A "years of experience" label is diverted to the tiered (flooring) branch **only** when it names a role/skill/domain FOREIGN to the applicant's profile ("as a Lead", "with COBOL", "in the insurance sector"). Generic phrasing ("as a whole", "in the software industry", "in the US") and the applicant's own role/skills keep the full figure — an under-claimed "1"/"2" trips "minimum N years" knockout filters. Unrelated pre-existing bug found while here → **T41**. |
| T41 | P3 | (found while implementing T40) `_get_profile_value`'s location branch does a tuple-membership substring check that matches `"city"` inside `"capacity"`, so `"years of experience in a professional/leadership capacity"` returns `profile["location"]` instead of a years figure. Pre-existing (same on `master`), **not** touched by T40. See `## T41` below. **Not started.** |
| T42 | P3 | (found verifying T40 against the live `user_profile.json`) T40's `_years_label_names_a_foreign_role_or_skill` "does the qualifier appear in the applicant's background?" check has a `len(w) >= 3` guard, so a 2-char skill/domain token (`ai`, `ml`, `go`, `ui`, `ux`, `qa`, `bi`, `r`) is **never** matched against the profile → always classified "foreign" → floored to `"1"`. Live: `"years of experience in AI/ML"` → `"1"` for an applicant with `RAG Architectures` / `LLM Fine-tuning` / `Vector Databases` skills, an M.S. in Data Science, and "AI applications" in the summary. Underclaim (safe direction, and `1` ≠ `0`), narrow (skills literally in the `skills` list as ≥3-char tokens — Python/Kubernetes/AWS/Go — resolve correctly via an earlier exact-match branch), but real for AI/ML-targeted applications. See `## T42` below. **Not started.** |
| — | P3 | (T14b reviewer note) Watch for orphaned `claude` processes after timeout-heavy runs — `asyncio.wait_for` on `llm.query` cancels the SDK generator mid-iteration; subprocess cleanup then depends on the SDK's `GeneratorExit` handling. |
| T39 | P3 | (found by the T22/T38 QA run 2026-09-06) OffsiteApply step-loop can't reach the apply form when a Greenhouse `boards.greenhouse.io/<co>` URL 30x-redirects to a company-hosted careers SPA (MongoDB). LLM scrolls, URL never changes, stuck-detection kills it → `applied=-2` → churns the `--reset-failed` pool forever. See `## T39` below. |

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
| T19 | P2 | ✅ **CLOSED** (#40, QA 2026-09-06) — `scripts/migrations/runner.py:run_pending_migrations` + `scripts/create_db.py:ensure_db_ready` run pending `NNN_*.py` migrations (tracked in `schema_migrations`, one-time online-backup, `threading.Lock`+`fcntl.flock` serialised) at every entrypoint: both retrievers, both scraper Dagster ops, and `apply_jobs.main`. QA: first run on the live production DB re-ran `001`/`002` as clean no-ops + recorded both + one backup, `integrity_check` ok; second run fully silent (no re-apply, no backup). Two reviewers verified idempotency on a prod-shape DB and deadlock-free concurrency under fork+spawn multiprocess + multithread stress. |
| T20 | P3 | ✅ **CLOSED** (#43, 2026-09-07) — `scripts/lint.sh` / `scripts/check.sh` bootstrap a project-local `.venv/` and run `ruff` (+ `pytest`); `ruff>=0.16.6,<0.17` / `pytest>=8.0,<10` pinned in `pyproject.toml`'s `dev` extra; `CLAUDE.md` + `README.md` point at the scripts. `check.sh` reports separate ruff/pytest exit codes and exits 0 when pytest passes and ruff only has lint findings. Verified on `master`: `./scripts/check.sh -q -k test_config` → ruff rc 1 (509 baseline), 23 tests pass, script exit 0. Tooling/docs only. |
| T21 | P2 | ✅ **CLOSED** (#33, QA'd 2026-09-06) — `fetch_job_details_op` had the same required-config bug T6 fixed for `search_jobs_op`; `details_schedule` (`RUNNING`, no run_config) failed config validation every 12h. Fixed with `Field(Int, default_value=25/30)`. |
| T22 | P2 | ✅ **CLOSED** (#37, QA 2026-09-06) — `run_session` loads `ats_domain` rows from `blocked_entities` once per session and the per-job check matches them (host-suffix) against `posting_domain`/`application_url`, marking a hit `applied=-3`. Operator-added domain blocks now fire with no code change. |
| T24 | P3 | ✅ **CLOSED** (folded into T19 / #40, QA 2026-09-06) — `ensure_schema_current`'s backfill re-run gate is now `LISTED_EPOCH_PENDING_PROBE_SQL` (`_epoch_fixable` mirrors the `_epoch_case` `WHEN` arms), so a permanently-unparseable `listed_epoch IS NULL` row no longer re-triggers the full-table backfill. Reviewer added `test_epoch_fixable_probe_never_drifts_from_backfill` (14 edge values, asserts probe-hit ⇔ backfill-fills-row). |

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

**Status:** ✅ **CLOSED** — PR 1 (#30) + PR 2 (#31) merged 2026-09-05, QA passed 2026-09-05. Split into two PRs because a single behaviour-preserving diff over ~1,500 lines of the primary production path was too large to review safely.

**QA (2026-09-05, `log-bug-detector`):** live `apply_jobs.py --auto --limit 12 --type OffsiteApply` run (started ~40s after PR #31 merged), with the 9 prior `applied=-2` jobs put back in the pool via `--reset-failed`. Result: `Applied 3 · Skipped 4 · Errors 0 · Blocked 2 · Deferred 3`. **Zero errors, zero refactor regressions** — no `_StepState` field errors, no stale-`page` after new-tab rebind, no `try/finally` swallowing a terminal return, no lost selector normalisation. The 3 applications were all real Rippling submissions confirmed by `verify_submission` via explicit confirmation text (not a URL heuristic) — including **OrthoFi (multi-step: résumé upload → React-Select pronouns → scroll → offscreen EEO-select → stuck-guard → deterministic submit), the exact job that looped in a React-Select fill and ended `-2` in a pre-T16b run.** The 2 blocks (Nelnet Workday, Bright Vision applytojob.com) went to `-3` via the pre-flight `_classify_domain` check. `ScriptApplyEngine` confirmed gone from all runtime paths. The 3 deferred jobs are the parked T30 NIM-classifier non-JSON issue (`nim_client.py`, not touched by T16b) — correctly left `pending`, fully retryable.

**Live-exercised this QA run:** `_page_snapshot`, `_decide_action`, `_execute_action` (upload / React-Select fallback / scroll / offscreen-click / deterministic-submit / stuck-guard), `_StepState` threading, `_detect_expired` (U.S. Bank expired → skipped), `_classify_domain` blocked→`-3`, `verify_submission`. **Still unit-covered only (no live hit this run):** `_handle_auth` (no offsite job needed login/account-creation), `_detect_terminal_state`'s dead-end/needs-human branch, T34's *mid-flow* blocked path (both blocks caught pre-flight). A future OffsiteApply run hitting a career site with account creation would close that gap — not required.

Split the ~1,500-line `_llm_guided_apply` into testable seams: `page_snapshot` · `decide_action` (the `llm.query` call) · `execute_action` · `detect_terminal_state` (applied / dead-end / needs-human — uses `verify_submission` from T33) · `handle_auth` (login + account creation + `EmailInbox` verification). Retire `ScriptApplyEngine` (the LLM-writes-a-Playwright-script path) — the step loop is the single OffsiteApply engine. Page representation → accessibility-tree / structured field list where practical (fewer tokens than raw DOM).

**PR 1 (#30, merged):** `ScriptApplyEngine` retired entirely (`script_engine.py` + its tests deleted, all refs removed). Seams extracted as methods on `OffsiteApplyFlow`, wired at the *same* call sites (no reordering) so behaviour is preserved:
- `_page_snapshot` / `_decide_action` — named wrappers over `_get_page_snapshot` / `_ask_llm_action`.
- `_classify_domain` — dedupes the spam/blocked-ATS/dead-end host check that was copy-pasted at 3 sites (pre-flight, per-step, post-nav).
- `_detect_expired` — closed/removed/not-found job detection.
- `_detect_terminal_state` — consolidates the consecutive mid-loop reCAPTCHA-widget / Cloudflare / expired-text / Greenhouse-security-code walls.
- `_handle_auth(phase="url"|"form")` — one entry point for SSO redirects, login-path pages, and mid-form password gates; **preserves the T34/T35 blocked(-3)-vs-failed(-2) split exactly**.
- Static config (`_SPAM_DOMAINS`, `_BLOCKED_AUTO_APPLY_DOMAINS`, `_LOGIN_PATHS`, `_SSO_DOMAINS`, expired/cloudflare patterns) hoisted to class attributes.
- New `tests/test_offsite_seams.py` (28 cases) covers every PR-1 seam in isolation. `test_blocked_status.py` (T34/T35) / `test_verify_submission.py` (T33) / `test_offsite_llm.py` / `test_bot_wall.py` all still green.

**PR 2 (#31):** `_execute_action` extracted — the 821-line `scroll`/`upload`/`fill`/`select`/`click` dispatch (React Select, Greenhouse jQuery-UI autocomplete, ITI phone, cover-letter guards, new-tab rebinding) moved to its own method **byte-for-byte** (one intended change: `_submit_clicked = True` → `state.submit_clicked = True`), wrapped in `try/finally`. New module-level `_StepState` (`__slots__`: `page`, `selector`, `forced_filled`, `submit_clicked`) threads the mutable per-step context: the orchestrator builds it, calls `_execute_action`, then reads back `state.page` (new-tab rebind), `state.selector` (`#<digit>`-id normalisation), `state.submit_clicked`. `forced_filled` is passed as the same dict object. The orchestrator loop body is now: guards → `done`/`failed` handling → `_execute_action` → history append. `_detect_terminal_state`'s mid-loop expired-text check now routes through `_detect_expired(check_url=False, body_text=…)`, sharing the one body-text read it already does for the Cloudflare check (gives `check_url`/`body_text` a real consumer; one read, silent on failure — matches master). 17 new `tests/test_offsite_seams.py` cases for the seam boundary (dispatch routing, new-tab rebind, submit latch + `_handle_submit` propagation, disabled-submit continue, `#<digit>` selector normalisation, CAPTCHA-exception → `"skipped"`, `forced_filled` sharing, single-read) + a `_coerce_numeric_answer` guard. All 230 tests green. `ruff check linkedin_apply.py` unchanged vs master (383 = 383).

**Acceptance:** each seam independently unit-tested (PR 1 ✅ 4 seams; PR 2 ✅ `execute_action`) — 230 tests; `ScriptApplyEngine` gone (PR 1 ✅); a live OffsiteApply run against known-failing jobs completes without the old function (✅ QA 2026-09-05 — 12-job run with the `-2` pool reset in, 0 errors, 3 real applications).

---

## T19 — Auto-run pending DB migrations on startup

**Phase:** P3 · **Risk:** low · **Deps:** T8, T9 · **Status:** ✅ fixed (PR pending) · Raised by log-bug-detector during Wave 1 QA.

T8's indexes + WAL and T9's schema changes only take effect when the operator manually runs the migration scripts. For an unattended agent that's a footgun. Add a lightweight "run all `scripts/migrations/NNN_*.py` that haven't been applied" step to `apply_jobs.py` startup (and/or a Dagster op), tracked via a `schema_migrations(id TEXT PRIMARY KEY, applied_at INTEGER)` table. Each migration is already idempotent, so worst case is a fast no-op.

**Fix (this PR):**
- New `scripts/migrations/runner.py:run_pending_migrations(db_path)` — discovers `scripts/migrations/NNN_*.py` in sorted order, runs any whose stem is absent from `schema_migrations(id TEXT PRIMARY KEY, applied_at INTEGER)`, and records each stem **only after that migration's own `migrate()` has committed** (a migration that raises leaves the tracking table untouched, the exception propagates, and later migrations don't run). Migration module contract documented in `scripts/migrations/__init__.py`: each `NNN_*.py` exposes `migrate(db_path)` (already true of `001_indexes.py` / `002_schema.py` — their `if __name__ == "__main__"` blocks still work).
- **Concurrency**: the discover → backup → apply → record critical section is serialised with `_INPROC_LOCK` (a module `threading.Lock`) **and** an advisory `fcntl.flock` on `<db>.migrate.lock`. Dagster's multiprocess executor starts `search_jobs_op` + `fetch_job_details_op` concurrently on a not-yet-migrated DB; without the lock the two `002_schema` runs raced its check-then-`ALTER` → `duplicate column name: listed_epoch`. The loser blocks, then **re-reads `schema_migrations` inside the lock**, finds nothing pending, returns clean. A cheap unlocked pre-check keeps the already-migrated startup lock-free.
- **DB backup**: before the first pending migration of a run, `linkedin_jobs.db` → `linkedin_jobs.db.bak-<epoch>` via the **SQLite online-backup API** (`sqlite3.Connection.backup` — consistent even under a concurrent writer, unlike a plain file copy that can catch a torn WAL). Gated on there being ≥1 pending migration — an up-to-date startup takes no backup. `.gitignore`'s `linkedin_jobs.db*` covers the backup name.
- New `scripts/create_db.py:ensure_db_ready(conn, cursor)` — the single "bring the DB fully current" entry point: `create_tables()` (fresh DDL + `ensure_schema_current` + indexes + WAL) then `run_pending_migrations()`. Derives the DB path from the connection (`PRAGMA database_list`); an in-memory / temp DB with no path skips the numbered-migration step (step 1 already made it current).
- Wired at **every** entry point that opens the DB: `search_retriever.py`, `details_retriever.py`, `scripts/dagster_retrievers.py` (`search_jobs_op`, `fetch_job_details_op` — the apply op shells out to `apply_jobs.py` so it inherits the CLI path), and `apply_jobs.py:main()`. Each previously called `create_tables` / `migrate_db` directly; those calls are now `ensure_db_ready`. `apply_jobs._ensure_apply_schema` (hit per-call by `get_pending_jobs`) stays the cheap `ensure_schema_current`-only path — migration discovery + backup run once per process from `main()`.
- `DatabaseStructure.md` regenerated (adds the `schema_migrations` table) via `python scripts/dump_schema.py`.
- New `tests/test_migration_runner.py` (28 cases): all-pending → both applied + recorded; one-time backup; second call no-op (no re-apply, no new backup); partial state (`001` pre-recorded → only `002` runs); no backup when nothing pending; discovery sorted + filtered; **4 threads racing a fresh DB → exactly one applies, no exception**; **mid-sequence migration failure → its id absent, exception propagates, third migration never ran, retry resumes**; `ensure_db_ready` on a fresh checkout / bare pre-migrations DB / in-memory DB / idempotency; plus the T24 cases below (incl. a 14-value `_epoch_fixable` ⇔ backfill drift-guard).

**Acceptance:** ✅ a fresh checkout + first `apply_jobs.py` (or retriever / Dagster op) run leaves `linkedin_jobs.db` fully migrated with no manual step; subsequent startups are a sub-millisecond no-op; concurrent first-run Dagster ops no longer race.

---

## T20 — Pin `ruff` into the interpreter that runs the agent

**Phase:** P3 · **Risk:** trivial · **Deps:** T1 · **Status:** ✅ fixed (PR pending) · Raised by log-bug-detector during Wave 1 QA.

`ruff` is in `[project.optional-dependencies].dev` but the `/opt/anaconda3/bin/python` env that actually runs the agent doesn't have it, so `ruff check .` only works in a fresh venv. Either document that lint runs in the venv, add a `make lint` / `scripts/lint.sh` that bootstraps it, or install it into the anaconda env and note that in `CLAUDE.md`.

**Acceptance:** `ruff check .` runs from the documented dev setup with one obvious command.

**Fix (this PR):**
- `scripts/_venv.sh` (sourced helper) — refuses direct execution (`(return 0 2>/dev/null) || exit 1`), creates a project-local `.venv/` if absent, then `python -m pip install -q -e ".[dev]"` (module form survives a partially-broken venv; idempotent; venv never recreated once it exists). Exports `REPO_ROOT` / `VENV_DIR`.
- `scripts/lint.sh` — `source _venv.sh` then `exec .venv/bin/ruff check <repo>` (forwards extra args, e.g. `--fix`).
- `scripts/check.sh` — same bootstrap, then `ruff` + `pytest` (both via `python -m`); prints a `==> summary: ruff exit N … pytest exit N` line and exits 0 when pytest passed and ruff either passed or only reported lint findings (rc 1), non-zero on a ruff crash (rc ≥ 2) or any pytest failure. No Chromium probe — no test drives a browser yet; the first browser test lands `playwright install` with real context.
- `pyproject.toml` `dev` extra: `ruff>=0.16.6,<0.17` (single minor — a ruff minor can add/retire rules and break the lint-parity baseline), `pytest>=8.0,<10` (floor + major ceiling; suite only uses raises/warns/parametrize/fixture/MonkeyPatch).
- `CLAUDE.md` "Lint / test" now points at `./scripts/lint.sh` / `./scripts/check.sh`, keeping the raw `ruff check .` / `pytest` lines for anyone with an active `.[dev]` venv.
- `.venv/` + `*.pyc` already covered by `.gitignore` (`/.venv/`, `*.pyc`) — no change needed. No CI (`.github/workflows/` absent). No `.py` changes; existing ~500 ruff findings untouched (out of scope).

---

## T21 — `fetch_job_details_op` required-config bug

**Phase:** follow-up · **Risk:** low · **Deps:** none · **Status:** ✅ **CLOSED** — merged (PR #33, 2026-09-06), QA passed 2026-09-06 (`validate_run_config` against the real `fetch_details_only` / `search_jobs_only` / `search_and_fetch_jobs` job objects: all validate with empty config, explicit override still honoured; `scripts.definitions` imports clean; reviewer independently confirmed the pre-fix schema raises `DagsterInvalidConfigError` so the new tests are genuine regression cover). Raised by the T6 reviewer.

`scripts/dagster_retrievers.py:fetch_job_details_op` had `config_schema={"max_updates": int, "sleep_time": int}` with both fields required, but `details_schedule` (`default_status=RUNNING`) supplies no `run_config` — so scheduled enrichment failed config validation every 12h (only `unscraped_jobs_sensor` provided config). Same class of bug T6 fixed for `search_jobs_op`. Fixed with the same `Field(Int, default_value=...)` treatment: `max_updates=25`, `sleep_time=30` (matches the existing `.get()` fallbacks; the sensor passes `sleep_time:30` + a dynamic `max_updates` that still overrides). New `tests/test_dagster_op_config.py`.

**Acceptance:** ✅ `details_schedule`'s job produces a valid run with no config; the sensor path is unaffected.

---

## T22 — Wire `blocked_entities.ats_domain` to `run_session`

**Phase:** follow-up · **Risk:** low · **Deps:** T9 (done) · **Status:** ✅ **CLOSED** — merged (PR #37), QA passed 2026-09-06. Raised by the T9 reviewer.

**QA note (2026-09-06):** the `run_session` apply-loop path was not hit by a live run (no operator-blocked domain landed in the 12-job pool; the two `-3` blocks that run came from the `linkedin_apply.py` pre-flight Workday check, a separate T33/T34 mechanism). Validated instead by: (a) a direct real-DB check — `load_session_blocked_domains(cur)` returns the operator `ats_domain` row unioned with the seed rows; a real pending `haystack.cv` job → `_match_blocked_domain` truthy; `job-boards.greenhouse.io` → `None` (negative control); (b) the reviewer's `inspect.getsource` wiring proof that `run_session` calls `_match_blocked_domain(session_blocked_domains, posting_domain, application_url)` and marks a hit `applied=-3` / `blocked_count++`; (c) 7 of 11 new tests fail on the pre-fix code. A future run hitting an operator-blocked domain would be belt-and-suspenders, not required.

T9 created `blocked_entities` and seeds `ats_domain` rows, but `run_session`'s URL check still reads `BLOCKED_DOMAINS` (derived from the frozen `BLOCKED_ENTITIES_SEED` Python constant), so an operator adding an `ats_domain` row to the table is silently ignored. Have `run_session` load `ats_domain` patterns from the table once per session (mirror the `get_pending_jobs` approach), making the table authoritative for domain blocks too.

**Fix (this PR):**
- New helper `apply_jobs.load_session_blocked_domains(cursor)` → `BLOCKED_DOMAINS | {pattern FROM blocked_entities WHERE kind = 'ats_domain'}`, `.strip().lower()`-normalised, whitespace-only rows dropped (a bare `'   '` would otherwise normalise to `""` and wildcard-match every job). Union keeps the seed authoritative on a not-yet-migrated DB; missing table → `sqlite3.OperationalError` caught → falls back to the constant.
- New host matcher `_match_blocked_domain(blocked_domains, *candidates)`, a sibling of `_match_spam_domain` — both now share extracted `_host_of` / `_host_matches` helpers (host-suffix match: `rex.zone` ≠ `forex.zone`; **not** the old substring `in` test).
- `run_session` loads `session_blocked_domains` once at session start and the per-job check calls `_match_blocked_domain(session_blocked_domains, posting_domain, application_url)`. **Corrected from PR #37 round 1:** the check previously read `url` (`= j.job_posting_url`), which is *always* a `linkedin.com/jobs/view` link (verified 123/123 rows), so no `ats_domain` pattern could ever fire. The ATS host lives in `posting_domain` / `application_url` (the same inputs the spam filter uses).
- An operator explicitly blocking a domain means "never attempt this" → the check now marks `applied = -3` (blocked, excluded from `--reset-failed`), consistent with T33/T34's `-3` for un-automatable ATS domains. Was `-1`.
- `BLOCKED_COMPANIES` has no analogous staleness — not read anywhere in `run_session`; company blocks are already table-authoritative via `get_pending_jobs`'s `NOT EXISTS`.

New `tests/test_session_blocked_domains.py` (15 cases): operator row blocks end-to-end with no code change; the linkedin `job_url` never trips the check; host-suffix not substring; whitespace-only row can't wildcard; table-missing fallback; seed still blocks; `inspect.getsource` guard that `run_session` feeds `posting_domain`/`application_url` (not `url`) into the matcher and marks `-3`. `pytest tests/` 255 green; `ruff check apply_jobs.py` 42 findings (43 pre-existing on master − 1 removed here, 0 added).

**Acceptance:** ✅ adding an `ats_domain` row to `blocked_entities` blocks that domain on the next apply session with no code change.

---

## T24 — Backfill re-run gate can't detect permanently-unparseable rows

**Phase:** follow-up (folded into T19) · **Risk:** low · **Occurrence:** 0 in current data · **Status:** ✅ fixed (folded into T19) · Raised by log-bug-detector during Wave 2 QA.

`ensure_schema_current()` gates the `listed_epoch` backfill behind `SELECT 1 FROM jobs WHERE listed_epoch IS NULL LIMIT 1`. Rows whose `original_listed_time`/`listed_time` are both unparseable stay `NULL` after the `CASE ... ELSE NULL` backfill, so they keep satisfying the probe → the full-table backfill `UPDATE` (scan + write lock) runs on every `ensure_schema_current()` call and never converges. Current `linkedin_jobs.db`: 1263/1263 parse, so zero impact. Fix: narrow the probe to rows the backfill *can* fix (mirror `_epoch_case` conditions), or sentinel-mark unfixable rows. Fold into T19.

**Fix (folded into T19):** chose the **narrow-the-probe** option (no data mutation, smallest change). New `scripts/create_db.py:_epoch_fixable(col)` returns a SQL boolean mirroring the two `WHEN` arms of `_epoch_case` **exactly** — including that `strftime('%s', <invalid-iso>)` is `NULL`, so a row matching the ISO `LIKE` shape but holding an unparseable date is correctly *not* fixable. `LISTED_EPOCH_PENDING_PROBE_SQL` = `listed_epoch IS NULL AND (_epoch_fixable(original_listed_time) OR _epoch_fixable(listed_time))`, and `ensure_schema_current`'s re-run gate now uses it. A row whose both source columns are permanently unparseable no longer satisfies the probe, so the backfill converges after one pass. The `_column_exists`-just-added branch still runs the backfill unconditionally once (correct — a fresh column always needs its first fill).

**Acceptance:** ✅ `tests/test_migration_runner.py::test_ensure_schema_current_backfills_at_most_once_with_unparseable_row` — a DB with one fixable + one permanently-unparseable NULL row: first `ensure_schema_current()` issues the backfill `UPDATE`, second call does not (spied on `cursor.execute`).

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

**Phase:** T33 follow-up · **Risk:** low · **Status:** ✅ **CLOSED** — merged (PR #26, 2026-09-04), QA passed 2026-09-04 (`tests/test_blocked_status.py` verified fail-pre-fix/pass-post-fix by both SWE and reviewer; live post-merge run showed no regression in EasyApply/spam-filter paths — did not itself exercise the changed branches, see T35's QA note for the same caveat and a suggested targeted follow-up run). Found during T33 live validation run 2026-09-02/04.

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

**Phase:** T33/T34 follow-up · **Risk:** medium (silent, already live in production) · **Status:** ✅ **CLOSED** — merged (PR #28, 2026-09-04), QA passed 2026-09-04. Found by the T34 SWE agent, confirmed independently by the T34 PR reviewer, 2026-09-04.

**QA note (2026-09-04):** post-merge live run (`apply_jobs.py --auto --limit 3`) showed no regression (1 EasyApply submitted+confirmed, 2 spam-domain pre-classification skips, DB deltas exact match, 0 errors) but did not itself reach a login-wall branch, so it's evidence of "no regression in the surrounding flow," not direct live confirmation of the new `-2`/`-3` split. Direct evidence comes from `tests/test_blocked_status.py`'s two new pre-flight-call-site tests, independently re-run by the reviewer against the pre-fix commit and confirmed to fail there / pass post-fix. QA recommendation: sufficient to close; optionally, the `pending` pool has 3 Workday `OffsiteApply` postings (same ATS class that originally exposed T34) that a future `--type OffsiteApply` run could hit for added production confidence — not required.

**Operator note:** consider auditing existing `applied=-3` rows dated on/after 2026-07-03 (T33's merge) for ones that trace to the pre-flight login-path call site rather than a genuine blocked-domain hit — those may have been transient login failures wrongly stuck non-retryable before this fix. Not automated.

`_handle_auth_page` (`linkedin_apply.py:~4671-4680`) has two failure branches: "no stored credentials for this domain" and "stored credentials exist but the login attempt failed" (transient/2FA/rate-limit — should be retryable). The second branch's `print(...)` has no `return` statement after it, so execution falls through into the same final `return False` the true no-credentials case uses — collapsing both into one signal. Its caller, the **pre-flight** login-path check at `linkedin_apply.py:3212-3217`, maps any `False` to `"blocked"` → `applied=-3` (excluded from `--reset-failed`). Net effect: since T33 merged (`c962fef0`, 2026-07-03), **any offsite job whose stored credentials exist but whose login fails for a transient reason has been permanently written off as `-3` instead of retried as `-2`** — the exact mis-classification T33/T34 exist to prevent, just at a third call site neither ticket's scope covered. (T34's mid-loop equivalent, `_handle_auth_page`'s sibling check at `:3320-3323`, does NOT have this bug — it correctly distinguishes the two cases; only the pre-flight call site inherits the fallthrough.)

**Fix:** add the missing `return` (or an explicit distinct sentinel, e.g. a 3-way return / exception, if `:3212-3217` needs to tell the two cases apart — check what that call site currently does with the `bool` and whether a signature change ripples further) after the "stored credentials exist but login failed" print in `_handle_auth_page`, so that branch reaches its own outcome instead of falling through to the no-credentials `return False`. Confirm `:3212-3217` still correctly maps the true no-credentials case to `"blocked"`/`-3` and the login-failed case to `"failed"`/`-2` (mirroring the mid-loop check T34 already got right).

**Operator recovery (after this PR merges):** some current `applied=-3` rows may actually be transient login failures wrongly stuck as non-retryable. Consider auditing/reconsidering `-3` rows going back to 2026-07-03 whose blocked reason traces to this call site (vs. a genuine blocked-domain hit) — not automated here, DB layer out of scope for this ticket. **Flagged for the operator, not done in this PR.**

**Acceptance:** a job where `_handle_auth_page` is reached via the pre-flight login-path check, stored credentials exist for the domain, and the login attempt itself fails, ends the run `applied=-2` (retryable), not `-3`. A job with no stored/discoverable credentials at all still correctly ends `-3`. Regression test covering both branches of `_handle_auth_page`'s caller at `:3212-3217` (not just the mid-loop `:3320-3323` one T34 covered). `pytest tests/` green.

**Fix (this PR):** `_handle_auth_page` (`linkedin_apply.py:4628`) changed its return type from a plain `bool` to `bool | str`: `True` on successful auth, `"failed"` when stored credentials exist for the domain but the login attempt itself fails, `"blocked"` when no stored/discoverable credentials exist at all — mirroring the `"blocked"`/`"failed"` string idiom `run_session` and the mid-loop check already use. The missing `return` after the "login failed with stored credentials" print (`:4679`) is now explicit (`return "failed"`) instead of falling through into the no-credentials branch's `return "blocked"` a few lines later. Its sole caller — the pre-flight login-path check (`:3212-3217`) — now branches on `ok is not True` and inspects the string: `"failed"` → `return "failed"` (`applied=-2`, retryable), anything else → `return "blocked"` (`applied=-3`, unchanged). `_handle_auth_page` has exactly one call site in the codebase (confirmed by grep), so no other caller needed updating. Added two regression tests to `tests/test_blocked_status.py` targeting this pre-flight call site specifically (T34's existing tests only covered the mid-loop `:3320-3323` check, which never had this bug): a login-path URL (e.g. `/login`) with no stored credentials → `"blocked"`, and the same URL with stored credentials that fail to log in → `"failed"`. `pytest tests/` (186 tests) green; `ruff check` on the two changed files clean (the pre-existing ~400 lint findings elsewhere in `linkedin_apply.py` are untouched debt, not introduced by this change).

---

## T31 / T32 — form answer quality (numeric fields + undersold "years of X")

**Phase:** T33 follow-up · **Risk:** low · **Status:** ✅ **CLOSED** — merged (PR #34), residual gap closed by **T37** (PR #36), QA passed 2026-09-06. **Follow-up 1:** live QA of PR #34 found the T31 symptom still reproducing on the EasyApply path for *long-labelled* scale fields — `_ask_llm`'s own local numeric-question detection had never been brought in sync with `_coerce_numeric_answer`. Closed by **T37**. The PR #34 work itself (the `_get_profile_value` tiering, the `_coerce_fill_value` seam) is sound and untouched by T37. **Follow-up 2 (closed by T40 — PR pending):** the 2026-09-06 QA run showed `_get_profile_value`'s `"years of experience"` catch-all short-circuited the T32 tiered branch — `"How many years of experience do you have as a Lead?"` → full `years_experience` (`4`), asserting a whole career as a Lead for an applicant who has never held the title. Mild overclaim via a pre-T32 code path (not a regression). Fixed in **T40**: a `_years_label_names_a_foreign_role_or_skill(l, profile)` guard now routes a "years of experience" label to the tiered branch only when the role/skill/domain it names is foreign to the applicant's profile; generic phrasing and the applicant's own role/skills keep the full figure (avoids trading the overclaim for underclaim knockouts). While implementing T40 an unrelated pre-existing bug surfaced (the location branch matches `"city"` inside `"capacity"`) — filed as **T41**, not touched here. · From the T27/T14b live apply runs 2026-09-01/02.

Both bugs live in the same code area (form answer generation), so they ship together. T33 (PR #25) landed a first pass — the `_coerce_numeric_answer` helper and a capped `_get_profile_value` "how many years" branch. This PR closes the paths that pass left uncovered.

### T31 — numeric / 1-N-scale fields got prose instead of a number
Symptom: "Rate your experience (1-10)" / "How many years…" free-text fields filled with `"I would rate my experience at an 8 out of 10…"` instead of `8`. Seen on both EasyApply and OffsiteApply.

**Fix:** `_coerce_numeric_answer` (the shared coercion point — numeric-label detection + prose→bare-int + range clamp + sensible fallback, no-op for genuine free text / selects / textareas) is now wired into **every** fill path:
- EasyApply one-shot LLM fill (`linkedin_apply._ask_llm`) — already wired in T33, unchanged.
- OffsiteApply step loop — extracted the coercion into a small `OffsiteApplyFlow._coerce_fill_value` seam called by the orchestrator before dispatch (`_execute_action` itself is untouched — T16b PR-2 byte-for-byte seam preserved). It resolves the target field from the snapshot by `selector` (`#id`/`[name]`) to get the real field `type` (so a `<textarea>` passes straight through). When the selector is a CSS-class / xpath / `:has-text` pattern that resolves to nothing, it falls back to the LLM's `text` click-target hint — but **only for a value that already contains a digit and is ≤ 40 chars**: pure extraction (`"…an 8 out of 10"` → `"8"`), never fabricating a number from the profile or collapsing a free-text paragraph.
- **New:** the interactive `[f]` focused-field helper (`apply_jobs._llm_fill_focused`) now coerces its result too — it was the one fill path with no coercion.
- Both LLM fill prompts (`_ask_llm`, `_ask_llm_action`) gained a "reply with just a number" instruction for years/scale questions.

### T32 — unmapped "years of &lt;skill&gt;" fields undersold to `0`
Symptom: "Years of Data Engineering experience" → `0` for an applicant with `years_experience: "4"`; "years with C#" / ".NET Framework" → `0` for a 4-YoE engineer.

**Fix:** `_get_profile_value`'s "years of &lt;skill&gt;" branch was broadened from matching only `"how many years"` / `"how many months"` to also catch `"years of"` / `"years with"` phrasings (the reported label "Years of Data Engineering experience" has no "how many" prefix), excluding `"relevant"` / `"total"` questions which want the full figure and are handled downstream. The fallback is **tiered** so it neither undersells to `0` nor fabricates tenure for a skill never touched:
- **listed skill** — the question names a skill in `profile["skills"]` (word-boundary regex, `/`-split, `#`/`+` aware) → full `years_experience`, still capped at that figure (never inflated);
- **adjacent skill** — a content word from the question (≥ 4 chars, not form boilerplate) also appears in the applicant's `current_title` / `headline` / `summary` / skill list → `min(years_experience, 2)`;
- **genuinely unrecognised** (COBOL, "management" for an IC, …) → `"1"` — a minimal non-zero floor that dodges the auto-filter without claiming experience that isn't there (matches the AI/ML branch and the no-overall-figure case);
- **never `0`.** A truthful zero comes only from `_coerce_numeric_answer`'s negative-phrase guard ("none", "never", "n/a") acting on the LLM's own answer text.

Reviewer feedback addressed: the earlier revision returned a blanket `min(tot, 2)` = `"2"` for every unlisted skill (COBOL, management, …) — the opposite failure to underselling. The adjacency tier fixes that; the LLM-prompt wording was also softened from "default to your overall years of experience" to "a reasonable non-zero figure, not exceeding your overall years of experience".

**Tests:** `tests/test_profile_value.py` — numeric detection positive + negative, the tiered "years of X" fallback (listed → full tenure, adjacent → 2, unrecognised/COBOL/management → 1, no figure → 1), "relevant"/"total" still full, genuine-`0` regression. `tests/test_offsite_seams.py` — `_coerce_fill_value` resolves the field type from the snapshot (textarea passes through), and the `text`-hint fallback is extraction-only (prose paragraph preserved, long value preserved, digit-bearing short value extracted). Full suite: **238 passed**. `ruff check .`: 512 = 512 (pre-existing repo lint debt untouched).

**Out of scope / follow-up:** T32's original row also noted a Playwright tab crash mid-fill → `applied=-2` — that's a browser-stability concern, not form-answer quality. Split out as **T36** (P3) in the follow-up table; no quick guard added here.

---

## T37 — `_ask_llm` routes long-labelled scale questions to prose, bypassing T31 coercion

**Phase:** T31/T32 follow-up · **Risk:** low · **Status:** ✅ **CLOSED** — merged (PR #36), QA passed 2026-09-06 (on unit-test strength — 272 tests, reviewer confirmed the pre-fix tree fails the new `_coerce` cases; the 2026-09-06 live run had no scale field / no LLM-driven fill so the unified detection was not live-reproduced — noted, not blocking). Found by live QA of PR #34 (T31/T32), EasyApply path.

**Symptom (still live after PR #34):** a job with three fields labelled `"Rate your experience (1-10) designing and building production data pipelines using SQL and Python."` (and near-identical variants) got filled with prose — `"I would rate my experience a 7. Over the…"`, `"6 — I have hands-on experience building…"`, `"I would rate my experience a 7 out of 10…"` — instead of a bare `7` / `6`. This is exactly the T31 failure mode, on a field T31's shared `_coerce_numeric_answer` already recognises.

**Root cause:** `_ask_llm` (`linkedin_apply.py`) did its *own* numeric-question detection with a narrow hardcoded tuple (`"how many years"`, `"years of experience"`, `"years experience"`, `"how many months"`) that never matched `"rate your experience (1-10) …"`. With that check False and the label 90+ chars, `is_long_form = (… or len(label) > 60) and not _is_numeric_question …` evaluated True → the field took the "write a professional 2-4 sentence answer" prompt, and then `if not is_long_form:` skipped the `_coerce_numeric_answer(...)` call entirely. Meanwhile `_coerce_numeric_answer` and its `_NUMERIC_LABEL_HINTS` constant already correctly recognise `"(1-10)"`, `"rate your"`, `"on a scale"`, `"1 to 10"`, etc. and would have extracted the range-clamped digit — the two detections had simply drifted apart. (T33 wired `_coerce_numeric_answer` *into* `_ask_llm`; it did not unify the `is_long_form` gate's own numeric test with it.)

**Fix:** extracted `_coerce_numeric_answer`'s field-is-numeric predicate into a module-level `_label_is_numeric(label, kind) -> bool` (the `_NUMERIC_LABEL_HINTS` membership test + the `\brate\b|\brating\b` regex + the up-front select/radio/checkbox/textarea/contenteditable/**email/tel/url** exclusion). Both `_ask_llm` (for the `is_long_form` exclusion) and `_coerce_numeric_answer` (for its `is_numeric` gate) now call it — single source of truth, cannot drift again. Net behaviour: `"Rate your experience (1-10) designing and building production data pipelines…"` with `kind="text"` → `_label_is_numeric` True → `is_long_form` False → non-long-form prompt → `_coerce_numeric_answer` runs → range-clamped bare integer. Genuine long free-text prompts (`"Describe your experience building data pipelines"`, `"Why do you want to work at Acme?"`, `"Tell us about a time you…"`) contain none of the numeric hints and are unaffected — they still get the 2-4 sentence prose path.

**Follow-up from PR-review round 1 (an inverse regression the first cut invited):** routing `_ask_llm`'s `is_long_form` decision through the *broad* `_NUMERIC_LABEL_HINTS` list is too aggressive — those tokens (`"how many"`, `"rate your"`, `\brate\b`) were tuned for post-hoc coercion, not prompt routing. A long behavioural/STAR question that merely *contains* one (`"How many times have you had to advocate for an unpopular decision? Give an example."` → prose answer `"…most memorably in 2021 when I…"`) started taking the terse prompt and having its first stray digit grabbed (`"2021"`). Two guards added:

1. **`_FREE_TEXT_LABEL_CUES` + `_label_has_free_text_cue(label)`** — a label containing `describe`, `tell us/me`, `explain`, `give an example`, `walk us/me through`, `why do/are you`, `share an experience`, `share a time`, `a time when/you` is a written-answer field. `_ask_llm` forces `is_long_form = True` for these (regardless of numeric hints), and `_coerce_numeric_answer` returns the answer untouched. This is deliberately **not** folded into `_label_is_numeric` — that would also disable `_coerce_numeric_answer`'s safety net for the OffsiteApply / focused-field fill paths.
2. **conservative digit-grab in `_coerce_numeric_answer`** — when the label states no explicit range and `kind != "number"`, the digit is only trusted if the answer already reads as a number. A prose sentence that merely contains a digit (`"…took down 3 services for 40 minutes."`) falls through to the no-digit handling. An explicit range in the label (`(1-10)`, `1 to 5`) keeps the unconditional grab.

*(Deviation from the review's literal spec, flagged: the reviewer's suggested `re.fullmatch(r"\D*\d+\D*", s)` still matches their own repro string `"…in 2021 when I…"` (one digit run, surrounded by non-digits). Tightened to `[\W_]*\d[\d\W_]*` — only punctuation/whitespace may surround the number, no words — so both review repro cases are preserved as prose.)*

**Follow-up from PR-review round 2 (round-1's digit-grab was too conservative):**

3. **strong rating cue = explicit range.** Round 1 keyed "strong signal" only on a literal `(1-N)` in the label, so a range-*less* rating label (`"Rate your Python proficiency"`, `"On a scale, rate your SQL expertise"`) + a >40-char prose answer fell through to prose — a thin re-open of T31, and range-less rating labels are common. `_coerce_numeric_answer` now treats `re.search(r"\brate\b|\brating\b|on a scale|scale of", lab)` as equivalent to an explicit range for the unconditional grab. The `_label_has_free_text_cue` short-circuit runs first, so `"Describe a time you had to rate a peer…"` still stays prose.
4. **digit-grab trust widened** for the no-range / non-`number` path: the answer is trusted when it is `<= 60` chars (was 40), **or** the digit is the first token (`re.match(r"\s*\d", s)`), **or** the digit is glued to a years/months unit (`re.search(r"\b\d+\s*\+?\s*(?:years?|yrs?|months?|mos?)\b", …)`), **or** the whole answer is just the number with punctuation (`re.fullmatch(r"[\W_]*\d[\d\W_]*", s)`). This restores extraction from a verbose "how many years" answer (`"I have approximately 5 years of professional experience with Python"` → `5`) that round-1 dropped to the T32 undersell, without re-admitting the STAR-prose false positives (their stray digits are neither first-token nor unit-glued, and the sentences run past 60 chars).

**Known gaps (accepted, low priority):**
- A **short** cue-bearing label routes to the prose prompt in `_ask_llm` even when a terse answer is wanted (`"Tell us your salary expectations"` → `_label_has_free_text_cue` True). Rare — salary fields are usually labelled "Desired salary" / "Compensation" and `_get_profile_value` handles those deterministically before the LLM is consulted.
- A range-less rating label whose prose answer leads with a *year* literal before the rating digit (`"Rate your experience"` → `"Since 2019 I've grown a lot, now maybe a 9"`) grabs `2019`. Pre-existing for ranged labels too (there it is at least clamped); rating answers rarely embed years. Not guarded.

**Also folded in (QA hygiene, same PR):** `apply_jobs.write_session_log` did `existing = json.loads(...)` then `existing["sessions"].append(report)`. Valid JSON of the wrong shape (e.g. a bare `[]`) slips past the `except Exception` parse guard and then raises `TypeError: list indices must be integers or slices, not str`, crashing the post-session bookkeeping of an otherwise-successful run. Added a shape check: after loading, `if not isinstance(existing, dict) or not isinstance(existing.get("sessions"), list): existing = {"sessions": []}` (also covers `{"sessions": 5}` / `{"sessions": {}}`).

**Tests:** `tests/test_profile_value.py` — `_label_is_numeric` positive/negative (incl. `select`/`textarea`/`email`/`tel`/`url` kinds); `_label_has_free_text_cue`; `_coerce_numeric_answer` on the live-QA prose answers, the STAR-with-hint-token repros (prose kept), the conservative digit-grab (long prose w/ stray digit kept, short/bare-number reduced, `kind="number"` always reduced), the range-less rating label (`"Rate your Python proficiency"` + verbose prose → bare int; cue-bearing `"Describe a time you rated a peer…"` still prose), and the years-unit / first-token trust (`"…5 years of professional experience…"` → `5`). `tests/test_offsite_llm.py` — `_ask_llm` w/ mocked `llm.query`: prose for a `"Rate … (1-10) …"` field → bare int, no 2-4-sentence prompt; genuine free-text → prose; STAR-with-hint-token → prose. `tests/test_write_session_log.py` (new) — bare-`[]`, dict-without-`sessions`, wrong-typed `sessions`, well-formed append, missing file, corrupt JSON. New `_coerce` tests were verified to fail cleanly (assertion, not collection error — the `_label_is_numeric` / `_label_has_free_text_cue` test bindings use `getattr`) against the pre-fix tree. Full suite: **272 passed**. `ruff check .`: 509 = 509 (no new findings).

**Acceptance:** a `kind in ("text","number")` field whose label matches `_coerce_numeric_answer`'s numeric hints, has no free-text cue, and (regardless of label length) never takes the long-form prose path in `_ask_llm` and always passes through `_coerce_numeric_answer`; a range-less rating label still reduces a verbose answer to a bare integer; a STAR / "describe …" question that merely contains a hint token still gets prose and is never digit-grabbed; `write_session_log` does not raise on a valid-but-wrong-shape `application_log.json`; `pytest tests/` green.

## T38 — Default the relevance classifier to the Claude Agent SDK; make NIM opt-in

**Phase:** T27 / T14 follow-up · **Risk:** low · **Status:** ✅ **CLOSED** — merged (PR #38), QA passed 2026-09-06 (clean full pass: 12/12 jobs classified via Agent SDK, 0 NIM calls, `Deferred: 0` vs `Deferred: 3` on every prior run; the BECU / Aalyria / DailyPay canaries that deferred every run for days all classified `✓ relevant` first try; session did not abort). **Supersedes & closes T30.** Decided by the project owner 2026-09-06.

**Root cause:** the NIM classifier route (`_classify_nim` → `nim_client.classify_via_nim`, model `meta/llama-3.2-11b-vision-instruct`) returns an **empty / whitespace response** for job descriptions over ~4–5K chars. Reproduced directly: short descriptions classify fine (clean JSON, 1.7–10s); a real 6.7K-char posting → `JSONDecodeError: Expecting value: line 1 column 1 (char 0)` every time. Real job postings are routinely 6–8K chars, so the OffsiteApply classifier route fails on most real jobs. Three consecutive failures trip `run_session`'s `_MAX_CLASSIFY_FAIL_STREAK` → the whole session aborts. The T27 circuit breaker only catches `TimeoutError`, not parse failures, so it never engages for this. This blocked QA runs for days (BECU / Aalyria / DailyPay deferred every run).

**Fix:**
- New `config.get_classifier_route()` reads `CLASSIFIER_ROUTE` (`agent` | `nim`, default `agent`; empty/bad → `agent`, non-empty bad value warns). Resolved once at import into `apply_jobs._NIM_CLASSIFIER_ENABLED`.
- `JobAgent.classify` routes to `_classify_nim` **only** when `_NIM_CLASSIFIER_ENABLED and application_type == "OffsiteApply" and not prefer_agent_sdk`; otherwise `_classify_agent`. Flag unset → every job (EasyApply and OffsiteApply) goes to the Agent SDK.
- `classify_with_circuit_breaker` short-circuits to `agent.classify(...)` at the top when NIM is disabled — the NIM-timeout breaker state is never touched, `nim_client.resolve_classifier` is never called. The breaker logic is left fully intact for the opt-in NIM case.
- `nim_client.py` and `_classify_nim` are untouched — the opt-in path still works if `CLASSIFIER_ROUTE=nim` is set.
- `.env.template`: classifier vars moved to an "OPTIONAL / opt-in" section; default needs no config.

**Tests:** `tests/test_classifier_routing.py` — default (no flag) routes OffsiteApply to `_classify_agent`; with `_NIM_CLASSIFIER_ENABLED=True`, OffsiteApply → `_classify_nim`, others → `_classify_agent`, `prefer_agent_sdk=True` → agent; `classify_with_circuit_breaker` with NIM disabled never calls `resolve_classifier`. `tests/test_config.py` — `get_classifier_route` default / `nim` / bad-value-warns. Existing NIM-route + circuit-breaker tests updated (autouse fixture enables the flag for that module). Full suite green + `ruff check .` diff-clean.

**Acceptance:** with no `CLASSIFIER_ROUTE` set, an OffsiteApply job with a 6–8K-char description classifies via the Agent SDK and the session does not abort; `CLASSIFIER_ROUTE=nim` restores the previous NIM-for-OffsiteApply behaviour incl. the circuit breaker; `pytest tests/` green.

---

## T39 — OffsiteApply step-loop stalls when a Greenhouse board URL redirects to a company careers SPA

**Phase:** T16b follow-up · **Risk:** low · **Status:** ✅ fixed (PR pending) — implemented options **1 + 3** · **Sev:** P3 · Found by the T22/T38 live QA run (`apply_jobs.py --auto --limit 12`, 2026-09-06), job 11.

**Fix (`linkedin_apply.py`):**
- **Option 1 — canonical Greenhouse embed retry.** New module helpers `_host_is_greenhouse()` / `_parse_greenhouse_job()` (`_GREENHOUSE_HOSTS` const). Early in `_llm_guided_apply`, if the ORIGINAL `application_url` host is a `*.greenhouse.io` board host but the page landed cross-host on a **non-greenhouse** host, retry once against `https://job-boards.greenhouse.io/<slug>/jobs/<id>` (the bare form, no company SPA wrapper). Slug + id are parsed from the `boards.greenhouse.io/<slug>/jobs/<id>`, `job-boards[.eu].greenhouse.io/...`, and `embed/job_app?token=…&for=…` shapes. If the canonical URL also redirects off greenhouse, it falls through to the step loop (→ option 3).
- **Option 3 — correct terminal state.** The "URL unchanged for 3 consecutive steps" stuck guard now returns `"blocked"` (`applied=-3`, excluded from `--reset-failed`) instead of `"failed"` (`-2`) **only** when the loop never engaged the form (`_form_engaged` flag: set solely by a `fill`/`select`/`upload`/`click` action executing — **not** by a page snapshot exposing `fields`, since `_get_page_snapshot` returns every visible input, so a careers-SPA nav search box / footer "job alerts" signup would falsely trip it) **and** the stuck host isn't a known ATS form host (`_FORM_DOMAINS` / `_ATS_REQUIRE_APPLY_PATH`). A stall *after* a non-scroll action still returns `"failed"` (retryable).
- Option 2 (un-stick the scroll loop by clicking bare "Apply" links) was **not** taken — riskier (wrong-CTA picks) and options 1+3 already cover the reported case and the churn.
- `_parse_greenhouse_job` only trusts *recognized* URL shapes (`/<slug>/jobs/<id>` path or `?for=<slug>`) — it never guesses a bare first path segment is a board slug, so a non-standard greenhouse URL falls through to the loop rather than emitting a junk canonical URL.

**Reviewer follow-ups (PR #47):** dropped the over-broad snapshot signal for `_form_engaged` (BLOCKING — a lone SPA nav input made `fields` truthy and defeated the option-3 discrimination on the "canonical retry also redirected" fall-through path); `_host_is_greenhouse` now derives from `_GREENHOUSE_HOSTS` (was dead code); tightened the slug heuristic (above). **Non-blocking decision:** Workday / iCIMS / Taleo are already in `_BLOCKED_AUTO_APPLY_DOMAINS`, so a scroll-only stall there is caught by the pre-flight / mid-loop `_classify_domain` → `"blocked"` **before** reaching the option-3 guard — no separate `_on_ats_host` entry needed for them. Other ATS form hosts (`greenhouse.io`, `ashbyhq.com`, `lever.co`, `rippling.com`, …) are in the `_on_ats_host` allowlist and keep `"failed"` (retryable) on a scroll-only stall.

**Tests:** `tests/test_greenhouse_redirect.py` (new, 11 cases) — `_parse_greenhouse_job` URL-shape matrix + non-greenhouse/incomplete rejection; flow-level: cross-host redirect from a greenhouse board URL triggers the exact canonical retry URL; embed-shape parsing; greenhouse URL that doesn't redirect cross-host → no retry; non-greenhouse redirect → no greenhouse retry; scroll-only dead end on a company SPA host → `blocked`; **scroll-only dead end with a lone nav search / job-alerts input in the snapshot → still `blocked`** (regression for the reviewer's blocking point); scroll-only stall on an ATS form host → stays `failed`; click-then-stall → stays `failed`. `pytest tests/` 330 passed; `ruff check` diff-clean.

**Symptom:** MongoDB "Senior Software Engineer, SQL Engines". `application_url` is `http://boards.greenhouse.io/mongodb/jobs/8161512?gh_src=…`. `_llm_guided_apply` navigates there; the host 30x-redirects to `https://www.mongodb.com/careers/jobs/8161512`, MongoDB's own JS careers page, where the apply CTA is not reachable by scrolling (it opens an embedded Greenhouse form on click). The step-loop issued `scroll` on steps 1/2/3, the URL never changed, and the "URL unchanged for 3 consecutive steps — browser is stuck, giving up" guard fired → `[!] Auto-apply failed` → `applied=-2`. ~40s + 3 `claude-sonnet-5` browser_action calls burned, no application.

**Not a regression.** None of T22/T31/T32/T37/T38 touch OffsiteApply navigation or the step-loop. The stuck-detection guard firing and marking `-2` is correct terminal behaviour given the loop genuinely could not progress. This is a pre-existing capability gap, distinct from **T36** (T36 = a Playwright tab *crash* mid-fill; this is a clean navigation dead-end with no exception).

**Why it's worth a ticket (not just "this ATS is hard"):**
- `-2` is in the `--reset-failed` retry pool, so this job re-burns the same ~40s + 3 LLM calls on every future `--auto` run and can never succeed as-is.
- The redirect pattern (an ATS "boards" URL bouncing to a company-branded careers SPA that hides the apply CTA behind a click) is not MongoDB-specific — several large employers configure Greenhouse this way.

**Suggested fix / next steps (pick one or more):**
1. **Canonical embed retry:** when the landing host differs from the `application_url` host and the original was `*.greenhouse.io`, retry once against the iframe-embed host `https://job-boards.greenhouse.io/<slug>/jobs/<id>` (renders the bare form directly) before entering the step-loop.
2. **Un-stick the scroll loop:** if two consecutive `scroll` actions produce no URL change and no new form fields / no Apply control, let the loop try clicking a visible `Apply` link even though the generic prompt currently discourages bare "Apply" nav links.
3. **Cheaper give-up + correct terminal state:** detect "no form, no reachable apply control after 2 scrolls" and mark it `-3` (needs a human) instead of `-2`, so it leaves the auto-retry pool. Lowest-effort mitigation; forfeits the apply but stops the churn.

**Affected users / impact:** one job per run stuck in a no-op retry loop; a class of Greenhouse-backed employers with custom careers SPAs is currently un-appliable via OffsiteApply.

---

## T41 — `_get_profile_value` location branch substring-matches `"city"` inside `"capacity"`

**Phase:** T40 follow-up · **Risk:** low · **Status:** open · **Sev:** P3 · Found while implementing **T40** (PR #41).

**Symptom:** `_get_profile_value`'s city/location branch is a tuple-membership substring test — `any(k in l for k in ("city", "location", "where are you", ...))`. `"city"` is a substring of `"capacity"`, so a label like `"How many years of experience do you have in a professional capacity?"` or `"years of experience in a leadership capacity"` matches the location branch (which runs well before the years-of-experience logic) and returns `profile["location"]` (or `None` when the profile has no location). The applicant never gets a years figure for these phrasings.

**Pre-existing — not a T40 regression.** Reproduces identically on `master`. T40 only touches the `"years of experience"` catch-alls, all of which sit *after* this branch, so T40 neither caused nor can fix it. T40's tests deliberately use non-colliding generic phrasings ("...in a leadership role", "...in a senior position") to stay clear of it.

**Why it's not a safe one-liner:** the fix is to convert that tuple-membership check to word-boundary matching (`re.search(r'\bcity\b', l)` etc.), but the same branch also carries `"location"`, `"where are you"`, `"your location"`, `"current location"`, `"city, state"`, `"city/state"` and feeds several downstream address branches. Tightening one key risks changing which branch a range of location/address labels resolve to. Needs the full `tests/test_profile_value.py` sweep (plus a few new location-label cases) to land safely, not a drive-by edit inside the T40 PR.

**Suggested fix:** replace the substring membership with anchored/word-boundary patterns for the short keys (`city`, `state`, `zip`, …) while leaving the multi-word phrases as substring checks; add characterization tests for `"...in a professional capacity"`, `"...capacity planning experience"`, and the existing location labels before/after.

---

## T42 — T40's "foreign skill?" check ignores 2-char skill tokens (AI, ML, Go)

**Phase:** T40 follow-up · **Risk:** low · **Status:** open · **Sev:** P3 · Found verifying **T40** (PR #41, merged `01354fd`) against the live `user_profile.json`.

**Symptom:** `_years_label_names_a_foreign_role_or_skill` (`linkedin_apply.py` ~line 494) decides whether a "years of experience with/in/as `<x>`" label names something the applicant has actually done, by checking whether the qualifier's words appear in `current_title` / `headline` / `summary` / `skills`. That check is guarded by `len(w) >= 3`, so a 2-character qualifier token is skipped entirely and the label is always classified "foreign" → routed to the T32 tiered branch → floored to `"1"`.

Affected tokens: `ai`, `ml`, `go`, `ui`, `ux`, `qa`, `bi`, `r`, `c` (and any ≤2-char acronym). Live example: `"How many years of experience do you have in AI / ML?"` → `"1"` for an applicant whose `skills` list has `Vector Databases` / `RAG Architectures` / `LLM Fine-tuning`, whose `summary` says "4+ years of experience in full-stack systems and **AI applications**" and "Master's in Data Science, focusing on advanced **AI applications**", and who is actively targeting AI/ML roles. (Pre-T40 this returned `"4"` via the loose catch-all.)

**Severity is low:**
- Underclaim, not overclaim — the agreed-safe direction (an overclaim risks a "minimum N years" knockout; `"1"` doesn't, and it's never `"0"`).
- Narrow — a skill that IS literally in the `skills` list as a ≥3-char token (`Python`, `Kubernetes`, `AWS`, `Go` — "Go" is caught by an earlier exact skill-name branch, not this detector) resolves to the full/capped figure correctly. Only short acronyms *not* spelled out in `skills` misfire.
- But real for AI/ML-heavy applications, which is a category this applicant targets.

**Suggested fix:** in the background-match loop, lower the length guard to `len(w) >= 2` for the `bg` word-boundary check specifically (it's an anchored regex against real profile text — `"ai"` matching "AI applications" is correct; most 2-char English filler — `us`, `it` — is already in `_GENERIC_QUALIFIER_WORDS` and filtered earlier). Optionally also match the *whole normalized span* (`"ai/ml"`, `"ai ml"`) against `skills` for multi-token acronym pairs. Add tests: `"years of experience in AI/ML"` / `"...in ML"` / `"...with Go"` → full/capped figure for a profile that has those; `"...with COBOL"` → still `"1"`.
