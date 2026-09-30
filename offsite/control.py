"""Control tools + call accounting for one agent run (OA6).

``RunControl`` installs on guard-mcp and gives the orchestrator a single
answer to "how did the model's run end?" — ``outcome``:

* ``"ready"``  — the model called ``report_ready(answers)`` with valid answers;
* ``"human"``  — it called ``request_human(reason, detail)`` (login, register,
  captcha, stuck);
* ``"skip"``   — it called ``skip_application(reason, evidence)``: the form or
  posting says visa sponsorship is not offered and the applicant needs it
  (only allowed when ``sponsorship_skip=True``, i.e. the profile needs
  sponsorship). Terminal: no fallback, no human review — the job is skipped;
* ``"loop"``   — the same action with the same arguments hit an unchanged page
  twice in a row — read-only calls in between (snapshot, wait, …) don't break
  the streak, and the guard banner is not part of "the page"; read-only tools
  on their own loop at three identical calls in a row;
* ``"budget"`` — it made ``BUDGET`` (40) tool calls on this application;
* ``None``     — none of the above (the run ended on its own — OA10 treats
  this as a fallback trigger too).

Once ``outcome`` is set, every further browser call returns ``STOP: …`` so the
model ends its turn; ``report_ready`` is still accepted after a loop/budget
stop (a finished form beats a fallback). ``reset(model)`` starts the count for
the next model (the fallback continues on the same page).

Design: docs/NEW_AGENTIC_APPLY_PLAN.md §3, §5.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal

import mcp_types as types
from pydantic import TypeAdapter, ValidationError

from offsite.guard_mcp import GuardMCP
from offsite.schemas import GeneratedAnswer

BUDGET = 40
STOP_PREFIX = "STOP: "
HUMAN_REASONS = ("login", "register", "captcha", "stuck")
SKIP_REASONS = ("sponsorship_not_offered",)

Outcome = Literal["ready", "human", "skip", "loop", "budget"]
_ENDED_BY = {"ready": "report_ready", "human": "request_human", "skip": "skip_application"}

# Tools that don't change the page: a repeat is less suspicious (waiting,
# re-reading), so they need one more identical call before it counts as a loop.
_READ_ONLY = {"browser_snapshot", "browser_find", "browser_wait_for",
              "browser_take_screenshot", "browser_tabs"}
_COVER = re.compile(r"cover[\s_-]*letter", re.I)
_PAGE_URL = re.compile(r"^- Page URL: .*$", re.M)
_YAML = re.compile(r"```yaml\n(.*?)```", re.S)
_ANSWERS = TypeAdapter(list[GeneratedAnswer])

REPORT_READY = types.Tool(
    name="report_ready",
    description=(
        "Call this ONCE when the application form is completely filled in — every page "
        "up to (not including) the final submit — and you are on the final page. Report "
        "one entry per form field you filled or deliberately left blank. Do NOT click the "
        "final submit button: the human reviews and submits. After this call, stop."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "answers": {"type": "array", "items": GeneratedAnswer.model_json_schema(),
                        "minItems": 1},
        },
        "required": ["answers"],
        "additionalProperties": False,
    },
)
REQUEST_HUMAN = types.Tool(
    name="request_human",
    description=(
        "Call this when a human must act in the browser before you can continue: "
        "'login' (sign-in wall), 'register' (account creation), 'captcha', or 'stuck' "
        "(anything else you cannot do). Explain in `detail` exactly what they should do. "
        "After this call, stop — you will be resumed once the human is done."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "reason": {"type": "string", "enum": list(HUMAN_REASONS)},
            "detail": {"type": "string", "minLength": 1},
        },
        "required": ["reason", "detail"],
        "additionalProperties": False,
    },
)


SKIP_APPLICATION = types.Tool(
    name="skip_application",
    description=(
        "Call this ONLY when the application form or the job posting explicitly says visa "
        "sponsorship is not available / not offered / not allowed for this role (e.g. "
        "'we are unable to sponsor', 'sponsorship is not allowed for this role') — the "
        "applicant needs sponsorship, so the application is skipped. A question like 'Will "
        "you require sponsorship?' is NOT such a statement. Quote the exact sentence in "
        "`evidence`. After this call, stop; do not fill anything else."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "reason": {"type": "string", "enum": list(SKIP_REASONS)},
            "evidence": {"type": "string", "minLength": 10},
        },
        "required": ["reason", "evidence"],
        "additionalProperties": False,
    },
)


def _text_result(text: str, *, error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)],
                                is_error=error)


class RunControl:
    """Per-application run state on guard-mcp: control tools + budget + loop detection."""

    def __init__(self, *, budget: int = BUDGET, sponsorship_skip: bool = False) -> None:
        self.budget = budget
        # skip_application is only offered when the applicant needs sponsorship
        self.sponsorship_skip = sponsorship_skip
        self.model: str | None = None
        self.reset()

    # ── state ─────────────────────────────────────────────────────────────────
    def reset(self, model: str | None = None) -> None:
        """Fresh count for a (new) model on the current application."""
        self.model = model
        self.calls = 0
        self.outcome: Outcome | None = None
        self.stop_reason: str | None = None
        self.answers: list[GeneratedAnswer] = []
        self.warnings: list[str] = []
        self.human_reason: str | None = None
        self.human_detail: str | None = None
        self.skip_reason: str | None = None
        self.skip_evidence: str | None = None
        self._last_action: str | None = None   # last state-changing call (+ page)
        self._last_read: str | None = None     # last read-only call (+ page)
        self._read_repeats = 0
        self._page_sig = ""

    def _stop(self, outcome: Outcome, reason: str) -> None:
        if self.outcome is None:
            self.outcome, self.stop_reason = outcome, reason

    # ── install ───────────────────────────────────────────────────────────────
    def install(self, guard: GuardMCP) -> None:
        guard.add_check(self.check, first=True)      # counts calls later guards refuse
        guard.add_observer(self.observe)
        guard.add_cancel_listener(self.on_cancel)
        guard.add_local_tool(REPORT_READY, self._report_ready)
        guard.add_local_tool(REQUEST_HUMAN, self._request_human)
        if self.sponsorship_skip:
            guard.add_local_tool(SKIP_APPLICATION, self._skip_application)

    # ── accounting (pre-call) ─────────────────────────────────────────────────
    async def check(self, name: str, args: dict[str, Any]) -> str | None:
        if name in (REPORT_READY.name, REQUEST_HUMAN.name, SKIP_APPLICATION.name):
            if self.outcome in _ENDED_BY:
                return f"you already called {self._ended_by()} — end your turn now"
            return None
        if self.outcome is not None:
            return self._stop_text()
        self.calls += 1
        sig = hashlib.sha1(
            json.dumps([name, args, self._page_sig], sort_keys=True, default=str).encode()
        ).hexdigest()
        if name in _READ_ONLY:
            self._read_repeats = self._read_repeats + 1 if sig == self._last_read else 0
            self._last_read = sig
            looped = self._read_repeats >= 2
        else:
            # click → snapshot → the same click on the same page is still a loop
            looped = sig == self._last_action
            self._last_action = sig
            self._last_read, self._read_repeats = None, 0
        if looped:
            self._stop("loop", f"{name} repeated with the same arguments and no page change")
            return self._stop_text()
        if self.calls > self.budget:
            self._stop("budget", f"{self.budget} tool calls used on this application")
            return self._stop_text()
        return None

    def on_cancel(self, name: str, args: dict[str, Any]) -> None:
        """A call the client cancelled never ran to completion — retrying it is not
        a loop."""
        if name in _READ_ONLY:
            self._last_read, self._read_repeats = None, 0
        else:
            self._last_action = None

    def _ended_by(self) -> str:
        return _ENDED_BY.get(self.outcome or "", "a control tool")

    def _stop_text(self) -> str:
        # returned as a guard refusal: "BLOCKED by guard: STOP: …"
        if self.outcome in _ENDED_BY:
            return f"{STOP_PREFIX}you already called {self._ended_by()} — end your turn now"
        return (f"{STOP_PREFIX}{self.stop_reason}. End your turn now; if the form is complete, "
                "call report_ready first.")

    async def observe(self, name: str, args: dict[str, Any],
                      result: types.CallToolResult) -> None:
        """Track the page state (URL + snapshot) from every result that carries it."""
        for c in result.content:
            text = getattr(c, "text", None) or ""
            url, yaml = _PAGE_URL.search(text), _YAML.search(text)
            if not (url or yaml):
                continue
            # the guard banner changes on every refusal; it is not "the page"
            body = "\n".join(line for line in (yaml.group(1) if yaml else "").splitlines()
                             if "BLOCKED by guard" not in line)
            page = (url.group(0) if url else "") + "\n" + body
            self._page_sig = hashlib.sha1(page.encode()).hexdigest()

    # ── control tools ─────────────────────────────────────────────────────────
    async def _report_ready(self, args: dict[str, Any]) -> types.CallToolResult:
        try:
            answers = _ANSWERS.validate_python(args.get("answers"))
        except ValidationError as e:
            errs = "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()[:8])
            return _text_result(f"report_ready rejected — fix and call again: {errs}", error=True)
        if not answers:
            return _text_result("report_ready rejected — report every field you filled",
                                error=True)
        warnings = [f"cover-letter field '{a.field_label}' was filled — it must stay blank"
                    for a in answers if _COVER.search(a.field_label) and a.answer.strip()]
        self.answers, self.warnings = answers, warnings
        self.outcome, self.stop_reason = "ready", None
        flagged = sum(1 for a in answers if a.sensitive or a.confidence < 0.7)
        msg = (f"Recorded {len(answers)} answers ({flagged} flagged for the human). "
               "Stop now — do not click submit; the human reviews and submits.")
        if warnings:
            msg += " Warnings: " + "; ".join(warnings)
        return _text_result(msg)

    async def _request_human(self, args: dict[str, Any]) -> types.CallToolResult:
        reason, detail = args.get("reason"), str(args.get("detail") or "").strip()
        if reason not in HUMAN_REASONS or not detail:
            return _text_result(
                f"request_human rejected — reason must be one of {list(HUMAN_REASONS)} and "
                "detail must say what the human should do", error=True)
        self.human_reason, self.human_detail = reason, detail
        self.outcome, self.stop_reason = "human", None
        return _text_result("The human has been asked. Stop now — you will be resumed.")

    async def _skip_application(self, args: dict[str, Any]) -> types.CallToolResult:
        reason, evidence = args.get("reason"), str(args.get("evidence") or "").strip()
        if not self.sponsorship_skip:
            return _text_result("skip_application is not available for this applicant — "
                                "continue the application", error=True)
        if reason not in SKIP_REASONS or len(evidence) < 10:
            return _text_result(
                f"skip_application rejected — reason must be one of {list(SKIP_REASONS)} and "
                "evidence must quote the sentence that says sponsorship is not offered",
                error=True)
        self.skip_reason, self.skip_evidence = reason, evidence[:500]
        self.outcome, self.stop_reason = "skip", None
        return _text_result("Application skipped (sponsorship not offered). Stop now — do not "
                            "fill or click anything else.")
