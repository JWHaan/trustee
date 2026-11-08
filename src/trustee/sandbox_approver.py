"""Approve a PayPal **sandbox** order in a browser, so an end-to-end run against the real sandbox needs no person.

Every PayPal path in this repository is tested against mocked HTTP. The one step that has kept the real sandbox out
of automated runs is the payer approving the order at PayPal. This module does that step with a browser:

    approve(approve_url, buyer_email, buyer_password, done_prefix="https://example.com/paypal/return")

The browser is either local Chromium (Playwright) or any remote browser reachable over the Chrome DevTools Protocol:
pass `cdp_url`. Cloud browser services for AI agents, such as KERNEL, give a CDP WebSocket URL for each browser they
start, so a scheduled CI job can run this without a local browser.

How it works: open the approve link, then repeat until the browser is redirected to `done_prefix` (the order's
return URL, which PayPal only redirects to after approval) or the timeout passes. Each round fills or clicks whatever
step of the PayPal flow is visible: email, "next", password, "log in", approve. The order of PayPal's pages varies
(remembered email, one-page login, already logged in), so the steps are matched by what is on screen, not by
sequence. Success is decided by the redirect, never by a selector.

The default selectors are a best guess at PayPal's sandbox pages and have **not** been checked against them from
this repository: PayPal changes its markup. Override any of them with `PAYPAL_E2E_SELECTORS` (a JSON object) without
changing code. The tests drive a local stand-in for the PayPal pages.

Safety: sandbox only. The approve link must be on a PayPal sandbox host (or a host you pass in `allow_hosts`, which
the tests use), and `PAYPAL_ENV=live` is refused. The password is typed into the page and nowhere else: it is never
logged, never in `steps`, never in an exception message.

Requires the optional `e2e` extra: `uv sync --extra e2e`.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

SANDBOX_HOSTS = frozenset({"www.sandbox.paypal.com", "sandbox.paypal.com"})

# Best-effort defaults for PayPal's sandbox login and review pages. UNVERIFIED: see the module docstring.
DEFAULT_SELECTORS: dict[str, str] = {
    "email": "input#email",
    "next": "button#btnNext",
    "password": "input#password",
    "login": "button#btnLogin",
    "approve": (
        "button#payment-submit-btn, button[data-id='payment-submit-btn'], button[data-testid='submit-button-initial']"
    ),
}


class ApprovalError(Exception):
    """The approval could not be attempted (bad input, unsafe host, missing browser support)."""


@dataclass
class ApprovalOutcome:
    approved: bool
    final_url: str
    steps: list[str] = field(default_factory=list)  # what was done, in order; never contains the password
    reason: str = ""

    @property
    def token(self) -> str | None:
        """The order id PayPal put on the return URL (`?token=`), when it redirected there."""
        from urllib.parse import parse_qs

        values = parse_qs(urlparse(self.final_url).query).get("token")
        return values[0] if values else None


def selectors_from_env(env: dict[str, str] | None = None) -> dict[str, str]:
    """DEFAULT_SELECTORS with any overrides from PAYPAL_E2E_SELECTORS (JSON object of step -> CSS selector)."""
    raw = (env if env is not None else os.environ).get("PAYPAL_E2E_SELECTORS", "").strip()
    merged = dict(DEFAULT_SELECTORS)
    if raw:
        try:
            extra = json.loads(raw)
        except json.JSONDecodeError:
            raise ApprovalError("PAYPAL_E2E_SELECTORS is not valid JSON") from None
        if not isinstance(extra, dict) or not all(isinstance(v, str) for v in extra.values()):
            raise ApprovalError("PAYPAL_E2E_SELECTORS must be a JSON object of step name to CSS selector")
        unknown = set(extra) - set(DEFAULT_SELECTORS)
        if unknown:
            raise ApprovalError(f"PAYPAL_E2E_SELECTORS has unknown steps: {', '.join(sorted(unknown))}")
        merged.update(extra)
    return merged


def check_target(approve_url: str, allow_hosts: Iterable[str] = ()) -> None:
    """Refuse anything but a PayPal sandbox approve link (or an explicitly allowed test host)."""
    if os.environ.get("PAYPAL_ENV", "sandbox").strip().lower() == "live":
        raise ApprovalError("refusing to run: PAYPAL_ENV=live. Browser approval is for the sandbox only")
    parsed = urlparse(approve_url)
    host = (parsed.hostname or "").lower()
    allowed = SANDBOX_HOSTS | {h.lower() for h in allow_hosts}
    if host not in allowed:
        raise ApprovalError(f"refusing to approve on {host or 'an empty host'}: not a PayPal sandbox host")
    if parsed.scheme != "https" and host in SANDBOX_HOSTS:
        raise ApprovalError("the sandbox approve link must be https")


def approve(
    approve_url: str,
    buyer_email: str,
    buyer_password: str,
    *,
    done_prefix: str,
    cdp_url: str | None = None,
    timeout: float = 120.0,
    selectors: dict[str, str] | None = None,
    allow_hosts: Iterable[str] = (),
    headless: bool = True,
    chromium_path: str | None = None,
) -> ApprovalOutcome:
    """Log in as the sandbox buyer and approve the order behind `approve_url`.

    `done_prefix` is the start of the order's return URL; reaching it is the only success signal. `cdp_url` connects
    to a remote browser (for example one started by KERNEL) instead of launching local Chromium; `chromium_path`
    (default: env PAYPAL_E2E_CHROMIUM) points Playwright at a specific Chromium binary."""
    if not buyer_email or not buyer_password:
        raise ApprovalError("set the sandbox buyer's email and password")
    if not done_prefix:
        raise ApprovalError("done_prefix (the order's return URL) is required")
    check_target(approve_url, allow_hosts)
    sel = selectors if selectors is not None else selectors_from_env()
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise ApprovalError("browser approval needs the e2e extra: uv sync --extra e2e") from None

    steps: list[str] = []
    with sync_playwright() as pw:
        if cdp_url:
            browser = pw.chromium.connect_over_cdp(cdp_url)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            steps.append("connected to a remote browser over CDP")
        else:
            path = chromium_path or os.environ.get("PAYPAL_E2E_CHROMIUM") or None
            browser = pw.chromium.launch(headless=headless, executable_path=path)
            context = browser.new_context()
            steps.append("launched local Chromium")
        page = context.new_page()
        try:
            return _drive(page, approve_url, buyer_email, buyer_password, done_prefix, timeout, sel, steps)
        finally:
            page.close()
            if not cdp_url:
                browser.close()  # a remote browser belongs to whoever started it


def _drive(
    page: Any,
    approve_url: str,
    email: str,
    password: str,
    done_prefix: str,
    timeout: float,
    sel: dict[str, str],
    steps: list[str],
) -> ApprovalOutcome:
    deadline = time.monotonic() + timeout
    done_ok: set[str] = set()  # steps already done on the current page, so a field is not filled twice

    def reached() -> bool:
        return str(page.url).startswith(done_prefix)

    def visible(name: str) -> Any | None:
        loc = page.locator(sel[name]).first
        try:
            return loc if loc.is_visible() else None
        except Exception:  # noqa: BLE001 - a page mid-navigation has no stable DOM; try again next round
            return None

    page.goto(approve_url, wait_until="domcontentloaded")
    steps.append("opened the approve link")
    last_url = page.url
    while time.monotonic() < deadline:
        if reached():
            steps.append("redirected to the return URL")
            return ApprovalOutcome(True, page.url, steps)
        if page.url != last_url:
            last_url, done_ok = page.url, set()
        acted = False
        field_ = visible("email")
        if field_ is not None and "email" not in done_ok:
            if field_.input_value():
                steps.append("buyer email already filled in")
            else:
                field_.fill(email)
                steps.append("entered the buyer email")
            done_ok.add("email")
            acted = True
        button = visible("next")
        if button is not None and "email" in done_ok and "next" not in done_ok:
            button.click()
            done_ok.add("next")
            steps.append("clicked next")
            acted = True
        field_ = visible("password")
        if field_ is not None and "password" not in done_ok:
            field_.fill(password)
            done_ok.add("password")
            steps.append("entered the password")
            acted = True
        button = visible("login")
        if button is not None and "password" in done_ok and "login" not in done_ok:
            button.click()
            done_ok.add("login")
            steps.append("clicked log in")
            acted = True
        button = visible("approve")
        if button is not None and "approve" not in done_ok:
            button.click()
            done_ok.add("approve")
            steps.append("clicked approve")
            acted = True
        if not acted:
            page.wait_for_timeout(250)
    reason = "timed out before PayPal redirected to the return URL"
    return ApprovalOutcome(False, page.url, steps, reason)
