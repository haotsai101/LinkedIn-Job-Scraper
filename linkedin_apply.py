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
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlparse, parse_qs
# Optional heavy dep: only needed when actually running an apply session. Guarded so
# the module (and its pure helpers like _get_profile_value) can be imported in
# environments without playwright installed — e.g. the test suite.
try:
    from playwright.async_api import Page, BrowserContext
except ImportError:  # pragma: no cover
    Page = BrowserContext = object

import config
import llm
# Shared stdlib-only helpers (ticket T4). ``_write_llm_log`` keeps its old private
# name as a thin alias so the ~6 internal call sites are untouched.
from common import extract_json_object, strip_code_fence
from common import write_llm_log as _write_llm_log

# Session timestamp prefix for screenshot filenames — ensures cross-run uniqueness
_SESSION_TS = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

# The browser agent's LLM: the Claude Agent SDK on subscription auth (ticket
# T14b). Every call site here builds a fully self-contained prompt (profile +
# job context + progress recap + page snapshot + rules, re-sent each call), so
# each goes through ``llm.query`` — a fresh one-shot session per call, no shared
# conversation history to accumulate. Model is ``self.model`` on the flow /
# engine (``config.get_llm_config("guided_apply").model``).


def _css_id(el_id: str) -> str:
    """Escape CSS-special characters in an element ID for use in a #id selector."""
    return re.sub(r'([\[\]().#+*?^$|{},~>\\])', r'\\\1', el_id)


# A *bare* CSS identifier as it occurs in this codebase: ASCII letters, digits,
# "_" and "-", not starting with a digit / "-<digit>" / "--". (Real CSS also
# permits non-ASCII and backslash escapes; we never emit those here.)
_BARE_CSS_IDENT_RE = re.compile(r'^-?[A-Za-z_][A-Za-z0-9_-]*$')
# Presence of any of these means the selector is more than one bare token:
# descendant (whitespace) + the child / adjacent / sibling / list combinators.
_SELECTOR_COMBINATOR_RE = re.compile(r'[\s>+~,]')


def _safe_selector(sel: str) -> str:
    """Normalise a bare ``#id`` / ``tag#id`` selector to the ``[id="…"]``
    attribute form whenever the id is not a valid *bare* CSS identifier.

    Why: React 18's ``useId()`` emits colon-wrapped ids like
    ``react-select-:Rehufl7rrrrlcq:-input``. ``#react-select-:R…:-input`` is not
    a valid CSS selector — Playwright's CSS engine raises
    ``SyntaxError: '…' is not a valid selector`` — so every react-select field on
    such a form becomes unfillable (observed live: Lumenalta job, all 3 fields,
    T44). ``[id="react-select-:R…:-input"]`` quotes the id, making the colons
    (and dots, brackets, parens) literal and safe. This generalisation also
    subsumes the older ``#<leading-digit>`` / ``input#<leading-digit>`` case
    (``#1foo`` -> ``[id="1foo"]``).

    Scope / limitation (deliberate): only a *bare* id token is rewritten. A
    selector that contains a combinator (whitespace, ``>``, ``+``, ``~``, ``,``)
    is passed through untouched. A trailing pseudo-class / attribute part fused
    to the same token (``#id:hover``, ``#id[data-x]``) would be folded into the
    quoted id rather than split off — but the LLM apply loop only ever emits
    bare ``#id`` / ``tag#id`` fill/select selectors, so this is acceptable;
    splitting id-from-trailer for every CSS case is error-prone and out of
    scope (T44).
    """
    if not sel or _SELECTOR_COMBINATOR_RE.search(sel):
        return sel
    if sel.startswith("#"):
        tag, ident = "", sel[1:]
    else:
        m = re.match(r'^([A-Za-z][A-Za-z0-9-]*)#(.+)$', sel)
        if not m:
            return sel
        tag, ident = m.group(1), m.group(2)
    if not ident or "#" in ident or _BARE_CSS_IDENT_RE.match(ident):
        return sel
    escaped = ident.replace("\\", "\\\\").replace('"', '\\"')
    return f'{tag}[id="{escaped}"]'


# ── Browser tab / renderer crash detection (T36) ──────────────────────────────
# Chromium tab/renderer crashes happen on memory-heavy ATS SPAs (large React
# forms). Playwright then raises ``TargetClosedError`` (a subclass of
# ``playwright.async_api.Error``) or a plain ``Error`` whose message contains
# "Target crashed" from whatever operation was in flight (``Locator.count``, a
# fill, a click). It is a *transient* fault — the job should be retried (-2), not
# dead-ended (-3) — but it must be caught deliberately so it does not escape as a
# generic unhandled error and, crucially, so the shared browser page can be
# rebuilt before the next job (see ``apply_jobs._recover_browser_if_crashed``).
try:  # pragma: no cover - import shape varies by playwright version
    from playwright._impl._errors import TargetClosedError as _TargetClosedError
except Exception:  # pragma: no cover
    _TargetClosedError = None

_CRASH_MARKERS = (
    "target crashed",
    "page crashed",
    "renderer process crashed",
    "target page, context or browser has been closed",
    "target closed",
    "browser has been closed",
    "page has been closed",
)


def _is_browser_crash(exc: BaseException) -> bool:
    """True when *exc* is a Chromium tab/renderer crash or a closed-target error.

    Matched both by type (``TargetClosedError``) and by message substring so it
    fires whichever form the caller happens to see. Deliberately narrow: a
    Playwright ``TimeoutError`` or a selector ``SyntaxError`` is NOT a crash.
    """
    if _TargetClosedError is not None and isinstance(exc, _TargetClosedError):
        return True
    msg = str(getattr(exc, "message", "") or exc).lower()
    return any(m in msg for m in _CRASH_MARKERS)


async def _human_type(el, value: str):
    """Type text character-by-character with random delays to mimic human input.

    Falls back to direct el.fill() if press_sequentially times out (e.g. Ashby
    long-answer textareas whose JS character-count handlers stall the event loop).
    """
    await el.click()
    await asyncio.sleep(random.uniform(0.1, 0.3))
    await el.clear()
    try:
        for _ch in str(value):
            await el.press_sequentially(_ch, delay=0)
            _r = random.random()
            if _r < 0.05:
                await asyncio.sleep(random.uniform(0.35, 0.8))   # rare pause
            elif _r < 0.20:
                await asyncio.sleep(random.uniform(0.12, 0.35))  # hesitation
            else:
                await asyncio.sleep(random.uniform(0.04, 0.12))  # normal
    except Exception as _ps_exc:
        if "Timeout" in str(_ps_exc) or "timeout" in str(_ps_exc):
            # character-count / autosave listeners can stall keydown events;
            # el.fill() bypasses keyboard events entirely and succeeds where
            # per-char typing stalls.
            print(f"  [fill] press_sequentially timed out — falling back to direct fill()")
            try:
                await el.fill(str(value))
            except Exception as _fb_exc:
                print(f"  [fill] fill() fallback also failed: {_fb_exc}")
                raise
        else:
            raise
    await asyncio.sleep(random.uniform(0.05, 0.2))

_AUTH_HOSTPATHS = ("/login", "/checkpoint", "/uas/")
_ACCOUNTS_PATH = "created_accounts.json"

# CSS for reCAPTCHA / hCaptcha widgets the agent cannot solve.
_CAPTCHA_WIDGET_SELECTOR = (
    "div.g-recaptcha, iframe[src*='recaptcha'], iframe[title*='recaptcha'], "
    "input[name='g-recaptcha-response'], textarea[id*='g-recaptcha-response'], "
    "input[id*='g-recaptcha-response']"
)
# Visible-text markers for Greenhouse's text "security code" bot wall (NOT a
# g-recaptcha widget — a human can type it, so in interactive mode we pause;
# in --auto there is no one to type it, so we skip).
_GREENHOUSE_SECURITY_SIGNALS = (
    "security code", "enter the code", "enter code", "verification code",
    "please enter the characters", "type the characters", "prove you're human",
    "prove you are human", "i'm not a robot", "i am not a robot",
    "are you human", "bot check", "human verification",
)

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

# Greenhouse board / embed hosts. A LinkedIn "OffsiteApply" job whose
# ``application_url`` points at one of these normally renders the application
# form directly — but some large employers configure the board host to
# 30x-redirect to a company-branded careers SPA that hides the apply CTA behind
# a click (T39 — MongoDB: boards.greenhouse.io/mongodb/jobs/<id> →
# www.mongodb.com/careers/jobs/<id>). ``job-boards.greenhouse.io/<slug>/jobs/<id>``
# always renders the bare form with no company wrapper, so it is the canonical
# retry target when that redirect is detected.
_GREENHOUSE_HOSTS = (
    "boards.greenhouse.io", "job-boards.greenhouse.io",
    "boards.eu.greenhouse.io", "job-boards.eu.greenhouse.io",
    "grnh.se",
)


def _host_is_greenhouse(host: str) -> bool:
    host = (host or "").lower()
    return host in _GREENHOUSE_HOSTS or host == "greenhouse.io" or host.endswith(".greenhouse.io")


def _parse_greenhouse_job(url: str) -> tuple[str, str] | None:
    """Extract ``(slug, job_id)`` from a Greenhouse board / embed URL.

    Only the *recognized* shapes yield a result — a bare first path segment is
    never guessed to be a board slug (that produced junk canonical URLs):
      * ``boards.greenhouse.io/<slug>/jobs/<id>`` (``?gh_jid=`` / ``.eu`` / the
        ``job-boards`` host are all the same path shape)
      * ``…/embed/job_app?token=<id>&for=<slug>``

    Returns ``None`` when a slug + numeric id can't both be recovered from one
    of those.
    """
    try:
        parsed = urlparse(url or "")
    except Exception:
        return None
    if not _host_is_greenhouse(parsed.netloc):
        return None
    qs = parse_qs(parsed.query)
    segments = [s for s in parsed.path.split("/") if s]

    slug = None
    job_id = None

    # Path form: /<slug>/jobs/<id>
    if len(segments) >= 3 and segments[-2] == "jobs" and segments[-1].isdigit():
        slug, job_id = segments[-3], segments[-1]

    # Query fallback for the id (embed/job_app?token=, ?gh_jid=, …)
    if not job_id:
        for key in ("gh_jid", "token", "job_id", "jobId"):
            if qs.get(key) and qs[key][0].isdigit():
                job_id = qs[key][0]
                break
    # Query fallback for the slug — a recognized shape (?for=), never segments[0]
    if not slug and qs.get("for"):
        slug = qs["for"][0]

    if slug and job_id:
        return slug, job_id
    return None


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


def _terminal_state_for_stall(page, *, form_engaged: bool) -> str:
    """Shared give-up outcome for the OffsiteApply step loop (T45, extends T39).

    A stall where the loop never engaged a real form field on a non-ATS host is a
    navigation dead end that needs a human -> ``"blocked"`` (``applied=-3``, out
    of the ``--reset-failed`` retry pool). A stall *after* real form interaction,
    or on a known ATS form host, is a transient failure worth retrying ->
    ``"failed"`` (``applied=-2``).

    Mirrors the inline check T39 added at the URL-unchanged stuck guard exactly
    (same host extraction, same ATS-host set) so all three give-up sites — the
    URL-unchanged guard, the repeated-action guard and the step-limit exit — reach
    an identical verdict.
    """
    host = urlparse(page.url).netloc.lower()
    on_ats_host = any(d in host for d in (*_FORM_DOMAINS, *_ATS_REQUIRE_APPLY_PATH))
    if not form_engaged and not on_ats_host:
        return "blocked"
    return "failed"


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


# Adjectives that can sit between "years of" and "experience" without turning an
# overall-experience question into a skill/role-qualified one — the label still
# asks for the applicant's whole career length. ("total" / "relevant" are left
# out on purpose: the dedicated "total / relevant experience" branch further
# down owns those, with its own default.)
_BARE_EXPERIENCE_FILLERS = (
    "professional", "work", "working", "industry", "overall",
    "full-time", "full time", "fulltime", "paid", "hands-on",
    "hands on", "prior", "previous", "combined", "cumulative",
)

# Generic English words that can follow "as a/an" or "with/in/using" in an
# overall-experience question without naming a concrete role, skill, or domain
# ("...as a whole", "...in a professional setting", "...in the software
# industry", "...in the US"). If the qualifier span is *only* words like these,
# the label is still a whole-career question and keeps the full figure.
_GENERIC_QUALIFIER_WORDS = {
    "whole", "professional", "result", "results", "rule", "minimum", "maximum",
    "team", "teams", "group", "groups", "company", "companies", "organization",
    "organisation", "org", "general", "total", "overall", "role", "roles",
    "position", "positions", "capacity", "environment", "environments",
    "setting", "settings", "space", "world", "country", "countries", "region",
    "regions", "industry", "industries", "field", "fields", "area", "areas",
    "career", "careers", "profession", "workforce", "workplace", "context",
    "manner", "level", "levels", "way", "matter", "job", "jobs", "this",
    "that", "which", "what", "all", "such", "these", "those", "here", "your",
    "years", "months", "year", "month", "us", "usa", "america", "any",
    # generic job words + the applicant's own industry (not literally in the
    # profile text, so the background check below would miss them)
    "contributor", "employee", "individual", "tech", "technology", "it",
}
# NB: "sector" / "domain" / "market" are deliberately NOT generic — "in the
# insurance sector", "in the payments domain" are real domain qualifiers.

# Connective words dropped from a qualifier span before it is inspected.
_QUALIFIER_CONNECTIVES = {
    "the", "a", "an", "of", "and", "or", "with", "in", "using", "at", "for",
    "to", "as", "on", "my", "our", "their",
}


def _split_skill_string(raw: str) -> list[str]:
    """Split a string-form ``skills`` list on ``,`` / ``;`` and strip a leading
    conjunction from each entry: ``"Python, Go, and R"`` -> ``["Python", "Go",
    "R"]`` (T48 — an un-stripped ``"and R"`` never matched the exact single-char
    skill check in :func:`_years_label_names_a_foreign_role_or_skill`, so
    ``"years of experience with R"`` was mis-floored). Entries may be blank;
    callers already filter those out.
    """
    return [s.strip().removeprefix("and ").strip() for s in re.split(r"[,;]", raw)]


def _years_label_names_a_foreign_role_or_skill(l: str, profile: dict) -> bool:
    """True when a "years of experience" label pins the experience to a role,
    skill, or domain the applicant has **not** worked in ("...as a Lead",
    "...experience with COBOL"). Only then is the label routed through the T32
    tiered "years of <skill/role>" branch, which floors unfamiliar tenure at
    "1" instead of returning the applicant's whole career length (T40 — observed
    live: "How many years of experience do you have as a Lead?" -> "4" for a
    "Software Engineer").

    Returns ``False`` — keep the full figure — for:
      * bare / generic phrasing ("...as a whole", "...in the software
        industry", "...in a professional setting"); and
      * a role / skill / domain that already appears in the applicant's own
        ``current_title`` / ``headline`` / ``skills`` / ``summary``
        ("...as a Software Engineer" for a Software Engineer, "...in AI/ML" for
        an ML engineer) — that experience is genuinely theirs.

    Erring toward ``False`` is deliberate: an under-claimed "1"/"2" can trip a
    "minimum N years" knockout filter, which is the exact failure T32 guards
    against. ``l`` is the already-normalized (lower-cased, ws-collapsed) label.
    """
    spans: list[str] = []
    m_role = re.search(r'\bas\s+an?\s+([a-z0-9.#+][a-z0-9 &/.+#-]*)', l)
    if m_role:
        spans.append(m_role.group(1))
    m_skill = re.search(
        r'\bexperience\b.*?\b(?:with|in|using)\s+([a-z0-9.#+][a-z0-9 &/.+#-]*)', l)
    if m_skill:
        spans.append(m_skill.group(1))
    if not spans:
        return False

    # Text the applicant can legitimately claim tenure in. ``summary`` is free
    # prose, so this set is deliberately permissive — any tech name-dropped in
    # the summary reads as non-foreign (a mild overclaim, the agreed-safe
    # direction here). Drop ``summary`` for a tighter check if overclaim ever
    # becomes the bigger concern than the "minimum N years" knockout.
    bg = " ".join(str(profile.get(k) or "") for k in
                  ("current_title", "headline", "summary", "skills")).lower()

    # Exact (case-insensitive) skill-list entries, for the single-char check
    # below ("...experience with R" for someone whose skills list has "R").
    _skills_raw = profile.get("skills") or []
    if isinstance(_skills_raw, str):
        _skills_raw = _split_skill_string(_skills_raw)
    skill_entries = {str(s).strip().lower() for s in _skills_raw if str(s).strip()}

    def _anchored_in_bg(token: str) -> bool:
        # Whole-token match against the profile text, not a substring: "go"
        # matches "Go" / "and Go," but not "golang" or "category". The boundary
        # class includes "-" and "_" (T48) so a 2-char token can't match a
        # hyphen fragment ("go" in "go-to", "co" in "co-founder", "ai" in
        # "ai-driven") — common résumé-prose conjunctive prefixes.
        return bool(re.search(
            r'(?<![a-z0-9+#.\-_])' + re.escape(token) + r'(?![a-z0-9+#.\-_])', bg))

    for span in spans:
        # Whole-span match first: a multi-token acronym pair ("ai/ml", "ai ml",
        # "a/b testing") that appears verbatim in the profile text or skills
        # list, but whose individual tokens are too short to match on their own.
        norm = re.sub(r'\s*/\s*', '/', re.sub(r'\s+', ' ', span)).strip()
        if any(len(f) >= 2 and _anchored_in_bg(f)
               for f in {norm, norm.replace('/', ' ')}):
            continue
        # Split on whitespace AND slashes so "ai/ml" -> ["ai", "ml"].
        words = [w.strip(".,?!:;()'\"") for w in re.split(r'[\s/]+', span)]
        words = [w for w in words if w and w not in _QUALIFIER_CONNECTIVES]
        if not words:
            continue
        # A generic filler word anywhere in the span -> not a real qualifier.
        if any(w in _GENERIC_QUALIFIER_WORDS for w in words):
            continue
        # The qualifier names something already in the applicant's background
        # -> genuine experience, keep the full figure. 2-char tokens (ai, ml,
        # ui, ux, bi, go, qa) ARE checked here: this is an anchored regex
        # against real profile prose, so "ai" matching "AI applications" is a
        # true positive. Single letters stay out (too noisy) -- except an
        # exact skill-list entry ("R", "C"), handled just below.
        if any(len(w) >= 2 and _anchored_in_bg(w) for w in words):
            continue
        # A single-char qualifier that is *exactly* a listed skill ("...with R"
        # for someone who lists "R") -> genuine experience, keep the full figure.
        if any(len(w) == 1 and w in skill_entries for w in words):
            continue
        # Otherwise: a concrete role / skill / domain foreign to the applicant.
        return True
    return False


