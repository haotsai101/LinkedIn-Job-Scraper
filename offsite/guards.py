"""Submit / Enter / upload guards for guard-mcp (OA5).

Two layers, so the agent **never** submits an application (design §4):

1. **MCP-level checks** (``SubmitGuard.check``, registered on ``GuardMCP``) give
   the model a clear ``BLOCKED by guard: …`` tool error for:
   * ``browser_click`` on a final-submit control — judged from the
     ``element`` description, the ``target`` string, and the accessible name of
     the ref in the latest snapshot (``observe`` keeps a ref → (role, name) map);
   * ``browser_press_key`` Enter (implicit form submission) and
     ``browser_type`` with ``submit: true``;
   * ``browser_file_upload`` of anything but the resume, or of the resume into a
     cover-letter field (judged from the click that opened the file chooser).
2. **In-page lock** (``offsite/page_lock.js``, an init script on the shared browser context,
   so every page and frame, surviving navigation). While locked it swallows, in
   capture phase, ``submit`` events, pointer/mouse/click events on submit
   controls, Enter in text inputs, and ``form.submit()`` / ``requestSubmit()``
   — whoever triggers them. It shows a ``role=status`` toast, so the model's
   next snapshot says the submit was blocked. This layer is authoritative:
   it holds even when the MCP check misjudges a label.

The page is **locked by default** on every new document; ``unlock()`` is only
called by the orchestrator when the human review starts (OA11), and it survives
same-tab navigations (``sessionStorage``) until ``lock()``.

Residual risk, documented: a site whose *non*-submit-looking button (e.g.
"Continue" on the last page) sends the application via ``fetch`` is not caught
by either layer. The human review screen (OA11) and live QA (OA13) watch for it.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import mcp_types as types

from offsite.browser import OffsiteBrowser
from offsite.guard_mcp import GuardMCP

# Final-submit wording. Deliberately *not* bare "apply": "Apply" / "Apply for
# this job" buttons open the form on listing pages (Workday, Greenhouse). A
# bare "Apply" that submits is caught by the in-page lock, which can see
# whether the control is a form's submit button.
_SUBMIT_WORDS = re.compile(
    r"\b(submit|send (my |your )?application|complete (my |your )?application|"
    r"finish (my |your )?application|apply now)\b",
    re.I,
)
_COVER = re.compile(r"cover[\s_-]*letter", re.I)
_ENTER_KEYS = {"enter", "numpadenter", "return"}

# Clickable roles in a Playwright MCP snapshot that can submit a form.
_BUTTON_ROLES = {"button", "menuitem"}
_SNAPSHOT_LINE = re.compile(
    r'^\s*- (?P<role>[a-z]+) "(?P<name>(?:[^"\\]|\\.)*)"[^\n]*?\[ref=(?P<ref>[a-z0-9]+)\]',
    re.M,
)


def is_submit_label(text: str) -> bool:
    """True for final-submit wording ("Submit application", "Send application",
    "Save and submit" — conservative: submit wording wins over next/save words),
    False for step navigation ("Next", "Save and continue", "Apply" on a listing)."""
    return bool(text) and bool(_SUBMIT_WORDS.search(text))


# ── in-page lock ───────────────────────────────────────────────────────────────
LOCK_JS = (Path(__file__).resolve().parent / "page_lock.js").read_text(encoding="utf-8")


class SubmitGuard:
    """Installs both guard layers on a ``GuardMCP`` + ``OffsiteBrowser`` pair.

        sg = SubmitGuard(browser, resume_path=profile["resume_path"])
        await sg.install(guard)      # page locked from here on
        ...agent runs...
        await sg.unlock()            # human review (OA11)
        await sg.lock()              # before the agent acts again
    """

    def __init__(self, browser: OffsiteBrowser, *, resume_path: str | Path | None) -> None:
        self.browser = browser
        self.resume_path = Path(resume_path).resolve() if resume_path else None
        self._refs: dict[str, tuple[str, str]] = {}   # ref → (role, accessible name)
        self._last_click: str = ""                     # description + name of the last click

    # ── install / lock ────────────────────────────────────────────────────────
    async def install(self, guard: GuardMCP) -> None:
        guard.add_check(self.check)
        guard.add_observer(self.observe)
        guard.add_refusal_listener(self._on_refusal)
        ctx = self.browser.context
        if ctx is None:
            raise RuntimeError("browser not started")
        await ctx.add_init_script(LOCK_JS)   # every new document, every frame
        await self._each_frame(LOCK_JS)      # documents that are already loaded
        await self.lock()

    async def lock(self) -> None:
        await self._each_frame("() => window.__oaSetLock && window.__oaSetLock(true)")

    async def unlock(self) -> None:
        await self._each_frame("() => window.__oaSetLock && window.__oaSetLock(false)")

    async def show(self, message: str) -> None:
        """Put ``message`` on the active page's guard banner (for the human watching)."""
        try:
            await self.browser.page.evaluate(
                "(m) => window.__oaBanner && window.__oaBanner(m)", message)
        except Exception:
            pass  # navigating / no page: the model still got the tool error

    async def _on_refusal(self, name: str, args: dict[str, Any], reason: str) -> None:
        # the model-facing reason ends with instructions for the model; the human
        # watching the browser only needs what was refused and why
        if reason.startswith("STOP: "):
            await self.show("agent stopped — " + reason.removeprefix("STOP: ").split(".")[0])
            return
        what = str(args.get("element") or args.get("key") or "").strip()
        await self.show(f"agent {name.removeprefix('browser_')}"
                        + (f" '{what}'" if what else "") + " refused — "
                        + reason.split(" — ")[0])

    async def is_locked(self) -> bool:
        """Locked state of the active page's main frame."""
        return bool(await self.browser.page.evaluate(
            "() => window.__oaIsLocked ? window.__oaIsLocked() : false"))

    async def _each_frame(self, script: str) -> None:
        for page in self.browser.context.pages:
            for frame in page.frames:
                try:
                    await frame.evaluate(script)
                except Exception:
                    pass  # detached / navigating frame: the init script covers its next document

    # ── MCP layer ─────────────────────────────────────────────────────────────
    def _name_of(self, target: str) -> str:
        role_name = self._refs.get(target)
        return role_name[1] if role_name else ""

    async def check(self, name: str, args: dict[str, Any]) -> str | None:
        if name == "browser_click":
            desc = str(args.get("element", ""))
            target = str(args.get("target", ""))
            ref_name = self._name_of(target)
            for text in (ref_name, desc, target if target not in self._refs else ""):
                if is_submit_label(text):
                    return ("clicking a final submit button is reserved for the human "
                            "reviewer — when every field is filled, call report_ready instead")
            self._last_click = f"{desc} {ref_name}".strip()
            return None
        if name == "browser_press_key":
            if str(args.get("key", "")).strip().lower() in _ENTER_KEYS:
                return ("Enter can submit the form — click the field / option you want "
                        "instead of pressing Enter")
            return None
        if name == "browser_type" and args.get("submit"):
            return "type without submit: true (pressing Enter can submit the form)"
        if name == "browser_file_upload":
            paths = args.get("paths") or []
            if not paths:
                return None  # cancels the file chooser
            if self.resume_path is None:
                return "no resume is configured, so no file may be uploaded"
            if any(Path(p).resolve() != self.resume_path for p in paths):
                return f"the only file you may upload is the resume: {self.resume_path}"
            if _COVER.search(self._last_click):
                return ("never upload the resume into a cover-letter field — leave cover "
                        "letter fields empty (cancel the file chooser with paths: [])")
            return None
        return None

    async def observe(self, name: str, args: dict[str, Any],
                      result: types.CallToolResult) -> None:
        for c in result.content:
            text = getattr(c, "text", None)
            if text and "[ref=" in text:
                for m in _SNAPSHOT_LINE.finditer(text):
                    self._refs[m["ref"]] = (m["role"], m["name"].replace('\\"', '"'))
