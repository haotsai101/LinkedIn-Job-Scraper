#!/usr/bin/env python3
"""
apply_jobs.py - Fully autonomous AI job application agent.

Loads your profile once, classifies each pending job with an LLM, and uses
deterministic Playwright flows to fill and submit applications without any
human intervention (--auto mode). A session summary is written to
application_log.json and emailed via Gmail when the run finishes.

LLM configuration lives in config.py (get_llm_config). Roles:
    classifier    - job relevance scoring; OffsiteApply -> NVIDIA NIM
                    (CLASSIFIER_* env), Easy Apply -> Claude Agent SDK.
    guided_apply  - the browser agent (OffsiteApplyFlow / EasyApplyFlow) via
                    the Claude Agent SDK. Subscription auth: no API key,
                    requires a `claude` CLI login. (T14b)

Environment variables (put in a .env file or export before running):
    CLASSIFIER_API / CLASSIFIER_BASE_URL / CLASSIFIER_MODEL - NIM classifier
    GMAIL_USER          - Gmail address to send summary emails from/to
    GMAIL_APP_PASSWORD  - 16-char Google App Password (needs 2FA enabled)
    MAX_AUTO_APPLY      - Default daily cap (default: 10)
    LLM_API / LLM_URL / LLM_MODEL - OPTIONAL. Only the legacy OpenAI-backed
                    profile-setup interview (--setup) still reads them; every
                    other path resolves its own config. Also accepted as
                    deprecated aliases by config.py for one more release.

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
from urllib.parse import urlparse

from openai import OpenAI
from playwright.async_api import async_playwright

import config
import llm
import nim_client
from common import prune_debug_screenshots, rotate_llm_log
from common import write_llm_log as _write_llm_log
from linkedin_apply import EasyApplyFlow, OffsiteApplyFlow, _get_profile_value
from scripts.create_db import BLOCKED_ENTITIES_SEED, ensure_schema_current

sys.stdout.reconfigure(line_buffering=True)

# ── Config ─────────────────────────────────────────────────────────────────────

DB_PATH      = str(Path(__file__).parent / "linkedin_jobs.db")

# Blocklist patterns now live in the ``blocked_entities`` SQLite table (migrated
# by scripts/migrations/002_schema.py). ``scripts.create_db.BLOCKED_ENTITIES_SEED``
# is the single source of truth for the fallback seed rows; the two sets below are
# derived from it purely for the in-process URL check in run_session (which does a
# Python ``in`` test, never SQL interpolation). get_pending_jobs filters against
# the table with a parameterized NOT EXISTS — it does not read these constants.
BLOCKED_COMPANIES = {p for kind, p, _ in BLOCKED_ENTITIES_SEED if kind == "company"}
BLOCKED_DOMAINS = {p for kind, p, _ in BLOCKED_ENTITIES_SEED if kind == "ats_domain"}
PROFILE_PATH = "user_profile.json"
LOG_PATH     = "application_log.json"
ACCOUNTS_PATH = "created_accounts.json"

# Domains we never open a browser tab for: pure aggregators, contractor-only
# platforms, assessment mills, and known scam/broker sites. Checked against a
# job's ``posting_domain`` / ``application_url`` BEFORE the relevance classifier
# runs (T29) so a spam listing never costs an LLM call.
#
# NOTE (T28): Greenhouse domains (job-boards.greenhouse.io / boards.greenhouse.io
# / grnh.se) are deliberately NOT here. grnh.se is only a link shortener and the
# *.greenhouse.io boards host real per-company application forms; blanket-blocking
# them discarded legitimate direct-employer jobs. OffsiteApplyFlow already has
# Greenhouse iframe-embed handling plus pre-loop / mid-loop / per-step reCAPTCHA
# detectors that return "skipped" at runtime, so a genuinely CAPTCHA-walled
# Greenhouse form is still skipped cleanly — as a runtime outcome, not a blind
# pre-filter.
_OFFSITE_SPAM = (
    # Pure spam / aggregator job boards
    "jobright.ai", "sundayy.com", "scale.jobs", "dice.com",
    "mercor.com", "remotehunter.com", "haystack.cv", "talentally.com",
    "micro1.ai", "tenex.ai", "bestjobtool.com", "fetchjobs.co",
    "alignerr.com", "app.dataannotation.tech",
    "theladders.com", "hiresome.ai",
    # Assessment / crossover platforms — not real direct-hire jobs
    "ed.crossover.com", "crossover.com",
    # Recruiter broker / broken stub sites
    "peakperformers.org", "work.mercor.com", "rex.zone",
    "motionrecruitment.com", "hirecrap.com",
    "codevertexinnovations.com",                # Scam site
)


def _match_spam_domain(*candidates: str) -> str | None:
    """Return the first host among *candidates* that matches ``_OFFSITE_SPAM``.

    Accepts bare domains (``posting_domain``) or full URLs (``application_url``);
    returns ``None`` when nothing matches.
    """
    for raw in candidates:
        value = (raw or "").strip().lower()
        if not value:
            continue
        host = urlparse(value).netloc if "//" in value else value.split("/", 1)[0]
        host = host.split("@")[-1].split(":")[0]
        if host and any(host == d or host.endswith("." + d) for d in _OFFSITE_SPAM):
            return host
    return None


async def _llm_fill_focused(page, profile: dict):
    """LLM-fill whichever input field is currently focused in the browser.

    Interactive-only helper (the semi-auto ``[f]`` command). One-shot
    ``llm.query`` on the guided_apply model, same as the flows."""
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
            answer = await llm.query(
                f"Job application form field.\nLabel: {label!r}\nType: {kind}\n"
                f"Profile: {profile_line}\n\n"
                "Reply with ONLY the value to fill in this field. No explanation. "
                "CRITICAL: Never fabricate URLs, social media handles, or info not in the profile. "
                "For URL/link fields not in the profile (Twitter, Instagram, blog, etc.), reply with empty string.",
                model=config.get_llm_config("guided_apply").model,
                timeout=40,
            )
            value = (answer or "").strip()
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
            total = (s.get("error_count", 0) + s.get("applied_count", 0)
                     + s.get("skipped_count", 0) + s.get("blocked_count", 0))
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

    # No LLM_* / LLM_URL hard requirement any more (T14b). Each LLM role resolves
    # its own config:
    #   * classifier   — OffsiteApply → nim_client (config.get_llm_config("classifier"),
    #                    honours CLASSIFIER_* + legacy aliases); Easy Apply → Claude
    #                    Agent SDK subscription auth.
    #   * browser agent (OffsiteApplyFlow / EasyApplyFlow) → Claude Agent SDK
    #                    subscription auth (config "guided_apply").
    # LLM_API / LLM_URL / LLM_MODEL are now optional — read here only for the
    # legacy OpenAI-backed profile-setup interview (build_profile_interactively),
    # which degrades to raw answers when they are unset.
    return api_key, base_url, model, gmail_user, gmail_pass, max_auto_env


# ── User profile ────────────────────────────────────────────────────────────────

def load_profile():
    p = Path(PROFILE_PATH)
    if p.exists():
        return json.loads(p.read_text())
    return None


def save_profile(profile: dict):
    Path(PROFILE_PATH).write_text(json.dumps(profile, indent=2))
    print(f"Profile saved to {PROFILE_PATH}")


def build_profile_interactively(client: "OpenAI | None", model: str) -> dict:
    print("\n── Profile Setup ─────────────────────────────────────────────────────")
    print("Answer the questions below. Your profile is stored locally and used")
    print("only to fill application forms on your behalf.\n")

    raw_answers: dict[str, str] = {}
    for key, prompt in PROFILE_QUESTIONS:
        raw_answers[key] = input(f"  {prompt}:\n  > ").strip()

    if client is None or not model:
        print(
            "\n  LLM_API / LLM_URL / LLM_MODEL not set — storing your raw answers "
            "as-is. Configure them in .env and re-run `--setup` to have the "
            "profile structured (skills list, education object, etc.)."
        )
        save_profile(raw_answers)
        return raw_answers

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
    except Exception as _exc:
        print(f" failed ({_exc}). Storing raw answers — re-run `--setup` after "
              "checking LLM_API / LLM_URL / LLM_MODEL.")
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
    blocked = report.get("blocked_count", 0)
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
  <b>Blocked (needs manual apply):</b> {blocked} &nbsp;|&nbsp;
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
    """Job-relevance classifier.

    Routing (``application_type`` is a known DB column, so it is decided before
    any LLM call):

    * citizenship / clearance keyword in the description  → immediate skip,
      no LLM call on either backend.
    * ``application_type == "OffsiteApply"``              → NIM (OpenAI-compatible,
      ``config.get_llm_config("classifier")`` → meta/llama-3.2-11b-vision-instruct
      @ NVIDIA NIM). Free. ``run_session`` may override this to the Agent SDK for
      the rest of a session once the NIM route trips its circuit breaker
      (see :func:`classify_with_circuit_breaker`).
    * ``SimpleOnsiteApply`` / ``ComplexOnsiteApply`` / anything else (incl.
      ``None``)                                          → Claude Agent SDK
      (``llm.query_json``, one isolated one-shot session per job, subscription auth).

    Both LLM paths ask for structured JSON output but still salvage a stray code
    fence / prose wrapper before giving up. Each LLM call is retried once on a
    transient failure (:meth:`_run_with_retry`); a hard failure propagates to
    ``run_session``, which leaves the job pending and only aborts the batch
    after several classifications fail in a row.
    """

    # Cheap Claude model for the Agent-SDK classifier path (Easy Apply jobs).
    _AGENT_MODEL = "claude-haiku-4-5"

    _SYSTEM = """You are a job application assistant helping the user review LinkedIn job listings.

