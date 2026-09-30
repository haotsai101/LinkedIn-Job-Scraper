"""One offsite application, supervised end to end (OA11).

``run_offsite_job`` drives a single job on the shared browser + guard-mcp:

1. ``offsite_applications`` attempt (PREPARING), page **locked**, application
   URL opened;
2. ``FillController.fill()`` — NIM → Claude, in place (OA10);
3. ``human`` → a terminal pause: the human signs in / registers / solves the
   CAPTCHA in the browser (for login/register the account *email* may be
   recorded — never a password), then ``resume()``;
4. ``skip`` (sponsorship not offered) → ``jobs.applied = -1``, SKIPPED, next;
5. ``ready`` / ``needs_human`` → **review**: a short "check these" list, the
   page is **unlocked**, and the human reviews the live form and submits it
   themselves if it looks right, then answers:

   ``[s]`` I submitted  ``[e]`` fix a field  ``[r]`` not interested
   ``[b]`` blocked  ``[l]`` later

6. DB writes per design §3; any crash → ``jobs.applied = -2``, FAILED.

The terminal I/O goes through ``ask`` / ``say`` so it can be scripted in tests.

CLI (one job, supervised):
    python -m offsite.session --url http://127.0.0.1:8811/multipage.html
    python -m offsite.session --job-id 4463107277 [--db copy.db]
"""
from __future__ import annotations

import argparse
import asyncio
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from offsite import store
from offsite.browser import OffsiteBrowser, host_of
from offsite.controller import FillController, FillResult
from offsite.prompts import JobInfo

Ask = Callable[[str], Awaitable[str]]
Say = Callable[[str], None]

APPLIED, SKIPPED_APPLIED, FAILED_APPLIED, BLOCKED_APPLIED = 1, -1, -2, -3
_CONFIRMATION = re.compile(
    r"thank|received|submitted|application (has been|was) (sent|received|submitted)|"
    r"we('| wi)ll (be in touch|review)|confirmation", re.I)
_KEYS = "[s] I submitted  [e] fix a field  [r] not interested  [b] blocked  [l] later"


class Controller(Protocol):
    async def fill(self) -> FillResult: ...
    async def resume(self, human_reason: str, human_detail: str = "") -> FillResult: ...
    async def fix(self, instruction: str) -> FillResult: ...


class Lock(Protocol):
    async def lock(self) -> None: ...
    async def unlock(self) -> None: ...


@dataclass
class JobOutcome:
    key: str                 # s / r / b / l / skip / failed
    applied: int | None      # jobs.applied written
    status: str              # offsite_applications.status
    attempt_id: int | None
    fill: FillResult | None = None


async def _input(prompt: str) -> str:
    return await asyncio.get_running_loop().run_in_executor(None, input, prompt)


def mark_job(conn: sqlite3.Connection | None, job_id: int | None, status: int | None) -> None:
    """Same semantics as ``apply_jobs.mark_job``: ``applied`` + ``applied_at``."""
    if conn is None or job_id is None:
        return
    conn.execute("UPDATE jobs SET applied = ?, applied_at = ? WHERE job_id = ?",
                 (status, int(time.time()) if status is not None else None, job_id))
    conn.commit()


def check_list(fill: FillResult) -> str:
    """One line: what the human should double-check (sensitive / low confidence)."""
    items = []
    for a in fill.answers:
        if a.sensitive or a.confidence < 0.7:
            why = "" if a.sensitive else f" (confidence {a.confidence:.1f})"
            items.append(f"{a.field_label[:45]}: {a.answer[:40]!r}{why}")
    items += [f"⚠ {w}" for w in fill.warnings]
    return " · ".join(items) if items else "nothing flagged"


