"""
linkedin_apply.py — Deterministic Playwright LinkedIn job application engine.

EasyApplyFlow   : LinkedIn Easy Apply modal (SimpleOnsiteApply, ComplexOnsiteApply)
OffsiteApplyFlow: External company career site (OffsiteApply)
"""

import asyncio
import json
import os
import random
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from openai import OpenAI
from playwright.async_api import Page, BrowserContext


_LLM_LOG_PATH = "llm_debug.jsonl"


def _write_llm_log(entry: dict):
    import json as _json
    with open(_LLM_LOG_PATH, "a") as f:
        f.write(_json.dumps(entry) + "\n")


async def _human_type(el, value: str):
    """Type text character-by-character with random delays to mimic human input."""
    await el.click()
    await asyncio.sleep(random.uniform(0.1, 0.3))
    await el.clear()
    await el.press_sequentially(str(value), delay=random.randint(40, 120))
    await asyncio.sleep(random.uniform(0.05, 0.2))

_AUTH_HOSTPATHS = ("/login", "/checkpoint", "/uas/")
_ACCOUNTS_PATH = "created_accounts.json"

# ATS job-listing URL params that appear on company-hosted pages (not the form itself)
_LISTING_PARAMS = ("gh_jid", "ashby_jid", "jId", "jobId", "job_id", "requisition_id", "req_id", "jobPostingId")
# Domains where /jobs/<id> WITHOUT /apply in path is a listing page, not the form
_ATS_REQUIRE_APPLY_PATH = (
    "ats.rippling.com",
    "jobs.lever.co",
)
# Domains that ARE already the application form regardless of path
_FORM_DOMAINS = (
    "greenhouse.io", "ashbyhq.com", "workable.com",
    "myworkdayjobs.com", "icims.com", "taleo.net", "successfactors.com",
    "smartrecruiters.com", "jobvite.com", "breezy.hr",
)


def _clean_linkedin_url(url: str) -> str:
    """Strip LinkedIn tracking params (?trk=...) from job URLs so the page renders in standard view."""
    try:
        from urllib.parse import urlencode
        parsed = urlparse(url)
        if "linkedin.com" not in parsed.netloc:
            return url
        params = parse_qs(parsed.query, keep_blank_values=True)
        params.pop("trk", None)
        params.pop("refId", None)
        params.pop("trackingId", None)
        clean_query = urlencode({k: v[0] for k, v in params.items()})
        return parsed._replace(query=clean_query).geturl()
    except Exception:
        return url


def _is_job_listing_url(url: str) -> bool:
    """Return True for job LISTING pages that have an Apply button to click."""
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    if any(d in parsed.netloc for d in _FORM_DOMAINS):
        return False
    # Query param indicates company-hosted ATS listing (e.g. ?gh_jid=)
    if any(p in params for p in _LISTING_PARAMS):
        return True
    # ATS domains where listing URL lacks /apply or /application in path
    if any(d in parsed.netloc for d in _ATS_REQUIRE_APPLY_PATH):
        path = parsed.path.lower()
        if not any(k in path for k in ("/apply", "/application", "/form")):
            return True
    return False


def _load_all_accounts() -> list[dict]:
    path = Path(_ACCOUNTS_PATH)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
        return data.get("accounts", []) if isinstance(data, dict) else data
    except Exception:
        return []


def _find_account_for_domain(domain: str) -> dict | None:
    matches = []
    for acct in _load_all_accounts():
        url = acct.get("website_url", "")
        try:
            acct_domain = urlparse(url).netloc
            if (acct_domain == domain
                    or domain.endswith("." + acct_domain)
                    or acct_domain.endswith("." + domain)):
                matches.append(acct)
        except Exception:
            pass
    if not matches:
        return None
    # Return most recently created account for this domain
    return max(matches, key=lambda a: a.get("created_at", ""))


def _append_account(record: dict):
    path = Path(_ACCOUNTS_PATH)
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except Exception:
            data = {"accounts": []}
    else:
        data = {"accounts": []}

    new_domain = urlparse(record.get("website_url", "")).netloc
    updated = False
    if new_domain:
        for i, existing in enumerate(data["accounts"]):
            existing_domain = urlparse(existing.get("website_url", "")).netloc
            if (existing_domain == new_domain
                    or new_domain.endswith("." + existing_domain)
                    or existing_domain.endswith("." + new_domain)):
                data["accounts"][i] = record
                updated = True
                break
    if not updated:
        data["accounts"].append(record)

    path.write_text(json.dumps(data, indent=2))

# ── JavaScript field extractors ────────────────────────────────────────────────

_EASY_APPLY_FIELDS_JS = """
(function() {
    var modal = document.querySelector('.jobs-easy-apply-modal')
             || document.querySelector('.jobs-apply-form')
             || document.querySelector('[data-test-modal-container]')
             || document.querySelector('[role="dialog"]')
             || document.body;
    var results = [];
    var radioNames = new Set();

    modal.querySelectorAll(
        'input:not([type=hidden]):not([type=submit]):not([type=button])' +
        ':not([type=radio]),textarea,select'
    ).forEach(function(el) {
        if (el.offsetParent === null) return;
        var lbl = el.id ? document.querySelector('label[for="' + el.id + '"]') : null;
        var labelText = (
            (lbl && lbl.textContent.trim()) ||
            el.getAttribute('aria-label') ||
            el.getAttribute('placeholder') ||
            el.name || ''
        ).trim();
        var opts = [];
        if (el.tagName === 'SELECT') {
            Array.from(el.options).forEach(function(o) {
                if (o.value) opts.push(o.text.trim());
            });
        }
        results.push({
            kind: el.type || el.tagName.toLowerCase(),
            label: labelText,
            id: el.id || '',
            name: el.name || '',
            options: opts,
            current_value: el.value || ''
        });
    });

    modal.querySelectorAll('input[type="radio"]').forEach(function(el) {
        if (el.offsetParent === null) return;
        var grpName = el.name;
        if (!grpName || radioNames.has(grpName)) return;
        radioNames.add(grpName);
        var fieldset = el.closest('fieldset') || el.closest('[role="group"]');
        var legend = fieldset ? fieldset.querySelector('legend') : null;
        var groupLabel = (legend && legend.textContent.trim()) || grpName;
        var radios = modal.querySelectorAll('input[type="radio"][name="' + grpName + '"]');
        var opts = [], optIds = [];
        radios.forEach(function(r) {
            var l = r.id ? document.querySelector('label[for="' + r.id + '"]') : null;
            opts.push(l ? l.textContent.trim() : r.value);
            optIds.push(r.id || '');
        });
        results.push({
            kind: 'radio',
            label: groupLabel,
            name: grpName,
            options: opts,
            option_ids: optIds,
            current_value: ''
        });
    });

    modal.querySelectorAll('input[type="checkbox"]').forEach(function(el) {
        if (el.offsetParent === null) return;
        if (el.checked) return;  // already checked — skip
        var lbl = el.id ? document.querySelector('label[for="' + el.id + '"]') : null;
        var labelText = (
            (lbl && lbl.textContent.trim()) ||
            el.getAttribute('aria-label') || el.name || ''
        ).trim();
        results.push({
            kind: 'checkbox',
            label: labelText,
            id: el.id || '',
            name: el.name || '',
            options: [],
            current_value: el.checked ? 'true' : 'false'
        });
    });

    return JSON.stringify(results);
})()
"""

_OFFSITE_FIELDS_JS = """
(function() {
    var results = [];
    var radioNames = new Set();

    document.querySelectorAll(
        'input:not([type=hidden]):not([type=submit]):not([type=button])' +
        ':not([type=radio]):not([type=checkbox]),textarea,select'
    ).forEach(function(el) {
        if (el.offsetParent === null) return;
        var lbl = el.id ? document.querySelector('label[for="' + el.id + '"]') : null;
        var labelText = (
            (lbl && lbl.textContent.trim()) ||
            el.getAttribute('aria-label') ||
            el.getAttribute('placeholder') ||
            el.name || ''
        ).trim();
        var opts = [];
        if (el.tagName === 'SELECT') {
            Array.from(el.options).forEach(function(o) {
                if (o.value) opts.push(o.text.trim());
            });
        }
        results.push({
            kind: el.type || el.tagName.toLowerCase(),
            label: labelText,
            id: el.id || '',
            name: el.name || '',
            options: opts,
            current_value: el.value || ''
        });
    });

    document.querySelectorAll('input[type="radio"]').forEach(function(el) {
        if (el.offsetParent === null) return;
        var grpName = el.name;
        if (!grpName || radioNames.has(grpName)) return;
        radioNames.add(grpName);
        var fieldset = el.closest('fieldset') || el.closest('[role="group"]');
        var legend = fieldset ? fieldset.querySelector('legend') : null;
        var groupLabel = (legend && legend.textContent.trim()) || grpName;
        var radios = document.querySelectorAll('input[type="radio"][name="' + grpName + '"]');
        var opts = [], optIds = [];
        radios.forEach(function(r) {
            var l = r.id ? document.querySelector('label[for="' + r.id + '"]') : null;
            opts.push(l ? l.textContent.trim() : r.value);
            optIds.push(r.id || '');
        });
        results.push({
            kind: 'radio',
            label: groupLabel,
            name: grpName,
            options: opts,
            option_ids: optIds,
            current_value: ''
        });
    });

    return JSON.stringify(results);
})()
"""

# ── Deterministic profile → field mapping ──────────────────────────────────────

