"""T14b — the offsite / EasyApply browser agent talks to Claude through the
one-shot ``llm.query`` helper (fresh isolated session per call), not a
persistent conversation.

No real ``claude`` subprocess, no browser: ``llm.query`` is replaced with a
recording stub and the flow methods are exercised directly.
"""

from __future__ import annotations

import asyncio

import pytest

import config
import linkedin_apply


class _QueryStub:
    """Records every ``llm.query`` call; returns a canned reply or raises."""

    def __init__(self, reply="ok", *, exc=None, delay=0.0):
        self.reply = reply
        self.exc = exc
        self.delay = delay
        self.calls: list[dict] = []

    async def __call__(self, prompt, *, model, system=None, timeout=None,
                       log_type="agent", log_calls=False):
        self.calls.append({"prompt": prompt, "model": model, "timeout": timeout})
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.exc is not None:
            raise self.exc
        return self.reply


GUIDED_MODEL = config.get_llm_config("guided_apply").model


@pytest.fixture(autouse=True)
def _no_log(monkeypatch):
    monkeypatch.setattr(linkedin_apply, "_write_llm_log", lambda *_a, **_k: None)


def _install(monkeypatch, stub):
    monkeypatch.setattr(linkedin_apply.llm, "query", stub)
    return stub


def _offsite(**kw):
    return linkedin_apply.OffsiteApplyFlow(
        page=None, context=None, profile={}, auto_mode=True,
        callbacks={}, generated_password="x", **kw,
    )


# ── model wiring ───────────────────────────────────────────────────────────

def test_flows_default_to_the_guided_apply_model():
    assert _offsite().model == GUIDED_MODEL == "claude-sonnet-5"
    assert linkedin_apply.EasyApplyFlow(
        page=None, profile={}, auto_mode=True, callbacks={}).model == GUIDED_MODEL


def test_constructing_a_flow_makes_no_llm_call(monkeypatch):
    stub = _install(monkeypatch, _QueryStub())
    _offsite()
    linkedin_apply.EasyApplyFlow(page=None, profile={}, auto_mode=True, callbacks={})
    assert stub.calls == []


# ── _summarize_job ─────────────────────────────────────────────────────────

def test_summarize_job_uses_one_shot_query(monkeypatch):
    stub = _install(monkeypatch, _QueryStub("A crisp three-sentence summary."))
    flow = _offsite(company_name="ACME", job_title="Data Eng",
                    job_description="Build pipelines. " * 20)
    out = asyncio.run(flow._summarize_job())
    assert out == "A crisp three-sentence summary."
    assert len(stub.calls) == 1
    assert stub.calls[0]["model"] == GUIDED_MODEL
    assert stub.calls[0]["timeout"] == 30


def test_summarize_job_falls_back_on_error(monkeypatch):
    _install(monkeypatch, _QueryStub(exc=linkedin_apply.llm.ClaudeAgentSDKError("boom")))
    flow = _offsite(company_name="ACME", job_title="Data Eng",
                    job_description="stuff")
    assert asyncio.run(flow._summarize_job()) == "Role: Data Eng at ACME."


# ── _ask_llm (field fill) ──────────────────────────────────────────────────

def test_ask_llm_field_fill_goes_through_query(monkeypatch):
    stub = _install(monkeypatch, _QueryStub("7"))
    out = asyncio.run(linkedin_apply._ask_llm(
        GUIDED_MODEL, {"years_experience": 7},
        {"label": "Years of experience", "kind": "number"},
    ))
    assert out == "7"
    assert stub.calls[0]["model"] == GUIDED_MODEL


def test_ask_llm_long_labelled_scale_field_yields_bare_integer(monkeypatch):
    # T37: a 90+ char "Rate ... (1-10) ..." label must NOT take the long-form
    # prose path — even when the model answers with a sentence, _ask_llm returns
    # the range-clamped bare integer.
    stub = _install(monkeypatch, _QueryStub("I would rate my experience a 7 out of 10..."))
    label = ("Rate your experience (1-10) designing and building production "
             "data pipelines using SQL and Python.")
    out = asyncio.run(linkedin_apply._ask_llm(
        GUIDED_MODEL, {"years_experience": 8}, {"label": label, "kind": "text"},
    ))
    assert out == "7"
    # The non-long-form prompt was used (no "2-4 sentence" instruction).
    assert "2-4 sentence" not in stub.calls[0]["prompt"]


def test_ask_llm_genuine_free_text_still_gets_prose(monkeypatch):
    # T37 guard: a long free-text label with no numeric wording keeps the prose path.
    prose = "I have spent a decade building distributed data platforms end to end."
    stub = _install(monkeypatch, _QueryStub(prose))
    out = asyncio.run(linkedin_apply._ask_llm(
        GUIDED_MODEL, {},
        {"label": "Describe your experience building and operating data pipelines at scale",
         "kind": "text"},
    ))
    assert out == prose
    assert "2-4 sentence" in stub.calls[0]["prompt"]


