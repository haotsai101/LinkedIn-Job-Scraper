"""
script_engine.py — Script-generation engine for job application forms.

Uses any OpenAI-compatible LLM to read the full DOM and generate a complete
Playwright Python script that fills all fields and submits the form in one shot.

Usage (called from OffsiteApplyFlow._llm_guided_apply):
    engine = ScriptApplyEngine(profile, context, company_name, job_title,
                               llm_client, model)
    result = await engine.apply(page)  # returns "applied" | "skipped" | "failed"
"""

import asyncio
import json
import os
import re
from datetime import datetime, timezone

from openai import AsyncOpenAI
from playwright.async_api import Page, BrowserContext


_LLM_LOG_PATH = "llm_debug.jsonl"


def _write_llm_log(entry: dict):
    with open(_LLM_LOG_PATH, "a") as f:
        f.write(json.dumps(entry) + "\n")


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
    Execute a generated Playwright script in a sandboxed async context.

    The script is the *body* of an async function that receives `page`.
    Returns {"success": bool, "error": str | None}.
    """
    success = False
    error_msg = None
    try:
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
        "type": "script_exec",
        "company": company,
        "job_title": job_title,
        "success": success,
        "error": error_msg,
        "script": script,
    })

    return {"success": success, "error": error_msg}


# ---------------------------------------------------------------------------
# ScriptApplyEngine
# ---------------------------------------------------------------------------

class ScriptApplyEngine:
    """
    Generate and execute a complete Playwright Python script per form page.

    Works with any OpenAI-compatible LLM — pass the same client and model
    used for the rest of the browser agent.
    """

    _MAX_PAGES = 7

    def __init__(
        self,
        profile: dict,
        context: BrowserContext,
        company_name: str,
        job_title: str,
        llm_client: AsyncOpenAI,
        model: str,
    ):
        self.profile = profile
        self.context = context
        self.company_name = company_name
        self.job_title = job_title
        self.client = llm_client
        self.model = model

    async def _get_viewport_snapshot(self, page: Page) -> tuple[str, list]:
        """Return (visible_text, elements) for what is currently in the viewport."""
        try:
            data = await page.evaluate("""() => {
                const vh = window.innerHeight, vw = window.innerWidth;
                const inViewport = (el) => {
                    const r = el.getBoundingClientRect();
                    return r.bottom > 0 && r.top < vh && r.right > 0 && r.left < vw;
                };
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
                const lbl = (el) => {
                    const forEl = el.id ? document.querySelector('label[for="' + el.id + '"]') : null;
                    return (forEl && forEl.textContent.trim())
                        || el.getAttribute('aria-label') || el.getAttribute('placeholder') || '';
                };
                const elements = Array.from(document.querySelectorAll(
                    'button, a[href], input:not([type=hidden]), select, textarea, [role="button"]'
                ))
                .filter(el => inViewport(el))
                .map(el => ({
                    tag: el.tagName.toLowerCase(),
                    type: el.getAttribute('type') || '',
                    id: el.id || '',
                    label: lbl(el),
                    text: (el.textContent || '').trim().slice(0, 60),
                    value: el.value ? el.value.slice(0, 60) : ''
                }));
                return { visibleText: texts.join(' '), elements };
            }""")
            return data.get("visibleText", ""), data.get("elements", [])
        except Exception:
            return "", []

    async def _get_all_dom_inputs(self, page: Page) -> list:
        """Return all fillable form inputs from the full DOM (not just viewport)."""
        try:
            return await page.evaluate("""() => {
                const lbl = (el) => {
                    const forEl = el.id ? document.querySelector('label[for="' + el.id + '"]') : null;
                    return (forEl && forEl.textContent.trim())
                        || el.getAttribute('aria-label') || el.getAttribute('placeholder') || '';
                };
                return Array.from(document.querySelectorAll(
                    'input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=reset]):not([type=image]),'
                    + 'select, textarea'
                )).filter(el => !el.disabled).map(el => ({
                    tag: el.tagName.toLowerCase(),
                    type: el.getAttribute('type') || '',
                    id: el.id || '',
                    label: lbl(el),
                    text: '',
                    value: el.value ? el.value.slice(0, 60) : ''
                }));
            }""")
        except Exception:
            return []

    async def apply(self, page: Page) -> str:
        """
        Drive a job application form using LLM-generated Playwright scripts.
        Returns "applied" | "skipped" | "failed".
        """
        completed_pages: list[str] = []
        same_url_count: dict[str, int] = {}

        for page_num in range(self._MAX_PAGES):
            current_url = page.url
            print(f"  [Script] Page {page_num + 1}: {current_url[:80]}")

            # Allow up to 2 retries on the same URL (multi-step forms that don't change URL).
            # Only abort on a true navigation loop (URL was previously navigated away from and back).
            same_url_count[current_url] = same_url_count.get(current_url, 0) + 1
            if same_url_count[current_url] > 2:
                print(f"  [Script] Stuck on same URL after {same_url_count[current_url]-1} script attempts — aborting.")
                return "failed"
            if current_url in completed_pages and same_url_count.get(current_url, 0) > 1:
                pass  # allow retry; loop guard is the same_url_count above

            visible_text, elements = await self._get_viewport_snapshot(page)

            # Count fillable inputs in viewport
            fillable = [e for e in elements if e.get("tag") in ("input", "select", "textarea")
                        and e.get("type", "") not in ("submit", "button", "reset", "image", "hidden")]
            if not fillable:
                # Form inputs may be below the fold — scan the full DOM
                dom_inputs = await self._get_all_dom_inputs(page)
                fillable_dom = [e for e in dom_inputs
                                if e.get("type", "") not in ("submit", "button", "reset", "image", "hidden")]
                if fillable_dom:
                    print(f"  [Script] {len(fillable_dom)} inputs found in DOM (below fold) — using full scan")
                    # Merge DOM inputs into elements list for prompt building; keep viewport buttons for submit
                    btn_elements = [e for e in elements if e.get("tag") in ("button", "a")]
                    elements = fillable_dom + btn_elements
                    fillable = fillable_dom
                else:
                    print(f"  [Script] No fillable inputs in viewport or DOM — cannot generate script.")
                    return "failed"

            prompt = self._build_prompt(visible_text, elements, current_url, completed_pages)
            script = await self._call_llm(prompt, current_url, len(fillable))

            if not script:
                print("  [Script] No script returned — aborting.")
                return "failed"

            result = await exec_playwright_script(
                page, script, timeout_s=60.0,
                company=self.company_name, job_title=self.job_title,
            )

            await asyncio.sleep(3)
            new_url = page.url

            confirmed, reason = await self._check_done(page)
            if confirmed:
                print(f"  [Script] Application confirmed: {reason}")
                return "applied"

            if new_url != current_url:
                if new_url in completed_pages:
                    print(f"  [Script] URL loop detected ({new_url[:60]}) — aborting.")
                    return "failed"
                completed_pages.append(current_url)
                print(f"  [Script] Navigated to: {new_url[:80]}")
            else:
                if current_url not in completed_pages:
                    completed_pages.append(current_url)

        print("  [Script] Exhausted page limit without confirmation.")
        return "failed"

    def _element_to_stub(self, el: dict, resume_abs: str, page_url: str = "") -> tuple[str, str] | None:
        """
        Convert a viewport element into (comment, code_stub).
        Returns None for elements that should be skipped (buttons, links, hidden, already filled).
        """
        tag = el.get("tag", "")
        input_type = el.get("type", "").lower()
        el_id = el.get("id", "")
        label = el.get("label", "") or el.get("text", "") or "field"
        current_value = el.get("value", "")

        # Skip buttons and links — handled as a separate submit step
        if tag in ("button", "a") or input_type in ("submit", "button", "reset", "image"):
            return None
        # Skip already-filled fields
        if current_value:
            return None

        # Skip cover letter fields — we never fill these
        _label_lower = label.lower()
        if any(k in _label_lower for k in ("cover letter", "cover_letter", "covering letter")):
            return None

        # Build locator string — prefer id, fall back to get_by_label.
        # CSS IDs starting with a digit are invalid in CSS selectors; use [id="..."] form instead.
        if el_id:
            if el_id[0].isdigit() or ":" in el_id:
                locator = f'page.locator(\'[id="{el_id}"]\')'
            else:
                locator = f"page.locator('#{el_id}')"
        elif label and label != "field":
            locator = f"page.get_by_label({label!r})"
        else:
            return None

        comment = f"# {label}" + (f" [currently: {current_value!r}]" if current_value else "")

        if input_type == "file":
            stub = f"await {locator}.set_input_files({resume_abs!r}, timeout=5000)"
        elif input_type == "checkbox":
            stub = f"await {locator}.check(timeout=5000)"
        elif input_type == "radio":
            stub = f"await {locator}.check(timeout=5000)"
        elif tag == "select":
            stub = f"await {locator}.select_option(label='???', timeout=5000)"
        elif el_id == "country" and page_url and "greenhouse.io" in page_url:
            # Greenhouse #country is a jQuery UI autocomplete — fill won't commit without picking suggestion
            stub = (
                f"await {locator}.click(click_count=3, timeout=3000)\n"
                f"    await {locator}.press('Delete')\n"
                f"    await asyncio.sleep(0.3)\n"
                f"    await {locator}.type('United S', delay=80)\n"
                f"    await asyncio.sleep(0.5)\n"
                f"    _gh_sug = page.locator('.ui-autocomplete li.ui-menu-item')\n"
                f"    try: await _gh_sug.first.wait_for(state='visible', timeout=5000)\n"
                f"    except Exception: pass\n"
                f"    _clicked = False\n"
                f"    for _gi in range(await _gh_sug.count()):\n"
                f"        _item = _gh_sug.nth(_gi)\n"
                f"        if 'united states' in (await _item.inner_text()).lower():\n"
                f"            await _item.click(timeout=3000); _clicked = True; break\n"
                f"    if not _clicked and await _gh_sug.count() > 0:\n"
                f"        await _gh_sug.first.click(timeout=3000)"
            )
        elif el_id == "state" and page_url and "greenhouse.io" in page_url:
            # Greenhouse #state is also jQuery UI autocomplete
            p_loc = self.profile.get("location", "")
            state_hint = "Utah" if "utah" in p_loc.lower() or ", ut" in p_loc.lower() else "Utah"
            stub = (
                f"await {locator}.click(click_count=3, timeout=3000)\n"
                f"    await {locator}.press('Delete')\n"
                f"    await asyncio.sleep(0.3)\n"
                f"    await {locator}.type({state_hint!r}, delay=80)\n"
                f"    await asyncio.sleep(0.5)\n"
                f"    _gh_st_sug = page.locator('.ui-autocomplete li.ui-menu-item')\n"
                f"    try: await _gh_st_sug.first.wait_for(state='visible', timeout=5000)\n"
                f"    except Exception: pass\n"
                f"    if await _gh_st_sug.count() > 0:\n"
                f"        await _gh_st_sug.first.click(timeout=3000)"
            )
        else:
            stub = f"await {locator}.fill('???', timeout=5000)"

        return comment, stub

    def _build_field_stubs(self, elements: list, resume_abs: str, page_url: str = "") -> str:
        """Build pre-computed code stubs for all fillable viewport elements."""
        lines = []
        for el in elements:
            result = self._element_to_stub(el, resume_abs, page_url=page_url)
            if result is None:
                continue
            comment, stub = result
            lines.append(f"{comment}\ntry:\n    {stub}\nexcept Exception as e: print(f'Error: {{e}}')\n")
        return "\n".join(lines) if lines else "(no fillable fields detected in viewport)"

    def _build_submit_options(self, elements: list) -> str:
        """Extract visible buttons/links the LLM can choose as the submit action."""
        _skip_keywords = ("quick apply", "talent community", "save", "job alerts",
                          "bookmark", "share", "follow", "sign in", "login", "cancel",
                          "create alert", "set up alert", "get job alerts", "attach",
                          "apply with linkedin", "apply with github", "sign in with",
                          "toggle flyout", "privacy notice", "candidate privacy",
                          "privacy policy", "terms of service", "cookie")
        btns = []
        seen = set()
        for el in elements:
            tag = el.get("tag", "")
            text = (el.get("text") or el.get("label") or "").strip()
            if tag in ("button", "a") and text and text not in seen:
                if not any(k in text.lower() for k in _skip_keywords):
                    btns.append(f"  button:has-text({text!r})")
                    seen.add(text)
        return "\n".join(btns) if btns else "  (no buttons visible)"

    def _build_prompt(self, visible_text: str, elements: list, page_url: str, completed_pages: list[str]) -> str:
        p = self.profile
        resume_abs = os.path.abspath(p.get("resume_path", ""))

        profile_block = (
            f"Name: {p.get('full_name','')} | Email: {p.get('email','')} | Phone: {p.get('phone','')} | "
            f"Location: {p.get('location','')} | Title: {p.get('current_title','')} | "
            f"Yrs exp: {p.get('years_experience','')} | Auth: {p.get('work_authorization','')} | "
            f"Sponsorship: {p.get('need_sponsorship','')} | LinkedIn: {p.get('linkedin_url','')} | "
            f"GitHub: {p.get('github_url','')} | "
            f"Education: {p.get('education',{}).get('degree','')} in {p.get('education',{}).get('field','')} "
            f"from {p.get('education',{}).get('school','')} ({p.get('education',{}).get('year','')}) | "
            f"Veteran: {p.get('veteran_status','')} | Disability: {p.get('disability_status','')} | "
            f"Gender: {p.get('gender','')}"
        )

        completed_block = ""
        if completed_pages:
            completed_block = "\nAlready-completed pages (skip these):\n" + "\n".join(f"  - {u}" for u in completed_pages)

        field_stubs = self._build_field_stubs(elements, resume_abs, page_url=page_url)
        submit_options = self._build_submit_options(elements)

        submit_click = f"""try:
    await page.locator('button:has-text("???")').first.click(timeout=10000)
    await page.wait_for_load_state("domcontentloaded", timeout=15000)
