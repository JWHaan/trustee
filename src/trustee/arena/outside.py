"""The `outside` scenario: two agents that live on their own servers, using Trustee only over HTTP.

Northwind's planning agent and Tidewater's sales agent agreed the price off-platform. Everything
here goes through `TrusteeClient` against the arena's own public /v1 API (a real socket in
production, a `TestClient` in tests): register principals, register agents, publish signed
mandates, sign the contract, open the deal, deliver, attest. Trustee only sees what they sign.

Each HTTP call lands on the tape as an `api` row, written from an httpx response hook so the
status code is the one the server actually returned. The platform's own ledger rows for the deal
are forwarded onto the same tape by the runner.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx

from ..agents.buyer import ArtifactStore, BuyerAgent, Checker
from ..agents.seller import SellerAgent
from ..client import TrusteeClient
from ..identity import AgentKey
from ..ledger import Ledger
from ..llm import LLM
from ..mandate import Mandate
from ..paypal import PayPalClient, PayPalError, sandbox_emails
from ..protocol import Contract
from .approval import ApprovalWait, wait_for_approval
from .scenario import Scenario

BUYER_BASE = "northwind-planner"
SELLER_BASE = "tidewater-sales"
# Principal names are API ids (no spaces): Northwind Outfitters, Tidewater Data Co.
BUYER_PRINCIPAL = "northwind-outfitters"
SELLER_PRINCIPAL = "tidewater-data-co"
@dataclass
class OutsideResult:
    view: dict[str, Any]
    attest: dict[str, Any] | None
    turns: int
    outcome: str | None  # None when the deal ran to the end; else why it stopped
    buyer_id: str
    seller_id: str


class OutsideFlow:
    def __init__(
        self,
        *,
        scenario: Scenario,
        deal_id: str,
        suffix: str,
        ledger: Ledger,
        paypal: PayPalClient,
        http: httpx.Client,
        operator_token: str | None,
        store: ArtifactStore,
        checkers: dict[str, Checker],
        llm: LLM,
        wait: ApprovalWait,
    ):
        self.scenario = scenario
        self.deal_id = deal_id
        # Ids are unique per run: the service keeps agents for good, and every run mints fresh keys.
        self.buyer_id = f"{BUYER_BASE}-{suffix}"
        self.seller_id = f"{SELLER_BASE}-{suffix}"
        self.buyer_principal = f"{BUYER_PRINCIPAL}-{suffix}"
        self.seller_principal = f"{SELLER_PRINCIPAL}-{suffix}"
        self.ledger = ledger
        self.paypal = paypal
        self.http = http
        self.store = store
        self.checkers = checkers
        self.llm = llm
        self.wait = wait
        self.client = TrusteeClient(str(http.base_url), http=http, operator_token=operator_token)
        self._actor = "arena"
        self._label = ""
        self._quiet = False

    # ------------------------------------------------------------------ tape

    def _hook(self, response: httpx.Response) -> None:
        if self._quiet:
            return
        req = response.request
        path = req.url.path.replace(self.deal_id, "{id}")
        self.ledger.append(
            self.deal_id,
            self._actor,
            "api",
            event=f"{req.method} {path}",
            status=response.status_code,
            note=f"{response.status_code} {self._label}".strip(),
        )

    def _as(self, actor: str, label: str) -> TrusteeClient:
        self._actor, self._label = actor, label
        return self.client

    def _log(self, actor: str, event: str, note: str, **extra: Any) -> None:
        self.ledger.append(self.deal_id, actor, "run" if actor == "arena" else "msg", event=event, note=note, **extra)

    # ------------------------------------------------------------------- run

    def run(self) -> OutsideResult:
        hooks = self.http.event_hooks.setdefault("response", [])
        hooks.append(self._hook)
        try:
            return self._run()
        finally:
            if self._hook in hooks:
                hooks.remove(self._hook)

    def _run(self) -> OutsideResult:
        sc = self.scenario
        price = sc.agreed_price or sc.ask
        k_buyer, k_seller, org_buyer, org_seller = (AgentKey.generate() for _ in range(4))

        self._log(
            self.buyer_id,
            "agreed",
            f"{self.buyer_id} and {self.seller_id} agreed {price} off-platform; Trustee sees only what they sign",
            price=str(price),
        )

        buyer_email, seller_email = sandbox_emails()
        self._as(self.buyer_id, "principal registered").register_principal(self.buyer_principal, org_buyer)
        self._as(self.seller_id, "principal registered").register_principal(self.seller_principal, org_seller)
        self._as(self.buyer_id, "agent registered").register_agent(
            self.buyer_id, k_buyer, self.buyer_principal, org_buyer, ["buyer"], paypal_email=buyer_email
        )
        self._as(self.seller_id, "agent registered").register_agent(
            self.seller_id, k_seller, self.seller_principal, org_seller, ["seller"], paypal_email=seller_email
        )
        ceiling = Decimal("500.00")
        category = frozenset({sc.good.category})
        self._as(self.buyer_id, "signed mandate published").publish_mandate(
            Mandate(
                agent_id=self.buyer_id,
                principal=self.buyer_principal,
                max_amount=ceiling,
                allowed_categories=category,
                allowed_counterparties=frozenset({self.seller_id}),
                grants="spend",
            ),
            org_buyer,
        )
        self._as(self.seller_id, "signed mandate published").publish_mandate(
            Mandate(
                agent_id=self.seller_id,
                principal=self.seller_principal,
                max_amount=ceiling,
                allowed_categories=category,
                allowed_counterparties=frozenset({self.buyer_id}),
                grants="receive",
            ),
            org_seller,
        )

        now = datetime.now()
        contract = (
            Contract(
                deal_id=self.deal_id,
                buyer=self.buyer_id,
                seller=self.seller_id,
                good=sc.good,
                price=price,
                deadline=now + timedelta(hours=1),
                formed_at=now,
            )
            .sign(self.buyer_id, k_buyer)
            .sign(self.seller_id, k_seller)
        )
        view = self._as(self.buyer_id, "both signatures verified, escrow opened").open_deal(contract)

        outcome: str | None = None
        if _awaiting_payer(view):
            view, outcome = self._await_payer(view)
        if outcome is not None or view["state"] != "ESCROWED":
            return OutsideResult(view, None, 0, outcome or "not_escrowed", self.buyer_id, self.seller_id)

        seller = SellerAgent(
            agent_id=self.seller_id,
            llm=self.llm,
            good=sc.good,
            floor_price=sc.floor,
            target_price=sc.ask,
            store=self.store,
            ledger=self.ledger,
        )
        delivery = seller.deliver(contract, cheat=sc.cheat).sign(k_seller)
        self._as(self.seller_id, "signed delivery accepted").deliver(delivery)

        buyer = BuyerAgent(
            agent_id=self.buyer_id,
            llm=self.llm,
            good=sc.good,
            budget=sc.budget,
            store=self.store,
            checkers=self.checkers,
            ledger=self.ledger,
        )
        attest = buyer.verify_delivery(contract, delivery).sign(k_buyer)
        self._as(self.buyer_id, "signed attestation accepted").attest(attest)

        final = self._as(self.buyer_id, "deal view").get_deal(self.deal_id)
        evidence = {k: str(v) for k, v in (attest.evidence or {}).items()}
        return OutsideResult(
            final,
            {"verdict": attest.verdict, "reason": attest.reason, "evidence": evidence},
            2,
            None,
            self.buyer_id,
            self.seller_id,
        )

    # -------------------------------------------------------------- approval

    def _await_payer(self, view: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
        order_id = view["escrow"]["order_id"]
        self.ledger.append(
            self.deal_id,
            "arena",
            "run",
            event="awaiting_approval",
            order_id=order_id,
            approve_url=view["escrow"]["approve_url"] or "",
            wait_s=int(self.wait.timeout_seconds),
            note="Approve this payment in the PayPal sandbox (test money). Funds are held only after approval.",
        )

        def attempt() -> bool:
            self._quiet = True
            try:
                current = self.client.get_deal(self.deal_id)
                if current["state"] != "CONTRACTED":
                    return True  # the platform's own sweep or webhook already moved it
                try:
                    status = str(self.paypal.get_order(order_id).get("status"))
                except PayPalError:
                    return False  # transient: the next tick asks again
                if status != "APPROVED":
                    return False
            finally:
                self._quiet = False
            # What the payer's browser does on return from PayPal.
            self._as(self.buyer_id, "payer approved at PayPal")
            self.http.get("/v1/paypal/return", params={"token": order_id}, headers={"Accept": "application/json"})
            return True

        result = wait_for_approval(attempt, threading.Event(), self.wait)
        if result != "approved":
            self._as("arena", "approval not given")
            self.http.get("/v1/paypal/cancel", params={"token": order_id}, headers={"Accept": "application/json"})
            self.ledger.append(
                self.deal_id, "arena", "run", event=f"approval_{result}", note="no payer approval; nothing was held"
            )
        final = self._as(self.buyer_id, "deal view").get_deal(self.deal_id)
        return final, (None if result == "approved" else f"approval_{result}")


def _awaiting_payer(view: dict[str, Any]) -> bool:
    escrow = view.get("escrow") or {}
    return view.get("state") == "CONTRACTED" and escrow.get("status") == "created"
