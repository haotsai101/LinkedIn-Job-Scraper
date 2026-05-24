#!/usr/bin/env python3
"""
apply_jobs.py - Fully autonomous AI job application agent.

Loads your profile once, classifies each pending job with an LLM, generates a
and uses browser-use to fill and submit applications
without any human intervention (--auto mode).  A session summary is written to
application_log.json and emailed via Gmail when the run finishes.

Environment variables (put in a .env file or export before running):
    LLM_API             - API key for the LLM endpoint
    LLM_URL             - Base URL of the OpenAI-compatible API
    LLM_MODEL           - Model name (default: gpt-4o-mini)
    GMAIL_USER          - Gmail address to send summary emails from/to
    GMAIL_APP_PASSWORD  - 16-char Google App Password (needs 2FA enabled)
    MAX_AUTO_APPLY      - Default daily cap (default: 10)

Usage:
    python apply_jobs.py                      # Semi-auto: confirm before each submit
    python apply_jobs.py --auto               # Fully autonomous: no confirmation
    python apply_jobs.py --auto --max-apply 5 # Cap this session at 5 applications
    python apply_jobs.py --limit 20           # Cap jobs reviewed this session to 20
    python apply_jobs.py --stats              # Print counts and exit
    python apply_jobs.py --setup              # Re-run profile interview

Commands while reviewing a listing (semi-auto / fallback mode):
    fill [instruction]   - Agent reads the page and fills form fields
    ask  [question]      - Ask the agent anything about the listing
    done / d / ENTER     - Mark as applied, go to next
    skip / s             - Skip this listing, go to next
    quit / q             - Save progress and exit
"""

import argparse
import asyncio
import csv
import json
import os
import smtplib
import sqlite3
import sys
import time
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from openai import OpenAI
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import Select
from browser_use.agent.service import Agent
from browser_use.controller import Controller
from browser_use.browser.session import BrowserSession
from browser_use.llm.openai.chat import ChatOpenAI as BrowserChatOpenAI

# ── Config ─────────────────────────────────────────────────────────────────────

DB_PATH = "linkedin_jobs.db"
PROFILE_PATH = "user_profile.json"
LOG_PATH = "application_log.json"
BROWSER = "edge"  # matches BROWSER in scripts/fetch.py; change to "chrome" if needed

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

# JS that extracts fillable form fields from the current page.
_FORM_FIELDS_JS = """
var SELECTOR = [
    'input:not([type="hidden"])',
    'input:not([type="submit"])',
    'input:not([type="button"])',
    'input:not([type="checkbox"])',
    'input:not([type="radio"])',
    'textarea',
    'select'
].join(',');
var results = [];
var idx = 0;
document.querySelectorAll(SELECTOR).forEach(function(el) {
    if (el.offsetParent === null) return;
    var labelEl = el.id ? document.querySelector('label[for="' + el.id + '"]') : null;
    var label = (
        (labelEl && labelEl.textContent.trim()) ||
        el.getAttribute('aria-label') ||
        el.getAttribute('placeholder') ||
        el.name || el.id || ''
    ).trim();
    var opts = [];
    if (el.tagName === 'SELECT') {
        Array.prototype.forEach.call(el.options, function(o) { opts.push(o.text.trim()); });
    }
    results.push({
        index: idx++,
        tag: el.tagName.toLowerCase(),
        type: el.type || '',
        id: el.id || '',
        name: el.name || '',
        label: label,
        current_value: el.value || '',
        options: opts
    });
});
return JSON.stringify(results);
"""

_PAGE_TEXT_JS = """
var el = document.body;
return el ? el.innerText.replace(/\\s+/g, ' ').trim().substring(0, 3000) : '';
"""


def _read_page_context(driver) -> str:
    try:
        url   = driver.current_url
        title = driver.title
        text  = driver.execute_script(_PAGE_TEXT_JS) or ""
    except Exception:
        return ""
    return (
        f"[Active browser tab]\n"
        f"URL   : {url}\n"
        f"Title : {title}\n"
        f"Text  : {text}"
    )


# ── Env / config loading ────────────────────────────────────────────────────────

