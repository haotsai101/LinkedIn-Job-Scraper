"""Unit tests for offsite_agentic.py (T54) — the opt-in agentic OffsiteApply
engine.

Follows the fake-Page/fake-Locator double pattern from tests/test_offsite_seams.py
(no browser, no network, no LLM subprocess). ``_execute_action`` itself is
mocked in most tests (it's the huge, already-tested-elsewhere Playwright
dispatch inherited unchanged from OffsiteApplyFlow) so these tests isolate the
new orchestration: the 7 tool wrappers, the finish()-is-advisory-only safety
invariant, the per-tool-call terminal-state short-circuit, the 30-call cap,
and the duplicate/stall guard.
"""

from __future__ import annotations

import asyncio
import json

import pytest

import offsite_agentic

AOF = offsite_agentic.AgenticOffsiteApplyFlow


def _run(coro):
    return asyncio.run(coro)


# ── fake Page / Locator / Context ────────────────────────────────────────────

class _Loc:
    def __init__(self, count=0, visible=False):
        self._count = count
        self._visible = visible

    @property
    def first(self):
        return self

    async def count(self):
        return self._count

    async def is_visible(self):
        return self._visible

    async def click(self):
        pass


class _Page:
    """Just what _agentic_guided_apply's pre-turn checks touch."""

    def __init__(self, url="https://jobs.acme.com/careers/1"):
        self.url = url

    def locator(self, _selector):
        return _Loc(0)

    async def evaluate(self, _js):
        return ""

    async def wait_for_load_state(self, _state, timeout=None):
        pass

    async def content(self):
        return "<html></html>"


class _Context:
    pages: list = []


def _agentic(**kw):
    profile = kw.pop("profile", None) or {"resume_path": "resume.pdf", "full_name": "Test User"}
    return AOF(
        page=_Page(), context=_Context(), profile=profile, auto_mode=True,
        callbacks={}, generated_password=kw.pop("generated_password", "x"),
        company_name="ACME", job_title="Dev",
        job_description=kw.pop("job_description", "Build things."),
        **kw,
    )


class _FakeFunction:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class _FakeToolCall:
    def __init__(self, id_, name, arguments_dict):
        self.id = id_
        self.function = _FakeFunction(name, json.dumps(arguments_dict))


class _FakeMessage:
    def __init__(self, tool_calls=None, content=None):
        self.role = "assistant"
        self.content = content
        self.tool_calls = tool_calls


def _wire_common_seams(monkeypatch, flow, *, terminal_state_results=None,
                       auth_result=None):
    """Stub out everything _agentic_guided_apply's one-time setup and
    per-turn pre-checks touch, so tests exercise only the new tool-calling
    orchestration."""
    async def _none(*_a, **_kw):
        return None

    async def _empty(*_a, **_kw):
        return ""

    async def _summary(*_a, **_kw):
        return "a job summary"

    monkeypatch.setattr(type(flow), "_detect_expired", _none)
    monkeypatch.setattr(type(flow), "_detect_bot_wall", _empty)
    monkeypatch.setattr(type(flow), "_summarize_job", _summary)
    monkeypatch.setattr(type(flow), "_prefer_workday_autofill", _none)

    async def _auth(_self, _page, *, phase):
        return auth_result
    monkeypatch.setattr(type(flow), "_handle_auth", _auth)

    if terminal_state_results is not None:
        _it = iter(terminal_state_results)

        async def _terminal(_self, _page, *, step):
            return next(_it, None)
        monkeypatch.setattr(type(flow), "_detect_terminal_state", _terminal)
    else:
        monkeypatch.setattr(type(flow), "_detect_terminal_state", _none)

    # Speed: skip the real inter-turn throttle sleep.
    _real_sleep = asyncio.sleep
    monkeypatch.setattr(offsite_agentic.asyncio, "sleep", lambda *_a, **_k: _real_sleep(0))


def _patch_client(monkeypatch, message_queue):
    calls = []

    def fake_resolve(cfg=None):
        return ("FAKE_CLIENT", "fake-model")

    def fake_call_with_tools(client, model, messages, tools, *, tool_choice="auto"):
        calls.append({"client": client, "model": model, "n_messages": len(messages)})
        return message_queue.pop(0)

    monkeypatch.setattr(offsite_agentic.browser_use_client, "resolve_browser_use", fake_resolve)
    monkeypatch.setattr(offsite_agentic.browser_use_client, "call_with_tools", fake_call_with_tools)
    return calls