# ── _get_profile_value: ordered (matcher, resolver) rule table (T47) ───────────
#
# Historically a ~80-branch sequential `if` cascade over the normalized label +
# `kind`. Every rule added in T40/T41/T42/T43 had to reason about *global* branch
# ordering to keep an earlier branch from stealing a label. T47 makes that
# ordering a single explicit list: `_PROFILE_VALUE_RULES` is iterated once, and
# the first rule whose `.matches(lbl, kind, p)` is True returns its
# `.resolve(lbl, kind, p)` — exactly reproducing "first matching `if` wins".
#
# Fall-through semantics: in the original cascade, *every* branch whose `if`
# condition was true executed a `return`. There is no "matched the label but fell
# through to a later branch" case that is not already encoded in the `if`
# condition itself. So a data-conditional branch such as the T43 combined
# City/State rule (`... and _location`) or the T40 bare-years rule (`... and not
# _years_label_names_a_foreign_role_or_skill(...)`) simply folds that condition
# into `.matches`: a data-less / foreign-qualifier profile fails the matcher and
# iteration continues to the next rule — identical to the old `if` being False.

_SELECTISH_KINDS = ("select", "select-one", "select-multiple", "radio", "checkbox")
_CHOICE_KINDS = ("select", "select-one", "radio")

# Residency-exclusion question building blocks (combinatorial verb × region).
_RESIDE_VERBS = ("reside in", "based in", "located in", "live in")
_EXCL_REGIONS = ("mexico", "latin", "south america", "central america")

# Multi-word location phrases matched as plain substrings. The bare word "city"
# is matched on a \bcity\b boundary instead (T41 — a substring test also fires
# inside "capacity").
_LOC_PHRASES = ("location", "where are you", "your location", "current location",
                "what is your current location", "city, state", "city/state")

# AI / ML stack keywords: a text/number field naming one of these resolves to the
# "1" floor — unless it is a "years of <foreign AI skill>" question, which drops
# to the tiered years rule to be floored consistently there (T40).
_AI_SKILL_KEYWORDS = (
    "generative ai", "gen ai", "llm", "large language model",
    "multimodal", "agentic", "rag", "retrieval augmented",
    "vector database", "embedding", "fine tun",
    "artificial intelligence", " ai ", "machine learning", "deep learning",
    "neural network", "natural language", "nlp", "computer vision",
    "data science", "data scientist", "model training", "model development",
    "model deployment", "ai/ml", "ai agent", "ai engineer", "prompt engineer",
    "transformer", "diffusion model", "reinforcement learning",
)

# Social / platform URL fields we hold no value for — return "" so the LLM
# fallback cannot fabricate a handle.
_UNKNOWN_SOCIAL = (
    "twitter", "x.com", "x handle", "x profile",
    "facebook", "instagram", "tiktok", "youtube",
    "medium.com", "substack", "blog url", "blog link",
    "stackoverflow", "stack overflow",
    "behance", "dribbble", "devpost", "kaggle",
    "other url", "other link", "other social", "other profile",
    "personal url", "social media url", "social profile",
)


class _ProfileRule(NamedTuple):
    """One entry in the ordered ``_PROFILE_VALUE_RULES`` table.

    ``matches(label, kind, profile) -> bool`` — the (former) ``if`` condition.
    ``resolve(label, kind, profile) -> str | None`` — the (former) ``return``
    expression; ``None`` is a valid resolved value and still stops iteration
    (e.g. an unknown-social field resolves to ``""``, a cover-letter field to
    ``None``).
    """

    name: str
    matches: Callable[[str, str, dict], bool]
    resolve: Callable[[str, str, dict], "str | None"]


def _is_years_query(lbl: str) -> bool:
    """Label asks about a span of time ("years of", "how many months", …).

    Formerly the ``_is_years_q`` local; gates the LinkedIn/GitHub URL rules and
    the AI-skills rule so a "years of experience with LinkedIn's API" style
    question is not answered with a profile URL.
    """
    return any(k in lbl for k in
               ("years of", "how many years", "how many months", "years experience"))


def _is_bare_years_experience(lbl: str) -> bool:
    """Label is an *overall* "years of experience" question — optionally with a
    filler adjective ("years of professional experience") but with no concrete
    role/skill/domain qualifier. Formerly the ``_bare_years_exp`` local.
    """
    return (
        any(k in lbl for k in ("years of experience", "years experience", "total experience"))
        or any(f"years of {f} experience" in lbl for f in _BARE_EXPERIENCE_FILLERS)
        or any(f"years {f} experience" in lbl for f in _BARE_EXPERIENCE_FILLERS)
    )


def _full_name_parts(p: dict) -> list[str]:
    return (p.get("full_name") or "").split()


def _resolve_resume(lbl: str, kind: str, p: dict) -> "str | None":
    raw = p.get("resume_path", "")
    if raw:
        return os.path.abspath(raw) if not os.path.isabs(raw) else raw
    return None


def _resolve_first_name(lbl: str, kind: str, p: dict) -> "str | None":
    parts = _full_name_parts(p)
    return parts[0] if parts else None


def _resolve_last_name(lbl: str, kind: str, p: dict) -> "str | None":
    parts = _full_name_parts(p)
    return parts[-1] if len(parts) > 1 else (parts[0] if parts else None)


def _resolve_years_of_skill(lbl: str, kind: str, p: dict) -> str:
    """Tiered answer for an unmapped "years of <specific skill>" question (T32).

    "0" reads as "no experience at all" and gets the applicant auto-filtered, so
    it is never returned here. Tiered so we neither undersell nor fabricate:
      * a skill the applicant explicitly lists -> their full tenure, capped at
        the overall years_experience figure (never inflated);
      * a skill adjacent to their background (a content word from the question
        also appears in their title / headline / summary / skill list) -> 2;
      * anything genuinely unrecognised (COBOL, "management" for an IC) -> "1".
    """
    try:
        _tot_years = int(float(str(p.get("years_experience", "")).strip() or 0))
    except (TypeError, ValueError):
        _tot_years = 0
    if _tot_years <= 0:
        return "1"
    _skills = p.get("skills") or []
    if isinstance(_skills, str):
        _skills = _split_skill_string(_skills)
    _skill_parts = [
        _part.strip()
        for _sk in _skills
        for _part in re.split(r"[/,]", str(_sk).lower())
        if len(_part.strip()) >= 2
    ]
    # Tier 1: the question names a skill the applicant explicitly lists. Same
    # anchored whole-token match as _anchored_in_bg, incl. the "-"/"_" boundary
    # chars (T48) so a 2-char skill token ("go", "ai", "r") can't match a hyphen
    # fragment in the label ("...with a go-to approach" must not match skill "Go").
    for _part in _skill_parts:
        if re.search(r"(?<![a-z0-9+#.\-_])" + re.escape(_part) + r"(?![a-z0-9+#.\-_])", lbl):
            return str(_tot_years)
    # Tier 2: adjacency — a substantive word from the question (>=4 chars, not
    # application-form boilerplate) appears in the applicant's own background.
    _bg = " ".join(str(p.get(_k) or "") for _k in
                   ("current_title", "headline", "summary")).lower()
    _bg += " " + " ".join(_skill_parts)
    _STOP = {"years", "year", "months", "month", "experience", "many",
             "with", "have", "your", "using", "working", "work", "professional",
             "hands", "practical", "level", "developing", "development",
             "about", "please", "tell", "describe", "paragraph"}
    _adjacent = any(
        len(_w) >= 4 and _w not in _STOP and _w in _bg
        for _w in re.findall(r"[a-z]+", lbl)
    )
    return str(min(_tot_years, 2)) if _adjacent else "1"


def _resolve_opt_stem(lbl: str, kind: str, p: dict) -> str:
    auth = (p.get("work_authorization") or "").upper()
    return "Yes" if any(x in auth for x in ("OPT", "STEM")) else "No"


def _resolve_sponsor(lbl: str, kind: str, p: dict) -> str:
    if p.get("need_sponsorship", "").lower() in ("yes", "true", "1"):
        return "Yes"
    auth = (p.get("work_authorization") or "").upper()
    return "Yes" if any(x in auth for x in ("OPT", "H1B", "H-1B", "F1", "TN")) else "No"


def _resolve_salary(lbl: str, kind: str, p: dict) -> str:
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


def _resolve_specific_degree(lbl: str, kind: str, p: dict) -> str:
    edu = p.get("education", {}) if isinstance(p.get("education"), dict) else {}
    user_rank = _degree_rank(edu.get("degree") or "")
    if any(t in lbl for t in ("doctor", "ph.d", "phd")):
        required_rank = 4
    elif "master" in lbl:
        required_rank = 3
    elif "bachelor" in lbl:
        required_rank = 2
    else:
        required_rank = 1  # associate
    return "Yes" if user_rank >= required_rank else "No"


def _resolve_highest_education(lbl: str, kind: str, p: dict) -> str:
    edu = p.get("education", {})
    # "Have you completed the following level of education: X?" is a Yes/No radio.
    if kind in ("radio",):
        return "Yes" if (isinstance(edu, dict) and edu.get("degree")) else "No"
    deg = (edu.get("degree") or "") if isinstance(edu, dict) else ""
    _deg_map = {"m.s.": "Master's Degree", "ms": "Master's Degree", "m.s": "Master's Degree",
                "b.s.": "Bachelor's Degree", "bs": "Bachelor's Degree", "b.s": "Bachelor's Degree",
                "ph.d": "Doctorate", "phd": "Doctorate", "mba": "Master's Degree"}
    return _deg_map.get(deg.lower().strip("."), deg) or "Master's Degree"


def _resolve_grad_year(lbl: str, kind: str, p: dict) -> "str | None":
    edu = p.get("education", {}) if isinstance(p.get("education"), dict) else {}
    yr = str(edu.get("year", "")).strip()
    return yr if yr else None


def _resolve_degree(lbl: str, kind: str, p: dict) -> "str | None":
    edu = p.get("education", {})
    if kind == "radio":
        return "Yes" if (isinstance(edu, dict) and edu.get("degree")) else "No"
    return edu.get("degree") if isinstance(edu, dict) else None


def _resolve_preferred_name(lbl: str, kind: str, p: dict) -> str:
    if p.get("preferred_name"):
        return p["preferred_name"]
    parts = _full_name_parts(p)
    return parts[0] if parts else ""


# ── Rule-table building blocks ────────────────────────────────────────────────
# Most rules are "label contains one of these substrings [and the field kind is
# one of these] -> a constant / a profile value". These factories keep the table
# terse; the handful of rules with real branching use an explicit lambda or a
# named ``_resolve_*`` helper above.

_MISSING = object()


def _kw(*words: str) -> Callable[[str, str, dict], bool]:
    """Matcher: any of ``words`` is a substring of the normalized label."""
    return lambda lbl, kind, p: any(w in lbl for w in words)


def _kw_kind(kinds: tuple, *words: str) -> Callable[[str, str, dict], bool]:
    """Matcher: ``_kw(*words)`` AND the field ``kind`` is one of ``kinds``."""
    return lambda lbl, kind, p: kind in kinds and any(w in lbl for w in words)


def _const(value: "str | None") -> Callable[[str, str, dict], "str | None"]:
    """Resolver: always return ``value``."""
    return lambda lbl, kind, p: value


def _pv(key: str, default: object = _MISSING) -> Callable[[str, str, dict], "str | None"]:
    """Resolver: ``profile.get(key)`` (with ``default`` when supplied)."""
    if default is _MISSING:
        return lambda lbl, kind, p: p.get(key)
    return lambda lbl, kind, p: p.get(key, default)


def _edu_field(key: str) -> Callable[[str, str, dict], "str | None"]:
    """Resolver: ``profile["education"][key]`` when education is a dict, else None."""
    return lambda lbl, kind, p: (
        p.get("education", {}).get(key) if isinstance(p.get("education"), dict) else None)


def _resolve_website(lbl: str, kind: str, p: dict) -> "str | None":
    return p.get("website_url") or p.get("portfolio_url") or p.get("linkedin_url")


def _resolve_headline(lbl: str, kind: str, p: dict) -> "str | None":
    return p.get("headline") or p.get("current_title")


def _resolve_summary(lbl: str, kind: str, p: dict) -> "str | None":
    return p.get("summary") or p.get("cover_letter_text")


def _latest_work_history_entry(p: dict) -> dict:
    """The applicant's current job, or their most recent one if none is marked
    current (T50) — the row an ATS "My Experience"/employment-history step reads
    from. ``work_history`` (see ``user_profile.json``) is expected ordered
    most-recent-first, but a ``current: True`` entry anywhere in the list still
    wins first (defensive against a hand-edited or out-of-order profile);
    otherwise the first entry is used. Returns ``{}`` when there is no usable
    work history so callers can ``.get(...)`` off it unconditionally.
    """
    history = p.get("work_history")
    if not isinstance(history, list) or not history:
        return {}
    for entry in history:
        if isinstance(entry, dict) and entry.get("current"):
            return entry
    first = history[0]
    return first if isinstance(first, dict) else {}


def _resolve_current_company(lbl: str, kind: str, p: dict) -> str:
    # T50: fall back to work_history's current/most-recent employer when the
    # legacy flat `current_company` / `employer` fields aren't set. The legacy
    # fields win when present so an explicit override still takes priority.
    return (p.get("current_company") or p.get("employer")
            or _latest_work_history_entry(p).get("employer") or "N/A")


def _resolve_work_history_start_date(lbl: str, kind: str, p: dict) -> "str | None":
    return _latest_work_history_entry(p).get("start_date") or None


def _resolve_work_history_end_date(lbl: str, kind: str, p: dict) -> str:
    entry = _latest_work_history_entry(p)
    if entry.get("current"):
        return "Present"
    return entry.get("end_date") or "Present"


def _resolve_currently_work_here(lbl: str, kind: str, p: dict) -> str:
    """"I currently work here" checkbox/select on an employment-history row.

    The deterministic checkbox filler (``_fill_field``) can only *check* a box,
    never uncheck one, so when the most recent ``work_history`` entry is NOT
    marked current we deliberately return ``""`` here: that skips filling (and
    the LLM fallback, since we already have a confident answer) and leaves the
    checkbox at its native default of unchecked — the correct state. Only the
    currently-employed case returns a truthy value so the filler checks it.
    """
    is_current = bool(_latest_work_history_entry(p).get("current"))
    if kind in ("select", "select-one"):
        return "Yes" if is_current else "No"
    return "on" if is_current else ""


def _resolve_job_type(lbl: str, kind: str, p: dict) -> str:
    return p.get("current_title") or "Software Engineer"


def _resolve_mailing_address(lbl: str, kind: str, p: dict) -> "str | None":
    return p.get("street_address") or p.get("location")


def _resolve_city_location(lbl: str, kind: str, p: dict) -> "str | None":
    return p.get("location") or p.get("city")


def _resolve_agree(lbl: str, kind: str, p: dict) -> str:
    return "Yes" if kind in ("select", "select-one") else "on"