def _get_profile_value(profile: dict, label: str, kind: str = "text") -> str | None:
    """Map a form field label to a profile value. Returns None if no confident match."""
    import os
    # Strip required-field markers and normalize
    import re as _re
    l = label.lower().replace("_", " ")
    l = _re.sub(r'\*\s*required\b', '', l)   # "*Required" / "* Required"
    l = l.replace("*", "").replace("(required)", "").strip()
    l = _re.sub(r'\s+', ' ', l).strip()
    p = profile

    if kind == "file" or any(k in l for k in ("resume", "cv", "upload")):
        raw = p.get("resume_path", "")
        if raw:
            return os.path.abspath(raw) if not os.path.isabs(raw) else raw
        return None

    if any(k in l for k in ("first name", "given name")):
        parts = (p.get("full_name") or "").split()
        return parts[0] if parts else None
    if any(k in l for k in ("last name", "family name", "surname")):
        parts = (p.get("full_name") or "").split()
        return parts[-1] if len(parts) > 1 else (parts[0] if parts else None)
    if "email" in l:
        return p.get("email")
    if "phone" in l or "mobile" in l:
        return p.get("phone")
    if "linkedin" in l:
        return p.get("linkedin_url")
    if "github" in l:
        return p.get("github_url")
    if any(k in l for k in ("portfolio", "personal website", "personal site")):
        return p.get("portfolio_url")
    if any(k in l for k in ("zip", "postal")):
        return p.get("zip_code")
    if any(k in l for k in ("address line 1", "street address", "address 1", "street")):
        return p.get("street_address")
    if "address line 2" in l or "address 2" in l or "apt" in l or "suite" in l:
        return ""  # leave blank
    if any(k in l for k in ("state", "province")):
        return p.get("state")
    if "country" in l:
        return p.get("country", "United States")
    if any(k in l for k in ("city", "location", "where are you", "your location", "current location", "what is your current location", "city, state", "city/state")):
        return p.get("location") or p.get("city")
    if "address" in l:
        return p.get("street_address") or p.get("location")
    if any(k in l for k in ("current title", "job title", "current role", "current position")):
        return p.get("current_title")
    if any(k in l for k in ("years of experience", "years experience", "total experience")):
        return str(p.get("years_experience", ""))
    if any(k in l for k in ("sponsor", "sponsorship", "visa support", "work visa")):
        if p.get("need_sponsorship", "").lower() in ("yes", "true", "1"):
            return "Yes"
        auth = (p.get("work_authorization") or "").upper()
        return "Yes" if any(x in auth for x in ("OPT", "H1B", "H-1B", "F1", "TN")) else "No"
    if any(k in l for k in ("authorized to work", "legally authorized", "legal right to work", "eligible to work")):
        return "Yes"
    if any(k in l for k in ("salary", "compensation", "expected pay", "desired pay")):
        return str(p.get("preferred_salary", ""))
    if "degree" in l:
        edu = p.get("education", {})
        return edu.get("degree") if isinstance(edu, dict) else None
    if any(k in l for k in ("school", "university", "college", "institution")):
        edu = p.get("education", {})
        return edu.get("school") if isinstance(edu, dict) else None
    if any(k in l for k in ("field of study", "major", "area of study")):
        edu = p.get("education", {})
        return edu.get("field") if isinstance(edu, dict) else None
    if any(k in l for k in ("disability", "disabled")):
        return p.get("disability_status", "No")
    if "gender" in l:
        return p.get("gender", "decline")
    if any(k in l for k in ("race", "ethnicity", "ethnic")):
        return p.get("race", "decline")
    if any(k in l for k in ("veteran", "military status", "protected veteran")):
        return p.get("veteran_status", "No")
    if any(k in l for k in ("where did you hear", "how did you hear", "how did you find out", "how did you learn about", "source of hire")):
        return "LinkedIn"
    if any(k in l for k in ("cover letter", "cover_letter")) and kind in ("text", "textarea"):
        return p.get("cover_letter_text") or None

    return None


# ── Shared helpers ─────────────────────────────────────────────────────────────

async def _ask_llm(llm_client: OpenAI, model: str, profile: dict, field: dict) -> str | None:
    label = field.get("label", "")
    if not label:
        return None
    options = field.get("options", [])
    prompt = f"Job application form field:\nLabel: {label}\nType: {field.get('kind', 'text')}\n"
    if options:
        prompt += f"Options: {', '.join(str(o) for o in options)}\n"
    prompt += (
        f"\nUser profile:\n{json.dumps(profile, indent=2)}\n\n"
        "Answer this single form field. "
        "If radio/select, reply with exactly one of the listed options. "
        "If the profile has no relevant info, reply with an empty string. "
        "Reply with ONLY the answer value, nothing else."
    )
    try:
        resp = await asyncio.wait_for(
            asyncio.to_thread(
                llm_client.chat.completions.create,
                model=model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=100,
            ),
            timeout=30,
        )
        raw_answer = resp.choices[0].message.content
        answer = raw_answer.strip().strip('"').strip("'")
        _write_llm_log({
            "ts":           datetime.now(timezone.utc).isoformat(),
            "type":         "field_fill",
            "model":        model,
            "field_label":  label,
            "field_kind":   field.get("kind"),
            "options":      field.get("options", []),
            "prompt":       prompt,
            "raw_response": raw_answer,
            "result":       answer,
        })
        return answer if answer else None
    except asyncio.TimeoutError:
        print(f"  [LLM timeout] Field '{label}' — no answer in 30s, skipping.")
        return None
    except Exception:
        return None


async def _fill_field(page: Page, field: dict, value: str):
    """Fill a single form field. Non-fatal on error."""
    import os
    kind = field.get("kind", "text")
    el_id = field.get("id", "")
    name = field.get("name", "")

    try:
        if kind == "file":
            # Resume / file upload — value should be an absolute path
            if value and os.path.isfile(value):
                sel = f'#{el_id}' if el_id else (f'[name="{name}"]' if name else 'input[type="file"]')
                el = page.locator(sel).first
                if await el.count() > 0:
                    await el.set_input_files(value)
            return

        if kind in ("text", "textarea", "email", "tel", "number", "url", "search", "password"):
            sel = f'#{el_id}' if el_id else f'[name="{name}"]'
            el = page.locator(sel).first
            if await el.count() > 0:
                await _human_type(el, value)
                await asyncio.sleep(random.uniform(0.2, 0.6))
                # Handle location autocomplete — click first suggestion if a dropdown appears
                label_l = field.get("label", "").lower()
                if any(k in label_l for k in ("city", "location", "where are you")):
                    try:
                        await asyncio.sleep(1)
                        suggestion = page.locator(
                            '[role="option"], [role="listbox"] li, '
                            '.basic-typeahead__selectable, .search-typeahead-v2__hit'
                        ).first
                        if await suggestion.count() > 0:
                            await suggestion.click()
                            await asyncio.sleep(0.5)
                    except Exception:
                        pass

        elif kind == "select":
            sel = f'#{el_id}' if el_id else f'[name="{name}"]'
            el = page.locator(sel).first
            if await el.count() > 0:
                if str(value).lower() == "decline":
                    _dk = ("not wish", "prefer not", "decline", "choose not", "do not wish", "don't wish", "no answer")
                    try:
                        opts = await el.evaluate("el => Array.from(el.options).map(o => o.text)")
                        target = next((o for o in opts if any(k in o.lower() for k in _dk)), None)
                        if target:
                            await el.select_option(label=target)
                    except Exception:
                        pass
                else:
                    try:
                        await el.select_option(label=value)
                    except Exception:
                        try:
                            await el.select_option(value=value)
                        except Exception:
                            pass

        elif kind == "radio":
            options = field.get("options", [])
            option_ids = field.get("option_ids", [])
            _dk = ("not wish", "prefer not", "decline", "choose not", "do not wish", "don't wish", "no answer")
            if str(value).lower() == "decline":
                matched = next(
                    (i for i, o in enumerate(options)
                     if any(k in str(o).lower() for k in _dk)),
                    None,
                )
            else:
                matched = next(
                    (i for i, o in enumerate(options)
                     if str(value).lower() == str(o).lower()
                     or str(value).lower() in str(o).lower()),
                    None,
                )
            if matched is not None:
                if matched < len(option_ids) and option_ids[matched]:
                    await page.locator(f'#{option_ids[matched]}').click()
                elif name:
                    await page.locator(
                        f'input[type="radio"][name="{name}"]'
                    ).nth(matched).click()

        elif kind == "checkbox":
            # Check agreement/required checkboxes; skip opt-in/marketing ones
            label_lower = field.get("label", "").lower()
            _opt_in = ("email", "newsletter", "marketing", "notification", "subscribe", "follow", "updates")
            if not any(k in label_lower for k in _opt_in):
                sel = f'#{el_id}' if el_id else (f'[name="{name}"]' if name else 'input[type="checkbox"]')
                el = page.locator(sel).first
                if await el.count() > 0 and not await el.is_checked():
                    await el.click()

    except Exception:
        pass


# ── EasyApplyFlow ──────────────────────────────────────────────────────────────