User profile:
{profile}

Classify whether a job is relevant (software engineering, AI/ML, data engineering/science/analytics).
Be accurate and concise. Never fabricate information not in the user's profile."""

    # JSON Schema shared by both routes (Agent SDK native structured output +
    # the prompt hint the NIM json_object mode is steered with).
    _CLASSIFY_SCHEMA = {
        "type": "object",
        "properties": {
            "relevant": {"type": "boolean"},
            "reason": {"type": "string"},
            "citizenship_required": {"type": "boolean"},
        },
        "required": ["relevant", "reason", "citizenship_required"],
        "additionalProperties": False,
    }

    _AGENT_PROMPT = (
        "Review this job posting on two dimensions:\n"
        "1. Is it related to software engineering, AI/ML, or data (engineering/science/analytics)?\n"
        "2. Does it explicitly require US citizenship or an active US security clearance "
        "(e.g. 'must be a US citizen', 'TS/SCI required', 'active Secret clearance')?\n\n"
        "Title: {title}\n\n"
        "Description:\n{description}\n\n"
        'Respond with JSON: {{"relevant": true|false, "reason": "<one sentence>", '
        '"citizenship_required": true|false}}'
    )

    # Keyword patterns that unambiguously require US citizenship — checked before any LLM call
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

    # A transient LLM call is retried this many times total before the error
    # propagates to run_session.
    _CALL_ATTEMPTS = 2
    _RETRY_DELAY_S = 2.0
    # Wall-clock ceiling for ONE classify attempt. Each attempt in
    # :meth:`_run_with_retry` gets its own deadline, so a slow-but-alive backend
    # still gets a fresh retry instead of being starved by a single outer wait.
    _ATTEMPT_TIMEOUT_S = 40.0

    def __init__(self, profile: dict):
        self._system = self._SYSTEM.format(profile=json.dumps(profile, indent=2))
        self._nim_client = None
        self._nim_model = ""

    async def classify(
        self, title: str, description: str, application_type: str | None,
        *, prefer_agent_sdk: bool = False,
    ) -> tuple[bool, str, bool]:
        """Returns (relevant, reason, citizenship_required).

        ``prefer_agent_sdk`` forces the Claude Agent SDK route even for
        ``OffsiteApply`` jobs — used by ``run_session`` after the NIM classifier
        route trips its circuit breaker. The citizenship keyword fast-path still
        runs first regardless.
        """
        desc = description or ""

        # Fast path: keyword scan on full description before paying for an LLM call
        desc_lower = desc.lower()
        for kw in self._CITIZENSHIP_KEYWORDS:
            if kw in desc_lower:
                reason = f"Requires US citizenship or active clearance ({kw})"
                self._log(title, "keyword", "keyword", None, None,
                          {"relevant": False, "reason": reason, "citizenship_required": True})
                return False, reason, True

        if application_type == "OffsiteApply" and not prefer_agent_sdk:
            return await self._classify_nim(title, desc)
        return await self._classify_agent(title, desc)

    async def _run_with_retry(self, factory):
        """Await ``factory()`` under a per-attempt ``_ATTEMPT_TIMEOUT_S`` deadline,
        retrying once on a *transient* failure (transport blip / 5xx).

        A ``TimeoutError`` is **not** retried: a route that already blew a 40s
        deadline almost never answers inside a second one, and retrying it just
        doubles the wall-clock before ``run_session`` can abort a degraded
        session. It propagates immediately so the caller (the circuit breaker)
        can fall back to the other route.

        ``NimConfigError`` is deterministic (missing key), so it too propagates
        immediately without a retry.
        """
        for attempt in range(1, self._CALL_ATTEMPTS + 1):
            try:
                return await asyncio.wait_for(factory(), self._ATTEMPT_TIMEOUT_S)
            except nim_client.NimConfigError:
                raise
            except Exception as exc:
                if attempt >= self._CALL_ATTEMPTS or isinstance(exc, TimeoutError):
                    raise
                print(f"\n  [classifier] attempt {attempt} failed ({exc}) — retrying…",
                      flush=True)
                await asyncio.sleep(self._RETRY_DELAY_S)

    async def _classify_nim(self, title: str, desc: str) -> tuple[bool, str, bool]:
        if self._nim_client is None:
            # Raises NimConfigError if no classifier key is resolvable — surfaced
            # to run_session, which leaves the job pending.
            self._nim_client, self._nim_model = nim_client.resolve_classifier()
        t0 = time.monotonic()
        data = await self._run_with_retry(
            lambda: asyncio.to_thread(
                nim_client.classify_via_nim,
                self._nim_client, self._nim_model, title, desc,
            )
        )
        return self._finalize(title, "nim", self._nim_model,
                              int((time.monotonic() - t0) * 1000), data)

    async def _classify_agent(self, title: str, desc: str) -> tuple[bool, str, bool]:
        prompt = self._AGENT_PROMPT.format(title=title, description=desc[:3000])
        t0 = time.monotonic()
        # One-shot isolated session per job — NOT a persistent client. See
        # llm.query_json / issue #560.
        data = await self._run_with_retry(
            lambda: llm.query_json(
                prompt, self._CLASSIFY_SCHEMA,
                model=self._AGENT_MODEL, system=self._system, log_calls=False,
            )
        )
        return self._finalize(title, "agent_sdk", self._AGENT_MODEL,
                              int((time.monotonic() - t0) * 1000), data)

    def _finalize(self, title, route, model, duration_ms, data) -> tuple[bool, str, bool]:
        """Coerce a raw classifier JSON object into the return tuple + log it.

        Shared by both routes so the citizenship override and telemetry shape
        stay identical.
        """
        relevant = bool(data.get("relevant"))
        reason = str(data.get("reason", ""))
        citizenship_required = bool(data.get("citizenship_required", False))
        if relevant and ("not relevant" in reason.lower() or "not related" in reason.lower()):
            relevant = False
        if citizenship_required:
            relevant = False
        result = {
            "relevant": relevant,
            "reason": reason,
            "citizenship_required": citizenship_required,
        }
        self._log(title, route, model, duration_ms, data, result)
        return relevant, reason, citizenship_required

    @staticmethod
    def _log(title, route, model, duration_ms, raw, result) -> None:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "type": "classifier",
            "route": route,
            "model": model,
            "title": title,
            "result": result,
        }
        if duration_ms is not None:
            entry["duration_ms"] = duration_ms
        if raw is not None:
            entry["raw_response"] = raw
        _write_llm_log(entry)


