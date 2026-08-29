# Architecture Review & Optimization Plan

_Generated 2026-08-28. Sections 1–2 are the as-is review; sections 3+ are the decided plan (design settled via a grilling pass on 2026-08-28). No PII or credentials were sent to any external service during this review._

---

## 1. Current structure

### 1.1 Repo map

| Area | Files | LOC | Role |
|---|---|---|---|
| **Discovery scraper** | `search_retriever.py`, `scripts/fetch.py:JobSearchRetriever` | ~230 | Voyager API → new `job_id`s, `scraped=0` |
| **Enrichment scraper** | `details_retriever.py`, `scripts/fetch.py:JobDetailRetriever` | ~210 | Full attributes for `scraped=0` → `scraped=1` |
| **Orchestration** | `scripts/dagster_*.py`, `scripts/definitions.py` | ~1,100 | Dagster ops/jobs/schedules/sensor + SDA lineage assets |
| **Apply agent — driver** | `apply_jobs.py` | 1,367 | CLI, env/profile, DB queries, `JobAgent` classifier, `run_session` loop, email |
| **Apply agent — browser flows** | `linkedin_apply.py` | **4,607** | `EasyApplyFlow`, `OffsiteApplyFlow`, field matching, account creation |
| **Apply agent — script generator** | `script_engine.py` | 547 | `ScriptApplyEngine`: LLM writes a full Playwright script per page |
| **Haiku helper CLI** | `apply_haiku.py` | 153 | list/mark/stats/log helpers for the `--haiku` skill |
| **DB bootstrap / helpers** | `scripts/create_db.py`, `database_scripts.py`, `helpers.py` | ~310 | schema DDL, upserts, cleaning |
| **Export / analysis** | `to_csv.py`, `analysis/` (gitignored, ~166 MB) | ~80 | CSV export + notebooks |

Total tracked Python: ~8,300 LOC across 18 files, but **72 % of it is in two files** (`linkedin_apply.py` + `apply_jobs.py`).

### 1.2 Data flow

```
                 ┌─────────────────── Dagster (scripts/) ───────────────────┐
                 │  search_schedule (0 */12)   details_schedule (10 */12)   │
                 │  unscraped_jobs_sensor ──────────────┐                   │
                 │  daily_apply_schedule (0 10 * * *)   │                   │
                 └──────────┬──────────────────┬────────┴───────────────────┘
                            │                  │
              ┌─────────────▼───────┐  ┌───────▼─────────────┐
   LinkedIn   │ JobSearchRetriever  │  │ JobDetailRetriever  │
   Voyager ◄──┤ requests.Session    │  │ requests.Session    │
   API        │ cookies via Selenium│  │ (multi-account)     │
              └─────────┬───────────┘  └───────┬─────────────┘
                        │ insert scraped=0     │ update scraped=1
                        ▼                      ▼
              ┌───────────────────────── linkedin_jobs.db (SQLite) ─────────┐
              │  jobs (1,263 rows) + 10 child tables, NO indexes            │
              └───────────────────────────────┬────────────────────────────┘
                                              │ applied IS NULL AND remote/UT
                                              ▼
        ┌──────────────────── apply_jobs.py run_session ────────────────────┐
        │  for each job:                                                     │
        │   1. title heuristics (staff/principal skip)                       │
        │   2. JobAgent.classify()  ──►  subprocess: `claude --model haiku`  │
        │   3. Playwright chromium (headless=False)                          │
        │        ├─ EasyApplyFlow  (Simple/ComplexOnsiteApply)               │
        │        └─ OffsiteApplyFlow (OffsiteApply)                          │
        │             ├─ ScriptApplyEngine ──► subprocess: `claude ...`      │
        │             └─ _llm_guided_apply (1,498-line loop) ──► `claude`    │
        │   4. mark applied = 1 / -1 / -2                                    │
        │  end: write application_log.json, email via Gmail SMTP             │
        └───────────────────────────────────────────────────────────────────┘
```

### 1.3 LLM usage (as actually wired today)

