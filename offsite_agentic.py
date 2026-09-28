"""offsite_agentic.py — opt-in agentic tool-use engine for OffsiteApply (T54).

``AgenticOffsiteApplyFlow`` is a drop-in alternative to
``linkedin_apply.OffsiteApplyFlow``'s default ``_llm_guided_apply`` step loop.
The step loop is stateless across iterations: every step rebuilds one giant
free-text prompt from scratch (profile + job summary + last-10 action history
+ last-5 context notes + a rendered page snapshot + a fixed rules block),
fires an isolated ``llm.query()`` call, and hand-parses the JSON reply into a
single ``{action, selector, value}`` tuple. This engine instead holds a real
conversation: a plain, hand-maintained OpenAI-style ``messages`` list (system +
user + assistant + tool roles) that the model's own tool calls extend turn by
turn, with 7 tools backed by Playwright actions. No vendor "session" object —
that is what makes it provider-agnostic: it runs on
``browser_use_client.call_with_tools``, standard
``chat.completions.create(..., tools=[...])`` function calling, so any
OpenAI-compatible provider works, not just Claude.

**Subclass, not a rewrite.** Everything below is inherited from
``OffsiteApplyFlow`` unchanged: ``_page_snapshot``, ``_execute_action``,
``_handle_auth``/``_try_login``/``_try_register``/``_fill_registration_form``,
``_classify_domain``, ``_detect_expired``, ``_detect_bot_wall``,
``_detect_terminal_state``, ``_check_submission_result``,
``_prefer_workday_autofill``, ``_summarize_job``, and the module-level
``verify_submission`` / ``_terminal_state_for_stall`` helpers. The only new
method is :meth:`_agentic_guided_apply`, the tool-calling replacement for
``_llm_guided_apply``; :meth:`_fill_external_form` and
:meth:`assist_from_page` are overridden to call it instead.

**Safety invariant — the model's ``finish()`` call is advisory only.** A real
submit click is already independently verified inside the inherited
``_execute_action`` (its ``click`` branch routes a submit-type button through
``_handle_submit``, which calls ``_check_submission_result`` /
``verify_submission`` itself). ``finish(status="applied", ...)`` does not go
through ``_execute_action`` at all — it never touches the page — so this
engine runs the same verification explicitly before ever trusting it, and
does not terminate on an unconfirmed ``finish(applied)``.

**Tool surface is deliberately narrow**: ``read_page``, ``fill_field``,
``select_field``, ``click``, ``upload_resume``, ``scroll``, ``finish``. No
shell, no filesystem access beyond the resume upload (which takes only a
selector — the resume path always comes from ``self.profile``, never from the
model), no arbitrary network tool. Auth/login/registration
(``_handle_auth`` and its helpers) never appear as a callable tool and stay
entirely outside the tool-call loop, exactly as in the step-loop engine.

Ships opt-in (``OFFSITE_ENGINE=agentic``, default ``stepwise`` — see
``config.get_offsite_engine``). The default ``browser_use`` NIM model
(``deepseek-ai/deepseek-v4-flash-0731``) has never been validated for
tool-calling reliability specifically — see docs/TICKETS.md T54.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any
from urllib.parse import urlparse

import browser_use_client
from linkedin_apply import (
    OffsiteApplyFlow,
    Page,
    _StepState,
    _terminal_state_for_stall,
)


class AgenticOffsiteApplyFlow(OffsiteApplyFlow):
    """Tool-calling OffsiteApply engine. See module docstring."""

    # Matches _llm_guided_apply's `for step in range(30)` cap — counts total
    # tool *executions*, not model turns (one turn can request several).
    _MAX_TOOL_CALLS = 30
    # Consecutive model replies with zero tool_calls before giving up. A
    # tool-calling model that stops calling tools is stalled, not thinking.
    _MAX_STALLS = 3
    # Same (selector, action, value) tuple with no page.url change this many
    # times in a row → the model is stuck, not making progress.
    _MAX_DUPLICATE_REPEATS = 3

    # Verbatim from linkedin_apply.py's _ask_llm_action prompt (the "Rules:"
    # block) — these encode real production fixes (T31, T39, T45, …) and are
    # NOT rewritten here, only reused. The one paragraph deliberately dropped
    # is the JSON-object *output format* instruction ("Output EXACTLY ONE
    # JSON object…") immediately above it in the original — tool-calling
    # replaces that entirely, so the model calls a tool instead of emitting a
    # JSON action object; keeping that paragraph here would be actively
    # misleading.
    _RULES = (
        "Rules: done=thank-you/confirmation visible. "
        "failed=captcha/identity-verify/stuck/job-no-longer-available. If you see 'job not "
        "found', 'no longer available', 'position closed', or similar expired-job text -> call "
        "finish(failed) with reason 'job no longer available'. If you see a tab or link labeled "
        "'Application' or 'Apply' in the page, click it immediately -- it opens the application "
        "form. click=button or link (use :has-text() NOT :contains()). fill_field=empty text "
        "input. select_field=native <select> element, radio, or checkbox ONLY. "
        "upload_resume=resume file input. scroll=reveal more. Priority: Fill ALL [EMPTY] fields "
        "in top-to-bottom order BEFORE clicking any Submit/Apply button. Always act on the first "
        "[EMPTY] field in the list above -- do not skip ahead to offscreen fields. NEVER fill a "
        "[FILLED] field -- it already has the correct value, skip it. Never re-fill a field you "
        "already filled earlier in this conversation. Never Cancel/Sign-out. Never fill or upload "
        "to any field labeled 'Cover Letter' or 'Covering Letter' -- skip entirely. Sponsorship "
        "questions: answer '{sponsor_val}'. Work authorization: always 'Yes'. For a 'years of "
        "<skill>' or 'how many years' field, fill just a number -- never 0 unless the profile "
        "clearly shows no experience with that skill; give a reasonable non-zero figure that does "
        "not exceed the applicant's overall years of experience (yrs=...). CRITICAL: Never "
        "fabricate URLs, social media handles, usernames, or any information not in the profile. "
        "For any field where you have no value (optional URL, referral email, social handle, "
        "portfolio, a 'who referred you' / 'referred by' / referral name field, etc.) -- do NOT "
        "call fill_field at all. Skip that field entirely and move to the next [EMPTY] field or "
        "click Submit. Never put the applicant's own name in a referral field. Never fill a field "
        "with an empty string -- an empty fill does nothing useful and can trigger browser "
        "validation errors. If all [EMPTY] fields are filled and a submit button is only visible "
        "off-screen, click it directly -- do not scroll first. Never click bare 'Apply' nav links "
        "-- only 'Apply Now', 'Apply for this job', 'Submit application'. Never click "
        "Login/Sign-in unless you just filled email+password. Never click utility buttons (Save, "
        "Bookmark, Share, Follow, Job alerts, Talent community, Sign in with LinkedIn). Call "
        "finish() only once you believe the application is fully submitted (status=applied) or "
        "the job is unrecoverable (status=skipped/failed/blocked) -- your finish(applied) claim "
        "will be independently verified before it is trusted, so do not call it speculatively."
    )

    _TOOLS: list[dict] = [
        {
            "type": "function",
            "function": {
                "name": "read_page",
                "description": (
                    "Get a fresh snapshot of the current page: visible text, "
                    "every form field (with its current value and whether it "
                    "is [EMPTY] or [FILLED]), and the visible buttons/links. "
                    "Call this whenever you need to see the current page "
                    "state -- e.g. right after a navigation, after filling "
                    "several fields, or if you are unsure what changed."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "fill_field",
                "description": (
                    "Fill a text input or textarea with a value. Never use "
                    "this for a native <select> dropdown, a radio button, or "
                    "a checkbox (use select_field for those), and never for a "
                    "file upload (use upload_resume)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "selector": {
                            "type": "string",
                            "description": (
                                "Playwright CSS selector for the target field, "
                                "e.g. '#email' or '[name=\"phone\"]'."
                            ),
                        },
                        "value": {
                            "type": "string",
                            "description": "The text to fill in.",
                        },
                    },
                    "required": ["selector", "value"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "select_field",
                "description": (
                    "Choose an option on a native <select> dropdown, or check "
                    "a radio button / checkbox, by its visible label or value."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "selector": {
                            "type": "string",
                            "description": "Playwright CSS selector for the target field.",
                        },
                        "value": {
                            "type": "string",
                            "description": "The option's visible label (or value) to select.",
                        },
                    },
                    "required": ["selector", "value"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "click",
                "description": (
                    "Click a button or link -- to advance to the next step, "
                    "open a section, or submit the application. Use "
                    ":has-text() selectors, not :contains()."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "selector": {
                            "type": "string",
                            "description": "Playwright CSS selector for the target element.",
                        },
                        "text": {
                            "type": "string",
                            "description": (
                                "Fallback: the visible button/link text, used "
                                "if the selector does not resolve."
                            ),
                        },
                    },
                    "required": ["selector"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "upload_resume",
                "description": (
                    "Upload the applicant's resume to a file input field. "
                    "Never target a cover-letter upload field with this."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "selector": {
                            "type": "string",
                            "description": "Playwright CSS selector for the file input.",
                        },
                    },
                    "required": ["selector"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "scroll",
                "description": "Scroll the page down to reveal more content.",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "finish",
                "description": (
                    "Declare the application flow complete or unrecoverable. "
                    "This is advisory only: an 'applied' claim is "
                    "independently verified against the actual page state "
                    "before it is trusted."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "status": {
                            "type": "string",
                            "enum": ["applied", "skipped", "failed", "blocked"],
                        },
                        "reason": {
                            "type": "string",
                            "description": "One sentence explaining why.",
                        },
                    },
                    "required": ["status", "reason"],
                    "additionalProperties": False,
                },
            },
        },
    ]

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Mirrors the step loop's per-job locals (linkedin_apply.py's
        # _llm_guided_apply): reset at the top of _agentic_guided_apply, held
        # as instance state here only because the tool wrapper methods below
        # need to read/mutate them across separate tool-call dispatches.
        self._forced_filled: dict[str, str] = {}
        self._submit_clicked = False
        self._form_engaged = False

    # ── entry points: reuse OffsiteApplyFlow.run() unchanged; only the final
    # hop into the loop changes ──────────────────────────────────────────────

    async def _fill_external_form(self, page: Page) -> str:
        return await self._agentic_guided_apply(page)

    async def assist_from_page(self) -> str:
        """Resume the agentic loop from the application form tab.

        Identical tab-selection logic to
        ``OffsiteApplyFlow.assist_from_page`` (prefers an already-open
        non-LinkedIn tab over the main LinkedIn page) but resumes via
        :meth:`_agentic_guided_apply` instead of ``_llm_guided_apply``.
        Duplicated rather than calling ``super()`` because the base
        implementation is hardwired to ``self._llm_guided_apply``.
        """
        target = self.page
        for pg in self.context.pages:
            try:
                if (
                    not pg.is_closed()
                    and "linkedin.com" not in pg.url
                    and pg.url not in ("", "about:blank")
                ):
                    target = pg
                    break
            except Exception:
                continue
        print(f"  [Agentic] Resuming from: {target.url}")
        return await self._agentic_guided_apply(target)

    # ── message construction ─────────────────────────────────────────────

    def _build_initial_messages(self, job_summary: str) -> list[dict]:
        """One-time system + user context for the whole job — never rebuilt
        again. This is the T54 improvement over the step loop: everything
        static goes in once, and the conversation (not a resent prompt) is
        the memory from here on.
        """
        p = self.profile
        resume_path = p.get("resume_path", "")
        if resume_path:
            resume_path = (
                resume_path if os.path.isabs(resume_path) else os.path.abspath(resume_path)
            )
        _need_sponsor = p.get("need_sponsorship", "")
        _sponsor_val = "Yes" if str(_need_sponsor).lower() in ("yes", "true", "1") else "No"
        profile_line = (
            f"name={p.get('full_name','')} preferred_name={p.get('preferred_name','')} "
            f"email={p.get('email','')} "
            f"phone={p.get('phone','')} location={p.get('location','')} "
            f"title={p.get('current_title','')} yrs={p.get('years_experience','')} "
            f"auth={p.get('work_authorization','')} needs_sponsorship={_sponsor_val} "
            f"linkedin={p.get('linkedin_url','')} github={p.get('github_url','')} "
            f"resume={resume_path}"
        )
        rules = self._RULES.format(sponsor_val=_sponsor_val)
        system = (
            "You are operating a headless web browser via tools to complete a "
            "job application on an external company careers site. Call "
            "exactly one meaningful tool per turn to make progress; call "
            "read_page whenever you need to see the current page state. "
            "When the application is fully submitted or unrecoverable, call "
            "finish with the appropriate status.\n\n" + rules
        )
        job_ctx = (
            f"Job: {self.job_title} at {self.company_name}\n"
            f"Summary: {job_summary}\n\n"
            f"Full description:\n{(self.job_description or '')[:3000]}"
        )
        user = f"Profile: {profile_line}\n\n{job_ctx}"
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

    @staticmethod
    def _message_to_dict(message: Any) -> dict:
        """Normalise an OpenAI ``ChatCompletionMessage`` (or a dict / test
        double with the same shape) into a plain dict appendable to the
        running ``messages`` list."""
        if isinstance(message, dict):
            return message
        if hasattr(message, "model_dump"):
            return message.model_dump(exclude_none=True)
        d: dict[str, Any] = {"role": getattr(message, "role", "assistant")}
        content = getattr(message, "content", None)
        if content is not None:
            d["content"] = content
        tool_calls = getattr(message, "tool_calls", None)
        if tool_calls:
            d["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in tool_calls
            ]
        return d

    # ── tool wrapper methods ─────────────────────────────────────────────
    # Each builds a _StepState exactly as the step loop's orchestrator does
    # (linkedin_apply.py:4812-4842), calls the inherited _execute_action, then
    # syncs state back onto self. Shared via _dispatch so all 5 page-touching
    # tools do this identically.

    async def _dispatch(
        self, action_type: str, *, selector: str = "", text: str = "", value: str = "",
    ) -> str | None:
        state = _StepState(self.page, selector, self._forced_filled, self._submit_clicked)
        result = await self._execute_action(action_type, text, value, state)
        self.page = state.page
        self._submit_clicked = state.submit_clicked
        if action_type in ("fill", "select", "upload"):
            self._form_engaged = True
        elif action_type == "click" and state.click_hit_target:
            self._form_engaged = True
        return result

    async def _tool_read_page(self) -> dict:
        return await self._page_snapshot(self.page)

    async def _tool_fill_field(self, selector: str, value: str) -> str | None:
        return await self._dispatch("fill", selector=selector, value=value)

    async def _tool_select_field(self, selector: str, value: str) -> str | None:
        return await self._dispatch("select", selector=selector, value=value)

    async def _tool_click(self, selector: str, text: str = "") -> str | None:
        return await self._dispatch("click", selector=selector, text=text)

    async def _tool_upload_resume(self, selector: str) -> str | None:
        # Deliberately takes only a selector — no path argument exists on
        # this tool's schema. _execute_action's upload branch resolves the
        # actual file from self.profile["resume_path"] itself; nothing here
        # or in the schema lets the model supply a path.
        return await self._dispatch("upload", selector=selector)

    async def _tool_scroll(self) -> str | None:
        return await self._dispatch("scroll")

    async def _tool_finish(
        self, page: Page, prev_url: str, status: str, reason: str,
    ) -> tuple[str, str | None]:
        """The safety invariant: an 'applied' claim is independently verified
        (the same check a real submit click already goes through inside
        ``_execute_action`` -> ``_handle_submit``) before ever being trusted.
        Everything else is trusted directly — there's no page-state claim to
        get wrong about "skipped"/"failed"/"blocked"."""
        if status == "applied":
            confirmed, msg = await self._check_submission_result(
                page, prev_url, submit_attempted=self._submit_clicked,
            )
            if confirmed:
                return f"confirmed: {msg}", "applied"
            return (
                f"Not confirmed ({msg}). Do not call finish(applied) again "
                f"until the submission is actually verified -- continue the "
                f"application.",
                None,
            )
        if status in ("skipped", "failed", "blocked"):
            return f"acknowledged: {status} ({reason})", status
        return f"unrecognized status {status!r} -- treating as failed", "failed"

    async def _execute_tool_call(
        self, page: Page, name: str, args: dict, prev_url: str,
    ) -> tuple[str, str | None]:
        """Dispatch one tool call. Returns (tool_result_content,
        terminal_status_or_None). A non-None terminal status means the caller
        must stop the loop and return it immediately."""
        if name == "read_page":
            snapshot = await self._tool_read_page()
            return json.dumps(snapshot)[:8000], None
        if name == "fill_field":
            result = await self._tool_fill_field(args.get("selector", ""), args.get("value", ""))
            return ("filled" if result is None else f"terminal: {result}"), result
        if name == "select_field":
            result = await self._tool_select_field(args.get("selector", ""), args.get("value", ""))
            return ("selected" if result is None else f"terminal: {result}"), result
        if name == "click":
            selector = (args.get("selector") or "").lower()
            if "recaptcha" in selector or "g-recaptcha" in selector:
                # T31-style guard, carried over from _llm_guided_apply's
                # per-step reCAPTCHA check: never even attempt a hallucinated
                # CAPTCHA selector.
                return (
                    "cannot click a reCAPTCHA element -- unsolvable, job will be skipped",
                    "skipped",
                )
            result = await self._tool_click(args.get("selector", ""), args.get("text", ""))
            return ("clicked" if result is None else f"terminal: {result}"), result
        if name == "upload_resume":
            result = await self._tool_upload_resume(args.get("selector", ""))
            return ("uploaded" if result is None else f"terminal: {result}"), result
        if name == "scroll":
            result = await self._tool_scroll()
            return ("scrolled" if result is None else f"terminal: {result}"), result
        if name == "finish":
            return await self._tool_finish(
                page, prev_url, args.get("status", "failed"), args.get("reason", ""),
            )
        return f"unknown tool {name!r} -- ignored", None

    # ── the conversation loop ────────────────────────────────────────────

    async def _agentic_guided_apply(self, page: Page) -> str:
        """Tool-calling replacement for ``_llm_guided_apply``.

        Same per-job reset + one-time landing checks as the step loop
        (spam/blocked-domain pre-check, expired-job check, bot-wall check,
        job summary), then a plain OpenAI-style chat loop: call the model with
        tools -> execute whichever tools it calls, one at a time -> re-check
        the same terminal-state safety nets after each -> repeat.

        Returns 'applied' | 'skipped' | 'failed' | 'blocked' | 'expired'.
        """
        self._auth_attempted = False
        self._registration_attempted = False
        self._forced_filled = {}
        self._submit_clicked = False
        self._form_engaged = False

        # Hard config failure for this whole engine — propagate, do not fall
        # back to the stepwise engine (that decision belongs to apply_jobs.py,
        # which picks the engine class before construction).
        client, model = browser_use_client.resolve_browser_use()

        _landing_domain = urlparse(page.url).netloc.lower()
        _pre = self._classify_domain(_landing_domain)
        if _pre == "skipped":
            print(f"  [Agentic] Spam/aggregator domain ({_landing_domain}) — skipping")
            return "skipped"
        if _pre == "blocked":
            print(
                f"  [Agentic] Blocked auto-apply domain ({_landing_domain}) — "
                f"needs a human, marking blocked"
            )
            return "blocked"

        if await self._detect_expired(page):
            return "expired"

        _wall = await self._detect_bot_wall(page)
        if _wall:
            print(f"  [Agentic] {_wall} on landing form — cannot proceed, skipping")
            return "skipped"

        job_summary = await self._summarize_job()
        _summary_suffix = "..." if len(job_summary) > 120 else ""
        print(f"  [Agentic] Job summary: {job_summary[:120]}{_summary_suffix}")

        _wall = await self._detect_bot_wall(page)
        if _wall:
            print(f"  [Agentic] {_wall} before tool loop — cannot submit, skipping")
            return "skipped"

        messages = self._build_initial_messages(job_summary)

        total_tool_calls = 0
        stall_count = 0
        last_call_key: tuple[str, str, str] | None = None
        last_call_url = ""
        repeat_count = 0
        prev_url = page.url

        try:
            while total_tool_calls < self._MAX_TOOL_CALLS:
                # (a) Same pre-turn deterministic checks _llm_guided_apply
                # runs at the top of its loop, calling the inherited methods
                # directly. Deliberately NOT including the mid-loop
                # phase="form" password-wall check here — that path can log
                # in / register with real credentials, and per the tool
                # surface's security constraint, auth stays entirely outside
                # this tool-call loop.
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=8000)
                except Exception:
                    pass

                current_url = page.url
                _url_domain = urlparse(current_url.lower()).netloc
                _mid = self._classify_domain(_url_domain, include_dead_end=True)
                if _mid == "skipped":
                    print(
                        f"  [Agentic] Redirected to spam/aggregator domain mid-flow "
                        f"({_url_domain}) — skipping"
                    )
                    return "skipped"
                if _mid == "blocked":
                    print(
                        f"  [Agentic] Redirected to blocked/dead-end domain mid-flow "
                        f"({_url_domain}) — marking blocked"
                    )
                    return "blocked"

                await self._prefer_workday_autofill(page)

                _auth = await self._handle_auth(page, phase="url")
                if _auth == self._AUTH_CONTINUE:
                    continue
                if _auth is not None:
                    return _auth

                for _csel in (
                    'button:has-text("Accept All")', 'button:has-text("Accept Cookies")',
                    'button:has-text("Accept")', 'button:has-text("I Accept")',
                    'button:has-text("I agree")', 'button:has-text("Got it")',
                    'button:has-text("Allow all")', '[aria-label*="Accept"]',
                ):
                    try:
                        _cb = page.locator(_csel).first
                        if await _cb.count() > 0 and await _cb.is_visible():
                            await _cb.click()
                            break
                    except Exception:
                        pass

                # _handle_auth / _prefer_workday_autofill never rebind page, but stay consistent
                page = self.page
                prev_url = page.url

                # Throttle model turns after the first, mirroring
                # _llm_guided_apply's step>0 sleep(8) — same rate-limit
                # concern applies to the browser_use endpoint (NIM's free
                # tier by default).
                if total_tool_calls > 0:
                    await asyncio.sleep(8)

                response_message = await asyncio.to_thread(
                    browser_use_client.call_with_tools, client, model, messages, self._TOOLS,
                )
                messages.append(self._message_to_dict(response_message))

                tool_calls = list(getattr(response_message, "tool_calls", None) or [])
                if not tool_calls:
                    stall_count += 1
                    if stall_count >= self._MAX_STALLS:
                        print("  [Agentic] Model stalled (no tool call) — giving up")
                        return "failed"
                    messages.append({
                        "role": "user",
                        "content": (
                            "You must call exactly one tool to make progress on "
                            "this application."
                        ),
                    })
                    continue
                stall_count = 0

                for tc in tool_calls:
                    if total_tool_calls >= self._MAX_TOOL_CALLS:
                        print("  [Agentic] Reached tool-call limit without completion")
                        return _terminal_state_for_stall(page, form_engaged=self._form_engaged)

                    name = tc.function.name
                    try:
                        call_args = json.loads(tc.function.arguments or "{}")
                    except json.JSONDecodeError:
                        call_args = {}
                    selector = str(call_args.get("selector", ""))
                    value = str(call_args.get("value", ""))

                    call_key = (name, selector, value)
                    _url_before_call = page.url
                    if call_key == last_call_key and _url_before_call == last_call_url:
                        repeat_count += 1
                    else:
                        repeat_count = 1
                    last_call_key = call_key
                    last_call_url = _url_before_call
                    if repeat_count >= self._MAX_DUPLICATE_REPEATS:
                        print(
                            f"  [Agentic] Same tool call repeated {repeat_count}x "
                            f"with no page change — giving up"
                        )
                        return _terminal_state_for_stall(page, form_engaged=self._form_engaged)

                    print(f"  [Agentic] Tool call: {name}({call_args})")
                    result_content, terminal = await self._execute_tool_call(
                        page, name, call_args, prev_url,
                    )
                    page = self.page
                    total_tool_calls += 1
                    messages.append(
                        {"role": "tool", "tool_call_id": tc.id, "content": result_content}
                    )

                    if terminal is not None:
                        print(f"  [Agentic] Terminal result from {name!r}: {terminal}")
                        return terminal

                    _terminal2 = await self._detect_terminal_state(page, step=total_tool_calls)
                    if _terminal2 is not None:
                        return _terminal2

                prev_url = page.url

            print("  [Agentic] Reached tool-call limit without completion")
            return _terminal_state_for_stall(page, form_engaged=self._form_engaged)
        finally:
            # No persistent client/session to close here — the OpenAI client
            # is a stateless sync HTTP client, unlike a Claude Agent SDK
            # session. This block exists only to document that self.page /
            # self._forced_filled / self._submit_clicked are already kept
            # consistent on every exit path: each _dispatch() call syncs
            # self.page immediately after _execute_action returns, before the
            # next model turn, including when an exception unwinds out of
            # browser_use_client.call_with_tools.
            pass
