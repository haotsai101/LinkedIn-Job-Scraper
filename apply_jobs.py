#!/usr/bin/env python3
"""
apply_jobs.py - Fully autonomous AI job application agent.

Loads your profile once, classifies each pending job with an LLM, and uses
deterministic Playwright flows to fill and submit applications without any
human intervention (--auto mode). A session summary is written to
application_log.json and emailed via Gmail when the run finishes.

Environment variables (put in a .env file or export before running):
    LLM_API             - API key for the LLM endpoint
    LLM_URL             - Base URL of the OpenAI-compatible API
    LLM_MODEL           - Model name (default: gpt-4o-mini)
    GMAIL_USER          - Gmail address to send summary emails from/to
    GMAIL_APP_PASSWORD  - 16-char Google App Password (needs 2FA enabled)
    MAX_AUTO_APPLY      - Default daily cap (default: 10)

    # Optional: separate model/endpoint just for the browser agent
    BROWSER_LLM_API     - API key (defaults to LLM_API)
    BROWSER_LLM_URL     - Base URL (defaults to LLM_URL)
    BROWSER_LLM_MODEL   - Model name (defaults to LLM_MODEL)

Usage:
    python apply_jobs.py                      # Semi-auto: confirm before each submit
    python apply_jobs.py --auto               # Fully autonomous: no confirmation
    python apply_jobs.py --auto --max-apply 5 # Cap this session at 5 applications
    python apply_jobs.py --limit 20           # Cap jobs reviewed this session to 20
    python apply_jobs.py --stats              # Print counts and exit
    python apply_jobs.py --setup              # Re-run profile interview
    python apply_jobs.py --accounts QUERY     # Search saved career-site accounts
"""

import argparse
import asyncio
import csv
import json
import os
import secrets
import smtplib
import sqlite3
import string
import sys
import time
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from openai import OpenAI
from playwright.async_api import async_playwright

from linkedin_apply import EasyApplyFlow, OffsiteApplyFlow

# ── Config ─────────────────────────────────────────────────────────────────────

DB_PATH      = "linkedin_jobs.db"
PROFILE_PATH = "user_profile.json"
LOG_PATH     = "application_log.json"
ACCOUNTS_PATH = "created_accounts.json"

PROFILE_QUESTIONS = [
    ("full_name",          "Full name"),
    ("email",              "Email address"),
    ("phone",              "Phone number"),
    ("location",           "City / state you are based in"),
    ("linkedin_url",       "LinkedIn profile URL (Enter to skip)"),
    ("github_url",         "GitHub URL (Enter to skip)"),
    ("portfolio_url",      "Personal website / portfolio (Enter to skip)"),
    ("current_title",      "Current or most recent job title"),
    ("years_experience",   "Years of professional experience"),
    ("skills",             "Top skills, comma-separated (e.g. Python, SQL, ML)"),
    ("education",          "Highest degree, field, school, year (e.g. B.S. CS, MIT, 2021)"),
    ("work_authorization", "Work authorization (e.g. US Citizen, H1B, OPT)"),
    ("willing_to_relocate","Willing to relocate? (yes / no)"),
    ("preferred_salary",   "Preferred salary or range (Enter to skip)"),
    ("summary",            "2-3 sentence professional summary about yourself"),
]


# ── Env / config loading ────────────────────────────────────────────────────────

def load_env():
    env_file = Path(".env")
    if env_file.exists():
        for raw in env_file.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())

    api_key      = os.environ.get("LLM_API", "").strip()
    base_url     = os.environ.get("LLM_URL", "").strip()
    model        = os.environ.get("LLM_MODEL", "gpt-4o-mini").strip()
    gmail_user   = os.environ.get("GMAIL_USER", "").strip()
    gmail_pass   = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    max_auto_env = int(os.environ.get("MAX_AUTO_APPLY", "10"))

    browser_api_key = os.environ.get("BROWSER_LLM_API", api_key).strip()
    browser_url     = os.environ.get("BROWSER_LLM_URL", base_url).strip()
    browser_model   = os.environ.get("BROWSER_LLM_MODEL", model).strip()

    if not api_key or not base_url:
        sys.exit("Error: LLM_API and LLM_URL must be set (in .env or environment).")

    return api_key, base_url, model, gmail_user, gmail_pass, max_auto_env, browser_api_key, browser_url, browser_model