# The ordered rule table. ORDER IS THE CONTRACT — it reproduces the original
# top-to-bottom `if` cascade exactly. A comment flags every entry whose *position*
# (not just its matcher) is load-bearing.
_PROFILE_VALUE_RULES: list[_ProfileRule] = [
    # Cover-letter fields never yield a resume path or any value — unconditional,
    # and MUST precede the resume rule ("cv"/"resume" tokens) and the later
    # cover-letter-text rule (now unreachable, kept for parity).
    _ProfileRule("cover_letter_guard",
                 _kw("cover letter", "cover_letter", "covering letter"),
                 _const(None)),
    _ProfileRule("resume",
                 lambda lbl, kind, p: kind == "file" or (
                     kind not in _SELECTISH_KINDS
                     and any(w in lbl for w in ("resume", "résumé", "cv", "upload your resume",
                                                "upload your résumé", "attach resume",
                                                "attach résumé", "attach cv"))),
                 _resolve_resume),
    # "first name" MUST precede "name" (substring) and "preferred name"
    # ("preferred first name" contains "first name" -> resolves to given name).
    _ProfileRule("first_name", _kw("first name", "given name"), _resolve_first_name),
    _ProfileRule("last_name", _kw("last name", "family name", "surname"), _resolve_last_name),
    _ProfileRule("email", _kw("email"), _pv("email")),
    # "phone country code" MUST precede "phone" and the generic "country" rule.
    _ProfileRule("phone_country_code",
                 _kw("phone country code", "country code", "country dial"),
                 _pv("country", "United States")),
    _ProfileRule("phone", _kw("phone", "mobile"), _pv("phone")),
    # LinkedIn/GitHub URL — excluded when the label is a "years of ..." question.
    _ProfileRule("linkedin_url",
                 lambda lbl, kind, p: "linkedin" in lbl and not _is_years_query(lbl),
                 _pv("linkedin_url")),
    _ProfileRule("github_url",
                 lambda lbl, kind, p: "github" in lbl and not _is_years_query(lbl),
                 _pv("github_url")),
    _ProfileRule("portfolio_url",
                 _kw("portfolio", "personal website", "personal site"),
                 _pv("portfolio_url")),
    _ProfileRule("website_url",
                 lambda lbl, kind, p: "website" in lbl and "personal" not in lbl,
                 _resolve_website),
    # T43: a combined "City, State" field wants the whole location line. MUST
    # precede the zip / street-address / state-of-residence rules (each would win
    # on ordering). The `_location` test is folded into the matcher so a profile
    # with no location string falls through instead of filling the field with "".
    _ProfileRule("city_state_combined",
                 lambda lbl, kind, p: (bool(re.search(r'\bcity\b', lbl))
                                       and bool(re.search(r'\bstate\b', lbl))
                                       and "relocat" not in lbl
                                       and bool((p.get("location") or "").strip())),
                 lambda lbl, kind, p: (p.get("location") or "").strip()),
    _ProfileRule("zip", _kw("zip", "postal"), _pv("zip_code")),
    _ProfileRule("street_address",
                 _kw("address line 1", "street address", "address 1", "street"),
                 _pv("street_address")),
    _ProfileRule("address_line_2",
                 _kw("address line 2", "address 2", "apt", "suite"),
                 _const("")),
    # State of residence. MUST precede the identity/country rules; the broad
    # `"state" in lbl` arm excludes work-authorization phrasings.
    _ProfileRule("state_of_residence",
                 lambda lbl, kind, p: any(w in lbl for w in (
                     "which state", "your state", "state of residence", "state you live",
                     "province", "state/province"))
                 or (("state" in lbl or "province" in lbl) and "united states" not in lbl
                     and "authorized" not in lbl and "visa" not in lbl),
                 _pv("state")),
    # Identity fields — MUST precede generic country/location to avoid cross-match.
    _ProfileRule("disability", _kw("disability", "disabled"), _pv("disability_status", "No")),
    _ProfileRule("gender", _kw("gender"), _pv("gender", "decline")),
    _ProfileRule("race", _kw("race", "ethnicity", "ethnic"), _pv("race", "decline")),
    _ProfileRule("veteran",
                 _kw("veteran", "military status", "protected veteran"),
                 _pv("veteran_status", "No")),
    # Right-to-work country picker — MUST fire before the generic "country" rule.
    _ProfileRule("right_to_work_country",
                 _kw("right to work", "verify right to work", "work from one of the following",
                     "currently based in and can verify"),
                 _pv("country", "United States")),
    _ProfileRule("us_based",
                 _kw("us based", "u.s. based", "united states based", "currently based in the us",
                     "currently based in the united states", "currently residing in the us",
                     "located in the us", "located in the united states"),
                 _const("Yes")),
    _ProfileRule("excluded_region_residency",
                 lambda lbl, kind, p: kind in _CHOICE_KINDS
                 and any(f"{v} {r}" in lbl for v in _RESIDE_VERBS for r in _EXCL_REGIONS),
                 _const("No")),
    _ProfileRule("relocation_yes_no",
                 _kw_kind(_CHOICE_KINDS, "open to relocat", "willing to relocat", "able to relocat",
                          "relocation assistance", "relocate to"),
                 _const("No")),
    _ProfileRule("rest_of_world", _kw("rest of world"), _const("")),
    _ProfileRule("country",
                 lambda lbl, kind, p: "country" in lbl and "relocat" not in lbl,
                 _pv("country", "United States")),
    # T41: \bcity\b boundary (not a bare substring, which also fires in
    # "capacity"). MUST run before the years-of-experience rules.
    _ProfileRule("city_location",
                 lambda lbl, kind, p: (bool(re.search(r'\bcity\b', lbl))
                                       or any(w in lbl for w in _LOC_PHRASES))
                 and "relocat" not in lbl,
                 _resolve_city_location),
    _ProfileRule("mailing_address",
                 lambda lbl, kind, p: any(w in lbl for w in (
                     "mailing address", "home address", "postal address", "billing address",
                     "current address", "your address")) or lbl.strip() == "address",
                 _resolve_mailing_address),
    _ProfileRule("middle_name", _kw("middle name", "middle initial"), _const("")),
    _ProfileRule("current_company",
                 lambda lbl, kind, p: any(w in lbl for w in (
                     "current company", "current employer", "current organization", "employer name",
                     "company name", "organization name", "most recent employer", "most recent company"))
                 or lbl in ("org", "company", "organization", "employer"),
                 _resolve_current_company),
    # "job title" is already covered above — current_title is the applicant's
    # current/most-recent title, kept in sync with work_history[0] by
    # user_profile.json / build_profile_interactively. No separate work-history
    # rule needed (T50 checked this explicitly before adding new rules).
    _ProfileRule("current_title",
                 _kw("current title", "job title", "current role", "current position"),
                 _pv("current_title")),
    # T50: employment-history date/checkbox fields on an ATS "My Experience"
    # step, sourced from work_history's current/most-recent entry
    # (_latest_work_history_entry). MUST precede the "start_date" job-offer rule
    # further down (~"earliest available" / "when can you start") so a bare
    # "Start Date" on an employment-history row resolves to a real past date
    # instead of the job-offer-availability constant "Immediately".
    #
    # "start date" / "from date" / "from" / "end date" / "to date" / "to" are
    # matched by EXACT label equality, not substring — deliberately, so a
    # qualified job-offer phrasing ("Desired Start Date", "Earliest Start
    # Date") does NOT match here (falls through to the job-offer rule instead,
    # which still substring-matches "start date") and an unrelated field that
    # merely contains "to date" ("Is your resume up to date?", "Achievements to
    # date") is never misread as an employment-record end date. The compound
    # "employment/job/position start|end date" phrases are unambiguous enough
    # to stay substring-matched.
    _ProfileRule("work_history_start_date",
                 lambda lbl, kind, p: (
                     lbl in ("start date", "from date", "from")
                     or any(w in lbl for w in ("employment start date", "job start date",
                                               "position start date"))
                 ),
                 _resolve_work_history_start_date),
    _ProfileRule("work_history_end_date",
                 lambda lbl, kind, p: (
                     lbl in ("end date", "to date", "to")
                     or any(w in lbl for w in ("employment end date", "job end date",
                                               "position end date"))
                 ),
                 _resolve_work_history_end_date),
    _ProfileRule("currently_work_here",
                 _kw_kind(_SELECTISH_KINDS, "currently work here", "currently working here",
                          "currently work in this role", "currently working in this role",
                          "currently employed here", "still work here", "still employed here",
                          "i currently work here"),
                 _resolve_currently_work_here),
    _ProfileRule("headline",
                 _kw("headline", "professional headline", "profile headline"),
                 _resolve_headline),
    _ProfileRule("summary",
                 _kw("summary", "professional summary", "about me", "bio"),
                 _resolve_summary),
    # T40: a *bare* overall "years of experience" question -> full tenure. A
    # phrasing that pins experience to a role/skill/domain the applicant has NOT
    # worked in ("...as a Lead", "...with COBOL") fails this matcher and drops to
    # the tiered rule below, which floors it.
    _ProfileRule("bare_years_experience",
                 lambda lbl, kind, p: _is_bare_years_experience(lbl)
                 and not _years_label_names_a_foreign_role_or_skill(lbl, p),
                 lambda lbl, kind, p: str(p.get("years_experience", ""))),
    # AI/ML stack keyword -> "1" floor. A "years of experience with <foreign AI
    # skill>" question is excluded here so it is floored in the tiered rule (T40).
    _ProfileRule("ai_skill_floor",
                 lambda lbl, kind, p: kind in ("text", "number")
                 and any(w in lbl for w in _AI_SKILL_KEYWORDS)
                 and not (_is_years_query(lbl)
                          and _years_label_names_a_foreign_role_or_skill(lbl, p)),
                 _const("1")),
    # T32 tiered "years of <specific skill>". MUST run after bare_years_experience
    # and ai_skill_floor; "relevant"/"total" are excluded (the total_experience
    # rule below owns those).
    _ProfileRule("years_of_skill_tiered",
                 lambda lbl, kind, p: (kind in ("text", "number")
                                       and any(w in lbl for w in (
                                           "how many years", "how many months", "years of",
                                           "years with", "yrs of experience", "yrs experience"))
                                       and not any(w in lbl for w in ("relevant", "total"))),
                 _resolve_years_of_skill),
    _ProfileRule("opt_stem",
                 _kw_kind(_CHOICE_KINDS, "opt or stem", "opt/stem", "stem opt", "currently on opt"),
                 _resolve_opt_stem),
    _ProfileRule("sponsorship",
                 _kw("sponsor", "sponsorship", "visa support", "work visa"),
                 _resolve_sponsor),
    _ProfileRule("authorized_to_work",
                 _kw("authorized to work", "legally authorized", "legal right to work", "eligible to work"),
                 _const("Yes")),
    _ProfileRule("ai_coding_tools",
                 _kw_kind(_CHOICE_KINDS, "ai coding", "ai code", "coding agent", "coding assistant",
                          "copilot", "cursor", "claude code"),
                 _const("Yes")),
    _ProfileRule("w2_employment",
                 _kw_kind(_CHOICE_KINDS, "willing to work on w2", "work on w2", "w2 employment",
                          "w2 contractor", "w2 basis"),
                 _const("Yes")),
    _ProfileRule("work_auth_expiry",
                 _kw("work authorization expire", "authorization expir", "visa expir"),
                 _pv("work_authorization_expiry", "N/A")),
    _ProfileRule("salary",
                 _kw("salary", "compensation", "expected pay", "desired pay"),
                 _resolve_salary),
    # Specific-degree completion — MUST precede the generic education rules
    # (which would answer "Yes" for any degree the user holds).
    _ProfileRule("specific_degree_completion",
                 lambda lbl, kind, p: kind in _CHOICE_KINDS and bool(re.search(
                     r'have you completed.{0,60}(doctor|ph\.?d|phd|master|bachelor|associate)', lbl)),
                 _resolve_specific_degree),
    _ProfileRule("highest_education",
                 _kw("highest level of education", "highest education", "education level",
                     "level of education"),
                 _resolve_highest_education),
    # "In which year did you complete your degree?" -> graduation year. MUST
    # precede the bare "degree" rule.
    _ProfileRule("graduation_year",
                 lambda lbl, kind, p: "year" in lbl and any(w in lbl for w in (
                     "degree", "master", "bachelor", "graduate", "graduated", "complet")),
                 _resolve_grad_year),
    _ProfileRule("degree", _kw("degree"), _resolve_degree),
    _ProfileRule("school",
                 _kw("school", "university", "college", "institution"),
                 _edu_field("school")),
    _ProfileRule("field_of_study",
                 _kw("field of study", "major", "area of study"),
                 _edu_field("field")),
    _ProfileRule("how_did_you_hear",
                 _kw("where did you hear", "how did you hear", "how did you find out",
                     "how did you learn about", "source of hire", "referral source",
                     "source of application", "how were you referred"),
                 _const("LinkedIn")),
    _ProfileRule("travel",
                 lambda lbl, kind, p: kind in _CHOICE_KINDS
                 and (any(w in lbl for w in ("willing to travel", "% travel", "travel requirement",
                                             "open to travel"))
                      or ("comfortable with" in lbl and "travel" in lbl)),
                 _pv("willing_to_travel", "No")),
    # Notice period / start timeline — MUST precede the generic "start date" rule.
    _ProfileRule("notice_period_timeline",
                 _kw_kind(_CHOICE_KINDS, "how quickly", "how soon", "when can you start",
                          "notice period", "earliest start", "available to start"),
                 _pv("notice_period", "Immediately")),
    _ProfileRule("commute_proximity",
                 _kw_kind(_CHOICE_KINDS, "commutable", "in-office attendance", "commuting distance",
                          "in commutable"),
                 _const("No")),
    # "start date" here is still a plain substring match (T50 did NOT remove
    # it) — the earlier work_history_start_date rule only claims an EXACT
    # "start date" / "from date" / "from" label, so a qualified phrasing like
    # "Desired Start Date" / "Earliest Start Date" doesn't match there and
    # falls through to substring-match "start date" here instead.
    _ProfileRule("start_date",
                 _kw("earliest available", "start date", "when can you start",
                     "available to start", "date available", "earliest start"),
                 _const("Immediately")),
    _ProfileRule("relative_works_here",
                 lambda lbl, kind, p: any(w in lbl for w in ("relative", "friend", "family member"))
                 and any(w in lbl for w in ("work for", "employed", "works at", "work at", "employee")),
                 _const("No")),
    _ProfileRule("secondary_employment",
                 _kw("secondary employment", "other employment", "work for another", "work elsewhere"),
                 _const("No")),
    _ProfileRule("applied_here_before",
                 _kw("applied here before", "applied to us before", "applied with us",
                     "previously applied", "filed an application", "application with"),
                 _const("No")),
    _ProfileRule("worked_here_before",
                 _kw("worked here before", "worked for us", "worked for this company",
                     "previously worked", "prior employment here", "former employee",
                     "previously employed by", "prior employment at"),
                 _const("No")),
    _ProfileRule("under_18_proof",
                 _kw("under 18", "proof of eligibility", "proof of age", "work permit"),
                 _const("N/A")),
    _ProfileRule("agree_consent",
                 _kw_kind(("select", "select-one", "checkbox"), "agree to", "i agree", "acknowledge",
                          "i understand", "consent to", "terms of service", "privacy policy",
                          "terms and conditions"),
                 _resolve_agree),
    _ProfileRule("comfortable_remote_commute_shift",
                 _kw("comfortable working", "comfortable with remote", "comfortable in a remote",
                     "ok for the remote", "remote engagement", "comfortable commuting",
                     "commuting to this job", "commute to this", "willing to commute",
                     "comfortable for", "shift hours", "est shift", "pst shift", "cst shift",
                     "mst shift", "work in our timezone", "work us hours", "work in us time"),
                 _const("Yes")),
    _ProfileRule("student_visa", _kw("student visa", "f-1 visa", "f1 visa"), _const("No")),
    _ProfileRule("background_check", _kw("background check"), _const("Yes")),
    _ProfileRule("drug_test", _kw("drug test"), _const("Yes")),
    _ProfileRule("contract_work",
                 _kw("contract work", "work a contract", "work contract", "contract position",
                     "contract role", "contract only", "corp-to-corp", "c2c", "1099"),
                 _const("No")),
    _ProfileRule("notice_period_days",
                 _kw("notice period", "notice days", "days notice"),
                 _const("14")),
    # "total" / "relevant" experience wants the full figure — but only as a
    # *bare* overall question ("total experience as a Lead" is still floored, T40).
    _ProfileRule("total_experience",
                 lambda lbl, kind, p: any(w in lbl for w in (
                     "total years", "total experience", "years of relevant", "relevant experience",
                     "total it experience"))
                 and not _years_label_names_a_foreign_role_or_skill(lbl, p),
                 lambda lbl, kind, p: str(p.get("years_experience", "4"))),
    _ProfileRule("ic_role_comfort",
                 _kw("individual contributor", "hands-on-keyboard", "hands on keyboard", "ic role",
                     "hands-on engineer"),
                 _const("Yes")),
    _ProfileRule("evaluation_frameworks",
                 _kw("evaluation framework", "llm-as-a-judge", "llm as a judge", "ai-as-a-judge",
                     "ai as a judge"),
                 _const("Yes")),
    _ProfileRule("deployed_to_production_count",
                 _kw_kind(("text", "number"), "deployed to production"),
                 _const("3")),
    _ProfileRule("ml_stack_yes_no",
                 _kw_kind(_CHOICE_KINDS, "building rag pipeline", "rag pipeline", "ml model",
                          "creating custom embedding", "designing and implementing ml"),
                 _const("Yes")),
    _ProfileRule("non_tech_domain",
                 _kw_kind(_CHOICE_KINDS, "insurance domain", "p&c insurance", "property & casualty",
                          "property and casualty", "healthcare domain", "financial domain",
                          "legal domain", "manufacturing domain", "retail domain"),
                 _const("No")),
    # Broad experience/skill "yes" rules — regex first (an interrupting product
    # name breaks the contiguous "do you have experience" substring).
    _ProfileRule("do_you_have_experience_regex",
                 lambda lbl, kind, p: kind in _CHOICE_KINDS
                 and bool(re.search(r'do you have .{0,50}(experience|expertise)', lbl)),
                 _const("Yes")),
    _ProfileRule("broad_experience_keywords",
                 _kw_kind(_CHOICE_KINDS, "hands-on experience", "have you built",
                          "professional experience with", "experience building", "experience using",
                          "experience developing", "experience implementing", "do you have experience",
                          "have you worked with", "have you used", "have you worked in",
                          "have you architected", "have you deployed", "have you designed",
                          "have you shipped", "have you developed", "have you led",
                          "early-stage startup", "high-ownership"),
                 _const("Yes")),
    # Unreachable (cover_letter_guard already returned None) — kept for parity.
    _ProfileRule("cover_letter_text",
                 _kw_kind(("text", "textarea"), "cover letter", "cover_letter", "covering letter"),
                 _const(None)),
    _ProfileRule("job_type_preference",
                 _kw("looking for", "job type preference", "what kind of job", "type of employment"),
                 _resolve_job_type),
    _ProfileRule("language",
                 _kw_kind(("text", "select", "select-one"), "language"),
                 _pv("preferred_language", "English")),
    _ProfileRule("preferred_name",
                 _kw("preferred name", "preferred first name", "nickname"),
                 _resolve_preferred_name),
    _ProfileRule("name_pronunciation",
                 _kw("pronunciation", "phonetic", "how to pronounce"),
                 _const("")),
    # A referral / "who referred you" field must stay blank — the applicant has
    # no referral. Position: BEFORE the broad "name" match below, which would
    # otherwise fill "…please add their name here" with the applicant's OWN name
    # (observed on real Easy Apply forms).
    # "referral source" / "how were you referred" are handled by how_did_you_hear
    # above (→ LinkedIn). Anything else mentioning a referral is a name-soliciting
    # field and must stay blank — never the applicant's own name.
    _ProfileRule("referral_name",
                 _kw("referred by", "referred you", "who referred", "person who referred",
                     "employee who referred", "referrer", "refer you to",
                     "name of the referrer", "referring employee", "referral"),
                 _const("")),
    # "name" is a broad substring — MUST be near the end (after first/last/
    # preferred/middle name, company name, etc.).
    _ProfileRule("full_name", _kw("name", "full name"), _pv("full_name")),
    _ProfileRule("unknown_social_url", _kw(*_UNKNOWN_SOCIAL), _const("")),
    # Any remaining url-typed field -> blank (MUST be last).
    _ProfileRule("url_kind_fallback",
                 lambda lbl, kind, p: kind == "url",
                 _const("")),
]


