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
    LLM_MODEL           - Model name
    GMAIL_USER          - Gmail address to send summary emails from/to
    GMAIL_APP_PASSWORD  - 16-char Google App Password (needs 2FA enabled)
    MAX_AUTO_APPLY      - Default daily cap (default: 10)

    # Optional: separate model/endpoint just for the browser agent
    BROWSER_LLM_API             - API key (defaults to LLM_API)
    BROWSER_LLM_URL             - Base URL (defaults to LLM_URL)
    BROWSER_LLM_MODEL           - Model name (defaults to LLM_MODEL)
    BROWSER_LLM_FALLBACK_MODEL  - Faster fallback model used after 2 consecutive LLM timeouts (defaults to LLM_MODEL)

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
import email
import email.message
import imaplib
import json
import os
import re
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

import httpx

from openai import OpenAI, AsyncOpenAI
from playwright.async_api import async_playwright

from linkedin_apply import EasyApplyFlow, OffsiteApplyFlow, _get_profile_value, _session_llm_state

sys.stdout.reconfigure(line_buffering=True)

# ── Config ─────────────────────────────────────────────────────────────────────

DB_PATH      = "linkedin_jobs.db"

# Companies to permanently skip (case-insensitive substring match on company name)
BLOCKED_COMPANIES = {
    "synergisticit",
    "ladders",       # theladders.com — paid job board, requires Ladders account
}

# Job posting domains that can't be auto-applied (OAuth-only, paid walls, etc.)
BLOCKED_DOMAINS = {
    "theladders.com",
    "ed.crossover.com",  # Apply with Google/LinkedIn only — no form
    "rex.zone",
}
PROFILE_PATH = "user_profile.json"
LOG_PATH     = "application_log.json"
LLM_LOG_PATH = "llm_debug.jsonl"
ACCOUNTS_PATH = "created_accounts.json"

