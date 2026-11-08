"""Browser approval of a PayPal sandbox order, driven against a local stand-in for PayPal's pages.

The stand-in has the shape that matters: an email step, a password step revealed on the same page, a review page
with an approve button, and a redirect to the order's return URL with `?token=`. Real PayPal markup is not checked
here (see the module docstring of trustee.sandbox_approver)."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

pytest.importorskip("playwright")

from trustee.sandbox_approver import (  # noqa: E402
    ApprovalError,
    approve,
    check_target,
    selectors_from_env,
)

CHROMIUM = os.environ.get("PAYPAL_E2E_CHROMIUM") or "/opt/pw-browsers/chromium"
if not Path(CHROMIUM).exists():
    pytest.skip("no Chromium binary for the browser tests", allow_module_level=True)

PASSWORD = "s3cret-sandbox-pw"

APPROVE_PAGE = """<!doctype html><html><body>
<div id="step-email"><input id="email" type="email"><button id="btnNext" type="button">Next</button></div>
<div id="step-password" style="display:none">
  <input id="password" type="password"><button id="btnLogin" type="button">Log In</button>
</div>
<script>
document.getElementById('btnNext').onclick = () => {
  if (!document.getElementById('email').value) return;
  document.getElementById('step-email').style.display = 'none';
  document.getElementById('step-password').style.display = 'block';
};
document.getElementById('btnLogin').onclick = () => {
  if (document.getElementById('password').value !== '__PASSWORD__') return;
  location.href = '/review';
};
</script></body></html>""".replace("__PASSWORD__", PASSWORD)

REVIEW_PAGE = """<!doctype html><html><body>
<button id="payment-submit-btn" type="button" onclick="location.href='/return?token=ORDER-123&PayerID=P1'">
Continue to Review Order</button></body></html>"""

STUCK_PAGE = "<!doctype html><html><body><p>Something went wrong</p></body></html>"


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - http.server API
        path = self.path.split("?")[0]
        body = {"/approve": APPROVE_PAGE, "/review": REVIEW_PAGE, "/stuck": STUCK_PAGE}.get(path, "<p>ok</p>")
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_args: object) -> None:
        pass


@pytest.fixture
def fake_paypal() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def run(base: str, path: str = "/approve", **kw: object):
    return approve(
        f"{base}{path}",
        "buyer@example.com",
        PASSWORD,
        done_prefix=f"{base}/return",
        allow_hosts=["127.0.0.1"],
        chromium_path=CHROMIUM,
        **kw,  # type: ignore[arg-type]
    )


def test_logs_in_approves_and_reaches_the_return_url(fake_paypal: str):
    outcome = run(fake_paypal, timeout=30)
    assert outcome.approved, outcome
    assert outcome.token == "ORDER-123"
    assert outcome.steps[-1] == "redirected to the return URL"
    assert "clicked approve" in outcome.steps and "entered the password" in outcome.steps
    assert PASSWORD not in repr(outcome)  # the password is typed, never recorded


def test_works_through_a_remote_browser_over_cdp(fake_paypal: str):
    """The KERNEL path: connect to a browser someone else started, by its CDP WebSocket URL."""
    with tempfile.TemporaryDirectory() as profile:
        proc = subprocess.Popen(
            [CHROMIUM, "--headless=new", "--remote-debugging-port=0", f"--user-data-dir={profile}", "--no-sandbox"],
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            ws_url = ""
            assert proc.stderr is not None
            for line in proc.stderr:
                match = re.search(r"DevTools listening on (ws://\S+)", line)
                if match:
                    ws_url = match.group(1)
                    break
            assert ws_url, "Chromium did not print a DevTools URL"
            outcome = run(fake_paypal, cdp_url=ws_url, timeout=30)
            assert outcome.approved and outcome.steps[0] == "connected to a remote browser over CDP"
        finally:
            proc.terminate()
            proc.wait(timeout=10)


def test_reports_a_timeout_when_paypal_never_redirects(fake_paypal: str):
    outcome = run(fake_paypal, path="/stuck", timeout=2)
    assert not outcome.approved
    assert "timed out" in outcome.reason


def test_refuses_hosts_that_are_not_the_paypal_sandbox(monkeypatch: pytest.MonkeyPatch):
    with pytest.raises(ApprovalError, match="not a PayPal sandbox host"):
        check_target("https://www.paypal.com/checkoutnow?token=X")
    with pytest.raises(ApprovalError, match="must be https"):
        check_target("http://www.sandbox.paypal.com/checkoutnow?token=X")
    check_target("https://www.sandbox.paypal.com/checkoutnow?token=X")
    monkeypatch.setenv("PAYPAL_ENV", "live")
    with pytest.raises(ApprovalError, match="PAYPAL_ENV=live"):
        check_target("https://www.sandbox.paypal.com/checkoutnow?token=X")


def test_selector_overrides_are_validated():
    assert selectors_from_env({"PAYPAL_E2E_SELECTORS": '{"approve": "#go"}'})["approve"] == "#go"
    with pytest.raises(ApprovalError, match="unknown steps"):
        selectors_from_env({"PAYPAL_E2E_SELECTORS": '{"pay": "#go"}'})
    with pytest.raises(ApprovalError, match="not valid JSON"):
        selectors_from_env({"PAYPAL_E2E_SELECTORS": "{"})


def test_needs_credentials_and_a_return_url():
    with pytest.raises(ApprovalError, match="email and password"):
        approve("https://www.sandbox.paypal.com/x", "", "pw", done_prefix="https://e.x/return")
    with pytest.raises(ApprovalError, match="done_prefix"):
        approve("https://www.sandbox.paypal.com/x", "a@b.c", "pw", done_prefix="")
