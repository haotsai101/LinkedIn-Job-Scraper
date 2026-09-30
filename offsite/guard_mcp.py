"""guard-mcp: the only door between a model and the browser (OA4).

Every model (NIM via the OpenAI Agents SDK, Claude via the Agent SDK) talks to
this MCP server — never to Playwright directly. It:

* spawns **one** Playwright MCP (``@playwright/mcp``, pinned) as a stdio child,
  attached with ``--cdp-endpoint`` to the shared ``OffsiteBrowser`` (OA2), so
  both models drive the same page the human is looking at;
* re-exposes only an **allowlist** of its tools. Code execution
  (``browser_evaluate``, ``browser_run_code_unsafe``), file drops
  (``browser_drop`` — would bypass the upload guard), ``browser_close`` and the
  rest are hidden, and refused if called anyway;
* **inlines post-action snapshots**: Playwright MCP 0.0.83 answers click /
  type / ... with a *link* to a snapshot file instead of the snapshot, which a
  model without file tools can't read. Its output goes to a private temp dir
  (never the repo) and guard-mcp replaces each link with the YAML itself, so
  the model sees the page after every action (validation errors included);
* **types like a person**: ``browser_type`` / ``browser_fill_form`` text is
  typed character by character, Normal(0.2 s, 0.1 s) apart
  (``offsite/human_typing.py``) instead of being set in one go;
* runs **pre-call checks** and **post-call observers** (OA5 guards, OA6
  accounting plug in via ``add_check`` / ``add_observer``), serves **local
  tools** (OA6 ``report_ready`` / ``request_human`` via ``add_local_tool``) and
  logs every call to ``llm_debug.jsonl``;
* serves streamable HTTP on 127.0.0.1 **inside the orchestrator's process**, so
  guard state is plain Python.

Design: docs/NEW_AGENTIC_APPLY_PLAN.md §2, §4.

    async with OffsiteBrowser() as browser, GuardMCP(browser) as guard:
        guard.url   # → http://127.0.0.1:<port>/mcp  (give this to the model's SDK)

CLI (manual check with the MCP Inspector):
    python -m offsite.guard_mcp --url <page url> [--port N] [--headless]
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import re
import shutil
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import mcp_types as types
import uvicorn
from mcp import Client, StdioServerParameters
from mcp.server.lowlevel import Server

from common import write_llm_log
from offsite.browser import OffsiteBrowser, free_port
from offsite.human_typing import HumanTyping

_REPO_ROOT = Path(__file__).resolve().parents[1]

# Pinned: tool names / arguments change between releases (0.0.83 renamed
# browser_run_code → browser_run_code_unsafe and added browser_drop).
PLAYWRIGHT_MCP_PACKAGE = "@playwright/mcp@0.0.83"

ALLOWED_TOOLS: frozenset[str] = frozenset({
    "browser_navigate",
    "browser_navigate_back",
    "browser_snapshot",
    "browser_find",
    "browser_click",
    "browser_type",
    "browser_fill_form",
    "browser_select_option",
    "browser_press_key",
    "browser_file_upload",
    "browser_wait_for",
    "browser_hover",
    "browser_handle_dialog",
    "browser_tabs",
    "browser_take_screenshot",
})

BLOCKED_PREFIX = "BLOCKED by guard: "

# Models sometimes copy a ref with its snapshot wrapper: "[ref=e9]" / "ref=e9" → "e9".
_WRAPPED_REF = re.compile(r"^\s*\[?\s*ref\s*=\s*([a-z0-9]+)\s*\]?\s*$")


def normalize_targets(args: dict[str, Any]) -> dict[str, Any]:
    """Unwrap ``[ref=e9]``-style targets (top level and ``fields[*].target``)."""
    def fix(v):
        m = _WRAPPED_REF.match(v) if isinstance(v, str) else None
        return m.group(1) if m else v
    out = dict(args)
    if "target" in out:
        out["target"] = fix(out["target"])
    if isinstance(out.get("fields"), list):
        out["fields"] = [{**f, "target": fix(f.get("target"))} if isinstance(f, dict)
                         and "target" in f else f for f in out["fields"]]
    return out


_SNAPSHOT_LINK = re.compile(r"- \[Snapshot\]\((/[^)\n]+\.yml)\)")

# A pre-call check returns None to allow the call, or a one-line reason to refuse it.
Check = Callable[[str, dict[str, Any]], Awaitable[str | None]]
# An observer sees every forwarded call and its (snapshot-inlined) result.
Observer = Callable[[str, dict[str, Any], types.CallToolResult], Awaitable[None]]
# Told about every refusal (tool, args, reason) — OA5 mirrors them onto the page.
RefusalListener = Callable[[str, dict[str, Any], str], Awaitable[None]]
# Told when a call was cancelled by the client before it finished (tool, args).
CancelListener = Callable[[str, dict[str, Any]], None]
# A local tool is served by guard-mcp itself (OA6 control tools), never forwarded.
LocalHandler = Callable[[dict[str, Any]], Awaitable[types.CallToolResult]]


def _error(text: str) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)], is_error=True)


def _log_args(args: dict[str, Any]) -> dict[str, Any]:
    return {k: (v[:200] if isinstance(v, str) else v) for k, v in args.items()}


class GuardMCP:
    """Allowlisting MCP proxy in front of Playwright MCP, served over streamable HTTP."""

    def __init__(
        self,
        browser: OffsiteBrowser,
        *,
        port: int | None = None,
        playwright_mcp_args: list[str] | None = None,
        typing: HumanTyping | None | bool = True,
    ) -> None:
        self.browser = browser
        # human-paced typing for browser_type / browser_fill_form (True → env / defaults;
        # None or False → Playwright MCP's instant fill)
        self.typing = HumanTyping.from_env() if typing is True else (typing or None)
        self.port = port or free_port()
        self.url = f"http://127.0.0.1:{self.port}/mcp"
        self._extra_args = playwright_mcp_args or []
        self._checks: list[Check] = []
        self._observers: list[Observer] = []
        self._local: dict[str, tuple[types.Tool, LocalHandler]] = {}
        self._refusal_listeners: list[RefusalListener] = []
        self._cancel_listeners: list[CancelListener] = []
        # One browser, one action at a time: a model may send tool calls in parallel,
        # and two human-paced typing calls would fight over keyboard focus.
        self._browser_lock = asyncio.Lock()
        self._stack = contextlib.AsyncExitStack()
        self._upstream: Client | None = None
        self._tools: list[types.Tool] = []
        self._uvicorn: uvicorn.Server | None = None
        self._serve_task: asyncio.Task | None = None
        self._output_dir: Path | None = None
        self.calls = 0  # every call that reached call_tool, allowed or not

    # ── lifecycle ─────────────────────────────────────────────────────────────
    async def __aenter__(self) -> GuardMCP:
        try:
            await self._start()
        except BaseException:
            await self._stack.aclose()
            raise
        return self

    async def __aexit__(self, *exc) -> None:
        if self._uvicorn is not None:
            self._uvicorn.should_exit = True
        if self._serve_task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._serve_task
        await self._stack.aclose()

    async def _start(self) -> None:
        self._output_dir = Path(tempfile.mkdtemp(prefix="guard-mcp-")).resolve()
        self._stack.callback(shutil.rmtree, self._output_dir, ignore_errors=True)
        params = StdioServerParameters(
            command="npx",
            args=["-y", PLAYWRIGHT_MCP_PACKAGE, "--cdp-endpoint", self.browser.cdp_endpoint,
                  "--output-dir", str(self._output_dir), "--file-paths", "absolute",
                  *self._extra_args],
            cwd=str(_REPO_ROOT),  # Playwright MCP only reads files under its workspace root
        )
        self._upstream = await self._stack.enter_async_context(Client(params))
        upstream_tools = (await self._upstream.list_tools()).tools
        self._tools = [t for t in upstream_tools if t.name in ALLOWED_TOOLS]
        missing = ALLOWED_TOOLS - {t.name for t in self._tools}
        if missing:
            raise RuntimeError(
                f"{PLAYWRIGHT_MCP_PACKAGE} no longer provides {sorted(missing)} — "
                "update ALLOWED_TOOLS after checking the new tool list"
            )

        server = Server("guard", on_list_tools=self._on_list_tools,
                        on_call_tool=self._on_call_tool)
        app = server.streamable_http_app(host="127.0.0.1")
        self._uvicorn = uvicorn.Server(uvicorn.Config(
            app, host="127.0.0.1", port=self.port, log_level="warning", lifespan="on"))
        self._serve_task = asyncio.create_task(self._uvicorn.serve())
        for _ in range(100):
            if self._uvicorn.started:
                return
            if self._serve_task.done():
                self._serve_task.result()  # re-raise the startup error
            await asyncio.sleep(0.05)
        raise RuntimeError(f"guard-mcp did not start on port {self.port}")

    # ── extension points (OA5 / OA6) ──────────────────────────────────────────
    def add_check(self, check: Check, *, first: bool = False) -> None:
        """Register a pre-call check; the first non-None reason refuses the call.
        ``first=True`` runs it before the others (OA6 accounting must see calls
        that a later guard refuses)."""
        if first:
            self._checks.insert(0, check)
        else:
            self._checks.append(check)

    def add_refusal_listener(self, listener: RefusalListener) -> None:
        """Called after a call is refused (errors in the listener are ignored)."""
        self._refusal_listeners.append(listener)

    def add_cancel_listener(self, listener: CancelListener) -> None:
        """Called when the client cancels a call before it finished."""
        self._cancel_listeners.append(listener)

    def add_local_tool(self, tool: types.Tool, handler: LocalHandler) -> None:
        """Serve ``tool`` from guard-mcp itself (listed alongside the allowlist)."""
        self._local[tool.name] = (tool, handler)

    def add_observer(self, observer: Observer) -> None:
        """Register a post-call observer (runs only for calls that were forwarded)."""
        self._observers.append(observer)

    @property
    def tool_names(self) -> list[str]:
        return sorted([t.name for t in self._tools] + list(self._local))

    # ── MCP handlers ──────────────────────────────────────────────────────────
    async def _on_list_tools(self, ctx, params) -> types.ListToolsResult:
        return types.ListToolsResult(tools=self._tools + [t for t, _ in self._local.values()])

    async def _on_call_tool(self, ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
        return await self.call(params.name, dict(params.arguments or {}))

    async def call(self, name: str, args: dict[str, Any]) -> types.CallToolResult:
        """Guarded call — what the MCP handler runs. Also usable in-process."""
        self.calls += 1
        args = normalize_targets(args)
        t0 = time.monotonic()
        entry: dict[str, Any] = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                 "source": "guard_mcp", "tool": name, "args": _log_args(args)}
        try:
            if name not in ALLOWED_TOOLS and name not in self._local:
                reason = f"tool {name!r} is not available"
            else:
                reason = None
                for check in self._checks:
                    reason = await check(name, args)
                    if reason:
                        break
            if reason:
                entry["blocked"] = reason
                for listener in self._refusal_listeners:
                    with contextlib.suppress(Exception):
                        await listener(name, args, reason)
                return _error(BLOCKED_PREFIX + reason)
            if name in self._local:
                result = await self._local[name][1](args)
                entry["is_error"] = bool(result.is_error)
                entry["local"] = True
                return result
            async with self._browser_lock:
                result = await self._forward(name, args)
            for observer in self._observers:
                await observer(name, args, result)
            entry["is_error"] = bool(result.is_error)
            entry["result_chars"] = sum(len(getattr(c, "text", "") or "") for c in result.content)
            return result
        except asyncio.CancelledError:
            entry["cancelled"] = True           # the client gave up on this call
            for listener in self._cancel_listeners:
                with contextlib.suppress(Exception):
                    listener(name, args)
            raise
        except Exception as e:  # upstream crash → a tool error the model can see
            entry["exception"] = f"{type(e).__name__}: {e}"
            return _error(f"tool {name} failed: {type(e).__name__}: {e}")
        finally:
            entry["ms"] = int((time.monotonic() - t0) * 1000)
            write_llm_log(entry)


    async def _upstream_call(self, name: str, args: dict[str, Any]) -> types.CallToolResult:
        assert self._upstream is not None
        return self._inline_snapshots(await self._upstream.call_tool(name, args))

    async def _forward(self, name: str, args: dict[str, Any]) -> types.CallToolResult:
        if self.typing is not None and name in ("browser_type", "browser_fill_form"):
            return await self.typing.handle(name, args, self._upstream_call, self.browser)
        return await self._upstream_call(name, args)

    def _inline_snapshots(self, result: types.CallToolResult) -> types.CallToolResult:
        """Replace ``- [Snapshot](<file>.yml)`` links with the file's YAML (only
        for files inside our own output dir), then delete the file."""
        def repl(m: re.Match) -> str:
            path = Path(m.group(1)).resolve()
            if self._output_dir is None or not path.is_relative_to(self._output_dir):
                return m.group(0)
            try:
                yaml = path.read_text(encoding="utf-8")
            except OSError:
                return m.group(0)
            path.unlink(missing_ok=True)
            return f"```yaml\n{yaml.rstrip()}\n```"

        for c in result.content:
            if isinstance(c, types.TextContent) and "[Snapshot](" in c.text:
                c.text = _SNAPSHOT_LINK.sub(repl, c.text)
        return result


def _profile_resume() -> str | None:
    import json

    try:
        rel = json.loads((_REPO_ROOT / "user_profile.json").read_text()).get("resume_path")
    except (OSError, ValueError):
        return None
    return str((_REPO_ROOT / rel).resolve()) if rel else None


async def _main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="python -m offsite.guard_mcp")
    ap.add_argument("--url", required=True, help="page to open first")
    ap.add_argument("--port", type=int, default=8812)
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--profile-dir", default=None)
    ap.add_argument("--resume", default=None,
                    help="the only file uploads may use (default: user_profile.json resume_path)")
    ap.add_argument("--no-guards", action="store_true",
                    help="OA4 pass-through only: skip the OA5 submit / Enter / upload guards")
    args = ap.parse_args(argv)

    kw = {"headless": args.headless}
    browser = OffsiteBrowser(args.profile_dir, **kw) if args.profile_dir else OffsiteBrowser(**kw)
    async with browser, GuardMCP(browser, port=args.port) as guard:
        from offsite.control import RunControl  # control/guards import this module

        control = RunControl()
        control.install(guard)
        if not args.no_guards:
            from offsite.guards import SubmitGuard

            resume = args.resume or _profile_resume()
            await SubmitGuard(browser, resume_path=resume).install(guard)
            print(f"guards    : ON (page locked; uploads limited to {resume})")
        await browser.open(args.url)
        print(f"guard-mcp : {guard.url}   (streamable HTTP)")
        print(f"browser   : {browser.cdp_endpoint}   (CDP)")
        print(f"tools     : {', '.join(guard.tool_names)}")
        print("Inspector : npx @modelcontextprotocol/inspector  → Transport 'Streamable HTTP', "
              "URL above")
        await asyncio.get_running_loop().run_in_executor(None, input, "Press Enter to stop. ")
        print(f"outcome   : {control.outcome} ({control.calls} calls"
              + (f", {control.stop_reason}" if control.stop_reason else "") + ")")
        if control.outcome == "human":
            print(f"human     : {control.human_reason} — {control.human_detail}")
        for a in control.answers:
            flag = "⚠" if a.sensitive or a.confidence < 0.7 else " "
            print(f"  {flag} {a.field_label}: {a.answer!r} ({a.source}, {a.confidence:.2f})")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(sys.argv[1:])))