def _write_llm_log(entry: dict):
    try:
        with open(LLM_LOG_PATH, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


async def _llm_fill_focused(page, llm_client: AsyncOpenAI, llm_model: str, profile: dict):
    """LLM-fill whichever input field is currently focused in the browser."""
    field = await page.evaluate("""() => {
        const el = document.activeElement;
        if (!el || el === document.body || el === document.documentElement) return null;
        const label = (el.labels && el.labels[0] && el.labels[0].textContent.trim())
            || el.getAttribute('aria-label') || el.placeholder || el.name || el.id || '';
        return {tag: el.tagName, type: el.type || '', id: el.id || '', name: el.name || '',
                label: label.trim(), value: el.value || ''};
    }""")
    if not field:
        print("  [f] No input field focused — click a field in the browser first.")
        return
    label = field.get("label") or field.get("name") or field.get("id") or "unknown"
    kind  = field.get("type") or "text"
    print(f"  [f] Focused field: '{label}' ({kind})")

    # Try deterministic profile lookup first
    value = _get_profile_value(profile, label, kind)
    if value is None:
        # Ask LLM
        profile_line = (
            f"name={profile.get('full_name')} preferred={profile.get('preferred_name','')} "
            f"email={profile.get('email')} phone={profile.get('phone')} "
            f"location={profile.get('location')} title={profile.get('current_title')} "
            f"yrs={profile.get('years_experience')} auth={profile.get('work_authorization')} "
            f"needs_sponsorship={profile.get('need_sponsorship')}"
        )
        try:
            resp = await llm_client.chat.completions.create(
                model=llm_model,
                messages=[{"role": "user", "content":
                    f"Job application form field.\nLabel: {label!r}\nType: {kind}\n"
                    f"Profile: {profile_line}\n\n"
                    "Reply with ONLY the value to fill in this field. No explanation. "
                    "CRITICAL: Never fabricate URLs, social media handles, or info not in the profile. "
                    "For URL/link fields not in the profile (Twitter, Instagram, blog, etc.), reply with empty string."}],
                max_tokens=100,
                timeout=30,
            )
            value = (resp.choices[0].message.content or "").strip()
        except Exception as e:
            print(f"  [f] LLM error: {e}")
            return

    if not value:
        print(f"  [f] No value determined for '{label}' — leaving blank.")
        return

    print(f"  [f] Filling '{label}' = {value!r}")
    try:
        el = page.locator(f"#{field['id']}").first if field.get("id") else None
        if not el or await el.count() == 0:
            el = page.get_by_label(label, exact=False).first
        if el and await el.count() > 0:
            await el.click()
            await el.fill(value)
        else:
            print(f"  [f] Could not locate field in page.")
    except Exception as e:
        print(f"  [f] Fill error: {e}")


def _check_recent_session_health() -> bool:
    """Returns False if the last 3 sessions all had >80% error rates — signals a systematic blocker."""
    log_path = Path(LOG_PATH)
    if not log_path.exists():
        return True
    try:
        sessions = json.loads(log_path.read_text()).get("sessions", [])[-3:]
        if len(sessions) < 3:
            return True
        def _error_rate(s):
            total = s.get("error_count", 0) + s.get("applied_count", 0) + s.get("skipped_count", 0)
            return s.get("error_count", 0) / total if total > 0 else 0
        return sum(1 for s in sessions if _error_rate(s) > 0.8) < 3
    except Exception:
        return True


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
    model        = os.environ.get("LLM_MODEL", "").strip()
    gmail_user   = os.environ.get("GMAIL_USER", "").strip()
    gmail_pass   = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    max_auto_env = int(os.environ.get("MAX_AUTO_APPLY", "10"))

    browser_api_key      = os.environ.get("BROWSER_LLM_API", api_key).strip()
    browser_url          = os.environ.get("BROWSER_LLM_URL", base_url).strip()
    browser_model        = os.environ.get("BROWSER_LLM_MODEL", model).strip()
    browser_fallback_model = os.environ.get("BROWSER_LLM_FALLBACK_MODEL", model).strip()

    classifier_api_key = os.environ.get("CLASSIFIER_LLM_API", api_key).strip()
    classifier_url     = os.environ.get("CLASSIFIER_LLM_URL", base_url).strip()
    classifier_model   = os.environ.get("CLASSIFIER_LLM_MODEL", model).strip()

    if not api_key or not base_url:
        sys.exit("Error: LLM_API and LLM_URL must be set (in .env or environment).")

    return api_key, base_url, model, gmail_user, gmail_pass, max_auto_env, browser_api_key, browser_url, browser_model, classifier_api_key, classifier_url, classifier_model


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


# ── Gmail IMAP inbox reader ────────────────────────────────────────────────────

class EmailInbox:
    """Reads Gmail via IMAP to retrieve verification links and codes sent to the applicant email."""

    def __init__(self, user: str, password: str):
        self.user = user
        self.password = password

    def _connect(self):
        M = imaplib.IMAP4_SSL("imap.gmail.com")
        M.login(self.user, self.password)
        M.select("INBOX")
        return M

    @staticmethod
    def _root_domain(netloc: str) -> str:
        """Extract registrable domain: jobs.company.com → company.com."""
        host = netloc.split(":")[0]  # drop port if present
        parts = host.split(".")
        return ".".join(parts[-2:]) if len(parts) >= 2 else host

    @staticmethod
    def _body(msg) -> str:
        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                ct = part.get_content_type()
                if ct == "text/plain":
                    body += part.get_payload(decode=True).decode(errors="replace")
                elif ct == "text/html" and not body:
                    body += part.get_payload(decode=True).decode(errors="replace")
        else:
            body = msg.get_payload(decode=True).decode(errors="replace")
        return body

    def _fetch_first_unseen(self, root_domain: str, timeout: int = 90) -> "str | None":
        """Hold one IMAP connection open and poll until an unseen email from root_domain arrives."""
        deadline = time.time() + timeout
        M = None
        try:
            M = self._connect()
        except Exception as exc:
            print(f"  [Inbox] IMAP connect error: {exc}")
            return None
        try:
            first_error = True
            while time.time() < deadline:
                try:
                    M.check()
                except Exception:
                    try:
                        M.logout()
                    except Exception:
                        pass
                    try:
                        M = self._connect()
                    except Exception as exc:
                        print(f"  [Inbox] IMAP reconnect error: {exc}")
                        return None
                try:
                    _, data = M.search(None, f'(UNSEEN FROM "{root_domain}")')
                    for num in (data[0].split() or []):
                        _, msg_data = M.fetch(num, "(RFC822)")
                        msg = email.message_from_bytes(msg_data[0][1])
                        body = self._body(msg)
                        M.store(num, "+FLAGS", "\\Seen")
                        return body
                except Exception as exc:
                    if first_error:
                        print(f"  [Inbox] IMAP search error: {exc}")
                        first_error = False
                time.sleep(5)
        finally:
            try:
                M.logout()
            except Exception:
                pass
        return None

    def fetch_verification(self, from_domain: str, timeout: int = 90,
                           keywords: tuple = ("verify", "confirm", "activate")) -> "tuple[str|None, str|None]":
        """Fetch one unseen email from from_domain; return (code, link) — whichever is present."""
        root = self._root_domain(from_domain)
        body = self._fetch_first_unseen(root, timeout)
        if not body:
            return None, None
        codes = re.findall(r'\b(\d{4,8})\b', body)
        urls = [
            u.rstrip(".,;:!?)")
            for u in re.findall(r'https?://[^\s<>"\']+', body)
            if any(k in u.lower() for k in keywords)
        ]
        return (codes[0] if codes else None), (urls[0] if urls else None)

    def wait_for_link(self, from_domain: str, timeout: int = 90,
                      keywords: tuple = ("verify", "confirm", "activate")) -> "str | None":
        """Poll INBOX for an unseen email from from_domain; return first URL containing a keyword."""
        _, link = self.fetch_verification(from_domain, timeout, keywords)
        return link

    def wait_for_code(self, from_domain: str, timeout: int = 90) -> "str | None":
        """Poll INBOX for an unseen email from from_domain; return first 4-8 digit code found."""
        code, _ = self.fetch_verification(from_domain, timeout)
        return code


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
    from urllib.parse import urlparse as _up
    path = Path(ACCOUNTS_PATH)
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except Exception:
            data = {"accounts": []}
    else:
        data = {"accounts": []}

    new_domain = _up(record.get("website_url", "")).netloc
    updated = False
    if new_domain:
        for i, existing in enumerate(data["accounts"]):
            existing_domain = _up(existing.get("website_url", "")).netloc
            if (existing_domain == new_domain
                    or new_domain.endswith("." + existing_domain)
                    or existing_domain.endswith("." + new_domain)):
                data["accounts"][i] = record  # replace with newest
                updated = True
                break
    if not updated:
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
    _SYSTEM = """/no_think
You are a job application assistant helping the user review LinkedIn job listings.

User profile:
{profile}

Classify whether a job is relevant (software engineering, AI/ML, data engineering/science/analytics).
Be accurate and concise. Never fabricate information not in the user's profile."""

    def __init__(self, client: OpenAI, model: str, profile: dict):
        self.client  = client
        self.model   = model
        self._system = self._SYSTEM.format(profile=json.dumps(profile, indent=2))

    # Keyword patterns that unambiguously require US citizenship — checked before LLM call
    _CITIZENSHIP_KEYWORDS = (
        "must be a u.s. citizen", "must be a us citizen",
        "must be us citizens", "us citizens or us persons",
        "must be united states citizens", "all candidates must be us",
        "us citizenship required", "u.s. citizenship required",
        "united states citizenship required",
        "requires us citizenship", "requires u.s. citizenship",
        # YCombinator Visa field variants
        "us citizen/visa only", "u.s. citizen/visa only",
        "citizens only", "visa: us",
        "ts/sci", "top secret/sci", "top secret sci",
        "active top secret clearance", "active secret clearance",
        "active ts clearance",
    )

    def classify(self, title: str, description: str) -> tuple[bool, str, bool]:
        """Returns (relevant, reason, citizenship_required)."""
        desc = description or ""

        # Fast path: keyword scan on full description before paying for an LLM call
        desc_lower = desc.lower()
        for kw in self._CITIZENSHIP_KEYWORDS:
            if kw in desc_lower:
                reason = f"Requires US citizenship or active clearance ({kw})"
                _write_llm_log({
                    "ts":      datetime.now(timezone.utc).isoformat(),
                    "type":    "classifier",
                    "model":   "keyword",
                    "title":   title,
                    "result":  {"relevant": False, "reason": reason, "citizenship_required": True},
                })
                return False, reason, True

        prompt = (
            "Review this job posting on two dimensions:\n"
            "1. Is it related to software engineering, AI/ML, or data (engineering/science/analytics)?\n"
            "2. Does it explicitly require US citizenship or an active US security clearance "
            "(e.g. 'must be a US citizen', 'TS/SCI required', 'active Secret clearance')?\n\n"
            f"Title: {title}\n\n"
            f"Description:\n{desc[:3000]}\n\n"
            'Respond with JSON: {"relevant": true|false, "reason": "<one sentence>", "citizenship_required": true|false}'
        )
        messages = [
            {"role": "system", "content": self._system},
            {"role": "user", "content": prompt},
        ]
        for attempt in range(3):
            try:
                _t0 = time.monotonic()
                resp = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    response_format={"type": "json_object"},
                    max_tokens=300,
                    timeout=60,
                )
                _call_ms = int((time.monotonic() - _t0) * 1000)
                raw = resp.choices[0].message.content
                data = json.loads(raw)
                relevant = bool(data.get("relevant"))
                reason   = data.get("reason", "")
                # If model returned the old single-dimension format (no citizenship_required field),
                # treat as indeterminate and retry so the full two-dimension prompt is re-sent.
                if "citizenship_required" not in data:
                    if attempt < 2:
                        continue
                    # Last attempt still missing field — default conservative (assume not required)
                citizenship_required = bool(data.get("citizenship_required", False))
                if relevant and ("not relevant" in reason.lower() or "not related" in reason.lower()):
                    relevant = False
                if citizenship_required:
                    relevant = False
                _write_llm_log({
                    "ts":           datetime.now(timezone.utc).isoformat(),
                    "type":         "classifier",
                    "model":        self.model,
                    "duration_ms":  _call_ms,
                    "title":        title,
                    "prompt":       prompt,
                    "raw_response": raw,
                    "result":       {"relevant": relevant, "reason": reason, "citizenship_required": citizenship_required},
                })
                return relevant, reason, citizenship_required
            except Exception as exc:
                exc_str = str(exc)
                is_timeout = "timeout" in exc_str.lower() or "timed out" in exc_str.lower()
                if ("429" in exc_str or "503" in exc_str or is_timeout) and attempt < 2:
                    wait = 10 * (2 ** attempt)  # 10s, 20s
                    if "429" in exc_str:
                        code = "429"
                    elif is_timeout:
                        code = "timeout"
                    else:
                        code = "503"
                    print(f"\n  [rate limit] {code} — waiting {wait}s before retry…", end="", flush=True)
                    time.sleep(wait)
                    print(" retrying.")
                    continue
                # All retries exhausted or non-transient error
                raise