# ── Classifier circuit breaker ─────────────────────────────────────────────────

# Consecutive NIM-route classify timeouts within one session before every
# remaining OffsiteApply job is routed through the Agent SDK instead.
_MAX_NIM_TIMEOUT_STREAK = 2
_NIM_DEGRADED_MSG = (
    "NIM classifier route degraded — falling back to Agent SDK for OffsiteApply"
)


def _new_classifier_breaker() -> dict:
    """Fresh, session-scoped circuit-breaker state. One per ``run_session``."""
    return {"nim_timeout_streak": 0, "nim_route_degraded": False}


async def classify_with_circuit_breaker(
    agent: "JobAgent", breaker: dict,
    title: str, description: str, application_type: str | None,
) -> tuple[bool, str, bool]:
    """Classify one job, applying the per-session NIM-route circuit breaker.

    ``breaker`` is the mutable dict from :func:`_new_classifier_breaker`; this
    function updates it in place.

    Behaviour:
      * Non-OffsiteApply jobs are unaffected — straight to ``agent.classify``
        (which uses the Agent SDK for them anyway).
      * OffsiteApply job, route healthy: try NIM. On ``TimeoutError``,
        bump the streak, classify *this* job via the Agent SDK instead, and — at
        ``_MAX_NIM_TIMEOUT_STREAK`` consecutive timeouts — flip the route to
        "degraded" and log it once.
      * OffsiteApply job, route degraded: straight to the Agent SDK, no wasted
        NIM attempt.

    A NIM timeout that the Agent SDK then classifies successfully does NOT raise
    — so the caller does not count it as a classifier failure. Only a genuine
    failure (Agent SDK also fails, or a non-timeout error) propagates.
    """
    is_offsite = (application_type or "") == "OffsiteApply"

    if not is_offsite or breaker["nim_route_degraded"]:
        return await agent.classify(
            title, description, application_type, prefer_agent_sdk=is_offsite,
        )

    try:
        result = await agent.classify(title, description, application_type)
    except TimeoutError:
        breaker["nim_timeout_streak"] += 1
        streak = breaker["nim_timeout_streak"]
        just_degraded = (
            streak >= _MAX_NIM_TIMEOUT_STREAK and not breaker["nim_route_degraded"]
        )
        if just_degraded:
            breaker["nim_route_degraded"] = True
            _write_llm_log({
                "ts": datetime.now(timezone.utc).isoformat(),
                "type": "classifier_route_degraded",
                "note": _NIM_DEGRADED_MSG,
                "nim_timeout_streak": streak,
            })
        print(
            f"\n  [!] NIM classifier timed out ({streak}/{_MAX_NIM_TIMEOUT_STREAK}) "
            f"— classifying this job via the Agent SDK instead."
            + (f"\n  [!] {_NIM_DEGRADED_MSG}" if just_degraded else "")
        )
        return await agent.classify(
            title, description, application_type, prefer_agent_sdk=True,
        )
    else:
        breaker["nim_timeout_streak"] = 0
        return result


