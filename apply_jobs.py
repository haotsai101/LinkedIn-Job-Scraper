#!/usr/bin/env python3
"""
apply_jobs.py - AI-assisted job application reviewer.

Loads your profile once, then iterates through scraped listings. The agent
automatically classifies each job (software / AI / data) and skips irrelevant
ones. Relevant jobs open in a browser window where you can browse, navigate to
the application form, and ask the agent to fill fields for you.

Environment variables (put in a .env file or export before running):
    LLM_API    - API key for the LLM endpoint
    LLM_URL    - Base URL of the OpenAI-compatible API
    LLM_MODEL  - Model name (default: gpt-4o-mini)

Usage:
    python apply_jobs.py              # Review all pending jobs
    python apply_jobs.py --limit 20   # Cap session to 20 jobs
    python apply_jobs.py --stats      # Print counts and exit
    python apply_jobs.py --setup      # Re-run profile interview

Commands while reviewing a listing:
    fill [instruction]   - Agent reads the page and fills form fields
    ask  [question]      - Ask the agent anything about the listing
    done / d / ENTER     - Mark as applied, go to next
    skip / s             - Skip this listing, go to next
    quit / q             - Save progress and exit
"""

import argparse
import csv
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

from openai import OpenAI
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import Select

# ── Config ─────────────────────────────────────────────────────────────────────

DB_PATH = "linkedin_jobs.db"
PROFILE_PATH = "user_profile.json"
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
# Must use top-level `return` — Selenium executes the script as an anonymous
# function body, so an IIFE's return value is NOT forwarded to Python.
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

# JS to grab visible body text (capped to avoid bloating the LLM context)
_PAGE_TEXT_JS = """
var el = document.body;
return el ? el.innerText.replace(/\\s+/g, ' ').trim().substring(0, 3000) : '';
"""


def _read_page_context(driver) -> str:
    """Return a short summary of the active browser tab for the agent."""
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

    api_key = os.environ.get("LLM_API", "").strip()
    base_url = os.environ.get("LLM_URL", "").strip()
    model    = os.environ.get("LLM_MODEL", "gpt-4o-mini").strip()

    if not api_key or not base_url:
        sys.exit("Error: LLM_API and LLM_URL must be set (in .env or environment).")

    return api_key, base_url, model


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
    """Run a terminal interview, then ask the LLM to structure the raw answers."""
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
        # Fallback: store raw answers if json_object mode unsupported
        profile = raw_answers

    print(" done.")
    save_profile(profile)
    return profile


# ── LLM Agent ──────────────────────────────────────────────────────────────────