async def run_offsite_job(
    job: JobInfo,
    *,
    conn: sqlite3.Connection | None,
    browser: OffsiteBrowser,
    submit_guard: Lock,
    controller: Controller,
    ask: Ask = _input,
    say: Say = print,
    page_text: Callable[[], Awaitable[str]] | None = None,
) -> JobOutcome:
    host = host_of(job.url)
    attempt = store.start_attempt(conn, job.job_id, host) if conn is not None and \
        job.job_id is not None else None

    def update(**kw: Any) -> None:
        if attempt is not None:
            store.update_attempt(conn, attempt, **kw)

    def record(fill: FillResult) -> dict[str, Any]:
        return {"model_used": fill.model_used, "fallback_reason": fill.fallback_reason,
                "tool_calls": fill.tool_calls, "answers": fill.answers}

    async def read_page() -> str:
        if page_text is not None:
            return await page_text()
        try:
            return (await browser.page.title()) + "\n" + await browser.page.inner_text("body")
        except Exception:  # noqa: BLE001
            return ""

    fill: FillResult | None = None
    try:
        await submit_guard.lock()
        await browser.open(job.url)
        say(f"▶ {job.title or 'job'} — {job.company or '?'} ({host})")
        fill = await controller.fill()

        while True:
            if fill.outcome == "human":
                fill = await _human_pause(fill, controller, ask, say, update, host)
                if isinstance(fill, JobOutcome):          # human chose b / l / r
                    fill.attempt_id = attempt
                    mark_job(conn, job.job_id, fill.applied)
                    return fill
                continue

            if fill.outcome == "skip":
                say(f"⏭ skipped — {fill.skip_reason}: “{fill.skip_evidence}”")
                mark_job(conn, job.job_id, SKIPPED_APPLIED)
                update(status=store.SKIPPED, error=fill.skip_evidence, **record(fill))
                return JobOutcome("skip", SKIPPED_APPLIED, store.SKIPPED, attempt, fill)

            # ready / needs_human → review on the live page
            update(status=store.READY_FOR_REVIEW, **record(fill))
            if fill.outcome == "needs_human":
                say(f"✋ The agents could not finish ({fill.fallback_reason}). Complete the "
                    "form yourself in the browser if you want to apply.")
            else:
                say(f"✅ Filled by {fill.model_used} ({fill.tool_calls} tool calls).")
            say(f"⚠ Check: {check_list(fill)}")
            say("Review the page in the browser. Submit it yourself if it looks right.")
            await submit_guard.unlock()
            key = await _ask_key(ask, _KEYS, "serbl")

            if key == "e":
                instruction = (await ask("Which field, and what change? ")).strip()
                await submit_guard.lock()
                if instruction:
                    fill = await controller.fix(instruction)
                continue

            await submit_guard.lock()
            if key == "s":
                text = await read_page()
                if not _CONFIRMATION.search(text):
                    sure = await _ask_key(
                        ask, "No confirmation message found on the page. Did you really "
                             "submit it? [y] yes  [n] no, back to review", "yn")
                    if sure == "n":
                        continue
                confirmation = re.sub(r"\s+", " ", text).strip()[:500]
                mark_job(conn, job.job_id, APPLIED)
                update(status=store.SUBMITTED, confirmation=confirmation,
                       submitted_at=int(time.time()))
                say("📨 Recorded as applied.")
                return JobOutcome("s", APPLIED, store.SUBMITTED, attempt, fill)
            out = _REVIEW_OUTCOMES[key]
            mark_job(conn, job.job_id, out[0])
            update(status=out[1])
            say(out[2])
            return JobOutcome(key, out[0], out[1], attempt, fill)

    except Exception as e:  # noqa: BLE001 - one job must never take the session down
        mark_job(conn, job.job_id, FAILED_APPLIED)
        update(status=store.FAILED, error=f"{type(e).__name__}: {str(e)[:300]}",
               **(record(fill) if fill else {}))
        say(f"💥 {type(e).__name__}: {e} — recorded as failed (retry with --reset-failed)")
        return JobOutcome("failed", FAILED_APPLIED, store.FAILED, attempt, fill)
    finally:
        try:
            await submit_guard.lock()
        except Exception:  # noqa: BLE001
            pass