def load_env():
    """Read .env file (if present) into os.environ, then validate required keys."""
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

    # browser-use needs a model that handles multimodal (screenshots) + tool calling.
    # Some providers (e.g. NVIDIA NIM with Qwen) fail on that message format.
    # Set BROWSER_LLM_* to use a separate model just for the browser agent.
    # Falls back to the main LLM vars when not set.
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
    """Append this session's report to application_log.json."""
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
    """Send an HTML session summary to gmail_user via Gmail SMTP."""
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


# ── LLM Agent ──────────────────────────────────────────────────────────────────

class JobAgent:
    """
    Stateful LLM agent that classifies jobs, fills form fields, and answers
    ad-hoc questions. Conversation history resets per listing.
    """

    _SYSTEM = """You are a job application assistant helping the user review LinkedIn job listings and fill out applications.

User profile:
{profile}

Your responsibilities:
1. Classify whether a job is relevant (software engineering, AI/ML, data engineering/science/analytics).
2. Read visible form fields and map them to the user's profile to fill applications accurately.
3. Answer questions about the current job listing concisely.

Be accurate and concise. Never fabricate information not in the user's profile."""

    def __init__(self, client: OpenAI, model: str, profile: dict):
        self.client = client
        self.model = model
        self.profile = profile
        self._system = self._SYSTEM.format(profile=json.dumps(profile, indent=2))
        self._history: list[dict] = []

    def _complete(self, user_message: str, *, json_mode: bool = False) -> str:
        messages = [{"role": "system", "content": self._system}]
        messages.extend(self._history)
        messages.append({"role": "user", "content": user_message})

        kwargs: dict = {"model": self.model, "messages": messages}
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        try:
            resp = self.client.chat.completions.create(**kwargs)
        except Exception as e:
            if json_mode:
                kwargs.pop("response_format")
                resp = self.client.chat.completions.create(**kwargs)
            else:
                raise e

        reply = resp.choices[0].message.content
        self._history.append({"role": "user",      "content": user_message})
        self._history.append({"role": "assistant",  "content": reply})
        return reply

    def reset_for_job(self, job_context: str):
        self._history = [
            {"role": "system", "content": f"Current job listing:\n{job_context}"}
        ]

    def classify(self, title: str, description: str) -> tuple[bool, str]:
        prompt = (
            "Is this job posting related to software engineering, AI/ML, or data "
            "(engineering / science / analytics)?\n\n"
            f"Title: {title}\n\n"
            f"Description excerpt:\n{(description or '')[:2000]}\n\n"
            'Respond with JSON: {"relevant": true|false, "reason": "<one sentence>"}'
        )
        try:
            raw = self._complete(prompt, json_mode=True)
            data = json.loads(raw)
            relevant = bool(data.get("relevant"))
            reason = data.get("reason", "")
            # Guard against the model returning a contradictory boolean/reason pair
            reason_lower = reason.lower()
            if relevant and ("not relevant" in reason_lower or "not related" in reason_lower):
                relevant = False
            return relevant, reason
        except Exception as exc:
            return True, f"Classification failed ({exc}); opening anyway."

    def fill_form(self, driver, instruction: str) -> str:
        try:
            raw_fields = driver.execute_script(_FORM_FIELDS_JS)
            if not raw_fields:
                return "No fillable fields found on the current page."
            fields: list[dict] = json.loads(raw_fields)
        except Exception as exc:
            return f"Could not read page fields: {exc}"

        if not fields:
            return "No fillable fields found on the current page."

        prompt = (
            f"Instruction from user: {instruction or 'fill everything you can from my profile'}\n\n"
            f"Visible form fields (JSON):\n{json.dumps(fields, indent=2)}\n\n"
            'Return JSON: {"fills": [{"index": <int>, "value": "<string>"}]}\n'
            "Rules:\n"
            "- Only fill fields for which you have confident, accurate data from the profile.\n"
            "- For SELECT fields, the value must exactly match one of the listed options.\n"
            "- Skip file inputs, unknown fields, and fields already filled correctly.\n"
            "- Never fabricate information."
        )

        try:
            raw_reply = self._complete(prompt, json_mode=True)
            fills: list[dict] = json.loads(raw_reply).get("fills", [])
        except Exception as exc:
            return f"LLM error: {exc}"

        if not fills:
            return "Nothing to fill — no confident matches between profile and form fields."

        selector = (
            'input:not([type="hidden"]):not([type="submit"])'
            ':not([type="button"]):not([type="checkbox"]):not([type="radio"]),'
            'textarea, select'
        )
        elements = driver.find_elements(By.CSS_SELECTOR, selector)

        filled, errors = [], []
        for action in fills:
            idx = action.get("index")
            val = str(action.get("value", ""))
            if idx is None or not val or idx >= len(elements):
                continue
            el = elements[idx]
            try:
                if el.tag_name.lower() == "select":
                    Select(el).select_by_visible_text(val)
                else:
                    el.clear()
                    el.send_keys(val)
                label = (
                    fields[idx].get("label")
                    or fields[idx].get("name")
                    or f"field[{idx}]"
                )
                filled.append(f"{label}: {val}")
            except Exception as exc:
                errors.append(f"field[{idx}]: {exc}")

        lines = [f"Filled {len(filled)} field(s):"]
        lines += [f"  • {f}" for f in filled]
        if errors:
            lines += [f"  ! {e}" for e in errors]
        return "\n".join(lines)

    def chat(self, message: str, driver=None) -> str:
        if driver is not None:
            page_ctx = _read_page_context(driver)
            message = f"{page_ctx}\n\nUser message: {message}"
        return self._complete(message)


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