# ── tool schema ───────────────────────────────────────────────────────────

def test_seven_tools_defined():
    names = {t["function"]["name"] for t in AOF._TOOLS}
    assert names == {
        "read_page", "fill_field", "select_field", "click", "upload_resume", "scroll", "finish",
    }


def test_upload_resume_schema_has_no_path_argument():
    """T54's non-negotiable: the model can never supply a file path — only a
    selector. The resume path always comes from self.profile."""
    schema = next(t for t in AOF._TOOLS if t["function"]["name"] == "upload_resume")
    props = schema["function"]["parameters"]["properties"]
    assert set(props) == {"selector"}


def test_no_tool_schema_mentions_password_or_credentials():
    blob = json.dumps(AOF._TOOLS).lower()
    assert "password" not in blob
    assert "credential" not in blob


def test_tool_schemas_have_no_shell_or_network_tool():
    names = {t["function"]["name"] for t in AOF._TOOLS}
    assert not ({"bash", "shell", "exec", "http", "fetch", "curl"} & names)


# ── tool wrappers: build _StepState, call _execute_action, sync back ───────

def _patch_execute_action(monkeypatch, flow, *, result=None, mutate=None):
    calls = []

    async def fake_execute_action(_self, action_type, text, value, state):
        calls.append({
            "action_type": action_type, "text": text, "value": value, "selector": state.selector,
        })
        if mutate:
            mutate(state)
        return result

    monkeypatch.setattr(type(flow), "_execute_action", fake_execute_action)
    return calls


def test_tool_fill_field_calls_execute_action_and_syncs_page(monkeypatch):
    flow = _agentic()
    new_page = _Page(url="https://after-fill.example.com")

    def mutate(state):
        state.page = new_page
        state.submit_clicked = False

    calls = _patch_execute_action(monkeypatch, flow, mutate=mutate)
    out = _run(flow._tool_fill_field("#email", "a@b.com"))

    assert out is None
    assert calls == [
        {"action_type": "fill", "text": "", "value": "a@b.com", "selector": "#email"},
    ]
    assert flow.page is new_page
    assert flow._form_engaged is True


def test_tool_select_field_engages_form(monkeypatch):
    flow = _agentic()
    calls = _patch_execute_action(monkeypatch, flow)
    _run(flow._tool_select_field("#country", "United States"))
    assert calls == [
        {"action_type": "select", "text": "", "value": "United States", "selector": "#country"},
    ]
    assert flow._form_engaged is True


def test_tool_upload_resume_takes_only_selector(monkeypatch):
    flow = _agentic()
    calls = _patch_execute_action(monkeypatch, flow)
    _run(flow._tool_upload_resume("#resume-input"))
    assert calls == [
        {"action_type": "upload", "text": "", "value": "", "selector": "#resume-input"},
    ]
    assert flow._form_engaged is True


def test_tool_scroll_does_not_engage_form(monkeypatch):
    flow = _agentic()
    calls = _patch_execute_action(monkeypatch, flow)
    _run(flow._tool_scroll())
    assert calls == [{"action_type": "scroll", "text": "", "value": "", "selector": ""}]
    assert flow._form_engaged is False


def test_tool_click_engages_form_only_when_click_hit_target(monkeypatch):
    flow = _agentic()

    def mutate_hit(state):
        state.click_hit_target = True
    _patch_execute_action(monkeypatch, flow, mutate=mutate_hit)
    _run(flow._tool_click("button.submit", "Submit"))
    assert flow._form_engaged is True

    flow2 = _agentic()

    def mutate_miss(state):
        state.click_hit_target = False
    _patch_execute_action(monkeypatch, flow2, mutate=mutate_miss)
    _run(flow2._tool_click("a.nav", "Apply"))
    assert flow2._form_engaged is False


def test_tool_read_page_delegates_to_page_snapshot(monkeypatch):
    flow = _agentic()
    snap = {"visible_text": "hi", "fields": [], "buttons": []}

    async def fake_snapshot(_self, page):
        assert page is flow.page
        return snap

    monkeypatch.setattr(type(flow), "_page_snapshot", fake_snapshot)
    out = _run(flow._tool_read_page())
    assert out == snap


