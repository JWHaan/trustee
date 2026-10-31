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
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx

from ..agents.buyer import ArtifactStore, BuyerAgent, Checker
from ..agents.seller import SellerAgent
from ..client import TrusteeClient
from ..identity import AgentKey
from ..intent import draft_intent, intent_summary
from ..ledger import Ledger
from ..llm import LLM
from ..mandate import Mandate
from ..paypal import PayPalClient, PayPalError, sandbox_emails
from ..protocol import Contract
from ..verify import verify_receipt
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
    receipt: dict[str, Any] | None = None  # the platform's signed settlement receipt, once the deal is terminal
    receipt_verified: bool | None = None  # checked offline against Trustee's published key and the deal's events


def publish_funding_and_intent(
    client: TrusteeClient,
    *,
    ledger: Ledger,
    deal_id: str,
    paypal: PayPalClient,
    demo_funding: Callable[[str], str | None] | None,
    instruction: str | None,
    principal: str,
    principal_key: AgentKey,
    agent_id: str,
    seller_names: dict[str, str],
    scenario: Scenario,
    llm: LLM | None,
) -> bool:
    """The human's one-time consent over the public API: vault a funding source, sign an intent, publish it.

    Dry-run: the platform vaults a fake source at once. Against the real sandbox only a pre-registered demo vault
    (PAYPAL_DEMO_VAULT_ID) is available to a headless run; without it nothing is published and the deal takes the
    human-present approval path, exactly as before. Returns True when an intent is on file."""
    if not instruction:
        return False
    if paypal.dry_run:
        funding = client.start_funding(principal, principal_key)
        ref = funding["funding_ref"] if funding.get("status") == "active" else None
    else:
        ref = demo_funding(principal) if demo_funding is not None else None
    if ref is None:
        ledger.append(
            deal_id,
            "arena",
            "policy",
            event="no_funding",
            note="no vaulted funding source (PAYPAL_DEMO_VAULT_ID is unset): a human approves this payment",
        )
        return False
    draft = draft_intent(
        instruction,
        llm if llm is not None and not _is_scripted(llm) else None,
        agent_id=agent_id,
        principal=principal,
        funding_ref=ref,
        good=scenario.good,
        known_sellers=seller_names,
    )
    client.publish_intent(draft.intent, principal_key)
    ledger.append(
        deal_id,
        principal,
        "policy",
        event="intent_consent",
        intent=draft.intent.digest()[:16],
        note=f'one-time consent signed by the human: "{draft.intent.instruction}"',
    )
    ledger.append(
        deal_id,
        principal,
        "policy",
        event="intent_limits",
        note=f"limits the agent may not exceed: {intent_summary(draft.intent)}",
    )
    ledger.append(
        deal_id,
        principal,
        "policy",
        event="funding_vaulted",
        note="payment method saved at PayPal once (a reference only; the vault id is never shown)",
    )
    return True


def _is_scripted(llm: Any) -> bool:
    return getattr(llm, "model", "") == "scripted"


def attach_receipt(result: OutsideResult, client: TrusteeClient, ledger: Ledger, deal_id: str) -> OutsideResult:
    """Fetch the finished deal's signed receipt, verify it offline against the published key, put it on the tape."""
    if result.view.get("state") not in ("SETTLED", "VOIDED"):
        return result
    try:
        receipt = client.receipt(deal_id)
        key = str(client.trustee_key()["public_key"])
        events = client.events(deal_id, limit=1000)["events"]
    except Exception:  # noqa: BLE001 - evidence must never break a finished deal
        return result
    report = verify_receipt(receipt, public_key=key, events=events)
    result.receipt, result.receipt_verified = receipt, report.ok
    ledger.append(
        deal_id,
        "trustee",
        "receipt",
        event="receipt_issued",
        verified=report.ok,
        human_present=receipt["human_present"],
        note=(
            f"signed {receipt['outcome']} receipt, checked offline: "
            + ("signature and ledger chain verified" if report.ok else "VERIFICATION FAILED")
        ),
    )
    return result


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
        demo_funding: Callable[[str], str | None] | None = None,
    ):
        self.demo_funding = demo_funding
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

        publish_funding_and_intent(
            self._as(self.buyer_id, "one-time consent"),
            ledger=self.ledger,
            deal_id=self.deal_id,
            paypal=self.paypal,
            demo_funding=self.demo_funding,
            instruction=sc.instruction,
            principal=self.buyer_principal,
            principal_key=org_buyer,
            agent_id=self.buyer_id,
            seller_names={"Tidewater": self.seller_id},
            scenario=sc,
            llm=self.llm,
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
        self._quiet = True
        try:
            return attach_receipt(
                OutsideResult(
                    final,
                    {"verdict": attest.verdict, "reason": attest.reason, "evidence": evidence},
                    2,
                    None,
                    self.buyer_id,
                    self.seller_id,
                ),
                self.client,
                self.ledger,
                self.deal_id,
            )
        finally:
            self._quiet = False

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