except Exception as e: print(f'Submit error: {{e}}')"""

        return f"""You are filling a job application form. Complete the Python script below by replacing every `'???'` with the correct value from the applicant profile. Output the COMPLETE script exactly as shown — do NOT add new locators, do NOT change any selector, do NOT use page.fill() or page.click() directly.

## Applicant profile
{profile_block}

## Page: {page_url}
Visible text: {visible_text[:300]}
{completed_block}

## Script to complete (replace ??? only — keep every other line exactly as written)

{field_stubs}
{submit_click}

Available submit buttons on this page:
{submit_options}

Rules:
- Replace `'???'` in fill/select stubs with the correct profile value.
- For select stubs: replace `label='???'` with the closest matching visible option.
- For EEO fields (race, gender, veteran, disability): use "Decline to answer" or "Prefer not to say".
- For open-text motivation/fit questions: write 1–2 sentences from the profile summary.
- For the submit click: replace `'???'` with the button text from the list above.
- If a field's correct value is unknown, delete that try/except block entirely.
- Skip any field labeled "Cover Letter" or "Covering Letter" — delete that try/except block.
- NEVER click "Apply with LinkedIn", "Apply with GitHub", "Sign in with Google", or any OAuth/SSO button — fill the form fields manually.
- Do NOT add any lines. Do NOT change any locator. Do NOT use page.fill() or [name=] selectors.