# ── User profile ────────────────────────────────────────────────────────────────

def load_profile():
    p = Path(PROFILE_PATH)
    if p.exists():
        return json.loads(p.read_text())
    return None


def save_profile(profile: dict):
    Path(PROFILE_PATH).write_text(json.dumps(profile, indent=2))
    print(f"Profile saved to {PROFILE_PATH}")


def build_profile_interactively(client: OpenAI, model: str) -> dict:
    print("\n── Profile Setup ─────────────────────────────────────────────────────")
    print("Answer the questions below. Your profile is stored locally and used")
    print("only to fill application forms on your behalf.\n")

    raw_answers: dict[str, str] = {}
    for key, prompt in PROFILE_QUESTIONS:
        raw_answers[key] = input(f"  {prompt}:\n  > ").strip()

    print("\n  Structuring profile with LLM…", end="", flush=True)

    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a career assistant. Given raw user answers, return a clean JSON "
                        "profile. Rules: parse 'skills' into a list; convert 'willing_to_relocate' "
                        "to boolean; parse 'education' into {degree, field, school, year}; "
                        "years_experience as integer; all other fields as strings. Return ONLY JSON."
                    ),
                },
                {"role": "user", "content": json.dumps(raw_answers)},
            ],
            response_format={"type": "json_object"},
        )
        profile = json.loads(resp.choices[0].message.content)
    except Exception:
        profile = raw_answers

    print(" done.")
    save_profile(profile)
    return profile


# ── Session log & email notification ───────────────────────────────────────────

def write_session_log(report: dict):
    log_path = Path(LOG_PATH)
    if log_path.exists():
        try:
            existing = json.loads(log_path.read_text())
        except Exception:
            existing = {"sessions": []}
    else:
        existing = {"sessions": []}

    existing["sessions"].append(report)
    log_path.write_text(json.dumps(existing, indent=2))
    print(f"  Session log written to {LOG_PATH}")


def send_session_email(gmail_user: str, app_password: str, report: dict):
    if not gmail_user or not app_password:
        print("  (Gmail credentials not set — skipping email notification)")
        return

    applied = report.get("applied_count", 0)
    skipped = report.get("skipped_count", 0)
    errors  = report.get("error_count", 0)
    date    = report.get("date", "")
    apps    = report.get("applications", [])

    subject = f"Job Applications Summary — {date} ({applied} applied)"

    rows = "".join(
        f"<tr>"
        f"<td style='padding:4px 8px'>{a.get('title', '')}</td>"
        f"<td style='padding:4px 8px'>{a.get('company', '')}</td>"
        f"<td style='padding:4px 8px'><a href='{a.get('url', '')}'>View</a></td>"
        f"</tr>"
        for a in apps
    )

    table_html = (
        "<table border='1' cellspacing='0' style='border-collapse:collapse'>"
        "<tr style='background:#f2f2f2'>"
        "<th style='padding:4px 8px'>Title</th>"
        "<th style='padding:4px 8px'>Company</th>"
        "<th style='padding:4px 8px'>Link</th>"
        "</tr>"
        f"{rows}"
        "</table>"
    ) if apps else "<p><em>No applications submitted this session.</em></p>"

    html = f"""
<html><body style='font-family:sans-serif;color:#333'>
<h2>Job Application Summary — {date}</h2>
<p>
  <b>Applied:</b> {applied} &nbsp;|&nbsp;
  <b>Skipped:</b> {skipped} &nbsp;|&nbsp;
  <b>Errors:</b> {errors}
</p>
<h3>Applications submitted:</h3>
{table_html}
<hr>
<p style='color:#888;font-size:12px'>Sent by linkedin-job-scraper apply_jobs.py</p>
</body></html>
"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = gmail_user
    msg["To"]      = gmail_user
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(gmail_user, app_password)
            server.send_message(msg)
        print(f"  Email summary sent to {gmail_user}")
    except Exception as exc:
        print(f"  [!] Failed to send email: {exc}")


# ── Career-site account management ─────────────────────────────────────────────

def _generate_password(length: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    pwd = [
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.digits),
        secrets.choice("!@#$%^&*"),
    ]
    pwd += [secrets.choice(alphabet) for _ in range(length - 4)]
    secrets.SystemRandom().shuffle(pwd)
    return "".join(pwd)


def save_account_to_file(record: dict):
    path = Path(ACCOUNTS_PATH)
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except Exception:
            data = {"accounts": []}
    else:
        data = {"accounts": []}
    data["accounts"].append(record)
    path.write_text(json.dumps(data, indent=2))


def search_accounts(query: str) -> list[dict]:
    path = Path(ACCOUNTS_PATH)
    if not path.exists():
        return []
    data = json.loads(path.read_text())
    q = query.lower()
    return [
        a for a in data.get("accounts", [])
        if q in a.get("company", "").lower() or q in a.get("website_url", "").lower()
    ]


# ── LLM classifier ─────────────────────────────────────────────────────────────

class JobAgent:
    _SYSTEM = """You are a job application assistant helping the user review LinkedIn job listings.