class EasyApplyFlow:
    """
    Drives the LinkedIn Easy Apply modal deterministically.

    callbacks dict keys:
        ready_to_submit(summary: str) -> "applied" | "skipped"
        get_credentials() -> (email, password)  — used only if redirected to login
    """

    def __init__(
        self,
        page: Page,
        profile: dict,
        llm_client: OpenAI,
        model: str,
        auto_mode: bool,
        callbacks: dict,
        classifier_client: OpenAI | None = None,
        classifier_model: str = "",
    ):
        self.page = page
        self.profile = profile
        self.llm_client = llm_client
        self.model = model
        self.auto_mode = auto_mode
        self.callbacks = callbacks
        self.classifier_client = classifier_client or llm_client
        self.classifier_model = classifier_model or model
        self.unanswered_fields: list[str] = []

    async def run(self, job_url: str) -> str:
        """Returns: 'applied' | 'expired' | 'already_applied' | 'skipped' | 'failed'"""
        job_url = _clean_linkedin_url(job_url)
        try:
            await self.page.goto(job_url, wait_until="domcontentloaded", timeout=20000)
        except Exception:
            pass
        await asyncio.sleep(2)

        if any(p in self.page.url for p in _AUTH_HOSTPATHS):
            await self._handle_login()
            await asyncio.sleep(3)
            try:
                await self.page.goto(job_url, wait_until="domcontentloaded", timeout=20000)
            except Exception:
                pass
            await asyncio.sleep(2)

        status = await self._detect_job_status()
        if status != "open":
            return status

        apply_kind = await self._click_easy_apply()
        if apply_kind == "external":
            return "external_apply"
        if apply_kind != "easy":
            return "no_easy_apply"

        return await self._process_all_steps()

    async def _handle_login(self):
        get_creds = self.callbacks.get("get_credentials")
        if not get_creds:
            return
        try:
            email, password = get_creds()
            await self.page.fill("#username", email)
            await self.page.fill("#password", password)
            await self.page.click('button.btn__primary--large[type="submit"]')
            await asyncio.sleep(3)
        except Exception:
            pass

    _MODAL_SELECTORS = (
        ".jobs-easy-apply-modal",
        "[data-test-modal-container]",
        ".artdeco-modal[role='dialog']",
        "[role='dialog']",
    )

    async def _detect_job_status(self) -> str:
        try:
            expired_phrases = [
                "No longer accepting applications",
                "This job is no longer accepting applications",
                "Applications are closed",
            ]
            for phrase in expired_phrases:
                if await self.page.get_by_text(phrase, exact=False).count() > 0:
                    return "expired"

            already_applied_patterns = [
                "text=Application submitted",
                "text=You applied",
                '[aria-label*="Applied"]',
            ]
            for pat in already_applied_patterns:
                if await self.page.locator(pat).count() > 0:
                    return "already_applied"
        except Exception:
            pass
        return "open"

    async def _click_easy_apply(self) -> str:
        """Returns 'easy' | 'external' | 'none'."""
        try:
            # Wait for job actions to render
            try:
                await self.page.wait_for_selector(
                    '.jobs-apply-button, [aria-label*="Easy Apply"], [aria-label*="Apply"]',
                    timeout=8000,
                )
            except Exception:
                pass

            # Try Easy Apply selectors from most to least specific.
            # "LinkedIn Apply to this job" is LinkedIn-hosted (same modal flow as Easy Apply).
            easy_apply_selectors = [
                '[aria-label*="Easy Apply"]',
                'button.jobs-apply-button:has-text("Easy Apply")',
                '.jobs-apply-button--top-card:has-text("Easy Apply")',
                'button:has-text("Easy Apply")',
                '[aria-label*="Apply to this job"]',
                'button[aria-label*="LinkedIn Apply"]',
                'a[aria-label*="LinkedIn Apply"]',
            ]
            btn = None
            for sel in easy_apply_selectors:
                try:
                    candidate = self.page.locator(sel).first
                    if await candidate.count() > 0:
                        btn = candidate
                        break
                except Exception:
                    continue

            if btn is None:
                # Check whether a plain external "Apply" button exists (job switched from Easy Apply)
                has_external = False
                try:
                    apply_count = await self.page.evaluate("""() => {
                        return Array.from(document.querySelectorAll('button,a'))
                            .filter(el => {
                                const label = (el.getAttribute('aria-label') || '').toLowerCase();
                                const text = (el.textContent || '').trim().toLowerCase();
                                if (label.includes('apply to this job') || label.includes('linkedin apply')) return false;
                                return text === 'apply' || label === 'apply';
                            })
                            .length;
                    }""")
                    has_external = apply_count > 0
                except Exception:
                    pass
                if has_external:
                    print("  [EasyApply] No Easy Apply button — plain Apply button found (job switched to external)")
                    return "external"
                print("  [EasyApply] No Easy Apply button found on page")
                return "none"

            await btn.click()

            # Wait for any modal to appear
            for modal_sel in self._MODAL_SELECTORS:
                try:
                    await self.page.wait_for_selector(modal_sel, timeout=5000)
                    return "easy"
                except Exception:
                    continue
            # No known modal selector matched — assume it opened anyway
            await asyncio.sleep(1)
            return "easy"

        except Exception as exc:
            print(f"  [EasyApply] Error clicking Easy Apply: {exc}")
            return "none"

    async def _get_modal_text(self) -> str:
        """Return a fingerprint of the current modal content."""
        for sel in self._MODAL_SELECTORS:
            try:
                el = self.page.locator(sel).first
                if await el.count() > 0:
                    return (await el.inner_text())[:400]
            except Exception:
                continue
        return ""

    async def _is_modal_open(self) -> bool:
        for sel in self._MODAL_SELECTORS:
            try:
                if await self.page.locator(sel).count() > 0:
                    return True
            except Exception:
                continue
        return False

    async def _process_all_steps(self) -> str:
        click_failures = 0

        for step_num in range(25):
            await asyncio.sleep(1.5)
            await self._fill_current_step()
            await asyncio.sleep(0.5)

            # Check all navigation buttons — prefer Submit > Review > Next
            submit = self.page.locator(
                '[aria-label*="Submit application"], button:has-text("Submit application")'
            ).first
            if await submit.count() > 0:
                print(f"  [EasyApply] Step {step_num + 1}: Submit button found")
                return await self._handle_submit()

            review = self.page.locator(
                '[aria-label*="Review your application"], button:has-text("Review your application")'
            ).first
            if await review.count() > 0:
                print(f"  [EasyApply] Step {step_num + 1}: Review button — clicking")
                await review.click()
                await asyncio.sleep(1.5)
                click_failures = 0
                continue

            next_btn = self.page.locator(
                '[aria-label*="Continue to next step"], button:has-text("Next")'
            ).first
            if await next_btn.count() > 0:
                before = await self._get_modal_text()
                print(f"  [EasyApply] Step {step_num + 1}: Next button — clicking")
                await next_btn.click()
                await asyncio.sleep(2)
                after = await self._get_modal_text()

                # Only count as stuck if we got real content and it didn't change
                if before and after and before == after:
                    click_failures += 1
                    print(f"  [EasyApply] Step {step_num + 1}: Next click didn't advance ({click_failures}/3)")
                    if click_failures >= 3:
                        return "failed"
                else:
                    click_failures = 0
                continue

            # No recognized button found
            if not await self._is_modal_open():
                # Modal closed — check if application was submitted
                confirmed, msg = await self._check_submission_result()
                if confirmed:
                    print(f"  [EasyApply] Modal closed — {msg}")
                    return "applied"
                print("  [EasyApply] Modal closed unexpectedly — treating as failed")
                return "failed"
            click_failures += 1
            print(f"  [EasyApply] Step {step_num + 1}: No navigation button found ({click_failures}/3)")
            if click_failures >= 3:
                return "failed"

        return "failed"

    async def _fill_current_step(self):
        # Handle resume selection step — pick the most recent resume (first card)
        try:
            resume_cards = self.page.locator(
                '.jobs-resume-picker__resume, [data-test-resume-card], '
                '[class*="resume-picker"] [class*="resume"], '
                'input[name*="resume"][type="radio"]'
            )
            if await resume_cards.count() > 0:
                first = resume_cards.first
                tag = await first.evaluate("el => el.tagName.toLowerCase()")
                if tag == "input":
                    if not await first.is_checked():
                        await first.click()
                else:
                    await first.click()
                print(f"  [EasyApply] Resume step — selected first resume")
        except Exception:
            pass

        try:
            raw = await self.page.evaluate(_EASY_APPLY_FIELDS_JS)
            fields = json.loads(raw) if raw else []
        except Exception:
            return

        if fields:
            print(f"  [EasyApply] Step fields: {[f.get('label','?') for f in fields]}")

        for field in fields:
            if field.get("current_value") and field.get("kind") != "radio":
                continue  # skip already-filled fields
            label = field.get("label", "")
            value = _get_profile_value(self.profile, label, field.get("kind", "text"))
            if value is None:
                value = await _ask_llm(self.classifier_client, self.classifier_model, self.profile, field)
            if value:
                print(f"  [EasyApply] Filling '{label}' = {str(value)[:40]!r}")
                await _fill_field(self.page, field, value)
            elif label and label not in self.unanswered_fields:
                self.unanswered_fields.append(label)

    async def _handle_submit(self) -> str:
        summary = (
            f"Application for {self.profile.get('full_name', 'user')}. "
            f"Email: {self.profile.get('email', 'N/A')}, "
            f"Phone: {self.profile.get('phone', 'N/A')}, "
            f"Work auth: {self.profile.get('work_authorization', 'N/A')}."
        )
        ready = self.callbacks.get("ready_to_submit")
        result = ready(summary) if ready else "applied"

        if result == "applied":
            try:
                btn = self.page.locator('[aria-label*="Submit application"]').first
                if await btn.count() > 0:
                    await btn.click()
                    # Wait for LinkedIn to process and show the confirmation screen
                    try:
                        await self.page.wait_for_load_state("networkidle", timeout=10000)
                    except Exception:
                        pass
                    await asyncio.sleep(2)
                    # Dismiss the "Follow company" dialog LinkedIn shows after submission
                    for dismiss_sel in (
                        '[aria-label*="Dismiss"]',
                        'button:has-text("Done")',
                        '[aria-label*="Done"]',
                        'button:has-text("Not now")',
                    ):
                        try:
                            d = self.page.locator(dismiss_sel).first
                            if await d.count() > 0:
                                await d.click()
                                await asyncio.sleep(1)
                                break
                        except Exception:
                            continue
            except Exception:
                pass
            # Verify LinkedIn showed the confirmation screen
            confirmed, msg = await self._check_submission_result()
            if confirmed:
                print(f"  [EasyApply] Submission confirmed: {msg}")
                return "applied"
            else:
                print(f"  [EasyApply] ⚠ Submit clicked but {msg}")
                return "failed"
        return "skipped"

    async def _check_submission_result(self) -> tuple[bool, str]:
        """Check whether LinkedIn accepted the Easy Apply submission."""
        try:
            page = self.page
            content = (await page.content()).lower()

            # Confirmation text is the strongest signal (modal may still be open showing it)
            for phrase in ("your application was sent", "application submitted",
                           "application was submitted", "successfully applied",
                           "you've applied", "you applied", "application sent",
                           "application was sent"):
                if phrase in content:
                    return True, f"found '{phrase}' on page"

            # Modal closed without error = success
            modal_open = await self._is_modal_open()
            if not modal_open:
                return True, "modal closed after submit"

            # Check if the Easy Apply button changed to "Applied"
            for sel in ('[aria-label*="Applied"]', 'button:has-text("Applied")'):
                if await page.locator(sel).count() > 0:
                    return True, "page shows 'Applied' indicator"

            # Modal still open — look for validation errors
            error_clues = [p for p in ("required", "please complete", "please fill",
                                       "missing", "invalid email", "invalid phone") if p in content]
            if error_clues:
                return False, f"modal open with validation errors: {error_clues[:2]}"
            return False, "modal still open after submit — form may not have submitted"
        except Exception as exc:
            return False, f"verification error: {exc}"


