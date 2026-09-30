"""NIM → Claude fallback on one application, continuing in place (OA10).

``FillController`` runs the model chain (default ``nim`` then ``claude``) on the
page that is already open, through the same guard-mcp:

* a run that ends with a control outcome — ``ready`` (report_ready), ``skip``
  (skip_application) or ``human`` (request_human) — ends the chain;
* anything else is a **fallback trigger** (design §5): the runner failed
  (timeout after retries, HTTP error / 429, invalid tool calls, turn limit, run
  cap), ``RunControl`` stopped it (``loop`` / ``budget``), or the model just
  ended its turn without a control tool. The next model gets a
  ``handoff_note`` and continues on the same page — fields already filled stay
  filled;
* the last model failing too → ``needs_human`` (the orchestrator hands the
  job to the human in the open browser).

``human`` is a pause, not an end: after the human acts, ``resume()`` re-runs
the model that asked (with the rest of the chain behind it). ``fix()`` runs
the chain again with the human's change request (OA11 ``[e]``).

``force_fallback_after`` (testing): the first model's budget is cut to N calls.
"""
from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from common import write_llm_log
from offsite.control import RunControl
from offsite.prompts import (
    JobInfo,
    fix_note,
    handoff_note,
    resume_note,
    start_message,
    system_prompt,
)
from offsite.runners import RunResult
from offsite.schemas import GeneratedAnswer

Runner = Callable[..., Awaitable[RunResult]]
FillOutcome = Literal["ready", "human", "skip", "needs_human"]
_TERMINAL = ("ready", "human", "skip")


def default_runners() -> dict[str, Runner]:
    from offsite.runners import claude, nim
    return {"nim": nim.run, "claude": claude.run}


@dataclass
class FillResult:
    outcome: FillOutcome
    model_used: str                       # "nim", "claude", "nim→claude", …
    fallback_reason: str | None = None    # why each earlier model was abandoned
    tool_calls: int = 0                   # across every run on this application
    answers: list[GeneratedAnswer] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    human_reason: str | None = None
    human_detail: str | None = None
    skip_reason: str | None = None
    skip_evidence: str | None = None
    runs: list[RunResult] = field(default_factory=list)


class FillController:
    def __init__(
        self,
        *,
        profile: dict[str, Any],
        job: JobInfo,
        guard_url: str,
        control: RunControl,
        runners: dict[str, Runner] | None = None,
        order: Sequence[str] = ("nim", "claude"),
        force_fallback_after: int | None = None,
    ) -> None:
        self.job, self.guard_url, self.control = job, guard_url, control
        self.system = system_prompt(profile, job)
        self.runners = runners or default_runners()
        self.order = list(order)
        self.force_fallback_after = force_fallback_after
        self._budget = control.budget
        self._current = 0                 # index into order of the model now in charge
        self._used: list[str] = []        # models that ran, in order (for model_used)
        self._reasons: list[str] = []     # fallback reasons, in order
        self._human_actions: list[str] = []
        self._forced = False
        self.tool_calls = 0
        self.runs: list[RunResult] = []

    # ── public ────────────────────────────────────────────────────────────────
    async def fill(self) -> FillResult:
        """First pass on the job, starting with the first model."""
        self._current = 0
        return await self._chain(lambda model, reason: (
            start_message(self.job) if reason is None else self._handoff(reason)))

    async def resume(self, human_reason: str, human_detail: str = "") -> FillResult:
        """After the human handled a request_human pause: same model, then the chain."""
        self._human_actions.append({"login": "signed in", "register": "created the account",
                                    "captcha": "solved the CAPTCHA"}.get(human_reason,
                                                                         "handled a blocker"))
        return await self._chain(lambda model, reason: (
            resume_note(self.job, human_reason, human_detail) if reason is None
            else self._handoff(reason)))

    async def fix(self, instruction: str) -> FillResult:
        """The human asked for a change at review: first model again, same chain rules."""
        self._current = 0
        return await self._chain(lambda model, reason: (
            fix_note(self.job, instruction) if reason is None
            else self._handoff(reason) + f"\nThe human also asked for: {instruction.strip()}"))

    # ── chain ─────────────────────────────────────────────────────────────────
    def _handoff(self, reason: str) -> str:
        prior = self._used[-1] if self._used else "another model"
        return handoff_note(self.job, reason=reason, prior_model=prior,
                            human_actions=self._human_actions)

    async def _chain(self, task_for: Callable[[str, str | None], str]) -> FillResult:
        reason: str | None = None
        for idx in range(self._current, len(self.order)):
            model = self.order[idx]
            self._current = idx
            self.control.reset(model=model)
            # testing aid: cut only the very first run's budget, once
            cut = bool(self.force_fallback_after) and not self._forced and idx == 0
            self._forced = self._forced or cut
            self.control.budget = self.force_fallback_after if cut else self._budget
            res = await self._run(model, task_for(model, reason))
            self.control.budget = self._budget
            self.runs.append(res)
            self.tool_calls += self.control.calls
            if not self._used or self._used[-1] != model:
                self._used.append(model)
            outcome = self.control.outcome
            self._log(model, res, outcome)
            if outcome in _TERMINAL:
                return self._result(outcome)
            reason = self._trigger(res)
            self._reasons.append(f"{model}: {reason}")
        return self._result("needs_human")

    async def _run(self, model: str, task: str) -> RunResult:
        try:
            return await self.runners[model](
                self.system, task, self.guard_url,
                stop_when=lambda: self.control.outcome is not None)
        except Exception as e:  # noqa: BLE001 - e.g. missing NIM key → fall back
            return RunResult("error", model, f"{type(e).__name__}: {str(e)[:200]}")

    def _trigger(self, res: RunResult) -> str:
        if self.control.outcome in ("loop", "budget"):
            return f"{self.control.outcome} — {self.control.stop_reason}"
        if res.status == "error":
            return res.error or "runner error"
        return "ended its turn without calling report_ready"

    def _result(self, outcome: FillOutcome) -> FillResult:
        c = self.control
        return FillResult(
            outcome=outcome,
            model_used="→".join(self._used),
            fallback_reason="; ".join(self._reasons) or None,
            tool_calls=self.tool_calls,
            answers=list(c.answers) if outcome == "ready" else [],
            warnings=list(c.warnings) if outcome == "ready" else [],
            human_reason=c.human_reason if outcome == "human" else None,
            human_detail=c.human_detail if outcome == "human" else None,
            skip_reason=c.skip_reason if outcome == "skip" else None,
            skip_evidence=c.skip_evidence if outcome == "skip" else None,
            runs=list(self.runs),
        )

    def _log(self, model: str, res: RunResult, outcome: str | None) -> None:
        write_llm_log({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "source": "fill_controller",
                       "job_id": self.job.job_id, "model": model, "status": res.status,
                       "error": res.error, "outcome": outcome, "calls": self.control.calls,
                       "stop_reason": self.control.stop_reason, "seconds": res.seconds})