User profile:
{profile}

Classify whether a job is relevant (software engineering, AI/ML, data engineering/science/analytics).
Be accurate and concise. Never fabricate information not in the user's profile."""

    def __init__(self, client: OpenAI, model: str, profile: dict):
        self.client  = client
        self.model   = model
        self._system = self._SYSTEM.format(profile=json.dumps(profile, indent=2))

    def classify(self, title: str, description: str) -> tuple[bool, str]:
        prompt = (
            "Is this job posting related to software engineering, AI/ML, or data "
            "(engineering / science / analytics)?\n\n"
            f"Title: {title}\n\n"
            f"Description excerpt:\n{(description or '')[:2000]}\n\n"
            'Respond with JSON: {"relevant": true|false, "reason": "<one sentence>"}'
        )
        try:
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": self._system},
                    {"role": "user", "content": prompt},
                ],
                response_format={"type": "json_object"},
            )
            data = json.loads(resp.choices[0].message.content)
            relevant = bool(data.get("relevant"))
            reason   = data.get("reason", "")
            if relevant and ("not relevant" in reason.lower() or "not related" in reason.lower()):
                relevant = False
            return relevant, reason
        except Exception as exc:
            return True, f"Classification failed ({exc}); opening anyway."


# ── Database helpers ────────────────────────────────────────────────────────────

def migrate_db(conn, cursor):
    cursor.execute("PRAGMA table_info(jobs)")
    cols = {row[1] for row in cursor.fetchall()}
    if "applied" not in cols:
        cursor.execute("ALTER TABLE jobs ADD COLUMN applied INTEGER DEFAULT NULL")
        conn.commit()
        print("DB migrated: added 'applied' column.")


def get_pending_jobs(cursor, limit=None):
    query = """
        SELECT j.job_id, j.title, j.job_posting_url, j.location,
               j.formatted_experience_level, j.description,
               COALESCE(c.name, '') AS company_name,
               j.application_type
        FROM jobs j
        LEFT JOIN companies c ON j.company_id = c.company_id
        WHERE j.scraped > 0
          AND j.applied IS NULL
          AND (
              j.remote_allowed = 1
              OR LOWER(j.location) LIKE '%remote%'
              OR LOWER(j.location) LIKE '%utah%'
              OR LOWER(j.location) LIKE '%, ut%'
          )
        ORDER BY j.scraped DESC
    """
    if limit:
        query += f" LIMIT {int(limit)}"
    cursor.execute(query)
    return cursor.fetchall()


