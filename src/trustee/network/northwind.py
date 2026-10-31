"""Northwind Outfitters' planning agent as a real A2A agent.

Northwind buys. `Northwind.buy(seller_url)` is the whole purchase, and every step is an A2A call:

1. Read the seller's Agent Card (and the Ed25519 key it publishes), then `quote`. The quoted good must equal the
   good Northwind wants, and must carry a sha256, or nothing is signed.
2. Negotiate with `BuyerAgent`'s policy for at most `MAX_ROUNDS` rounds. Every move is signed, every reply is
   checked against the seller's published key, and the price is capped at the budget. The model chooses whether to
   counter or accept and at what counter price; it never chooses the price of an acceptance (that is the seller's
   standing price) and never the good.
3. Sign the contract, have the seller countersign it (`sign-contract`), and check the seller's signature.
4. Register itself, its principal and a `spend` mandate at Trustee (`register-principal`, `register-agent`,
   `publish-mandate`), then `open-escrow`. If PayPal needs the payer to approve, the approve URL is surfaced and the
   deal task is read until it moves on. Then it tells the seller the escrow is open (`escrow-opened`).
5. Wait for the delivery. The seller sends the CSV to Northwind's `receive-delivery` skill as an A2A FilePart. Only
   a delivery signed by the seller of a deal Northwind is waiting on, for the contract Northwind signed, is stored.
6. Verify the bytes itself with `BuyerAgent.verify_delivery` (hash against the contract, then the checkers), sign an
   attestation, send `attest-delivery`, and read the deal task until it is `COMPLETED`.

Nothing Northwind receives can make it pay: money moves only when it signs the contract and, later, the
attestation, and each of those is Trustee's decision to act on.

Run it alone: `python -m trustee.network.northwind --port 8101 --trustee http://127.0.0.1:8000`, optionally with
`--buy http://127.0.0.1:8102` to make one purchase from a Tidewater.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import logging
import os
import tempfile
import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

from ..a2a.client import A2AClient, artifact_data
from ..a2a.models import TERMINAL_STATES, A2AError, Message, Task, TaskState
from ..agents.buyer import ArtifactStore, BuyerAgent, Checker
from ..identity import verify
from ..ledger import Ledger
from ..llm import LLM
from ..mandate import Mandate
from ..paypal import sandbox_emails
from ..protocol import Accept, Contract, Counter, Delivery, GoodSpec, Offer, new_id
from .common import (
    AgentIdentity,
    Call,
    HopObserver,
    NetworkAgent,
    PeerError,
    PeerKey,
    PeerLink,
    SkillDef,
    SkillRefused,
    Waiter,
    discover_peer,
    move_is_signed_by,
    parse_move,
    publish_mandate,
    refusal_of,
    register_with_trustee,
    result_data,
    state_name,
)

log = logging.getLogger(__name__)

MAX_ROUNDS = 3  # buyer + seller exchanges after the opening offer
MAX_DELIVERY_BYTES = 8 << 20
MAX_PENDING = 100


class PurchaseError(Exception):
    """The purchase cannot continue and nothing about it is a normal outcome (a bad reply, a refusal)."""


@dataclass
class InboxItem:
    """A delivery Northwind accepted: the bytes on disk plus the signed delivery and the contract they belong to."""

    deal_id: str
    ref: str
    sha256: str
    size: int
    contract_digest: str
    sender: str
    delivery: Delivery
    path: Path


@dataclass
class Purchase:
    """How a purchase ended. `outcome` is None when the deal ran to its end; otherwise why it stopped."""

    deal_id: str
    seller: str
    price: Decimal | None = None
    contract: Contract | None = None
    task: Task | None = None
    receipt: dict[str, Any] | None = None
    attest: dict[str, Any] | None = None
    outcome: str | None = None
    turns: int = 0

    @property
    def settled(self) -> bool:
        return bool(self.receipt and self.receipt.get("outcome") == "settled")


@dataclass
class _Pending:
    contract: Contract
    seller: PeerKey


@dataclass
class _Spend:
    version: int = 0
    categories: frozenset[str] = field(default_factory=frozenset)
    sellers: frozenset[str] = field(default_factory=frozenset)


class Northwind(NetworkAgent):
    card_name = "Northwind Outfitters planning agent"
    card_description = (
        "Buys data for Northwind Outfitters. It finds a seller by its Agent Card, negotiates over A2A with signed "
        "moves, and pays through Trustee escrow. It accepts deliveries (an A2A FilePart) only from the seller of a "
        "deal it signed, and checks the bytes against the contract before it lets Trustee release the money."
    )
    org_name = "Northwind Outfitters"

    def __init__(
        self,
        *,
        good: GoodSpec,
        budget: Decimal,
        llm: LLM,
        checkers: dict[str, Checker] | None = None,
        trustee_url: str | None = None,
        operator_token: str | None = None,
        ledger: Ledger | None = None,
        waiter: Waiter | None = None,
        on_approval: Callable[[str, str, str], None] | None = None,
        poll_hook: Callable[[], str | None] | None = None,
        mandate_ceiling: Decimal = Decimal("500.00"),
        inbox_root: Path | None = None,
        identity: AgentIdentity | None = None,
        http: httpx.Client | None = None,
        public_url: str | None = None,
        observer: HopObserver | None = None,
    ):
        super().__init__(
            identity
            or AgentIdentity.generate("northwind-planner", "northwind-outfitters", "buyer", sandbox_emails()[0]),
            http=http,
            public_url=public_url,
            observer=observer,
        )
        self.good = good
        self.budget = budget
        self.llm = llm
        self.checkers = checkers or {}
        self.trustee_url = trustee_url.rstrip("/") if trustee_url else None
        self.operator_token = operator_token
        self.ledger = ledger
        self.waiter = waiter or Waiter()
        self.on_approval = on_approval
        self.poll_hook = poll_hook
        self.mandate_ceiling = mandate_ceiling
        self.inbox_root = inbox_root
        self.inbox: OrderedDict[str, InboxItem] = OrderedDict()
        self._pending: OrderedDict[str, _Pending] = OrderedDict()
        self._spend: dict[str, _Spend] = {}
        self._registered: set[str] = set()
        self._lock = threading.RLock()
        self._tmp_inbox: Path | None = None
        self.add_skill(
            SkillDef(
                "receive-delivery",
                "Receive a delivery",
                'Input {"delivery": <Delivery signed by the seller>, "contract_digest"} plus one FilePart with the '
                "bytes. Accepted only from the seller of a deal this agent signed and is waiting on.",
                ["delivery", "files"],
                ["Here is returns-2026q3.csv for deal deal_ab12"],
                self._receive_delivery,
            )
        )

    # ------------------------------------------------------------------------------ plumbing

    def _inbox_dir(self, deal_id: str) -> Path:
        if self.inbox_root is not None:
            root = self.inbox_root
        else:
            if self._tmp_inbox is None:
                self._tmp_inbox = Path(tempfile.mkdtemp(prefix="northwind-inbox-"))
            root = self._tmp_inbox
        return root / deal_id

    def _trustee(self) -> PeerLink:
        if not self.trustee_url:
            raise PurchaseError("this agent has no Trustee URL configured")
        auth = f"Bearer {self.operator_token}" if self.operator_token else None
        return self.link("trustee", A2AClient(self.trustee_url, http=self.http, authorization=auth))

    def card_url(self) -> str:
        if not self.public_url:
            raise PurchaseError("this agent has no public URL, so the seller cannot read its Agent Card")
        return f"{self.public_url}/.well-known/agent-card.json"

    def _msg(self, deal_id: str, event: str, price: Decimal, note: str) -> None:
        if self.ledger is not None:
            self.ledger.append(deal_id, self.identity.agent_id, "msg", event=event, price=str(price), note=note)

    # ------------------------------------------------------------------- receive-delivery skill

    def _receive_delivery(self, call: Call) -> dict[str, Any]:
        try:
            delivery = Delivery.model_validate(call.input.get("delivery"))
        except ValueError:
            raise SkillRefused(422, "input.delivery is not a valid delivery") from None
        deal_id = str(delivery.deal_id)
        with self._lock:
            pending = self._pending.get(deal_id)
        if pending is None:
            raise SkillRefused(409, f"this agent is not waiting on a delivery for deal {deal_id}")
        seller = pending.seller
        if delivery.sender != seller.agent_id or not verify(
            seller.public_key, delivery.signature or "", delivery.signing_bytes()
        ):
            raise SkillRefused(401, f"the delivery is not signed by the key {seller.agent_id} published in its card")
        contract = pending.contract
        if call.input.get("contract_digest") != contract.digest():
            raise SkillRefused(409, "the delivery names a different contract than the one this agent signed")
        ref = contract.good.deliverable_ref
        if delivery.deliverable_ref != ref or Path(ref).name != ref:
            raise SkillRefused(422, f"the contract names {ref!r}; the delivery names {delivery.deliverable_ref!r}")
        files = call.files()
        if len(files) != 1 or files[0].raw is None:
            raise SkillRefused(422, "send exactly one FilePart with the deliverable bytes")
        try:
            payload = base64.b64decode(files[0].raw, validate=True)
        except (binascii.Error, ValueError):
            raise SkillRefused(422, "the FilePart is not valid base64") from None
        if len(payload) > MAX_DELIVERY_BYTES:
            raise SkillRefused(422, "the delivery is too large")
        store = ArtifactStore(goods_dir=self._inbox_dir(deal_id), manifest={}, inbox=self._inbox_dir(deal_id))
        digest = store.receive(ref, payload)
        item = InboxItem(
            deal_id, ref, digest, len(payload), contract.digest(), delivery.sender, delivery, store.received(ref)
        )
        with self._lock:
            self.inbox[deal_id] = item
        return {"summary": f"received {ref} ({len(payload)} bytes)", "received": True, "sha256": digest}

    # --------------------------------------------------------------------------- the purchase

    def buy(self, seller_url: str, *, deal_id: str | None = None) -> Purchase:
        me = self.identity
        deal_id = deal_id or new_id("deal")
        try:
            seller = discover_peer(self.http, seller_url)
        except (PeerError, httpx.HTTPError) as exc:
            raise PurchaseError(f"could not read the seller's Agent Card: {exc}") from None
        tw = self.link(seller.label, A2AClient(card_url=seller.card_url, http=self.http))
        tw.card_fetched(seller.name or seller.agent_id)
        purchase = Purchase(deal_id=deal_id, seller=seller.agent_id)

        ask = self._quote(tw, seller)
        price, purchase.turns = self._negotiate(tw, seller, deal_id, ask)
        purchase.price = price
        if price > self.budget:
            raise PurchaseError(f"the agreed price {price} is over the budget {self.budget}")

        contract = self._contract(tw, seller, deal_id, price)
        purchase.contract = contract

        trustee = self._trustee()
        self._ensure_registered(trustee, seller.agent_id)
        with self._lock:
            self._pending[deal_id] = _Pending(contract, seller)
            while len(self._pending) > MAX_PENDING:
                self._pending.popitem(last=False)

        task = trustee.skill("open-escrow", contract.model_dump(mode="json"))
        if not isinstance(task, Task):
            raise PurchaseError("Trustee answered open-escrow with a message, not a task")
        refused = refusal_of(task)
        if refused is not None:
            status = int(refused.get("status") or 0)
            if status in (401, 403):
                purchase.outcome = "rejected_identity" if status == 401 else "rejected_mandate"
                purchase.task = task
                return purchase
            raise PurchaseError(f"Trustee refused the contract: {refused.get('detail')}")
        purchase.task = task

        self._tell_seller(tw, deal_id, contract)

        stopped = self._await_funds(trustee, task)
        if stopped is not None:
            purchase.outcome, purchase.task = stopped
            return purchase
        item = self._await_delivery(deal_id)
        if item is None:
            purchase.outcome = "delivery_timeout"
            purchase.task = trustee.get_task(deal_id)
            return purchase
        if not self.waiter.until(lambda: trustee.get_task(deal_id).status.state != TaskState.INPUT_REQUIRED):
            purchase.outcome = "delivery_not_recorded"
            purchase.task = trustee.get_task(deal_id)
            return purchase

        attest = self._buyer(deal_id).verify_delivery(contract, item.delivery).sign(me.agent_key)
        purchase.attest = {
            "verdict": attest.verdict,
            "reason": attest.reason,
            "evidence": {k: str(v) for k, v in (attest.evidence or {}).items()},
        }
        purchase.turns += 1
        done = trustee.skill("attest-delivery", attest.model_dump(mode="json"), task_id=deal_id)
        if not isinstance(done, Task):
            raise PurchaseError("Trustee answered attest-delivery with a message, not a task")
        refused = refusal_of(done)
        if refused is not None:
            raise PurchaseError(f"Trustee refused the attestation: {refused.get('detail')}")
        final = done
        if final.status.state not in TERMINAL_STATES:
            self.waiter.until(lambda: trustee.get_task(deal_id).status.state in TERMINAL_STATES)
            final = trustee.get_task(deal_id)
        purchase.task = final
        purchase.receipt = artifact_data(final, "settlement-receipt")
        if purchase.receipt is None and final.status.state != TaskState.COMPLETED:
            purchase.outcome = f"deal_{state_name(final).lower()}"
        return purchase

    # ---------------------------------------------------------------------------------- steps

    def _quote(self, tw: PeerLink, seller: PeerKey) -> Decimal:
        quote = tw.skill("quote", {})
        if not isinstance(quote, Task) or quote.status.state != TaskState.COMPLETED:
            raise PurchaseError(f"{seller.agent_id} would not quote")
        data = result_data(quote)
        if data.get("seller") != seller.agent_id:
            raise PurchaseError("the quote is from a different agent than the card")
        quoted = GoodSpec.model_validate(data["good"])
        if quoted.model_dump(mode="json") != self.good.model_dump(mode="json"):
            raise PurchaseError("the quoted good is not the good Northwind wants")
        if quoted.sha256 is None:
            raise PurchaseError("the quoted good carries no sha256; refusing to buy something that cannot be verified")
        if Path(quoted.deliverable_ref).name != quoted.deliverable_ref:
            raise PurchaseError("the quoted deliverable name is not a plain file name")
        return Decimal(str(data["ask"]))

    def _send(self, tw: PeerLink, seller: PeerKey, move: Offer | Counter | Accept) -> Counter | Accept:
        def describe(result: Task | Message) -> tuple[str, str]:
            if isinstance(result, Task) and result.status.state == TaskState.COMPLETED:
                reply = (artifact_data(result, "result") or {}).get("move") or {}
                return str(reply.get("kind", "")), str(reply.get("price", ""))
            return PeerLink.default_note(result)

        result = tw.skill(
            "negotiate",
            {"move": move.model_dump(mode="json"), "buyer_card": self.card_url()},
            describe=describe,
        )
        if not isinstance(result, Task) or result.status.state != TaskState.COMPLETED:
            refused = refusal_of(result) if isinstance(result, Task) else None
            raise PurchaseError(f"{seller.agent_id} refused the {move.kind}: {(refused or {}).get('detail')}")
        reply = parse_move(result_data(result).get("move"))
        if not isinstance(reply, Counter | Accept):
            raise PurchaseError(f"{seller.agent_id} answered with a {reply.kind}")
        if not move_is_signed_by(reply, seller):
            raise PurchaseError(f"the reply is not signed by the key {seller.agent_id} published in its card")
        if reply.deal_id != move.deal_id or reply.good.model_dump(mode="json") != self.good.model_dump(mode="json"):
            raise PurchaseError("the reply is about a different deal or good")
        return reply

    def _negotiate(self, tw: PeerLink, seller: PeerKey, deal_id: str, ask: Decimal) -> tuple[Decimal, int]:
        me = self.identity
        buyer = BuyerAgent(me.agent_id, self.llm, self.good, self.budget, self._store(deal_id), ledger=self.ledger)
        opening = buyer.opening_price(ask)
        offer = buyer.open_deal(opening, seller.agent_id).model_copy(update={"deal_id": deal_id}).sign(me.agent_key)
        gap = f"{(1 - opening / ask) * 100:.0f}%" if ask else "0%"
        self._msg(deal_id, "offer", offer.price, f"opens {offer.price}, {gap} under the {ask} ask")
        turns = 2
        reply = self._send(tw, seller, offer)
        rounds = 0
        while isinstance(reply, Counter):
            standing = reply
            if rounds >= MAX_ROUNDS:
                answer: Counter | Accept = self._accept(standing, deal_id)
                self._msg(deal_id, "cap_reached", standing.price, "move cap: accepting the standing price")
            else:
                chosen = buyer.respond(standing.model_dump(mode="json"), opening, max_turns=2)
                if isinstance(chosen, Accept):
                    answer = self._accept(standing, deal_id)  # the LLM says whether to accept, never at what price
                else:
                    capped = min(chosen.price, self.budget)
                    answer = chosen.model_copy(
                        update={"deal_id": deal_id, "price": capped, "responds_to": standing.msg_id}
                    ).sign(me.agent_key)
            rounds += 1
            if isinstance(answer, Accept):
                self._msg(deal_id, "accept", answer.price, "accepts the standing price")
            else:
                self._msg(deal_id, "counter", answer.price, f"counters {answer.price} against {standing.price}")
            reply = self._send(tw, seller, answer)
            turns += 2
            if isinstance(answer, Accept) and (not isinstance(reply, Accept) or reply.price != answer.price):
                raise PurchaseError("the seller did not confirm the price it quoted")
            if isinstance(answer, Counter) and isinstance(reply, Accept) and reply.price != answer.price:
                raise PurchaseError("the seller accepted a different price than the one offered")
        return reply.price, turns

    def _accept(self, standing: Counter, deal_id: str) -> Accept:
        return Accept(
            sender=self.identity.agent_id,
            responds_to=standing.msg_id,
            good=self.good,
            price=standing.price,
            deadline=datetime.now() + timedelta(hours=24),
            deal_id=deal_id,
        ).sign(self.identity.agent_key)

    def _contract(self, tw: PeerLink, seller: PeerKey, deal_id: str, price: Decimal) -> Contract:
        now = datetime.now()
        mine = Contract(
            deal_id=deal_id,
            buyer=self.identity.agent_id,
            seller=seller.agent_id,
            good=self.good,
            price=price,
            deadline=now + timedelta(hours=1),
            formed_at=now,
        ).sign(self.identity.agent_id, self.identity.agent_key)
        reply = tw.skill("sign-contract", {"contract": mine.model_dump(mode="json")})
        if not isinstance(reply, Task) or reply.status.state != TaskState.COMPLETED:
            refused = refusal_of(reply) if isinstance(reply, Task) else None
            raise PurchaseError(f"{seller.agent_id} would not sign: {(refused or {}).get('detail')}")
        signed = Contract.model_validate(result_data(reply)["contract"])
        theirs = signed.signatures.get(seller.agent_id, "")
        if signed.digest() != mine.digest() or not verify(seller.public_key, theirs, signed.digest().encode("ascii")):
            raise PurchaseError("the countersigned contract is not signed by the seller's published key")
        return signed

    def _ensure_registered(self, trustee: PeerLink, seller_id: str) -> None:
        """Principal, agent and a spend mandate that covers this seller, published once and widened only as needed."""
        url = self.trustee_url or ""
        me = self.identity
        spend = self._spend.setdefault(url, _Spend())
        sellers = spend.sellers | {seller_id}
        categories = spend.categories | {self.good.category}
        if url in self._registered and sellers == spend.sellers and categories == spend.categories:
            return
        mandate = Mandate(
            agent_id=me.agent_id,
            principal=me.principal,
            max_amount=self.mandate_ceiling,
            allowed_categories=categories,
            allowed_counterparties=sellers,
            grants="spend",
            version=spend.version + 1,
        )
        try:
            if url in self._registered:
                publish_mandate(trustee, me, mandate)
            else:
                register_with_trustee(trustee, me, mandate)
        except (PeerError, A2AError) as exc:
            raise PurchaseError(str(exc)) from None
        self._registered.add(url)
        self._spend[url] = _Spend(mandate.version, categories, sellers)

    def _tell_seller(self, tw: PeerLink, deal_id: str, contract: Contract) -> None:
        reply = tw.skill("escrow-opened", {"deal_id": deal_id, "contract_digest": contract.digest()})
        if not isinstance(reply, Task) or reply.status.state != TaskState.COMPLETED:
            refused = refusal_of(reply) if isinstance(reply, Task) else None
            raise PurchaseError(f"the seller would not watch the escrow: {(refused or {}).get('detail')}")

    def _await_funds(self, trustee: PeerLink, task: Task) -> tuple[str, Task] | None:
        """Wait for the payer to approve at PayPal, if the task asks for it. Returns (outcome, task) to stop."""
        deal_id = task.id
        if task.status.state != TaskState.AUTH_REQUIRED:
            return None
        meta = (task.metadata or {}).get("trustee") or {}
        if self.on_approval is not None:
            self.on_approval(deal_id, str(meta.get("orderId", "")), str(meta.get("approveUrl", "")))
        abort: list[str] = []

        def tick() -> None:
            # The hook may return a reason to stop waiting (PayPal refused the approved order for good).
            reason = self.poll_hook() if self.poll_hook is not None else None
            if reason:
                abort.append(reason)

        waited = self.waiter.until(
            lambda: bool(abort) or trustee.get_task(deal_id).status.state != TaskState.AUTH_REQUIRED, tick=tick
        )
        current = trustee.get_task(deal_id)
        if not waited or abort:
            try:
                current = trustee.cancel_task(deal_id)
            except A2AError:
                current = trustee.get_task(deal_id)  # the payer approved while we were cancelling
                if current.status.state == TaskState.INPUT_REQUIRED:
                    return None
            return ("approval_failed" if abort else "approval_timeout"), current
        if current.status.state == TaskState.CANCELED:
            return "approval_cancelled", current
        if current.status.state in TERMINAL_STATES:
            return f"deal_{state_name(current).lower()}", current
        return None

    def _await_delivery(self, deal_id: str) -> InboxItem | None:
        self.waiter.until(lambda: deal_id in self.inbox)
        return self.inbox.get(deal_id)

    def _store(self, deal_id: str) -> ArtifactStore:
        inbox = self._inbox_dir(deal_id)
        return ArtifactStore(goods_dir=inbox, manifest={}, inbox=inbox)

    def _buyer(self, deal_id: str) -> BuyerAgent:
        return BuyerAgent(
            self.identity.agent_id,
            self.llm,
            self.good,
            self.budget,
            self._store(deal_id),
            checkers=self.checkers,
            ledger=self.ledger,
        )


# ------------------------------------------------------------------------------------------ factory


def create_northwind(**overrides: Any) -> Northwind:
    """Northwind wanting the demo dataset on the honest scenario's budget; `overrides` replace any argument."""
    from ..arena.runner import default_checkers
    from ..arena.scenario import get_scenario
    from .common import default_llm

    scenario = get_scenario("honest")
    settings: dict[str, Any] = {
        "good": scenario.good,
        "budget": scenario.budget,
        "llm": default_llm("auto"),
        "checkers": default_checkers(),
    }
    settings.update(overrides)
    return Northwind(**settings)


