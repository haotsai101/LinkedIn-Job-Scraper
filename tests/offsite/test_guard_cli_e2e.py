"""End-to-end: the real ``python -m offsite.guard_mcp`` CLI, driven over MCP.

This is the OA4–OA6 manual "How to test" walkthrough (what you'd click in the
MCP Inspector), automated: a model-like client fills ``multipage.html`` through
guard-mcp, every submit path is refused, the page banner tells the human
watching what was refused, a click → snapshot → same-click loop is stopped,
``report_ready`` still lands, and nothing reaches ``/__submissions``.
Needs npx + Playwright Chromium (else skipped). ~10 s.
"""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest
from mcp import Client
from playwright.async_api import async_playwright

from offsite.browser import free_port
from tests.fixtures.offsite.serve import fixture_server

REPO = Path(__file__).resolve().parents[2]
RESUME = REPO / "tests" / "fixtures" / "offsite" / "dummy_resume.pdf"


def _t(r) -> str:
    return "".join(getattr(c, "text", "") or "" for c in r.content)


def _ref(snap: str, role: str, name: str) -> str:
    m = re.search(rf'{role} "{re.escape(name)}"[^\n]*?\[ref=([a-z0-9]+)\]', snap)
    assert m, f"{role} {name!r} not in snapshot:\n{snap[:2000]}"
    return m.group(1)


@pytest.fixture(scope="module")
def cli(tmp_path_factory):
    if shutil.which("npx") is None:
        pytest.skip("npx (Node) not installed")
    with fixture_server() as base:
        port = free_port()
        proc = subprocess.Popen(
            [sys.executable, "-m", "offsite.guard_mcp", "--url", f"{base}/multipage.html",
             "--headless", "--port", str(port), "--resume", str(RESUME),
             "--profile-dir", str(tmp_path_factory.mktemp("cli-profile"))],
            cwd=REPO, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True)
        lines: list[str] = []
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            lines.append(line)
            if line.startswith("browser   :"):
                break
        if not lines or not lines[-1].startswith("browser   :"):
            proc.kill()
            pytest.skip("guard-mcp CLI did not start: " + "".join(lines)[-500:])
        cdp = lines[-1].split()[2]
        try:
            yield {"base": base, "url": f"http://127.0.0.1:{port}/mcp", "cdp": cdp,
                   "proc": proc, "header": "".join(lines)}
        finally:
            if proc.poll() is None:
                proc.communicate("\n", timeout=30)


def _count(base) -> int:
    with urllib.request.urlopen(f"{base}/__submissions") as r:
        return json.loads(r.read())["count"]


async def _banner(cdp: str) -> str:
    async with async_playwright() as pw:
        br = await pw.chromium.connect_over_cdp(cdp)
        page = br.contexts[0].pages[-1]
        el = await page.query_selector("#__oa_guard_toast")
        return await el.inner_text() if el else ""