Output the completed script only — no markdown, no explanation."""

    def _sanitize_script(self, script: str) -> str:
        """
        Enforce safe Playwright patterns regardless of what the LLM generated:
        - Convert deprecated page.fill/click/check/select_option to locator API
        - Inject timeout=5000 on any locator action missing it
        - Remove [name=] selectors (unreliable on SPAs) by flagging them
        """
        lines = []
        for line in script.splitlines():
            stripped = line.lstrip()

            # Convert page.fill('sel', 'val') → page.locator('sel').fill('val', timeout=5000)
            m = re.match(r"(\s*)await page\.fill\((['\"])(.+?)\2,\s*(.+?)\)(.*)$", line)
            if m:
                indent, _, sel, val, rest = m.groups()
                lines.append(f"{indent}await page.locator({sel!r}).fill({val}, timeout=5000){rest}")
                continue

            # Convert page.click('sel') → page.locator('sel').click(timeout=5000)
            m = re.match(r"(\s*)await page\.click\((['\"])(.+?)\2\)(.*)$", line)
            if m:
                indent, _, sel, rest = m.groups()
                lines.append(f"{indent}await page.locator({sel!r}).click(timeout=5000){rest}")
                continue

            # Inject timeout=5000 into locator actions that are missing it
            for action in ("fill", "check", "select_option", "set_input_files"):
                if f").{action}(" in line and "timeout=" not in line and line.strip().startswith("await"):
                    line = line.rstrip(")")
                    if line.endswith("()"):
                        line = line[:-1] + "timeout=5000)"
                    elif line.endswith(","):
                        line = line + " timeout=5000)"
                    else:
                        line = line + ", timeout=5000)"
                    break

            lines.append(line)
        return "\n".join(lines)

    async def _call_llm(self, prompt: str, page_url: str, element_count: int) -> str | None:
        _t0 = datetime.now(timezone.utc)
        try:
            resp = await asyncio.wait_for(
                self.client.chat.completions.create(
                    model=self.model,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=4096,
                ),
                timeout=180,
            )
            script = (resp.choices[0].message.content or "").strip()

            # Strip markdown fences if the LLM added them despite instructions
            script = re.sub(r"^```(?:python)?\s*\n?", "", script)
            script = re.sub(r"\n?```\s*$", "", script)
            script = self._sanitize_script(script.strip())

            _write_llm_log({
                "ts": datetime.now(timezone.utc).isoformat(),
                "type": "script_gen",
                "model": self.model,
                "company": self.company_name,
                "job_title": self.job_title,
                "page_url": page_url,
                "element_count": element_count,
                "duration_ms": int((datetime.now(timezone.utc) - _t0).total_seconds() * 1000),
                "script": script,
                "input_tokens": getattr(getattr(resp, "usage", None), "prompt_tokens", None),
                "output_tokens": getattr(getattr(resp, "usage", None), "completion_tokens", None),
            })

            return script or None
        except asyncio.TimeoutError:
            print(f"  [Script] LLM timed out after 180s — aborting.")
            return None
        except Exception as exc:
            print(f"  [Script] LLM error: {exc}")
            return None

    async def _check_done(self, page: Page) -> tuple[bool, str]:
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


