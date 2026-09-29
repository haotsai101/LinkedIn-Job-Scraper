"""
linkedin_apply.py — Deterministic Playwright LinkedIn job application engine.

EasyApplyFlow: LinkedIn Easy Apply modal (SimpleOnsiteApply, ComplexOnsiteApply)

OffsiteApplyFlow (external company career sites, OffsiteApply) was removed in
full — automation for that application_type is pending a from-scratch redesign.
apply_jobs.py now skips OffsiteApply jobs without touching their DB state.
"""

import asyncio
import json
import os
import random
import re
from collections.abc import Callable
from datetime import datetime, timezone
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
# Shared stdlib-only helper (ticket T4). ``_write_llm_log`` keeps its old private
# name as a thin alias so the ~6 internal call sites are untouched.
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


# ── Submission verification (used by EasyApplyFlow; general-purpose enough for
#    a future OffsiteApply redesign to reuse) ───────────────────────────────────

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

    Currently called only by ``EasyApplyFlow`` (pass ``modal_open`` from
    ``_is_modal_open()``, leave ``url_before`` unset — Easy Apply never
    navigates). Kept general-purpose — ``url_before`` / ``submit_attempted``
    were designed for a navigation-based flow (the removed ``OffsiteApplyFlow``;
    pass ``url_before``, ``modal_open=None``, and ``submit_attempted=False``
    when the caller claimed "done" without a submit click actually having
    happened) — so a from-scratch OffsiteApply redesign can reuse it as-is.

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
            "Write a professional 1-2 sentence answer for this job application field. "
            "Be concise and direct — no filler, no padding, no unnecessary elaboration. "
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