_LOGIN_URL = "https://www.linkedin.com/checkpoint/rm/sign-in-another-account"
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


def _is_on_auth_page(driver) -> bool:
    path = driver.current_url.split("linkedin.com", 1)[-1] if "linkedin.com" in driver.current_url else ""
    return any(path.startswith(p) for p in _AUTH_HOSTPATHS)


def get_driver() -> webdriver.Remote:
    if BROWSER == "chrome":
        from selenium.webdriver.chrome.options import Options
        opts = Options()
        opts.add_argument("--start-maximized")
        return webdriver.Chrome(options=opts)
    from selenium.webdriver.edge.options import Options
    opts = Options()
    opts.add_argument("--start-maximized")
    return webdriver.Edge(options=opts)


def login_linkedin(driver) -> None:
    email, password = _get_login_credentials()
    print(f"  Signing into LinkedIn as {email}…", end="", flush=True)
    driver.get(_LOGIN_URL)
    time.sleep(1.5)

    try:
        driver.find_element(By.ID, "username").send_keys(email)
        driver.find_element(By.ID, "password").send_keys(password)
        driver.find_element(
            By.CSS_SELECTOR, 'button.btn__primary--large[type="submit"]'
        ).click()
    except Exception as exc:
        print(f"\n  [!] Could not interact with login form: {exc}")
        input("  Complete the login manually in the browser, then press ENTER to continue…")
        return

    for _ in range(15):
        time.sleep(1)
        if not _is_on_auth_page(driver):
            print(" done.")
            return

    print("\n  LinkedIn requires additional verification (CAPTCHA / 2-FA).")
    input("  Complete it in the browser window, then press ENTER to continue…")


async def login_linkedin_playwright(browser_session: BrowserSession) -> None:
    email, password = _get_login_credentials()
    print(f"  Signing into LinkedIn as {email} (Playwright)…", end="", flush=True)

    await browser_session.start()
    await browser_session.navigate_to(_LOGIN_URL)
    await asyncio.sleep(1.5)

    page = await browser_session.get_current_page()
    if page is None:
        page = await browser_session.new_page(_LOGIN_URL)

    try:
        username_els = await page.get_elements_by_css_selector("#username")
        password_els = await page.get_elements_by_css_selector("#password")
        submit_els   = await page.get_elements_by_css_selector('button.btn__primary--large[type="submit"]')
        if not username_els or not password_els or not submit_els:
            raise RuntimeError("Login form elements not found on page.")
        await username_els[0].fill(email)
        await password_els[0].fill(password)
        await submit_els[0].click()
    except Exception as exc:
        print(f"\n  [!] Could not interact with login form: {exc}")
        input("  Complete login manually in the browser, then press ENTER…")
        return

    for _ in range(20):
        await asyncio.sleep(1)
        url = await page.get_url()
        if "linkedin.com" in url and not any(p in url for p in _AUTH_HOSTPATHS):
            print(" done.")
            return

    print("\n  LinkedIn requires additional verification (CAPTCHA / 2-FA).")
    input("  Complete it in the browser, then press ENTER to continue…")