def test_terminal_result_from_execute_action_propagates(monkeypatch):
    """A submit-type click already routes through _handle_submit inside the
    inherited _execute_action, which returns a verified terminal status
    directly — the tool wrapper must pass it straight through."""
    flow = _agentic()
    _patch_execute_action(monkeypatch, flow, result="applied")
    out = _run(flow._tool_click('button:has-text("Submit")', "Submit"))
    assert out == "applied"


# ── finish(): advisory only, independently verified ─────────────────────────

def test_finish_applied_not_confirmed_does_not_terminate(monkeypatch):
    flow = _agentic()

    async def fake_check(_self, _page, _prev_url, submit_attempted=True):
        return False, "no confirmation text found"

    monkeypatch.setattr(type(flow), "_check_submission_result", fake_check)
    content, terminal = _run(flow._tool_finish(flow.page, flow.page.url, "applied", "looks done"))
    assert terminal is None
    assert "Not confirmed" in content


def test_finish_applied_confirmed_terminates(monkeypatch):
    flow = _agentic()

    async def fake_check(_self, _page, _prev_url, submit_attempted=True):
        return True, "confirmation text 'thank you'"

    monkeypatch.setattr(type(flow), "_check_submission_result", fake_check)
    content, terminal = _run(flow._tool_finish(flow.page, flow.page.url, "applied", "done"))
    assert terminal == "applied"
    assert "confirmed" in content


@pytest.mark.parametrize("status", ["skipped", "failed", "blocked"])
def test_finish_negative_status_trusted_directly_no_verification_call(monkeypatch, status):
    flow = _agentic()

    async def boom(*_a, **_kw):
        raise AssertionError("_check_submission_result must not be called for a non-applied status")

    monkeypatch.setattr(type(flow), "_check_submission_result", boom)
    _content, terminal = _run(flow._tool_finish(flow.page, flow.page.url, status, "reason"))
    assert terminal == status


def test_finish_unrecognized_status_treated_as_failed():
    flow = _agentic()
    _content, terminal = _run(flow._tool_finish(flow.page, flow.page.url, "bogus", "?"))
    assert terminal == "failed"


# ── _execute_tool_call dispatch ──────────────────────────────────────────

def test_execute_tool_call_recaptcha_selector_short_circuits_without_execute_action(monkeypatch):
    flow = _agentic()

    async def boom(*_a, **_kw):
        raise AssertionError("_execute_action must not be called for a reCAPTCHA selector")

    monkeypatch.setattr(type(flow), "_execute_action", boom)
    content, terminal = _run(flow._execute_tool_call(
        flow.page, "click", {"selector": "#g-recaptcha-response-100000"}, flow.page.url,
    ))
    assert terminal == "skipped"
    assert "recaptcha" in content.lower()


def test_execute_tool_call_read_page_returns_json(monkeypatch):
    flow = _agentic()
    snap = {"visible_text": "abc", "fields": [], "buttons": []}

    async def fake_snapshot(_self, _page):
        return snap

    monkeypatch.setattr(type(flow), "_page_snapshot", fake_snapshot)
    content, terminal = _run(flow._execute_tool_call(flow.page, "read_page", {}, flow.page.url))
    assert terminal is None
    assert json.loads(content) == snap


def test_execute_tool_call_unknown_tool_is_not_terminal(monkeypatch):
    flow = _agentic()
    content, terminal = _run(
        flow._execute_tool_call(flow.page, "delete_everything", {}, flow.page.url)
    )
    assert terminal is None
    assert "unknown tool" in content


# ── the conversation loop ────────────────────────────────────────────────

def test_multiple_tool_calls_only_first_executes_before_terminal_check_fires(monkeypatch):
    flow = _agentic()
    _wire_common_seams(monkeypatch, flow, terminal_state_results=["skipped"])

    exec_calls = []

    async def fake_execute_action(_self, action_type, _text, _value, state):
        exec_calls.append((action_type, state.selector))
        return None  # not terminal on its own — _detect_terminal_state fires

    monkeypatch.setattr(type(flow), "_execute_action", fake_execute_action)

    tc1 = _FakeToolCall("call_1", "fill_field", {"selector": "#a", "value": "1"})
    tc2 = _FakeToolCall("call_2", "fill_field", {"selector": "#b", "value": "2"})
    _patch_client(monkeypatch, [_FakeMessage(tool_calls=[tc1, tc2])])

    out = _run(flow._agentic_guided_apply(_Page()))

    assert out == "skipped"
    assert exec_calls == [("fill", "#a")]  # tc2 never executed


