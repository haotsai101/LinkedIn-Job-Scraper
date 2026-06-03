"""
claude_apply.py — Claude-powered Playwright script-generation engine for job application forms.

Usage:
    engine = ClaudeScriptApplyEngine(profile, context, company_name, job_title)
    result = await engine.apply(page)  # returns "applied" | "failed"

The engine:
1. Dumps the full page DOM, strips it down to form-relevant HTML
2. Asks Claude to generate a complete Playwright Python script
3. Executes that script in a sandboxed async exec
4. Repeats for up to 5 pages (multi-step forms)
"""

import asyncio
import json
import os
import re
from datetime import datetime, timezone
from html.parser import HTMLParser

import anthropic
from playwright.async_api import Page, BrowserContext


_LLM_LOG_PATH = "llm_debug.jsonl"


def _write_llm_log(entry: dict):
    with open(_LLM_LOG_PATH, "a") as f:
        f.write(json.dumps(entry) + "\n")


# ---------------------------------------------------------------------------
# strip_form_html
# ---------------------------------------------------------------------------

_KEEP_TAGS = frozenset({
    "form", "input", "select", "textarea", "button",
    "label", "fieldset", "legend", "option", "a",
})

_KEEP_ATTRS = frozenset({
    "id", "name", "type", "placeholder", "value", "required",
    "aria-label", "for", "data-field", "data-automation-id", "action",
    "href",  # kept temporarily so we can filter <a> by href content
})

_DROP_TAGS = frozenset({
    "script", "style", "svg", "img", "link", "meta", "head",
    "noscript", "iframe", "canvas", "video", "audio",
})