def test_ask_llm_star_question_with_a_hint_token_gets_prose(monkeypatch):
    # T37 review: a long STAR question that *contains* "how many" / "rate" but
    # also a free-text cue ("describe", "give an example") must take the prose
    # path and NOT be digit-coerced — the inverse of the T37 bug.
    prose = ("There were several occasions, most memorably in 2021 when I pushed "
             "back on a rushed migration.")
    stub = _install(monkeypatch, _QueryStub(prose))
    out = asyncio.run(linkedin_apply._ask_llm(
        GUIDED_MODEL, {"years_experience": 8},
        {"label": "How many times have you had to advocate for an unpopular "
                  "decision? Give an example.", "kind": "text"},
    ))
    assert out == prose
    assert "2-4 sentence" in stub.calls[0]["prompt"]


# ── _ask_llm_action decide-action loop ─────────────────────────────────────

_SNAP = {"visible_text": "Apply now", "fields": [], "url": "https://ex.com/apply"}


def test_ask_llm_action_success_returns_parsed_dict(monkeypatch):
    _install(monkeypatch, _QueryStub('{"action": "done", "reason": "confirmation visible"}'))
    flow = _offsite(company_name="ACME", job_title="Dev")
    out = asyncio.run(flow._ask_llm_action(_SNAP, 0))
    assert out == {"action": "done", "reason": "confirmation visible"}


def test_ask_llm_action_three_timeouts_then_failed(monkeypatch):
    """The reviewer's requested guard: 3 one-shot attempts all time out ->
    a clean {"action": "failed"} instead of an infinite hang."""
    stub = _install(monkeypatch, _QueryStub(exc=asyncio.TimeoutError()))
    flow = _offsite(company_name="ACME", job_title="Dev")
    out = asyncio.run(flow._ask_llm_action(_SNAP, 0))
    assert out["action"] == "failed"
    assert "timed out after 3" in out["reason"]
    assert len(stub.calls) == 3
    assert {c["timeout"] for c in stub.calls} == {120}


def test_ask_llm_action_non_retryable_sdk_error_fails_fast(monkeypatch):
    stub = _install(monkeypatch, _QueryStub(
        exc=linkedin_apply.llm.ClaudeAgentSDKError("malformed request")))
    flow = _offsite(company_name="ACME", job_title="Dev")
    out = asyncio.run(flow._ask_llm_action(_SNAP, 0))
    assert out["action"] == "failed"
    assert "ClaudeAgentSDKError" in out["reason"]
    assert len(stub.calls) == 1  # not retried


def test_ask_llm_action_rate_limit_is_retried(monkeypatch):
    """A ClaudeAgentSDKError whose message names a usage/rate limit is retried
    (was stdout string-matching in the old subprocess helper)."""
    _real_sleep = asyncio.sleep
    monkeypatch.setattr(linkedin_apply.asyncio, "sleep",
                        lambda *_a, **_k: _real_sleep(0))
    calls = {"n": 0}

    async def _flaky(prompt, *, model, system=None, timeout=None,
                     log_type="agent", log_calls=False):
        calls["n"] += 1
        if calls["n"] == 1:
            raise linkedin_apply.llm.ClaudeAgentSDKError("usage limit reached")
        return '{"action": "scroll", "reason": "reveal more"}'

    monkeypatch.setattr(linkedin_apply.llm, "query", _flaky)
    flow = _offsite(company_name="ACME", job_title="Dev")
    out = asyncio.run(flow._ask_llm_action(_SNAP, 0))
    assert out == {"action": "scroll", "reason": "reveal more"}
    assert calls["n"] == 2


# ── T33: blocked-domain jobs end as "blocked", not "failed" ────────────────

class _UrlOnlyPage:
    def __init__(self, url):
        self.url = url


def test_blocked_ats_landing_domain_returns_blocked(monkeypatch):
    """A landing on an un-automatable ATS (UltiPro etc.) short-circuits to
    'blocked' before any LLM/browser work — run_session maps that to applied=-3
    so --reset-failed never brings it back.

    T51: this used a myworkdayjobs.com URL before Workday was removed from
    _BLOCKED_AUTO_APPLY_DOMAINS — swapped to another still-blocked ATS
    (UltiPro); see test_workday_flow.py for Workday-specific coverage."""
    _install(monkeypatch, _QueryStub())
    flow = _offsite(company_name="ACME", job_title="Dev")
    out = asyncio.run(flow._llm_guided_apply(_UrlOnlyPage("https://acme.ultipro.com/en-US/careers/job/123")))
    assert out == "blocked"


def test_spam_aggregator_landing_domain_still_returns_skipped(monkeypatch):
    _install(monkeypatch, _QueryStub())
    flow = _offsite(company_name="ACME", job_title="Dev")
    out = asyncio.run(flow._llm_guided_apply(_UrlOnlyPage("https://jobright.ai/jobs/123")))
    assert out == "skipped"


# ── no persistent-session / subprocess plumbing left ──────────────────────

def test_no_claude_subprocess_or_session_in_browser_agent_modules():
    for mod in (linkedin_apply,):
        src = open(mod.__file__).read()
        assert 'subprocess.run(["claude"' not in src
        assert "import subprocess" not in src
        assert "ClaudeSession" not in src        # one-shot llm.query only
        assert "_ClaudeAgentMixin" not in src


def test_no_asyncopenai_in_browser_agent_modules():
    for mod in (linkedin_apply,):
        src = open(mod.__file__).read()
        assert "AsyncOpenAI" not in src
        assert "from openai" not in src