Despite `.env.template`, `README.md`, and `CLAUDE.md` describing "any OpenAI-compatible endpoint" and `run_session` constructing `OpenAI(...)` / `AsyncOpenAI(...)` clients and threading them through every flow, **every real LLM call shells out to the `claude` CLI as a subprocess**:

- `apply_jobs.py:JobAgent.classify` → `subprocess.run(["claude", "--model", "claude-haiku-4-5", "-p", ...])`
- `linkedin_apply.py:_call_claude` (used by `_ask_llm_action`, `_ask_llm`, EEO option pickers) → same
- `script_engine.py:_call_claude` / `_call_llm` → same

This is a **deliberate choice** (commit `2026-06-29 "Use claude CLI subprocess for job relevance classification"`) — it rides the Claude Code subscription instead of paying per-token. The `AsyncOpenAI` objects are threaded ~10 levels deep and are now dead code. `CLASSIFIER_LLM_*` / `BROWSER_LLM_*` env split, the circuit-breaker fallback model, and `httpx.Timeout` tuning are all no-ops in the current path.

The cost this pays: every call spawns a fresh `node` + Claude Code harness (~1–3 s cold start, MCP handshakes), no connection reuse, no context reuse, no prompt caching, and it competes with interactive Claude Code usage (`_call_claude` already string-matches `"usage limit"` / `"rate limit"` in stdout).

**Prompt volume** (from `llm_debug.jsonl`, ~7,800 entries / 54 MB):

| Call type | Count | Avg prompt | Max prompt |
|---|---|---|---|
| `browser_action` (`_llm_guided_apply`) | 3,952 | ~10.7 KB (~2.7K tok) | ~48 KB (~12K tok) |
| `classifier` | 2,281 | ~3.8 KB | ~4.6 KB |
| `script_gen` | 624 | ~2.6 KB | ~8 KB |

### 1.4 Database

`linkedin_jobs.db` — 8.9 MB, 11 tables, `jobs` = 1,263 rows (156 applied, 1,024 skipped, 78 pending, 5 failed).

- `jobs` PK is `job_id`; **no secondary indexes anywhere** — `sqlite_master` shows only auto-indexes on child-table PKs.
- Hot query `get_pending_jobs` filters `scraped > 0 AND applied IS NULL AND (remote_allowed=1 OR location LIKE ...)`, `LEFT JOIN companies`, `ORDER BY original_listed_time` → full scan + filesort every run.
- `original_listed_time` / `listed_time` stored as **TEXT**; ordered with `COALESCE(..., 0)`.
- `DatabaseStructure.md` documents ~15 `jobs` columns (`salaries`, `benefits`, `med_salary`, `skills_desc`, …) that are not in the live `CREATE TABLE` — schema drift.
- Blocked-company / blocked-ATS lists are Python constants interpolated into SQL `LIKE` fragments (`f"...NOT LIKE '%{co}%'"`) — brittle and unindexable.

### 1.5 Reliability today

`application_log.json`: **84 sessions → 30 applied, 346 skipped, 58 errored.** ~66 % of non-skipped attempts fail. 30 real applications in ~2 months from a 1,263-job pool. This is the number the plan targets.

### 1.6 Repo hygiene

- `pyproject.toml` dependency list is missing `openai` and `playwright` (only in `requirements.txt`); two out-of-sync sources of truth.
- `jobs.db` (empty, 0 bytes) tracked alongside the real `linkedin_jobs.db`.
- `DAGSTER_COMPLETE_GUIDE.md` is 581 lines of mostly generic Dagster tutorial content.
- `debug_screenshots/` has 614 files; `llm_debug.jsonl` grows unbounded to 54 MB (both gitignored, neither rotated).
- `analysis/` is 166 MB of HTML/ipynb sitting in the working tree.
- Duplicated helpers copy-pasted across 3 files: `_write_llm_log`, `_call_claude`, JSON-fence stripping.

---

## 2. Problems, ranked