class _FormHTMLParser(HTMLParser):
    """Walk the DOM and keep only form-relevant elements and attributes."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []
        self._depth = 0            # indentation depth for kept tags
        self._skip_depth: int | None = None   # set when inside a _DROP_TAGS subtree

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _indent(self) -> str:
        return "  " * self._depth

    def _should_skip(self) -> bool:
        return self._skip_depth is not None

    # ------------------------------------------------------------------
    # HTMLParser callbacks
    # ------------------------------------------------------------------

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]):
        if self._should_skip():
            return

        if tag in _DROP_TAGS:
            self._skip_depth = self._depth
            return

        if tag not in _KEEP_TAGS:
            # Not a drop tag but not a keep tag either — pass through structurally
            # but don't emit HTML (we only want form elements)
            return

        attr_dict = dict(attrs)

        # For <a> tags, only keep if href contains "apply" or "submit"
        if tag == "a":
            href = attr_dict.get("href") or ""
            if not re.search(r"apply|submit", href, re.IGNORECASE):
                return

        # Filter attributes
        kept_attrs: list[str] = []
        for attr in _KEEP_ATTRS:
            if attr == "href" and tag != "a":
                continue  # href only relevant on <a>
            val = attr_dict.get(attr)
            if val is not None:
                # Escape double quotes inside attribute values
                val_escaped = val.replace('"', "&quot;")
                kept_attrs.append(f'{attr}="{val_escaped}"')

        attr_str = (" " + " ".join(kept_attrs)) if kept_attrs else ""
        self._out.append(f"{self._indent()}<{tag}{attr_str}>")
        self._depth += 1

    def handle_endtag(self, tag: str):
        if self._skip_depth is not None:
            if self._depth <= self._skip_depth or tag in _DROP_TAGS:
                self._skip_depth = None
            return

        if tag not in _KEEP_TAGS:
            return

        # Check for <a> — we may have skipped it without incrementing depth
        # Use a best-effort approach: only decrement if depth > 0
        if self._depth > 0:
            self._depth -= 1
        self._out.append(f"{self._indent()}</{tag}>")

    def handle_data(self, data: str):
        if self._should_skip():
            return
        text = data.strip()
        if text and self._depth > 0:
            self._out.append(f"{self._indent()}{text}")

    def result(self) -> str:
        return "\n".join(self._out)


def strip_form_html(html: str, max_chars: int = 40_000) -> str:
    """
    Strip a full page HTML down to form-relevant elements only.

    Keeps: form, input, select, textarea, button, label, fieldset, legend,
           option, and <a> tags whose href contains "apply" or "submit".
    Strips: style, class, event handlers, <script>, <style>, <svg>, <img>,
            <link>, <meta>, <head> and all other non-form tags.

    If the result exceeds max_chars it is truncated at a clean newline boundary
    and `<!-- truncated -->` is appended.
    """
    parser = _FormHTMLParser()
    parser.feed(html)
    stripped = parser.result()

    if len(stripped) <= max_chars:
        return stripped

    # Truncate at a clean newline boundary
    cutoff = stripped.rfind("\n", 0, max_chars)
    if cutoff == -1:
        cutoff = max_chars
    return stripped[:cutoff] + "\n<!-- truncated -->"


# ---------------------------------------------------------------------------
# exec_playwright_script
# ---------------------------------------------------------------------------

async def exec_playwright_script(
    page: Page,
    script: str,
    timeout_s: float = 60.0,
    company: str = "",
    job_title: str = "",
) -> dict:
    """
    Execute a Claude-generated Playwright script in a sandboxed async context.

    The script string should contain the *body* of an async function that
    receives `page` as its only argument.

    Returns {"success": bool, "error": str | None}.
    Also appends a log entry to llm_debug.jsonl.
    """
    success = False
    error_msg = None
    try:
        # Wrap the script body into a proper async function definition
        indented_lines = "\n".join(f"    {line}" for line in script.splitlines())
        wrapped = f"import asyncio\nasync def _gen(page):\n{indented_lines}\n"

        ns: dict = {"page": page, "asyncio": asyncio}
        exec(wrapped, ns)  # noqa: S102
        await asyncio.wait_for(ns["_gen"](page), timeout=timeout_s)
        success = True
    except asyncio.TimeoutError:
        error_msg = f"Script execution timed out after {timeout_s}s"
    except Exception as exc:
        error_msg = f"{type(exc).__name__}: {exc}"

    _write_llm_log({
        "ts": datetime.now(timezone.utc).isoformat(),
        "type": "claude_script_exec",
        "company": company,
        "job_title": job_title,
        "success": success,
        "error": error_msg,
        "script": script,
    })

    return {"success": success, "error": error_msg}


# ---------------------------------------------------------------------------
# ClaudeScriptApplyEngine
# ---------------------------------------------------------------------------

class ClaudeScriptApplyEngine:
    """
    Generate and execute a complete Playwright Python script per form page,
    using Claude as the code-generation backend.

    Parameters
    ----------
    profile:      dict from user_profile.json
    context:      Playwright BrowserContext (not directly used but stored for
                  potential future use, e.g. cookie persistence)
    company_name: Name of the company being applied to
    job_title:    Title of the job being applied for
    """

    _MAX_PAGES = 5

    def __init__(
        self,
        profile: dict,
        context: BrowserContext,
        company_name: str,
        job_title: str,
    ):
        self.profile = profile
        self.context = context
        self.company_name = company_name
        self.job_title = job_title
        self.client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))
        self.model = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def apply(self, page: Page) -> str:
        """
        Drive a job application form using Claude-generated Playwright scripts.

        Returns
        -------
        "applied"  — confirmation text or success URL detected
        "failed"   — exhausted retries or script execution failed
        """
        completed_pages: list[str] = []

        for page_num in range(self._MAX_PAGES):
            current_url = page.url
            print(f"  [Claude] Page {page_num + 1}: {current_url[:80]}")

            # 1. Dump DOM and strip to form HTML
            html = await page.content()
            stripped = strip_form_html(html)

            # 2. Build prompt and call Claude
            prompt = self._build_prompt(stripped, current_url, completed_pages)
            script = await self._call_claude(prompt, current_url, stripped)

            if not script:
                print("  [Claude] No script returned from Claude — aborting.")
                return "failed"

            # 3. Execute the generated script
            result = await exec_playwright_script(
                page,
                script,
                timeout_s=60.0,
                company=self.company_name,
                job_title=self.job_title,
            )

            # 4. Allow SPA a moment to render
            await asyncio.sleep(3)

            new_url = page.url

            # 5. Check for completion
            confirmed, reason = await self._check_done(page)
            if confirmed:
                print(f"  [Claude] Application confirmed: {reason}")
                return "applied"

            # 6. If script failed AND URL didn't change → bail
            if not result["success"] and new_url == current_url:
                print(f"  [Claude] Script failed and page didn't change: {result['error']}")
                return "failed"

            # 7. URL changed — record and continue
            if new_url != current_url:
                completed_pages.append(current_url)
                print(f"  [Claude] Navigated to: {new_url[:80]}")
            else:
                # URL didn't change — might be a SPA that updated in place
                # Still record the page as processed so we don't loop forever
                if current_url not in completed_pages:
                    completed_pages.append(current_url)

        print("  [Claude] Exhausted page limit without confirmation.")
        return "failed"

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _build_prompt(
        self,
        stripped_html: str,
        page_url: str,
        completed_pages: list[str],
    ) -> str:
        p = self.profile
        resume_abs = os.path.abspath(p.get("resume_path", ""))

        profile_block = f"""