def mark_job(conn, cursor, job_id: int, status: int):
    """status: 1=applied, -1=skipped, -2=auto-failed."""
    cursor.execute("UPDATE jobs SET applied = ? WHERE job_id = ?", (status, job_id))
    conn.commit()


def print_stats(cursor):
    cursor.execute("""
        SELECT
            SUM(CASE WHEN applied IS NULL AND scraped > 0 THEN 1 ELSE 0 END),
            SUM(CASE WHEN applied =  1 THEN 1 ELSE 0 END),
            SUM(CASE WHEN applied = -1 THEN 1 ELSE 0 END),
            SUM(CASE WHEN applied = -2 THEN 1 ELSE 0 END)
        FROM jobs
    """)
    pending, applied, skipped, failed = cursor.fetchone()
    print(
        f"\nStats — Pending: {pending or 0}  Applied: {applied or 0}  "
        f"Skipped: {skipped or 0}  Auto-failed: {failed or 0}"
    )


# ── Browser & LinkedIn login ────────────────────────────────────────────────────

_LOGIN_URL    = "https://www.linkedin.com/checkpoint/rm/sign-in-another-account"
_AUTH_HOSTPATHS = ("/login", "/checkpoint", "/uas/")


def _get_login_credentials() -> tuple[str, str]:
    logins_path = Path("logins.csv")
    if not logins_path.exists():
        sys.exit("logins.csv not found. Copy logins.csv.template and add your credentials.")

    rows: list[dict] = []
    with logins_path.open(newline="") as fh:
        rows = list(csv.DictReader(fh))

    for method in ("apply", "search"):
        for row in rows:
            if row.get("method", "").strip() == method:
                return row["emails"].strip(), row["passwords"].strip()

    if rows:
        return rows[0]["emails"].strip(), rows[0]["passwords"].strip()

    sys.exit("No credentials found in logins.csv.")


async def login_linkedin_playwright(page) -> None:
    email, password = _get_login_credentials()
    print(f"  Signing into LinkedIn as {email} (Playwright)…", end="", flush=True)

    try:
        await page.goto(_LOGIN_URL, wait_until="domcontentloaded", timeout=20000)
    except Exception:
        pass
    await asyncio.sleep(1.5)

    try:
        await page.fill("#username", email)
        await page.fill("#password", password)
        await page.click('button.btn__primary--large[type="submit"]')
    except Exception as exc:
        print(f"\n  [!] Could not interact with login form: {exc}")
        input("  Complete login manually in the browser, then press ENTER…")
        return

    for _ in range(20):
        await asyncio.sleep(1)
        if "linkedin.com" in page.url and not any(p in page.url for p in _AUTH_HOSTPATHS):
            print(" done.")
            return

    print("\n  LinkedIn requires additional verification (CAPTCHA / 2-FA).")
    input("  Complete it in the browser, then press ENTER to continue…")


# ── Main session ───────────────────────────────────────────────────────────────