def main(argv: list[str] | None = None) -> int:
    from .app import running_server
    from .common import default_llm

    parser = argparse.ArgumentParser(description="Run Northwind's planning agent as a standalone A2A server.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8101)
    parser.add_argument("--trustee", default=os.environ.get("TRUSTEE_URL", "http://127.0.0.1:8000"))
    parser.add_argument(
        "--public-url", default=None, help="where peers can reach this agent (default http://HOST:PORT)"
    )
    parser.add_argument("--mode", choices=["auto", "live", "scripted"], default="auto")
    parser.add_argument("--operator-token", default=os.environ.get("TRUSTEE_OPERATOR_TOKEN"))
    parser.add_argument("--buy", metavar="SELLER_URL", default=None, help="buy once from this seller, then exit")
    args = parser.parse_args(argv)

    public = args.public_url or f"http://{args.host}:{args.port}"
    agent = create_northwind(
        trustee_url=args.trustee,
        public_url=public,
        operator_token=args.operator_token,
        llm=default_llm(args.mode),
        on_approval=lambda deal, order, url: print(f"approve the PayPal payment for {deal}: {url}"),
        observer=lambda hop: print(hop.line()),
    )
    print(f"Northwind {agent.identity.agent_id} on {public}  (card: {public}/.well-known/agent-card.json)")
    with running_server(agent.app, args.host, args.port, keep_alive=args.buy is None):
        if args.buy:
            purchase = agent.buy(args.buy)
            print(f"outcome: {purchase.outcome or 'finished'}; receipt: {purchase.receipt}")
            return 0 if purchase.settled else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

__all__ = ["InboxItem", "Northwind", "Purchase", "PurchaseError", "create_northwind"]
