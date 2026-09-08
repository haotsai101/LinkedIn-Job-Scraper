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

import contextlib
import hashlib
import json
import os
import tempfile
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
    cookies and are gitignored (``storage_state*``).
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
        from playwright.sync_api import Error as PWError
        from playwright.sync_api import TimeoutError as PWTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - playwright is a hard dep in prod
        raise LinkedInLoginError(
            "playwright is not installed — run `pip install -e .` and "
            "`playwright install chromium`"
        ) from exc

    try:
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
    except LinkedInLoginError:
        raise
    except PWError as exc:
        # Most common on a fresh box: the Chromium binary isn't installed, so
        # p.chromium.launch() raises a bare playwright Error. Don't let that
        # traceback escape a Dagster op / _reauth — surface the fix.
        raise LinkedInLoginError(
            f"Playwright could not run the LinkedIn login for {email!r}: {exc} "
            "(if the browser binary is missing, run `playwright install chromium`)"
        ) from exc

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
    # This file is a live session credential: create it 0600 from the start
    # (mkstemp does that) and swap it in atomically, so a reader never sees a
    # half-written or briefly world-readable file.
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f"{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(state))
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def csrf_token(session: requests.Session) -> str:
    """LinkedIn's CSRF token is the ``JSESSIONID`` cookie value with quotes
    stripped. Iterates the jar (``.get('JSESSIONID')`` raises ``CookieConflictError``
    when the same name exists on multiple domains, which happens with a real
    ``storage_state``)."""
    for cookie in session.cookies:
        if cookie.name == "JSESSIONID":
            return (cookie.value or "").strip('"')
    raise LinkedInLoginError("session has no JSESSIONID cookie — state file is stale")


def cookie_header(session: requests.Session) -> str:
    """``Cookie:`` header value for a Voyager request — one crumb per name.

    A real Playwright ``storage_state`` carries several cookies (``JSESSIONID``,
    ``bcookie``, ``lidc``, ``lang`` …) on *both* ``.linkedin.com`` and
    ``.www.linkedin.com``. Iterating the jar yields every crumb, which would
    repeat names in the header (``JSESSIONID=x; …; JSESSIONID=x``). Every
    LinkedIn cookie domain matches the Voyager host ``www.linkedin.com``, so we
    collapse to a single crumb per name. The jar iterates grouped by domain, so
    ``.www.linkedin.com`` (the more specific host match) is seen last and wins —
    matching what a browser would send, and de-duped like the old Selenium
    ``session.cookies.set(name, value)`` loop.
    """
    crumbs: dict[str, str] = {}
    for cookie in session.cookies:
        crumbs[cookie.name] = cookie.value
    return "; ".join(f"{name}={value}" for name, value in crumbs.items())


def session_from_storage_state(path: str | os.PathLike) -> requests.Session:
    """Build an authenticated ``requests.Session`` from a Playwright
    ``storage_state`` JSON. **No browser launch.**

    Cookies are loaded with their domain / path so ``requests`` sends them to
    ``www.linkedin.com``. Nothing is put on ``session.headers`` — the retrievers
    build a complete per-request header dict (``scripts/fetch.py:_make_headers``)
    and populating session defaults here would reorder the on-the-wire header
    keys (a known anti-bot fingerprint). Raises :class:`LinkedInLoginError` early
    if the file has no usable ``JSESSIONID`` (the caller re-authenticates).
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
    csrf_token(session)  # fail fast if the state file has no JSESSIONID
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