def _get_profile_value(profile: dict, label: str, kind: str = "text") -> str | None:
    """Map a form field label to a profile value. Returns None if no confident match.

    T47: a slim driver over the ordered ``_PROFILE_VALUE_RULES`` table. The
    normalization preamble below is unchanged; the former ~80-branch `if` cascade
    is now the table, iterated once (first matching rule wins).
    """
    # Collapse all whitespace (including embedded newlines from DOM textContent),
    # strip asterisk/required markers, then normalize spaces.
    l = label.lower().replace("_", " ")
    l = re.sub(r'\s+', ' ', l).strip()            # collapse newlines → single space first
    l = re.sub(r'\*?\s*required\s*$', '', l).strip()  # trailing "Required" / "* Required"
    l = re.sub(r'\*\s*required\b', '', l)          # mid-string "*Required"
    l = l.replace("*", "").replace("(required)", "").strip()
    p = profile

    for rule in _PROFILE_VALUE_RULES:
        if rule.matches(l, kind, p):
            return rule.resolve(l, kind, p)
    return None


# ── Shared helpers ─────────────────────────────────────────────────────────────

# Numeric / 1-N scale field detection for T31. If a *free-text* field is really
# asking for a number, an LLM prose answer ("I'd rate myself an 8 out of 10…")
# must be reduced to the bare integer before it is typed in.
_NUMERIC_LABEL_HINTS = (
    "how many years", "how many months", "number of years", "years of experience",
    "years experience", "(1-10)", "(1 to 10)", "1 to 10", "1-10", "1 - 10",
    "scale of 1", "on a scale", "rate your", "rate the", "how would you rate",
    "years of", "how many",
)

# Free-text / STAR-question cues. A label carrying one of these wants a written
# answer even if it also contains a numeric hint token ("How many times have you
# had to escalate a production issue? Describe one." — "how many" + "describe").
# Used by ``_ask_llm`` ONLY, to force the prose prompt; it must not feed
# ``_label_is_numeric`` (that would also disable ``_coerce_numeric_answer``'s
# safety net for the OffsiteApply / focused-field fill paths). T37 review.
_FREE_TEXT_LABEL_CUES = (
    "describe", "tell us", "tell me", "explain", "give an example",
    "give me an example", "walk us through", "walk me through", "why do you",
    "why are you", "share an experience", "share a time", "a time when",
    "a time you",
)


def _label_has_free_text_cue(label: str) -> bool:
    """Label explicitly asks for a written/STAR answer (see ``_FREE_TEXT_LABEL_CUES``)."""
    lab = re.sub(r"\s+", " ", (label or "").lower()).strip()
    return any(c in lab for c in _FREE_TEXT_LABEL_CUES)


def _label_is_numeric(label: str, kind: str = "text") -> bool:
    """Whether a form field is really asking for a bare number / 1-N scale value.

    Single source of truth shared by :func:`_coerce_numeric_answer` (its
    ``is_numeric`` gate) and :func:`_ask_llm` (its ``is_long_form`` exclusion) so
    the two detections can never drift apart again (T37 — a long-labelled
    "Rate your experience (1-10) …" field slipped past ``_ask_llm``'s old narrow
    hardcoded tuple, so it took the 2-4-sentence prose path *and* had coercion
    skipped by the ``if not is_long_form`` guard).

    Every select/radio/checkbox and every genuine long-form field
    (``textarea``/``contenteditable``) is excluded up front, so a real free-text
    prompt ("Describe your experience …", "Why do you want to work here?") is
    never misclassified as numeric. ``email``/``tel``/``url`` are excluded too —
    a phone or URL field is never a 1-N scale even if its label says "number".
    """
    if kind in ("select", "select-one", "select-multiple", "radio", "checkbox",
                "textarea", "contenteditable", "email", "tel", "url"):
        return False
    lab = re.sub(r"\s+", " ", (label or "").lower()).strip()
    return (
        kind == "number"
        or any(h in lab for h in _NUMERIC_LABEL_HINTS)
        or bool(re.search(r"\brate\b|\brating\b", lab))
    )


def _coerce_numeric_answer(label: str, answer: str, kind: str = "text",
                           profile: dict | None = None) -> str:
    """Reduce a prose answer to a bare integer when the field is numeric/scale (T31).

    Genuine free-text fields (and every select/radio/checkbox) pass through
    untouched. For a numeric field:

    * the first ``\\d+`` in the answer wins, clamped to a range stated in the
      label (e.g. ``(1-10)``, ``1 to 5``);
    * a label with an explicit range **or** a strong rating cue (``rate`` /
      ``rating`` / ``on a scale`` / ``scale of``) is a strong enough signal to
      grab the digit unconditionally — "Rate your Python proficiency" (no
      parenthetical) still reduces a verbose "…probably an 8 out of 10…" answer;
    * otherwise (a plain "how many …" / "years of …" label, no range, not
      ``type=number``) the digit is only trusted when the answer already looks
      like a number: ``<= 60`` chars, the digit is the first token, the digit is
      glued to a "years"/"months" unit, or the whole answer is just the number
      with surrounding punctuation. A stray year/count inside a genuine prose
      sentence ("…most memorably in 2021 when I…") is NOT grabbed (T37 review) —
      such answers fall through to the no-digit path.
    * no confident digit → a sensible fallback: the profile's ``years_experience``
      for a "years"/"months" question, the midpoint of a stated scale otherwise,
      and finally the answer unchanged.
    """
    if not answer or not isinstance(answer, str):
        return answer
    # Numeric-field detection (incl. the select/radio/checkbox/textarea/
    # contenteditable exclusion that keeps this helper safe to call
    # unconditionally) lives in the shared ``_label_is_numeric`` predicate so
    # ``_ask_llm``'s ``is_long_form`` exclusion can never drift from it (T37).
    if not _label_is_numeric(label, kind):
        return answer

    lab = re.sub(r"\s+", " ", (label or "").lower()).strip()
    s = answer.strip()
    # A STAR / "describe … / give an example / a time when …" cue in the label
    # means this is a written-answer field that merely *contains* a numeric hint
    # token ("how many times…", "how would you rate…"). Never digit-grab it — a
    # stray year/count in the prose ("…most memorably in 2021…") is not the
    # answer. ``_ask_llm`` routes these to the prose prompt for the same reason;
    # this guard covers the OffsiteApply / focused-field paths that call the
    # coercion directly. (T37 review.)
    if _label_has_free_text_cue(label):
        return answer
    rng = re.search(r"(\d+)\s*(?:-|–|to)\s*(\d+)", lab)
    lo, hi = (int(rng.group(1)), int(rng.group(2))) if rng else (None, None)
    # A rating label ("Rate your X", "on a scale, rate …") is as strong a signal
    # as an explicit (1-N) range — a range-less rating field is very common and
    # otherwise re-opens the T31 bug (T37 review). The free-text-cue short-circuit
    # above already ran, so "Describe a time you rated a peer …" stays prose.
    rating_label = bool(re.search(r"\brate\b|\brating\b|on a scale|scale of", lab))

    m = re.search(r"\d+", s)
    if m:
        n = int(m.group())
        if lo is not None and hi is not None and lo <= hi:
            # An explicit range in the label is a strong signal — grab the digit
            # unconditionally and clamp it.
            return str(max(lo, min(hi, n)))
        if rating_label:
            return str(n)
        # Plain "how many …" / "years of …" label, no range. Trust the digit only
        # when the answer already reads as a number, not a sentence that merely
        # contains one ("…took down 3 services for 40 minutes." → falls through).
        if (kind == "number"
                or len(s) <= 60
                or re.match(r"\s*\d", s)
                or re.search(r"\b\d+\s*\+?\s*(?:years?|yrs?|months?|mos?)\b", s.lower())
                or re.fullmatch(r"[\W_]*\d[\d\W_]*", s)):
            return str(n)

    # No digit in the answer (or no digit we trust).
    # An explicit "I don't have this" must stay 0 — don't bump a truthful
    # negative up to a fabricated number.
    if re.search(r"\b(none|no experience|no exp|never|n/?a|zero|not at all|no prior)\b",
                 s.lower()):
        return "0"
    # Otherwise fall back rather than type prose.
    if "year" in lab or "month" in lab:
        if profile is not None:
            yrs = str(profile.get("years_experience", "")).strip()
            _mm = re.search(r"\d+", yrs)
            if _mm:
                return _mm.group()
        return "1"
    if lo is not None and hi is not None and lo <= hi:
        return str((lo + hi) // 2)
    return answer


# ── Submission verification (shared by EasyApplyFlow + OffsiteApplyFlow) ────────

# Confirmation text — the strongest success signal, valid whether or not the page
# navigated. The OffsiteApply flow checks the full union against the whole
# post-submit page; EasyApply keeps its stricter historical subset
# (_EASYAPPLY_CONFIRM_PHRASES) because it reads the entire LinkedIn job-view DOM,
# which carries "you applied" chrome for *other* ("similar") jobs.
_SUBMIT_CONFIRM_PHRASES = (
    "your application was sent", "application submitted", "application was submitted",
    "successfully applied", "you've applied", "you applied", "application sent",
    "application was sent", "thank you for applying", "thank you for your application",
    "application received", "application is complete", "application complete",
    "successfully submitted", "has been submitted", "we received your application",
    "we'll be in touch", "we will be in touch", "you have applied",
    "submission received", "we received your",
)
_EASYAPPLY_CONFIRM_PHRASES = (
    "your application was sent", "application submitted", "application was submitted",
    "successfully applied", "you've applied", "you applied", "application sent",
    "application was sent",
)

# "You have already applied" — a prior application is on file. Retrying is
# pointless and the rest of the agent already treats this as applied.
_ALREADY_APPLIED_PHRASES = (
    "you have already applied", "already applied to this", "already submitted an application",
    "application already on file", "you already applied",
)

# Content substrings that mark a post-submit page as NOT a success.
_SUBMIT_FAIL_PHRASES = (
    "sign in", "log in", "login", "create an account", "create account", "register",
    "something went wrong", "page not found", "404 error", "session expired",
    "please try again", "an error occurred", "no longer accepting applications",
)

# A post-submit redirect whose URL contains one of these is a failure.
_SUBMIT_FAIL_URL_WORDS = ("login", "signin", "sign-in", "register", "/error", "404", "/auth")
# Path segments (matched against urlparse(url).path, NOT the whole URL — bare
# substrings gave false positives: "sent" in /consent, "complete" in
# /application-incomplete, "confirm" in /confirm-email) that confirm success.
_SUBMIT_SUCCESS_PATH_SEGMENTS = (
    "/confirmation", "/success", "/thank", "/applied", "/application-complete",
    "/application-received", "/submitted", "/done",
)
# Landing paths that mean the ATS bounced us back to a listing / careers / home
# page. For Rippling, Greenhouse, Ashby and Lever that redirect can BE the
# success signal (T33 — was a false negative), but only with corroboration:
# a submit was actually clicked AND there is no error banner AND no application
# submit button remains.
_ATS_LISTING_PATH_HINTS = (
    "/jobs", "/careers", "/opportunities", "/openings", "/positions", "/postings", "/board",
)

_ERROR_CONTAINER_SELECTORS = (
    '[role="alert"]', '.error', '.field-error', '.field_error', '.form-error',
    '.errors', '.error-message', '.alert-danger', '.alert-error', '.usa-alert--error',
)
# Text that identifies a *form submit* CTA (not a job-board "Apply" link).
_SUBMIT_CTA_TEXTS = (
    "submit application", "submit your application", "submit my application",
    "submit this application", "complete application", "complete your application",
    "send application", "finish application", "review your application",
)


def _registrable_host(netloc: str) -> str | None:
    """Best-effort eTLD+1 for the "same ATS?" check in submission verification.

    Deliberately partial — this list only covers suffixes we actually see. A
    host it cannot confidently reduce returns ``None``, and the caller MUST
    treat ``None`` as "different site" (fail closed), because this function only
    ever gates a *success* verdict.
    """
    host = (netloc or "").split(":")[0].lower().strip(".")
    if not host or host.replace(".", "").isdigit():   # empty or bare IP
        return None
    parts = host.split(".")
    if len(parts) < 2:
        return None
    # Two-label public suffixes (partial): ccTLD second levels + platform
    # pseudo-suffixes where every subdomain is a different owner.
    _two_label_suffixes = {
        "co.uk", "org.uk", "ac.uk", "gov.uk", "com.au", "co.nz", "co.jp",
        "com.br", "co.in", "com.sg", "co.za", "com.mx", "co.il", "co.kr",
        "com.cn",
        "github.io", "vercel.app", "netlify.app", "pages.dev", "web.app",
        "workers.dev", "onrender.com", "herokuapp.com",
    }
    if len(parts) >= 3 and ".".join(parts[-2:]) in _two_label_suffixes:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


async def _page_has_error_banner(page: Page) -> bool:
    """True if the page shows a visible, non-empty error container."""
    for sel in _ERROR_CONTAINER_SELECTORS:
        try:
            loc = page.locator(sel)
            for i in range(min(await loc.count(), 5)):
                el = loc.nth(i)
                if await el.is_visible() and (await el.inner_text()).strip():
                    return True
        except Exception:
            continue
    return False


async def _page_has_active_submit_cta(page: Page) -> bool:
    """True if a visible, enabled *application submit* button is still on the page
    (i.e. we are still on / bounced back to the form, not a confirmation)."""
    try:
        loc = page.locator(
            'button:not([disabled]), input[type="submit"]:not([disabled]), [role="button"]'
        )
        for i in range(min(await loc.count(), 40)):
            el = loc.nth(i)
            try:
                if not await el.is_visible():
                    continue
                txt = ((await el.inner_text()) or "").strip().lower()
            except Exception:
                continue
            if any(c in txt for c in _SUBMIT_CTA_TEXTS):
                return True
    except Exception:
        return True   # can't tell → assume still on the form → fail closed
    return False


async def verify_submission(
    page: Page,
    *,
    url_before: str | None = None,
    modal_open: bool | None = None,
    submit_attempted: bool = True,
    confirm_phrases: tuple[str, ...] | None = None,
) -> tuple[bool, str]:
    """Decide whether a just-submitted application actually went through.

    Shared by ``EasyApplyFlow`` (pass ``modal_open`` from ``_is_modal_open()``,
    leave ``url_before`` unset — Easy Apply never navigates) and
    ``OffsiteApplyFlow`` (pass ``url_before``; ``modal_open`` stays ``None``;
    pass ``submit_attempted=False`` when the LLM claimed "done" without a submit
    click actually having happened).

    Returns ``(success, signal)`` — ``signal`` names which rule fired so the
    caller can log it. Strongly conservative: a wrong ``True`` here is
    unrecoverable (``applied=1`` is not in the ``--reset-failed`` pool), so every
    ambiguous end-state returns ``False`` and the job is retried.
    """
    _confirm = confirm_phrases or _SUBMIT_CONFIRM_PHRASES
    try:
        content = (await page.content()).lower()
    except Exception as exc:
        return False, f"verification error: could not read page ({exc})"

    # 1. Confirmation text — strongest signal, independent of navigation.
    for phrase in _confirm:
        if phrase in content:
            return True, f"confirmation text {phrase!r}"
    # "Already applied" is a success state offsite (an application is on file).
    # Skipped for Easy Apply: LinkedIn's job-view DOM carries this text for other
    # ("similar") jobs, and EasyApplyFlow already detects it pre-submit.
    if modal_open is None:
        for phrase in _ALREADY_APPLIED_PHRASES:
            if phrase in content:
                return True, f"already-applied text {phrase!r} — an application is on file"

    # 2. Easy Apply modal semantics (no URL navigation inside the modal).
    if modal_open is False:
        return True, "modal closed after submit"
    # The green "Applied" pill is a LinkedIn Easy Apply signal ONLY. On a company
    # careers page "Applied" matches Applied Materials / Applied Intuition / an
    # "Applied" filter chip — never trust it off LinkedIn.
    if modal_open is not None:
        try:
            for sel in ('[aria-label*="Applied"]', 'button:has-text("Applied")'):
                if await page.locator(sel).count() > 0:
                    return True, "page shows the Easy Apply 'Applied' indicator"
        except Exception:
            pass

    # 3. URL-change analysis (OffsiteApply).
    try:
        url_after = page.url or ""
    except Exception:
        url_after = ""
    if url_before is not None and url_after and url_after != url_before:
        new_url = url_after.lower()
        before_l = url_before.lower()
        _after_path_only = urlparse(new_url).path

        if any(k in new_url for k in _SUBMIT_FAIL_URL_WORDS):
            return False, f"redirected to an auth/error URL -> {url_after[:100]}"
        if any(p in content for p in _SUBMIT_FAIL_PHRASES):
            return False, f"post-submit page shows a failure signal -> {url_after[:100]}"
        if await _page_has_error_banner(page):
            return False, f"post-submit page shows an error banner -> {url_after[:100]}"
        if any(seg in _after_path_only for seg in _SUBMIT_SUCCESS_PATH_SEGMENTS):
            return True, f"success URL path -> {url_after[:100]}"

        # Not normalised-equal → a real navigation happened.
        if new_url.rstrip("/") != before_l.rstrip("/"):
            _before_host = urlparse(before_l).netloc
            _after_host = urlparse(new_url).netloc
            _after_path = _after_path_only.rstrip("/")
            _before_path = urlparse(before_l).path.rstrip("/")
            _rh_before = _registrable_host(_before_host)
            _rh_after = _registrable_host(_after_host)
            _same_site = _rh_before is not None and _rh_before == _rh_after
            _looks_like_listing = (
                _after_path in ("", "/")
                or any(h in _after_path for h in _ATS_LISTING_PATH_HINTS)
                # Navigated "up" to an ancestor of the form path (e.g. the form
                # was …/jobs/<id>/apply and we landed on …/jobs or …/<company>).
                or (bool(_after_path) and _before_path.startswith(_after_path))
            )
            _left_the_form = _after_path != _before_path and len(_after_path) <= len(_before_path)
            # "Redirected back to a listing" is success ONLY with corroboration:
            # a submit was actually clicked, no error banner (checked above), and
            # no application submit button still on the page. A bare nav to a
            # listing (stray LLM nav, premature "done") is NOT success.
            if _same_site and _looks_like_listing and _left_the_form:
                if not submit_attempted:
                    return False, (
                        f"navigated to a listing page ({_after_host}) but no submit "
                        f"was ever clicked -> not a submission"
                    )
                if await _page_has_active_submit_cta(page):
                    return False, (
                        f"landed on {_after_host} but an application submit button is "
                        f"still present -> not submitted"
                    )
                return True, (
                    f"submit clicked, then the ATS redirected to its listing/careers "
                    f"page on {_after_host} with no error -> success"
                )
            return False, f"URL changed to an ambiguous destination -> {url_after[:100]}"

    # 4. Same page (or normalised-equal) — look for validation errors.
    error_clues = [p for p in ("required", "please complete", "please fill", "missing",
                               "invalid email", "invalid phone") if p in content]
    if error_clues:
        return False, f"form still showing validation errors: {error_clues[:2]}"
    if await _page_has_error_banner(page):
        return False, "form still showing an error banner after submit"

    if modal_open is True:
        return False, "modal still open after submit — form may not have submitted"
    return False, "no confirmation signal found after submit"


async def _ask_llm(model: str, profile: dict, field: dict) -> str | None:
    """Fill one form field via a one-shot ``llm.query`` call (self-contained
    prompt — profile + field descriptor). ``model`` is the guided_apply model."""
    label = field.get("label", "")
    # Fall back to field name/id as label hint when label is missing
    if not label:
        label = field.get("name", "") or field.get("id", "")
    if not label:
        return None
    options = field.get("options", [])
    kind = field.get("kind", "text")
    # A numeric / 1-N-scale field (incl. long-labelled "Rate your experience
    # (1-10) …" prompts) expects a bare number, not a sentence — use the same
    # predicate ``_coerce_numeric_answer`` gates on so the two stay in sync (T37).
    _is_numeric_question = _label_is_numeric(label, kind)
    # …but an explicit "describe / give an example / a time when …" cue always
    # wins: a STAR question that merely contains "how many" or "rate" must still
    # get the 2-4-sentence prose prompt, never digit-coercion (T37 review).
    _is_free_text_question = _label_has_free_text_cue(label)
    # Select/radio with options is never long-form regardless of label length
    _is_choice = kind in ("select", "select-one", "select-multiple", "radio") or bool(options)
    _len_or_area = kind in ("textarea", "contenteditable") or len(label) > 60
    is_long_form = not _is_choice and (
        _is_free_text_question
        or (_len_or_area and not _is_numeric_question)
    )
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
            "For a 'years of <skill>' or 'how many years' question, reply with just a number "
            "and never 0 unless the profile clearly shows no experience with that skill — "
            "give a reasonable non-zero figure that does not exceed the applicant's overall "
            "years of experience. "
            "CRITICAL: Never fabricate URLs, social media handles, usernames, or specific data not in the profile. "
            "For URL/link fields (Twitter, Instagram, Facebook, personal blog, etc.) not explicitly in the profile, reply with an empty string. "
            "If the profile has no relevant info for a non-select/non-radio non-numeric field, reply with an empty string. "
            "Reply with ONLY the answer value, nothing else."
        )
    _t = 70 if is_long_form else 50
    try:
        raw_answer = await llm.query(prompt, model=model, timeout=_t)
        answer = raw_answer.strip().strip('"').strip("'")
        # T31: a numeric / 1-N-scale free-text field must get a bare integer,
        # never the model's prose ("I'd rate my experience an 8 out of 10…").
        if not is_long_form:
            answer = _coerce_numeric_answer(label, answer, kind, profile)
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
    except Exception as _exc:
        print(f"  [LLM error] Field '{label}' — {type(_exc).__name__}: {_exc}")
        _write_llm_log({
            "ts":        datetime.now(timezone.utc).isoformat(),
            "type":      "field_fill_error",
            "model":     model,
            "field_label": label,
            "error":     f"{type(_exc).__name__}: {_exc}",
        })
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
        auto_mode: bool,
        callbacks: dict,
        model: str = "",
        verbose: bool = False,
    ):
        self.page = page
        self.profile = profile
        # Browser-agent LLM: the Claude Agent SDK on subscription auth (T14b).
        # Every LLM call here is a one-shot ``llm.query`` on this model.
        self.model = model or config.get_llm_config("guided_apply").model
        self.auto_mode = auto_mode
        self.callbacks = callbacks
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
                if (kind == "radio" and not current_val) or is_unchecked_checkbox:
                    pass  # fall through to re-fill
                else:
                    continue
            value = _get_profile_value(self.profile, label, kind)
            # If _get_profile_value returned a value that doesn't match any available select option,
            # discard it and let the LLM decide — prevents numeric years ("4") being used for Yes/No selects.
            if value is not None and kind in ("select", "select-one", "select-multiple"):
                field_opts = field.get("options", [])
                if field_opts:
                    opts_lower = [o.lower() for o in field_opts]
                    if value.lower() not in opts_lower:
                        value = None
            if value is None:
                value = await _ask_llm(self.model, self.profile, field)
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
        """Check whether LinkedIn accepted the Easy Apply submission.

        Thin wrapper over the shared ``verify_submission`` helper — Easy Apply
        never navigates, so the only extra signal it contributes is whether the
        modal is still open.
        """
        try:
            modal_open = await self._is_modal_open()
        except Exception:
            modal_open = None
        return await verify_submission(
            self.page, modal_open=modal_open,
            confirm_phrases=_EASYAPPLY_CONFIRM_PHRASES,
        )


