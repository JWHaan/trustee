"""Drive one hosted arena scenario that needs a human PayPal approval.

Creates the run, waits for the escrow approve_url, approves it in headless
Chromium as the sandbox buyer, then polls until the run reaches a terminal
state. Sandbox creds come from the environment (never committed).
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request

from trustee.sandbox_approver import approve

BASE = os.environ.get("ARENA_URL", "https://trustee-arena.onrender.com")
BUYER_EMAIL = os.environ["PAYPAL_SANDBOX_BUYER_EMAIL"]
BUYER_PASSWORD = os.environ["PAYPAL_SANDBOX_BUYER_PASSWORD"]
TERMINAL = {"SETTLED", "VOIDED", "FAILED", "REFUSED", "CANCELLED"}


def api(path: str, method: str = "GET", body: dict | None = None) -> dict:
    req = urllib.request.Request(
        BASE + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def main() -> int:
    scenario = sys.argv[1]
    run = api("/api/runs", "POST", {"scenario": scenario})
    run_id = run["run_id"]
    print(f"run {run_id} scenario={scenario}", flush=True)

    approve_url = None
    deadline = time.time() + 240
    while time.time() < deadline:
        rec = api(f"/api/runs/{run_id}")
        approve_url = (rec.get("escrow") or {}).get("approve_url")
        if approve_url:
            break
        if rec.get("state") in TERMINAL:
            print(f"terminal before approval: {rec.get('state')} {rec.get('outcome')}")
            return 0
        time.sleep(3)
    if not approve_url:
        print("no approve_url appeared; giving up")
        return 1

    print(f"approving: {approve_url[:80]}...", flush=True)
    outcome = approve(
        approve_url,
        BUYER_EMAIL,
        BUYER_PASSWORD,
        done_prefix=BASE,
        timeout=120,
    )
    print(f"approval outcome: approved={outcome.approved} token={outcome.token}", flush=True)

    deadline = time.time() + 300
    while time.time() < deadline:
        rec = api(f"/api/runs/{run_id}")
        state = rec.get("state")
        if state in TERMINAL:
            escrow = rec.get("escrow") or {}
            print(
                f"FINAL {scenario}: state={state} outcome={rec.get('outcome')} "
                f"escrow={escrow.get('status')} ruling={json.dumps(rec.get('ruling'))[:200]}"
            )
            return 0
        time.sleep(5)
    print("run did not reach a terminal state in time")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