# ── Database helpers ────────────────────────────────────────────────────────────

def migrate_db(conn, cursor):
    cursor.execute("PRAGMA table_info(jobs)")
    cols = {row[1] for row in cursor.fetchall()}
    if "applied" not in cols:
        cursor.execute("ALTER TABLE jobs ADD COLUMN applied INTEGER DEFAULT NULL")
        conn.commit()
        print("DB migrated: added 'applied' column.")


def get_pending_jobs(cursor, limit=None, apply_type=None):
    where_extra = ""
    if apply_type:
        types = [f"'{t.strip()}'" for t in apply_type.split(",")]
        where_extra = f" AND j.application_type IN ({', '.join(types)})"
    blocked_clause = ""
    if BLOCKED_COMPANIES:
        conditions = " AND ".join(
            f"LOWER(COALESCE(c.name, '')) NOT LIKE '%{co}%'"
            for co in BLOCKED_COMPANIES
        )
        blocked_clause = f" AND ({conditions})"
    query = f"""
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
          ){where_extra}{blocked_clause}
        ORDER BY COALESCE(j.original_listed_time, 0) DESC
    """
    if limit:
        query += f" LIMIT {int(limit)}"
    cursor.execute(query)
    return cursor.fetchall()


def mark_job(conn, cursor, job_id: int, status: int):
    """status: 1=applied, -1=skipped, -2=auto-failed."""
    cursor.execute("UPDATE jobs SET applied = ? WHERE job_id = ?", (status, job_id))
    conn.commit()