def test_finish_applied_in_a_multi_call_batch_is_verified_before_trusting(monkeypatch):
    """finish(applied) sitting among several tool_calls in ONE response must
    still go through _check_submission_result — trust nothing on the model's
    say-so alone."""
    flow = _agentic()
    _wire_common_seams(monkeypatch, flow)

    async def fake_execute_action(_self, _action_type, _text, _value, _state):
        return None

    monkeypatch.setattr(type(flow), "_execute_action", fake_execute_action)

    checked = []

    async def fake_check(_self, _page, _prev_url, submit_attempted=True):
        checked.append(True)
        return True, "confirmation text 'thank you'"

    monkeypatch.setattr(type(flow), "_check_submission_result", fake_check)

    tc1 = _FakeToolCall("call_1", "fill_field", {"selector": "#a", "value": "1"})
    tc2 = _FakeToolCall("call_2", "finish", {"status": "applied", "reason": "done"})
    _patch_client(monkeypatch, [_FakeMessage(tool_calls=[tc1, tc2])])

    out = _run(flow._agentic_guided_apply(_Page()))

    assert out == "applied"
    assert checked == [True]


def test_stall_guard_terminates_after_max_stalls(monkeypatch):
    flow = _agentic()
    _wire_common_seams(monkeypatch, flow)

    async def boom(*_a, **_kw):
        raise AssertionError("_execute_action must never be called — the model never calls a tool")

    monkeypatch.setattr(type(flow), "_execute_action", boom)

    stall_msgs = [
        _FakeMessage(tool_calls=None, content="thinking...") for _ in range(AOF._MAX_STALLS + 2)
    ]
    _patch_client(monkeypatch, stall_msgs)

    out = _run(flow._agentic_guided_apply(_Page()))
    assert out == "failed"


def test_duplicate_call_guard_terminates_before_30_call_cap(monkeypatch):
    flow = _agentic()
    _wire_common_seams(monkeypatch, flow)

    exec_calls = []

    async def fake_execute_action(_self, action_type, _text, _value, state):
        exec_calls.append((action_type, state.selector))
        return None

    monkeypatch.setattr(type(flow), "_execute_action", fake_execute_action)

    # Same fill_field call every turn, page.url never changes -> the
    # duplicate guard must fire well before the 30-call cap.
    same_calls = [
        _FakeMessage(tool_calls=[
            _FakeToolCall(f"call_{i}", "fill_field", {"selector": "#stuck", "value": "x"})
        ])
        for i in range(10)
    ]
    _patch_client(monkeypatch, same_calls)

    out = _run(flow._agentic_guided_apply(_Page()))
    assert out in ("failed", "blocked")
    # The guard checks BEFORE executing: it fires on the 3rd sighting of the
    # same call, so only MAX_DUPLICATE_REPEATS - 1 executions actually happen.
    assert len(exec_calls) == AOF._MAX_DUPLICATE_REPEATS - 1


def test_tool_call_cap_terminates_after_30_executions(monkeypatch):
    flow = _agentic()
    _wire_common_seams(monkeypatch, flow)

    exec_calls = []

    async def fake_execute_action(_self, action_type, _text, _value, state):
        exec_calls.append((action_type, state.selector))
        return None

    monkeypatch.setattr(type(flow), "_execute_action", fake_execute_action)

    # Distinct selector every turn -> never trips the duplicate guard, so the
    # 30-execution cap is what fires.
    messages = [
        _FakeMessage(tool_calls=[
            _FakeToolCall(f"call_{i}", "fill_field", {"selector": f"#f{i}", "value": "x"})
        ])
        for i in range(40)
    ]
    _patch_client(monkeypatch, messages)

    out = _run(flow._agentic_guided_apply(_Page()))
    assert out == "failed"  # form_engaged (fill_field ran) on a non-ATS host
    assert len(exec_calls) == AOF._MAX_TOOL_CALLS


def test_landing_domain_spam_precheck_skips_before_any_model_call(monkeypatch):
    flow = _agentic()
    _wire_common_seams(monkeypatch, flow)
    calls = _patch_client(monkeypatch, [])  # must never be called

    out = _run(flow._agentic_guided_apply(_Page(url="https://jobright.ai/jobs/1")))
    assert out == "skipped"
    assert calls == []