# ── OffsiteApplyFlow ───────────────────────────────────────────────────────────

class OffsiteApplyFlow:
    """
    Drives an external company career site application.

    callbacks dict keys:
        ready_to_submit(summary: str) -> "applied" | "skipped"
        get_credentials() -> (email, password)
        save_account(record: dict) -> None
    """

    def __init__(
        self,
        page: Page,
        context: BrowserContext,
        profile: dict,
        llm_client: OpenAI,
        model: str,
        auto_mode: bool,
        callbacks: dict,
        generated_password: str,
        company_name: str = "",
        job_title: str = "",
        classifier_client: OpenAI | None = None,
        classifier_model: str = "",
    ):
        self.page = page
        self.context = context
        self.profile = profile
        self.llm_client = llm_client
        self.model = model
        self.auto_mode = auto_mode
        self.callbacks = callbacks
        self.generated_password = generated_password
        self.company_name = company_name
        self.job_title = job_title
        self.classifier_client = classifier_client or llm_client
        self.classifier_model = classifier_model or model
        self.unanswered_fields: list[str] = []

    _EXPIRED_PHRASES = [
        "No longer accepting applications",
        "This job is no longer accepting applications",
        "Applications are closed",
    ]

    async def run(self, job_url: str) -> str:
        job_url = _clean_linkedin_url(job_url)
        try:
            await self.page.goto(job_url, wait_until="domcontentloaded", timeout=20000)
        except Exception:
            pass
        await asyncio.sleep(2)

        if any(p in self.page.url for p in _AUTH_HOSTPATHS):
            await self._handle_login()
            await asyncio.sleep(3)
            try:
                await self.page.goto(job_url, wait_until="domcontentloaded", timeout=20000)
            except Exception:
                pass
            await asyncio.sleep(2)

        # Check for expired or already-applied before looking for Apply button
        try:
            for phrase in self._EXPIRED_PHRASES:
                if await self.page.get_by_text(phrase, exact=False).count() > 0:
                    print(f"  [Offsite] Job closed — skipped.")
                    return "expired"
            for pat in ('[aria-label*="Applied"]', 'text=Application submitted', 'text=You applied'):
                if await self.page.locator(pat).count() > 0:
                    return "already_applied"
        except Exception:
            pass

        ext_page, apply_status = await self._click_apply_and_get_page()
        if ext_page is None:
            return apply_status  # "failed", "no_easy_apply", etc.

        return await self._fill_external_form(ext_page)

    async def _handle_login(self):
        get_creds = self.callbacks.get("get_credentials")
        if not get_creds:
            return
        try:
            email, password = get_creds()
            await self.page.fill("#username", email)
            await self.page.fill("#password", password)
            await self.page.click('button.btn__primary--large[type="submit"]')
            await asyncio.sleep(3)
        except Exception:
            pass

    async def _extract_external_apply_url(self) -> str | None:
        """Try to extract the external apply URL from LinkedIn page data without clicking."""
        try:
            result = await self.page.evaluate("""() => {
                // Check JSON-LD structured data
                const scripts = Array.from(document.querySelectorAll('script[type="application/ld+json"]'));
                for (const s of scripts) {
                    try {
                        const d = JSON.parse(s.textContent || '{}');
                        if (d.url && !d.url.includes('linkedin.com')) return d.url;
                        if (d.applicationContact && d.applicationContact.url) return d.applicationContact.url;
                    } catch(e) {}
                }
                // Check data attributes on Apply buttons
                const btns = Array.from(document.querySelectorAll('[data-job-id], [data-apply-url]'));
                for (const b of btns) {
                    const u = b.getAttribute('data-apply-url') || b.getAttribute('data-url');
                    if (u && !u.includes('linkedin.com')) return u;
                }
                // Check anchor tags near Apply buttons
                const anchors = Array.from(document.querySelectorAll('a[href*="apply"]'));
                for (const a of anchors) {
                    const href = a.href || '';
                    if (href && !href.includes('linkedin.com') && href.startsWith('http')) return href;
                }
                return null;
            }""")
            return result
        except Exception:
            return None

    async def _click_apply_and_get_page(self) -> tuple[Page | None, str]:
        """Returns (page, status). status is 'ok' on success, else a status string."""
        # Wait for job actions to render — LinkedIn is a React SPA, needs more time
        try:
            await self.page.wait_for_selector(
                '.jobs-apply-button, [data-control-name*="apply"], [aria-label*="Apply"]',
                timeout=15000,
            )
        except Exception:
            pass

        selectors = [
            '.jobs-apply-button--top-card',
            '.jobs-apply-button',
            '[aria-label*="Apply on company site"]',
            'button:has-text("Apply on company site")',
            'a:has-text("Apply on company site")',
            '[aria-label^="Apply to"]',
            'button[aria-label*="Apply"]:not([aria-label*="Easy"])',
            'a[aria-label*="Apply"]:not([aria-label*="Easy"])',
            'button:has-text("Apply"):not(:has-text("Easy"))',
            'a:has-text("Apply"):not(:has-text("Easy"))',
        ]
        btn = None
        matched_sel = None
        for sel in selectors:
            try:
                candidate = self.page.locator(sel).first
                if await candidate.count() > 0:
                    btn = candidate
                    matched_sel = sel
                    break
            except Exception:
                continue

        if btn is None:
            # Check if this is actually an Easy Apply job (DB type may be stale)
            try:
                easy_btn = self.page.locator('[aria-label*="Easy Apply"], button:has-text("Easy Apply")').first
                if await easy_btn.count() > 0:
                    print("  [Offsite] Job has Easy Apply button — DB type is stale. Skipping.")
                    return None, "no_easy_apply"
            except Exception:
                pass
            # Log all visible buttons to help diagnose missing selectors
            try:
                visible_btns = await self.page.evaluate("""
                    () => Array.from(document.querySelectorAll('button, a[role="button"], a[href]'))
                        .filter(el => el.offsetParent !== null)
                        .slice(0, 20)
                        .map(el => (el.getAttribute('aria-label') || el.textContent || '').trim().slice(0, 60))
                        .filter(t => t)
                """)
                print(f"  [Offsite] No Apply button found. Visible buttons: {visible_btns[:10]}")
            except Exception:
                print("  [Offsite] No Apply button found on LinkedIn job page")
            return None, "no_apply_button"

        try:
            btn_label = (await btn.get_attribute("aria-label") or await btn.inner_text() or "?").strip()[:50]
            print(f"  [Offsite] Apply button: {btn_label!r} (sel: {matched_sel})")
        except Exception:
            pass

        url_before = self.page.url

        async def _wait_for_new_tab(timeout_ms: int) -> Page | None:
            try:
                async with self.context.expect_page(timeout=timeout_ms) as new_page_info:
                    pass  # event already in flight
                new_page = await new_page_info.value
                await new_page.wait_for_load_state("domcontentloaded")
                return new_page
            except Exception:
                return None

        # Click the apply button, listening for a new tab (up to 10 s)
        try:
            async with self.context.expect_page(timeout=10000) as new_page_info:
                await btn.click()
            new_page = await new_page_info.value
            await new_page.wait_for_load_state("domcontentloaded")
            print(f"  [Offsite] Opened new tab: {new_page.url}")
            return new_page, "ok"
        except Exception:
            pass

        # No immediate new tab — LinkedIn may show a "leaving LinkedIn" modal.
        # Give it 3 s to appear, then look for a Continue/Confirm button.
        await asyncio.sleep(3)

        _modal_btns = [
            'button:has-text("Continue")',
            'button:has-text("Confirm")',
            'button:has-text("Apply now")',
            'a:has-text("Continue")',
            '[role="dialog"] button:not([aria-label*="Dismiss"]):not([aria-label*="Close"]):not(:has-text(""))',
            '.artdeco-modal button:not([aria-label*="Dismiss"]):not([aria-label*="Close"])',
        ]
        for msel in _modal_btns:
            try:
                mbtn = self.page.locator(msel).first
                if await mbtn.count() > 0:
                    mtext = (await mbtn.inner_text()).strip()[:30]
                    if not mtext:  # skip empty-text buttons — they're spinners/overlays
                        continue
                    print(f"  [Offsite] Clicking modal button: {mtext!r}")
                    try:
                        async with self.context.expect_page(timeout=8000) as new_page_info:
                            await mbtn.click()
                        new_page = await new_page_info.value
                        await new_page.wait_for_load_state("domcontentloaded")
                        print(f"  [Offsite] Modal → new tab: {new_page.url}")
                        return new_page, "ok"
                    except Exception:
                        # Modal click may navigate same-tab
                        await asyncio.sleep(2)
                        if self.page.url != url_before and "linkedin.com" not in self.page.url:
                            print(f"  [Offsite] Modal → same-tab: {self.page.url}")
                            return self.page, "ok"
                        break
            except Exception:
                continue

        # Check for same-tab navigation — including LinkedIn interstitial pages
        # that stay on linkedin.com before forwarding to the external site
        if self.page.url != url_before:
            current = self.page.url
            if "linkedin.com" not in current:
                print(f"  [Offsite] Same-tab navigation to: {current}")
                return self.page, "ok"
            # LinkedIn interstitial page (still on linkedin.com) — wait for it to forward
            print(f"  [Offsite] LinkedIn interstitial: {current} — waiting for forward…")
            try:
                await self.page.wait_for_url(
                    lambda u: "linkedin.com" not in u, timeout=10000
                )
                print(f"  [Offsite] Interstitial forwarded to: {self.page.url}")
                return self.page, "ok"
            except Exception:
                # Interstitial didn't auto-forward — look for a Continue button on it
                for msel in ('button:has-text("Continue")', 'a:has-text("Visit")',
                             'a:has-text("Continue")', 'button:has-text("Visit")'):
                    try:
                        mbtn = self.page.locator(msel).first
                        if await mbtn.count() > 0:
                            mtext = (await mbtn.inner_text()).strip()[:30]
                            print(f"  [Offsite] Interstitial button: {mtext!r}")
                            try:
                                async with self.context.expect_page(timeout=8000) as npi:
                                    await mbtn.click()
                                new_page = await npi.value
                                await new_page.wait_for_load_state("domcontentloaded")
                                return new_page, "ok"
                            except Exception:
                                await asyncio.sleep(2)
                                if "linkedin.com" not in self.page.url:
                                    return self.page, "ok"
                    except Exception:
                        continue

        # Check for late-opening tabs
        new_pages = [p for p in self.context.pages if p != self.page and not p.is_closed()]
        if new_pages:
            new_page = new_pages[-1]
            print(f"  [Offsite] Late new tab detected: {new_page.url}")
            return new_page, "ok"

        # Try extracting the href from the Apply button and navigating directly
        try:
            href = await btn.get_attribute("href")
            if href and href.startswith("http") and "linkedin.com" not in href:
                print(f"  [Offsite] Direct href navigate: {href[:80]}")
                await self.page.goto(href, wait_until="domcontentloaded", timeout=15000)
                if "linkedin.com" not in self.page.url:
                    return self.page, "ok"
        except Exception:
            pass

        # Try extracting external URL from LinkedIn's embedded page data (no click needed)
        ext_url = await self._extract_external_apply_url()
        if ext_url:
            print(f"  [Offsite] Extracted apply URL from page data: {ext_url[:80]}")
            await self.page.goto(ext_url, wait_until="domcontentloaded", timeout=20000)
            if "linkedin.com" not in self.page.url:
                return self.page, "ok"

        # Last resort: log visible buttons to help diagnose what LinkedIn showed
        try:
            post_click_btns = await self.page.evaluate("""
                () => Array.from(document.querySelectorAll('button, a[role="button"]'))
                    .filter(el => el.offsetParent !== null)
                    .slice(0, 15)
                    .map(el => (el.getAttribute('aria-label') || el.textContent || '').trim().slice(0, 60))
                    .filter(t => t)
            """)
            print(f"  [Offsite] Post-click buttons: {post_click_btns[:8]}")
        except Exception:
            pass

        print(f"  [Offsite] Apply click didn't navigate — still on {self.page.url}")
        return None, "failed"

    async def _try_career_page_apply(self, page: Page) -> tuple:
        """
        On a company job listing page, click the site's own Apply button to get
        to the actual application form. Returns (page, clicked) where clicked is
        True when a button was found and clicked (regardless of navigation outcome),
        False when no button was found at all.
        """
        career_apply_selectors = [
            'a:has-text("Apply for this job")',
            'button:has-text("Apply for this job")',
            'a:has-text("Apply for this position")',
            'button:has-text("Apply for this position")',
            'a:has-text("Apply Now")', 'a:has-text("Apply now")',
            'button:has-text("Apply Now")', 'button:has-text("Apply now")',
            '[role="button"]:has-text("Apply Now")', '[role="button"]:has-text("Apply now")',
            'a[class*="apply"]',
            'button[class*="apply"]',
            'a:has-text("Apply")',
            'button:has-text("Apply")',
            '[role="button"]:has-text("Apply")',
        ]
        for sel in career_apply_selectors:
            try:
                btn = page.locator(sel).first
                if await btn.count() == 0:
                    continue
                btn_text = (await btn.inner_text()).strip()[:40]
                print(f"  [Offsite] Clicking career page Apply: {btn_text!r} ({sel})")
                url_before = page.url
                try:
                    async with self.context.expect_page(timeout=5000) as new_page_info:
                        await btn.click()
                    new_page = await new_page_info.value
                    await new_page.wait_for_load_state("domcontentloaded")
                    print(f"  [Offsite] Career Apply opened new tab: {new_page.url}")
                    return new_page, True
                except Exception:
                    # No new tab — wait for in-page navigation or modal
                    await asyncio.sleep(3)
                    if page.url != url_before:
                        print(f"  [Offsite] Career Apply navigated to: {page.url}")
                    else:
                        # Check if a modal or inline form appeared
                        try:
                            raw = await page.evaluate(_OFFSITE_FIELDS_JS)
                            fields_now = json.loads(raw) if raw else []
                        except Exception:
                            fields_now = []
                        if fields_now:
                            print(f"  [Offsite] Career Apply revealed inline form ({len(fields_now)} fields)")
                        else:
                            print(f"  [Offsite] Career Apply clicked — checking for modal/form")
                    return page, True
            except Exception:
                continue
        print("  [Offsite] No career-page Apply button found")
        return page, False

    # ── LLM-guided page analysis ───────────────────────────────────────────────

    async def _get_page_snapshot(self, page: Page) -> str:
        """Capture URL, viewport text, and interactive elements for the LLM (compact form)."""
        try:
            visible_text = await page.evaluate(
                "() => (document.body.innerText || '').slice(0, 600)"
            )
        except Exception:
            visible_text = ""
        try:
            elements = await page.evaluate("""() => {
                const lbl = (el) => {
                    const forEl = el.id ? document.querySelector('label[for="' + el.id + '"]') : null;
                    return (forEl && forEl.textContent.trim())
                        || el.getAttribute('aria-label') || el.getAttribute('placeholder') || '';
                };
                return Array.from(document.querySelectorAll(
                    'button, a[href], input:not([type=hidden]), select, textarea, [role="button"]'
                ))
                .filter(el => el.offsetParent !== null)
                .slice(0, 25)
                .map(el => ({
                    tag: el.tagName.toLowerCase(),
                    type: el.getAttribute('type') || '',
                    id: el.id || '',
                    label: lbl(el),
                    text: (el.textContent || '').trim().slice(0, 50),
                    value: el.value ? el.value.slice(0, 40) : ''
                }));
            }""")
        except Exception:
            elements = []
        return (
            f"URL: {page.url}\n"
            f"Page text (first 600 chars): {visible_text}\n"
            f"Elements (first 25): {json.dumps(elements)}"
        )

    async def _ask_llm_action(self, snapshot: str, step: int, history: list[str] | None = None, current_url: str = "") -> dict:
        """Ask the LLM what single action to take next (compact prompt for speed)."""
        if history is None:
            history = []
        import os as _os
        p = self.profile
        resume_path = p.get("resume_path", "")
        if resume_path:
            resume_path = _os.path.abspath(resume_path) if not _os.path.isabs(resume_path) else resume_path
        profile_line = (
            f"name={p.get('full_name','')} email={p.get('email','')} "
            f"phone={p.get('phone','')} location={p.get('location','')} "
            f"title={p.get('current_title','')} yrs={p.get('years_experience','')} "
            f"auth={p.get('work_authorization','')} resume={resume_path}"
        )
        history_str = ""
        if history:
            history_str = "\nDone already (do NOT repeat): " + " | ".join(history[-5:]) + "\n"

        prompt = (
            f"Job application automation, step {step + 1}.\n"
            f"Page: {snapshot}\n"
            f"Profile: {profile_line}\n"
            f"{history_str}\n"
            "Output EXACTLY ONE JSON object (no preamble, no explanation, never multiple objects):\n"
            '{"action":"click|fill|select|upload|scroll|done|failed",'
            '"selector":"<Playwright CSS selector>","text":"<btn/link text fallback>","value":"<fill/select value>",'
            '"reason":"<1 sentence>"}\n\n'
            "Rules: done=thank-you/confirmation visible. failed=captcha/identity-verify/stuck/job-no-longer-available. "
            "If you see 'job not found', 'no longer available', 'position closed', or similar expired-job text, respond with failed and reason 'job no longer available' — do NOT navigate to job search or other pages. "
            "click=button or link (use :has-text() NOT :contains()). fill=empty text input. "
            "select=dropdown. upload=resume file input. scroll=reveal more. "
            "Priority: Submit/Apply buttons first when all fields are filled. Fill ONLY empty fields — skip any field that already has the correct value. "
            "Never re-fill a field already in history. Never Cancel/Sign-out. "
            "Never click bare 'Apply' anchor links in navigation menus — only click clearly labeled application buttons like 'Apply Now', 'Apply for this job', 'Submit application'. "
            "Never click 'Login', 'Log in', or 'Sign in' buttons unless you have just filled BOTH an email field AND a password field in this step — a Login button in a site header is never a form submit. "
            "Never click utility buttons (Add to favorites, Save, Bookmark, Share, Follow, Subscribe, "
            "Join our talent community, Get job alerts, Stay connected, Join our network, Sign up for alerts, "
            "Refer a friend) — they are not part of applying."
        )

        raw = ""
        for _attempt in range(3):
            try:
                resp = await asyncio.wait_for(
                    asyncio.to_thread(
                        self.llm_client.chat.completions.create,
                        model=self.model,
                        messages=[{"role": "user", "content": prompt}],
                        max_tokens=1000,
                    ),
                    timeout=120,
                )
                raw = resp.choices[0].message.content
                # Strip markdown fences if present
                clean = raw.strip()
                if "```" in clean:
                    clean = clean.split("```")[1].lstrip("json").strip()
                # Find JSON object in response
                start = clean.find("{")
                end = clean.rfind("}") + 1
                if start >= 0 and end > start:
                    clean = clean[start:end]
                # If the model emitted multiple JSON objects, take only the first one
                first_end = clean.find("}", start) + 1
                try:
                    parsed = json.loads(clean)
                except json.JSONDecodeError:
                    parsed = json.loads(clean[start:first_end])
                _write_llm_log({
                    "ts":           datetime.now(timezone.utc).isoformat(),
                    "type":         "browser_action",
                    "model":        self.model,
                    "step":         step + 1,
                    "current_url":  current_url,
                    "company":      self.company_name,
                    "job_title":    self.job_title,
                    "snapshot":     snapshot,
                    "history":      (history or [])[-5:],
                    "prompt":       prompt,
                    "raw_response": raw,
                    "action":       parsed,
                })
                return parsed
            except asyncio.TimeoutError:
                _write_llm_log({
                    "ts":        datetime.now(timezone.utc).isoformat(),
                    "type":      "timeout_error",
                    "model":     self.model,
                    "step":      step + 1,
                    "company":   self.company_name,
                    "job_title": self.job_title,
                    "snapshot":  snapshot,
                })
                print(f"  [LLM] API timeout on attempt {_attempt + 1}/3 — retrying" if _attempt < 2 else "  [LLM] API timeout after 3 attempts — giving up")
                continue
            except json.JSONDecodeError:
                _write_llm_log({
                    "ts":           datetime.now(timezone.utc).isoformat(),
                    "type":         "parse_error",
                    "model":        self.model,
                    "step":         step + 1,
                    "company":      self.company_name,
                    "job_title":    self.job_title,
                    "raw_response": raw,
                    "snapshot":     snapshot,
                })
                return {"action": "failed", "reason": f"LLM returned non-JSON: {raw[:120]}"}
            except Exception as exc:
                exc_name = type(exc).__name__
                if "RateLimit" in exc_name or "429" in str(exc):
                    wait = 20 * (_attempt + 1)
                    print(f"  [LLM] Rate limited — waiting {wait}s before retry {_attempt + 1}/3")
                    await asyncio.sleep(wait)
                    continue
                return {"action": "failed", "reason": f"LLM error ({exc_name}): {exc}"}
        return {"action": "failed", "reason": "LLM rate limited after 3 retries"}

    async def _llm_guided_apply(self, page: Page) -> str:
        """
        LLM-guided application loop.
        At each step: snapshot page → ask LLM → execute action → log → repeat.
        Returns 'applied' | 'failed'.
        """
        import os as _os
        auth_attempted = False
        _login_paths = (
            "/login", "/signin", "/sign-in", "/auth/login",
            "/dashboard/login", "/register", "/create-account", "/join",
        )
        _unblockable = ("persona", "identity", "verif", "captcha", "recaptcha")
        action_history: list[str] = []
        prev_url = ""
        unchanged_steps = 0

        # Skip enterprise ATS domains that require employer-provisioned accounts — no self-registration path.
        # Checked at domain level so Workday's /apply/applyManually (not a login URL) is caught too.
        _enterprise_ats_domains = ("icims.com", "taleo.net", "successfactors.com",
                                   "myworkdayjobs.com", "sap.com",
                                   "governmentjobs.com",     # NEOGOV — always requires pre-registered account
                                   "workforcenow.adp.com")   # ADP Workforce Now — lands on company portal, not job form
        _landing_domain = urlparse(page.url).netloc.lower()
        if any(ats in _landing_domain for ats in _enterprise_ats_domains):
            print(f"  [LLM] Enterprise ATS domain ({_landing_domain}) — skipping immediately")
            return "skipped"

        # Greenhouse embed detection: ?gh_jid= means the Greenhouse form lives inside a cross-origin
        # <iframe> from boards.greenhouse.io — Playwright's page evaluation can't see into it.
        # Navigate to the iframe src directly so the LLM operates on the actual form page.
        _qs = parse_qs(urlparse(page.url).query)
        if "gh_jid" in _qs:
            _gh_frames = [f for f in page.frames if "greenhouse.io" in f.url and f.url != page.url]
            if _gh_frames:
                _gh_url = _gh_frames[0].url
            else:
                _gh_token = _qs["gh_jid"][0]
                _gh_url = f"https://boards.greenhouse.io/embed/job_app?token={_gh_token}"
            print(f"  [LLM] Greenhouse iframe embed detected — navigating to {_gh_url}")
            try:
                await page.goto(_gh_url, wait_until="domcontentloaded", timeout=20000)
                await asyncio.sleep(2)
            except Exception as _gh_e:
                print(f"  [LLM] Could not navigate to Greenhouse direct URL ({_gh_e}) — skipping")
                return "skipped"

        # Check immediately if the landing page shows an expired/unavailable job.
        # URL check runs first (never throws); text check is separate so a JS error can't suppress the URL check.
        _expired_url_patterns = ("/second-chance", "/job-expired", "/job-not-found", "/404", "/error")
        _expired_text_phrases = (
            "no longer available", "opportunity is no longer", "position has been filled",
            "listing expired", "job has been filled", "this job is no longer", "job is closed",
            "no longer accepting applications", "job not found", "error: job not found",
            "this position has been", "this role has been filled",
        )
        if any(p in page.url.lower() for p in _expired_url_patterns):
            print(f"  [LLM] Landing URL indicates expired job ({page.url}) — skipping")
            return "expired"
        try:
            _landing_text = (await page.evaluate("() => (document.body.innerText || '').slice(0, 800)")).lower()
            if any(p in _landing_text for p in _expired_text_phrases):
                print(f"  [LLM] Landing page text indicates expired job — skipping")
                return "expired"
        except Exception as _e:
            print(f"  [LLM] Warning: could not read landing page text for expired check ({_e})")

        # Claude script-generation engine: reads full DOM and generates a complete Playwright
        # script per page. Preferred over the step loop when ANTHROPIC_API_KEY is configured.
        if os.getenv("ANTHROPIC_API_KEY"):
            try:
                from claude_apply import ClaudeScriptApplyEngine as _ClaudeEngine
                _ce = _ClaudeEngine(
                    profile=self.profile,
                    context=self.context,
                    company_name=self.company_name,
                    job_title=self.job_title,
                )
                _cr = await _ce.apply(page)
                print(f"  [Claude] Engine result: {_cr}")
                if _cr != "failed":
                    return _cr
                print("  [Claude] Engine returned failed — falling back to step loop")
            except Exception as _ce_err:
                print(f"  [Claude] Engine error ({_ce_err}) — falling back to step loop")

        for step in range(20):
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=8000)
            except Exception:
                pass
            await asyncio.sleep(2)

            current_url = page.url
            print(f"  [LLM] Step {step + 1} — {current_url}")

            # Auth wall (URL-based)
            if any(p in current_url.lower() for p in _login_paths):
                if auth_attempted:
                    print("  [LLM] Auth already attempted — giving up")
                    return "failed"
                auth_attempted = True
                ok = await self._handle_auth_page(page)
                if not ok:
                    # Any auth wall we can't get past (no registration link, SSO-only, enterprise ATS)
                    # is not an agent error — count as skipped so error rates reflect real failures.
                    _auth_domain = urlparse(page.url).netloc.lower()
                    print(f"  [LLM] Could not authenticate on {_auth_domain} — skipping")
                    return "skipped"
                await asyncio.sleep(2)
                continue

            # Dismiss cookie banners
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
                        await asyncio.sleep(1)
                        break
                except Exception:
                    pass

            # Step 0: try common Apply buttons deterministically before invoking LLM.
            # Prevents wrong-CTA picks (e.g. "Join our talent community", "Add to favorites")
            # on pages where the real Apply button is present but not the LLM's top choice.
            if step == 0:
                _det_apply_sels = [
                    'a:has-text("Apply for this job")',    'button:has-text("Apply for this job")',
                    'a:has-text("Apply for this position")', 'button:has-text("Apply for this position")',
                    'a:has-text("Apply Now")',             'button:has-text("Apply Now")',
                    'a:has-text("Apply now")',             'button:has-text("Apply now")',
                    '[data-automation-id*="applyButton"]',
                    'a[href*="/apply"]:not([href*="linkedin"]):not([href*="talent"])',
                ]
                for _asel in _det_apply_sels:
                    try:
                        _abtn = page.locator(_asel).first
                        if await _abtn.count() == 0 or not await _abtn.is_visible():
                            continue
                        _btn_text = (await _abtn.inner_text()).strip()[:50]
                        _url_before_det = page.url
                        print(f"  [LLM] Step 0 deterministic apply: {_btn_text!r}")
                        try:
                            async with self.context.expect_page(timeout=3000) as _npi:
                                await _abtn.click()
                            page = await _npi.value
                            await page.wait_for_load_state("domcontentloaded", timeout=10000)
                        except Exception:
                            await _abtn.click()
                            await asyncio.sleep(2)
                        if page.url != _url_before_det:
                            print(f"  [LLM] Deterministic click navigated → {page.url}")
                            prev_url = page.url
                        break  # found a match — proceed with snapshot of current page
                    except Exception:
                        continue

            # Get page snapshot
            snapshot = await self._get_page_snapshot(page)

            # Mid-loop check: detect Cloudflare bot-gates and "job no longer available" after navigation
            if step > 0:
                try:
                    _mid_text = (await page.evaluate("() => (document.body.innerText || '').slice(0, 400)")).lower()
                    _cloudflare_signals = ("performing security verification", "security service to protect",
                                          "enable javascript and cookies", "ray id:")
                    if any(s in _mid_text for s in _cloudflare_signals):
                        print(f"  [LLM] Cloudflare security wall detected mid-loop — skipping")
                        return "skipped"
                    if any(p in _mid_text for p in _expired_text_phrases):
                        print(f"  [LLM] Expired job text detected mid-loop — skipping")
                        return "expired"
                except Exception:
                    pass

            # Detect stuck pages using URL only — avoids false resets from cosmetic mutations
            # (e.g. "Add to favorites" → "Favorited" changes text but not URL)
            if step > 0 and page.url == prev_url:
                unchanged_steps += 1
                if unchanged_steps >= 3:
                    # Before giving up, try a deterministic submit click — covers single-page forms
                    # (e.g. Rippling) where the URL never changes until after submission.
                    for _submit_sel in (
                        'button[type="submit"]', 'input[type="submit"]',
                        'button:has-text("Apply")', 'button:has-text("Submit")',
                        'button:has-text("Submit application")',
                    ):
                        try:
                            _sbtn = page.locator(_submit_sel).first
                            if await _sbtn.count() > 0 and await _sbtn.is_visible():
                                _stext = (await _sbtn.inner_text()).strip()[:40]
                                print(f"  [LLM] Stuck guard: attempting deterministic submit: {_stext!r}")
                                return await self._handle_submit(page, _sbtn)
                        except Exception:
                            continue
                    print(f"  [LLM] URL unchanged for 3 consecutive steps — browser is stuck, giving up")
                    return "failed"
            else:
                unchanged_steps = 0
            prev_url = page.url

            # Throttle to avoid rate limits
            if step > 0:
                await asyncio.sleep(8)

            # Ask LLM (pass history for memory)
            action = await self._ask_llm_action(snapshot, step, action_history, current_url=page.url)
            action_type = action.get("action", "failed")
            reason = action.get("reason", "")
            selector = action.get("selector", "")
            text = action.get("text", "")
            value = action.get("value", "")

            print(f"  [LLM] Thought: {reason}")
            _display_value = (self.profile.get("resume_path", value) if action_type == "upload" else value)
            print(f"  [LLM] Action: {action_type}" + (f" → {selector or text!r}" if selector or text else "") + (f" = {_display_value[:80]!r}" if _display_value else ""))

            # Bail if the LLM proposes an action already attempted — page is not advancing
            if action_type not in ("done", "failed", "scroll"):
                hist_key = f"{action_type}:{selector or text}"
                if any(h == hist_key or h.startswith(hist_key + "=") for h in action_history):
                    print(f"  [LLM] Action '{hist_key}' already in history — page not advancing, giving up")
                    return "failed"

            if action_type == "done":
                print("  [LLM] Application confirmed complete!")
                return "applied"

            if action_type == "failed":
                _expired_reasons = ("no longer available", "job not found", "position closed", "job closed", "expired")
                if any(k in reason.lower() for k in _expired_reasons):
                    print(f"  [LLM] Job reported as expired/closed ({reason}) — skipping")
                    return "expired"
                # Detect unblockable verification walls
                if any(k in reason.lower() for k in _unblockable):
                    print(f"  [LLM] Unblockable wall ({reason}) — skipping job")
                    return "skipped"
                return "failed"

            if action_type == "scroll":
                try:
                    await page.evaluate("window.scrollBy(0, window.innerHeight * 0.85)")
                except Exception:
                    pass

            elif action_type == "upload":
                resume_path = self.profile.get("resume_path", "")
                if resume_path:
                    abs_path = _os.path.abspath(resume_path) if not _os.path.isabs(resume_path) else resume_path
                    if _os.path.isfile(abs_path) and selector:
                        try:
                            el = page.locator(selector).first
                            if await el.count() > 0:
                                await el.set_input_files(abs_path)
                                await asyncio.sleep(1)
                        except Exception as exc:
                            print(f"  [LLM] Upload failed: {exc}")

            elif action_type == "fill" and selector and value:
                def _safe_selector(sel: str) -> str:
                    """Convert #<id-starting-with-digit> to [id="..."] (valid CSS)."""
                    if sel.startswith("#") and len(sel) > 1 and sel[1].isdigit():
                        return f'[id="{sel[1:]}"]'
                    tag_prefix = ""
                    if sel.startswith("input#") and sel[6:7].isdigit():
                        return f'input[id="{sel[6:]}"]'
                    return sel
                try:
                    safe_sel = _safe_selector(selector)
                    el = page.locator(safe_sel).first
                    if await el.count() == 0 and text:
                        el = page.locator(f'input[placeholder*="{text}" i], input[name*="{text}" i]').first
                    if await el.count() > 0:
                        await _human_type(el, value)
                        await asyncio.sleep(0.5)
                        # Auto-select location autocomplete suggestion
                        if any(k in (action.get("label", "") + selector).lower() for k in ("location", "city")):
                            await asyncio.sleep(1)
                            sug = page.locator('[role="option"], [role="listbox"] li').first
                            if await sug.count() > 0:
                                await sug.click()
                except Exception as exc:
                    print(f"  [LLM] Fill failed: {exc}")

            elif action_type == "select" and selector and value:
                try:
                    el = page.locator(selector).first
                    if await el.count() > 0:
                        try:
                            await el.select_option(label=value)
                        except Exception:
                            await el.select_option(value=value)
                        await asyncio.sleep(0.5)
                except Exception as exc:
                    print(f"  [LLM] Select failed: {exc}")

            elif action_type == "click":
                _submit_keywords = (
                    "submit application", "submit", "send application", "complete application",
                    "finish application", "finish", "send my application",
                )
                combined = (selector + " " + text + " " + value).lower()
                is_submit_btn = any(k in combined for k in _submit_keywords)

                loc_strs = [selector, f'button:has-text("{text}")', f'a:has-text("{text}")', f'[aria-label*="{text}"]']
                loc_strs = [l for l in loc_strs if l and l not in ('button:has-text("")', 'a:has-text("")', '[aria-label*=""]')]

                if is_submit_btn:
                    # Route submit-type buttons through ready_to_submit callback
                    for loc_str in loc_strs:
                        try:
                            el = page.locator(loc_str).first
                            if await el.count() > 0 and await el.is_visible():
                                return await self._handle_submit(page, el)
                        except Exception:
                            continue
                    print(f"  [LLM] Submit button not found: {selector!r} / {text!r}")
                else:
                    clicked = False
                    for loc_str in loc_strs:
                        try:
                            el = page.locator(loc_str).first
                            if await el.count() > 0:
                                # Watch for new tab
                                try:
                                    async with self.context.expect_page(timeout=3000) as npi:
                                        await el.click()
                                    new_tab = await npi.value
                                    await new_tab.wait_for_load_state("domcontentloaded", timeout=10000)
                                    page = new_tab
                                except Exception:
                                    await el.click()
                                    await asyncio.sleep(2)
                                clicked = True
                                break
                        except Exception:
                            continue
                    if not clicked:
                        print(f"  [LLM] Click target not found: {selector!r} / {text!r}")

            # Record action in history (keep last 10)
            hist_entry = f"{action_type}:{selector or text}"
            if value:
                hist_entry += f"={value[:30]}"
            action_history.append(hist_entry)
            if len(action_history) > 10:
                action_history = action_history[-10:]

        print("  [LLM] Reached step limit without completion")
        return "failed"

    async def _fill_external_form(self, page: Page) -> str:
        return await self._llm_guided_apply(page)

    async def _handle_submit(self, page: Page, submit_btn) -> str:
        summary = (
            f"External application for {self.profile.get('full_name', 'user')} "
            f"at {self.company_name or page.url}."
        )
        ready = self.callbacks.get("ready_to_submit")
        result = ready(summary) if ready else "applied"

        if result == "applied":
            url_before = page.url
            try:
                await submit_btn.click()
            except Exception:
                pass
            # Wait for navigation/load — try networkidle first, fall back to domcontentloaded
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=5000)
                except Exception:
                    pass
            # Give SPA a moment to render confirmation content
            await asyncio.sleep(2)
            # If URL hasn't changed yet, wait a bit longer for redirect
            if page.url == url_before:
                try:
                    await page.wait_for_url(lambda u: u != url_before, timeout=5000)
                except Exception:
                    pass
            confirmed, msg = await self._check_submission_result(page, url_before)
            if confirmed:
                print(f"  [Offsite] Submission confirmed: {msg}")
                return "applied"
            else:
                print(f"  [Offsite] ⚠ Submit clicked but {msg}")
                return "failed"
        return "skipped"

    async def _check_submission_result(self, page: Page, url_before: str) -> tuple[bool, str]:
        """Returns (success, description). Used to verify a form submission went through."""
        _CONFIRM_PHRASES = (
            "thank you for applying", "thank you for your application",
            "application submitted", "application received", "application is complete",
            "successfully submitted", "has been submitted", "we received your application",
            "we'll be in touch", "we will be in touch", "your application was sent",
            "application was sent", "you have applied", "you've applied",
            "you applied", "submission received", "we received your",
        )
        _FAIL_PHRASES = (
            "sign in", "log in", "login", "create an account", "register",
            "error", "something went wrong", "page not found", "404",
            "session expired", "please try again",
        )
        try:
            content = (await page.content()).lower()

            # Definite success — confirmation text present
            for phrase in _CONFIRM_PHRASES:
                if phrase in content:
                    return True, f"found '{phrase}' on page"

            # URL changed — check if destination looks like success or failure
            if page.url != url_before:
                new_url = page.url.lower()
                # Known failure destinations
                if any(k in new_url for k in ("login", "signin", "sign-in", "register", "error", "404")):
                    return False, f"URL changed to failure page → {page.url[:80]}"
                # Redirected back to same listing or very similar path — not a success
                if new_url.rstrip("/") == url_before.lower().rstrip("/"):
                    return False, f"URL unchanged after normalisation → {page.url[:80]}"
                # Check content of new page for failure signals
                if any(p in content for p in _FAIL_PHRASES[:4]):  # sign in / log in / login / create account
                    return False, f"redirected to auth/error page → {page.url[:80]}"
                # URL changed to something that isn't a known failure — treat as success
                return True, f"URL changed → {page.url[:80]}"

            # Check for form validation errors on same page
            error_clues = [p for p in ("required", "please complete", "please fill",
                                       "missing", "invalid email") if p in content]
            if error_clues:
                return False, f"form showing validation errors: {error_clues[:2]}"
            return False, "URL unchanged and no confirmation text found"
        except Exception as exc:
            return False, f"verification error: {exc}"

    # ── Authentication helpers ─────────────────────────────────────────────────

    async def _handle_auth_page(self, page: Page) -> bool:
        """
        Handles a 3rd-party login or registration page.
        Checks stored credentials first; falls back to registering a new account.
        Returns True if we appear to be authenticated afterward.
        """
        domain = urlparse(page.url).netloc
        print(f"  [Auth] Handling auth for {domain}")

        # Try "Continue with LinkedIn" first — we're already logged in to LinkedIn
        linkedin_btn = page.locator(
            'a:has-text("Continue with LinkedIn"), button:has-text("Continue with LinkedIn"), '
            'a:has-text("Sign in with LinkedIn"), button:has-text("Sign in with LinkedIn"), '
            '[aria-label*="LinkedIn"]'
        ).first
        if await linkedin_btn.count() > 0:
            print(f"  [Auth] Clicking 'Continue with LinkedIn'")
            url_before = page.url
            try:
                async with self.context.expect_page(timeout=8000) as new_page_info:
                    await linkedin_btn.click()
                li_page = await new_page_info.value
                await li_page.wait_for_load_state("domcontentloaded", timeout=10000)
                # LinkedIn OAuth — may auto-approve if already logged in
                for allow_sel in (
                    'button:has-text("Allow")', 'button:has-text("Authorize")',
                    'button:has-text("Continue")', '[aria-label*="Allow"]',
                ):
                    allow = li_page.locator(allow_sel).first
                    if await allow.count() > 0:
                        await allow.click()
                        await asyncio.sleep(2)
                        break
                await asyncio.sleep(3)
                if page.url != url_before or "linkedin.com" not in page.url:
                    print(f"  [Auth] LinkedIn OAuth succeeded")
                    return True
            except Exception:
                # No new tab — LinkedIn OAuth may complete in same tab
                await asyncio.sleep(4)
                if page.url != url_before:
                    print(f"  [Auth] LinkedIn OAuth succeeded (same tab)")
                    return True
            print(f"  [Auth] LinkedIn OAuth didn't navigate — falling through")

        existing = _find_account_for_domain(domain)
        if existing:
            print(f"  [Auth] Found stored credentials for {domain} — trying login")
            if await self._try_login(page, existing["email"], existing["password"]):
                print(f"  [Auth] Login successful")
                return True
            print(f"  [Auth] Login failed with stored credentials")

        print(f"  [Auth] No valid credentials for {domain} — attempting registration")
        return await self._try_register(page, domain)

    async def _try_login(self, page: Page, email: str, password: str) -> bool:
        """Fill login form and submit. Returns True if URL changed away from login page."""
        url_before = page.url
        try:
            for sel in ('input[type="email"]', '#email', 'input[name*="email" i]',
                        'input[placeholder*="email" i]', 'input[name*="user" i]'):
                el = page.locator(sel).first
                if await el.count() > 0 and await el.is_visible():
                    await _human_type(el, email)
                    await asyncio.sleep(random.uniform(0.3, 0.7))
                    break
            for sel in ('input[type="password"]', '#password', 'input[name*="password" i]'):
                el = page.locator(sel).first
                if await el.count() > 0 and await el.is_visible():
                    await _human_type(el, password)
                    await asyncio.sleep(random.uniform(0.2, 0.5))
                    break
            for sel in (
                '[data-automation-id="click_filter"]',
                '[data-automation-id="signInSubmitButton"]',
                'button[type="submit"]', 'input[type="submit"]',
                'button:has-text("Sign in")', 'button:has-text("Sign In")',
                'button:has-text("Log in")', 'button:has-text("Login")',
                'button:has-text("Continue")',
            ):
                btn = page.locator(sel).first
                if await btn.count() > 0:
                    try:
                        await btn.click(timeout=5000)
                    except Exception:
                        try:
                            await btn.click(force=True, timeout=5000)
                        except Exception:
                            continue
                    break
            await asyncio.sleep(3)
            return page.url != url_before
        except Exception:
            return False

    async def _try_register(self, page: Page, domain: str) -> bool:
        """
        Register a new account on the current site.
        If on a login page, looks for a "Create account" / "Sign up" link first.
        Fills the registration form and saves credentials.
        Returns True if the URL changed after submitting (indicating success).
        """
        try:
            # Check if we're already on a registration page (has confirm-password field)
            confirm_field = page.locator(
                'input[name*="confirm" i], input[name*="repeat" i], input[placeholder*="confirm" i]'
            ).first
            is_reg_page = await confirm_field.count() > 0

            if not is_reg_page:
                reg_selectors = [
                    'a:has-text("Create account")', 'a:has-text("Create Account")',
                    'button:has-text("Create account")', 'button:has-text("Create Account")',
                    'a:has-text("Create an account")', 'a:has-text("Create a new account")',
                    'a:has-text("Sign up")', 'a:has-text("Sign Up")',
                    'button:has-text("Sign up")', 'button:has-text("Sign Up")',
                    '[role="button"]:has-text("Sign up")', '[role="button"]:has-text("Sign Up")',
                    '[role="tab"]:has-text("Sign up")', '[role="tab"]:has-text("Sign Up")',
                    'a:has-text("Register")', 'button:has-text("Register")',
                    'a:has-text("New user")', 'a:has-text("New User")',
                    'a:has-text("New User Registration")',
                    'a:has-text("Don\'t have an account")',
                    'a:has-text("No account")',
                    'a[href*="register"]', 'a[href*="signup"]', 'a[href*="create"]',
                    '[href*="signup"]', '[href*="register"]',
                ]
                clicked = False
                for sel in reg_selectors:
                    el = page.locator(sel).first
                    if await el.count() > 0 and await el.is_visible():
                        try:
                            await el.click(timeout=5000)
                        except Exception:
                            # Overlay interception (e.g. Workday) — try force click
                            try:
                                await el.click(force=True, timeout=5000)
                            except Exception:
                                try:
                                    await page.evaluate("el => el.click()", await el.element_handle())
                                except Exception:
                                    continue
                        await asyncio.sleep(2)
                        try:
                            await page.wait_for_load_state("domcontentloaded", timeout=5000)
                        except Exception:
                            pass
                        clicked = True
                        print(f"  [Auth] Navigated to registration via {sel!r}")
                        break
                if not clicked:
                    # Log visible links/buttons to help identify the correct selector
                    try:
                        visible = await page.evaluate(
                            "() => Array.from(document.querySelectorAll('a,button,[role=\"button\"],[role=\"tab\"]'))"
                            ".filter(e => e.offsetParent !== null)"
                            ".map(e => (e.textContent || '').trim().slice(0, 50))"
                            ".filter(t => t).slice(0, 15)"
                        )
                        print(f"  [Auth] Visible links/buttons: {visible}")
                    except Exception:
                        pass
                    print(f"  [Auth] No registration link found on {domain}")
                    return False

            url_before = page.url
            await self._fill_registration_form(page)

            # Workday uses an overlay div that intercepts clicks on the real submit button.
            # Try the overlay div first, then fall back to normal + force click.
            reg_submit_selectors = (
                '[data-automation-id="click_filter"]',  # Workday overlay
                '[data-automation-id="createAccountSubmitButton"]',
                'button[type="submit"]', 'input[type="submit"]',
                'button:has-text("Create account")', 'button:has-text("Create Account")',
                'button:has-text("Register")', 'button:has-text("Sign up")',
                'button:has-text("Sign Up")', 'button:has-text("Submit")',
                'button:has-text("Continue")',
            )
            for sel in reg_submit_selectors:
                btn = page.locator(sel).first
                if await btn.count() > 0:
                    try:
                        await btn.click(timeout=5000)
                    except Exception:
                        try:
                            await btn.click(force=True, timeout=5000)
                        except Exception:
                            try:
                                await page.evaluate("el => el.click()", await btn.element_handle())
                            except Exception:
                                continue
                    break
            await asyncio.sleep(3)

            # Determine success: URL changed OR confirmation text present
            url_changed = page.url != url_before
            try:
                body = (await page.content()).lower()
            except Exception:
                body = ""
            _success_phrases = ("verify your email", "check your email", "account created",
                                 "registration complete", "welcome", "thank you for registering",
                                 "successfully created", "sign in", "log in")
            _error_phrases = ("error", "invalid", "already exists", "already registered",
                              "please try again", "something went wrong")
            confirmed = url_changed or any(p in body for p in _success_phrases)
            has_error = any(p in body for p in _error_phrases)

            if not confirmed or has_error:
                print(f"  [Auth] Registration appears to have failed — not saving credentials")
                return False

            record = {
                "created_at": datetime.now(timezone.utc).isoformat(),
                "company": domain,
                "website_url": page.url,
                "email": self.profile.get("email", ""),
                "password": self.generated_password,
                "job_title": self.job_title,
                "notes": f"Auto-registered on {domain}",
            }
            save_fn = self.callbacks.get("save_account")
            if save_fn:
                save_fn(record)
            else:
                _append_account(record)
            print(f"  [Auth] Account registered on {domain} — credentials saved")
            return True

        except Exception as exc:
            print(f"  [Auth] Registration error: {exc}")
            return False

    async def _fill_registration_form(self, page: Page):
        """Fill standard registration fields using profile data."""
        p = self.profile
        pw = self.generated_password
        parts = (p.get("full_name") or "").split()
        first = parts[0] if parts else ""
        last = parts[-1] if len(parts) > 1 else first

        async def _try(selectors: list, value: str):
            for sel in selectors:
                el = page.locator(sel).first
                if await el.count() > 0 and await el.is_visible():
                    try:
                        await _human_type(el, value)
                        await asyncio.sleep(random.uniform(0.2, 0.5))
                    except Exception:
                        pass
                    return

        await _try(['#firstName', 'input[name="firstName"]', 'input[name*="first" i]',
                    'input[placeholder*="first name" i]'], first)
        await _try(['#lastName', 'input[name="lastName"]', 'input[name*="last" i]',
                    'input[placeholder*="last name" i]'], last)
        await _try(['input[type="email"]', '#email', 'input[name*="email" i]',
                    'input[placeholder*="email" i]'], p.get("email", ""))

        # Fill all visible password fields (covers password + confirm password)
        pw_fields = page.locator('input[type="password"]')
        count = await pw_fields.count()
        for i in range(min(count, 2)):
            el = pw_fields.nth(i)
            if await el.is_visible():
                try:
                    await _human_type(el, pw)
                    await asyncio.sleep(random.uniform(0.2, 0.4))
                except Exception:
                    pass

        await _try(['input[type="tel"]', 'input[name*="phone" i]',
                    'input[placeholder*="phone" i]'], p.get("phone", ""))