def test_cli_walkthrough(cli):
    assert "guards    : ON" in cli["header"]

    async def go():
        async with Client(cli["url"]) as c:
            names = {t.name for t in (await c.list_tools()).tools}
            assert len(names) == 17 and {"report_ready", "request_human"} <= names
            assert not names & {"browser_evaluate", "browser_run_code_unsafe", "browser_drop"}
            assert "not available" in _t(await c.call_tool("browser_evaluate",
                                                           {"function": "() => 1"}))

            # OA4: Next on the empty form answers with the inlined post-action snapshot
            snap = _t(await c.call_tool("browser_snapshot", {}))
            r = await c.call_tool("browser_click", {"element": "Next",
                                                    "target": _ref(snap, "button", "Next")})
            assert "```yaml" in _t(r) and "Please fix" in _t(r) and "[Snapshot](" not in _t(r)

            # step 1, filled like a model would
            snap = _t(await c.call_tool("browser_snapshot", {}))
            for label, val in (("First name *", "Ada"), ("Last name *", "Lovelace"),
                               ("Email *", "ada@example.com")):
                await c.call_tool("browser_type", {"element": label, "text": val,
                                                   "target": _ref(snap, "textbox", label)})
            await c.call_tool("browser_select_option", {
                "element": "Country", "target": _ref(snap, "combobox", "Country *"),
                "values": ["United States"]})

            # OA5: Enter / submit:true refused, and the human sees it on the page
            r = await c.call_tool("browser_press_key", {"key": "Enter"})
            assert r.is_error and "Enter" in _t(r)
            assert "agent press_key 'Enter' refused" in await _banner(cli["cdp"])
            r = await c.call_tool("browser_type", {"element": "Email", "text": "x", "submit": True,
                                                   "target": _ref(snap, "textbox", "Email *")})
            assert r.is_error and "submit: true" in _t(r)

            # OA5: uploads — resume only, never into the cover letter
            await c.call_tool("browser_click", {"element": "Resume / CV",
                                                "target": _ref(snap, "button", "Resume / CV *")})
            assert "only file" in _t(await c.call_tool("browser_file_upload",
                                                       {"paths": ["/etc/hosts"]}))
            r = await c.call_tool("browser_file_upload", {"paths": [str(RESUME)]})
            assert not r.is_error, _t(r)
            snap = _t(await c.call_tool("browser_snapshot", {}))
            r = await c.call_tool("browser_click", {"element": "Next",
                                                    "target": _ref(snap, "button", "Next")})
            assert "Step 2 of 3" in _t(r)
            snap = _t(r)
            await c.call_tool("browser_click", {
                "element": "Cover letter (optional)",
                "target": _ref(snap, "button", "Cover letter (optional)")})
            assert "cover-letter" in _t(await c.call_tool("browser_file_upload",
                                                          {"paths": [str(RESUME)]}))
            assert not (await c.call_tool("browser_file_upload", {"paths": []})).is_error

            # step 2 → 3
            snap = _t(await c.call_tool("browser_snapshot", {}))
            await c.call_tool("browser_type", {
                "element": "years", "text": "4", "target": _ref(
                    snap, "textbox",
                    "How many years of professional Python experience do you have? *")})
            snap = _t(await c.call_tool("browser_snapshot", {}))
            for ref in re.findall(r'radio "Yes"[^\n]*?\[ref=([a-z0-9]+)\]', snap):
                await c.call_tool("browser_click", {"element": "Yes", "target": ref})
            snap = _t(await c.call_tool("browser_snapshot", {}))
            r = await c.call_tool("browser_click", {"element": "Next",
                                                    "target": _ref(snap, "button", "Next")})
            assert "Step 3 of 3" in _t(r)
            box = re.search(r"checkbox[^\n]*?\[ref=([a-z0-9]+)\]", _t(r)).group(1)
            await c.call_tool("browser_click", {"element": "certify", "target": box})

            # OA5 + OA6: submit refused; click → snapshot → same click = loop stop
            sub = _ref(_t(await c.call_tool("browser_snapshot", {})), "button",
                       "Submit Application")
            r1 = await c.call_tool("browser_click", {"element": "Submit Application",
                                                     "target": sub})
            b1 = await _banner(cli["cdp"])
            assert r1.is_error and "agent click 'Submit Application' refused" in b1
            await c.call_tool("browser_snapshot", {})
            r2 = await c.call_tool("browser_click", {"element": "Submit Application",
                                                     "target": sub})
            b2 = await _banner(cli["cdp"])
            assert "STOP:" in _t(r2) and "agent stopped" in b2 and b2 != b1

            # OA6: report_ready still lands after a loop stop; then the run is over
            r = await c.call_tool("report_ready", {"answers": [
                {"field_label": "Email", "answer": "ada@example.com", "source": "profile",
                 "confidence": 1, "evidence": ["email"], "sensitive": False},
                {"field_label": "Sponsorship", "answer": "Yes", "source": "profile",
                 "confidence": 1, "evidence": ["need_sponsorship"], "sensitive": True}]})
            assert not r.is_error and "Recorded 2 answers (1 flagged" in _t(r)
            r = await c.call_tool("request_human", {"reason": "login", "detail": "x"})
            assert r.is_error and "already called report_ready" in _t(r)

    asyncio.run(go())
    assert _count(cli["base"]) == 0

    out, _ = cli["proc"].communicate("\n", timeout=30)
    assert "outcome   : ready" in out
    assert "⚠ Sponsorship: 'Yes'" in out