# ── OffsiteApplyFlow ───────────────────────────────────────────────────────────

class _StepState:
    """Mutable per-step context handed to :meth:`OffsiteApplyFlow._execute_action`.

    * ``page`` — the live tab; ``_execute_action`` rebinds it when a non-submit
      click opens a new tab, and the orchestrator loop must pick that up.
    * ``selector`` — the action's CSS selector; the ``select`` branch normalises a
      ``#<digit-id>`` to ``[id="…"]`` in place, and the loop's history entry uses
      the normalised form (matches the pre-refactor inline behaviour).
    * ``forced_filled`` — the shared dict of fields to treat as already filled
      (jQuery-UI / React-Select commit to a hidden input and clear the visible
      one); the same object the orchestrator passes to ``_decide_action``.
    * ``submit_clicked`` — latches True once a real submit button is clicked;
      ``verify_submission`` uses it to tell a real post-submit redirect from a
      stray navigation.
    * ``click_hit_target`` — set by ``_execute_action``'s ``click`` branch when
      the click actually resolved a target that was *not* a plain nav link
      (T45). The orchestrator reads it to decide whether a ``click`` counts as
      engaging the form (``_form_engaged``): a not-found click or a bare-``<a>``
      nav-link click ("Apply" / "Working with us" on a careers SPA) must not.
    """
    __slots__ = ("page", "selector", "forced_filled", "submit_clicked", "click_hit_target")

    def __init__(self, page, selector, forced_filled, submit_clicked):
        self.page = page
        self.selector = selector
        self.forced_filled = forced_filled
        self.submit_clicked = submit_clicked
        self.click_hit_target = False


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
        auto_mode: bool,
        callbacks: dict,
        generated_password: str,
        model: str = "",
        company_name: str = "",
        job_title: str = "",
        job_description: str = "",
        verbose: bool = False,
        inbox=None,
        application_url: str = "",
    ):
        self.page = page
        self.context = context
        self.profile = profile
        # Browser-agent LLM: the Claude Agent SDK on subscription auth (T14b).
        # Every LLM call here is a one-shot ``llm.query`` on this model.
        self.model = model or config.get_llm_config("guided_apply").model
        self.application_url = application_url
        self.auto_mode = auto_mode
        self.callbacks = callbacks
        self.generated_password = generated_password
        self.company_name = company_name
        self.job_title = job_title
        self.job_description = job_description
        self.verbose = verbose
        self.inbox = inbox
        self.unanswered_fields: list[str] = []
        # Reset at the top of each _llm_guided_apply run; declared here so the
        # auth seams are safe to call standalone (tests, _fill_external_form).
        self._auth_attempted = False
        # T36: latched True when a Chromium tab/renderer crash is caught mid-apply.
        # run_session reads it to rebuild the shared browser page before the next
        # job (a swallowed crash otherwise poisons every subsequent job).
        self._browser_crashed = False

    _EXPIRED_PHRASES = [
        "No longer accepting applications",
        "This job is no longer accepting applications",
        "Applications are closed",
    ]

    async def assist_from_page(self) -> str:
        """Resume LLM-guided apply from the application form tab.

        Prefers an already-open non-LinkedIn tab (the one the failed apply left
        behind) over the main LinkedIn page.  Falls back to self.page if no
        third-party tab is found.
        """
        target = self.page
        for pg in self.context.pages:
            try:
                if not pg.is_closed() and "linkedin.com" not in pg.url and pg.url not in ("", "about:blank"):
                    target = pg
                    break
            except Exception:
                continue
        print(f"  [Retry] Resuming from: {target.url}")
        return await self._llm_guided_apply(target)

    async def run(self, job_url: str) -> str:
        # Fast path: navigate directly to the ATS application URL from DB,
        # skipping LinkedIn entirely (avoids Premium wall / Apply button click)
        if self.application_url and "linkedin.com" not in self.application_url:
            print(f"  [Offsite] Direct ATS navigation: {self.application_url[:80]}")
            try:
                await self.page.goto(self.application_url, wait_until="domcontentloaded", timeout=20000)
            except Exception as _e:
                print(f"  [Offsite] Direct nav failed ({_e}) — falling back to LinkedIn click")
            else:
                if "linkedin.com" not in self.page.url:
                    return await self._fill_external_form(self.page)

        # Fallback: open LinkedIn job page and click Apply
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
            return apply_status

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

        # Final fallback: navigate directly to application_url from DB (bypasses LinkedIn wall)
        if self.application_url and "linkedin.com" not in self.application_url:
            print(f"  [Offsite] Navigating directly to application_url: {self.application_url[:80]}")
            try:
                await self.page.goto(self.application_url, wait_until="domcontentloaded", timeout=20000)
                if "linkedin.com" not in self.page.url:
                    return self.page, "ok"
            except Exception as _e:
                print(f"  [Offsite] Direct application_url navigation failed: {_e}")

        print(f"  [Offsite] Apply click didn't navigate — still on {self.page.url} — marking failed")
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

                // Label resolver — prefers <label for=id>, then aria-label, then placeholder,
                // then name, then data-automation-id (T51 — Workday and other ATSes that
                // identify controls primarily via data-automation-id rather than a visible
                // <label>/aria-label/name would otherwise surface as unlabeled [EMPTY] fields
                // the LLM step loop can't act on meaningfully).
                const lbl = (el) => {
                    const forEl = el.id ? document.querySelector('label[for="' + el.id + '"]') : null;
                    return (forEl && forEl.textContent.trim())
                        || el.getAttribute('aria-label')
                        || el.getAttribute('placeholder')
                        || el.getAttribute('name')
                        || el.getAttribute('data-automation-id')
                        || '';
                };

                // Form fields (inputs, selects, textareas) — include value regardless of viewport
                const fields = Array.from(document.querySelectorAll(
                    'input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=reset]),' +
                    'select, textarea'
                ))
                .filter(el => !el.disabled)
                // Skip OneTrust cookie-consent inputs — they're always offscreen and irrelevant
                .filter(el => !(el.id && el.id.startsWith('ot-')) && !el.closest('#onetrust-consent-sdk'))
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
                    // T51: a data-automation-id-only control (no id/name/aria-label/placeholder)
                    // must still be included — otherwise it's dropped before lbl() ever runs.
                    const hasAutomationId = !!el.getAttribute('data-automation-id');
                    return hasId || hasName || hasLabel || hasAutomationId;
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
                    'button:not([disabled]), [role="button"], [role="tab"], a[href]'
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
        except Exception as exc:
            # T36: a renderer crash here would otherwise look like a blank SPA and
            # the step loop would return "expired" (-1). Let it propagate so the
            # crash is handled as a retryable failure and the page is rebuilt.
            if _is_browser_crash(exc):
                raise
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
            "If you see a tab or link labeled 'Application' or 'Apply' in the buttons list, click it immediately — it opens the application form. "
            "click=button or link (use :has-text() NOT :contains()). fill=empty text input. "
            "select=native <select> element ONLY (tag is SELECT in HTML). upload=resume file input. scroll=reveal more. "
            "Priority: Fill ALL [EMPTY] fields in top-to-bottom order BEFORE clicking any Submit/Apply button. "
            "Always act on the first [EMPTY] field in the list above — do not skip ahead to offscreen fields. "
            "NEVER fill a [FILLED] field — it already has the correct value, skip it. "
            "Never re-fill a field already in 'Actions taken so far'. Never Cancel/Sign-out. "
            "Never fill or upload to any field labeled 'Cover Letter' or 'Covering Letter' — skip entirely. "
            f"Sponsorship questions: answer '{_sponsor_val}'. Work authorization: always 'Yes'. "
            "For a 'years of <skill>' or 'how many years' field, fill just a number — never 0 "
            "unless the profile clearly shows no experience with that skill; give a reasonable "
            "non-zero figure that does not exceed the applicant's overall years of experience "
            "(yrs=...). "
            "CRITICAL: Never fabricate URLs, social media handles, usernames, or any information not in the profile. "
            "For any field where you have no value (optional URL, referral email, social handle, portfolio, a 'who referred you' / 'referred by' / referral name field, etc.) — do NOT issue a fill action at all. Skip that field entirely and move to the next [EMPTY] field or click Submit. Never put the applicant's own name in a referral field. Never fill a field with an empty string (value='') — an empty fill does nothing useful and can trigger browser validation errors. "
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
                raw = await llm.query(prompt, model=self.model, timeout=120)
                _call_ms = int((datetime.now(timezone.utc) - _call_start).total_seconds() * 1000)
                # Strip markdown fences if present, then isolate the JSON object
                clean = strip_code_fence(raw)
                # NOTE: `start` indexes the pre-extraction string; extract_json_object
                # then trims to [first '{' .. last '}'], so clean[start:first_end]
                # in the fallback below still points at the first object.
                start = clean.find("{")
                clean = extract_json_object(clean)
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
                _exc_l = str(exc).lower()
                # llm.ClaudeAgentSDKError surfaces rate/usage limits as typed
                # errors now (the old subprocess helper string-matched stdout).
                if ("RateLimit" in exc_name or "429" in _exc_l
                        or "rate limit" in _exc_l or "usage limit" in _exc_l):
                    wait = 20 * (_attempt + 1)
                    print(f"  [LLM] Rate/usage limited — waiting {wait}s before retry {_attempt + 1}/3")
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
            return await llm.query(prompt, model=self.model, timeout=30)
        except Exception:
            return f"Role: {self.job_title} at {self.company_name}."

    async def _detect_bot_wall(self, page: Page) -> str:
        """Return a short reason string if the page shows a bot wall this agent
        cannot get past, else ''.

        * a reCAPTCHA / hCaptcha widget — unsolvable in any mode, any host;
        * a Greenhouse text "security code" challenge — only when the page is
          actually on ``greenhouse.io`` AND we're in ``--auto`` (no human to
          type it). The host gate matters: ``_GREENHOUSE_SECURITY_SIGNALS``
          ("verification code", "enter the code", …) overlaps the *legitimate*
          email-verification step of account-creation offsite flows, which
          ``run_session`` supports on purpose via ``self.inbox``. In interactive
          mode the step loop pauses for the user instead, so this returns ''.

        Cheap enough (one ``locator().count()`` + one ``innerText`` slice) to run
        before the ``_summarize_job`` LLM call so a walled job is skipped without
        burning it.
        """
        try:
            if await page.locator(_CAPTCHA_WIDGET_SELECTOR).count() > 0:
                return "reCAPTCHA widget"
        except Exception:
            pass
        if self.auto_mode and "greenhouse.io" in urlparse(page.url).netloc.lower():
            try:
                _txt = (await page.evaluate(
                    "() => (document.body.innerText || '').slice(0, 800)"
                )).lower()
                if any(s in _txt for s in _GREENHOUSE_SECURITY_SIGNALS):
                    return "Greenhouse security-code challenge (no human in --auto)"
            except Exception:
                pass
        return ""

    # ── Static config for the step loop / seam helpers ────────────────────────
    # Hoisted out of _llm_guided_apply so the extracted seams
    # (_classify_domain, _handle_auth, _detect_terminal_state) share one copy.

    # URL path prefixes that indicate a login / registration gate.
    _LOGIN_PATHS = (
        "/login", "/signin", "/sign-in", "/auth/login",
        "/dashboard/login", "/register", "/create-account", "/join",
    )
    # Domains that are dead ends regardless of path (SSO login walls, enterprise portals).
    _DEAD_END_DOMAINS = ("my.greenhouse.io",)
    # Workday career-site domains (T51). No longer in _BLOCKED_AUTO_APPLY_DOMAINS —
    # gates the Workday-specific auth selectors (_try_login/_try_register) and the
    # "prefer Autofill with Resume over Apply Manually" nudge (_prefer_workday_autofill).
    _WORKDAY_DOMAINS = ("myworkdayjobs.com", "myworkdaysite.com")
    # Reasons in an LLM "failed" action that mean an unbeatable verification wall → skip.
    _UNBLOCKABLE_WALL_KEYWORDS = ("persona", "identity", "verif", "captcha", "recaptcha")
    # SSO / IdP hosts — handed to _handle_sso_page instead of the LLM loop.
    _SSO_DOMAINS = (
        "login.microsoftonline.com",   # Microsoft OIDC / SAML
        "accounts.google.com",          # Google OAuth
        "login.okta.com",               # Okta hosted login
        "auth0.com",                    # Auth0
        "onelogin.com",                 # OneLogin
        "pingidentity.com",             # Ping Identity
        "shibboleth",                   # Shibboleth (many academic/enterprise)
    )
    _CLOUDFLARE_SIGNALS = (
        "performing security verification", "security service to protect",
        "enable javascript and cookies", "ray id:",
    )
    _EXPIRED_URL_PATTERNS = (
        "/second-chance", "/job-expired", "/job-not-found", "/404", "/error",
        "ns_inactive_job=1", "inactive_job",
    )
    _EXPIRED_TEXT_PHRASES = (
        "no longer available", "opportunity is no longer", "position has been filled",
        "listing expired", "job has been filled", "this job is no longer", "job is closed",
        "no longer accepting applications", "job not found", "error: job not found",
        "this position has been", "this role has been filled",
        "no longer active", "posting is no longer", "job posting has expired",
        "this posting has been removed", "job has expired",
    )
    # Job aggregators / contractor-only / broken sites — not real employer apply flows, skip permanently.
    _SPAM_DOMAINS = (
        "jobright.ai",                  # aggregator — requires Jobright account to apply
        "sundayy.com",                  # aggregator — requires account
        "scale.jobs",                   # aggregator — requires account
        "dice.com",                     # aggregator — OAuth redirects to wrong page
        "mercor.com",                   # aggregator — behind account login
        "remotehunter.com",             # aggregator — "Apply" navigates to homepage login wall
        "haystack.cv",                  # aggregator — behind account login wall
        "talentally.com",               # aggregator — pre-registered account only
        "micro1.ai",                    # aggregator
        "tenex.ai",                     # aggregator
        "bestjobtool.com",              # aggregator — /job-description-usb/ path; script engine fails
        "fetchjobs.co",                 # aggregator — /job-description-usb/ path; React EEO timeouts
        "alignerr.com",                 # contractor — Google/LinkedIn OAuth only, no email registration
        "app.dataannotation.tech",      # contractor — /worker_signup is registration, not a job apply form
        "peakperformers.org",           # broken — apply form not on page, LLM hallucinates nav selector
        "sourcehire.app",               # broken — "Apply to this role" button does not navigate
        "jobs.gainwelltechnologies.com", # broken — portal loops back to job search, no direct apply form
    )
    # Real listings on ATS platforms / employer portals where auto-apply is blocked (CAPTCHA, account
    # wall, chatbot, invisible SPA modal). Marked blocked (-3) so a human picks them up; no auto-retry.
    # Checked at domain level so Workday's /apply/applyManually (not a login URL) is caught too.
    _BLOCKED_AUTO_APPLY_DOMAINS = (
        # reCAPTCHA / technical blockers the agent cannot overcome
        "governmentjobs.com",           # NEOGOV — account + CAPTCHA required
        "zohorecruit.com",              # Zoho Recruit — CAPTCHA blocks submission
        "applytojob.com",               # ApplyToJob — reCAPTCHA on landing form
        "hirebridge.com",               # HireBridge — hidden inputs + reCAPTCHA
        "hackajob.com",                 # hackajob — email gate + reCAPTCHA
        "jobs.twilio.com",              # Twilio — hidden g-recaptcha-response
        "burtchworks.com",              # Burtch Works — React form fills don't persist
        "jobs.cvshealth.com",           # CVS Health Phenom chatbot — navigation fails
        "amazon.jobs",                  # Amazon portal — duplicate invisible fields
        # Company career pages backed by reCAPTCHA (Greenhouse)
        "careers.airbnb.com",
        "www.pinterestcareers.com",
        # Company sites requiring account login
        "apply.careers.microsoft.com",  # Requires Microsoft account
        "ycombinator.com",              # YC Work — SSO only
        # ATS platforms with invisible SPA login modals (Apply opens overlay Playwright can't inspect)
        # NOTE: Workday (myworkdayjobs.com / myworkdaysite.com) was here until T51 — see
        # _WORKDAY_DOMAINS above for the Autofill-with-Resume + correction-loop approach
        # that replaced the blanket block.
        "ultipro.com",                              # UltiPro/UKG
        "bamboohr.com",                             # BambooHR
        "icims.com",                                # iCIMS
        "jibeapply.com",                            # Jibe/Jobvite
        "taleo.net",                                # Oracle Taleo
        "paycomonline.net",                         # Paycom
        "recruitingbypaycor.com",                   # Paycor
        "yourpayrollhr.com",                        # Paycor-based
        "oraclecloud.com",                          # Oracle HCM
        "jobvite.com",                              # Jobvite
        "recruiting.paylocity.com",                 # Paylocity
        "etscareers.submit4jobs.com",               # ETS careers
        # Career pages / ATSes where Apply form is inaccessible headlessly
        "talent.fullstack.com",                     # FullStack — invisible modal
        "careers-page.com",                         # Careers Page ATS
        "careers.bigbear.ai",                       # BigBear.ai — only Search inputs visible
        "careers.rideuta.com",                      # Utah Transit Authority — no Apply button
        "entertimeonline.com",                      # EnterTime ATS — only Search field
        "butterflymx.com",                          # ButterflyMX — Ashby embed, only Search visible
        "www.seismic.com",                          # Seismic — embedded form, URL never changes
        "hiringthing.com",                          # HiringThing ATS — stuck on listing page
    )

    @staticmethod
    def _domain_matches(netloc: str, pattern: str) -> bool:
        # Exact suffix match — prevents "fetchjobs.co" matching "fetchjobs.com", etc.
        return netloc == pattern or netloc.endswith("." + pattern)

    # ── Seam: page_snapshot ──────────────────────────────────────────────────
    async def _page_snapshot(self, page: Page) -> dict:
        """Structured representation of the current page for the LLM: URL, visible
        (in-viewport) text, form fields with their current values, and buttons.

        Thin wrapper over the DOM-walk in :meth:`_get_page_snapshot` — kept as a
        named seam so the orchestrator and tests have one entry point.
        """
        return await self._get_page_snapshot(page)

    # ── Seam: decide_action ─────────────────────────────────────────────────
    async def _decide_action(
        self, snapshot: dict, step: int, history: list[str] | None = None, *,
        current_url: str = "", job_summary: str = "",
        context_notes: list[str] | None = None, override_filled: dict | None = None,
    ) -> dict:
        """One ``llm.query`` round-trip: snapshot + context in, next action dict out.

        Pure-ish — given fixed inputs it makes exactly one LLM call and parses the
        result. Delegates to :meth:`_ask_llm_action`; isolated here for testing.
        """
        return await self._ask_llm_action(
            snapshot, step, history, current_url=current_url,
            job_summary=job_summary, context_notes=context_notes,
            override_filled=override_filled,
        )

    # ── Seam: coerce_fill_value ────────────────────────────────────────────
    def _coerce_fill_value(self, selector: str, text: str, value: str,
                           snapshot: dict) -> str:
        """T31: reduce a prose fill answer to a bare integer when the target is a
        numeric / 1-N-scale field. Runs in the orchestrator before the action is
        dispatched to :meth:`_execute_action` (which is left untouched).

        Two resolution paths:

        * **snapshot field** — match the action ``selector`` (``#id`` / ``[name]``)
          against a field in the snapshot. This gives the real field ``type``, so
          :func:`_coerce_numeric_answer` passes a ``<textarea>`` straight through.
        * **``text`` hint fallback** — when the selector is a CSS-class / xpath /
          ``:has-text`` pattern that resolves to no snapshot field. ``text`` is the
          LLM action's click-target hint, not a field label, so this is
          best-effort and deliberately conservative: only when the value already
          contains a digit and is short (≤ 40 chars) — pure extraction
          (``"…an 8 out of 10"`` → ``"8"``), never fabricating a number from the
          profile and never collapsing a genuine free-text paragraph.
        """
        _tgt = next(
            (f for f in snapshot.get("fields", [])
             if (f.get("id") and selector and f["id"] in selector)
             or (f.get("name") and selector and f["name"] in selector)),
            None,
        )
        if _tgt:
            return _coerce_numeric_answer(
                _tgt.get("label") or "", value,
                _tgt.get("type", "text"), self.profile,
            )
        if text and re.search(r"\d", value) and len(value) <= 40:
            return _coerce_numeric_answer(text, value, "text", self.profile)
        return value

    # ── Seam: detect_terminal_state ────────────────────────────────────────
    def _classify_domain(self, netloc: str, *, include_dead_end: bool = False) -> str | None:
        """Classify a hostname against the static block lists.

        Returns ``"skipped"`` (spam/aggregator), ``"blocked"`` (un-automatable
        ATS / SSO dead-end → ``applied=-3``, no auto-retry), or ``None`` when the
        domain is fine to proceed on. ``include_dead_end`` adds the substring
        match against :data:`_DEAD_END_DOMAINS` used only for mid-flow redirects.
        """
        netloc = netloc.lower()
        if include_dead_end and any(d in netloc for d in self._DEAD_END_DOMAINS):
            return "blocked"
        if any(self._domain_matches(netloc, d) for d in self._SPAM_DOMAINS):
            return "skipped"
        if any(self._domain_matches(netloc, d) for d in self._BLOCKED_AUTO_APPLY_DOMAINS):
            return "blocked"
        return None

    async def _detect_expired(self, page: Page, *, check_url: bool = True,
                              text_len: int = 800,
                              body_text: str | None = None) -> str | None:
        """Return ``"expired"`` if the page is a closed / removed / not-found job
        posting, else ``None``. ``check_url`` also matches the URL against
        :data:`_EXPIRED_URL_PATTERNS` (skipped mid-loop, where a redirect back to
        a listing URL is not itself an expiry signal); ``text_len`` caps the
        body-text slice read. The URL check runs first and never throws, so a JS
        error in the text read can't suppress it.

        ``body_text``: if the caller already read ``document.body.innerText``
        (the mid-loop path does, for its Cloudflare check), pass it here to
        avoid a second ``page.evaluate`` — and, matching that caller's
        pre-refactor behaviour, a read failure there is swallowed silently
        rather than logged.
        """
        if check_url and any(p in page.url.lower() for p in self._EXPIRED_URL_PATTERNS):
            print(f"  [LLM] URL indicates expired job ({page.url}) — skipping")
            return "expired"
        if body_text is None:
            try:
                body_text = await page.evaluate(
                    f"() => (document.body.innerText || '').slice(0, {int(text_len)})"
                )
            except Exception as _e:
                print(f"  [LLM] Warning: could not read page text for expired check ({_e})")
                return None
        if any(p in body_text.lower() for p in self._EXPIRED_TEXT_PHRASES):
            print("  [LLM] Page text indicates expired job — skipping")
            return "expired"
        return None

    async def _detect_terminal_state(self, page: Page, *, step: int) -> str | None:
        """Mid-loop classifier for walls that appear *after* the Apply click and
        are not domain- or auth-based: a live reCAPTCHA/hCaptcha widget, a
        Cloudflare interstitial, expired-job text, and the Greenhouse text
        "security code" challenge.

        Returns a terminal status (``"skipped"`` / ``"expired"``) or ``None`` to
        keep looping. In interactive mode a Greenhouse security challenge pauses
        for the human and then returns ``None``; in ``--auto`` it returns
        ``"skipped"`` (a bare ``input()`` would wedge the event loop and the
        outer ``asyncio.wait_for`` guard forever).
        """
        # reCAPTCHA / hCaptcha widget loaded into the form
        try:
            if await page.locator(_CAPTCHA_WIDGET_SELECTOR).count() > 0:
                print("  [LLM] reCAPTCHA detected mid-loop — cannot submit, skipping")
                return "skipped"
        except Exception:
            pass

        # Cloudflare bot-gate + "job no longer available" after a navigation.
        # One body-text read, shared by the Cloudflare check and _detect_expired
        # (URL patterns skipped — a mid-loop redirect to a listing URL is not an
        # expiry signal). A read failure is swallowed silently, as on master.
        if step > 0:
            try:
                _mid_text = await page.evaluate(
                    "() => (document.body.innerText || '').slice(0, 400)"
                )
            except Exception:
                _mid_text = ""
            if any(s in _mid_text.lower() for s in self._CLOUDFLARE_SIGNALS):
                print("  [LLM] Cloudflare security wall detected mid-loop — skipping")
                return "skipped"
            if await self._detect_expired(page, check_url=False, body_text=_mid_text):
                return "expired"

        # Greenhouse text "security code" bot wall — host-gated (the phrases
        # overlap the legitimate account-creation email-verification step).
        if "greenhouse.io" in urlparse(page.url).netloc.lower():
            try:
                _gh_text = (await page.evaluate(
                    "() => (document.body.innerText || '').slice(0, 800)"
                )).lower()
                if any(s in _gh_text for s in _GREENHOUSE_SECURITY_SIGNALS):
                    if self.auto_mode:
                        print(f"  [LLM] Greenhouse security challenge at {page.url} — "
                              f"cannot solve in --auto, skipping")
                        return "skipped"
                    print(f"\n  [LLM] ⚠ Greenhouse security check detected at {page.url}")
                    print("  [LLM] Please solve the security challenge in the browser, "
                          "then press Enter to continue...")
                    try:
                        input("  Press Enter when done: ")
                    except EOFError:
                        pass
                    print("  [LLM] Resuming after security challenge...")
                    await asyncio.sleep(1)
            except Exception:
                pass
        return None

    # ── Seam: prefer_workday_autofill (T51) ─────────────────────────────────
    async def _prefer_workday_autofill(self, page: Page) -> None:
        """Workday-only nudge: when the application-start page offers both
        "Autofill with Resume" and "Apply Manually", click Autofill so Workday
        parses the uploaded resume into My Experience / My Information before
        the generic step loop starts correcting whatever the parse got wrong
        (Option C — see docs/TICKETS.md T51).

        Deliberately does NOT handle the resume upload itself: Autofill
        reveals a ``<input type="file">`` the same way every other ATS's
        resume-upload step already does on this codebase, and the existing
        generic ``upload`` action in :meth:`_execute_action` (driven by the
        LLM step loop) picks it up on the very next step — no Workday-specific
        upload code needed, matching T51's "the correction loop should fall
        out of the existing generic step loop" scope.

        A no-op on any non-Workday domain, and a no-op when only one of the
        two buttons is present (Autofill already used on a prior step, or a
        Workday tenant that only offers Manual entry) — the LLM step loop
        continues normally from there either way.
        """
        try:
            _netloc = urlparse(page.url).netloc.lower()
        except Exception:
            return
        if not any(self._domain_matches(_netloc, d) for d in self._WORKDAY_DOMAINS):
            return
        try:
            autofill = page.locator(
                'button:has-text("Autofill with Resume"), a:has-text("Autofill with Resume"), '
                '[role="button"]:has-text("Autofill with Resume")'
            ).first
            manual = page.locator(
                'button:has-text("Apply Manually"), a:has-text("Apply Manually"), '
                '[role="button"]:has-text("Apply Manually")'
            ).first
            if await autofill.count() == 0 or await manual.count() == 0:
                return
            if not await autofill.is_visible():
                return
            print("  [Offsite] Workday: 'Autofill with Resume' + 'Apply Manually' both "
                  "offered — preferring Autofill")
            # Same 3-tier click-retry chain used for Workday's registration submit
            # (_try_register): a transparent overlay div can intercept the real click.
            try:
                await autofill.click(timeout=5000)
            except Exception:
                try:
                    await autofill.click(force=True, timeout=5000)
                except Exception:
                    try:
                        await page.locator('[data-automation-id="click_filter"]').first.click(
                            force=True, timeout=5000
                        )
                    except Exception:
                        print("  [Offsite] Workday: could not click Autofill — "
                              "falling through to normal step loop")
                        return
            await asyncio.sleep(2)
        except Exception as exc:
            print(f"  [Offsite] Workday autofill preference check errored ({exc}) — "
                  "continuing normally")

    # ── Seam: handle_auth ─────────────────────────────────────────────────
    # Return protocol for _handle_auth:
    #   None            → not an auth page; the orchestrator proceeds normally
    #   "__continue__"  → auth handled; the orchestrator should `continue` (re-snapshot)
    #   "__proceed__"   → logged in mid-form; proceed in the SAME iteration
    #   other str       → terminal status ("skipped" / "failed" / "blocked")
    _AUTH_CONTINUE = "__continue__"
    _AUTH_PROCEED = "__proceed__"

    async def _handle_auth(self, page: Page, *, phase: str) -> str | None:
        """Login walls, SSO redirects and mid-form password gates.

        ``phase="url"`` (before the snapshot): SSO / IdP redirects go to
        :meth:`_handle_sso_page`; a ``_LOGIN_PATHS`` URL goes to
        :meth:`_handle_auth_page` (which checks stored credentials, tries
        "Continue with LinkedIn", and — via :meth:`_try_register` /
        :meth:`_fill_registration_form` / ``EmailInbox`` — can create an
        account). ``phase="form"`` (after the snapshot): a bare
        ``input[type=password]`` on the page is a login gate — try stored
        credentials, else it needs a human.

        Preserves the T34/T35 blocked-vs-failed split exactly: "no credentials
        exist / domain is a dead end" → ``"blocked"`` (-3); "stored credentials
        exist but the login attempt itself failed" → ``"failed"`` (-2).
        """
        _url = page.url.lower()
        _url_domain = urlparse(_url).netloc

        if phase == "url":
            _is_sso = any(s in _url_domain for s in self._SSO_DOMAINS) or ".okta.com" in _url_domain
            if _is_sso:
                if self._auth_attempted:
                    print(f"  [LLM] SSO auth already attempted on {_url_domain} — skipping")
                    return "skipped"
                self._auth_attempted = True
                print(f"  [LLM] SSO redirect detected ({_url_domain}) — invoking sign-in flow")
                ok = await self._handle_sso_page(page)
                if not ok:
                    print(f"  [LLM] SSO sign-in failed on {_url_domain} — skipping")
                    return "skipped"
                await asyncio.sleep(2)
                return self._AUTH_CONTINUE

            _url_path = urlparse(_url).path
            if any(_url_path == p or _url_path.startswith(p + "/") or _url_path.startswith(p + "?")
                   for p in self._LOGIN_PATHS):
                if self._auth_attempted:
                    print("  [LLM] Auth already attempted — giving up")
                    return "failed"
                self._auth_attempted = True
                ok = await self._handle_auth_page(page)
                if ok is not True:
                    _auth_domain = urlparse(page.url).netloc.lower()
                    if ok == "failed":
                        # Stored credentials exist but the login attempt itself failed
                        # (transient / 2FA / rate-limit) — retryable, not a dead end.
                        print(f"  [LLM] Login with stored credentials failed for "
                              f"{_auth_domain} — marking failed")
                        return "failed"
                    print(f"  [LLM] Login wall on {_auth_domain} — needs a human, "
                          f"marking blocked (no auto-retry)")
                    return "blocked"
                await asyncio.sleep(2)
                return self._AUTH_CONTINUE
            return None

        # phase == "form": mid-loop password-field login wall.
        #
        # master wrapped the whole probe + login attempt in ONE broad
        # ``try: … except Exception: pass`` so any throw (a dead browser page
        # mid-login, a locator error, a bad logins.csv row) was swallowed and
        # the step loop continued — the job could still recover to "applied" on
        # a later step. Preserve that: only the deliberate "failed" / "blocked"
        # returns below are terminal; a genuine exception falls through to
        # ``None`` (proceed this iteration).
        try:
            _pwd_fields = await page.locator("input[type='password']").count()
            if _pwd_fields <= 0:
                return None
            _cur_domain = urlparse(page.url).netloc.lower()
            existing = _find_account_for_domain(_cur_domain)
            if existing:
                if await self._try_login(page, existing["email"], existing["password"]):
                    return self._AUTH_PROCEED   # logged in — keep going this iteration
                print(f"  [LLM] Login with stored credentials failed for {_cur_domain} — marking failed")
                return "failed"
            print(f"  [LLM] Login wall detected (password field) on {_cur_domain} — needs a human, "
                  f"marking blocked (no auto-retry)")
            return "blocked"
        except Exception as _auth_exc:
            print(f"  [LLM] Mid-loop login-wall check errored ({_auth_exc}) — continuing")
            return None

    async def _llm_guided_apply(self, page: Page) -> str:
        """
        LLM-guided application loop (the single OffsiteApply engine).

        Thin orchestrator over the seams: ``_page_snapshot`` → ``_detect_terminal_state``
        / ``_classify_domain`` / ``_handle_auth`` → ``_decide_action`` → execute →
        repeat, with loop-guard / repeat-action / max-step logic preserved.
        Returns 'applied' | 'skipped' | 'failed' | 'blocked' | 'expired'.
        """
        self._auth_attempted = False
        action_history: list[str] = []
        context_notes: list[str] = []   # per-step observations from the LLM, accumulated as running context
        prev_url = ""
        unchanged_steps = 0
        _submit_clicked = False  # a real submit-type button has been clicked at least once
        last_action_type = None  # used to not penalize fill/select/upload for not changing URL
        # T39: has the loop ever engaged the form? True once a non-scroll action
        # (fill/select/upload/click) executes. Deliberately NOT set from a page
        # snapshot exposing `fields` — `_get_page_snapshot` returns every visible
        # input on the page, so a careers-SPA nav search box or footer "job
        # alerts" signup would falsely mark the form engaged. If the LLM sees a
        # real actionable field it acts on it; a run of pure scrolls means the
        # form was genuinely unreachable. Distinguishes a navigation dead-end
        # (→ "blocked"/-3, no auto-retry) from a mid-form stall (→ "failed"/-2).
        _form_engaged = False
        consecutive_duplicates = 0  # consecutive duplicate-fill guard firings without URL change
        _selector_attempts: dict[str, int] = {}  # per-selector retry count; skip after 3 failures
        _exhausted_selectors: set[str] = set()  # selectors permanently blocked after 3 failed attempts
        _email_verified = False  # prevents re-triggering inbox poll after verification is handled
        # Fields whose values are committed to hidden DOM state (e.g. jQuery UI autocomplete) but
        # whose visible input is cleared by site JS. Tracked here so the LLM sees them as FILLED.
        _forced_filled: dict[str, str] = {}

        _landing_domain = urlparse(page.url).netloc.lower()
        _pre = self._classify_domain(_landing_domain)
        if _pre == "skipped":
            print(f"  [LLM] Spam/aggregator domain ({_landing_domain}) — skipping")
            return "skipped"
        if _pre == "blocked":
            print(f"  [LLM] Blocked auto-apply domain ({_landing_domain}) — needs a human, "
                  f"marking blocked (no auto-retry)")
            return "blocked"

        # T39 — Canonical Greenhouse embed retry. Some employers point their
        # LinkedIn OffsiteApply ``application_url`` at a *.greenhouse.io board
        # host that 30x-redirects to a company-branded careers SPA where the
        # apply CTA opens an embedded form on click and is unreachable by the
        # step-loop (it only ever scrolls, then the stuck guard fires → -2, and
        # -2 re-burns on every --auto run). If the ORIGINAL application_url was a
        # greenhouse host and we landed cross-host on a non-greenhouse host,
        # retry once against job-boards.greenhouse.io/<slug>/jobs/<id>, which
        # renders the bare form with no company wrapper.
        # Intentionally placed AFTER the spam / blocked-domain pre-check above:
        # if the SPA host is itself a skip/blocked domain we want that verdict,
        # not a canonical retry.
        _orig_url = getattr(self, "application_url", "") or ""
        _orig_host = urlparse(_orig_url).netloc.lower()
        _gh_job = _parse_greenhouse_job(_orig_url)
        if (
            _gh_job
            and _host_is_greenhouse(_orig_host)
            and not _host_is_greenhouse(_landing_domain)
            and _landing_domain != _orig_host
        ):
            _gh_slug, _gh_id = _gh_job
            _canonical = f"https://job-boards.greenhouse.io/{_gh_slug}/jobs/{_gh_id}"
            print(f"  [Offsite] Greenhouse board URL redirected to {_landing_domain} — "
                  f"retrying canonical embed job-boards.greenhouse.io/{_gh_slug}/jobs/{_gh_id}")
            try:
                await page.goto(_canonical, wait_until="domcontentloaded", timeout=20000)
                await asyncio.sleep(2)
            except Exception as _gh_re:
                print(f"  [Offsite] Canonical Greenhouse retry navigation failed ({_gh_re}) — "
                      f"continuing from redirected page")
            _landing_domain = urlparse(page.url).netloc.lower()
            if _host_is_greenhouse(_landing_domain):
                print(f"  [Offsite] Canonical Greenhouse embed loaded → {page.url}")
            else:
                print(f"  [Offsite] Canonical embed also redirected ({_landing_domain}) "
                      f"— falling through to step loop")

        # Lever listing-page fast-path: jobs.lever.co/<company>/<id> is a listing page with no form.
        # Append /apply to navigate directly to the application form, saving a wasted step-loop iteration.
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

        # Check immediately if the landing page shows an expired/unavailable job
        # (URL patterns + body text — _detect_expired logs which one matched).
        if await self._detect_expired(page):
            return "expired"

        # Bot-wall pre-check: skip BEFORE the _summarize_job LLM call — a
        # CAPTCHA-walled job can't be submitted no matter what we do next.
        _wall = await self._detect_bot_wall(page)
        if _wall:
            print(f"  [LLM] {_wall} on landing form — cannot proceed, skipping")
            return "skipped"

        # Summarize job before entering the step loop — gives the LLM stable context about the role
        job_summary = await self._summarize_job()
        print(f"  [LLM] Job summary: {job_summary[:120]}{'...' if len(job_summary) > 120 else ''}")

        # Re-run the bot-wall check: navigation done above (Lever /apply,
        # Greenhouse embed) may have landed on a new page (e.g. a CAPTCHA step).
        _wall = await self._detect_bot_wall(page)
        if _wall:
            print(f"  [LLM] {_wall} before step loop — cannot submit, skipping")
            return "skipped"

        _session_start = datetime.now(timezone.utc)
        for step in range(30):
            _step_start = datetime.now(timezone.utc)
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=8000)
            except Exception:
                pass
            await asyncio.sleep(2)

            # Email verification: code or link sent to inbox
            if self.inbox and not _email_verified:
                try:
                    _pg_text = (await page.evaluate("() => (document.body.innerText || '').slice(0, 600)")).lower()
                    _email_signals = ("enter the code", "enter code", "verification code",
                                      "enter your code", "check your email", "we sent a code",
                                      "sent you a code", "email verification", "sent to your email")
                    if any(s in _pg_text for s in _email_signals):
                        _ev_domain = urlparse(page.url).netloc.lower()
                        print(f"  [LLM] Email verification detected — checking inbox…")
                        _ev_code, _ev_link = await asyncio.to_thread(
                            self.inbox.fetch_verification, _ev_domain, 60
                        )
                        if _ev_code:
                            print(f"  [LLM] Email code: {_ev_code}")
                            _code_inp = page.locator(
                                'input[type="text"], input[type="number"], '
                                'input[name*="code" i], input[placeholder*="code" i], '
                                'input[aria-label*="code" i]'
                            ).first
                            if await _code_inp.count() > 0 and await _code_inp.is_visible():
                                await _code_inp.fill(_ev_code)
                                await _code_inp.press("Enter")
                                await asyncio.sleep(1)
                            _email_verified = True
                        elif _ev_link:
                            print(f"  [LLM] Email verification link found — navigating…")
                            await page.goto(_ev_link, wait_until="domcontentloaded", timeout=20000)
                            await asyncio.sleep(2)
                            _email_verified = True
                        else:
                            print(f"  [LLM] No verification email received in time")
                except Exception as _exc:
                    print(f"  [LLM] Email verification check error: {_exc}")

            current_url = page.url
            _elapsed = int((datetime.now(timezone.utc) - _session_start).total_seconds())
            print(f"  [LLM] Step {step + 1} (+{_elapsed}s) — {current_url}")

            # Mid-flow redirect into a dead-end / spam / blocked domain
            # (e.g. Dynatrace → SuccessFactors, my.greenhouse.io SSO wall).
            _url_domain = urlparse(current_url.lower()).netloc
            _mid = self._classify_domain(_url_domain, include_dead_end=True)
            if _mid == "skipped":
                print(f"  [LLM] Redirected to spam/aggregator domain mid-flow ({_url_domain}) — skipping")
                return "skipped"
            if _mid == "blocked":
                print(f"  [LLM] Redirected to blocked / dead-end domain mid-flow ({_url_domain}) — "
                      f"needs a human, marking blocked (no auto-retry)")
                return "blocked"

            # T51: Workday-only nudge — prefer "Autofill with Resume" over "Apply
            # Manually" when both are offered. No-op off Workday or once Autofill
            # has already been used; runs every iteration since it's unclear
            # which step (pre- or post-account-creation) a given Workday tenant
            # shows this choice on.
            await self._prefer_workday_autofill(page)

            # Auth walls (URL-based): SSO / IdP redirects and login/registration
            # pages. Path segments are matched so /joinroot/ != /join, etc.
            _auth = await self._handle_auth(page, phase="url")
            if _auth == self._AUTH_CONTINUE:
                continue
            if _auth is not None:
                return _auth

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
                            # Re-check blocked domains — deterministic click may have redirected into one
                            _post_nav_domain = urlparse(page.url).netloc.lower()
                            _post = self._classify_domain(_post_nav_domain)
                            if _post == "skipped":
                                print(f"  [LLM] Post-navigation spam domain ({_post_nav_domain}) — skipping")
                                return "skipped"
                            if _post == "blocked":
                                print(f"  [LLM] Post-navigation blocked ATS ({_post_nav_domain}) — needs a human, "
                                      f"marking blocked (no auto-retry)")
                                return "blocked"
                        break  # found a match — proceed with snapshot of current page
                    except Exception:
                        continue

            # Get page snapshot — retry up to 2× if page is blank (SPA still rendering)
            for _snap_try in range(3):
                snapshot = await self._page_snapshot(page)
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

            # Mid-loop login-wall check: a password field is a login gate. Try
            # stored credentials; else it needs a human.
            _auth = await self._handle_auth(page, phase="form")
            if _auth is not None and _auth != self._AUTH_PROCEED:
                return _auth

            # Mid-loop walls that appear after the Apply click and aren't
            # domain- or auth-based: live reCAPTCHA widget, Cloudflare gate,
            # expired-job text, Greenhouse "security code" challenge.
            _terminal = await self._detect_terminal_state(page, step=step)
            if _terminal is not None:
                return _terminal

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
                    # T39/T45: distinguish a navigation dead-end from a mid-form
                    # stall. If the loop never engaged a real form field and we're
                    # not on a known ATS form host, this is an unreachable apply
                    # CTA (e.g. a careers SPA) — mark blocked (-3, no auto-retry)
                    # instead of failed (-2), which would re-burn the same ~40s +
                    # LLM calls every run. Shared with the other two give-up sites.
                    _stuck_host = urlparse(page.url).netloc.lower()
                    _terminal = _terminal_state_for_stall(page, form_engaged=_form_engaged)
                    if _terminal == "blocked":
                        print(f"  [Offsite] No form or reachable apply control after "
                              f"{unchanged_steps} scrolls on {_stuck_host} — marking blocked "
                              f"(needs a human, no auto-retry)")
                    else:
                        print(f"  [LLM] URL unchanged for 3 consecutive steps — browser is stuck, giving up")
                    return _terminal
            else:
                unchanged_steps = 0
                _exhausted_selectors.clear()   # new page — field selectors are no longer relevant
                _selector_attempts.clear()     # new page — per-selector retry counts reset too
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
            action = await self._decide_action(
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
            # T31: if the LLM is filling a numeric / 1-N-scale field with prose,
            # reduce it to the bare integer before it is typed.
            if action_type == "fill" and value:
                value = self._coerce_fill_value(selector, text, value, snapshot)
            # Capture per-step observation and append to running context
            update = action.get("update", "").strip()
            if update:
                context_notes.append(update)

            _step_ms = int((datetime.now(timezone.utc) - _step_start).total_seconds() * 1000)
            print(f"  [LLM] Thought: {reason}")
            _display_value = (self.profile.get("resume_path", value) if action_type == "upload" else value)
            print(f"  [LLM] Action: {action_type}" + (f" → {selector or text!r}" if selector or text else "") + (f" = {_display_value[:80]!r}" if _display_value else "") + f"  [{_step_ms}ms]")

            # Layer 1 exhausted-selector guard: if the LLM proposes a selector that has already
            # been permanently blocked (attempted 3+ times with no state change), intercept before
            # execution and try to advance the form instead. This catches cases where the LLM
            # ignores the BLOCKED SELECTORS notice in the prompt (e.g. because the visible field
            # text still shows the field as empty even after _forced_filled is set).
            _check_key = selector or text
            if _check_key and _check_key in _exhausted_selectors:
                _advanced = False
                for _adv_sel in (
                    'button:has-text("Continue")',
                    'button:has-text("Next")',
                    'button:has-text("Submit Application")',
                    'button:has-text("Submit")',
                ):
                    try:
                        _adv_btn = page.locator(_adv_sel).first
                        if await _adv_btn.count() > 0 and await _adv_btn.is_visible() and await _adv_btn.is_enabled():
                            print(f"  [LLM] Exhausted selector proposed again — attempting form advance via {_adv_sel!r}")
                            await _adv_btn.evaluate("el => el.click()")
                            await asyncio.sleep(2)
                            _advanced = True
                            break
                    except Exception:
                        continue
                if not _advanced:
                    print(f"  [LLM] Exhausted selector proposed again — no advance button found, continuing loop")
                continue

            # Per-step reCAPTCHA guard: if the LLM hallucinates a reCAPTCHA-related selector
            # (e.g. "#g-recaptcha-response-100000:has-text('Submit Application')"), bail immediately
            # rather than waiting for a Playwright timeout on a selector that can never match.
            if "recaptcha" in (selector or "").lower() or "g-recaptcha" in (selector or "").lower():
                print(f"  [LLM] reCAPTCHA selector in proposed action — cannot proceed without CAPTCHA solver, skipping")
                return "skipped"

            # Per-selector retry cap: if the same selector has been attempted 3+ times
            # without a URL change, mark it as permanently stuck and skip it.
            # This prevents infinite loops on unresolvable fields (React Select, tabindex=-1, etc.)
            if action_type in ("fill", "select") and (selector or text):
                _sel_key = selector or text
                _selector_attempts[_sel_key] = _selector_attempts.get(_sel_key, 0) + 1
                if _selector_attempts[_sel_key] >= 3:
                    _exhausted_selectors.add(_sel_key)  # Layer 1 will intercept future proposals
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
                            return _terminal_state_for_stall(page, form_engaged=_form_engaged)
                        # Page advanced (or we tried to) — skip re-executing the duplicate action
                        continue
                    else:
                        print(f"  [LLM] Action '{hist_key}' already in history — page not advancing, giving up")
                        return _terminal_state_for_stall(page, form_engaged=_form_engaged)

            if action_type == "done":
                confirmed, conf_reason = await self._check_submission_result(
                    page, prev_url, submit_attempted=_submit_clicked)
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
                _expired_reasons = ("no longer available", "no longer active", "job not found",
                                    "position closed", "job closed", "expired", "posting.*no longer",
                                    "not accepting", "has been filled", "position has been")
                if any(k in reason.lower() for k in _expired_reasons):
                    print(f"  [LLM] Job reported as expired/closed ({reason}) — skipping")
                    return "expired"
                # Detect unblockable verification walls
                if any(k in reason.lower() for k in self._UNBLOCKABLE_WALL_KEYWORDS):
                    print(f"  [LLM] Unblockable wall ({reason}) — skipping job")
                    return "skipped"
                return "failed"

            # ── Seam: execute the decided action (scroll/upload/fill/select/click) ──
            _state = _StepState(page, selector, _forced_filled, _submit_clicked)
            _exec_result = await self._execute_action(action_type, text, value, _state)
            page = _state.page
            selector = _state.selector          # `select` may normalise a #<digit> id
            _submit_clicked = _state.submit_clicked
            if _exec_result is not None:
                return _exec_result

            # Record action in history (keep last 10)
            hist_entry = f"{action_type}:{selector or text}"
            if value:
                hist_entry += f"={value[:30]}"
            action_history.append(hist_entry)
            if len(action_history) > 10:
                action_history = action_history[-10:]
            last_action_type = action_type
            # T39/T45: a real form interaction means a later stall is a mid-form
            # failure (-2, retryable), not a navigation dead end (-3).
            # fill/select/upload only ever run against a real field. A `click`
            # counts only if it resolved a non-nav-link target
            # (_StepState.click_hit_target) — a not-found click or a bare-nav-link
            # click ("Apply" / "Working with us" on a careers SPA) must not flip
            # this, or a pure navigation dead end looks form-engaged and gets a
            # pointless -2 retry every --reset-failed run.
            if action_type in ("fill", "select", "upload"):
                _form_engaged = True
            elif action_type == "click" and _state.click_hit_target:
                _form_engaged = True

        print("  [LLM] Reached step limit without completion")
        return _terminal_state_for_stall(page, form_engaged=_form_engaged)

    async def _execute_action(self, action_type: str, text: str, value: str,
                              state: "_StepState") -> str | None:
        """Seam: apply one decided action to the page — ``scroll`` / ``upload`` /
        ``fill`` / ``select`` / ``click``. Verbatim extraction of the dispatch that
        used to sit inline in :meth:`_llm_guided_apply`; no LLM call except the
        React-Select decline-option picker (unchanged from the inline version).

        Returns a terminal status (``"skipped"``, or whatever :meth:`_handle_submit`
        returns for a submit click) to end the flow, or ``None`` to keep looping.
        Threads mutable per-step context through ``state``: a non-submit click that
        opens a new tab rebinds ``state.page``; the ``select`` branch normalises a
        ``#<digit-id>`` selector in place (``state.selector``); ``forced_filled`` is
        mutated directly; ``state.submit_clicked`` latches once a submit fires.
        """
        page = state.page
        selector = state.selector
        _forced_filled = state.forced_filled
        try:
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
                try:
                    # Module-level _safe_selector: rewrites bare #id / tag#id to
                    # the [id="…"] attribute form when the id is not a valid bare
                    # CSS identifier (React 18 useId colon ids, leading digits) —
                    # see its docstring for the deliberate compound-selector limit.
                    # Normalise in place (as the `select` branch does) so the
                    # `finally` writeback records the same form in step history.
                    selector = _safe_selector(selector)
                    safe_sel = selector
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
                                                    _llm_picked = await llm.query(
                                                        f"Dropdown options: {_llm_opts_texts}\n"
                                                        f"Which option best means 'prefer not to disclose' or declining to answer? "
                                                        f"Reply with ONLY the exact option text from the list.",
                                                        model=self.model,
                                                        timeout=25,
                                                    )
                                                    _llm_picked = _llm_picked.strip().strip('"').strip("'")
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
                                    # Some ATSes (Greenhouse, Lever, etc.) commit the autocomplete
                                    # value to a hidden field and clear the visible input. Track any
                                    # field whose value disappears after autocomplete selection so the
                                    # LLM sees it as FILLED and doesn't retry it next step.
                                    # Track fields where the value disappeared after fill — some ATSes
                                    # (Greenhouse, Lever, etc.) commit the value to a hidden field and
                                    # clear the visible input. Mark as filled so LLM doesn't retry.
                                    await asyncio.sleep(0.5)
                                    try:
                                        _ov_id = await el.get_attribute("id") or ""
                                        _post_fill_val = await el.input_value()
                                        if not _post_fill_val and _ov_id:
                                            _forced_filled[_ov_id] = value
                                            _reason = "after suggestion click" if _clicked_suggestion else "value committed internally"
                                            print(f"  [LLM] Field #{_ov_id!r}: {_reason}, marked as filled")
                                    except Exception:
                                        pass
                except Exception as exc:
                    if _is_browser_crash(exc):
                        raise  # T36: handled by the method-level crash catch
                    exc_str = str(exc)
                    print(f"  [LLM] Fill failed: {exc_str[:200]}")
                    if "captcha" in exc_str.lower() or "hcaptcha" in exc_str.lower():
                        print("  [LLM] CAPTCHA detected — cannot proceed, skipping")
                        return "skipped"

            elif action_type == "select" and selector and value:
                try:
                    # Same normalisation as the `fill` branch — a React 18 useId
                    # colon id (or any non-bare-CSS id) would otherwise raise
                    # SyntaxError in page.locator(). The `finally` writeback then
                    # records the normalised form in step history (T44).
                    selector = _safe_selector(selector)
                    el = page.locator(selector).first
                    if await el.count() > 0:
                        _sel_done = False
                        # Guard: radio/checkbox must be .check()ed — select_option raises and
                        # the combobox fallback's `_tag == "input"` check would match, then
                        # el.fill() on a radio throws "Input of type 'radio' cannot be filled".
                        _input_type = (await el.get_attribute("type") or "").lower()
                        if _input_type in ("radio", "checkbox"):
                            if _input_type != "checkbox" or not await el.is_checked():
                                await el.check()
                            _sel_done = True
                            _field_id = await el.get_attribute("id") or ""
                            if _field_id:
                                _forced_filled[_field_id] = value
                            print(f"  [LLM] Auto-redirected select→check for {_input_type}")
                        # Try native select_option first
                        if not _sel_done:
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
                                                _llm_sel_pick = await llm.query(
                                                    f"Dropdown options: {_llm_sel_texts}\n"
                                                    f"Which option best means 'prefer not to disclose' or declining to answer? "
                                                    f"Reply with ONLY the exact option text from the list.",
                                                    model=self.model,
                                                    timeout=25,
                                                )
                                                _llm_sel_pick = _llm_sel_pick.strip().strip('"').strip("'")
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
                                # Mark the field as filled so the LLM doesn't re-propose the same
                                # select action on the next step, creating an infinite retry loop.
                                try:
                                    _fail_id = (await el.get_attribute("id") or selector).lstrip("#")
                                    if _fail_id:
                                        _forced_filled[_fail_id] = "(failed)"
                                except Exception:
                                    pass
                        await asyncio.sleep(0.5)
                except Exception as exc:
                    if _is_browser_crash(exc):
                        raise  # T36: handled by the method-level crash catch
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
                    # Record that a submit was attempted — verify_submission uses
                    # this to distinguish a real post-submit redirect from a
                    # stray nav / premature "done".
                    state.submit_clicked = True
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
                                    # T45: force-clicking a real submit button on a
                                    # form (inputs present, else is_submit_btn was
                                    # already flipped off) is genuine engagement.
                                    state.click_hit_target = True
                                    # Continue step loop so LLM sees the validation errors
                                    break
                                return await self._handle_submit(page, el)
                        except Exception as _click_exc:
                            if _is_browser_crash(_click_exc):
                                raise  # T36: handled by the method-level crash catch
                            continue
                    else:
                        print(f"  [LLM] Submit button not found: {selector!r} / {text!r}")
                else:
                    clicked = False
                    _clicked_anchor = False
                    for loc_str in loc_strs:
                        try:
                            el = page.locator(loc_str).first
                            if await el.count() > 0:
                                # T45: decide nav-link-ness from the element that
                                # actually resolved, not the LLM's proposed
                                # `selector`. loc_strs falls back to
                                # a:has-text()/[aria-label*=] locators, so a
                                # non-`a` primary selector + `text` can still land
                                # on a real <a> nav link and must not count as
                                # form engagement. Read the live tag; on failure
                                # fall back to the resolved locator string.
                                try:
                                    _tag = (await el.evaluate("e => e.tagName") or "").lower()
                                except Exception:
                                    _tag = ""
                                _clicked_anchor = (
                                    _tag == "a" if _tag
                                    else loc_str.lstrip().startswith("a")
                                )
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
                        except Exception as _click_exc:
                            if _is_browser_crash(_click_exc):
                                raise  # T36: handled by the method-level crash catch
                            continue
                    if not clicked:
                        print(f"  [LLM] Click target not found: {selector!r} / {text!r}")
                    # T45: a click "engages the form" only when it resolved a real
                    # target AND was not a plain nav-link click. A bare <a> is
                    # marketing/careers chrome ("Apply" / "Working with us" on a
                    # careers SPA) that only navigates; treating it as engagement
                    # makes a navigation dead end look like a mid-form stall and
                    # earns a pointless -2 retry every --reset-failed run.
                    state.click_hit_target = clicked and not _clicked_anchor
        except Exception as exc:
            # T36: a Chromium tab/renderer crash mid-action ("Target crashed" /
            # TargetClosedError from a .count()/.fill()/.click()). The per-branch
            # handlers above re-raise it to here. End the job deliberately as a
            # retryable failure — NOT a stall (don't route through
            # _terminal_state_for_stall) — and flag the flow so run_session
            # rebuilds the shared page before the next job.
            if _is_browser_crash(exc):
                self._browser_crashed = True
                print(f"  [Offsite] Browser tab crashed mid-apply ({exc}) — "
                      f"marking failed for retry")
                return "failed"
            raise
        finally:
            # Terminal returns above unwind straight out; only the fall-through
            # path needs the new-tab / normalised-selector rebinds synced back.
            state.page = page
            state.selector = selector
        return None

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

    async def _check_submission_result(
        self, page: Page, url_before: str, submit_attempted: bool = True,
    ) -> tuple[bool, str]:
        """Returns (success, description). Verifies a form submission went through.

        Delegates to the shared ``verify_submission`` helper. The T33 fix lives
        there: an ATS that redirects the tab back to its own listing / careers
        page after a submit (Rippling ``…/jobs?page=0``, and similar for
        Greenhouse / Ashby / Lever) counts as success — but only with
        corroboration (``submit_attempted`` true, no error banner, no submit
        button left), not on the redirect alone.
        """
        return await verify_submission(
            page, url_before=url_before, submit_attempted=submit_attempted,
        )

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

    async def _handle_auth_page(self, page: Page) -> bool | str:
        """
        Handles a 3rd-party login or registration page.
        Checks stored credentials first; falls back to registering a new account.
        Returns True if we appear to be authenticated afterward, "failed" if
        stored credentials exist for this domain but the login attempt itself
        failed (transient/2FA/rate-limit — retryable), or "blocked" if no
        stored/discoverable credentials exist for this domain at all (needs a
        human, not auto-retryable).
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
            print(f"  [Auth] Login failed with stored credentials for {domain} — marking failed")
            return "failed"

        print(f"  [Auth] No stored credentials for {domain} — needs a human, "
              f"marking blocked (no auto-retry)")
        return "blocked"

    async def _try_login(self, page: Page, email: str, password: str) -> bool:
        """Fill login form and submit. Returns True if URL changed away from login page."""
        url_before = page.url
        try:
            # T51: Workday-specific automation ids tried first (well-documented, stable
            # Workday convention) — generic selectors remain the fallback for every other ATS.
            for sel in ('[data-automation-id="email"]', 'input[type="email"]', '#email',
                        'input[name*="email" i]', 'input[placeholder*="email" i]',
                        'input[name*="user" i]'):
                el = page.locator(sel).first
                if await el.count() > 0 and await el.is_visible():
                    await _human_type(el, email)
                    await asyncio.sleep(random.uniform(0.3, 0.7))
                    break
            for sel in ('[data-automation-id="password"]', 'input[type="password"]', '#password',
                        'input[name*="password" i]'):
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
            # Check if we're already on a registration page (has confirm-password field).
            # T51: Workday's own confirm-password field is data-automation-id="verifyPassword".
            confirm_field = page.locator(
                'input[name*="confirm" i], input[name*="repeat" i], '
                'input[placeholder*="confirm" i], [data-automation-id="verifyPassword"]'
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

            # If the site requires email verification, fetch the link and follow it
            if self.inbox and any(p in body for p in ("verify your email", "check your email")):
                print(f"  [Auth] Email verification required — checking inbox…")
                _vlink = await asyncio.to_thread(self.inbox.wait_for_link, domain)
                if _vlink:
                    print(f"  [Auth] Verification link found — navigating…")
                    try:
                        await page.goto(_vlink, wait_until="domcontentloaded", timeout=20000)
                        await asyncio.sleep(2)
                    except Exception:
                        pass
                else:
                    print(f"  [Auth] No verification email received in time — proceeding")

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
        # T51: Workday's create-account email field is data-automation-id="email" —
        # tried first, generic selectors remain the fallback for every other ATS.
        await _try(['[data-automation-id="email"]', 'input[type="email"]', '#email',
                    'input[name*="email" i]', 'input[placeholder*="email" i]'], p.get("email", ""))

        # T51: Workday exposes explicit automation ids for password + confirm-password
        # (data-automation-id="password" / "verifyPassword") — try those first so the
        # right field gets the right value even if DOM order ever differs from the
        # generic assumption below. Falls back to filling the first two
        # input[type=password] elements positionally (password, then confirm) for
        # every other ATS, unchanged from before.
        _wd_password = page.locator('[data-automation-id="password"]').first
        _wd_verify_password = page.locator('[data-automation-id="verifyPassword"]').first
        if await _wd_password.count() > 0 and await _wd_verify_password.count() > 0:
            for el in (_wd_password, _wd_verify_password):
                if await el.is_visible():
                    try:
                        await _human_type(el, pw)
                        await asyncio.sleep(random.uniform(0.2, 0.4))
                    except Exception:
                        pass
        else:
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

        # T51: Workday requires accepting Terms & Conditions before account creation
        # will succeed — data-automation-id="createAccountCheckbox" is a documented,
        # stable Workday convention. No generic equivalent is added here (a bare
        # "any checkbox on the page" selector risks ticking an unrelated marketing/
        # opt-in checkbox on other ATSes) — Workday-only, by design.
        _wd_checkbox = page.locator('[data-automation-id="createAccountCheckbox"]').first
        if await _wd_checkbox.count() > 0 and await _wd_checkbox.is_visible():
            try:
                if not await _wd_checkbox.is_checked():
                    await _wd_checkbox.check()
                    await asyncio.sleep(random.uniform(0.2, 0.4))
            except Exception:
                pass
