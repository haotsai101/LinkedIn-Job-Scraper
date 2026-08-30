# T14 (part 1) — Live QA checklist

T14 shipped in PR #15 (`f36f0a6`). Offline QA passed (112 tests, imports safe without the SDK, `--stats` works, no `subprocess.run(["claude"])` left in `apply_jobs.py`, `permission_mode="dontAsk"`, classifier plumbing removed). The classifier's live path is **unverified** — it needs a real `claude` login + `claude-agent-sdk` installed + a NIM key. Do not close T14, start T14b, or start T15 until this passes.

## Prerequisites (on the machine that runs the agent)

```bash
# 1. install the Agent SDK into the interpreter that runs apply_jobs.py
/opt/anaconda3/bin/pip install -e '.[dev]'        # picks up claude-agent-sdk>=0.2.140,<0.3
/opt/anaconda3/bin/python -c "import claude_agent_sdk; print(claude_agent_sdk.__version__)"

# 2. confirm the claude CLI is logged in (subscription auth — NOT an API key)
claude --version          # present: 2.1.251
#   the Agent SDK uses this login; no ANTHROPIC_API_KEY should be set

# 3. .env — currently has legacy names CLASSIFIER_LLM_API / CLASSIFIER_LLM_MODEL.
#    config.py reads them as fallback aliases (with a DeprecationWarning).
#    Rename to the canonical names to silence the warning:
#      CLASSIFIER_API=<nim key>
#      CLASSIFIER_MODEL=meta/llama-3.1-8b-instruct
#      CLASSIFIER_BASE_URL=https://integrate.api.nvidia.com/v1
```

## Checks

### 1. Isolation probe (the reason T14 was reworked)
Classify a clearly-relevant SWE role → a clearly-irrelevant role (e.g. "Registered Nurse") → the SWE role again, all EasyApply (so they hit the Agent SDK path). Confirm:
- the nurse result is `relevant: false` with a `reason` that does **not** reference the SWE job
- verdicts are the same regardless of order
- (if you can print it) `ResultMessage.session_id` differs between two consecutive calls → proves fresh sessions

Quick way: `python apply_jobs.py --auto --limit 5 --type ComplexOnsiteApply` and eyeball the `reason` strings in `llm_debug.jsonl` for cross-contamination.

### 2. Structured output actually populated
With `max_turns=1`, confirm `ResultMessage.structured_output` comes back as a dict (not always falling through to text parsing). Add a temporary debug print in `llm.query_json` if needed. **If it's empty, bump `max_turns` to 2 in `llm.py` (both call sites) — lockdown is unaffected.**

### 3. NIM accepts JSON mode
Confirm `integrate.api.nvidia.com` accepts `response_format={"type":"json_object"}` for `meta/llama-3.1-8b-instruct`. If it 400s or returns prose, the new `extract_json_object` salvage in `nim_client.py` covers it, but note which happened.

### 4. Route telemetry
`python apply_jobs.py --auto --limit 3` over a mix of OffsiteApply + EasyApply pending jobs → `llm_debug.jsonl` shows `"type":"classifier"` lines with both `"route":"nim"` and `"route":"agent_sdk"`, each carrying `duration_ms`.

### 5. Resilience — bad NIM key
Temporarily point `CLASSIFIER_API` (or `CLASSIFIER_LLM_API`) at a bad key → confirm the session **stops with a clear message** ("NIM classifier misconfigured … stopping session"), not silent per-run skipping of all OffsiteApply jobs.

### 6. Lazy import still holds
`apply_jobs.py --stats` works with `claude-agent-sdk` **uninstalled** (already verified offline — re-confirm after any local install/uninstall).

## After it passes
- Mark T14 closed in `docs/TICKETS.md`.
- Clear `application_log.json` per the standing convention.
- Proceed to **T14b** (browser-agent `_call_claude` → `ClaudeSession`) and then **T15** (browser-use spike).