_REVIEW_OUTCOMES = {
    "r": (SKIPPED_APPLIED, store.REJECTED, "👋 Not interested — skipped."),
    "b": (BLOCKED_APPLIED, store.BLOCKED, "⛔ Blocked — needs a human another time."),
    "l": (None, store.DEFERRED, "⏳ Later — left pending."),
}


async def _ask_key(ask: Ask, prompt: str, allowed: str, default: str | None = None) -> str:
    while True:
        key = (await ask(prompt + " > ")).strip().lower()[:1] or (default or "")
        if key and key in allowed:
            return key


async def _human_pause(fill: FillResult, controller: Controller, ask: Ask, say: Say,
                       update: Callable[..., None], host: str) -> FillResult | JobOutcome:
    say(f"⏸ {fill.human_reason} on {host}: {fill.human_detail}")
    if fill.human_reason in ("login", "register"):
        email = (await ask("Account email you use on this site (Enter to skip; never a "
                           "password): ")).strip()
        if email:
            update(account_email=email[:200], account_host=host)
    key = await _ask_key(ask, "Do it in the browser, then: [Enter/c] continue  "
                              "[b] blocked  [l] later  [r] not interested", "cblr",
                        default="c")
    if key == "c":
        return await controller.resume(fill.human_reason or "stuck", fill.human_detail or "")
    out = _REVIEW_OUTCOMES[key]
    update(status=out[1])
    say(out[2])
    return JobOutcome(key, out[0], out[1], None, fill)


# ── CLI ────────────────────────────────────────────────────────────────────────

async def _cli(args: argparse.Namespace) -> int:
    from offsite.control import RunControl
    from offsite.guard_mcp import GuardMCP
    from offsite.guards import SubmitGuard
    from offsite.prompts import load_job, load_profile, needs_sponsorship

    profile = load_profile()
    conn = None
    if args.job_id:
        db = args.db
        if not db:
            db = str(Path(tempfile.mkdtemp(prefix="offsite-session-")) / "linkedin_jobs.db")
            shutil.copy(_default_db(), db)
            print(f"(writing to a copy of the DB: {db} — pass --db to write elsewhere)")
        conn = sqlite3.connect(db)
        from scripts.create_db import ensure_db_ready
        ensure_db_ready(conn, conn.cursor())
        job = load_job(conn, args.job_id)
        if args.url:
            job = JobInfo(job.job_id, job.title, job.company, args.url, job.description,
                          job.location)
    else:
        job = JobInfo(None, "Software Engineer", "Test Company", args.url,
                      "Placeholder job for a fixture run.", "Remote")

    kw = {"headless": args.headless}
    browser = OffsiteBrowser(args.profile_dir, **kw) if args.profile_dir else OffsiteBrowser(**kw)
    async with browser, GuardMCP(browser) as guard:
        control = RunControl(sponsorship_skip=needs_sponsorship(profile))
        control.install(guard)
        sg = SubmitGuard(browser, resume_path=profile.get("resume_path"))
        await sg.install(guard)
        ctl = FillController(profile=profile, job=job, guard_url=guard.url, control=control,
                             order=("nim", "claude") if args.model == "auto" else (args.model,))
        out = await run_offsite_job(job, conn=conn, browser=browser, submit_guard=sg,
                                    controller=ctl)
    print(f"result: {out.key} → jobs.applied={out.applied}, status={out.status}"
          + (f", attempt #{out.attempt_id}" if out.attempt_id else ""))
    return 0


def _default_db() -> Path:
    return Path(__file__).resolve().parents[1] / "linkedin_jobs.db"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="python -m offsite.session", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", help="application URL (optional with --job-id)")
    ap.add_argument("--job-id", type=int)
    ap.add_argument("--db", help="DB to write to (default with --job-id: a temp copy)")
    ap.add_argument("--model", choices=["auto", "nim", "claude"], default="auto")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--profile-dir", default=None)
    args = ap.parse_args(argv)
    if not args.url and not args.job_id:
        ap.error("give --url or --job-id")
    return asyncio.run(_cli(args))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