# ── Database helpers ────────────────────────────────────────────────────────────

def migrate_db(conn, cursor):
    cursor.execute("PRAGMA table_info(jobs)")
    cols = {row[1] for row in cursor.fetchall()}
    if "applied" not in cols:
        cursor.execute("ALTER TABLE jobs ADD COLUMN applied INTEGER DEFAULT NULL")
        conn.commit()
        print("DB migrated: added 'applied' column.")


def _ensure_apply_schema(cursor):
    """Bring an apply-agent DB up to the current schema baseline. Idempotent + cheap.

    Thin wrapper: the canonical schema-modernization logic now lives in
    ``scripts.create_db.ensure_schema_current`` (T23 — killed the dual
    implementation that used to live here). Kept as a named function because
    ``get_pending_jobs`` and the tests reference it.

    ``ensure_schema_current`` adds ``jobs.listed_epoch`` + backfills, creates and
    seeds ``blocked_entities``, and — when it changed the schema — rebuilds every
    secondary index (so ``idx_jobs_listed`` lands on ``(applied, listed_epoch
    DESC)`` even on an apply-only clone that never runs a retriever). That makes
    the ``_has_index`` check in ``get_pending_jobs`` a safety net rather than
    load-bearing.
    """
    # TODO(T19): consolidate with the startup auto-migrator
    ensure_schema_current(cursor.connection, cursor)


