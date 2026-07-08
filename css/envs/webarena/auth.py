"""Login state for the WebArena farm — the ``auto_login.py`` equivalent.

Upstream WebArena ships ``browser_env/auto_login.py``: it drives a real UI login
per site and dumps Playwright ``storage_state`` to ``.auth/<combo>_state.json``.
Every task config then names the file it needs (``"require_login": true,
"storage_state": "./.auth/shopping_admin_state.json"``). Without it, more than
half the benchmark is unreachable — ``mutate`` is 52% of train / 63% of test and
almost every mutate task acts as a logged-in user.

Two mechanisms, chosen per site by what the image supports:

* **shopping_admin — HTTP header.** The ServiceNow images bake in
  ``WebArena\\AutoLogin\\Plugin\\AutoLoginPlugin`` which reads
  ``X-M2-Admin-Auto-Login: <user>:<pass>`` on adminhtml requests and calls
  Magento's ``Auth::login()``. Header auth survives container recreation for
  free — no cookie to regenerate after a lane refresh.
* **shopping / reddit / gitlab — UI login → storage_state.** Same flow as
  upstream, but stored per (stack, site) rather than per site-combination: a
  cookie is only valid against the container that issued it, and a lane refresh
  destroys that server-side session. Multi-site tasks merge the states of the
  sites they pinned, which is strictly more flexible than upstream's ten
  precomputed combos.

The credentials are the benchmark's fixed public singletons (they ship in
upstream's ``env_config.py``); they are secrets in name only.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any

_log = logging.getLogger("css.webarena")

# Header-authenticated sites: no cookie, no regeneration after refresh.
ADMIN_AUTO_LOGIN_HEADER = "X-M2-Admin-Auto-Login"

# Upstream WebArena's fixed accounts (browser_env/env_config.py).
ACCOUNTS: dict[str, dict[str, str]] = {
    "shopping": {"username": "emma.lopez@gmail.com", "password": "Password.123"},
    "shopping_admin": {"username": "admin", "password": "admin1234"},
    "reddit": {"username": "MarvelsGrantMan136", "password": "test1234"},
    "gitlab": {"username": "byteblaze", "password": "hello1234"},
}

COOKIE_SITES = ("shopping", "reddit", "gitlab")   # need a real UI login
HEADER_SITES = ("shopping_admin",)                # header does the work


def extra_headers(sites: "tuple | list") -> dict:
    """HTTP headers that authenticate the header-auth sites among ``sites``.

    Context-level headers reach every origin the episode touches. A stray
    ``X-M2-Admin-Auto-Login`` on reddit is inert (nothing reads it), so we do
    not bother scoping it per request.
    """
    if any(s in HEADER_SITES for s in sites):
        acc = ACCOUNTS["shopping_admin"]
        return {ADMIN_AUTO_LOGIN_HEADER: f"{acc['username']}:{acc['password']}"}
    return {}


def state_path(auth_dir: str, stack: str, site: str) -> str:
    return os.path.join(auth_dir, f"{stack}.{site}_state.json")


def _origin(url: str) -> str:
    scheme, _, rest = (url or "").partition("://")
    return f"{scheme}://{rest.split('/', 1)[0]}" if scheme else url


# ── UI login, one site at a time (upstream's renew_comb, per stack) ──────────

def _login_shopping(page, base_url: str) -> None:
    acc = ACCOUNTS["shopping"]
    page.goto(f"{_origin(base_url)}/customer/account/login/")
    page.get_by_label("Email", exact=True).fill(acc["username"])
    page.get_by_label("Password", exact=True).fill(acc["password"])
    page.get_by_role("button", name="Sign In").click()


def _login_reddit(page, base_url: str) -> None:
    acc = ACCOUNTS["reddit"]
    page.goto(f"{_origin(base_url)}/login")
    page.get_by_label("Username").fill(acc["username"])
    page.get_by_label("Password").fill(acc["password"])
    page.get_by_role("button", name="Log in").click()


def _login_gitlab(page, base_url: str) -> None:
    acc = ACCOUNTS["gitlab"]
    page.goto(f"{_origin(base_url)}/users/sign_in")
    page.get_by_test_id("username-field").click()
    page.get_by_test_id("username-field").fill(acc["username"])
    page.get_by_test_id("username-field").press("Tab")
    page.get_by_test_id("password-field").fill(acc["password"])
    page.get_by_test_id("password-field").press("Enter")


_LOGIN_FN = {"shopping": _login_shopping, "reddit": _login_reddit,
             "gitlab": _login_gitlab}

# Post-login proof: a selector/URL that only a logged-in session reaches.
_VERIFY_PATH = {"shopping": "/customer/account/",
                "reddit": "/submit",
                "gitlab": "/-/profile"}


def generate_state(stack: str, site: str, base_url: str, auth_dir: str,
                   *, timeout_ms: int = 60000) -> str:
    """Drive a real UI login and dump ``storage_state``. Returns the path.

    Raises on failure — a silently unauthenticated run would look like a model
    problem for the next several hours (the 2026-07-08 probes: wa_0144 looping
    on the login page, wa_0187 hunting admin inventory in the storefront).
    """
    if site in HEADER_SITES:
        raise ValueError(f"{site} authenticates by header, not by cookie")
    from playwright.sync_api import sync_playwright

    os.makedirs(auth_dir, exist_ok=True)
    path = state_path(auth_dir, stack, site)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, channel="chromium")
        ctx = browser.new_context()
        ctx.set_default_timeout(timeout_ms)
        try:
            page = ctx.new_page()
            _LOGIN_FN[site](page, base_url)
            page.wait_for_load_state("domcontentloaded")
            # Verify: hit a page only an authenticated session may see.
            page.goto(_origin(base_url) + _VERIFY_PATH[site],
                      wait_until="domcontentloaded")
            url = page.url
            if "login" in url or "sign_in" in url:
                raise RuntimeError(
                    f"{stack}/{site}: login did not stick (landed on {url})")
            ctx.storage_state(path=path)
        finally:
            ctx.close()
            browser.close()
    _log.info("webarena/auth — %s/%s logged in -> %s", stack, site, path)
    return path


def merged_state(auth_dir: str, stack: str, sites: "tuple | list") -> "dict | None":
    """Merge the storage_states of ``sites`` on ``stack`` into one dict.

    Playwright accepts ``storage_state`` as a dict, so multi-site tasks get one
    context carrying every cookie they need. Missing files are skipped (the
    site may be header-authenticated, or read-only tasks may not need it).
    """
    cookies: list = []
    origins: list = []
    for site in sites:
        if site in HEADER_SITES:
            continue
        path = state_path(auth_dir, stack, site)
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                st = json.load(f)
        except Exception as exc:  # noqa: BLE001 — a corrupt jar must not kill the run
            _log.warning("webarena/auth — unreadable %s: %s", path, exc)
            continue
        cookies.extend(st.get("cookies") or [])
        origins.extend(st.get("origins") or [])
    if not cookies and not origins:
        return None
    return {"cookies": cookies, "origins": origins}


# ── refresh integration ──────────────────────────────────────────────────────

_gen_lock = threading.Lock()


def refresh_login(stack: str, site: str, base_url: str, auth_dir: str) -> None:
    """Re-establish login after a lane refresh destroyed the server session.

    Header-auth sites are no-ops by construction. Called by the env's refresh
    hook, serialized so concurrent lane refreshes do not launch a browser storm.
    """
    if site in HEADER_SITES:
        return
    with _gen_lock:
        generate_state(stack, site, base_url, auth_dir)


def bootstrap(stacks: "dict[str, dict[str, str]]", auth_dir: str) -> dict:
    """Log in on every (stack, cookie-site) pair. Returns {(stack, site): path}.

    Run once before an experiment; the refresh hook keeps it fresh afterwards.
    """
    out = {}
    for stack, urls in stacks.items():
        for site in COOKIE_SITES:
            if site not in urls:
                continue
            out[(stack, site)] = generate_state(stack, site, urls[site], auth_dir)
    return out
