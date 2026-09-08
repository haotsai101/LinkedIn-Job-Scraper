"""LinkedIn session bootstrap for the scraper (T17 PR 2).

Replaces the old Selenium ``create_session`` in ``scripts/fetch.py``. The Selenium
path launched a real Chrome/Edge window on every retriever construction and hung
indefinitely when ChromeDriver stalled on ``driver.get`` (a 120s ReadTimeout that
crashed the whole discovery run). This module does two separable things:

* ``login_and_save_state`` — a one-time Playwright headless login that writes a
  ``storage_state`` JSON (cookies + origins) to disk. This is the *only* path
  that launches a browser.
* ``session_from_storage_state`` — builds an authenticated ``requests.Session``
  from that JSON with **no browser launch**. This is the hot path.

``get_session`` ties them together: reuse the state file if present, otherwise
log in once. A valid state file therefore means zero browser launches — that is
the T17 acceptance criterion.

Why not share the apply agent's login (``apply_jobs.login_linkedin_playwright``):
that one drives a *headful, persistent* context and blocks on ``input()`` for
CAPTCHA / 2-FA by design (a human is watching). The scraper runs unattended and
headless, so it needs a login that fails fast with a clear message instead of
hanging. The two are deliberately kept separate; this module is scraper-scoped.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import requests
from requests.cookies import create_cookie

# Kept byte-for-byte in sync with the header block the retrievers send on Voyager
# calls (scripts/fetch.py). Changing the UA / client version without matching the
# retriever headers gets the account flagged faster.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/117.0.0.0 Safari/537.36"
)
X_LI_TRACK = (
    '{"clientVersion":"1.13.5589","mpVersion":"1.13.5589","osName":"web",'
    '"timezoneOffset":-7,"timezone":"America/Los_Angeles",'
    '"deviceFormFactor":"DESKTOP","mpName":"voyager-web","displayDensity":1,'
    '"displayWidth":360,"displayHeight":800}'
)

_LOGIN_URL = "https://www.linkedin.com/checkpoint/rm/sign-in-another-account"
# Any of these path fragments in the URL means we're still on an auth / challenge
# screen (login not complete).
_AUTH_PATHS = ("/checkpoint", "/login", "/challenge", "/security", "/uas/")
# Hard ceiling on any single navigation / wait. The old Selenium code had no
# navigation timeout at all — that is exactly how it hung.
_NAV_TIMEOUT_MS = 30_000


class LinkedInLoginError(RuntimeError):
    """A Playwright login attempt did not reach the logged-in state.

    Typically a 2-FA / CAPTCHA / checkpoint challenge that needs a human, or bad
    credentials. Raised instead of hanging."""


def state_path_for(email: str, base_dir: str | os.PathLike | None = None) -> Path:
    """Per-account ``storage_state`` file path.

    ``storage_state_<sha1(email)[:12]>.json`` — next to ``linkedin_jobs.db`` by
    default, or under ``$LINKEDIN_STATE_DIR`` / ``base_dir`` if set. The email is
    lower-cased and stripped before hashing so ``A@x.com`` and ``a@x.com`` share
    one file. The filename carries no PII (hash only); the file contents are live
    cookies and are gitignored (``storage_state*.json``).
    """
    base = Path(base_dir or os.environ.get("LINKEDIN_STATE_DIR") or ".")
    digest = hashlib.sha1(email.strip().lower().encode()).hexdigest()[:12]
    return base / f"storage_state_{digest}.json"


def _playwright_login(email: str, password: str, *, headless: bool) -> dict:
    """Drive a headless Playwright Chromium through the LinkedIn login form and
    return the resulting ``storage_state`` dict.

    Split out from :func:`login_and_save_state` so tests can stub the browser
    without touching the file-writing / validation logic.
    """
    try:
        from playwright.sync_api import TimeoutError as PWTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - playwright is a hard dep in prod
        raise LinkedInLoginError(
            "playwright is not installed — run `pip install -e .` and "
            "`playwright install chromium`"
        ) from exc

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        try:
            context = browser.new_context(user_agent=USER_AGENT)
            page = context.new_page()
            page.set_default_navigation_timeout(_NAV_TIMEOUT_MS)
            page.set_default_timeout(_NAV_TIMEOUT_MS)

            page.goto(_LOGIN_URL, wait_until="domcontentloaded")
            page.fill("#username", email)
            page.fill("#password", password)
            page.click('button.btn__primary--large[type="submit"]')

            try:
                page.wait_for_url(
                    lambda url: "linkedin.com" in url
                    and not any(frag in url for frag in _AUTH_PATHS),
                    timeout=_NAV_TIMEOUT_MS,
                )
            except PWTimeoutError as exc:
                raise LinkedInLoginError(
                    f"login for {email!r} did not complete within "
                    f"{_NAV_TIMEOUT_MS // 1000}s — still on {page.url!r} "
                    f"(2-FA / CAPTCHA / checkpoint?)"
                ) from exc

            state = context.storage_state()
        finally:
            browser.close()

    return state


def login_and_save_state(
    email: str,
    password: str,
    path: str | os.PathLike,
    *,
    headless: bool | None = None,
) -> None:
    """Log in once via Playwright and persist ``storage_state`` to ``path``.

    Launches a browser. Callers should prefer :func:`get_session`, which only
    calls this when the state file is missing / unusable.

    ``headless`` defaults to True; set ``LINKEDIN_LOGIN_HEADFUL=1`` (or pass
    ``headless=False``) to watch / hand-solve a challenge.
    """
    if headless is None:
        headless = os.environ.get("LINKEDIN_LOGIN_HEADFUL", "") not in ("1", "true", "yes")

    state = _playwright_login(email, password, headless=headless)

    if not any(c.get("name") == "li_at" for c in state.get("cookies", [])):
        raise LinkedInLoginError(
            f"login for {email!r} produced no li_at cookie — treating as failed "
            f"(2-FA / CAPTCHA / bad credentials?)"
        )

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # 0600 — this file is a live session credential.
    path.write_text(json.dumps(state))
    try:
        path.chmod(0o600)
    except OSError:  # pragma: no cover - non-POSIX / weird fs
        pass


def csrf_token(session: requests.Session) -> str:
    """LinkedIn's CSRF token is the ``JSESSIONID`` cookie value with quotes
    stripped. Iterates the jar (``.get('JSESSIONID')`` raises ``CookieConflictError``
    when the same name exists on multiple domains, which happens with a real
    ``storage_state``)."""
    for cookie in session.cookies:
        if cookie.name == "JSESSIONID":
            return (cookie.value or "").strip('"')
    raise LinkedInLoginError("session has no JSESSIONID cookie — state file is stale")


def _voyager_headers(session: requests.Session) -> dict:
    """The static Voyager header block, with ``Csrf-Token`` derived from the
    session's ``JSESSIONID``. Kept in parity with the per-request header dicts
    the retrievers build (scripts/fetch.py) — no extra headers vs. the old
    Selenium path. Cookies ride on the session jar."""
    return {
        "Accept": "application/vnd.linkedin.normalized+json+2.1",
        "Accept-Language": "en-US,en;q=0.9",
        "Csrf-Token": csrf_token(session),
        "User-Agent": USER_AGENT,
        "X-Li-Track": X_LI_TRACK,
    }


def session_from_storage_state(path: str | os.PathLike) -> requests.Session:
    """Build an authenticated ``requests.Session`` from a Playwright
    ``storage_state`` JSON. **No browser launch.**

    Cookies are loaded with their domain / path so ``requests`` sends them to
    ``www.linkedin.com``; the Voyager headers (incl. ``Csrf-Token``) are set on
    the session. Raises :class:`LinkedInLoginError` if the file is missing a
    usable ``JSESSIONID`` (the caller re-authenticates).
    """
    path = Path(path)
    data = json.loads(path.read_text())

    session = requests.Session()
    for c in data.get("cookies", []):
        session.cookies.set_cookie(
            create_cookie(
                name=c["name"],
                value=c["value"],
                domain=c.get("domain", ".linkedin.com"),
                path=c.get("path", "/"),
                secure=c.get("secure", False),
            )
        )
    session.headers.update(_voyager_headers(session))  # raises if no JSESSIONID
    return session


def get_session(
    email: str,
    password: str,
    path: str | os.PathLike | None = None,
) -> requests.Session:
    """Return an authenticated ``requests.Session`` for ``email``.

    * state file present and usable  -> load it, **no browser**
    * state file missing / unusable  -> one Playwright login, then load

    This is the T17 acceptance path: a valid ``storage_state`` means zero browser
    launches.
    """
    path = Path(path) if path is not None else state_path_for(email)

    if path.exists():
        try:
            return session_from_storage_state(path)
        except (LinkedInLoginError, ValueError, KeyError, OSError) as exc:
            print(f"[linkedin_auth] stored state {path} unusable ({exc}) — re-authenticating")

    login_and_save_state(email, password, path)
    return session_from_storage_state(path)