Name:               {p.get('full_name', '')}
Email:              {p.get('email', '')}
Phone:              {p.get('phone', '')}
Location:           {p.get('location', '')}
Street address:     {p.get('street_address', '')}
City:               {p.get('city', '')}
State:              {p.get('state', '')}
ZIP:                {p.get('zip_code', '')}
Country:            {p.get('country', '')}
Current title:      {p.get('current_title', '')}
Years experience:   {p.get('years_experience', '')}
Work authorization: {p.get('work_authorization', '')}
Need sponsorship:   {p.get('need_sponsorship', '')}
LinkedIn URL:       {p.get('linkedin_url', '')}
GitHub URL:         {p.get('github_url', '')}
Portfolio URL:      {p.get('portfolio_url', '')}
Resume path:        {resume_abs}
Education:          {p.get('education', {}).get('degree', '')} in {p.get('education', {}).get('field', '')} from {p.get('education', {}).get('school', '')} ({p.get('education', {}).get('year', '')})
Summary:            {p.get('summary', '')}
Skills:             {', '.join(p.get('skills', []))}
Preferred salary:   {p.get('preferred_salary', '')}
Veteran status:     {p.get('veteran_status', '')}
Disability status:  {p.get('disability_status', '')}
Race:               {p.get('race', '')}
Gender:             {p.get('gender', '')}
Willing to relocate:{p.get('willing_to_relocate', '')}
""".strip()

        completed_block = ""
        if completed_pages:
            completed_block = (
                "\n\nAlready-completed pages (do NOT re-fill):\n"
                + "\n".join(f"  - {u}" for u in completed_pages)
            )

        return f"""You are an expert Playwright automation engineer helping to fill in a job application form.

## Applicant profile
{profile_block}

## Current page URL
{page_url}
{completed_block}

## Form HTML (stripped)
{stripped_html}

## Instructions
Write a single Python async function `run(page)` followed immediately by `await run(page)`.
The function must:
1. Fill ALL visible required form fields using the applicant profile above.
2. For file upload inputs, use:
       await page.locator(selector).set_input_files("{os.path.abspath(p.get('resume_path', ''))}")
3. Do NOT navigate to any other domain.
4. Do NOT click Cancel, Back, Close, Sign Out, or any destructive action.
5. After filling all fields, click the button that advances the form — Next, Continue, Submit, Apply,
   or equivalent. Only ONE such button click at the very end.
6. If a field already contains the correct value, skip it.
7. After any page navigation use:
       await page.wait_for_load_state("domcontentloaded", timeout=10000)
8. Use robust locators: prefer `page.locator('[name="..."]')`, `page.get_by_label("...")`,
   or `page.locator('#id')`. Fall back to `page.locator('text=...')` for buttons.
9. Use `await locator.fill(value)` for text inputs, `await locator.select_option(value)` for
   <select> elements, and `await locator.check()` for checkboxes/radio buttons.
10. For "decline to answer" / "prefer not to say" options in EEO fields, select those options.

Return ONLY valid Python code — no markdown fences, no explanation, no comments outside the code."""

    async def _call_claude(
        self,
        prompt: str,
        page_url: str,
        stripped_html: str,
    ) -> str | None:
        """Call the Claude API and return the generated script text."""
        try:
            message = self.client.messages.create(
                model=self.model,
                max_tokens=4096,
                messages=[{"role": "user", "content": prompt}],
            )
            script = message.content[0].text.strip() if message.content else ""

            # Strip markdown fences if Claude added them despite instructions
            script = re.sub(r"^```(?:python)?\s*\n?", "", script)
            script = re.sub(r"\n?```\s*$", "", script)
            script = script.strip()

            _write_llm_log({
                "ts": datetime.now(timezone.utc).isoformat(),
                "type": "claude_script_gen",
                "company": self.company_name,
                "job_title": self.job_title,
                "page_url": page_url,
                "stripped_html_chars": len(stripped_html),
                "script": script,
                "model": self.model,
                "input_tokens": getattr(message.usage, "input_tokens", None),
                "output_tokens": getattr(message.usage, "output_tokens", None),
            })

            return script or None
        except Exception as exc:
            _write_llm_log({
                "ts": datetime.now(timezone.utc).isoformat(),
                "type": "claude_script_gen",
                "company": self.company_name,
                "job_title": self.job_title,
                "page_url": page_url,
                "stripped_html_chars": len(stripped_html),
                "script": None,
                "error": f"{type(exc).__name__}: {exc}",
            })
            print(f"  [Claude] API error: {exc}")
            return None

    async def _check_done(self, page: Page) -> tuple[bool, str]:
        """
        Return (True, reason) if the page shows application-confirmation signals.
        Checks page body text and URL patterns.
        """
        _CONFIRM_PHRASES = (
            "thank you for applying",
            "thank you for your application",
            "application submitted",
            "application received",
            "successfully submitted",
            "has been submitted",
            "we received your application",
            "you have applied",
            "you've applied",
            "application is complete",
            "submission received",
        )
        _CONFIRM_URL_FRAGMENTS = ("/thank", "/confirmation", "/success", "/submitted")

        try:
            body_text: str = await page.evaluate(
                "(document.body && document.body.innerText || '').toLowerCase()"
            )
            for phrase in _CONFIRM_PHRASES:
                if phrase in body_text:
                    return True, f"found phrase: '{phrase}'"

            url_lower = page.url.lower()
            for fragment in _CONFIRM_URL_FRAGMENTS:
                if fragment in url_lower:
                    return True, f"URL contains '{fragment}'"

            return False, "no confirmation detected"
        except Exception as exc:
            return False, f"check_done error: {exc}"
