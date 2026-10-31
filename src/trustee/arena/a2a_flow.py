"""The `a2a` scenario: Northwind and Tidewater are real A2A agents and Trustee is the third.

Compare `outside.py`, where one object drives Trustee's REST API on both agents' behalf. Here nobody drives:
Northwind's planning agent and Tidewater's sales agent are the mounted A2A servers (`/agents/northwind`,
`/agents/tidewater`), and the run only starts a purchase on Northwind. The agents find each other by Agent Card,
negotiate with signed moves, countersign the contract, register at Trustee and open the escrow, all as A2A
calls over HTTP to the arena's own address (a real socket in production, a `TestClient` in tests).
Tidewater is told by push when the funds are held, and ships the CSV to Northwind as an A2A file part.

Two things are written to the tape that the agents do not write themselves:

* Every A2A call is a ledger row of kind `a2a` (actor = the caller, event = `<peer> <method> <skill>`, note = the
  task state or a one-word summary of the reply). The agents report each hop to an observer; this module is the
  observer. Trustee's push to Tidewater is a hop too, with Trustee as the caller.
* The platform's own rows for the deal are copied onto the tape by the runner, as for `outside`.

The agents are long-lived and shared, so a run borrows them (`NetworkAgent.session`): it swaps in this run's
model, Trustee URL, ledger and observer, and puts everything back afterwards. A second run waits for the first.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

import httpx

from ..agents.buyer import ArtifactStore, Checker
from ..agents.trustee import is_transient_paypal
from ..client import TrusteeClient, TrusteeError
from ..ledger import Ledger
from ..llm import LLM
from ..network.app import NORTHWIND_MOUNT, TIDEWATER_MOUNT
from ..network.common import Hop, Waiter
from ..network.northwind import Northwind, Purchase
from ..network.tidewater import Tidewater
from ..paypal import PayPalClient, PayPalError
from .approval import ApprovalWait
from .outside import OutsideResult
from .scenario import Scenario

POLL_CAP_SECONDS = 0.25  # the agents read the deal task this often; the PayPal check is throttled separately


class A2AFlow:
    def __init__(
        self,
        *,
        scenario: Scenario,
        deal_id: str,
        ledger: Ledger,
        paypal: PayPalClient,
        http: httpx.Client,
        operator_token: str | None,
        store: ArtifactStore,
        checkers: dict[str, Checker],
        llm: LLM,
        wait: ApprovalWait,
        northwind: Northwind,
        tidewater: Tidewater,
    ):
        self.scenario = scenario
        self.deal_id = deal_id
        self.ledger = ledger
        self.paypal = paypal
        self.http = http
        self.operator_token = operator_token
        self.store = store
        self.checkers = checkers
        self.llm = llm
        self.wait = wait
        self.northwind = northwind
        self.tidewater = tidewater
        self._order_id = ""
        self._last_nudge = 0.0
        self._refused: str | None = None

    # ------------------------------------------------------------------ tape

    def _observe(self, hop: Hop) -> None:
        self.ledger.append(
            self.deal_id,
            hop.caller,
            "a2a",
            event=f"{hop.peer} {hop.method} {hop.skill}",
            note=hop.note,
            price=hop.price,
        )

    def _on_approval(self, deal_id: str, order_id: str, approve_url: str) -> None:
        self._order_id = order_id
        self.ledger.append(
            deal_id,
            "arena",
            "run",
            event="awaiting_approval",
            order_id=order_id,
            approve_url=approve_url,
            wait_s=int(self.wait.timeout_seconds),
            note="Approve this payment in the PayPal sandbox (test money). Funds are held only after approval.",
        )

    def _nudge(self) -> str | None:
        """What the payer's browser does on return from PayPal, for a payer who approved but never came back.

        Returns a reason once PayPal has refused the approved order for good, so the buyer stops waiting. Only a
        transient failure (no status, 408, 429, 5xx) is retried: PayPal locks an order after a few failed attempts."""
        if self._refused is not None:
            return self._refused
        if not self._order_id or time.monotonic() - self._last_nudge < max(self.wait.poll_seconds, POLL_CAP_SECONDS):
            return None
        self._last_nudge = time.monotonic()
        try:
            status = str(self.paypal.get_order(self._order_id).get("status"))
        except PayPalError:
            return None  # transient: the next tick asks again
        if status != "APPROVED":
            return None
        resp = self.http.get(
            "/v1/paypal/return", params={"token": self._order_id}, headers={"Accept": "application/json"}
        )
        if resp.status_code != 502:
            return None
        try:
            explanation = str(resp.json().get("explanation", ""))
        except ValueError:
            explanation = ""
        found = re.search(r"\((\d{3})\)", explanation)
        code = int(found.group(1)) if found else None
        if is_transient_paypal(code):
            return None
        self._refused = f"PayPal refused to authorize the approved order (HTTP {code}). Nothing was held."
        self.ledger.append(self.deal_id, "arena", "run", event="approval_failed", note=self._refused)
        return self._refused

    # ------------------------------------------------------------------- run

    def run(self) -> OutsideResult:
        sc = self.scenario
        base = str(self.http.base_url).rstrip("/")
        waiter = Waiter(
            poll_seconds=min(max(self.wait.poll_seconds, 0.01), POLL_CAP_SECONDS),
            timeout_seconds=self.wait.timeout_seconds,
        )
        seller_settings: dict[str, Any] = {
            "good": sc.good,
            "ask": sc.ask,
            "floor": sc.floor,
            "store": self.store,
            "llm": self.llm,
            "cheat": sc.cheat,
            "trustee_url": base,
            "operator_token": self.operator_token,
            "ledger": self.ledger,
            "http": self.http,
            "public_url": base + TIDEWATER_MOUNT,
            "observer": self._observe,
        }
        buyer_settings: dict[str, Any] = {
            "good": sc.good,
            "budget": sc.budget,
            "llm": self.llm,
            "checkers": self.checkers,
            "trustee_url": base,
            "operator_token": self.operator_token,
            "ledger": self.ledger,
            "waiter": waiter,
            "on_approval": self._on_approval,
            "poll_hook": self._nudge,
            "inbox_root": Path(self.store.inbox_dir),
            "http": self.http,
            "public_url": base + NORTHWIND_MOUNT,
            "observer": self._observe,
        }
        self.ledger.append(
            self.deal_id,
            "arena",
            "run",
            event="a2a_network",
            note=(
                "Northwind and Tidewater are A2A agents with their own Agent Cards; "
                "they find each other by card and Trustee only holds the money"
            ),
        )
        with self.tidewater.session(**seller_settings), self.northwind.session(**buyer_settings):
            purchase = self.northwind.buy(base + TIDEWATER_MOUNT, deal_id=self.deal_id)
        return self._result(purchase)

    def _result(self, purchase: Purchase) -> OutsideResult:
        client = TrusteeClient(str(self.http.base_url), http=self.http, operator_token=self.operator_token)
        try:
            view = client.get_deal(self.deal_id)
        except TrusteeError:
            # The deal never formed (a refused contract is not stored): report the negotiation's last state.
            contract = purchase.contract
            view = {
                "state": "NEGOTIATING",
                "contract": contract.key_clauses() if contract is not None else None,
                "escrow": None,
                "ruling": None,
            }
        return OutsideResult(
            view,
            purchase.attest,
            purchase.turns,
            purchase.outcome,
            self.northwind.identity.agent_id,
            self.tidewater.identity.agent_id,
        )
