"""Run one model on one application page, supervised (OA8 / OA9 manual check).

    python -m offsite.run_agent --model nim --url <application url> [--job-id N]
    python -m offsite.run_agent --model nim --url http://127.0.0.1:8811/multipage.html

Starts the shared browser + guard-mcp with every guard (RunControl budget/loop,
SubmitGuard lock / Enter / uploads), opens the URL, runs the model with the
OA7 prompts, then prints how it ended and the answers it reported. Nothing is
ever submitted; the browser stays open for you to look until you press Enter
(``--no-wait`` to skip). ``--job-id`` loads the job text from the DB; otherwise
a placeholder job is used (fine for fixtures).
"""
from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys

from offsite.browser import OffsiteBrowser
from offsite.control import BUDGET, RunControl
from offsite.guard_mcp import GuardMCP
from offsite.guards import SubmitGuard
from offsite.prompts import (
    DEFAULT_DB,
    JobInfo,
    load_job,
    load_profile,
    needs_sponsorship,
    start_message,
    system_prompt,
)
from offsite.runners import RunResult


async def run_one(model: str, url: str, *, job: JobInfo, profile: dict, headless: bool,
                  budget: int, wait: bool, profile_dir: str | None) -> tuple[RunResult, RunControl]:
    kw = {"headless": headless}
    browser = OffsiteBrowser(profile_dir, **kw) if profile_dir else OffsiteBrowser(**kw)
    async with browser, GuardMCP(browser) as guard:
        control = RunControl(budget=budget, sponsorship_skip=needs_sponsorship(profile))
        control.install(guard)
        await SubmitGuard(browser, resume_path=profile.get("resume_path")).install(guard)
        await browser.open(url)
        control.reset(model=model)
        system, task = system_prompt(profile, job), start_message(job)
        print(f"[run_agent] {model} on {job.host or url} — budget {budget}, guard {guard.url}")
        if model == "nim":
            from offsite.runners import nim
            res = await nim.run(system, task, guard.url, stop_when=lambda: bool(control.outcome))
        elif model == "claude":
            from offsite.runners import claude  # OA9
            res = await claude.run(system, task, guard.url,
                                   stop_when=lambda: bool(control.outcome))
        else:
            raise SystemExit(f"unknown model {model!r}")
        _report(res, control)
        if wait:
            await asyncio.get_running_loop().run_in_executor(
                None, input, "Browser left open for you to inspect — press Enter to close. ")
        return res, control


def _report(res: RunResult, control: RunControl) -> None:
    print(f"run      : {res.status}"
          + (f" — {res.error}" if res.error else "")
          + f" ({res.model}, {res.turns} turns, {res.seconds}s)")
    print(f"outcome  : {control.outcome} ({control.calls} tool calls"
          + (f", {control.stop_reason}" if control.stop_reason else "") + ")")
    if control.outcome == "human":
        print(f"human    : {control.human_reason} — {control.human_detail}")
    if control.outcome == "skip":
        print(f"skipped  : {control.skip_reason} — “{control.skip_evidence}”")
    for w in control.warnings:
        print(f"warning  : {w}")
    for a in control.answers:
        flag = "⚠" if a.sensitive or a.confidence < 0.7 else " "
        print(f"  {flag} {a.field_label[:60]}: {a.answer[:80]!r} ({a.source}, {a.confidence:.2f})")
    if res.final_text:
        print(f"final    : {res.final_text[:300]}")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="python -m offsite.run_agent", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", choices=["nim", "claude"], required=True)
    ap.add_argument("--url", required=True)
    ap.add_argument("--job-id", type=int)
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--budget", type=int, default=BUDGET)
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--no-wait", action="store_true")
    ap.add_argument("--profile-dir", default=None, help="browser profile dir")
    args = ap.parse_args(argv)

    profile = load_profile()
    if args.job_id:
        conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
        job = load_job(conn, args.job_id)
        conn.close()
        job = JobInfo(job.job_id, job.title, job.company, args.url, job.description, job.location)
    else:
        job = JobInfo(None, "Software Engineer", "Test Company", args.url,
                      "Placeholder job for a fixture run.", "Remote")
    res, control = asyncio.run(run_one(
        args.model, args.url, job=job, profile=profile, headless=args.headless,
        budget=args.budget, wait=not args.no_wait, profile_dir=args.profile_dir))
    return 0 if control.outcome == "ready" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
