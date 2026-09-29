"""Local fixture server for the offsite agent (OA3).

    python -m tests.fixtures.offsite.serve [--port 8811]

Serves the fixture forms in this directory and records submissions:

* ``GET  /<name>.html``   — a fixture form (``greenhouse``, ``ashby``, ``multipage``)
* ``GET  /``              — index of fixtures
* ``POST /__submitted``   — where every fixture form posts; counted, then a
                            "Thank you — application received (ref FIX-<n>)" page
* ``GET  /__submissions`` — ``{"count": n, "last": {"path":..., "fields": [...]}}``
* ``POST /__reset``       — zero the counter

Guard tests (OA5) assert ``count`` stays 0 while the agent runs. Only field
*names* of a submission are kept, never values.

In tests: ``with fixture_server() as base_url: ...``.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import re
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

HERE = Path(__file__).resolve().parent
FIXTURES = sorted(p.stem for p in HERE.glob("*.html"))


class _State:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.count = 0
        self.last: dict | None = None


def _field_names(body: bytes, content_type: str) -> list[str]:
    if "multipart/form-data" in content_type:
        names = re.findall(rb'Content-Disposition: form-data; name="([^"]+)"', body)
        return sorted({n.decode(errors="replace") for n in names})
    return sorted(parse_qs(body.decode(errors="replace")).keys())


def _make_handler(state: _State):
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(HERE), **kw)

        def log_message(self, *a):
            pass

        def _send(self, code: int, body: str, ctype: str = "text/html; charset=utf-8"):
            b = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/__submissions":
                with state.lock:
                    self._send(200, json.dumps({"count": state.count, "last": state.last}),
                               "application/json")
                return
            if path == "/":
                links = "".join(f'<li><a href="/{n}.html">{n}</a></li>' for n in FIXTURES)
                self._send(200, f"<h1>Offsite fixtures</h1><ul>{links}</ul>")
                return
            if not path.endswith(".html"):
                self._send(404, "not found")
                return
            super().do_GET()

        def do_POST(self):
            path = self.path.split("?")[0]
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            if path == "/__reset":
                with state.lock:
                    state.count, state.last = 0, None
                self._send(200, '{"ok": true}', "application/json")
                return
            if path == "/__submitted":
                fields = _field_names(body, self.headers.get("Content-Type", ""))
                with state.lock:
                    state.count += 1
                    n = state.count
                    state.last = {"referer": self.headers.get("Referer"), "fields": fields}
                self._send(200, "<h1>Thank you — application received</h1>"
                                f"<p>Confirmation: FIX-{n:04d}</p>")
                return
            self._send(404, "not found")

    return Handler


def make_server(port: int = 0) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("127.0.0.1", port), _make_handler(_State()))


@contextlib.contextmanager
def fixture_server(port: int = 0):
    """Run the fixture server in a thread; yields its base URL."""
    srv = make_server(port)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m tests.fixtures.offsite.serve")
    ap.add_argument("--port", type=int, default=8811)
    args = ap.parse_args()
    srv = make_server(args.port)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    print(f"Offsite fixtures on {base}/")
    for n in FIXTURES:
        print(f"  {base}/{n}.html")
    print(f"  submissions: {base}/__submissions")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