# ── Autonomous application ─────────────────────────────────────────────────────

async def auto_apply(
    job_url: str,
    job_title: str,
    profile: dict,
    api_key: str,
    base_url: str,
    model: str,
    browser_session: BrowserSession | None = None,
    auto_mode: bool = False,
    browser_api_key: str = "",
    browser_url: str = "",
    browser_model: str = "",
    application_type: str = "",
) -> str:
    """
    Use browser-use to apply to a job.

    auto_mode=True  → submits without any human confirmation.
    auto_mode=False → pauses at the review screen and waits for user input.

    Returns:
        "applied"  — application submitted
        "skipped"  — user chose to skip (semi-auto only)
        "failed"   — unrecoverable error
    """
    profile_json = json.dumps(profile, indent=2)
    outcome: dict[str, str] = {"status": "failed"}
    _ready_to_submit_called: list[bool] = [False]
    controller = Controller()

    @controller.action(
        "Notify user that the application is filled and ready for final review"
    )
    def ready_to_submit(summary: str) -> str:
        """
        Call this once ALL form fields are filled and you are on the final
        review / confirmation page. Do NOT click the Submit button before calling.

        summary: plain-English description of what was filled in.
        """
        _ready_to_submit_called[0] = True

        if outcome["status"] == "applied":
            return "Already approved. Click the Submit / Send Application button now."

        if auto_mode:
            # Fully autonomous — submit immediately without asking
            print(f"\n  Auto-submitting: {job_title}")
            print(f"  {summary}")
            outcome["status"] = "applied"
            return "Auto-approved. Click the final Submit / Send Application button now."

        # Semi-auto — show summary and wait for user decision
        print(f"\n{'═' * 64}")
        print(f"  Application ready: {job_title}")
        print(f"\n  {summary}")
        print(f"{'═' * 64}")
        try:
            choice = input(
                "  Review the form above in the browser.\n"
                "  [ENTER] = submit application   [s] = skip\n"
                "  > "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            choice = "s"

        if choice in ("s", "skip"):
            outcome["status"] = "skipped"
            return "User chose to skip. Do NOT click Submit. Stop here."
        else:
            outcome["status"] = "applied"
            return "User approved. Click the final Submit / Send Application button now."

    _agent_task: list[asyncio.Task] = []  # set just before browser_agent.run(); used to cancel early

    @controller.action("Mark job as no longer accepting applications")
    def mark_no_longer_accepting() -> str:
        """
        Call this when the job posting says it is no longer accepting applications,
        the position is closed, or the Apply button is missing/disabled.
        This will skip the posting permanently so it is never reviewed again.
        """
        outcome["status"] = "expired"
        for t in _agent_task:
            t.cancel()
        return "Job marked as expired. Stopping now."

    @controller.action(
        "Mark job as already applied — call when LinkedIn shows you already submitted an application for this job"
    )
    def mark_already_applied() -> str:
        """
        Call this when LinkedIn shows a message like 'You applied X days/hours ago'
        or 'Application submitted', indicating this job was already applied to.
        Do NOT use this for jobs that are closed or no longer accepting applications —
        use mark_no_longer_accepting for those.
        """
        outcome["status"] = "applied"
        for t in _agent_task:
            t.cancel()
        return "Job already applied. Stopping now."

    @controller.action(
        "Mark this job application as failed — call when you are stuck in a loop or "
        "cannot make progress after multiple attempts"
    )
    def mark_auto_failed(reason: str) -> str:
        """
        Call this when you have tried the same action 3+ times without success,
        are unable to locate the application form, or are in an unrecoverable state.
        """
        outcome["status"] = "failed"
        for t in _agent_task:
            t.cancel()
        return f"Job marked as auto-failed: {reason}. Stopping now."

    @controller.action(
        "Signal that the task is complete — only call after ready_to_submit has been called "
        "and the application has been submitted or explicitly stopped"
    )
    def done(text: str, success: bool = True) -> str:  # noqa: A001
        """
        Override the built-in done action to enforce that ready_to_submit was called
        before declaring the task complete.
        """
        if not _ready_to_submit_called[0] and outcome["status"] == "failed":
            outcome["status"] = "failed"
            for t in _agent_task:
                t.cancel()
            return (
                "ERROR: You cannot call done without first calling ready_to_submit. "
                "If the application form is filled, call ready_to_submit now. "
                "If you cannot complete the application, call mark_auto_failed instead."
            )
        for t in _agent_task:
            t.cancel()
        return text

    @controller.action(
        "Log into LinkedIn using stored credentials — call ONLY when the current page "
        "shows a LinkedIn login form with username and password fields"
    )
    async def handle_linkedin_login() -> str:
        """
        Call this ONLY when you see a LinkedIn login page (linkedin.com/login or
        linkedin.com/checkpoint) with visible username and password fields.
        Do NOT call for any other reason — for expired jobs use mark_no_longer_accepting,
        for other errors just report them.
        Credentials are handled internally — do NOT type them yourself.
        """
        email, password = _get_login_credentials()
        page = await browser_session.get_current_page()
        if page is None:
            return "No active browser page found."
        try:
            username_els = await page.get_elements_by_css_selector("#username")
            password_els = await page.get_elements_by_css_selector("#password")
            submit_els   = await page.get_elements_by_css_selector('button.btn__primary--large[type="submit"]')
            if not username_els or not password_els or not submit_els:
                return "Login form elements not found — page may not be a login page yet."
            await username_els[0].fill(email)
            await password_els[0].fill(password)
            await submit_els[0].click()
            await asyncio.sleep(2)
            return "LinkedIn authentication complete. Continue with the application task."
        except Exception as exc:
            return f"Login attempt encountered: {exc}. Additional verification (CAPTCHA/2-FA) may be needed."

    llm = BrowserChatOpenAI(
        model=browser_model or model,
        api_key=browser_api_key or api_key,
        base_url=browser_url or base_url,
    )

    is_offsite = application_type == "OffsiteApply"

    if is_offsite:
        apply_instructions = """3. Find and click the "Apply" button on the job listing. It will open the company's career site.
   - If a new browser tab opens automatically, switch to it using the switch action.
   - If the posting says "No longer accepting applications" or there is no Apply button, call mark_no_longer_accepting and stop.
   - If you see a message like "You applied X hours/days ago" or "Application submitted", call mark_already_applied and stop.
4. On the company career site, fill every visible form field using the user profile below.
   - For multi-step forms, click "Next" or "Continue" after completing each page.
   - Upload a resume only if there is a pre-filled resume option; skip file uploads otherwise.
   - If the career site requires creating an account or logging in and there is no guest/continue-without-account option, call mark_auto_failed."""
        new_tab_rule = "- If clicking Apply opens a new tab, switch to it immediately using the switch action, then fill the form there."
    else:
        apply_instructions = """3. Find and click the "Easy Apply" button on the job listing.
   - If the posting says "No longer accepting applications", the position is closed, or there is no Easy Apply button, call mark_no_longer_accepting and stop.
   - If you see a message like "You applied X hours/days ago" or "Application submitted", call mark_already_applied and stop.
4. Fill every visible form field using the user profile below.
   - For multi-step forms, click "Next" or "Continue" after completing each page.
   - Upload a resume only if there is a pre-filled resume option; skip file uploads otherwise."""
        new_tab_rule = "- Do not open new tabs. All navigation must stay in the current tab (never use new_tab=True)."

    task = f"""You are applying to a job on behalf of the user.

Follow these steps exactly:

1. Navigate to: {job_url}
2. If you see a LinkedIn login form with visible username and password fields (URL contains /login or /checkpoint), call handle_linkedin_login — do NOT type credentials yourself. Only call this for an actual login form, not for any other issue.
{apply_instructions}
5. When ALL fields are filled and you are on the final review / confirmation page,
   call the ready_to_submit action with a plain-English summary of what you filled.
   CRITICAL: You are FORBIDDEN from clicking Submit / Send Application before calling ready_to_submit.
   If you click Submit before calling ready_to_submit, the application will be misrecorded.
6. After ready_to_submit returns, follow its instruction exactly:
   - If it says "Auto-approved" or "User approved", click the Submit / Send Application button now.
   - If it says to stop, do NOT click Submit.

User profile:
{profile_json}

Rules:
- Never type LinkedIn credentials — use handle_linkedin_login if an auth page appears.
- Never invent or guess information that is not in the profile.
- Leave fields blank if the profile does not contain the required information.
- Do not attach files unless a pre-populated option is already present.
{new_tab_rule}
- If a form step is confusing, scroll down to see all fields before taking any action.
- Sponsorship questions: if asked "Will you now or in the future require sponsorship?", answer YES if work_authorization is OPT, H1B, F1, or TN; answer NO if work_authorization is "US Citizen" or "Green Card".
- Authorization questions: if asked "Are you legally authorized to work in the US?", answer YES (OPT is authorized to work).
- If you have attempted the same action (clicking a button, navigating to a URL) 3 or more times without a different result, call mark_auto_failed — you are stuck in a loop.
"""

    _JOB_TIMEOUT_SECS = 300  # 5 minutes max per job regardless of steps

    try:
        browser_agent = Agent(
            task=task,
            llm=llm,
            controller=controller,
            browser_session=browser_session,
            use_vision=False,        # NVIDIA NIM can't handle mixed image+text content
            max_actions_per_step=3,  # smaller batches reduce per-call token load and timeout risk
            llm_timeout=120,         # default 75s times out on large DOM steps
            use_thinking=False,      # disable chain-of-thought to prevent truncated JSON responses
        )
        agent_run = asyncio.create_task(browser_agent.run(max_steps=60))
        _agent_task.append(agent_run)
        await asyncio.wait_for(asyncio.shield(agent_run), timeout=_JOB_TIMEOUT_SECS)
    except asyncio.TimeoutError:
        for t in _agent_task:
            t.cancel()
        print(f"\n  [!] Auto-apply timed out after {_JOB_TIMEOUT_SECS}s")
    except asyncio.CancelledError:
        pass  # expected when mark_no_longer_accepting / mark_already_applied cancels the task
    except Exception as exc:
        print(f"\n  [!] Auto-apply error: {exc}")

    return outcome["status"]


# ── Main ────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="AI-assisted LinkedIn job reviewer.")
    parser.add_argument("--auto",      action="store_true", help="Fully autonomous: submit without confirmation.")
    parser.add_argument("--max-apply", type=int, default=None, help="Max applications to submit this session.")
    parser.add_argument("--limit",     type=int, default=None, help="Max jobs to review this session.")
    parser.add_argument("--stats",     action="store_true", help="Print stats and exit.")
    parser.add_argument("--setup",     action="store_true", help="Re-run profile setup interview.")
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

    # --max-apply CLI overrides env MAX_AUTO_APPLY
    max_apply = args.max_apply if args.max_apply is not None else max_auto_env

    print(f"\nFound {total} unreviewed job(s).")
    if args.auto:
        print(f"Mode: FULLY AUTONOMOUS (cap: {max_apply} applications)")
    else:
        print("Mode: semi-auto (you confirm before each submit)")
        print("Fallback commands:")
        print("  fill [instruction]  — fill form fields on the current page")
        print("  ask  [question]     — ask the agent about the listing")
        print("  done / d / ENTER    — mark as applied, next listing")
        print("  skip / s            — skip, next listing")
        print("  quit / q            — save and exit")

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
    """
    Single async context that owns the Playwright browser for the full session.
    Writes application_log.json and sends a Gmail summary when done.
    """
    agent = JobAgent(client, model, profile)

    started_at = datetime.now(timezone.utc).isoformat()
    session_date = datetime.now().strftime("%Y-%m-%d")
    applications: list[dict] = []
    applied_count = skipped_count = error_count = 0

    browser_session: BrowserSession | None = None
    driver = None

    print("\nOpening browser and signing into LinkedIn…")
    browser_session = BrowserSession(headless=False, keep_alive=True)
    await login_linkedin_playwright(browser_session)
    print("  Session ready.\n")

    try:
        for idx, row in enumerate(jobs, 1):
            job_id, title, job_url, location, exp_level, description, company_name, application_type = row
            url = job_url or f"https://www.linkedin.com/jobs/view/{job_id}/"

            # ── Classification ─────────────────────────────────────────────────
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

            # ── Stop if apply cap reached ──────────────────────────────────────
            if auto_mode and applied_count >= max_apply:
                print(f"  [cap] Reached max-apply limit ({max_apply}). Stopping applications.")
                break

            # ── Autonomous / semi-auto apply ───────────────────────────────────
            print("  Applying via browser-use…")
            tabs_before = {t.target_id for t in await browser_session.get_tabs()}

            status = await auto_apply(
                url, title or "Unknown", profile,
                api_key, base_url, model,
                browser_session=browser_session,
                auto_mode=auto_mode,
                browser_api_key=browser_api_key,
                browser_url=browser_url,
                browser_model=browser_model,
                application_type=application_type or "",
            )

            # Close any tabs opened for this posting before moving to the next one
            try:
                for tab in await browser_session.get_tabs():
                    if tab.target_id not in tabs_before:
                        await browser_session.close_page(tab.target_id)
            except Exception:
                pass

            # Navigate to blank so the next agent doesn't start on this job's confirmation page
            try:
                page = await browser_session.get_current_page()
                if page:
                    await page.goto("about:blank")
            except Exception:
                pass

            await asyncio.sleep(3)  # let the browser session settle focus before next agent

            if status == "applied":
                mark_job(conn, cursor, job_id, 1)
                applied_count += 1
                applications.append({
                    "job_id":                  job_id,
                    "title":                   title or "",
                    "company":                 company_name or "",
                    "url":                     url,
                    "applied_at":              datetime.now(timezone.utc).isoformat(),
                })
                print("  [+] Applied!")
                time.sleep(10)  # brief pause to avoid rate limiting
                continue

            elif status == "expired":
                mark_job(conn, cursor, job_id, -1)
                skipped_count += 1
                print("  [~] No longer accepting applications — marked as skipped.")
                continue

            elif status == "skipped":
                mark_job(conn, cursor, job_id, -1)
                skipped_count += 1
                print("  [-] Skipped.")
                continue

            # ── Auto-apply failed ──────────────────────────────────────────────
            error_count += 1

            if auto_mode:
                # In fully autonomous mode: log the failure and move on
                mark_job(conn, cursor, job_id, -2)
                print("  [!] Auto-apply failed — marked as auto-failed, continuing.")
                continue

            # ── Fallback: manual Selenium mode (semi-auto only) ────────────────
            print("  [!] Auto-apply failed — switching to manual mode.")
            if driver is None:
                print("  Opening Selenium browser…")
                driver = get_driver()
                login_linkedin(driver)

            try:
                driver.get(url)
            except Exception as exc:
                print(f"  [!] Browser error: {exc}")

            agent.reset_for_job(
                f"Title: {title}\nCompany: {company_name}\n"
                f"Location: {location}\nLevel: {exp_level}\nURL: {url}\n"
                f"Description excerpt:\n{(description or '')[:1200]}"
            )

            print(f"{'─' * 64}")

            while True:
                try:
                    raw = input("  > ").strip()
                except (EOFError, KeyboardInterrupt):
                    raw = "quit"

                if not raw:
                    raw = "done"

                lower = raw.lower()

                if lower in ("quit", "q"):
                    print(f"\nQuitting. Applied: {applied_count}, Skipped: {skipped_count}")
                    return

                elif lower in ("done", "d", "applied", "next"):
                    mark_job(conn, cursor, job_id, 1)
                    applied_count += 1
                    applications.append({
                        "job_id":                  job_id,
                        "title":                   title or "",
                        "company":                 company_name or "",
                        "url":                     url,
                        "applied_at":              datetime.now(timezone.utc).isoformat(),
                    })
                    print("  [+] Marked as applied.")
                    break

                elif lower in ("skip", "s"):
                    mark_job(conn, cursor, job_id, -1)
                    skipped_count += 1
                    print("  [-] Skipped.")
                    break

                elif lower.startswith("fill"):
                    instruction = raw[4:].strip()
                    print("  Filling…", flush=True)
                    result = agent.fill_form(driver, instruction)
                    print(f"\n{result}\n")

                elif lower.startswith("ask"):
                    question = raw[3:].strip() or "Summarise this job listing for me."
                    reply = agent.chat(question, driver=driver)
                    print(f"\n  {reply}\n")

                else:
                    reply = agent.chat(raw, driver=driver)
                    print(f"\n  {reply}\n")

    finally:
        if browser_session:
            await browser_session.kill()
        if driver:
            driver.quit()
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


if __name__ == "__main__":
    main()
