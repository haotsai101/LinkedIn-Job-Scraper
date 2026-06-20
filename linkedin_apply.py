"""
linkedin_apply.py — Deterministic Playwright LinkedIn job application engine.

EasyApplyFlow   : LinkedIn Easy Apply modal (SimpleOnsiteApply, ComplexOnsiteApply)
OffsiteApplyFlow: External company career site (OffsiteApply)
"""

import asyncio
import json
import os
import random
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from openai import OpenAI, AsyncOpenAI
from playwright.async_api import Page, BrowserContext


_LLM_LOG_PATH = "llm_debug.jsonl"
# Session timestamp prefix for screenshot filenames — ensures cross-run uniqueness
_SESSION_TS = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _css_id(el_id: str) -> str:
    """Escape CSS-special characters in an element ID for use in a #id selector."""
    return re.sub(r'([\[\]().#+*?^$|{},~>\\])', r'\\\1', el_id)


def _write_llm_log(entry: dict):
    # Best-effort telemetry: swallow any IO/serialization error so callers
    # (especially except-branch handlers that must always return None) are protected.
    try:
        import json as _json
        with open(_LLM_LOG_PATH, "a") as f:
            f.write(_json.dumps(entry) + "\n")
    except Exception:
        pass


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
    // Use the same selector that _get_modal_text uses for correct text extraction.
    // .jobs-easy-apply-content is the innermost form content div LinkedIn uses.
    // Avoid querySelector('[role="dialog"]') — it returns the FIRST dialog in the DOM
    // which may be a LinkedIn preferences sidebar, not the apply form.
    var modal = document.querySelector('.jobs-easy-apply-content')
             || document.querySelector('.jobs-easy-apply-modal .artdeco-modal__content')
             || document.querySelector('.jobs-apply-form')
             || document.querySelector('[data-test-modal-container]')
             || (function() {
                   var ds = document.querySelectorAll('[role="dialog"]');
                   return ds.length ? ds[ds.length - 1] : null;
                })()
             || document.body;
    var results = [];
    var radioNames = new Set();

    function isHidden(el) {
        // Walk up the DOM to check if any ancestor hides this element
        var node = el;
        while (node && node !== document.body) {
            var s = window.getComputedStyle(node);
            if (s.display === 'none' || s.visibility === 'hidden') return true;
            if (node.getAttribute('aria-hidden') === 'true') return true;
            if (node.hasAttribute('hidden')) return true;
            node = node.parentElement;
        }
        return false;
    }

    modal.querySelectorAll(
        'input:not([type=hidden]):not([type=submit]):not([type=button])' +
        ':not([type=radio]),textarea,select'
    ).forEach(function(el) {
        if (isHidden(el)) return;
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

    // Capture contenteditable/role=textbox elements (LinkedIn's rich-text screening questions)
    var seenContentEditable = new Set();
    modal.querySelectorAll(
        '[contenteditable="true"]:not([aria-readonly="true"]), [role="textbox"]:not([aria-readonly="true"])'
    ).forEach(function(el) {
        if (isHidden(el)) return;
        var ariaLbl = el.getAttribute('aria-labelledby') || '';
        var lblEl = ariaLbl ? document.getElementById(ariaLbl) : null;
        var labelText = (lblEl && lblEl.textContent.trim()) || el.getAttribute('aria-label') || '';
        if (!labelText) {
            // Walk up through ancestors to find a nearby label/heading
            var node = el.parentElement;
            var searchDepth = 0;
            while (node && node !== modal && searchDepth < 6) {
                // Look for label/legend/heading sibling of current node
                var lh = node.querySelector('label, legend, h3, h4, [class*="label"], [class*="heading"], [class*="title"]');
                if (lh && lh !== el && !lh.contains(el)) {
                    labelText = lh.textContent.trim();
                    break;
                }
                // Also check previous sibling of node
                var prev = node.previousElementSibling;
                if (prev && prev.textContent.trim()) {
                    var prevText = prev.textContent.trim();
                    // Only use if it looks like a question (reasonable length, not just a checkbox value)
                    if (prevText.length > 5 && prevText.length < 300) {
                        labelText = prevText;
                        break;
                    }
                }
                node = node.parentElement;
                searchDepth++;
            }
        }
        var key = labelText || el.id || ariaLbl || ('contenteditable-' + results.length);
        if (seenContentEditable.has(key)) return;
        seenContentEditable.add(key);
        var currentText = (el.innerText || el.textContent || '').trim();
        results.push({
            kind: 'contenteditable',
            label: labelText,
            id: el.id || '',
            name: el.getAttribute('name') || '',
            aria_labelledby: ariaLbl,
            options: [],
            current_value: currentText
        });
    });

    modal.querySelectorAll('input[type="radio"]').forEach(function(el) {
        if (isHidden(el)) return;
        var grpName = el.name;
        if (!grpName || radioNames.has(grpName)) return;
        radioNames.add(grpName);
        var fieldset = el.closest('fieldset') || el.closest('[role="group"]') || el.closest('[role="radiogroup"]');
        var groupLabel = grpName;
        if (fieldset) {
            var labelId = fieldset.getAttribute('aria-labelledby');
            var labelEl = labelId ? document.getElementById(labelId) : null;
            var legend = fieldset.querySelector('legend');
            groupLabel = (labelEl && labelEl.textContent.trim()) || (legend && legend.textContent.trim()) || grpName;
        }
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
        if (isHidden(el)) return;
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
        var fieldset = el.closest('fieldset') || el.closest('[role="group"]') || el.closest('[role="radiogroup"]');
        var groupLabel = grpName;
        if (fieldset) {
            var labelId = fieldset.getAttribute('aria-labelledby');
            var labelEl = labelId ? document.getElementById(labelId) : null;
            var legend = fieldset.querySelector('legend');
            groupLabel = (labelEl && labelEl.textContent.trim()) || (legend && legend.textContent.trim()) || grpName;
        }
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

def _degree_rank(deg: str) -> int:
    """Map a degree string (full name or abbreviation) to a numeric rank for hierarchy
    comparisons. Substring matching on raw text fails for abbreviations like "M.S." (does
    not contain "master"), so callers must compare ranks rather than substrings.
    0 = none, 1 = associate, 2 = bachelor, 3 = master, 4 = doctorate."""
    d = deg.lower()
    if any(t in d for t in ("ph.d", "phd", "doctor", "d.sc", "dsc", "edd", "ed.d")):
        return 4
    if any(t in d for t in ("master", "m.s.", "mba", "m.eng", "meng", "m.a.")) \
            or re.search(r'\b(ms|ma)\b', d):
        return 3
    if any(t in d for t in ("bachelor", "b.s.", "b.a.", "b.eng")) \
            or re.search(r'\b(bs|ba)\b', d):
        return 2
    if any(t in d for t in ("associate", "a.s.", "a.a.")):
        return 1
    return 0


def _get_profile_value(profile: dict, label: str, kind: str = "text") -> str | None:
    """Map a form field label to a profile value. Returns None if no confident match."""
    # Collapse all whitespace (including embedded newlines from DOM textContent),
    # strip asterisk/required markers, then normalize spaces.
    l = label.lower().replace("_", " ")
    l = re.sub(r'\s+', ' ', l).strip()            # collapse newlines → single space first
    l = re.sub(r'\*?\s*required\s*$', '', l).strip()  # trailing "Required" / "* Required"
    l = re.sub(r'\*\s*required\b', '', l)          # mid-string "*Required"
    l = l.replace("*", "").replace("(required)", "").strip()
    p = profile

    # Never return a resume path for cover letter fields
    if any(k in l for k in ("cover letter", "cover_letter", "covering letter")):
        return None
    if kind == "file" or (kind not in ("select", "select-one", "select-multiple", "radio", "checkbox") and any(k in l for k in ("resume", "résumé", "cv", "upload your resume", "upload your résumé", "attach resume", "attach résumé", "attach cv"))):
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
    if any(k in l for k in ("phone country code", "country code", "country dial")):
        return p.get("country", "United States")
    if "phone" in l or "mobile" in l:
        return p.get("phone")
    if "linkedin" in l:
        return p.get("linkedin_url")
    if "github" in l:
        return p.get("github_url")
    if any(k in l for k in ("portfolio", "personal website", "personal site")):
        return p.get("portfolio_url")
    if "website" in l and "personal" not in l:
        return p.get("website_url") or p.get("portfolio_url") or p.get("linkedin_url")
    if any(k in l for k in ("zip", "postal")):
        return p.get("zip_code")
    if any(k in l for k in ("address line 1", "street address", "address 1", "street")):
        return p.get("street_address")
    if "address line 2" in l or "address 2" in l or "apt" in l or "suite" in l:
        return ""  # leave blank
    if any(k in l for k in ("which state", "your state", "state of residence", "state you live", "province", "state/province")) or (("state" in l or "province" in l) and "united states" not in l and "authorized" not in l and "visa" not in l):
        return p.get("state")
    # Identity fields checked BEFORE generic country/location to prevent cross-matching
    if any(k in l for k in ("disability", "disabled")):
        return p.get("disability_status", "No")
    if "gender" in l:
        return p.get("gender", "decline")
    if any(k in l for k in ("race", "ethnicity", "ethnic")):
        return p.get("race", "decline")
    if any(k in l for k in ("veteran", "military status", "protected veteran")):
        return p.get("veteran_status", "No")
    # Work eligibility / right-to-work country picker — must fire BEFORE generic "country" rule
    if any(k in l for k in ("right to work", "verify right to work",
                             "work from one of the following", "currently based in and can verify")):
        return p.get("country", "United States")
    # "Are you currently US based?" / "Are you based in the US?" style questions
    if any(k in l for k in ("us based", "u.s. based", "united states based",
                             "currently based in the us", "currently based in the united states",
                             "currently residing in the us", "located in the us",
                             "located in the united states")):
        return "Yes"
    # Geographic residency exclusion questions — user resides in Utah, USA.
    # Combinatorial match so every verb/region pairing is covered symmetrically
    # (e.g. "based in South America", "located in Central America", "located in Latin").
    _RESIDE_VERBS = ("reside in", "based in", "located in", "live in")
    _EXCL_REGIONS = ("mexico", "latin", "south america", "central america")
    if any(f"{v} {r}" in l for v in _RESIDE_VERBS for r in _EXCL_REGIONS) \
            and kind in ("select", "select-one", "radio"):
        return "No"
    # Relocation yes/no questions — user targets remote roles, not willing to relocate
    if any(k in l for k in ("open to relocat", "willing to relocat", "able to relocat",
                             "relocation assistance", "relocate to")) and kind in ("select", "select-one", "radio"):
        return "No"
    # "REST OF WORLD" conditional follow-up — leave blank (user is US-based)
    if "rest of world" in l:
        return ""
    if "country" in l and "relocat" not in l:
        return p.get("country", "United States")
    if any(k in l for k in ("city", "location", "where are you", "your location", "current location", "what is your current location", "city, state", "city/state")) and "relocat" not in l:
        return p.get("location") or p.get("city")
    # Only match postal/physical address fields — avoid "addressed", "redress", "address it", etc.
    if any(k in l for k in ("mailing address", "home address", "postal address", "billing address", "current address", "your address")) or l.strip() == "address":
        return p.get("street_address") or p.get("location")
    if any(k in l for k in ("middle name", "middle initial")):
        return ""  # user has no middle name
    if any(k in l for k in ("current company", "current employer", "current organization",
                             "employer name", "company name", "organization name",
                             "most recent employer", "most recent company")) or l in ("org", "company", "organization", "employer"):
        return p.get("current_company") or p.get("employer") or "N/A"
    if any(k in l for k in ("current title", "job title", "current role", "current position")):
        return p.get("current_title")
    if any(k in l for k in ("headline", "professional headline", "profile headline")):
        return p.get("headline") or p.get("current_title")
    if any(k in l for k in ("summary", "professional summary", "about me", "bio")):
        return p.get("summary") or p.get("cover_letter_text")
    if any(k in l for k in ("years of experience", "years experience", "total experience")):
        return str(p.get("years_experience", ""))
    if any(k in l for k in (
        "generative ai", "gen ai", "llm", "large language model",
        "multimodal", "agentic", "rag", "retrieval augmented",
        "vector database", "embedding", "fine tun",
        "artificial intelligence", " ai ", "machine learning", "deep learning",
        "neural network", "natural language", "nlp", "computer vision",
        "data science", "data scientist", "model training", "model development",
        "model deployment", "ai/ml", "ai agent", "ai engineer", "prompt engineer",
        "transformer", "diffusion model", "reinforcement learning",
    )) and kind in ("text", "number"):
        return "1"
    if any(k in l for k in ("how many years", "how many months")) and kind in ("text", "number"):
        # Default to 0 for technology-specific year questions we can't match
        return "0"
    if any(k in l for k in ("sponsor", "sponsorship", "visa support", "work visa")):
        if p.get("need_sponsorship", "").lower() in ("yes", "true", "1"):
            return "Yes"
        auth = (p.get("work_authorization") or "").upper()
        return "Yes" if any(x in auth for x in ("OPT", "H1B", "H-1B", "F1", "TN")) else "No"
    if any(k in l for k in ("authorized to work", "legally authorized", "legal right to work", "eligible to work")):
        return "Yes"
    if any(k in l for k in ("work authorization expire", "authorization expir", "visa expir")):
        return p.get("work_authorization_expiry", "N/A")
    if any(k in l for k in ("salary", "compensation", "expected pay", "desired pay")):
        raw_sal = str(p.get("preferred_salary", ""))
        # If stored as a range (e.g. "100000 - 120000"), return the upper bound for
        # fields that require a single numeric value.
        if "-" in raw_sal or "–" in raw_sal:
            parts = re.split(r'[-–]', raw_sal)
            nums = [re.sub(r'[^\d.]', '', x) for x in parts]
            nums = [x for x in nums if x]
            if nums:
                return nums[-1]  # upper bound
        return raw_sal
    # Specific-degree completion questions: "Have you completed [DEGREE]?"
    # Must run BEFORE the generic education branch, which would otherwise answer
    # "Yes" for any degree the user holds (e.g. claiming a PhD when they have a B.S.).
    _deg_label = l  # l is the lowercased label
    if re.search(r'have you completed.{0,60}(doctor|ph\.?d|phd|master|bachelor|associate)', _deg_label) \
            and kind in ("select", "select-one", "radio"):
        edu = p.get("education", {}) if isinstance(p.get("education"), dict) else {}
        user_rank = _degree_rank(edu.get("degree") or "")
        if any(t in _deg_label for t in ("doctor", "ph.d", "phd")):
            required_rank = 4
        elif "master" in _deg_label:
            required_rank = 3
        elif "bachelor" in _deg_label:
            required_rank = 2
        else:
            required_rank = 1  # associate
        return "Yes" if user_rank >= required_rank else "No"
    if any(k in l for k in ("highest level of education", "highest education", "education level", "level of education")):
        edu = p.get("education", {})
        # "Have you completed the following level of education: X?" is a Yes/No radio, not a degree picker
        if kind in ("radio",):
            return "Yes" if (isinstance(edu, dict) and edu.get("degree")) else "No"
        deg = (edu.get("degree") or "") if isinstance(edu, dict) else ""
        _deg_map = {"m.s.": "Master's Degree", "ms": "Master's Degree", "m.s": "Master's Degree",
                    "b.s.": "Bachelor's Degree", "bs": "Bachelor's Degree", "b.s": "Bachelor's Degree",
                    "ph.d": "Doctorate", "phd": "Doctorate", "mba": "Master's Degree"}
        return _deg_map.get(deg.lower().strip("."), deg) or "Master's Degree"
    if "degree" in l:
        edu = p.get("education", {})
        if kind == "radio":
            return "Yes" if (isinstance(edu, dict) and edu.get("degree")) else "No"
        return edu.get("degree") if isinstance(edu, dict) else None
    if any(k in l for k in ("school", "university", "college", "institution")):
        edu = p.get("education", {})
        return edu.get("school") if isinstance(edu, dict) else None
    if any(k in l for k in ("field of study", "major", "area of study")):
        edu = p.get("education", {})
        return edu.get("field") if isinstance(edu, dict) else None
    if any(k in l for k in ("where did you hear", "how did you hear", "how did you find out", "how did you learn about", "source of hire")):
        return "LinkedIn"
    # Travel willingness — "comfortable with" tightened to also require "travel" so it
    # doesn't swallow unrelated "comfortable with X" questions handled further down.
    if (any(k in l for k in ("willing to travel", "% travel", "travel requirement", "open to travel"))
            or ("comfortable with" in l and "travel" in l)) \
            and kind in ("select", "select-one", "radio"):
        return p.get("willing_to_travel", "No")
    # Notice period / start timeline — runs BEFORE the generic "start date -> Immediately"
    # rule below so dropdowns get a realistic option (e.g. "2 weeks") instead of "Immediately".
    if any(k in l for k in ("how quickly", "how soon", "when can you start",
                             "notice period", "earliest start", "available to start")) \
            and kind in ("select", "select-one", "radio"):
        return p.get("notice_period", "2 weeks")
    # Geographic commute / in-office proximity — user is in Utah; these appear on non-remote
    # roles, so answer "No". Runs before the generic "comfortable commuting -> Yes" rule.
    if any(k in l for k in ("commutable", "commute to", "in-office attendance",
                             "commuting distance", "work onsite", "in commutable")) \
            and kind in ("select", "select-one", "radio"):
        return "No"
    # Prior employment at this company
    if any(k in l for k in ("previously employed by", "worked for us before",
                             "former employee", "prior employment at",
                             "previously worked at", "previously worked for")) \
            and kind in ("select", "select-one", "radio"):
        return "No"
    if any(k in l for k in ("earliest available", "start date", "when can you start", "available to start", "date available", "earliest start")):
        return "Immediately"
    if any(k in l for k in ("relative", "friend", "family member")) and any(k in l for k in ("work for", "employed", "works at", "work at", "employee")):
        return "No"
    if any(k in l for k in ("secondary employment", "other employment", "work for another", "work elsewhere")):
        return "No"
    if any(k in l for k in ("applied here before", "applied to us before", "applied with us", "previously applied", "filed an application", "application with")):
        return "No"
    if any(k in l for k in ("worked here before", "worked for us", "worked for this company", "previously worked", "prior employment here", "former employee")):
        return "No"
    if any(k in l for k in ("under 18", "proof of eligibility", "proof of age", "work permit")):
        return "N/A"
    if any(k in l for k in ("agree to", "i agree", "acknowledge", "i understand", "consent to", "terms of service", "privacy policy", "terms and conditions")) and kind in ("select", "select-one", "checkbox"):
        return "Yes" if kind in ("select", "select-one") else "on"
    if any(k in l for k in ("comfortable working", "comfortable with remote", "comfortable in a remote", "ok for the remote", "remote engagement",
                             "comfortable commuting", "commuting to this job", "commute to this", "willing to commute",
                             "comfortable for", "shift hours", "est shift", "pst shift", "cst shift", "mst shift",
                             "work in our timezone", "work us hours", "work in us time")):
        return "Yes"
    if any(k in l for k in ("student visa", "f-1 visa", "f1 visa")):
        return "No"
    if any(k in l for k in ("background check",)):
        return "Yes"
    if any(k in l for k in ("drug test",)):
        return "Yes"
    if any(k in l for k in ("contract work", "work a contract", "work contract", "contract position", "contract role", "contract only", "corp-to-corp", "c2c", "1099")):
        return "No"
    if any(k in l for k in ("notice period", "notice days", "days notice")):
        return "14"
    if any(k in l for k in ("total years", "total experience", "years of relevant", "relevant experience", "total it experience")):
        return str(p.get("years_experience", "4"))
    # IC / hands-on role comfort questions
    if any(k in l for k in ("individual contributor", "hands-on-keyboard", "hands on keyboard", "ic role", "hands-on engineer")):
        return "Yes"
    # Evaluation frameworks / LLM-as-a-judge experience
    if any(k in l for k in ("evaluation framework", "llm-as-a-judge", "llm as a judge", "ai-as-a-judge", "ai as a judge")):
        return "Yes"
    # "How many [AI/RAG/agent] applications deployed to production" numeric question
    if "deployed to production" in l and kind in ("text", "number"):
        return "3"
    # Yes/No skill questions about ML/AI stack experience
    if any(k in l for k in ("building rag pipeline", "rag pipeline", "ml model", "creating custom embedding", "designing and implementing ml")) and kind in ("select", "select-one", "radio"):
        return "Yes"
    # Non-tech domain experience questions — answer No (P&C insurance, healthcare, etc.)
    if any(k in l for k in (
        "insurance domain", "p&c insurance", "property & casualty", "property and casualty",
        "healthcare domain", "financial domain", "legal domain", "manufacturing domain", "retail domain",
    )) and kind in ("select", "select-one", "radio"):
        return "No"
    # Broad experience/skill select questions — answer Yes.
    # Regex first: catches "do you have <product> development experience?" where an
    # interrupting product name (e.g. "twilio development") breaks the contiguous
    # "do you have experience" substring the keyword list below relies on.
    if re.search(r'do you have .{0,50}(experience|expertise)', l) \
            and kind in ("select", "select-one", "radio"):
        return "Yes"
    if any(k in l for k in (
        "hands-on experience", "have you built", "professional experience with",
        "experience building", "experience using", "experience developing",
        "experience implementing", "do you have experience",
        "have you worked with", "have you used", "have you worked in",
        "have you architected", "have you deployed", "have you designed",
        "have you shipped", "have you developed", "have you led",
        "early-stage startup", "high-ownership",
    )) and kind in ("select", "select-one", "radio"):
        return "Yes"
    if any(k in l for k in ("cover letter", "cover_letter", "covering letter")) and kind in ("text", "textarea"):
        return None  # always skip cover letter text fields
    if any(k in l for k in ("looking for", "job type preference", "what kind of job", "type of employment")):
        return p.get("current_title") or "Software Engineer"
    if "language" in l and kind in ("text", "select", "select-one"):
        return p.get("preferred_language", "English")
    if any(k in l for k in ("preferred name", "preferred first name", "nickname")):
        if p.get("preferred_name"):
            return p["preferred_name"]
        parts = (p.get("full_name") or "").split()
        return parts[0] if parts else ""
    if any(k in l for k in ("pronunciation", "phonetic", "how to pronounce")):
        return ""  # leave blank — user has no phonetic guide in profile
    if any(k in l for k in ("name", "full name")):
        return p.get("full_name")

    # Social/platform URLs not in profile — return empty so LLM doesn't fabricate a fake URL
    _UNKNOWN_SOCIAL = (
        "twitter", "x.com", "x handle", "x profile",
        "facebook", "instagram", "tiktok", "youtube",
        "medium.com", "substack", "blog url", "blog link",
        "stackoverflow", "stack overflow",
        "behance", "dribbble", "devpost", "kaggle",
        "other url", "other link", "other social", "other profile",
        "personal url", "social media url", "social profile",
    )
    if any(k in l for k in _UNKNOWN_SOCIAL):
        return ""
    # Any field typed as url= that we didn't already match — leave blank
    if kind == "url":
        return ""

    return None


# ── Shared helpers ─────────────────────────────────────────────────────────────

async def _ask_llm(llm_client: AsyncOpenAI, model: str, profile: dict, field: dict) -> str | None:
    label = field.get("label", "")
    # Fall back to field name/id as label hint when label is missing
    if not label:
        label = field.get("name", "") or field.get("id", "")
    if not label:
        return None
    options = field.get("options", [])
    kind = field.get("kind", "text")
    label_lower = label.lower()
    # "How many years..." and "years of experience" questions expect a number, not a sentence
    _is_numeric_question = (
        any(k in label_lower for k in ("how many years", "years of experience", "years experience", "how many months"))
        and kind in ("text", "number")
    )
    # Select/radio with options is never long-form regardless of label length
    _is_choice = kind in ("select", "select-one", "select-multiple", "radio") or bool(options)
    is_long_form = (kind in ("textarea", "contenteditable") or len(label) > 60) and not _is_numeric_question and not _is_choice
    prompt = f"Job application form field:\nLabel: {label}\nType: {kind}\n"
    if options:
        prompt += f"Options: {', '.join(str(o) for o in options)}\n"
    if is_long_form:
        prompt += (
            f"\nUser profile:\n{json.dumps(profile, indent=2)}\n\n"
            "Write a professional 2-4 sentence answer for this job application field. "
            "Draw on the profile's skills, experience, and background. "
            "If the question is about the company specifically, write a plausible, enthusiastic answer based on the applicant's goals. "
            "Never leave it blank — always produce a meaningful answer. "
            "CRITICAL: Never fabricate URLs, social media handles, usernames, or any specific data not stated in the profile. "
            "Reply with ONLY the answer text, nothing else."
        )
    else:
        prompt += (
            f"\nUser profile:\n{json.dumps(profile, indent=2)}\n\n"
            "Answer this single form field. "
            "If radio/select, reply with exactly one of the listed options — always pick one, never leave blank. "
            "For Yes/No experience or skill questions, pick the truthful answer based on the profile, or 'No' as a safe default if unknown. "
            "For ANY numeric/years/experience text field, always reply with a number — never leave blank. "
            "CRITICAL: Never fabricate URLs, social media handles, usernames, or specific data not in the profile. "
            "For URL/link fields (Twitter, Instagram, Facebook, personal blog, etc.) not explicitly in the profile, reply with an empty string. "
            "If the profile has no relevant info for a non-select/non-radio non-numeric field, reply with an empty string. "
            "Reply with ONLY the answer value, nothing else."
        )
    _t = 45 if is_long_form else 30
    try:
        resp = await asyncio.wait_for(
            llm_client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=400 if is_long_form else 100,
            ),
            timeout=_t,
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
        if not answer:
            print(f"  [LLM empty] Field '{label}' — LLM returned empty string, skipping.")
        return answer if answer else None
    except asyncio.TimeoutError:
        _write_llm_log({
            "ts":           datetime.now(timezone.utc).isoformat(),
            "type":         "field_fill_timeout",
            "model":        model,
            "field_label":  label,
            "field_kind":   field.get("kind"),
            "options":      field.get("options", []),
            "prompt":       prompt,
            "timeout_s":    _t,
        })
        print(f"  [LLM timeout] Field '{label}' — no answer in {_t}s, skipping.")
        return None
    except Exception:
        return None


async def _fill_field(page: Page, field: dict, value: str):
    """Fill a single form field. Non-fatal on error."""
    kind = field.get("kind", "text")
    el_id = field.get("id", "")
    name = field.get("name", "")

    try:
        if kind == "file":
            # Resume / file upload — value should be an absolute path.
            # Skip if the field label indicates cover letter (we only upload the resume).
            _lbl_lower = field.get("label", "").lower()
            _is_cover_letter = any(k in _lbl_lower for k in ("cover letter", "cover_letter", "covering letter"))
            if _is_cover_letter:
                return
            if value and os.path.isfile(value):
                sel = f'#{_css_id(el_id)}' if el_id else (f'[name="{name}"]' if name else 'input[type="file"]')
                el = page.locator(sel).first
                if await el.count() > 0:
                    await el.set_input_files(value)
            return

        if kind in ("text", "textarea", "email", "tel", "number", "url", "search", "password"):
            sel = f'#{_css_id(el_id)}' if el_id else (f'[name="{name}"]' if name else None)
            el = page.locator(sel).first if sel else None
            # If found by ID but not visible/interactable (e.g. sidebar clone), prefer label lookup
            if el is not None and await el.count() > 0:
                try:
                    _vis = await el.is_visible()
                except Exception:
                    _vis = False
                if not _vis:
                    el = None  # fall through to label-based fallback
            # Fallback: locate by label text when id/name absent or element not visible
            if el is None or await el.count() == 0:
                _lbl = field.get("label", "")
                if _lbl:
                    el = page.get_by_label(_lbl, exact=False).first
            if el is None or await el.count() == 0:
                _lbl = field.get("label", "")
                if _lbl:
                    el = page.locator(
                        f'input[placeholder*="{_lbl}" i], textarea[placeholder*="{_lbl}" i]'
                    ).first
            if el is not None and await el.count() > 0:
                try:
                    await _human_type(el, value)
                except Exception:
                    # Primary element not interactable — try get_by_label as last resort
                    _lbl = field.get("label", "")
                    if _lbl:
                        _el2 = page.get_by_label(_lbl, exact=False).first
                        if await _el2.count() > 0:
                            await _human_type(_el2, value)
                            el = _el2
                        else:
                            return
                    else:
                        return
                await asyncio.sleep(random.uniform(0.2, 0.6))
                # Skip typeahead for numeric/year fields — no autocomplete expected on these
                _skip_typeahead = (
                    kind in ("number",)
                    or str(value).startswith("http")
                    or any(k in field.get("label", "").lower()
                           for k in ("how many years", "how many months", "years of experience",
                                     "years experience", "number of years", "salary", "compensation",
                                     "linkedin", "github", "portfolio", "website", "rest of world"))
                )
                if _skip_typeahead:
                    return
                # Select from autocomplete/typeahead dropdown if one appears
                _typeahead_sel = (
                    '.artdeco-typeahead__hit, '
                    '[role="option"]:not([id^="iti"]), [role="listbox"] li:not([id^="iti"]), '
                    '.basic-typeahead__selectable, .search-typeahead-v2__hit'
                )
                try:
                    await asyncio.sleep(1.5)
                    suggestion = page.locator(_typeahead_sel).first
                    _n = await suggestion.count()
                    _vis = (await suggestion.is_visible()) if _n > 0 else False
                    if _n > 0 and _vis:
                        await suggestion.click(timeout=3000)
                        await asyncio.sleep(0.5)
                        print("  [EasyApply] Typeahead: clicked suggestion")
                    else:
                        # ArrowDown may trigger the dropdown — try it then check again
                        await el.press("ArrowDown")
                        await asyncio.sleep(0.6)
                        suggestion2 = page.locator(_typeahead_sel).first
                        _n2 = await suggestion2.count()
                        _vis2 = (await suggestion2.is_visible()) if _n2 > 0 else False
                        if _n2 > 0 and _vis2:
                            await el.press("Enter")
                            await asyncio.sleep(0.3)
                            print("  [EasyApply] Typeahead: ArrowDown+Enter fallback succeeded")
                        else:
                            # No typeahead dropdown — press Enter to accept the typed value as-is
                            await el.press("Enter")
                            await asyncio.sleep(0.2)
                            print("  [EasyApply] Typeahead: no suggestions — pressing Enter to accept typed value")
                except Exception:
                    pass

        elif kind in ("select", "select-one", "select-multiple"):
            sel = f'#{_css_id(el_id)}' if el_id else (f'[name="{name}"]' if name else None)
            el = page.locator(sel).first if sel else None
            # Fallback: locate by label when id and name are both absent (LinkedIn EasyApply selects)
            if el is None or await el.count() == 0:
                _lbl = field.get("label", "")
                if _lbl:
                    el = page.get_by_label(_lbl, exact=False).first
            if el is not None and await el.count() > 0:
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
                            # Fuzzy fallback: pick first option whose label starts with or contains value
                            try:
                                opts = await el.evaluate("el => Array.from(el.options).map(o => ({t: o.text, v: o.value}))")
                                vl = value.lower()
                                match = next((o for o in opts if o["t"].lower().startswith(vl)), None)
                                if not match:
                                    match = next((o for o in opts if vl in o["t"].lower()), None)
                                if match:
                                    await el.select_option(label=match["t"])
                                else:
                                    _lbl_hint = field.get("label", "")[:60]
                                    _opt_names = [o["t"] for o in opts[:6]]
                                    print(f"  [EasyApply] select-one '{_lbl_hint}': no option matched {value!r}, options={_opt_names}")
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
                # Resolve the matched radio to a single-element locator, matching the
                # selector style already used in this function.
                if matched < len(option_ids) and option_ids[matched]:
                    radio_loc = page.locator(f'#{option_ids[matched]}')
                elif name:
                    radio_loc = page.locator(
                        f'input[type="radio"][name="{name}"]'
                    ).nth(matched)
                else:
                    return False
                await radio_loc.click()
                # Playwright's native .click() fires a DOM event that LinkedIn's React
                # controlled components ignore (onChange never fires), so the selection
                # is dropped and the field reads empty on the next step. Dispatch
                # synthetic React-compatible events to force the onChange.
                handle = await radio_loc.element_handle()
                if handle is not None:
                    await page.evaluate(
                        """el => {
                            el.click();
                            el.dispatchEvent(new MouseEvent('click', {bubbles: true}));
                            ['change', 'input'].forEach(t =>
                                el.dispatchEvent(new Event(t, {bubbles: true}))
                            );
                        }""",
                        handle,
                    )
                # React processes setState() on the next microtask tick, so is_checked()
                # called immediately after dispatching synthetic events races the
                # controlled-component reconciliation: the DOM may still read the optimistic
                # checked=true before React resets it to false. Settle first, then read.
                await asyncio.sleep(0.35)  # wait for React reconciliation
                # Verify the selection registered; retry forcefully and re-dispatch if not.
                if not await radio_loc.is_checked():
                    await radio_loc.click(force=True)
                    if handle is not None:
                        await page.evaluate(
                            "el => el.dispatchEvent(new Event('change', {bubbles:true}))",
                            handle,
                        )
                    await asyncio.sleep(0.35)  # wait again before re-read
                # Report whether the click was confirmed so callers can decide on a retry.
                return await radio_loc.is_checked()

        elif kind == "contenteditable":
            # LinkedIn rich-text screening questions (Quill editor / role=textbox)
            aria_lbl = field.get("aria_labelledby", "")
            el = None
            if el_id:
                el = page.locator(f'#{_css_id(el_id)}').first
            if el is None or await el.count() == 0:
                if aria_lbl:
                    el = page.locator(
                        f'[aria-labelledby="{aria_lbl}"][contenteditable="true"], '
                        f'[aria-labelledby="{aria_lbl}"][role="textbox"]'
                    ).first
            if el is None or await el.count() == 0:
                _lbl = field.get("label", "")
                if _lbl:
                    el = page.get_by_role("textbox", name=_lbl, exact=False).first
            if el is not None and await el.count() > 0:
                try:
                    await el.click()
                    await asyncio.sleep(0.2)
                    # Clear existing content via keyboard shortcut
                    await page.keyboard.press("Control+a")
                    await asyncio.sleep(0.1)
                    await el.evaluate("el => { el.focus(); el.textContent = ''; }")
                    await asyncio.sleep(0.2)
                    await el.press_sequentially(str(value), delay=random.randint(30, 80))
                    await asyncio.sleep(random.uniform(0.2, 0.5))
                except Exception:
                    pass

        elif kind == "checkbox":
            # Check agreement/required checkboxes; skip opt-in/marketing ones
            label_lower = field.get("label", "").lower()
            _opt_in = ("email", "newsletter", "marketing", "notification", "subscribe", "follow", "updates")
            if not any(k in label_lower for k in _opt_in):
                sel = f'#{_css_id(el_id)}' if el_id else (f'[name="{name}"]' if name else 'input[type="checkbox"]')
                el = page.locator(sel).first
                if await el.count() > 0:
                    if not await el.is_checked():
                        await el.click()
                    # Report whether the box ended up checked so callers can retry on failure.
                    return await el.is_checked()
            # Opt-in/marketing checkbox intentionally left unchecked — treat as done.
            return True

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
        llm_client: AsyncOpenAI,
        model: str,
        auto_mode: bool,
        callbacks: dict,
        classifier_client: OpenAI | None = None,
        classifier_model: str = "",
        verbose: bool = False,
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
        self.verbose = verbose
        self._verbose_company = ""  # set by apply_jobs before run()

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

    async def _verbose_screenshot(self, label: str):
        """Save a debug screenshot when verbose mode is on."""
        if not self.verbose:
            return
        try:
            import os as _os
            _os.makedirs("debug_screenshots", exist_ok=True)
            _safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in (self._verbose_company or "easyapply"))
            _safe_label = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)
            _path = f"debug_screenshots/{_SESSION_TS}_{_safe}_{_safe_label}.png"
            await self.page.screenshot(path=_path, full_page=False)
            print(f"  [verbose] Screenshot → {_path}")
        except Exception as _e:
            print(f"  [verbose] Screenshot failed: {_e}")

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
        filled_labels: set[str] = set()  # track labels filled this session to avoid re-filling tag inputs

        for step_num in range(25):
            await asyncio.sleep(1.5)
            await self._fill_current_step(filled_labels)
            await asyncio.sleep(0.5)

            # Check all navigation buttons — prefer Submit > Review > Next
            submit = self.page.locator(
                '[aria-label*="Submit application"], button:has-text("Submit application"), '
                'button:has-text("Submit Application")'
            ).first
            if await submit.count() > 0:
                print(f"  [EasyApply] Step {step_num + 1}: Submit button found")
                return await self._handle_submit()

            review = self.page.locator(
                '[aria-label*="Review your application"], button:has-text("Review your application")'
            ).first
            if await review.count() > 0:
                before = await self._get_modal_text()
                print(f"  [EasyApply] Step {step_num + 1}: Review button — clicking")
                await review.click()
                await asyncio.sleep(1.5)
                after = await self._get_modal_text()
                if before and after and before == after:
                    click_failures += 1
                    print(f"  [EasyApply] Step {step_num + 1}: Review click didn't advance ({click_failures}/3)")
                    print(f"  [EasyApply] Modal at stuck Review: {before[:400]!r}")
                    await self._verbose_screenshot(f"stuck_review_step{step_num + 1}")
                    if click_failures >= 3:
                        await self._verbose_screenshot(f"autofail_step{step_num + 1}")
                        return "failed"
                    # Re-attempt any required fields the modal is still complaining about
                    # before the next Review click. Without this the retry re-clicks the same
                    # button against the same empty fields and is guaranteed to fail all 3
                    # times. _fill_current_step skips already-filled fields, so this is
                    # idempotent and safe to call on every retry.
                    await self._fill_current_step(filled_labels)
                    await asyncio.sleep(0.5)
                else:
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
                    # Print modal text and take screenshot on first stuck occurrence
                    if click_failures == 1:
                        print(f"  [EasyApply] Modal at stuck step: {before[:400]!r}")
                        await self._verbose_screenshot(f"stuck_next_step{step_num + 1}")
                        # Scroll the modal to reveal any off-screen fields (e.g. additional questions)
                        try:
                            await self.page.evaluate(
                                "var m=document.querySelector('.jobs-easy-apply-content,.artdeco-modal__content,.jobs-apply-form');"
                                "if(m)m.scrollTop+=300;"
                            )
                            await asyncio.sleep(0.5)
                        except Exception:
                            pass
                    if click_failures >= 2:
                        # Re-attempt any required fields the modal is still complaining
                        # about before trying to skip. Mirrors the Review-button stuck
                        # path; combined with the filled_labels confirmation fix, fields
                        # whose fill silently failed (e.g. radios) can now actually be
                        # retried instead of being permanently skipped.
                        await self._fill_current_step(filled_labels)
                        await asyncio.sleep(0.5)
                        # Try to skip LinkedIn preference/screening steps via dismiss buttons
                        _skipped = False
                        for _skip_sel in (
                            'button:has-text("Skip")',
                            'button:has-text("Not now")',
                            'button:has-text("Later")',
                        ):
                            try:
                                _sb = self.page.locator(_skip_sel).first
                                if await _sb.count() > 0 and await _sb.is_visible():
                                    print(f"  [EasyApply] Stuck step — trying skip via {_skip_sel!r}")
                                    await _sb.click()
                                    await asyncio.sleep(1.5)
                                    _skipped = True
                                    click_failures = 0
                                    break
                            except Exception:
                                continue
                        if _skipped:
                            continue
                    if click_failures >= 3:
                        await self._verbose_screenshot(f"autofail_step{step_num + 1}")
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
                await self._verbose_screenshot(f"modal_closed_step{step_num + 1}")
                print("  [EasyApply] Modal closed unexpectedly — treating as failed")
                return "failed"
            click_failures += 1
            print(f"  [EasyApply] Step {step_num + 1}: No navigation button found ({click_failures}/3)")
            await self._verbose_screenshot(f"no_button_step{step_num + 1}")
            if click_failures >= 3:
                return "failed"

        await self._verbose_screenshot("autofail_step_limit")
        return "failed"

    async def _fill_current_step(self, filled_labels: set | None = None):
        if filled_labels is None:
            filled_labels = set()
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

        # Use Playwright locators which pierce shadow DOM.
        # LinkedIn's EasyApply form fields live inside a shadow-root component, so
        # page.evaluate(querySelectorAll) only sees 2 non-modal inputs from the
        # LinkedIn search bar. Playwright locators handle shadow DOM natively.
        fields = await self._collect_fields_playwright()

        if fields:
            print(f"  [EasyApply] Step fields: {[(f.get('label','?'), f.get('kind','?'), f.get('current_value','')) for f in fields]}")

        for field in fields:
            label = field.get("label", "")
            current_val = field.get("current_value", "")
            kind = field.get("kind", "text")
            # Skip already-filled fields, but not if current_value looks like a placeholder
            is_placeholder = current_val and current_val.lower() == label.lower()
            # Select fields: treat default option text as empty (not yet chosen)
            _select_defaults = (
                "select an option", "please select", "-- select --", "- select -",
                "select one", "choose one", "choose an option", "select", "none",
            )
            is_select_placeholder = (
                kind in ("select", "select-one", "select-multiple")
                and current_val.lower() in _select_defaults
            )
            # For checkboxes: "false" string means unchecked — treat as unfilled
            is_unchecked_checkbox = kind == "checkbox" and current_val in ("false", "0", "")
            if current_val and kind != "radio" and not is_placeholder and not is_select_placeholder and not is_unchecked_checkbox:
                continue
            # Skip fields that were already filled in a previous step of this session.
            # Prevents infinite loops on tag-input fields (e.g. "I'm looking for…") whose
            # text input clears itself after Enter, making current_value stay '' forever.
            if label and label in filled_labels:
                # Radios/checkboxes are safe to retry — re-clicking a correctly-checked
                # control is a no-op. If is_checked() returned a false positive (React
                # reconciliation race) the label got added despite the fill failing, which
                # would permanently deadlock the stuck-Review re-fill loop. Allow a re-attempt
                # whenever the value still reads empty.
                if kind in ("radio", "checkbox") and not current_val:
                    pass  # fall through to re-fill
                else:
                    continue
            value = _get_profile_value(self.profile, label, kind)
            if value is None:
                value = await _ask_llm(self.classifier_client, self.classifier_model, self.profile, field)
            if value:
                print(f"  [EasyApply] Filling '{label}' = {str(value)[:40]!r}")
                confirmed = await _fill_field(self.page, field, value)
                # For radio/checkbox, _fill_field returns a bool indicating whether the
                # selection actually registered (LinkedIn's React form can silently drop
                # a click). Only mark the field done when it took, so the stuck-step retry
                # loop can re-attempt it. _fill_field returns None for other kinds, which
                # we treat as done since no confirmation signal is available.
                if label and not (kind in ("radio", "checkbox") and confirmed is False):
                    filled_labels.add(label)
            elif label and label not in self.unanswered_fields:
                self.unanswered_fields.append(label)

    async def _collect_fields_playwright(self) -> list[dict]:
        """
        Enumerate visible form fields inside the EasyApply modal using Playwright locators,
        which pierce LinkedIn's shadow DOM components.  Returns a list of field dicts
        compatible with _fill_field / _ask_llm.
        """
        # Find the modal container via Playwright (shadow-DOM aware)
        modal = self.page  # fallback: search entire page
        for sel in self._MODAL_SELECTORS:
            loc = self.page.locator(sel).first
            try:
                if await loc.count() > 0:
                    modal = loc
                    break
            except Exception:
                continue

        fields: list[dict] = []
        seen_ids: set[str] = set()
        seen_names: set[str] = set()  # for radio groups

        _GET_FIELD_META = """el => {
            var root = el.getRootNode() || document;
            var lbl = el.id ? root.querySelector('label[for="' + el.id + '"]') : null;
            if (!lbl) lbl = el.closest('label');
            var labelText = (lbl && lbl.textContent.trim())
                         || el.getAttribute('aria-label')
                         || el.getAttribute('placeholder')
                         || el.name || '';
            // Walk up to find a fieldset legend or nearby heading for better label
            if (!labelText || labelText.length < 2) {
                var node = el.parentElement;
                var depth = 0;
                while (node && depth < 6) {
                    var lh = node.querySelector('label,legend,h3,h4,[class*="label"],[class*="heading"]');
                    if (lh && lh !== el && !lh.contains(el)) {
                        labelText = lh.textContent.trim(); break;
                    }
                    node = node.parentElement; depth++;
                }
            }
            var opts = [];
            if (el.tagName === 'SELECT') {
                Array.from(el.options).forEach(function(o) {
                    if (o.value) opts.push(o.text.trim());
                });
            }
            return {
                kind: el.type || el.tagName.toLowerCase(),
                label: labelText.trim(),
                id: el.id || '',
                name: el.name || '',
                options: opts,
                current_value: el.value || '',
            };
        }"""

        def _norm_label(raw: str) -> str:
            """Collapse whitespace and strip trailing Required/asterisk badges from DOM labels."""
            s = re.sub(r'\s+', ' ', raw).strip()
            s = re.sub(r'\s*\*?\s*required\s*$', '', s, flags=re.IGNORECASE).strip()
            return s

        # Standard text/email/phone/select/textarea inputs
        field_loc = modal.locator(
            "input:not([type=hidden]):not([type=submit]):not([type=button])"
            ":not([type=reset]):not([type=radio]):not([type=file]),"
            "textarea,"
            "select"
        )
        n = await field_loc.count()
        for i in range(n):
            el = field_loc.nth(i)
            try:
                if not await el.is_visible():
                    continue
                props = await el.evaluate(_GET_FIELD_META)
                props["label"] = _norm_label(props.get("label", ""))
                uid = props.get("id") or props.get("name") or props.get("label")
                if uid and uid in seen_ids:
                    continue
                if uid:
                    seen_ids.add(uid)
                fields.append(props)
            except Exception:
                pass

        # Contenteditable / role=textbox (LinkedIn rich-text screening questions)
        _CE_META = """el => {
            var root = el.getRootNode() || document;
            var ariaLbl = el.getAttribute('aria-labelledby') || '';
            var lblEl = ariaLbl ? root.getElementById(ariaLbl) : null;
            var labelText = (lblEl && lblEl.textContent.trim())
                         || el.getAttribute('aria-label') || '';
            if (!labelText) {
                var node = el.parentElement; var depth = 0;
                while (node && depth < 6) {
                    var lh = node.querySelector('label,legend,h3,h4,[class*="label"]');
                    if (lh && lh !== el && !lh.contains(el)) { labelText = lh.textContent.trim(); break; }
                    var prev = node.previousElementSibling;
                    if (prev && prev.textContent.trim().length > 5 && prev.textContent.trim().length < 300) {
                        labelText = prev.textContent.trim(); break;
                    }
                    node = node.parentElement; depth++;
                }
            }
            return {
                kind: 'contenteditable',
                label: labelText.trim(),
                id: el.id || '',
                name: el.getAttribute('name') || '',
                aria_labelledby: ariaLbl,
                options: [],
                current_value: (el.innerText || el.textContent || '').trim(),
            };
        }"""
        ce_loc = modal.locator(
            '[contenteditable="true"]:not([aria-readonly="true"]),'
            '[role="textbox"]:not([aria-readonly="true"])'
        )
        nc = await ce_loc.count()
        for i in range(nc):
            el = ce_loc.nth(i)
            try:
                if not await el.is_visible():
                    continue
                props = await el.evaluate(_CE_META)
                props["label"] = _norm_label(props.get("label", ""))
                lbl = props.get("label", "")
                if lbl and any(f.get("label") == lbl for f in fields):
                    continue  # deduplicate
                uid = props.get("id") or props.get("aria_labelledby") or lbl
                if uid and uid in seen_ids:
                    continue
                if uid:
                    seen_ids.add(uid)
                fields.append(props)
            except Exception:
                pass

        # Radio button groups
        _RADIO_META = """el => {
            var root = el.getRootNode() || document;
            var grpName = el.name || '';
            if (!grpName) return null;
            var fieldset = el.closest('fieldset') || el.closest('[role="group"]') || el.closest('[role="radiogroup"]');
            var groupLabel = grpName;
            if (fieldset) {
                var labelId = fieldset.getAttribute('aria-labelledby');
                var labelEl = labelId ? root.getElementById(labelId) : null;
                var legend = fieldset.querySelector('legend');
                groupLabel = (labelEl && labelEl.textContent.trim())
                           || (legend && legend.textContent.trim()) || grpName;
            }
            var radios = root.querySelectorAll('input[type="radio"][name="' + grpName + '"]');
            var opts = [], optIds = [];
            radios.forEach(function(r) {
                var l = r.id ? root.querySelector('label[for="' + r.id + '"]') : null;
                opts.push(l ? l.textContent.trim() : r.value);
                optIds.push(r.id || '');
            });
            return { kind: 'radio', label: groupLabel.trim(), name: grpName,
                     options: opts, option_ids: optIds,
                     current_value: el.checked ? el.value : '' };
        }"""
        radio_loc = modal.locator('input[type=radio]')
        nr = await radio_loc.count()
        for i in range(nr):
            el = radio_loc.nth(i)
            try:
                if not await el.is_visible():
                    continue
                props = await el.evaluate(_RADIO_META)
                if not props:
                    continue
                props["label"] = _norm_label(props.get("label", ""))
                # Also normalize each radio option label
                props["options"] = [_norm_label(o) for o in props.get("options", [])]
                grp_name = props.get("name", "")
                if grp_name in seen_names:
                    continue
                seen_names.add(grp_name)
                fields.append(props)
            except Exception:
                pass

        return fields

    async def _handle_submit(self) -> str:
        summary = (
            f"Application for {self.profile.get('full_name', 'user')}. "
            f"Email: {self.profile.get('email', 'N/A')}, "
            f"Phone: {self.profile.get('phone', 'N/A')}, "
            f"Work auth: {self.profile.get('work_authorization', 'N/A')}."
        )
        ready = self.callbacks.get("ready_to_submit")
        result = (await ready(summary)) if ready else "applied"

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
        llm_client: AsyncOpenAI,
        model: str,
        auto_mode: bool,
        callbacks: dict,
        generated_password: str,
        company_name: str = "",
        job_title: str = "",
        job_description: str = "",
        classifier_client: OpenAI | None = None,
        classifier_model: str = "",
        verbose: bool = False,
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
        self.job_description = job_description
        self.classifier_client = classifier_client or llm_client
        self.classifier_model = classifier_model or model
        self.verbose = verbose
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

    async def _get_page_snapshot(self, page: Page) -> dict:
        """Capture URL, visible text, form fields (with current values), and buttons."""
        try:
            snapshot_data = await page.evaluate("""() => {
                const vh = window.innerHeight, vw = window.innerWidth;
                const inViewport = (el) => {
                    const r = el.getBoundingClientRect();
                    return r.bottom > 0 && r.top < vh && r.right > 0 && r.left < vw;
                };

                // Visible text — collapse whitespace, cap at 1200 chars
                const walker = document.createTreeWalker(
                    document.body, NodeFilter.SHOW_TEXT,
                    { acceptNode(node) {
                        const p = node.parentElement;
                        if (!p) return NodeFilter.FILTER_REJECT;
                        const s = window.getComputedStyle(p);
                        if (s.display === 'none' || s.visibility === 'hidden') return NodeFilter.FILTER_REJECT;
                        return inViewport(p) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
                    }}
                );
                const texts = [];
                let node;
                while ((node = walker.nextNode())) {
                    const t = node.textContent.trim();
                    if (t) texts.push(t);
                }
                const visibleText = texts.join(' ').replace(/\\s+/g, ' ').slice(0, 1200);

                // Label resolver — prefers <label for=id>, then aria-label, then placeholder, then name
                const lbl = (el) => {
                    const forEl = el.id ? document.querySelector('label[for="' + el.id + '"]') : null;
                    return (forEl && forEl.textContent.trim())
                        || el.getAttribute('aria-label')
                        || el.getAttribute('placeholder')
                        || el.getAttribute('name')
                        || '';
                };

                // Form fields (inputs, selects, textareas) — include value regardless of viewport
                const fields = Array.from(document.querySelectorAll(
                    'input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=reset]),' +
                    'select, textarea'
                ))
                .filter(el => !el.disabled)
                // Skip tabindex=-1 inputs (autocomplete children like Workable #city, phone country search)
                // Skip React Select internal combobox inputs — they always appear empty even when selected
                // Skip fully unlabeled/unnamed inputs with no id — hidden React control inputs
                .filter(el => {
                    if (el.getAttribute('tabindex') === '-1') return false;
                    // React Select combobox: input inside [class*="react-select"] or role="combobox" container
                    const parent = el.closest('[class*="react-select"]') || el.closest('[role="combobox"]');
                    if (parent && !el.id && !el.name) return false;
                    const hasId = !!el.id;
                    const hasName = !!el.name;
                    const hasLabel = !!(el.getAttribute('aria-label') || el.getAttribute('placeholder') ||
                        (el.id && document.querySelector('label[for="' + el.id + '"]')));
                    return hasId || hasName || hasLabel;
                })
                .map(el => {
                    const f = {
                        tag: el.tagName.toLowerCase(),
                        type: (el.getAttribute('type') || el.tagName.toLowerCase()).toLowerCase(),
                        id: el.id || '',
                        name: el.name || '',
                        label: lbl(el),
                        // Always capture the actual current value — empty string means truly unfilled
                        value: (el.value !== undefined && el.value !== null) ? el.value.slice(0, 80) : '',
                        inViewport: inViewport(el),
                    };
                    if (el.tagName === 'SELECT') {
                        f.options = Array.from(el.options)
                            .map(o => o.text.trim()).filter(t => t && t.toLowerCase() !== 'select an option' && t !== '--').slice(0, 20);
                    }
                    return f;
                });

                // Buttons visible in viewport, plus off-screen submit/apply/next buttons
                const _allBtns = Array.from(document.querySelectorAll(
                    'button:not([disabled]), [role="button"], a[href]'
                ));
                const _submitKeywords = ['submit', 'apply', 'next', 'continue', 'send', 'finish', 'complete'];
                const buttons = _allBtns
                .filter(el => {
                    if (inViewport(el)) return true;
                    // Include off-screen buttons only if they look like a submit/navigation CTA
                    const t = (el.textContent || el.getAttribute('aria-label') || '').trim().toLowerCase();
                    return _submitKeywords.some(k => t.includes(k));
                })
                .map(el => ({
                    tag: el.tagName.toLowerCase(),
                    text: (el.textContent || el.getAttribute('aria-label') || '').trim().slice(0, 60),
                    id: el.id || '',
                    inViewport: inViewport(el),
                }))
                .filter(b => b.text)
                .slice(0, 20);

                return { visibleText, fields, buttons };
            }""")
            visible_text = snapshot_data.get("visibleText", "")
            fields = snapshot_data.get("fields", [])
            buttons = snapshot_data.get("buttons", [])
        except Exception:
            visible_text = ""
            fields = []
            buttons = []
        return {
            "url": page.url,
            "visible_text": visible_text,
            "fields": fields,
            "buttons": buttons,
        }

    async def _ask_llm_action(self, snapshot: dict, step: int, history: list[str] | None = None, current_url: str = "", job_summary: str = "", context_notes: list[str] | None = None, override_filled: dict | None = None) -> dict:
        """Ask the LLM what single action to take next."""
        if history is None:
            history = []
        if context_notes is None:
            context_notes = []
        p = self.profile
        resume_path = p.get("resume_path", "")
        if resume_path:
            resume_path = os.path.abspath(resume_path) if not os.path.isabs(resume_path) else resume_path
        _need_sponsor = p.get("need_sponsorship", "")
        _sponsor_val = "Yes" if str(_need_sponsor).lower() in ("yes", "true", "1") else "No"
        profile_line = (
            f"name={p.get('full_name','')} preferred_name={p.get('preferred_name','')} "
            f"email={p.get('email','')} "
            f"phone={p.get('phone','')} location={p.get('location','')} "
            f"title={p.get('current_title','')} yrs={p.get('years_experience','')} "
            f"auth={p.get('work_authorization','')} needs_sponsorship={_sponsor_val} "
            f"linkedin={p.get('linkedin_url','')} github={p.get('github_url','')} "
            f"resume={resume_path}"
        )

        # --- Section 1: Summary of actions taken + running context notes ---
        history_lines = "\n".join(f"  {i+1}. {a}" for i, a in enumerate(history[-10:])) if history else "  (none yet — this is the first step)"
        notes_lines = ("\n  Notes from previous steps:\n" + "\n".join(f"  - {n}" for n in context_notes[-5:])) if context_notes else ""
        section1 = f"### 1. Summary of progress so far\n{history_lines}{notes_lines}"

        # --- Section 2: Visible text on screen ---
        visible_text = snapshot.get("visible_text", "").strip()
        section2 = f"### 2. Visible text on screen\n{visible_text or '(none captured)'}"

        # --- Section 3: Form fields on screen ---
        fields = snapshot.get("fields", [])
        # Sort: viewport-visible fields first, then offscreen — LLM should prioritize what's visible
        fields = sorted(fields, key=lambda f: (0 if f.get("inViewport") else 1))
        _select_defaults = {"select an option", "please select", "-- select --", "- select -",
                            "select one", "choose one", "choose an option", "select", "none", ""}
        field_lines = []
        for f in fields:
            label = f.get("label") or f.get("name") or f.get("id") or "(unlabeled)"
            ftype = f.get("type", "text")
            fid = f.get("id", "")
            fname = f.get("name", "")
            val = f.get("value", "")
            in_vp = f.get("inViewport", True)
            selector_hint = f'#{fid}' if fid else (f'[name="{fname}"]' if fname else "")
            # Python-level override: some fields (e.g. Greenhouse #country) have their values
            # committed to a hidden field by jQuery UI, leaving the visible input empty.
            # We track these in override_filled so the LLM sees them as FILLED.
            if override_filled:
                _ov_key = fid or fname
                if _ov_key and _ov_key in override_filled:
                    val = override_filled[_ov_key]
            # Determine filled vs empty
            is_empty = (not val) or (ftype in ("select", "select-one") and val.lower() in _select_defaults)
            if is_empty:
                status = "[EMPTY]"
            else:
                status = f'[FILLED: "{val[:60]}"]'
            viewport_marker = "" if in_vp else " (offscreen)"
            opts = f.get("options", [])
            opts_str = f" — options: {', '.join(opts[:10])}" if opts else ""
            field_lines.append(f"  {status}{viewport_marker}  {label}  ({ftype}{', ' + selector_hint if selector_hint else ''}){opts_str}")
        buttons = snapshot.get("buttons", [])
        btn_parts = []
        for b in buttons[:15]:
            t = b.get("text", "")
            if not t:
                continue
            marker = "" if b.get("inViewport", True) else " (offscreen)"
            btn_parts.append(f"{t}{marker}")
        section3 = "### 3. Form fields on screen\n"
        section3 += ("\n".join(field_lines) if field_lines else "  (no form fields detected)")
        if btn_parts:
            section3 += f"\n\nButtons/links visible: {' | '.join(btn_parts)}"

        url = snapshot.get("url", current_url)
        job_ctx = f"### Job context\n{job_summary}\n\n" if job_summary else ""
        prompt = (
            f"Job application automation — step {step + 1}.\n"
            f"URL: {url}\n"
            f"Profile: {profile_line}\n\n"
            f"{job_ctx}"
            f"{section1}\n\n"
            f"{section2}\n\n"
            f"{section3}\n\n"
            "Output EXACTLY ONE JSON object (no preamble, no explanation, never multiple objects):\n"
            '{"action":"click|fill|select|upload|scroll|done|failed",'
            '"selector":"<Playwright CSS selector>","text":"<btn/link text fallback>","value":"<fill/select value>",'
            '"reason":"<1 sentence>","update":"<1 sentence describing what this page/step revealed — e.g. form structure, new requirements, page count>"}\n\n'
            "Rules: done=thank-you/confirmation visible. failed=captcha/identity-verify/stuck/job-no-longer-available. "
            "If you see 'job not found', 'no longer available', 'position closed', or similar expired-job text → failed with reason 'job no longer available'. "
            "click=button or link (use :has-text() NOT :contains()). fill=empty text input. "
            "select=native <select> element ONLY (tag is SELECT in HTML). upload=resume file input. scroll=reveal more. "
            "Priority: Fill ALL [EMPTY] fields in top-to-bottom order BEFORE clicking any Submit/Apply button. "
            "Always act on the first [EMPTY] field in the list above — do not skip ahead to offscreen fields. "
            "NEVER fill a [FILLED] field — it already has the correct value, skip it. "
            "Never re-fill a field already in 'Actions taken so far'. Never Cancel/Sign-out. "
            "Never fill or upload to any field labeled 'Cover Letter' or 'Covering Letter' — skip entirely. "
            f"Sponsorship questions: answer '{_sponsor_val}'. Work authorization: always 'Yes'. "
            "CRITICAL: Never fabricate URLs, social media handles, usernames, or any information not in the profile. "
            "For URL/link fields asking for Twitter, Instagram, Facebook, personal blog, or any social/platform not listed in the profile, use value='' (empty string) — leave them blank. "
            "If all [EMPTY] fields are filled and a submit button is listed as (offscreen), use action=click with its selector to click it — do not scroll first. "
            "Never click bare 'Apply' nav links — only 'Apply Now', 'Apply for this job', 'Submit application'. "
            "Never click Login/Sign-in unless you just filled email+password. "
            "Never click utility buttons (Save, Bookmark, Share, Follow, Job alerts, Talent community, Sign in with LinkedIn)."
        )

        raw = ""
        _t0 = datetime.now(timezone.utc)
        for _attempt in range(3):
            try:
                _call_start = datetime.now(timezone.utc)
                resp = await asyncio.wait_for(
                    self.llm_client.chat.completions.create(
                        model=self.model,
                        messages=[{"role": "user", "content": prompt}],
                        max_tokens=1000,
                    ),
                    timeout=60,
                )
                _call_ms = int((datetime.now(timezone.utc) - _call_start).total_seconds() * 1000)
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
                    "duration_ms":  _call_ms,
                    "current_url":  current_url,
                    "company":      self.company_name,
                    "job_title":    self.job_title,
                    "snapshot":     snapshot,
                    "history":      (history or [])[-5:],
                    "prompt":       prompt,
                    "raw_response": raw,
                    "action":       parsed,
                })
                if self.verbose:
                    print(f"\n{'─' * 40} LLM INPUT (step {step+1}) {'─' * 40}")
                    print(prompt)
                    print(f"\n{'─' * 40} LLM OUTPUT (step {step+1}) {'─' * 40}")
                    print(raw)
                    print(f"{'─' * 90}\n")
                return parsed
            except asyncio.TimeoutError:
                _to_ms = int((datetime.now(timezone.utc) - _call_start).total_seconds() * 1000)
                _write_llm_log({
                    "ts":          datetime.now(timezone.utc).isoformat(),
                    "type":        "timeout_error",
                    "duration_ms": _to_ms,
                    "model":       self.model,
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
        return {"action": "failed", "reason": "LLM timed out after 3 retries"}

    async def _summarize_job(self) -> str:
        """One-shot LLM call to produce a 3-sentence summary of the job for context injection."""
        if not self.job_description:
            return f"Role: {self.job_title} at {self.company_name}."
        prompt = (
            f"Summarize this job posting in 3 concise sentences covering: "
            f"(1) the role and company, (2) key technical requirements, "
            f"(3) anything notable about the application process or candidate fit.\n\n"
            f"Title: {self.job_title}\nCompany: {self.company_name}\n\n"
            f"Description:\n{self.job_description[:3000]}"
        )
        try:
            resp = await asyncio.wait_for(
                self.classifier_client.chat.completions.create(
                    model=self.classifier_model,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=200,
                ),
                timeout=30,
            )
            return resp.choices[0].message.content.strip()
        except Exception:
            return f"Role: {self.job_title} at {self.company_name}."

    async def _llm_guided_apply(self, page: Page) -> str:
        """
        LLM-guided application loop.
        At each step: snapshot page → ask LLM → execute action → log → repeat.
        Returns 'applied' | 'failed'.
        """
        auth_attempted = False
        _login_paths = (
            "/login", "/signin", "/sign-in", "/auth/login",
            "/dashboard/login", "/register", "/create-account", "/join",
        )
        # Domains that are dead ends regardless of path (SSO login walls, enterprise portals)
        _dead_end_domains = ("my.greenhouse.io",)
        _unblockable = ("persona", "identity", "verif", "captcha", "recaptcha")
        action_history: list[str] = []
        context_notes: list[str] = []   # per-step observations from the LLM, accumulated as running context
        prev_url = ""
        unchanged_steps = 0
        last_action_type = None  # used to not penalize fill/select/upload for not changing URL
        consecutive_duplicates = 0  # consecutive duplicate-fill guard firings without URL change
        _selector_attempts: dict[str, int] = {}  # per-selector retry count; skip after 3 failures
        # Fields whose values are committed to hidden DOM state (e.g. jQuery UI autocomplete) but
        # whose visible input is cleared by site JS. Tracked here so the LLM sees them as FILLED.
        _forced_filled: dict[str, str] = {}

        # Skip enterprise ATS domains that require employer-provisioned accounts — no self-registration path.
        # Checked at domain level so Workday's /apply/applyManually (not a login URL) is caught too.
        _enterprise_ats_domains = ("icims.com", "taleo.net", "successfactors.com", "successfactors.eu",
                                   "sap.com",
                                   "myworkdayjobs.com",           # Workday public boards — requires account creation
                                   "myworkdaysite.com",            # Workday client instances — same account requirement
                                   "ultipro.com",                  # UltiPro/UKG — redirects to auth
                                   "paycomonline.net",             # Paycom — form buried behind account login
                                   "governmentjobs.com",           # NEOGOV — always requires pre-registered account
                                   "workforcenow.adp.com",         # ADP Workforce Now — lands on company portal, not job form
                                   "ycombinator.com",              # YC Work — apply requires YC Work account (SSO-only, no self-reg)
                                   "alignerr.com",                 # Alignerr — Google/LinkedIn OAuth only, no email registration
                                   "jobs.gainwelltechnologies.com", # Gainwell — portal loops back to job search, no direct apply form
                                   "jobright.ai",                  # Jobright — job aggregator, requires Jobright account signup to apply
                                   "sundayy.com",                  # Sundayy — job aggregator requiring account
                                   "scale.jobs",                   # Scale.jobs — job aggregator, requires account
                                   "tenex.ai",
                                   "bamboohr.com",                 # BambooHR — requires account creation to submit
                                   "recruitingbypaycor.com",       # Paycor — hidden location field causes 30s timeout, very slow
                                   "yourpayrollhr.com",            # Paycor-based — LLM navigates away from job page
                                   "zohorecruit.com",              # Zoho Recruit — CAPTCHA blocks autonomous submission
                                   "burtchworks.com",              # React form — fills don't persist
                                   "micro1.ai",
                                   "recruiting.paylocity.com",     # Paylocity recruiting portal — account required
                                   "sapsf.com",                    # SAP SuccessFactors hosted — same as successfactors.com
                                   "sourcehire.app",               # sourcehire — "Apply to this role" button does not navigate
                                   "remotehunter.com",             # RemoteHunter — job aggregator, "Apply" navigates to homepage login wall
                                   "talentally.com",               # TalentAlly — requires pre-registered account, can't self-register
                                   "haystack.cv",                  # Haystack — job aggregator behind account login wall
                                   "dice.com",                     # Dice — job aggregator, OAuth redirects to wrong page
                                   "app.breezy.hr",                # BreezyHR — repeated fill retries trigger spam detection
                                   "mercor.com")                   # Mercor — job aggregator behind account login
        _landing_domain = urlparse(page.url).netloc.lower()
        if any(ats in _landing_domain for ats in _enterprise_ats_domains):
            print(f"  [LLM] Enterprise ATS domain ({_landing_domain}) — skipping immediately")
            return "skipped"

        # Lever listing-page fast-path: jobs.lever.co/<company>/<id> is a listing page with no form.
        # Append /apply to navigate directly to the application form, skipping a wasted ScriptEngine call.
        if "jobs.lever.co" in _landing_domain and _is_job_listing_url(page.url):
            _lever_apply_url = page.url.rstrip("/") + "/apply"
            print(f"  [LLM] Lever listing page detected — navigating to {_lever_apply_url}")
            try:
                await page.goto(_lever_apply_url, wait_until="domcontentloaded", timeout=20000)
                await asyncio.sleep(2)
            except Exception as _lv_e:
                print(f"  [LLM] Lever /apply navigation failed ({_lv_e}) — continuing from listing page")

        # Greenhouse embed detection: ?gh_jid= means the Greenhouse form lives inside a cross-origin
        # <iframe> from boards.greenhouse.io — Playwright's page evaluation can't see into it.
        # Navigate to the iframe src directly so the LLM operates on the actual form page.
        # Note: if the URL is already on job-boards.greenhouse.io or boards.greenhouse.io
        # (e.g. the for=<company>&token= format), the form is directly accessible — no redirect needed.
        _gh_domains = ("job-boards.greenhouse.io", "boards.greenhouse.io")
        _already_on_gh = any(d in _landing_domain for d in _gh_domains)
        _qs = parse_qs(urlparse(page.url).query)
        if not _already_on_gh and "gh_jid" in _qs:
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

        # Script-generation engine: reads full DOM and generates a complete Playwright
        # script per page. Runs first; falls back to step loop on failure.
        try:
            from script_engine import ScriptApplyEngine as _ScriptEngine
            _ce = _ScriptEngine(
                profile=self.profile,
                context=self.context,
                company_name=self.company_name,
                job_title=self.job_title,
                llm_client=self.llm_client,
                model=self.model,
            )
            _cr = await _ce.apply(page)
            print(f"  [Script] Engine result: {_cr}")
            if _cr != "failed":
                return _cr
            print("  [Script] Engine returned failed — falling back to step loop")
        except Exception as _ce_err:
            print(f"  [Script] Engine error ({_ce_err}) — falling back to step loop")

        # Summarize job before entering the step loop — gives the LLM stable context about the role
        job_summary = await self._summarize_job()
        print(f"  [LLM] Job summary: {job_summary[:120]}{'...' if len(job_summary) > 120 else ''}")

        _session_start = datetime.now(timezone.utc)
        for step in range(30):
            _step_start = datetime.now(timezone.utc)
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=8000)
            except Exception:
                pass
            await asyncio.sleep(2)

            current_url = page.url
            _elapsed = int((datetime.now(timezone.utc) - _session_start).total_seconds())
            print(f"  [LLM] Step {step + 1} (+{_elapsed}s) — {current_url}")

            # Auth wall (URL-based) — match against path segments to avoid false positives
            # e.g. /joinroot/ should NOT match /join; /signup-flow should NOT match /signup
            _url_domain = urlparse(current_url.lower()).netloc
            if any(dead in _url_domain for dead in _dead_end_domains):
                print(f"  [LLM] Landed on dead-end domain ({_url_domain}) — skipping")
                return "skipped"

            # Mid-flow redirect to enterprise ATS (e.g. Dynatrace → SuccessFactors)
            if any(ats in _url_domain for ats in _enterprise_ats_domains):
                print(f"  [LLM] Redirected to enterprise ATS mid-flow ({_url_domain}) — skipping")
                return "skipped"

            # SSO / Identity Provider redirect — hand off to dedicated sign-in flow instead of LLM
            _sso_domains = (
                "login.microsoftonline.com",   # Microsoft OIDC / SAML
                "accounts.google.com",          # Google OAuth
                "login.okta.com",               # Okta hosted login
                "auth0.com",                    # Auth0
                "onelogin.com",                 # OneLogin
                "pingidentity.com",             # Ping Identity
                "shibboleth",                   # Shibboleth (many academic/enterprise)
            )
            _is_sso = any(s in _url_domain for s in _sso_domains) or ".okta.com" in _url_domain
            if _is_sso:
                if auth_attempted:
                    print(f"  [LLM] SSO auth already attempted on {_url_domain} — skipping")
                    return "skipped"
                auth_attempted = True
                print(f"  [LLM] SSO redirect detected ({_url_domain}) — invoking sign-in flow")
                ok = await self._handle_sso_page(page)
                if not ok:
                    print(f"  [LLM] SSO sign-in failed on {_url_domain} — skipping")
                    return "skipped"
                await asyncio.sleep(2)
                continue

            _url_path = urlparse(current_url.lower()).path
            if any(_url_path == p or _url_path.startswith(p + "/") or _url_path.startswith(p + "?")
                   for p in _login_paths):
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
                    'a:has-text("Apply for Position")',    'button:has-text("Apply for Position")',
                    'a:has-text("Apply for Role")',        'button:has-text("Apply for Role")',
                    'a:has-text("Apply to this role")',    'button:has-text("Apply to this role")',
                    'a:has-text("Apply Now")',             'button:has-text("Apply Now")',
                    'a:has-text("Apply now")',             'button:has-text("Apply now")',
                    '[data-automation-id*="applyButton"]',
                    'a[href*="/apply/"]:not([href*="linkedin"]):not([href*="talent"]):not([href*="search"]):not([href*="jobs"])',
                ]
                for _asel in _det_apply_sels:
                    try:
                        _abtn = page.locator(_asel).first
                        if await _abtn.count() == 0 or not await _abtn.is_visible():
                            continue
                        _btn_text = (await _abtn.inner_text()).strip()[:50]
                        # Skip elements whose text content is not apply-related
                        # (e.g. "VIEW ALL JOBS" href may contain "/apply/" but is not a CTA)
                        _btn_text_lower = _btn_text.lower()
                        if not any(w in _btn_text_lower for w in ("apply", "submit", "start")):
                            continue
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
                            # Re-check enterprise ATS — deterministic click may have redirected into one
                            _post_nav_domain = urlparse(page.url).netloc.lower()
                            if any(ats in _post_nav_domain for ats in _enterprise_ats_domains):
                                print(f"  [LLM] Post-navigation ATS domain ({_post_nav_domain}) — skipping")
                                return "skipped"
                        break  # found a match — proceed with snapshot of current page
                    except Exception:
                        continue

            # Get page snapshot — retry up to 2× if page is blank (SPA still rendering)
            for _snap_try in range(3):
                snapshot = await self._get_page_snapshot(page)
                _snap_empty = (
                    not snapshot.get("fields") and not snapshot.get("buttons")
                    and len(snapshot.get("visible_text", "")) < 20
                )
                if not _snap_empty or _snap_try == 2:
                    break
                print(f"  [LLM] Blank page detected — waiting for SPA render (attempt {_snap_try + 1}/3)")
                try:
                    await page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass
                await asyncio.sleep(3)
            if _snap_empty:
                print("  [LLM] Page still blank after retries — skipping")
                return "expired"

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

            # Greenhouse security code: pause and wait for human to solve it
            _current_domain = urlparse(page.url).netloc.lower()
            if "greenhouse.io" in _current_domain:
                try:
                    _gh_text = (await page.evaluate("() => (document.body.innerText || '').slice(0, 800)")).lower()
                    _security_signals = (
                        "security code", "enter the code", "enter code", "verification code",
                        "please enter the characters", "type the characters", "prove you're human",
                        "prove you are human", "i'm not a robot", "i am not a robot",
                        "are you human", "bot check", "human verification",
                    )
                    if any(s in _gh_text for s in _security_signals):
                        print(f"\n  [LLM] ⚠ Greenhouse security check detected at {page.url}")
                        print(f"  [LLM] Please solve the security challenge in the browser, then press Enter to continue...")
                        import sys
                        try:
                            input("  Press Enter when done: ")
                        except EOFError:
                            pass
                        print(f"  [LLM] Resuming after security challenge...")
                        await asyncio.sleep(1)
                except Exception:
                    pass

            # Detect stuck pages using URL only — avoids false resets from cosmetic mutations
            # (e.g. "Add to favorites" → "Favorited" changes text but not URL)
            if step > 0 and page.url == prev_url:
                # Fill/select/upload don't navigate by design — only count click-type actions
                if last_action_type not in ("fill", "select", "upload"):
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

            # Verbose mode — screenshot before LLM call
            if self.verbose:
                try:
                    import os as _os
                    _os.makedirs("debug_screenshots", exist_ok=True)
                    _safe_company = "".join(c if c.isalnum() or c in "-_" else "_" for c in (self.company_name or "unknown"))
                    _shot_path = f"debug_screenshots/{_SESSION_TS}_{_safe_company}_step{step+1:03d}.png"
                    await page.screenshot(path=_shot_path, full_page=False)
                    print(f"  [verbose] Screenshot → {_shot_path}")
                except Exception as _se:
                    print(f"  [verbose] Screenshot failed: {_se}")

            # Ask LLM (pass history + job context + running page notes for memory)
            action = await self._ask_llm_action(
                snapshot, step, action_history,
                current_url=page.url,
                job_summary=job_summary,
                context_notes=context_notes,
                override_filled=_forced_filled if _forced_filled else None,
            )
            action_type = action.get("action", "failed")
            reason = action.get("reason", "")
            selector = action.get("selector", "")
            text = action.get("text", "")
            value = action.get("value", "")
            # Capture per-step observation and append to running context
            update = action.get("update", "").strip()
            if update:
                context_notes.append(update)

            _step_ms = int((datetime.now(timezone.utc) - _step_start).total_seconds() * 1000)
            print(f"  [LLM] Thought: {reason}")
            _display_value = (self.profile.get("resume_path", value) if action_type == "upload" else value)
            print(f"  [LLM] Action: {action_type}" + (f" → {selector or text!r}" if selector or text else "") + (f" = {_display_value[:80]!r}" if _display_value else "") + f"  [{_step_ms}ms]")

            # Per-selector retry cap: if the same selector has been attempted 3+ times
            # without a URL change, mark it as permanently stuck and skip it.
            # This prevents infinite loops on unresolvable fields (React Select, tabindex=-1, etc.)
            if action_type in ("fill", "select") and (selector or text):
                _sel_key = selector or text
                _selector_attempts[_sel_key] = _selector_attempts.get(_sel_key, 0) + 1
                if _selector_attempts[_sel_key] > 3:
                    print(f"  [LLM] Selector {_sel_key!r} attempted 3+ times with no change — skipping this field")
                    # Add to forced_filled so LLM sees it as FILLED and moves on
                    _fid = _sel_key.lstrip("#")
                    if _fid:
                        _forced_filled[_fid] = "(skipped)"
                    continue

            # Bail if the LLM proposes an action already attempted — page is not advancing
            if action_type not in ("done", "failed", "scroll"):
                hist_key = f"{action_type}:{selector or text}"
                if any(h == hist_key or h.startswith(hist_key + "=") for h in action_history):
                    if action_type in ("fill", "select", "upload"):
                        # Duplicate fill on a SPA form: try to advance the section before giving up
                        _url_before_dup = page.url
                        _advanced = False
                        for _next_sel in (
                            'button:has-text("Continue")', 'button:has-text("Next")',
                            'button:has-text("Next Step")', 'button:has-text("Save and Continue")',
                            'a:has-text("Continue")',
                            'button:has-text("Submit Application")',
                            'button:has-text("Submit")', 'button[type="submit"]',
                        ):
                            try:
                                _nbtn = page.locator(_next_sel).first
                                if await _nbtn.count() > 0 and await _nbtn.is_visible():
                                    print(f"  [LLM] Duplicate fill — advancing section via {_next_sel!r}")
                                    # Use JS click to bypass Playwright actionability checks
                                    # (form validation can briefly detach/modify the button)
                                    await _nbtn.evaluate("el => el.click()")
                                    await asyncio.sleep(2)
                                    _advanced = True
                                    break
                            except Exception:
                                continue
                        if page.url == _url_before_dup:
                            consecutive_duplicates += 1
                        else:
                            consecutive_duplicates = 0
                        if consecutive_duplicates >= 3:
                            print(f"  [LLM] Duplicate fill guard fired 3 times without page advancing — giving up")
                            return "failed"
                        if not _advanced:
                            # Try scrolling down to reveal more of the form
                            try:
                                await page.evaluate("window.scrollBy(0, 400)")
                                await asyncio.sleep(1)
                            except Exception:
                                pass
                            print(f"  [LLM] Action '{hist_key}' already in history — page not advancing, giving up")
                            return "failed"
                    else:
                        print(f"  [LLM] Action '{hist_key}' already in history — page not advancing, giving up")
                        return "failed"

            if action_type == "done":
                confirmed, conf_reason = await self._check_submission_result(page, prev_url)
                if confirmed:
                    print("  [LLM] Application confirmed complete!")
                    return "applied"
                print(f"  [LLM] LLM returned done but no confirmation detected ({conf_reason}) — continuing")
                # If validation errors remain and resume hasn't been uploaded yet,
                # upload it now — the LLM can't tell file inputs are empty from the snapshot.
                if "required" in conf_reason.lower() or "validation" in conf_reason.lower():
                    try:
                        _resume_p = self.profile.get("resume_path", "")
                        if _resume_p:
                            _resume_abs = os.path.abspath(_resume_p) if not os.path.isabs(_resume_p) else _resume_p
                            _file_inputs = page.locator('input[type="file"]')
                            for _fi in range(await _file_inputs.count()):
                                _fi_el = _file_inputs.nth(_fi)
                                _fi_lbl = (await _fi_el.evaluate(
                                    "el => { var l = el.id ? document.querySelector('label[for=\"'+el.id+'\"]') : null;"
                                    " return (l && l.textContent) || el.getAttribute('aria-label') || el.name || ''; }"
                                )).lower()
                                _is_cv = any(k in _fi_lbl for k in ("cover letter", "cover_letter", "covering letter"))
                                if not _is_cv and os.path.isfile(_resume_abs):
                                    _cur_val = await _fi_el.evaluate("el => el.value || ''")
                                    if not _cur_val:
                                        await _fi_el.set_input_files(_resume_abs)
                                        print(f"  [LLM] Auto-uploaded resume to empty file input (label: {_fi_lbl!r})")
                                        await asyncio.sleep(2)
                    except Exception:
                        pass
                continue

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
                # Never upload the resume to a cover letter field
                _up_label = (selector + " " + text + " " + value).lower()
                _is_cover_upload = any(k in _up_label for k in ("cover letter", "cover_letter", "covering letter"))
                if _is_cover_upload:
                    print(f"  [LLM] Skipping upload — target is a cover letter field, not resume")
                    # Try to upload resume to the actual resume file input instead
                    resume_path = self.profile.get("resume_path", "")
                    if resume_path:
                        abs_path = os.path.abspath(resume_path) if not os.path.isabs(resume_path) else resume_path
                        if os.path.isfile(abs_path):
                            _resume_sel = (
                                'input[type="file"][id*="resume"]:not([id*="cover"]), '
                                'input[type="file"][name*="resume"]:not([name*="cover"]), '
                                'input[type="file"][aria-label*="Resume" i]:not([aria-label*="cover" i])'
                            )
                            try:
                                _rf = page.locator(_resume_sel).first
                                if await _rf.count() > 0:
                                    _cur_val = await _rf.evaluate("el => el.value || ''")
                                    if not _cur_val:
                                        await _rf.set_input_files(abs_path)
                                        print(f"  [LLM] Auto-uploaded resume to actual resume field")
                            except Exception:
                                pass
                else:
                    resume_path = self.profile.get("resume_path", "")
                    if resume_path:
                        abs_path = os.path.abspath(resume_path) if not os.path.isabs(resume_path) else resume_path
                        if os.path.isfile(abs_path) and selector:
                            try:
                                el = page.locator(selector).first
                                if await el.count() > 0:
                                    await el.set_input_files(abs_path)
                                    # Short wait only — don't block on server-side resume parsing (e.g. Ashby)
                                    try:
                                        await page.wait_for_load_state("domcontentloaded", timeout=5000)
                                    except Exception:
                                        pass
                                    await asyncio.sleep(1)
                            except Exception as exc:
                                print(f"  [LLM] Upload failed: {exc}")

            elif action_type == "fill" and selector and value:
                def _safe_selector(sel: str) -> str:
                    """Convert #<id-starting-with-digit> to [id="..."] (valid CSS)."""
                    if sel.startswith("#") and len(sel) > 1 and sel[1].isdigit():
                        return f'[id="{sel[1:]}"]'
                    if sel.startswith("input#") and sel[6:7].isdigit():
                        return f'input[id="{sel[6:]}"]'
                    return sel
                try:
                    safe_sel = _safe_selector(selector)
                    el = page.locator(safe_sel).first
                    if await el.count() == 0 and text:
                        el = page.locator(f'input[placeholder*="{text}" i], input[name*="{text}" i]').first
                    # Fallback: try get_by_label using text extracted from :has-label("...") pattern
                    if await el.count() == 0:
                        _lbl_match = re.search(r':has-label\(["\']([^"\']+)["\']', selector)
                        if _lbl_match:
                            _lbl_text = _lbl_match.group(1).rstrip("*").strip()
                            el = page.get_by_label(_lbl_text, exact=False).first
                    # LLM often uses input#<id> even for <select> elements it didn't see in viewport.
                    # Fall back to select#<id> so country/state dropdowns are actually filled.
                    if await el.count() == 0 and safe_sel.startswith("input#"):
                        el = page.locator("select" + safe_sel[5:]).first
                    if await el.count() > 0:
                        # Auto-detect element type and route accordingly
                        try:
                            _el_tag = await el.evaluate("el => el.tagName.toLowerCase()")
                            _el_type = await el.evaluate("el => (el.getAttribute('type') || '').toLowerCase()")
                        except Exception:
                            _el_tag = ""
                            _el_type = ""
                        if _el_tag == "select":
                            try:
                                await el.select_option(label=value)
                            except Exception:
                                try:
                                    await el.select_option(value=value)
                                except Exception:
                                    pass
                            await asyncio.sleep(0.5)
                        elif _el_type == "file":
                            # LLM sent fill action for a file input — redirect to upload.
                            # Skip if the field is a cover letter upload (we only upload resume).
                            _file_label = (
                                await el.evaluate(
                                    "el => { var l = el.id ? document.querySelector('label[for=\"'+el.id+'\"]') : null;"
                                    " return (l && l.textContent) || el.getAttribute('aria-label') || el.name || ''; }"
                                )
                            ).lower()
                            _is_cover = any(k in _file_label for k in ("cover letter", "cover_letter", "covering letter"))
                            if _is_cover:
                                print(f"  [LLM] Skipping file upload — field is cover letter, not resume")
                            else:
                                _resume = self.profile.get("resume_path", "")
                                if _resume:
                                    _resume = os.path.abspath(_resume) if not os.path.isabs(_resume) else _resume
                                    await el.set_input_files(_resume)
                                    print(f"  [LLM] Auto-redirected fill→upload for file input")
                        elif _el_type == "checkbox":
                            # LLM sent fill action for a checkbox — check it
                            if not await el.is_checked():
                                await el.check()
                            print(f"  [LLM] Auto-redirected fill→check for checkbox")
                        elif _el_type == "radio":
                            await el.check()
                            print(f"  [LLM] Auto-redirected fill→check for radio")
                        else:
                            # Skip cover letter text fields — we never fill these.
                            _fill_label = (
                                await el.evaluate(
                                    "el => { var l = el.id ? document.querySelector('label[for=\"'+el.id+'\"]') : null;"
                                    " return (l && l.textContent) || el.getAttribute('aria-label') || el.name || el.placeholder || ''; }"
                                )
                            ).lower()
                            if any(k in _fill_label for k in ("cover letter", "cover_letter", "covering letter")):
                                print(f"  [LLM] Skipping cover letter text field")
                            elif "resumetext" in (await el.get_attribute("id") or "").lower() or "resumetext" in (await el.get_attribute("name") or "").lower():
                                # applytojob.com resume-paste field — file already uploaded, skip
                                print(f"  [LLM] Skipping resume paste-text field (file already uploaded)")
                            elif "__search-input" in selector and "iti" in selector:
                                # International Telephone Input country-code dropdown — not a fill target
                                print(f"  [LLM] Skipping ITI phone country selector {selector!r}")
                            else:
                                # Special handler for Greenhouse jQuery UI country autocomplete.
                                # Greenhouse's #country uses an AJAX-backed jQueryUI autocomplete
                                # that takes 1-3s to return results — we need to clear, retype a
                                # short prefix, wait for the list, then click the matching item.
                                _el_id = await el.get_attribute("id") or ""
                                _is_gh_country = (
                                    "greenhouse.io" in page.url
                                    and _el_id == "country"
                                )
                                _is_react_combobox = False  # set in else branch below
                                if _is_gh_country:
                                    print(f"  [LLM] Greenhouse #country autocomplete: filling with {value!r}")
                                    await el.click(click_count=3)
                                    await el.press("Delete")
                                    await asyncio.sleep(0.3)
                                    # Type just enough to disambiguate ("United S" → "United States")
                                    _prefix = value[:8] if len(value) >= 8 else value
                                    await el.type(_prefix, delay=80)
                                    # Look for autocomplete suggestions: jQuery UI (.ui-menu-item)
                                    # or any visible listbox option not tied to intl-tel-input phone prefix
                                    _gh_sug = page.locator(
                                        '.ui-autocomplete li.ui-menu-item, '
                                        '.ui-autocomplete .ui-menu-item, '
                                        '.ui-menu li.ui-menu-item, '
                                        '[role="listbox"] [role="option"]:not([id^="iti"])'
                                    )
                                    try:
                                        await _gh_sug.first.wait_for(state="visible", timeout=7000)
                                    except Exception:
                                        pass
                                    await asyncio.sleep(0.3)
                                    _gh_count = await _gh_sug.count()
                                    print(f"  [LLM] Greenhouse #country suggestions: {_gh_count} item(s)")
                                    _gh_clicked = False
                                    # Two passes: prefer items without "+" (country-only), fall back to any match.
                                    # Greenhouse sometimes shows "United States (+1)" — that IS the correct
                                    # country entry; the "(+1)" is just display formatting in their autocomplete.
                                    _best = None
                                    _best_with_plus = None
                                    for _gi in range(_gh_count):
                                        _item = _gh_sug.nth(_gi)
                                        try:
                                            _item_text = (await _item.inner_text()).strip()
                                        except Exception:
                                            continue
                                        _tl = _item_text.lower()
                                        _vl = value.lower()
                                        _has_plus = "+" in _item_text
                                        # Strip trailing phone code "(+N)" for comparison
                                        _clean = re.sub(r'\s*\(\+\d+\)\s*$', '', _item_text).strip()
                                        _cl = _clean.lower()
                                        _matches = (_tl == _vl or _cl == _vl
                                                    or _cl.startswith(_vl) or _vl in _cl)
                                        if _matches:
                                            if not _has_plus and _best is None:
                                                _best = (_item, _item_text, _clean)
                                            elif _has_plus and _best_with_plus is None:
                                                _best_with_plus = (_item, _item_text, _clean)
                                    _chosen = _best or _best_with_plus
                                    if _chosen is None and _gh_count > 0:
                                        # Take the first item as last resort
                                        try:
                                            _fi0 = _gh_sug.nth(0)
                                            _ft0 = (await _fi0.inner_text()).strip()
                                            _clean0 = re.sub(r'\s*\(\+\d+\)\s*$', '', _ft0).strip()
                                            _chosen = (_fi0, _ft0, _clean0)
                                        except Exception:
                                            pass
                                    if _chosen:
                                        try:
                                            await _chosen[0].click(timeout=3000)
                                            _gh_clicked = True
                                            print(f"  [LLM] Greenhouse #country: clicked {_chosen[1]!r}")
                                        except Exception as _gh_ce:
                                            print(f"  [LLM] Greenhouse #country: click failed ({_gh_ce}) — trying keyboard")
                                    if not _gh_clicked:
                                        # Last resort: ArrowDown + Enter
                                        try:
                                            await el.press("ArrowDown")
                                            await asyncio.sleep(0.5)
                                            await el.press("Enter")
                                            print(f"  [LLM] Greenhouse #country: keyboard select fallback")
                                            _gh_clicked = True
                                        except Exception as _kbe:
                                            print(f"  [LLM] Greenhouse #country: keyboard fallback failed ({_kbe})")
                                    await asyncio.sleep(0.5)
                                    # jQuery UI autocomplete clears the visible text field after selection,
                                    # storing the value only in a hidden field. Force-set the visible input
                                    # so our snapshot reads it as [FILLED] and the LLM doesn't retry.
                                    _display_val = (_chosen[2] if _chosen else value)
                                    # Track in _forced_filled so the LLM sees #country as FILLED
                                    # even though Greenhouse's JS clears the visible input.
                                    _forced_filled["country"] = _display_val or value
                                    print(f"  [LLM] Greenhouse #country: marked as filled ({_display_val!r}) in prompt override")
                                    try:
                                        _cur_country = await el.input_value()
                                        if not _cur_country or _cur_country.lower() not in (_display_val.lower(), value.lower()):
                                            _js = "(el) => { el.value = " + json.dumps(_display_val) + "; el.dispatchEvent(new Event('input', {bubbles: true})); }"
                                            await el.evaluate(_js)
                                    except Exception:
                                        pass
                                else:
                                    # Detect React Select / combobox inputs — need click first to open dropdown.
                                    _fill_role = await el.get_attribute("role") or ""
                                    _fill_popup = await el.get_attribute("aria-haspopup") or ""
                                    _fill_class = await el.get_attribute("class") or ""
                                    _is_react_combobox = (
                                        _fill_role == "combobox"
                                        or bool(_fill_popup)
                                        or "select__input" in _fill_class
                                    )
                                    if _is_react_combobox:
                                        print(f"  [LLM] React Select fill fallback for {selector!r}")
                                        _rc_opts = page.locator(
                                            '[role="option"]:not([id^="iti"]), '
                                            '.select__option, '
                                            '[class*="option"]:not([id^="iti"])'
                                        )
                                        _rc_decline_kws = ("decline", "prefer not", "rather not", "not to self", "no answer", "not wish", "i prefer not", "not disclose")
                                        _rc_placeholder_vals = {"select...", "select", "choose...", "choose", "please select", "--", "---", "n/a", ""}
                                        _vl_rc = value.lower()
                                        _is_decline_rc = any(k in _vl_rc for k in _rc_decline_kws)
                                        _is_placeholder_rc = _vl_rc in _rc_placeholder_vals
                                        _rc_clicked = False

                                        # Step 1: Open dropdown WITHOUT typing — scan all unfiltered options.
                                        # Clicking the input alone often doesn't open React Select (need the control container).
                                        # Use JS to click the ancestor __control div, then click the input for fallback.
                                        try:
                                            await el.scroll_into_view_if_needed()
                                        except Exception:
                                            pass
                                        try:
                                            await el.evaluate(
                                                "el => { const c = el.closest('[class*=\"__control\"]') "
                                                "|| el.closest('[class*=\"select__control\"]') "
                                                "|| el.parentElement; if(c) c.click(); }"
                                            )
                                        except Exception:
                                            pass
                                        await el.click()
                                        await asyncio.sleep(0.8)
                                        try:
                                            await _rc_opts.first.wait_for(state="visible", timeout=5000)
                                        except Exception:
                                            pass
                                        _rc_count_all = await _rc_opts.count()
                                        print(f"  [LLM] React Select fill: opened, {_rc_count_all} option(s) visible")

                                        # Step 2: Exact/contains match among all unfiltered options
                                        for _ri in range(_rc_count_all):
                                            _ropt = _rc_opts.nth(_ri)
                                            try:
                                                _rt = (await _ropt.inner_text()).strip()
                                            except Exception:
                                                continue
                                            _rtl = _rt.lower()
                                            if "no option" in _rtl or "no result" in _rtl:
                                                continue
                                            if _rtl == _vl_rc or _vl_rc in _rtl:
                                                await _ropt.click(timeout=3000)
                                                _rc_clicked = True
                                                print(f"  [LLM] React Select fill: clicked option {_rt!r}")
                                                break

                                        # Step 3: Semantic decline match (handles EEO "prefer not to say" style fields)
                                        if not _rc_clicked and _is_decline_rc:
                                            for _ri in range(_rc_count_all):
                                                _ropt = _rc_opts.nth(_ri)
                                                try:
                                                    _rt = (await _ropt.inner_text()).strip()
                                                except Exception:
                                                    continue
                                                _rtl = _rt.lower()
                                                if "no option" in _rtl or "no result" in _rtl:
                                                    continue
                                                if any(k in _rtl for k in _rc_decline_kws):
                                                    await _ropt.click(timeout=3000)
                                                    _rc_clicked = True
                                                    print(f"  [LLM] React Select fill: clicked decline option {_rt!r}")
                                                    break

                                        # Step 3.5: LLM-pick — for decline requests where no keyword match found,
                                        # AND for placeholder values ('Select...') where the LLM had no specific answer.
                                        if not _rc_clicked and (_is_decline_rc or _is_placeholder_rc) and _rc_count_all > 0:
                                            _llm_opts_texts: list[str] = []
                                            for _ri in range(_rc_count_all):
                                                try:
                                                    _rt = (await _rc_opts.nth(_ri).inner_text()).strip()
                                                    if _rt and "no option" not in _rt.lower() and "no result" not in _rt.lower():
                                                        _llm_opts_texts.append(_rt)
                                                except Exception:
                                                    continue
                                            if _llm_opts_texts:
                                                print(f"  [LLM] React Select fill: asking LLM to pick decline option from {_llm_opts_texts}")
                                                try:
                                                    _llm_pick_resp = await self.llm_client.chat.completions.create(
                                                        model=self.model,
                                                        max_tokens=30,
                                                        messages=[{
                                                            "role": "user",
                                                            "content": (
                                                                f"Dropdown options: {_llm_opts_texts}\n"
                                                                f"Which option best means 'prefer not to disclose' or declining to answer? "
                                                                f"Reply with ONLY the exact option text from the list."
                                                            )
                                                        }]
                                                    )
                                                    _llm_picked = (_llm_pick_resp.choices[0].message.content or "").strip().strip('"').strip("'")
                                                    print(f"  [LLM] React Select fill: LLM picked {_llm_picked!r}")
                                                    for _ri in range(_rc_count_all):
                                                        try:
                                                            _ropt = _rc_opts.nth(_ri)
                                                            _rt = (await _ropt.inner_text()).strip()
                                                        except Exception:
                                                            continue
                                                        if _rt.lower() == _llm_picked.lower() or _llm_picked.lower() in _rt.lower() or _rt.lower() in _llm_picked.lower():
                                                            await _ropt.click(timeout=3000)
                                                            _rc_clicked = True
                                                            print(f"  [LLM] React Select fill: clicked LLM-picked option {_rt!r}")
                                                            break
                                                except Exception as _llm_pick_exc:
                                                    print(f"  [LLM] React Select fill: LLM pick failed: {_llm_pick_exc}")

                                        # Step 4: Type to filter — for long lists (location autocomplete) where
                                        # the full list is empty or too large to match by scan alone.
                                        # Skip for decline/placeholder fields: typing them always produces "No options".
                                        if not _rc_clicked and not _is_decline_rc and not _is_placeholder_rc:
                                            await el.fill(value)
                                            await asyncio.sleep(0.5)
                                            try:
                                                await _rc_opts.first.wait_for(state="visible", timeout=3000)
                                            except Exception:
                                                pass
                                            _rc_count_f = await _rc_opts.count()
                                            for _ri in range(_rc_count_f):
                                                _ropt = _rc_opts.nth(_ri)
                                                try:
                                                    _rt = (await _ropt.inner_text()).strip()
                                                except Exception:
                                                    continue
                                                _rtl = _rt.lower()
                                                if "no option" in _rtl or "no result" in _rtl:
                                                    continue
                                                if _rtl == _vl_rc or _vl_rc in _rtl:
                                                    await _ropt.click(timeout=3000)
                                                    _rc_clicked = True
                                                    print(f"  [LLM] React Select fill: clicked filtered option {_rt!r}")
                                                    break

                                        # Step 5: Keyboard fallback
                                        if not _rc_clicked:
                                            _rc_count_final = await _rc_opts.count()
                                            if _rc_count_final == 0:
                                                # Dropdown closed (filter typed in step 4 produced no matches).
                                                # Reopen by clicking the container, then recount.
                                                try:
                                                    await el.evaluate(
                                                        "el => { const c = el.closest('[class*=\"__control\"]') "
                                                        "|| el.closest('[class*=\"select__control\"]') "
                                                        "|| el.parentElement; if(c) c.click(); }"
                                                    )
                                                    await asyncio.sleep(0.5)
                                                except Exception:
                                                    pass
                                                _rc_count_final = await _rc_opts.count()
                                            if _rc_count_final > 0:
                                                # If filter text produced "No options", clear it so real options appear
                                                try:
                                                    _kf_text = (await _rc_opts.first.inner_text()).strip().lower()
                                                    if "no option" in _kf_text or "no result" in _kf_text:
                                                        await el.evaluate(
                                                            "el => { el.value = ''; el.dispatchEvent(new Event('input', {bubbles: true, cancelable: true})); }"
                                                        )
                                                        await asyncio.sleep(0.5)
                                                except Exception:
                                                    pass
                                                await el.press("ArrowDown")
                                                await asyncio.sleep(0.3)
                                                await el.press("Enter")
                                                _rc_clicked = True
                                                print(f"  [LLM] React Select fill: keyboard fallback")
                                            else:
                                                print(f"  [LLM] React Select fill: dropdown closed, cannot select")

                                        if _rc_clicked:
                                            await asyncio.sleep(0.3)
                                            try:
                                                _rc_id = await el.get_attribute("id") or ""
                                                if _rc_id:
                                                    _forced_filled[_rc_id] = value
                                                    print(f"  [LLM] React Select fill: marked {_rc_id!r} as filled")
                                            except Exception:
                                                pass
                                    else:
                                        await _human_type(el, value)
                                        await asyncio.sleep(1.5)
                                # Auto-select from any autocomplete/typeahead dropdown that appeared.
                                # Covers: LinkedIn typeahead, jQuery UI, Selectize (non-combobox fields).
                                if not _is_gh_country and not _is_react_combobox:
                                    _autocomplete_sel = (
                                        '[role="option"]:not([id^="iti"]), '   # skip intl-tel-input phone prefix
                                        '[role="listbox"] li:not([id^="iti"]), '
                                        '.ui-autocomplete li, '               # jQuery UI (Greenhouse country)
                                        '.ui-menu-item, '
                                        '.select2-results__option, '          # Select2
                                        '.basic-typeahead__selectable, '
                                        '.search-typeahead-v2__hit'
                                    )
                                    _clicked_suggestion = False
                                    try:
                                        sug = page.locator(_autocomplete_sel).first
                                        # Wait up to 2.5s for autocomplete to appear
                                        if await sug.count() == 0:
                                            try:
                                                await sug.wait_for(state="visible", timeout=2500)
                                            except Exception:
                                                pass
                                        if await sug.count() > 0:
                                            try:
                                                await sug.click(timeout=3000)
                                                _clicked_suggestion = True
                                            except Exception:
                                                await sug.click(force=True, timeout=2000)
                                                _clicked_suggestion = True
                                    except Exception:
                                        pass
                                    if not _clicked_suggestion:
                                        # Keyboard fallback — only when a dropdown container is actually open.
                                        try:
                                            _is_expanded = await el.evaluate(
                                                "el => el.getAttribute('aria-expanded') === 'true' || el.getAttribute('aria-haspopup') !== null"
                                            )
                                            _open_list = await page.locator(
                                                '[role="listbox"]:not([aria-hidden="true"]), '
                                                '.ui-autocomplete:not([style*="display: none"]):not([style*="display:none"])'
                                            ).count()
                                            if _is_expanded or _open_list:
                                                await el.press("ArrowDown")
                                                await asyncio.sleep(0.3)
                                                await el.press("Enter")
                                                await asyncio.sleep(0.3)
                                                _clicked_suggestion = True
                                        except Exception:
                                            pass
                                    # Greenhouse autocomplete fields (location, etc.) commit their value
                                    # to a hidden field and clear the visible input. Track any field
                                    # whose value disappears after autocomplete selection so the LLM
                                    # sees it as FILLED and doesn't retry.
                                    if _clicked_suggestion and "greenhouse.io" in page.url:
                                        await asyncio.sleep(0.5)
                                        try:
                                            _ov_id = await el.get_attribute("id") or ""
                                            _post_sug_val = await el.input_value()
                                            if not _post_sug_val and _ov_id:
                                                _forced_filled[_ov_id] = value
                                                print(f"  [LLM] Greenhouse #{_ov_id!r}: marked as filled in prompt override")
                                        except Exception:
                                            pass
                except Exception as exc:
                    exc_str = str(exc)
                    print(f"  [LLM] Fill failed: {exc_str[:200]}")
                    if "captcha" in exc_str.lower() or "hcaptcha" in exc_str.lower():
                        print("  [LLM] CAPTCHA detected — cannot proceed, skipping")
                        return "skipped"

            elif action_type == "select" and selector and value:
                try:
                    if selector.startswith("#") and len(selector) > 1 and selector[1].isdigit():
                        selector = f'[id="{selector[1:]}"]'
                    el = page.locator(selector).first
                    if await el.count() > 0:
                        _sel_done = False
                        # Try native select_option first
                        try:
                            await el.select_option(label=value)
                            _sel_done = True
                        except Exception:
                            try:
                                await el.select_option(value=value)
                                _sel_done = True
                            except Exception:
                                pass
                        # Fallback: React Select / combobox (input[role="combobox"] or aria-haspopup)
                        if not _sel_done:
                            try:
                                _tag = (await el.evaluate("el => el.tagName")).lower()
                                _role = await el.get_attribute("role") or ""
                                _popup = await el.get_attribute("aria-haspopup") or ""
                                _is_combobox = _role == "combobox" or _popup or _tag == "input"
                                if _is_combobox:
                                    print(f"  [LLM] React Select combobox fallback for {selector!r}")
                                    _sel_opts = page.locator('[role="option"]:not([id^="iti"]), [role="listbox"] li, .select__option')
                                    _sel_decline_kws = ("decline", "prefer not", "rather not", "not to self", "no answer", "not wish", "i prefer not", "not disclose")
                                    _vl_sel = value.lower()
                                    _is_decline_sel = any(k in _vl_sel for k in _sel_decline_kws)
                                    _opt_clicked = False

                                    # Open dropdown WITHOUT typing first — click control container, not just input
                                    try:
                                        await el.scroll_into_view_if_needed()
                                    except Exception:
                                        pass
                                    try:
                                        await el.evaluate(
                                            "el => { const c = el.closest('[class*=\"__control\"]') "
                                            "|| el.closest('[class*=\"select__control\"]') "
                                            "|| el.parentElement; if(c) c.click(); }"
                                        )
                                    except Exception:
                                        pass
                                    await el.click()
                                    await asyncio.sleep(0.8)
                                    try:
                                        await _sel_opts.first.wait_for(state="visible", timeout=5000)
                                    except Exception:
                                        pass
                                    _sel_count_all = await _sel_opts.count()
                                    print(f"  [LLM] React Select: opened, {_sel_count_all} option(s) visible")

                                    # Exact/contains match on all unfiltered options
                                    for _oi in range(_sel_count_all):
                                        _opt = _sel_opts.nth(_oi)
                                        try:
                                            _ot = (await _opt.inner_text()).strip()
                                        except Exception:
                                            continue
                                        _otl = _ot.lower()
                                        if "no option" in _otl or "no result" in _otl:
                                            continue
                                        if _otl == _vl_sel or _vl_sel in _otl:
                                            await _opt.click(timeout=3000)
                                            _opt_clicked = True
                                            _sel_done = True
                                            print(f"  [LLM] React Select: clicked option {_ot!r}")
                                            try:
                                                _cb_id = await el.get_attribute("id") or ""
                                                if _cb_id:
                                                    _forced_filled[_cb_id] = value
                                            except Exception:
                                                pass
                                            break

                                    # Semantic decline match
                                    if not _opt_clicked and _is_decline_sel:
                                        for _oi in range(_sel_count_all):
                                            _opt = _sel_opts.nth(_oi)
                                            try:
                                                _ot = (await _opt.inner_text()).strip()
                                            except Exception:
                                                continue
                                            _otl = _ot.lower()
                                            if "no option" in _otl or "no result" in _otl:
                                                continue
                                            if any(k in _otl for k in _sel_decline_kws):
                                                await _opt.click(timeout=3000)
                                                _opt_clicked = True
                                                _sel_done = True
                                                print(f"  [LLM] React Select: clicked decline option {_ot!r}")
                                                try:
                                                    _cb_id = await el.get_attribute("id") or ""
                                                    if _cb_id:
                                                        _forced_filled[_cb_id] = value
                                                except Exception:
                                                    pass
                                                break

                                    # LLM-pick decline option — for EEO fields with no standard "decline" text
                                    if not _opt_clicked and _is_decline_sel and _sel_count_all > 0:
                                        _llm_sel_texts: list[str] = []
                                        for _oi in range(_sel_count_all):
                                            try:
                                                _ot = (await _sel_opts.nth(_oi).inner_text()).strip()
                                                if _ot and "no option" not in _ot.lower() and "no result" not in _ot.lower():
                                                    _llm_sel_texts.append(_ot)
                                            except Exception:
                                                continue
                                        if _llm_sel_texts:
                                            print(f"  [LLM] React Select: asking LLM to pick decline option from {_llm_sel_texts}")
                                            try:
                                                _llm_sel_resp = await self.llm_client.chat.completions.create(
                                                    model=self.model,
                                                    max_tokens=30,
                                                    messages=[{
                                                        "role": "user",
                                                        "content": (
                                                            f"Dropdown options: {_llm_sel_texts}\n"
                                                            f"Which option best means 'prefer not to disclose' or declining to answer? "
                                                            f"Reply with ONLY the exact option text from the list."
                                                        )
                                                    }]
                                                )
                                                _llm_sel_pick = (_llm_sel_resp.choices[0].message.content or "").strip().strip('"').strip("'")
                                                print(f"  [LLM] React Select: LLM picked {_llm_sel_pick!r}")
                                                for _oi in range(_sel_count_all):
                                                    try:
                                                        _opt = _sel_opts.nth(_oi)
                                                        _ot = (await _opt.inner_text()).strip()
                                                    except Exception:
                                                        continue
                                                    if _ot.lower() == _llm_sel_pick.lower() or _llm_sel_pick.lower() in _ot.lower() or _ot.lower() in _llm_sel_pick.lower():
                                                        await _opt.click(timeout=3000)
                                                        _opt_clicked = True
                                                        _sel_done = True
                                                        print(f"  [LLM] React Select: clicked LLM-picked option {_ot!r}")
                                                        try:
                                                            _cb_id = await el.get_attribute("id") or ""
                                                            if _cb_id:
                                                                _forced_filled[_cb_id] = value
                                                        except Exception:
                                                            pass
                                                        break
                                            except Exception as _llm_sel_exc:
                                                print(f"  [LLM] React Select: LLM pick failed: {_llm_sel_exc}")

                                    # Type to filter (for long autocomplete lists)
                                    # Skip for decline-type fields: typing a decline phrase always produces "No options".
                                    if not _opt_clicked and not _is_decline_sel:
                                        await el.fill(value)
                                        await asyncio.sleep(0.5)
                                        try:
                                            await _sel_opts.first.wait_for(state="visible", timeout=3000)
                                        except Exception:
                                            pass
                                        _sel_count_f = await _sel_opts.count()
                                        for _oi in range(_sel_count_f):
                                            _opt = _sel_opts.nth(_oi)
                                            try:
                                                _ot = (await _opt.inner_text()).strip()
                                            except Exception:
                                                continue
                                            _otl = _ot.lower()
                                            if "no option" in _otl or "no result" in _otl:
                                                continue
                                            if _otl == _vl_sel or _vl_sel in _otl:
                                                await _opt.click(timeout=3000)
                                                _opt_clicked = True
                                                _sel_done = True
                                                print(f"  [LLM] React Select: clicked filtered option {_ot!r}")
                                                try:
                                                    _cb_id = await el.get_attribute("id") or ""
                                                    if _cb_id:
                                                        _forced_filled[_cb_id] = value
                                                except Exception:
                                                    pass
                                                break

                                    # Keyboard fallback
                                    if not _opt_clicked:
                                        _sel_final = await _sel_opts.count()
                                        if _sel_final == 0:
                                            # Dropdown closed — reopen via container click
                                            try:
                                                await el.evaluate(
                                                    "el => { const c = el.closest('[class*=\"__control\"]') "
                                                    "|| el.closest('[class*=\"select__control\"]') "
                                                    "|| el.parentElement; if(c) c.click(); }"
                                                )
                                                await asyncio.sleep(0.5)
                                            except Exception:
                                                pass
                                            _sel_final = await _sel_opts.count()
                                        if _sel_final > 0:
                                            # If filter text produced "No options", clear it so real options appear
                                            try:
                                                _kf_text_sel = (await _sel_opts.first.inner_text()).strip().lower()
                                                if "no option" in _kf_text_sel or "no result" in _kf_text_sel:
                                                    await el.evaluate(
                                                        "el => { el.value = ''; el.dispatchEvent(new Event('input', {bubbles: true, cancelable: true})); }"
                                                    )
                                                    await asyncio.sleep(0.5)
                                            except Exception:
                                                pass
                                            await el.press("ArrowDown")
                                            await asyncio.sleep(0.3)
                                            await el.press("Enter")
                                            _sel_done = True
                                            print(f"  [LLM] React Select: keyboard fallback (ArrowDown+Enter)")
                                            try:
                                                _cb_id = await el.get_attribute("id") or ""
                                                if _cb_id:
                                                    _forced_filled[_cb_id] = value
                                            except Exception:
                                                pass
                                        else:
                                            print(f"  [LLM] React Select: dropdown closed, cannot select")
                            except Exception as _cb_exc:
                                print(f"  [LLM] React Select combobox fallback failed: {_cb_exc}")
                        await asyncio.sleep(0.5)
                except Exception as exc:
                    print(f"  [LLM] Select failed: {exc}")

            elif action_type == "click":
                _submit_keywords = (
                    "submit application", "submit", "send application", "complete application",
                    "finish application", "finish", "send my application",
                )
                # "apply" / "apply now" are submit-like only on button elements, not <a> nav links
                _apply_keywords = ("apply", "apply now", "apply for this job")
                combined = (selector + " " + text + " " + value).lower()
                _is_apply_word = any(k in combined for k in _apply_keywords)
                _is_anchor_only = selector.lstrip().startswith("a") and "button" not in selector
                is_submit_btn = (
                    any(k in combined for k in _submit_keywords)
                    or (_is_apply_word and not _is_anchor_only)
                )

                # Strip :has-text() from ID selectors — it's Playwright syntax, invalid in combined CSS
                _clean_sel = re.sub(r':has-text\(["\'][^"\']*["\']\)', '', selector).strip()
                loc_strs = [_clean_sel, f'button:has-text("{text}")', f'a:has-text("{text}")', f'[aria-label*="{text}"]']
                loc_strs = [l for l in loc_strs if l and l not in ('button:has-text("")', 'a:has-text("")', '[aria-label*=""]')]

                if is_submit_btn:
                    # If there are no form inputs on the current page this is a listing page
                    # "Apply Now" that NAVIGATES to the form — don't treat it as a submit.
                    try:
                        _form_inputs = await page.locator(
                            'input:not([type=hidden]):not([type=submit]):not([type=button]),'
                            'select, textarea'
                        ).count()
                    except Exception:
                        _form_inputs = 1  # assume form present on error
                    if _form_inputs == 0:
                        is_submit_btn = False

                if is_submit_btn:
                    # Route submit-type buttons through ready_to_submit callback.
                    # If the button is aria-disabled (e.g. Rippling), JS-click it to trigger
                    # client-side validation errors instead — then let the LLM loop continue
                    # to fill whatever required fields the errors reveal.
                    for loc_str in loc_strs:
                        try:
                            el = page.locator(loc_str).first
                            if await el.count() > 0 and await el.is_visible():
                                _btn_disabled = await el.evaluate(
                                    "el => el.getAttribute('aria-disabled') === 'true'"
                                    " || el.getAttribute('data-disabled') === 'true'"
                                )
                                if _btn_disabled:
                                    print(f"  [LLM] Submit button is aria-disabled — JS force-click to reveal validation errors")
                                    await el.evaluate("el => el.click()")
                                    await asyncio.sleep(1.5)
                                    # Continue step loop so LLM sees the validation errors
                                    break
                                return await self._handle_submit(page, el)
                        except Exception:
                            continue
                    else:
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
            last_action_type = action_type

        print("  [LLM] Reached step limit without completion")
        return "failed"

    async def _fill_external_form(self, page: Page) -> str:
        return await self._llm_guided_apply(page)

    async def _handle_submit(self, page: Page, submit_btn) -> str:
        summary = (
            f"External application for {self.profile.get('full_name', 'user')} "
            f"at {self.company_name or page.url}."
        )

        if self.auto_mode:
            ready = self.callbacks.get("ready_to_submit")
            result = (await ready(summary)) if ready else "applied"
            if result != "applied":
                return "skipped"
            url_before = page.url
            try:
                await submit_btn.click()
            except Exception:
                pass
        else:
            # Semi-auto: let user click submit manually in the browser.
            # Capture url_before NOW — before user interaction — so we can detect
            # navigation even if the page already moved by the time they press ENTER.
            url_before = page.url
            print(f"\n{'═' * 64}")
            print(f"  [Offsite] Submit button ready — please click it manually in the browser.")
            print(f"  {summary}")
            print(f"{'═' * 64}")
            fill_focused = self.callbacks.get("fill_focused")
            while True:
                try:
                    choice = input(
                        "  [ENTER] = I submitted manually   [s] = skip   [f] = LLM-fill focused field\n"
                        "  > "
                    ).strip().lower()
                except (EOFError, KeyboardInterrupt):
                    choice = "s"
                if choice in ("s", "skip"):
                    return "skipped"
                if choice == "f" and fill_focused:
                    await fill_focused()
                    continue
                break

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
            action = "clicked" if self.auto_mode else "submitted manually"
            print(f"  [Offsite] ⚠ Submit {action} but {msg}")
            return "failed"

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
                # URL path has explicit success keywords — strong signal
                _success_url_words = ("confirm", "success", "thank", "applied", "complete", "submitted", "done", "sent")
                if any(k in new_url for k in _success_url_words):
                    return True, f"URL changed to success path → {page.url[:80]}"
                # URL changed to ambiguous destination — require confirmation text too
                return False, f"URL changed but no confirmation text or success URL pattern → {page.url[:80]}"

            # Check for form validation errors on same page
            error_clues = [p for p in ("required", "please complete", "please fill",
                                       "missing", "invalid email") if p in content]
            if error_clues:
                return False, f"form showing validation errors: {error_clues[:2]}"
            return False, "URL unchanged and no confirmation text found"
        except Exception as exc:
            return False, f"verification error: {exc}"

    # ── Authentication helpers ─────────────────────────────────────────────────

    async def _handle_sso_page(self, page: Page) -> bool:
        """
        Handles enterprise SSO / Identity Provider pages (Microsoft, Google, Okta, Auth0).
        Uses the applicant's email from user_profile and, if available, an `sso_password` field.
        Returns True if we appear to have authenticated (URL left the SSO domain).
        """
        domain = urlparse(page.url).netloc.lower()
        email = self.profile.get("email", "")
        password = self.profile.get("sso_password") or self.profile.get("password", "")
        if not email:
            print(f"  [SSO] No email in profile — cannot sign in to {domain}")
            return False

        print(f"  [SSO] Attempting sign-in on {domain} as {email}")

        try:
            # Step 1: fill email field
            for sel in (
                'input[type="email"]', '#i0116', '#email', 'input[name="email" i]',
                'input[name="identifier"]', 'input[placeholder*="email" i]',
                'input[autocomplete="username"]',
            ):
                _el = page.locator(sel).first
                if await _el.count() > 0 and await _el.is_visible():
                    await _el.fill(email)
                    await asyncio.sleep(0.4)
                    break

            # Step 2: click Next / Continue to advance to the password step
            for sel in (
                '#idSIButton9',                              # Microsoft "Next"
                'input[type="submit"]',
                'button[type="submit"]',
                'button:has-text("Next")', 'button:has-text("Continue")',
                'button:has-text("Sign in")', 'button:has-text("Log in")',
            ):
                _btn = page.locator(sel).first
                if await _btn.count() > 0 and await _btn.is_visible():
                    await _btn.click()
                    await asyncio.sleep(2)
                    break

            if not password:
                print(f"  [SSO] No password in profile — skipping after email step")
                return False

            # Step 3: wait for password field (Microsoft shows it on next screen)
            _pw_el = None
            for sel in ('input[type="password"]', '#i0118', '#password',
                        'input[name="password" i]', 'input[name="passwd"]'):
                _candidate = page.locator(sel).first
                try:
                    await _candidate.wait_for(state="visible", timeout=6000)
                    _pw_el = _candidate
                    break
                except Exception:
                    continue

            if _pw_el is None:
                print(f"  [SSO] Password field not found after Next — checking if already past SSO")
                _new_domain = urlparse(page.url).netloc.lower()
                _sso_domains_check = ("microsoftonline.com", "google.com", "okta.com",
                                      "auth0.com", "onelogin.com", "pingidentity.com")
                return not any(s in _new_domain for s in _sso_domains_check)

            await _pw_el.fill(password)
            await asyncio.sleep(0.4)

            # Step 4: submit
            for sel in (
                '#idSIButton9',                              # Microsoft "Sign in"
                'button[type="submit"]', 'input[type="submit"]',
                'button:has-text("Sign in")', 'button:has-text("Sign In")',
                'button:has-text("Log in")', 'button:has-text("Login")',
            ):
                _btn = page.locator(sel).first
                if await _btn.count() > 0 and await _btn.is_visible():
                    await _btn.click()
                    await asyncio.sleep(3)
                    break

            # Step 5: handle "Stay signed in?" prompt (Microsoft)
            for sel in ('#idSIButton9', 'button:has-text("Yes")', 'button:has-text("No")'):
                _btn = page.locator(sel).first
                try:
                    await _btn.wait_for(state="visible", timeout=4000)
                    if await _btn.count() > 0:
                        await _btn.click()
                        await asyncio.sleep(2)
                        break
                except Exception:
                    break

            _final_domain = urlparse(page.url).netloc.lower()
            _sso_domains_chk = ("microsoftonline.com", "google.com", "okta.com",
                                 "auth0.com", "onelogin.com", "pingidentity.com")
            success = not any(s in _final_domain for s in _sso_domains_chk)
            print(f"  [SSO] Sign-in {'succeeded' if success else 'still on SSO page'} → {page.url[:80]}")
            return success

        except Exception as _sso_exc:
            print(f"  [SSO] Sign-in error: {_sso_exc}")
            return False

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
