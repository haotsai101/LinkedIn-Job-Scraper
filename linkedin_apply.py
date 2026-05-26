"""
linkedin_apply.py — Deterministic Playwright LinkedIn job application engine.

EasyApplyFlow   : LinkedIn Easy Apply modal (SimpleOnsiteApply, ComplexOnsiteApply)
OffsiteApplyFlow: External company career site (OffsiteApply)
"""

import asyncio
import json
from datetime import datetime, timezone
from openai import OpenAI
from playwright.async_api import Page, BrowserContext

_AUTH_HOSTPATHS = ("/login", "/checkpoint", "/uas/")

# ── JavaScript field extractors ────────────────────────────────────────────────

_EASY_APPLY_FIELDS_JS = """
(function() {
    var modal = document.querySelector('.jobs-easy-apply-modal') || document.body;
    var results = [];
    var radioNames = new Set();

    modal.querySelectorAll(
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

def _get_profile_value(profile: dict, label: str) -> str | None:
    """Map a form field label to a profile value. Returns None if no confident match."""
    l = label.lower()
    p = profile

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
    if any(k in l for k in ("city", "location", "where are you", "address")):
        return p.get("location")
    if any(k in l for k in ("current title", "job title", "current role", "current position")):
        return p.get("current_title")
    if any(k in l for k in ("years of experience", "years experience", "total experience")):
        return str(p.get("years_experience", ""))
    if any(k in l for k in ("sponsor", "sponsorship", "visa support", "work visa")):
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
        resp = llm_client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=100,
        )
        answer = resp.choices[0].message.content.strip().strip('"').strip("'")
        return answer if answer else None
    except Exception:
        return None


async def _fill_field(page: Page, field: dict, value: str):
    """Fill a single form field. Non-fatal on error."""
    kind = field.get("kind", "text")
    el_id = field.get("id", "")
    name = field.get("name", "")

    try:
        if kind in ("text", "textarea", "email", "tel", "number", "url", "search", "password"):
            sel = f'#{el_id}' if el_id else f'[name="{name}"]'
            el = page.locator(sel).first
            if await el.count() > 0:
                await el.clear()
                await el.fill(value)

        elif kind == "select":
            sel = f'#{el_id}' if el_id else f'[name="{name}"]'
            el = page.locator(sel).first
            if await el.count() > 0:
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
    ):
        self.page = page
        self.profile = profile
        self.llm_client = llm_client
        self.model = model
        self.auto_mode = auto_mode
        self.callbacks = callbacks

    async def run(self, job_url: str) -> str:
        """Returns: 'applied' | 'expired' | 'already_applied' | 'skipped' | 'failed'"""
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

        if not await self._click_easy_apply():
            return "failed"

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

    async def _detect_job_status(self) -> str:
        try:
            if await self.page.locator("text=No longer accepting applications").count() > 0:
                return "expired"
            if (
                await self.page.locator('[aria-label*="Applied"]').count() > 0
                or await self.page.locator("text=Application submitted").count() > 0
            ):
                return "already_applied"
        except Exception:
            pass
        return "open"

    async def _click_easy_apply(self) -> bool:
        try:
            btn = self.page.locator('[aria-label*="Easy Apply"]').first
            if await btn.count() == 0:
                return False
            await btn.click()
            for sel in (".jobs-easy-apply-modal", "[data-test-modal-container]", '[role="dialog"]'):
                try:
                    await self.page.wait_for_selector(sel, timeout=5000)
                    return True
                except Exception:
                    continue
            return True  # assume it opened
        except Exception:
            return False

    async def _process_all_steps(self) -> str:
        last_fingerprint = ""
        repeated = 0

        for _ in range(15):
            await asyncio.sleep(1)
            await self._fill_current_step()
            await asyncio.sleep(0.5)

            submit = self.page.locator('[aria-label*="Submit application"]').first
            if await submit.count() > 0:
                return await self._handle_submit()

            review = self.page.locator('[aria-label*="Review your application"]').first
            if await review.count() > 0:
                await review.click()
                await asyncio.sleep(1)
                continue

            next_btn = self.page.locator('[aria-label*="Continue to next step"]').first
            if await next_btn.count() > 0:
                try:
                    fp = (await self.page.locator(".jobs-easy-apply-modal").inner_text())[:300]
                except Exception:
                    fp = self.page.url
                if fp == last_fingerprint:
                    repeated += 1
                    if repeated >= 3:
                        return "failed"
                else:
                    repeated = 0
                    last_fingerprint = fp
                await next_btn.click()
                await asyncio.sleep(1)
                continue

            # No recognized button
            try:
                modal_gone = await self.page.locator(".jobs-easy-apply-modal").count() == 0
            except Exception:
                modal_gone = True
            if modal_gone:
                return "failed"
            repeated += 1
            if repeated >= 3:
                return "failed"

        return "failed"

    async def _fill_current_step(self):
        try:
            raw = await self.page.evaluate(_EASY_APPLY_FIELDS_JS)
            fields = json.loads(raw) if raw else []
        except Exception:
            return

        for field in fields:
            if field.get("current_value") and field.get("kind") != "radio":
                continue  # skip already-filled fields
            value = _get_profile_value(self.profile, field.get("label", ""))
            if value is None:
                value = await _ask_llm(self.llm_client, self.model, self.profile, field)
            if value:
                await _fill_field(self.page, field, value)

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
                    await asyncio.sleep(2)
            except Exception:
                pass
            return "applied"
        return "skipped"


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

    async def run(self, job_url: str) -> str:
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

        ext_page = await self._click_apply_and_get_page()
        if ext_page is None:
            return "failed"

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

    async def _click_apply_and_get_page(self) -> Page | None:
        selectors = [
            '[aria-label*="Apply on company site"]',
            'button:has-text("Apply on company site")',
            'a:has-text("Apply on company site")',
            '[aria-label="Apply"]:not([aria-label*="Easy"])',
        ]
        btn = None
        for sel in selectors:
            candidate = self.page.locator(sel).first
            if await candidate.count() > 0:
                btn = candidate
                break

        if btn is None:
            return None

        try:
            async with self.context.expect_page() as new_page_info:
                await btn.click()
            new_page = await new_page_info.value
            await new_page.wait_for_load_state("domcontentloaded")
            return new_page
        except Exception:
            await asyncio.sleep(2)
            pages = self.context.pages
            if len(pages) > 1:
                return pages[-1]
            return self.page

    async def _fill_external_form(self, page: Page) -> str:
        account_saved = False

        for _ in range(10):
            await asyncio.sleep(2)

            try:
                raw = await page.evaluate(_OFFSITE_FIELDS_JS)
                fields = json.loads(raw) if raw else []
            except Exception:
                fields = []

            has_password = any(
                f.get("kind") == "password"
                or "password" in f.get("label", "").lower()
                for f in fields
            )

            for field in fields:
                kind = field.get("kind", "")
                label = field.get("label", "").lower()

                if kind == "password" or "password" in label:
                    value = self.generated_password
                elif "confirm" in label and kind == "password":
                    value = self.generated_password
                else:
                    value = _get_profile_value(self.profile, field.get("label", ""))
                    if value is None:
                        value = await _ask_llm(self.llm_client, self.model, self.profile, field)

                if value:
                    await _fill_field(page, field, value)

            # Save account credentials if a registration form was detected
            if has_password and not account_saved:
                save_fn = self.callbacks.get("save_account")
                if save_fn:
                    record = {
                        "created_at": datetime.now(timezone.utc).isoformat(),
                        "company": self.company_name or page.url.split("/")[2],
                        "website_url": page.url,
                        "email": self.profile.get("email", ""),
                        "password": self.generated_password,
                        "job_title": self.job_title,
                        "notes": "",
                    }
                    save_fn(record)
                    print(f"\n  [account] Credentials saved for {self.company_name or page.url}")
                    account_saved = True

            # Check for submit
            for submit_sel in (
                '[aria-label*="Submit"]',
                'button[type="submit"]:visible',
                'button:has-text("Submit")',
                'button:has-text("Send application")',
            ):
                submit_btn = page.locator(submit_sel).first
                if await submit_btn.count() > 0:
                    return await self._handle_submit(page, submit_btn)

            # Check for next/continue
            advanced = False
            for next_sel in (
                'button:has-text("Next")',
                'button:has-text("Continue")',
                'a:has-text("Next")',
            ):
                next_btn = page.locator(next_sel).first
                if await next_btn.count() > 0:
                    await next_btn.click()
                    advanced = True
                    break

            if not advanced:
                return "failed"

        return "failed"

    async def _handle_submit(self, page: Page, submit_btn) -> str:
        summary = (
            f"External application for {self.profile.get('full_name', 'user')} "
            f"at {self.company_name or page.url}."
        )
        ready = self.callbacks.get("ready_to_submit")
        result = ready(summary) if ready else "applied"

        if result == "applied":
            try:
                await submit_btn.click()
                await asyncio.sleep(2)
            except Exception:
                pass
            return "applied"
        return "skipped"