def test_url_phase_auth_wall_returns_before_any_model_call(monkeypatch):
    flow = _agentic()
    _wire_common_seams(monkeypatch, flow, auth_result="blocked")
    calls = _patch_client(monkeypatch, [])  # must never be called

    out = _run(flow._agentic_guided_apply(_Page()))
    assert out == "blocked"
    assert calls == []


# ── credential-leak guard ────────────────────────────────────────────────

def test_no_credential_strings_in_initial_messages_or_tool_schemas():
    flow = _agentic(generated_password="SuperSecretPW123!")
    flow.profile["sso_password"] = "AnotherSecret!"
    flow.profile["password"] = "ProfilePassword!"

    messages = flow._build_initial_messages("a job summary")
    blob = json.dumps(messages)
    for secret in ("SuperSecretPW123!", "AnotherSecret!", "ProfilePassword!"):
        assert secret not in blob

    tools_blob = json.dumps(AOF._TOOLS)
    for secret in ("SuperSecretPW123!", "AnotherSecret!", "ProfilePassword!"):
        assert secret not in tools_blob


def test_full_simulated_run_never_leaks_credentials(monkeypatch):
    """End-to-end simulated run — a fill, then a verified finish(applied) —
    with account/SSO credentials set on the profile. Auth (_handle_auth) is
    stubbed to simulate having been through an auth wall earlier, but stays
    entirely outside the tool-call loop per the T54 security constraint.
    Asserts no secret string ever appears in any message sent to the model."""
    flow = _agentic(generated_password="SuperSecretPW123!")
    flow.profile["sso_password"] = "AnotherSecret!"
    flow.profile["password"] = "ProfilePassword!"

    _wire_common_seams(monkeypatch, flow, auth_result=None)  # no wall on this run

    async def fake_execute_action(_self, _action_type, _text, _value, _state):
        return None

    monkeypatch.setattr(type(flow), "_execute_action", fake_execute_action)

    async def fake_check(_self, _page, _prev_url, submit_attempted=True):
        return True, "confirmation text 'thank you'"

    monkeypatch.setattr(type(flow), "_check_submission_result", fake_check)

    sent_snapshots = []
    queue = [
        _FakeMessage(tool_calls=[
            _FakeToolCall("c1", "fill_field", {"selector": "#email", "value": "a@b.com"}),
        ]),
        _FakeMessage(tool_calls=[
            _FakeToolCall("c2", "finish", {"status": "applied", "reason": "done"}),
        ]),
    ]

    def fake_call_with_tools(client, model, messages, tools, *, tool_choice="auto"):
        sent_snapshots.append(json.loads(json.dumps(messages)))
        return queue.pop(0)

    monkeypatch.setattr(
        offsite_agentic.browser_use_client, "resolve_browser_use", lambda cfg=None: ("C", "m"),
    )
    monkeypatch.setattr(offsite_agentic.browser_use_client, "call_with_tools", fake_call_with_tools)

    out = _run(flow._agentic_guided_apply(_Page()))
    assert out == "applied"

    blob = json.dumps(sent_snapshots)
    for secret in ("SuperSecretPW123!", "AnotherSecret!", "ProfilePassword!"):
        assert secret not in blob


# ── config wiring / hard-failure propagation ─────────────────────────────

def test_browser_use_config_error_propagates_without_fallback(monkeypatch):
    """A hard config failure for this engine must propagate — never silently
    fall back to the stepwise engine (that's apply_jobs.py's call, made
    before construction, not this class's)."""
    flow = _agentic()

    def raise_cfg(cfg=None):
        raise offsite_agentic.browser_use_client.BrowserUseConfigError("no key")

    monkeypatch.setattr(offsite_agentic.browser_use_client, "resolve_browser_use", raise_cfg)

    with pytest.raises(offsite_agentic.browser_use_client.BrowserUseConfigError):
        _run(flow._agentic_guided_apply(_Page()))


# ── acceptance guard: no Claude/Anthropic import in this module ─────────

def test_no_claude_or_openai_sdk_import_leaked_into_offsite_agentic():
    import inspect
    src = inspect.getsource(offsite_agentic)
    assert "import anthropic" not in src
    assert "from anthropic" not in src
    assert "claude_agent_sdk" not in src
    assert "AsyncOpenAI" not in src
    assert "from openai" not in src