def skip_ineligible_jobs(conn, cursor) -> int:
    """Mark all pending scraped jobs that are not remote/Utah as skipped. Returns count."""
    cursor.execute("""
        UPDATE jobs
        SET applied = -1
        WHERE scraped > 0
          AND applied IS NULL
          AND remote_allowed IS NOT 1
          AND LOWER(COALESCE(location, '')) NOT LIKE '%remote%'
          AND LOWER(COALESCE(location, '')) NOT LIKE '%utah%'
          AND LOWER(COALESCE(location, '')) NOT LIKE '%, ut%'
    """)
    conn.commit()
    return cursor.rowcount


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
        try:
            input("  Complete login manually in the browser, then press ENTER…")
        except EOFError:
            pass
        return

    for _ in range(20):
        await asyncio.sleep(1)
        if "linkedin.com" in page.url and not any(p in page.url for p in _AUTH_HOSTPATHS):
            print(" done.")
            return

    print("\n  LinkedIn requires additional verification (CAPTCHA / 2-FA).")
    try:
        input("  Complete it in the browser, then press ENTER to continue…")
    except EOFError:
        import sys as _sys
        if not _sys.stdin.isatty():
            print("  No terminal — waiting 45 s for you to solve CAPTCHA in the browser…")
            import time as _time
            _time.sleep(45)
            print("  Continuing.")


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
    classifier_api_key: str = "",
    classifier_url: str = "",
    classifier_model: str = "",
    verbose: bool = False,
):
    classifier_client = OpenAI(
        api_key=classifier_api_key or api_key,
        base_url=classifier_url or base_url,
        timeout=httpx.Timeout(90.0),  # 90s wall-clock per request
    )
    _classifier_model = classifier_model or model
    agent = JobAgent(classifier_client, _classifier_model, profile)

    inbox = EmailInbox(gmail_user, gmail_pass) if gmail_user and gmail_pass else None

    started_at   = datetime.now(timezone.utc).isoformat()
    session_date = datetime.now().strftime("%Y-%m-%d")
    applications: list[dict] = []
    applied_count = skipped_count = error_count = 0

    # Write a session boundary marker so log-monitor agents can filter to just this run
    _write_llm_log({
        "ts":   started_at,
        "type": "session_start",
        "auto_mode": auto_mode,
        "total_jobs": total,
    })
    all_unanswered_fields: list[str] = []

    browser_llm_client = AsyncOpenAI(
        api_key=browser_api_key or api_key,
        base_url=browser_url or base_url,
        timeout=190.0,  # hard HTTP timeout; AsyncOpenAI is native asyncio so asyncio.wait_for can cancel it
        max_retries=0,  # we handle retries ourselves in _ask_llm_action
    )
    browser_llm_model = browser_model or model
    browser_llm_fallback = browser_fallback_model or model

    # Reset circuit-breaker state for this session
    _session_llm_state["timeout_streak"] = 0
    _session_llm_state["model_switched"] = False

    print("\nOpening browser and signing into LinkedIn…")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(
            permissions=[],  # deny browser notification/location prompts
        )
        page    = await context.new_page()

        try:
            await login_linkedin_playwright(page)
            print("  Session ready.\n")
        except Exception as _login_err:
            await browser.close()
            raise

        if not _check_recent_session_health():
            print("\n  [!] Warning: the last 3 sessions all had >80% error rates.")
            print("  Check llm_debug.jsonl and application_log.json for a systematic blocker before continuing.\n")

        try:
            for idx, row in enumerate(jobs, 1):
                job_id, title, job_url, location, exp_level, description, company_name, application_type = row
                url = job_url or f"https://www.linkedin.com/jobs/view/{job_id}/"

                print(f"\n{'─' * 64}")
                print(f"  [{idx}/{total}]  {title or 'Unknown'}  |  {company_name or 'Unknown company'}")
                print(f"  Location : {location or 'N/A'}   Level: {exp_level or 'N/A'}")

                _title_lower = (title or "").lower()
                if ("staff" in _title_lower or "principal" in _title_lower) and "engineer" in _title_lower:
                    mark_job(conn, cursor, job_id, -1)
                    skipped_count += 1
                    print("  Auto-skipped — staff/principal engineer position.")
                    continue

                print("  Classifying…", end="", flush=True)
                try:
                    relevant, reason, citizenship_required = await asyncio.wait_for(
                        asyncio.to_thread(agent.classify, title or "", description or ""),
                        timeout=90,
                    )
                except asyncio.TimeoutError:
                    print(" [timeout] Classifier did not respond in 90s — leaving pending for retry.")
                    skipped_count += 1
                    continue
                except Exception as exc:
                    print(f"\n  [!] Classification failed after all retries ({exc}) — leaving pending, stopping session.")
                    break
                await asyncio.sleep(1)  # avoid LLM rate limiting between calls
                if citizenship_required:
                    tag = "⊘ citizenship required"
                elif relevant:
                    tag = "✓ relevant"
                else:
                    tag = "✗ not relevant"
                print(f" {tag} — {reason}")

                if not relevant:
                    mark_job(conn, cursor, job_id, -1)
                    skipped_count += 1
                    if citizenship_required:
                        print("  Skipped — citizenship/clearance requirement.")
                    else:
                        print("  Auto-skipped.")
                    continue

                if auto_mode and applied_count >= max_apply:
                    print(f"  [cap] Reached max-apply limit ({max_apply}). Stopping applications.")
                    break

                # Skip jobs whose application URL is on a blocked domain
                from urllib.parse import urlparse as _urlparse
                _app_url = url or ""
                if any(bd in _urlparse(_app_url).netloc for bd in BLOCKED_DOMAINS):
                    mark_job(conn, cursor, job_id, -1)
                    skipped_count += 1
                    print(f"  [~] Application domain blocked — skipped.")
                    continue

                # ── Build callbacks ────────────────────────────────────────────
                outcome: dict[str, str] = {"status": "pending"}

                def _make_ready_to_submit(outcome_ref, job_title_ref):
                    async def ready_to_submit(summary: str) -> str:
                        if auto_mode:
                            print(f"\n  Auto-submitting: {job_title_ref}")
                            print(f"  {summary}")
                            outcome_ref["status"] = "applied"
                            return "applied"
                        print(f"\n{'═' * 64}")
                        print(f"  Application ready: {job_title_ref}")
                        print(f"\n  {summary}")
                        print(f"{'═' * 64}")
                        while True:
                            try:
                                choice = input(
                                    "  Review the form in the browser.\n"
                                    "  [ENTER] = submit   [s] = skip   [f] = LLM-fill focused field\n"
                                    "  > "
                                ).strip().lower()
                            except (EOFError, KeyboardInterrupt):
                                choice = "s"
                            if choice in ("s", "skip"):
                                outcome_ref["status"] = "skipped"
                                return "skipped"
                            if choice == "f":
                                await _llm_fill_focused(page, browser_llm_client, browser_model, profile)
                                continue
                            outcome_ref["status"] = "applied"
                            return "applied"
                    return ready_to_submit

                async def _fill_focused_cb():
                    await _llm_fill_focused(page, browser_llm_client, browser_model, profile)

                callbacks = {
                    "ready_to_submit":  _make_ready_to_submit(outcome, title or "Unknown"),
                    "fill_focused":     _fill_focused_cb,
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
                        job_description=description or "",
                        classifier_client=classifier_client,
                        classifier_model=_classifier_model,
                        verbose=verbose,
                        inbox=inbox,
                        fallback_model=browser_llm_fallback,
                    )
                else:
                    flow = EasyApplyFlow(
                        page=page,
                        profile=profile,
                        llm_client=browser_llm_client,
                        model=browser_llm_model,
                        auto_mode=auto_mode,
                        callbacks=callbacks,
                        classifier_client=classifier_client,
                        classifier_model=_classifier_model,
                        verbose=verbose,
                    )
                    flow._verbose_company = company_name or "unknown"

                try:
                    status = await asyncio.wait_for(flow.run(url), timeout=600)
                except asyncio.TimeoutError:
                    print(f"\n  [!] Timed out after 600s")
                    status = "failed"
                except Exception as exc:
                    print(f"\n  [!] Error during apply: {exc}")
                    status = "failed"

                # Easy Apply job switched to external apply — retry with OffsiteApplyFlow
                if status == "external_apply" and not isinstance(flow, OffsiteApplyFlow):
                    print("  [~] Job switched from Easy Apply to external — retrying with OffsiteApplyFlow…")
                    pages_before = set(context.pages)
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
                        job_description=description or "",
                        classifier_client=classifier_client,
                        classifier_model=_classifier_model,
                        inbox=inbox,
                        fallback_model=browser_llm_fallback,
                    )
                    try:
                        status = await asyncio.wait_for(flow.run(url), timeout=600)
                    except asyncio.TimeoutError:
                        print(f"\n  [!] Timed out after 600s")
                        status = "failed"
                    except Exception as exc:
                        print(f"\n  [!] Error during offsite apply: {exc}")
                        status = "failed"

                # Collect unanswered fields for profile improvement
                for f in getattr(flow, "unanswered_fields", []):
                    if f not in all_unanswered_fields:
                        all_unanswered_fields.append(f)

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
                    print("  [~] Job closed — skipped.")

                elif status == "no_easy_apply":
                    mark_job(conn, cursor, job_id, -1)
                    skipped_count += 1
                    print("  [~] No Easy Apply button — job uses external apply, skipped.")

                elif status == "no_apply_button":
                    mark_job(conn, cursor, job_id, -1)
                    skipped_count += 1
                    print("  [~] No Apply button on LinkedIn page — permanently skipped.")

                elif status == "skipped":
                    mark_job(conn, cursor, job_id, -1)
                    skipped_count += 1
                    print("  [-] Skipped.")

                else:  # "failed"
                    if not auto_mode:
                        # Browser tab is still open — let user interact before moving on
                        print(f"\n{'═' * 64}")
                        print(f"  [!] Apply failed: {title or 'Unknown'}")
                        print(f"  Browser tab is still open — you can interact manually.")
                        print(f"{'═' * 64}")
                        try:
                            fail_choice = input(
                                "  [m] = I applied manually   [ENTER] = auto-fail   [s] = skip\n"
                                "  > "
                            ).strip().lower()
                        except (EOFError, KeyboardInterrupt):
                            fail_choice = ""
                        if fail_choice == "m":
                            mark_job(conn, cursor, job_id, 1)
                            applied_count += 1
                            applications.append({
                                "job_id":     job_id,
                                "title":      title or "",
                                "company":    company_name or "",
                                "url":        url,
                                "applied_at": datetime.now(timezone.utc).isoformat(),
                            })
                            print("  [~] Marked as manually applied.")
                        elif fail_choice in ("s", "skip"):
                            mark_job(conn, cursor, job_id, -1)
                            skipped_count += 1
                            print("  [-] Skipped.")
                        else:
                            mark_job(conn, cursor, job_id, -2)
                            error_count += 1
                            print("  [!] Marked as auto-failed.")
                    else:
                        mark_job(conn, cursor, job_id, -2)
                        error_count += 1
                        print("  [!] Auto-apply failed — marked as auto-failed.")
                    if error_count >= 5 and error_count > 2 * (applied_count + skipped_count):
                        print(f"\n  [!] Error rate too high ({error_count} errors vs {applied_count + skipped_count} successes) — stopping session early.")
                        break

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

        finally:
            conn.close()
            await browser.close()
            print("  Browser closed.")

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

    if all_unanswered_fields:
        print(f"\n⚠ Profile gaps — these form fields had no answer from your profile or AI:")
        for f in all_unanswered_fields:
            print(f"    • {f}")
        print("  → Add them to user_profile.json to improve future applications.")

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
    parser.add_argument("--type",         metavar="TYPE",   help="Filter by application_type (e.g. SimpleOnsiteApply,ComplexOnsiteApply).")
    parser.add_argument("--reset-failed", action="store_true", help="Reset all auto-failed jobs (applied=-2) back to pending (NULL) and exit.")
    parser.add_argument("--verbose",      action="store_true", help="Print full LLM prompt/response and save screenshots per step.")
    args = parser.parse_args()

    api_key, base_url, model, gmail_user, gmail_pass, max_auto_env, browser_api_key, browser_url, browser_model, classifier_api_key, classifier_url, classifier_model = load_env()
    client = OpenAI(api_key=api_key, base_url=base_url)

    conn   = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    migrate_db(conn, cursor)

    if args.stats:
        print_stats(cursor)
        conn.close()
        return

    if args.reset_failed:
        cursor.execute("SELECT COUNT(*) FROM jobs WHERE applied = -2")
        count = cursor.fetchone()[0]
        cursor.execute("UPDATE jobs SET applied = NULL WHERE applied = -2")
        conn.commit()
        print(f"Reset {count} auto-failed job(s) back to pending.")
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

    ineligible = skip_ineligible_jobs(conn, cursor)
    if ineligible:
        print(f"  Auto-skipped {ineligible} job(s) not matching remote/Utah criteria.")

    jobs  = get_pending_jobs(cursor, limit=args.limit, apply_type=args.type)
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
            classifier_api_key=classifier_api_key,
            classifier_url=classifier_url,
            classifier_model=classifier_model,
            verbose=args.verbose,
        )
    )


if __name__ == "__main__":
    main()