def _has_index(cursor, name: str) -> bool:
    return cursor.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (name,)
    ).fetchone() is not None


def get_pending_jobs(cursor, limit=None, apply_type=None, include_failed=False):
    """Return pending apply candidates, newest first.

    Every dynamic value is passed as a bound parameter. The pieces of SQL
    assembled by string concatenation below are fixed fragments chosen by a
    branch (the ``applied`` predicate, the optional index hint) and an
    ``IN (?, ?, ...)`` placeholder list (structure, not values) — no caller data
    is ever interpolated. Blocked companies are filtered via a parameterized
    NOT EXISTS against the ``blocked_entities`` table rather than interpolated
    LIKE clauses.
    """
    _ensure_apply_schema(cursor)

    params: list = []

    # Fixed fragments — not interpolated values. The no-OR form on the hot
    # (``--auto``) path lets a single seek on idx_jobs_listed satisfy both the
    # predicate and the ORDER BY; the OR form used by the interactive path forces
    # a small TEMP B-TREE sort (only pending + failed rows) and is left unhinted.
    if include_failed:
        applied_clause = "(j.applied IS NULL OR j.applied = -2)"
        index_hint = " "
    else:
        applied_clause = "j.applied IS NULL"
        index_hint = (
            " INDEXED BY idx_jobs_listed " if _has_index(cursor, "idx_jobs_listed") else " "
        )

    type_clause = ""
    if apply_type:
        types = [t.strip() for t in apply_type.split(",") if t.strip()]
        if types:
            type_clause = " AND j.application_type IN (" + ", ".join(["?"] * len(types)) + ")"
            params.extend(types)

    query = (
        "SELECT j.job_id, j.title, j.job_posting_url, j.location, "
        "       j.formatted_experience_level, j.description, "
        "       COALESCE(c.name, '') AS company_name, "
        "       j.application_type, "
        "       COALESCE(j.posting_domain, '') AS posting_domain, "
        "       COALESCE(j.application_url, '') AS application_url "
        "FROM jobs j" + index_hint +
        "LEFT JOIN companies c ON j.company_id = c.company_id "
        "WHERE j.scraped > 0 "
        "  AND " + applied_clause + " "
        "  AND ( "
        "      j.remote_allowed = 1 "
        "      OR LOWER(j.location) LIKE '%remote%' "
        "      OR LOWER(j.location) LIKE '%utah%' "
        "      OR LOWER(j.location) LIKE '%, ut%' "
        "  )"
        + type_clause +
        "  AND NOT EXISTS ( "
        "      SELECT 1 FROM blocked_entities be "
        "      WHERE be.kind = 'company' "
        "        AND be.pattern <> '' "
        "        AND LOWER(COALESCE(c.name, '')) LIKE '%' || LOWER(be.pattern) || '%' "
        "  ) "
        "ORDER BY j.listed_epoch DESC"
    )
    if limit:
        query += " LIMIT ?"
        params.append(int(limit))
    cursor.execute(query, params)
    return cursor.fetchall()