| # | Problem | Evidence | Impact |
|---|---|---|---|
| **P1** | Apply agent fails ~2/3 of attempts, mostly OffsiteApply | §1.5 | The core problem |
| **P2** | `_llm_guided_apply` is a **1,498-line** function; `run_session` 474; `_get_profile_value` 315 (0 tests) | `ast` scan | Can't change the apply agent safely |
| **P3** | LLM calls spawn a fresh `claude` subprocess per call — cold start, no context/prompt reuse, competes with interactive usage | §1.3 | Latency + throttling |
| **P4** | Two LLM abstractions coexist; the OpenAI-client path is dead code threaded ~10 levels deep | §1.3 | Confusion, silent misconfig |
| **P5** | Three separate LLM-driven form-fillers (`_llm_guided_apply`, `ScriptApplyEngine`, `apply_haiku` path) | §1.1 | Maintenance sprawl |
| **P6** | No DB indexes; TEXT timestamps; blocklists built by f-string SQL interpolation | §1.4 | Slow-ish, injection-shaped, unindexable filters |
| **P7** | Config/dep/doc drift: `pyproject` vs `requirements`, `DatabaseStructure.md` vs live schema, docs vs code on LLM backend | §1.4, §1.6 | Onboarding friction |
| **P8** | Unbounded logs/screenshots; 166 MB `analysis/` in tree | §1.6 | Disk, slow tooling |

---

## 3. Decided plan

### 3.0 Constraints (fixed)

- **Billing:** ride the Claude Code subscription. **Zero per-token Anthropic API spend.** Plan is Pro → be call-frugal, keep daily job volume low.
- **Second LLM provider:** the free NVIDIA NIM catalog (`build.nvidia.com`, OpenAI-compatible, **~40 RPM shared across the whole key**). Used only where subscription billing shouldn't be.
- **`EasyApplyFlow` is not touched** — LinkedIn's Easy Apply modal is stable and works. Only shared helpers it depends on change, and those are covered by tests first.
- Every phase ships through the CLAUDE.md pipeline: feature branch → `senior-swe` → `pr-code-reviewer` → QA. SWE and reviewer are separate agents.

### 3.1 LLM execution model

**Replace** all `subprocess.run(["claude", …])` per-call shells (`JobAgent.classify`, `linkedin_apply._call_claude`, `script_engine._call_claude`) **with the Claude Agent SDK** (`claude-agent-sdk` Python package):