async def run_session(
    jobs,
    total,
    profile,
    client,
    model,
    api_key,
    base_url,
    conn,
    cursor,
    *,
    auto_mode: bool = False,
    max_apply: int = 10,
    gmail_user: str = "",
    gmail_pass: str = "",
    browser_api_key: str = "",
    browser_url: str = "",
    browser_model: str = "",
):
    agent = JobAgent(client, model, profile)

    started_at   = datetime.now(timezone.utc).isoformat()
    session_date = datetime.now().strftime("%Y-%m-%d")
    applications: list[dict] = []
    applied_count = skipped_count = error_count = 0

    browser_llm_client = OpenAI(
        api_key=browser_api_key or api_key,
        base_url=browser_url or base_url,
    )
    browser_llm_model = browser_model or model

    print("\nOpening browser and signing into LinkedIn…")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page    = await context.new_page()

        await login_linkedin_playwright(page)
        print("  Session ready.\n")

        try:
            for idx, row in enumerate(jobs, 1):
                job_id, title, job_url, location, exp_level, description, company_name, application_type = row
                url = job_url or f"https://www.linkedin.com/jobs/view/{job_id}/"

                print(f"\n{'─' * 64}")
                print(f"  [{idx}/{total}]  {title or 'Unknown'}  |  {company_name or 'Unknown company'}")
                print(f"  Location : {location or 'N/A'}   Level: {exp_level or 'N/A'}")

                print("  Classifying…", end="", flush=True)
                relevant, reason = agent.classify(title or "", description or "")
                tag = "✓ relevant" if relevant else "✗ not relevant"
                print(f" {tag} — {reason}")

                if not relevant:
                    mark_job(conn, cursor, job_id, -1)
                    skipped_count += 1
                    print("  Auto-skipped.")
                    continue

                if auto_mode and applied_count >= max_apply:
                    print(f"  [cap] Reached max-apply limit ({max_apply}). Stopping applications.")
                    break

                # ── Build callbacks ────────────────────────────────────────────
                outcome: dict[str, str] = {"status": "pending"}

                def _make_ready_to_submit(outcome_ref, job_title_ref):
                    def ready_to_submit(summary: str) -> str:
                        if auto_mode:
                            print(f"\n  Auto-submitting: {job_title_ref}")
                            print(f"  {summary}")
                            outcome_ref["status"] = "applied"
                            return "applied"
                        print(f"\n{'═' * 64}")
                        print(f"  Application ready: {job_title_ref}")
                        print(f"\n  {summary}")
                        print(f"{'═' * 64}")
                        try:
                            choice = input(
                                "  Review the form in the browser.\n"
                                "  [ENTER] = submit   [s] = skip\n"
                                "  > "
                            ).strip().lower()
                        except (EOFError, KeyboardInterrupt):
                            choice = "s"
                        if choice in ("s", "skip"):
                            outcome_ref["status"] = "skipped"
                            return "skipped"
                        outcome_ref["status"] = "applied"
                        return "applied"
                    return ready_to_submit

                callbacks = {
                    "ready_to_submit":  _make_ready_to_submit(outcome, title or "Unknown"),
                    "save_account":     save_account_to_file,
                    "get_credentials":  _get_login_credentials,
                }

                # ── Choose apply flow ──────────────────────────────────────────
                pages_before = set(context.pages)
                print("  Applying via Playwright…")

                if (application_type or "") == "OffsiteApply":
                    flow = OffsiteApplyFlow(
                        page=page,
                        context=context,
                        profile=profile,
                        llm_client=browser_llm_client,
                        model=browser_llm_model,
                        auto_mode=auto_mode,
                        callbacks=callbacks,
                        generated_password=_generate_password(),
                        company_name=company_name or "",
                        job_title=title or "",
                    )
                else:
                    flow = EasyApplyFlow(
                        page=page,
                        profile=profile,
                        llm_client=browser_llm_client,
                        model=browser_llm_model,
                        auto_mode=auto_mode,
                        callbacks=callbacks,
                    )

                try:
                    status = await asyncio.wait_for(flow.run(url), timeout=300)
                except asyncio.TimeoutError:
                    print(f"\n  [!] Timed out after 300s")
                    status = "failed"
                except Exception as exc:
                    print(f"\n  [!] Error during apply: {exc}")
                    status = "failed"

                # Close any new tabs opened during apply
                for pg in list(context.pages):
                    if pg not in pages_before and not pg.is_closed():
                        try:
                            await pg.close()
                        except Exception:
                            pass

                # Navigate back to blank for next job
                try:
                    if not page.is_closed():
                        await page.goto("about:blank")
                except Exception:
                    pass
                await asyncio.sleep(2)

                # ── Record outcome ─────────────────────────────────────────────
                if status == "applied":
                    mark_job(conn, cursor, job_id, 1)
                    applied_count += 1
                    applications.append({
                        "job_id":     job_id,
                        "title":      title or "",
                        "company":    company_name or "",
                        "url":        url,
                        "applied_at": datetime.now(timezone.utc).isoformat(),
                    })
                    print("  [+] Applied!")
                    await asyncio.sleep(10)

                elif status == "already_applied":
                    mark_job(conn, cursor, job_id, 1)
                    applied_count += 1
                    print("  [~] Already applied — marked as applied.")

                elif status == "expired":
                    mark_job(conn, cursor, job_id, -1)
                    skipped_count += 1
                    print("  [~] No longer accepting — skipped.")

                elif status == "skipped":
                    mark_job(conn, cursor, job_id, -1)
                    skipped_count += 1
                    print("  [-] Skipped.")

                else:  # "failed"
                    mark_job(conn, cursor, job_id, -2)
                    error_count += 1
                    print("  [!] Auto-apply failed — marked as auto-failed.")

        finally:
            conn.close()

    # ── Session wrap-up ────────────────────────────────────────────────────────
    completed_at = datetime.now(timezone.utc).isoformat()
    report = {
        "date":          session_date,
        "started_at":    started_at,
        "completed_at":  completed_at,
        "applied_count": applied_count,
        "skipped_count": skipped_count,
        "error_count":   error_count,
        "auto_mode":     auto_mode,
        "applications":  applications,
    }

    print(f"\n{'═' * 64}")
    print(f"Session complete — Applied: {applied_count}  Skipped: {skipped_count}  Errors: {error_count}")

    write_session_log(report)
    send_session_email(gmail_user, gmail_pass, report)


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="AI-assisted LinkedIn job application agent.")
    parser.add_argument("--auto",      action="store_true", help="Fully autonomous: submit without confirmation.")
    parser.add_argument("--max-apply", type=int, default=None, help="Max applications to submit this session.")
    parser.add_argument("--limit",     type=int, default=None, help="Max jobs to review this session.")
    parser.add_argument("--stats",     action="store_true",   help="Print stats and exit.")
    parser.add_argument("--setup",     action="store_true",   help="Re-run profile setup interview.")
    parser.add_argument("--accounts",  metavar="QUERY",       help="Search saved career-site accounts and exit.")
    args = parser.parse_args()

    api_key, base_url, model, gmail_user, gmail_pass, max_auto_env, browser_api_key, browser_url, browser_model = load_env()
    client = OpenAI(api_key=api_key, base_url=base_url)

    conn   = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    migrate_db(conn, cursor)

    if args.stats:
        print_stats(cursor)
        conn.close()
        return

    if args.accounts:
        results = search_accounts(args.accounts)
        if not results:
            print(f"No accounts found matching '{args.accounts}'.")
        else:
            for r in results:
                print(f"\n  Company  : {r['company']}")
                print(f"  Site     : {r['website_url']}")
                print(f"  Email    : {r['email']}")
                print(f"  Password : {r['password']}")
                print(f"  Job      : {r['job_title']}")
                print(f"  Created  : {r['created_at']}")
                if r.get("notes"):
                    print(f"  Notes    : {r['notes']}")
        conn.close()
        return

    profile = load_profile()
    if profile is None or args.setup:
        profile = build_profile_interactively(client, model)

    jobs  = get_pending_jobs(cursor, limit=args.limit)
    total = len(jobs)

    if total == 0:
        print("No pending jobs to review.")
        print_stats(cursor)
        conn.close()
        return

    max_apply = args.max_apply if args.max_apply is not None else max_auto_env

    print(f"\nFound {total} unreviewed job(s).")
    if args.auto:
        print(f"Mode: FULLY AUTONOMOUS (cap: {max_apply} applications)")
    else:
        print("Mode: semi-auto (you confirm before each submit)")

    asyncio.run(
        run_session(
            jobs, total, profile, client, model,
            api_key, base_url, conn, cursor,
            auto_mode=args.auto,
            max_apply=max_apply,
            gmail_user=gmail_user,
            gmail_pass=gmail_pass,
            browser_api_key=browser_api_key,
            browser_url=browser_url,
            browser_model=browser_model,
        )
    )


if __name__ == "__main__":
    main()