def mark_job(conn, cursor, job_id: int, status: int):
    """status: 1=applied, -1=skipped, -2=auto-failed, -3=blocked (no auto-retry)."""
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
            SUM(CASE WHEN applied = -2 THEN 1 ELSE 0 END),
            SUM(CASE WHEN applied = -3 THEN 1 ELSE 0 END)
        FROM jobs
    """)
    pending, applied, skipped, failed, blocked = cursor.fetchone()
    print(
        f"\nStats — Pending: {pending or 0}  Applied: {applied or 0}  "
        f"Skipped: {skipped or 0}  Auto-failed: {failed or 0}  Blocked: {blocked or 0}"
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
    conn,
    cursor,
    *,
    auto_mode: bool = False,
    max_apply: int = 10,
    gmail_user: str = "",
    gmail_pass: str = "",
    verbose: bool = False,
):
    # JobAgent owns its own classifier backends: NIM for OffsiteApply, a fresh
    # one-shot Claude Agent SDK session per job for Easy Apply. Nothing to close.
    agent = JobAgent(profile)
    # Consecutive classification failures — a transient blip leaves one job
    # pending and moves on; a run of them means something systemic, so bail.
    classify_fail_streak = 0
    _MAX_CLASSIFY_FAIL_STREAK = 3
    # NIM-route circuit breaker — session-scoped, resets each run_session.
    classifier_breaker = _new_classifier_breaker()

    inbox = EmailInbox(gmail_user, gmail_pass) if gmail_user and gmail_pass else None

    started_at   = datetime.now(timezone.utc).isoformat()
    session_date = datetime.now().strftime("%Y-%m-%d")
    applications: list[dict] = []
    applied_count = skipped_count = error_count = 0
    # Jobs left pending because classification failed/timed out — NOT skipped
    # (no mark_job), tracked separately so "Skipped: N" does not over-report.
    deferred_count = 0
    # Jobs on a blocked ATS / login wall — marked applied=-3 ("we chose not to
    # try, don't auto-retry"). Distinct from -2 auto-fails so --reset-failed
    # leaves them alone and the error-rate circuit breaker doesn't trip on them.
    blocked_count = 0

    # Write a session boundary marker so log-monitor agents can filter to just this run
    _write_llm_log({
        "ts":   started_at,
        "type": "session_start",
        "auto_mode": auto_mode,
        "total_jobs": total,
    })
    all_unanswered_fields: list[str] = []

    # Skip LinkedIn login when every job in the queue is OffsiteApply —
    # those jobs navigate directly to the company ATS via application_url.
    _all_offsite = all(row[7] == "OffsiteApply" for row in jobs)
    if _all_offsite:
        print("\nOpening browser (OffsiteApply only — skipping LinkedIn login)…")
    else:
        print("\nOpening browser and signing into LinkedIn…")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(
            permissions=[],  # deny browser notification/location prompts
        )
        page    = await context.new_page()

        if not _all_offsite:
            try:
                await login_linkedin_playwright(page)
                print("  Session ready.\n")
            except Exception as _login_err:
                await browser.close()
                raise
        else:
            print("  Session ready.\n")

        if not _check_recent_session_health():
            print("\n  [!] Warning: the last 3 sessions all had >80% error rates.")
            print("  Check llm_debug.jsonl and application_log.json for a systematic blocker before continuing.\n")

        try:
            for idx, row in enumerate(jobs, 1):
                job_id, title, job_url, location, exp_level, description, company_name, application_type, posting_domain, application_url = row
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

                # Spam / aggregator domains are skipped BEFORE the classifier
                # runs (T29) — a spam listing must never cost an LLM call.
                if (application_type or "") == "OffsiteApply":
                    _spam_host = _match_spam_domain(posting_domain, application_url)
                    if _spam_host:
                        print(f"  [skip] Spam/aggregator domain ({_spam_host}) — "
                              f"skipped before classification.")
                        mark_job(conn, cursor, job_id, -1)
                        skipped_count += 1
                        continue

                print("  Classifying…", end="", flush=True)
                try:
                    relevant, reason, citizenship_required = await classify_with_circuit_breaker(
                        agent, classifier_breaker,
                        title or "", description or "", application_type,
                    )
                    classify_fail_streak = 0
                except nim_client.NimConfigError as exc:
                    # Missing / bad CLASSIFIER_API — deterministic, won't fix
                    # itself, and interleaved EasyApply successes would keep
                    # resetting the streak while every OffsiteApply job is
                    # silently skipped. Abort now.
                    print(f"\n  [!] NIM classifier misconfigured ({exc}) — stopping session.")
                    break
                except Exception as exc:
                    # Both classifier routes failed for this job (the circuit
                    # breaker already tried the Agent SDK fallback on a NIM
                    # timeout). Leave the job pending — do NOT mark_job, do NOT
                    # count it as skipped.
                    classify_fail_streak += 1
                    what = ("timed out" if isinstance(exc, TimeoutError)
                            else f"failed ({exc})")
                    print(f"\n  [!] Classifier {what} — leaving job pending "
                          f"({classify_fail_streak}/{_MAX_CLASSIFY_FAIL_STREAK}).")
                    deferred_count += 1
                    if classify_fail_streak >= _MAX_CLASSIFY_FAIL_STREAK:
                        print("  [!] Too many consecutive classifier failures — "
                              "likely systemic; stopping session.")
                        break
                    continue
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
                                await _llm_fill_focused(page, profile)
                                continue
                            outcome_ref["status"] = "applied"
                            return "applied"
                    return ready_to_submit

                async def _fill_focused_cb():
                    await _llm_fill_focused(page, profile)

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
                    # Spam/aggregator domains were already filtered before the
                    # classifier call (see _match_spam_domain above).
                    flow = OffsiteApplyFlow(
                        page=page,
                        context=context,
                        profile=profile,
                        auto_mode=auto_mode,
                        callbacks=callbacks,
                        generated_password=_generate_password(),
                        company_name=company_name or "",
                        job_title=title or "",
                        job_description=description or "",
                        verbose=verbose,
                        inbox=inbox,
                        application_url=application_url or "",
                    )
                else:
                    flow = EasyApplyFlow(
                        page=page,
                        profile=profile,
                        auto_mode=auto_mode,
                        callbacks=callbacks,
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
                        auto_mode=auto_mode,
                        callbacks=callbacks,
                        generated_password=_generate_password(),
                        company_name=company_name or "",
                        job_title=title or "",
                        job_description=description or "",
                        verbose=verbose,
                        inbox=inbox,
                        application_url=application_url or "",
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

                elif status == "blocked":
                    # Blocked ATS / login wall / dead-end domain. -3 keeps it out
                    # of the --reset-failed retry pool (unlike -2) — a human has
                    # to apply manually.
                    mark_job(conn, cursor, job_id, -3)
                    blocked_count += 1
                    print("  [~] Blocked — needs a manual apply, will not auto-retry.")

                else:  # "failed"
                    if not auto_mode:
                        # Browser tab is still open — let user interact before moving on
                        print(f"\n{'═' * 64}")
                        print(f"  [!] Apply failed: {title or 'Unknown'}")
                        print(f"  Browser tab is still open — you can interact manually.")
                        print(f"{'═' * 64}")
                        while True:
                            try:
                                fail_choice = input(
                                    "  [r] = retry (agent fills form)   [f] = fill focused field   "
                                    "[m] = I applied manually   [s] = skip   [ENTER] = auto-fail\n"
                                    "  > "
                                ).strip().lower()
                            except (EOFError, KeyboardInterrupt):
                                fail_choice = ""
                            if fail_choice == "f":
                                # Fill the focused field on whichever tab the user is looking at
                                _active_pg = page
                                for _pg in context.pages:
                                    try:
                                        if not _pg.is_closed() and "linkedin.com" not in _pg.url and _pg.url not in ("", "about:blank"):
                                            _active_pg = _pg
                                            break
                                    except Exception:
                                        continue
                                await _llm_fill_focused(_active_pg, profile)
                                continue
                            if fail_choice == "r" and isinstance(flow, OffsiteApplyFlow):
                                try:
                                    input("  Navigate to the application form in the browser, then press ENTER…")
                                except (EOFError, KeyboardInterrupt):
                                    pass
                                try:
                                    status = await asyncio.wait_for(flow.assist_from_page(), timeout=600)
                                except asyncio.TimeoutError:
                                    status = "failed"
                                except Exception as _retry_exc:
                                    print(f"  [!] Retry error: {_retry_exc}")
                                    status = "failed"
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
                                    break
                                elif status in ("skipped", "expired", "no_apply_button"):
                                    mark_job(conn, cursor, job_id, -1)
                                    skipped_count += 1
                                    print("  [-] Skipped.")
                                    break
                                elif status == "blocked":
                                    mark_job(conn, cursor, job_id, -3)
                                    blocked_count += 1
                                    print("  [~] Blocked — needs a manual apply, will not auto-retry.")
                                    break
                                else:
                                    print("  [!] Retry also failed — choose again.")
                                    continue
                            elif fail_choice == "m":
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
                                break
                            elif fail_choice in ("s", "skip"):
                                mark_job(conn, cursor, job_id, -1)
                                skipped_count += 1
                                print("  [-] Skipped.")
                                break
                            else:
                                mark_job(conn, cursor, job_id, -2)
                                error_count += 1
                                print("  [!] Marked as auto-failed.")
                                break
                    else:
                        mark_job(conn, cursor, job_id, -2)
                        error_count += 1
                        print("  [!] Auto-apply failed — marked as auto-failed.")
                    _progress = applied_count + skipped_count + blocked_count
                    if error_count >= 5 and error_count > 2 * _progress:
                        print(f"\n  [!] Error rate too high ({error_count} errors vs {_progress} handled) — stopping session early.")
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
        "blocked_count": blocked_count,
        "deferred_count": deferred_count,
        "auto_mode":     auto_mode,
        "applications":  applications,
    }

    print(f"\n{'═' * 64}")
    _deferred_note = f"  Deferred: {deferred_count}" if deferred_count else ""
    _blocked_note = f"  Blocked: {blocked_count}" if blocked_count else ""
    print(f"Session complete — Applied: {applied_count}  Skipped: {skipped_count}  "
          f"Errors: {error_count}{_blocked_note}{_deferred_note}")

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

    api_key, base_url, model, gmail_user, gmail_pass, max_auto_env = load_env()

    conn   = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    migrate_db(conn, cursor)

    if args.stats:
        print_stats(cursor)
        conn.close()
        return

    if args.reset_failed:
        # Only -2 (auto-failed) is retryable. -3 (blocked ATS / login wall) is
        # deliberately left alone — those need a human, not another agent run.
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

    # An actual apply session starts here (the --stats / --reset-failed /
    # --accounts branches above have already returned). Keep the unbounded
    # artifacts in check before we start writing more of them (ticket T3).
    rotate_llm_log()
    prune_debug_screenshots()

    profile = load_profile()
    if profile is None or args.setup:
        # The profile-structuring interview is the only path that still uses the
        # legacy OpenAI-compatible LLM_* endpoint; build the client lazily here
        # so a missing LLM_API / LLM_URL never blocks --stats / --auto runs.
        setup_client = None
        if api_key and base_url:
            try:
                setup_client = OpenAI(api_key=api_key, base_url=base_url)
            except Exception as _oc_exc:
                print(f"  [setup] Could not init LLM client ({_oc_exc}).")
        profile = build_profile_interactively(setup_client, model)

    ineligible = skip_ineligible_jobs(conn, cursor)
    if ineligible:
        print(f"  Auto-skipped {ineligible} job(s) not matching remote/Utah criteria.")

    jobs  = get_pending_jobs(cursor, limit=args.limit, apply_type=args.type, include_failed=not args.auto)
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
            jobs, total, profile, conn, cursor,
            auto_mode=args.auto,
            max_apply=max_apply,
            gmail_user=gmail_user,
            gmail_pass=gmail_pass,
            verbose=args.verbose,
        )
    )


if __name__ == "__main__":
    main()