- Subscription auth (log in with the Claude account — **not** an API key; that keeps it in scope for the subscription's Agent SDK credit and pool).
- In-process, async, one persistent session per apply run — no per-call `node`/MCP cold start, context reused across calls.
- Typed errors + built-in backoff replace the current stdout string-matching for `"usage limit"`.

**Delete** the dead `AsyncOpenAI` / `openai` plumbing: the `llm_client` / `classifier_client` params threaded through `EasyApplyFlow.__init__` / `OffsiteApplyFlow.__init__` and `run_session`. Each engine constructs its own LLM client at its own boundary.

**Model configuration** — one `config.py` reading env vars, zero hardcoded model strings anywhere:

| Env var | Role | Default |
|---|---|---|
| `CLASSIFIER_MODEL` + `CLASSIFIER_API` + `CLASSIFIER_BASE_URL` | job relevance (NIM path) | small NIM model, e.g. `meta/llama-3.1-8b-instruct` |
| `BROWSER_USE_MODEL` + `BROWSER_USE_API` + `BROWSER_USE_BASE_URL` | browser-use agent | `deepseek-ai/deepseek-v4-flash-0731` (NIM) |
| `GUIDED_APPLY_MODEL` | decomposed `_llm_guided_apply` fallback (Agent SDK) | `claude-sonnet-5` |

Swapping any model is a one-line `.env` edit.

### 3.2 Classifier

Per-job routing on `application_type` (already a DB column, known before classification):

- `OffsiteApply` → **NIM** small model (free — matches the free apply path)
- `SimpleOnsiteApply` / `ComplexOnsiteApply` → **Claude Agent SDK** (subscription — matches the subscription apply path)

Keyword fast-path (citizenship / clearance keywords) stays in front of both. Keep the two-dimension prompt (relevance + citizenship) and move to structured output so the regex / JSON-fence salvage code can go.

Current pending queue is ~75 % OffsiteApply, so most classification runs free.

### 3.3 Apply agent — OffsiteApply

**Primary engine: `browser-use`**, driven by NIM.

- Model: `deepseek-ai/deepseek-v4-flash-0731` (on `build.nvidia.com`, OpenAI-compatible, built for agentic tool-use). Documented swap target if it underperforms: `qwen/qwen3-coder-480b-a35b-instruct`.
- Wired via `ChatOpenAI(base_url=<NIM>, model=<BROWSER_USE_MODEL>)`; expect to need `add_schema_to_system_prompt` / `remove_min_items_from_schema` for a non-OpenAI model.
- DOM-extraction mode, **not** vision (vision blows the 40 RPM budget).
- Jobs processed serially; accept 40 RPM throttling for now, apply for the 200 RPM upgrade.
- browser-use manages its own page representation.

**Fallback engine: decomposed `_llm_guided_apply`** on the Claude Agent SDK (`GUIDED_APPLY_MODEL`, default `claude-sonnet-5` — a job that already failed the free model is exactly when capability beats cost; the path is low-frequency).

**Fallback fires when any of:**
1. browser-use raises, times out, or hits its max-steps limit without a verified submission
2. NIM returns 429 / rate-limit after N retries with backoff
3. browser-use reports success but `verify_submission(page, job)` disagrees

**`ScriptApplyEngine`** (547 LOC) is retired once the browser-use spike passes. **`apply_haiku.py`** + the Claude-in-Chrome apply path is deleted in Phase 1.

**Decomposition seams** for `_llm_guided_apply` (1,498 lines):
`page_snapshot` · `decide_action` (the LLM call) · `execute_action` · `detect_terminal_state` (applied / dead-end / needs-human) · `handle_auth` (login + account creation + email verification).

**Decomposition depth is conditional on the spike outcome:**

- **Spike passes** → *light* extraction: pull the 5 seams just far enough to unit-test the fallback handoff; leave the internals alone. It's a rare fallback — size matters in proportion to how often it's touched.
- **Spike fails** → *full* teardown: browser-use is shelved, the decomposed Claude path becomes the OffsiteApply primary, and it's worth gold-plating.

The decomposed path sends an **accessibility-tree snapshot / structured field list**, not raw DOM/HTML (~4× fewer tokens per current industry benchmarks).

### 3.4 Apply agent — EasyApply

Untouched. Protected during shared-helper refactors by:

- a new **pytest suite** for `_get_profile_value` / field-matching (315 lines of pure logic, currently zero tests), written *before* the `common.py` extraction
- a **baseline QA run** captured before Phase 1a merges: `apply_jobs.py --type SimpleOnsiteApply,ComplexOnsiteApply --limit 3` + the corresponding `llm_debug.jsonl` slice

### 3.5 Verification

A single unified `verify_submission(page, job) -> bool` replaces the separate `_check_submission_result` implementations in `EasyApplyFlow` and `OffsiteApplyFlow`. Built in **Phase 3** — it is a hard dependency of both the spike's success metric and fallback trigger #3.

### 3.6 Database (Phase 1b)

```sql
CREATE INDEX idx_jobs_pending  ON jobs(applied, scraped);
CREATE INDEX idx_jobs_company  ON jobs(company_id);
CREATE INDEX idx_jobs_listed   ON jobs(original_listed_time DESC);
CREATE INDEX idx_jobs_apptype  ON jobs(application_type);
PRAGMA journal_mode=WAL;
```

- **Timestamps:** add `listed_epoch INTEGER`, backfill from the TEXT columns, switch `get_pending_jobs` ordering to it, drop the old columns in a later phase. Back up the DB before running.
- **Blocklists:** replace the f-string-interpolated Python constants with a `blocked_entities(kind TEXT, pattern TEXT, reason TEXT)` table, joined/filtered once. Removes the injection-shaped SQL building.
- **Doc:** regenerate `DatabaseStructure.md` from `PRAGMA table_info` (small script), or delete it and point at `scripts/create_db.py`.

### 3.7 Orchestration (Phase 1c)

Trim Dagster — delete `scripts/dagster_db_assets.py`, `scripts/dagster_relationships.py` (420 LOC of SDA lineage assets nothing consumes) and `DAGSTER_COMPLETE_GUIDE.md`. Keep the 3 ops + 3 schedules + `unscraped_jobs_sensor`. No Prefect migration — churn for a working system.

### 3.8 Scraping (Phase 5)

No new sources — discovery is not the bottleneck (30 applied vs 1,024 skipped).

- **Phase 1:** narrow the search keywords (`"software engineer AI ML"` is too broad — config change only).
- **Phase 5:** replace the standalone `while True: time.sleep()` scripts (`search_retriever.py`, `details_retriever.py`) with the Dagster ops that already exist + `tenacity` for retry/backoff; move cookie extraction from Selenium to Playwright (already a dep) with a persisted storage-state JSON refreshed only on 401.

### 3.9 Hygiene (Phase 1a)

- `pyproject.toml` becomes the single dependency source (`[project.dependencies]` = full list incl. `openai`→remove, `playwright`, `claude-agent-sdk`, `browser-use`); regenerate or delete `requirements.txt`.
- `git rm --cached jobs.db`.
- `llm_debug.jsonl` size-based rotation; prune `debug_screenshots/` on session start (keep last N).
- Move `analysis/` (166 MB) out of the repo tree — it slows every `graphify` / grep / IDE index.
- Extract shared helpers (`_write_llm_log`, JSON-fence strip, etc.) into `common.py`.
- Add `ruff` (config in `pyproject.toml`).

---

## 4. Phase plan

Each phase = one or more feature branches through `senior-swe` → `pr-code-reviewer` → QA.

| Phase | Content | Gate / notes |
|---|---|---|
| **Pre-work** | Baseline QA run (`--type SimpleOnsiteApply,ComplexOnsiteApply --limit 3`) + `llm_debug.jsonl` slice saved | Regression reference for shared-helper changes |
| **1a** | Deletions (`apply_haiku.py` + Claude-in-Chrome path, dead `AsyncOpenAI`/`llm_client` plumbing) · hygiene (pyproject single-source, `git rm --cached jobs.db`, log rotation, prune screenshots, `analysis/` out of tree, `common.py`) · `ruff` · narrow search keywords · pytest suite for `_get_profile_value` / field-matching | Low-risk, lands fast |
| **1b** | DB migration: indexes + WAL · TEXT→epoch timestamps · blocklist → `blocked_entities` table · regenerate `DatabaseStructure.md` | Only phase touching live data — isolated for QA. Back up DB first |
| **1c** | Trim Dagster: delete `dagster_db_assets.py`, `dagster_relationships.py`, `DAGSTER_COMPLETE_GUIDE.md` | Independent |
| **2** | Claude Agent SDK migration: replace all `_call_claude` shells with a persistent Agent SDK session · classifier per-job routing (NIM for OffsiteApply, Agent SDK for EasyApply) · structured-output classifier · finish deleting the OpenAI path · `config.py` model env vars | |
| **3** | **browser-use spike** — 10 currently-failing OffsiteApply jobs, `deepseek-v4-flash` on NIM, DOM mode. Build unified `verify_submission`. | **Go/no-go: ≥5/10 reach verified submission · median < 5 min/job · ≤1 hard failure caused by 40 RPM throttling.** Written on a real branch, keepable, time-boxed |
| **4 (go)** | browser-use = OffsiteApply primary · **light** decomposition of `_llm_guided_apply` (5 seams, test the handoff) as fallback · fallback triggers 1–3 · retire `ScriptApplyEngine` | |
| **4 (no-go)** | **full** decomposition of `_llm_guided_apply` as the OffsiteApply primary · browser-use shelved · retire `ScriptApplyEngine` | |
| **5** | Scraper cleanup: Dagster-owned loops + `tenacity` · Selenium → Playwright cookie extraction with persisted storage-state | |

Phases 1–2 are committed. Phase 3 is the gate that decides Phase 4's shape. Phase 5 is opportunistic.

---

## 5. Tools

| Tool | Use | Billing | Link |
|---|---|---|---|
| `claude-agent-sdk` (Python) | Replace `claude` CLI subprocess; persistent session; classifier (EasyApply) + `_llm_guided_apply` fallback | Subscription | [help](https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan) · [billing change](https://koromo.io/en/blog/claude-agent-sdk-credit-guide/) |
| `browser-use` | OffsiteApply primary engine | Free (NIM key) | https://github.com/browser-use/browser-use · [supported models](https://docs.browser-use.com/supported-models) |
| NVIDIA NIM (`build.nvidia.com`) | Free OpenAI-compatible endpoint for browser-use + OffsiteApply classifier | Free, ~40 RPM | [deepseek-v4-flash](https://build.nvidia.com/deepseek-ai/deepseek-v4-flash-0731) · [rate limit](https://forums.developer.nvidia.com/t/request-nvidia-nim-free-tier-rate-limit-increase-40-rpm-severely-limits-agentic-ai-workflows/369762) |
| `deepseek-ai/deepseek-v4-flash-0731` | browser-use model (spike) | — | [NVIDIA blog](https://developer.nvidia.com/blog/build-with-deepseek-v4-using-nvidia-blackwell-and-gpu-accelerated-endpoints/) |
| Playwright accessibility-tree snapshots | Page representation for the decomposed Claude path (~4× fewer tokens) | — | [Playwright CLI benchmark](https://testcollab.com/blog/playwright-cli) · [token benchmark 2026](https://www.ytyng.com/en/blog/ai-browser-automation-tools-comparison-2026) |
| `tenacity` | Retry/backoff for scraper loops | — | — |
| `ruff` | Linter (none configured today) | — | — |

### Sources

- [Use the Claude Agent SDK with your Claude plan — Anthropic](https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan)
- [Claude Agent SDK credit / billing change June 2026 — koromo](https://koromo.io/en/blog/claude-agent-sdk-credit-guide/)
- [Claude Code stream-json output format — Background Claude](https://backgroundclaude.com/blog/stream-json)
- [browser-use — supported models](https://docs.browser-use.com/supported-models)
- [browser-use — local / alternative LLM support (DeepWiki)](https://deepwiki.com/browser-use/browser-use/8.6-local-and-alternative-llm-support)
- [Stagehand vs Browser Use vs Playwright (2026) — NxCode](https://www.nxcode.io/resources/news/stagehand-vs-browser-use-vs-playwright-ai-browser-automation-2026)
- [DeepSeek V4 Flash on NVIDIA NIM](https://build.nvidia.com/deepseek-ai/deepseek-v4-flash-0731)
- [Build with DeepSeek V4 on NVIDIA — NVIDIA blog](https://developer.nvidia.com/blog/build-with-deepseek-v4-using-nvidia-blackwell-and-gpu-accelerated-endpoints/)
- [NVIDIA NIM free-tier 40 RPM limit — NVIDIA Developer Forums](https://forums.developer.nvidia.com/t/request-nvidia-nim-free-tier-rate-limit-increase-40-rpm-severely-limits-agentic-ai-workflows/369762)
- [Playwright CLL: token-efficient alternative to MCP — TestCollab](https://testcollab.com/blog/playwright-cli)
- [AI browser automation token benchmark 2026 — ytyng.com](https://www.ytyng.com/en/blog/ai-browser-automation-tools-comparison-2026)
- [Best Open-Source LLMs 2026 (tool-calling) — Hugging Face](https://huggingface.co/blog/daya-shankar/open-source-llms)