class JobAgent:
    """
    Stateful LLM agent that:
      - classifies jobs as relevant (software / AI / data) or not
      - fills application form fields from the user's profile
      - answers ad-hoc questions about the current listing

    Conversation history is reset for each new listing so context stays focused.
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

    # ── internal ──────────────────────────────────────────────────────────────

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
            # Some endpoints don't support json_object; retry without it
            if json_mode:
                kwargs.pop("response_format")
                resp = self.client.chat.completions.create(**kwargs)
            else:
                raise e

        reply = resp.choices[0].message.content
        self._history.append({"role": "user",      "content": user_message})
        self._history.append({"role": "assistant",  "content": reply})
        return reply

    # ── public API ────────────────────────────────────────────────────────────

    def reset_for_job(self, job_context: str):
        """Start a fresh conversation for the given job listing."""
        self._history = [
            {"role": "system", "content": f"Current job listing:\n{job_context}"}
        ]

    def classify(self, title: str, description: str) -> tuple[bool, str]:
        """
        Return (is_relevant, reason).
        relevant = True  →  job is software / AI / data related.
        """
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
            return bool(data.get("relevant")), data.get("reason", "")
        except Exception as exc:
            return True, f"Classification failed ({exc}); opening anyway."

    def fill_form(self, driver, instruction: str) -> str:
        """
        Read visible form fields from the current page and fill them using the
        user's profile. Returns a human-readable summary of what was filled.
        """
        try:
            raw_fields = driver.execute_script(_FORM_FIELDS_JS)
            if not raw_fields:
                return "No fillable fields found on the current page (script returned nothing)."
            fields: list[dict] = json.loads(raw_fields)
        except Exception as exc:
            return f"Could not read page fields: {exc}"

        if not fields:
            return "No fillable fields found on the current page."

        prompt = (
            f"Instruction from user: {instruction or 'fill everything you can from my profile'}\n\n"
            f"Visible form fields (JSON):\n{json.dumps(fields, indent=2)}\n\n"
            "Return JSON: {\"fills\": [{\"index\": <int>, \"value\": \"<string>\"}]}\n"
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

        # Re-query elements fresh (avoids stale references after partial DOM updates)
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
        """
        General-purpose chat. When driver is provided the agent sees the
        current browser tab (URL, title, visible text) before answering.
        """
        if driver is not None:
            page_ctx = _read_page_context(driver)
            message = f"{page_ctx}\n\nUser message: {message}"
        return self._complete(message)


# ── Database helpers ────────────────────────────────────────────────────────────

def migrate_db(conn, cursor):
    cursor.execute("PRAGMA table_info(jobs)")
    if "applied" not in {row[1] for row in cursor.fetchall()}:
        cursor.execute("ALTER TABLE jobs ADD COLUMN applied INTEGER DEFAULT NULL")
        conn.commit()
        print("DB migrated: added 'applied' column.")


def get_pending_jobs(cursor, limit=None):
    query = """
        SELECT job_id, title, job_posting_url, location, formatted_experience_level, description
        FROM jobs
        WHERE scraped > 0
          AND applied IS NULL
          AND (
              remote_allowed = 1
              OR LOWER(location) LIKE '%remote%'
              OR LOWER(location) LIKE '%utah%'
              OR LOWER(location) LIKE '%, ut%'
          )
        ORDER BY scraped DESC
    """
    if limit:
        query += f" LIMIT {int(limit)}"
    cursor.execute(query)
    return cursor.fetchall()


def mark_job(conn, cursor, job_id: int, status: int):
    """status: 1 = applied, -1 = skipped."""
    cursor.execute("UPDATE jobs SET applied = ? WHERE job_id = ?", (status, job_id))
    conn.commit()


def print_stats(cursor):
    cursor.execute("""
        SELECT
            SUM(CASE WHEN applied IS NULL AND scraped > 0 THEN 1 ELSE 0 END),
            SUM(CASE WHEN applied =  1 THEN 1 ELSE 0 END),
            SUM(CASE WHEN applied = -1 THEN 1 ELSE 0 END)
        FROM jobs
    """)
    pending, applied, skipped = cursor.fetchone()
    print(f"\nStats — Pending: {pending or 0}  Applied: {applied or 0}  Skipped: {skipped or 0}")


# ── Browser & LinkedIn login ────────────────────────────────────────────────────

_LOGIN_URL = "https://www.linkedin.com/checkpoint/rm/sign-in-another-account"
_AUTH_HOSTPATHS = ("/login", "/checkpoint", "/uas/")


def _get_login_credentials() -> tuple[str, str]:
    """
    Read the first usable credential from logins.csv.
    Prefers method='apply', falls back to method='search', then any row.
    """
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

    # Fallback: any row
    if rows:
        return rows[0]["emails"].strip(), rows[0]["passwords"].strip()

    sys.exit("No credentials found in logins.csv.")


def _is_on_auth_page(driver) -> bool:
    """Return True while the browser is still on a LinkedIn login/checkpoint page."""
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
    """
    Sign into LinkedIn using credentials from logins.csv.

    Submits the form, then waits up to 15 s for the redirect to complete.
    If LinkedIn presents a CAPTCHA or verification challenge, the user is
    prompted in the terminal to complete it manually before continuing.
    """
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

    # Wait for redirect away from auth pages (up to 15 s)
    for _ in range(15):
        time.sleep(1)
        if not _is_on_auth_page(driver):
            print(" done.")
            return

    # Still on an auth page — could be a CAPTCHA or 2-FA challenge
    print("\n  LinkedIn requires additional verification (CAPTCHA / 2-FA).")
    input("  Complete it in the browser window, then press ENTER to continue…")


# ── Main ────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="AI-assisted LinkedIn job reviewer.")
    parser.add_argument("--limit", type=int,    default=None, help="Max jobs to review this session.")
    parser.add_argument("--stats", action="store_true",       help="Print stats and exit.")
    parser.add_argument("--setup", action="store_true",       help="Re-run profile setup interview.")
    args = parser.parse_args()

    # ── Bootstrap ──────────────────────────────────────────────────────────────
    api_key, base_url, model = load_env()
    client = OpenAI(api_key=api_key, base_url=base_url)

    conn   = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    migrate_db(conn, cursor)

    if args.stats:
        print_stats(cursor)
        conn.close()
        return

    # ── Profile ────────────────────────────────────────────────────────────────
    profile = load_profile()
    if profile is None or args.setup:
        profile = build_profile_interactively(client, model)

    agent = JobAgent(client, model, profile)

    # ── Job queue ──────────────────────────────────────────────────────────────
    jobs  = get_pending_jobs(cursor, limit=args.limit)
    total = len(jobs)

    if total == 0:
        print("No pending jobs to review.")
        print_stats(cursor)
        conn.close()
        return

    print(f"\nFound {total} unreviewed job(s).")
    print("The agent will classify each listing; irrelevant ones are auto-skipped.\n")
    print("Commands while reviewing:")
    print("  fill [instruction]  — agent fills form fields on the current page")
    print("  ask  [question]     — ask the agent about the listing")
    print("  done / d / ENTER    — mark as applied, next listing")
    print("  skip / s            — skip, next listing")
    print("  quit / q            — save and exit\n")

    driver = get_driver()
    login_linkedin(driver)
    applied_count = skipped_count = 0

    try:
        for idx, (job_id, title, job_url, location, exp_level, description) in enumerate(jobs, 1):
            url = job_url or f"https://www.linkedin.com/jobs/view/{job_id}/"

            # ── Classification ────────────────────────────────────────────────
            print(f"\n{'─' * 64}")
            print(f"  [{idx}/{total}]  {title or 'Unknown'}")
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

            # ── Open in browser ───────────────────────────────────────────────
            print(f"  Opening: {url}")
            try:
                driver.get(url)
            except Exception as exc:
                print(f"  [!] Browser error: {exc}")

            agent.reset_for_job(
                f"Title: {title}\nLocation: {location}\nLevel: {exp_level}\nURL: {url}\n"
                f"Description excerpt:\n{(description or '')[:1200]}"
            )

            print(f"{'─' * 64}")

            # ── Interactive loop ───────────────────────────────────────────────
            while True:
                try:
                    raw = input("  > ").strip()
                except (EOFError, KeyboardInterrupt):
                    raw = "quit"

                if not raw:
                    # bare ENTER = done/applied
                    raw = "done"

                lower = raw.lower()

                if lower in ("quit", "q"):
                    print(f"\nQuitting. Applied: {applied_count}, Skipped: {skipped_count}")
                    return

                elif lower in ("done", "d", "applied", "next"):
                    mark_job(conn, cursor, job_id, 1)
                    applied_count += 1
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
                    # Treat any other input as a free-form chat message
                    reply = agent.chat(raw, driver=driver)
                    print(f"\n  {reply}\n")

    finally:
        driver.quit()
        conn.close()

    print(f"\nSession complete. Applied: {applied_count}, Skipped: {skipped_count} (of {total} reviewed).")


if __name__ == "__main__":
    main()
