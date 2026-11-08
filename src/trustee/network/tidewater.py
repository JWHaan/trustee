"""Tidewater Data Co.'s sales agent as a real A2A agent.

Tidewater sells the Q3 returns dataset. It answers three skills for a buyer that found it by Agent Card:

* `quote`: the good (including the sha256 both sides will fix in the contract) and the asking price.
* `negotiate`: a signed Offer, Counter or Accept in, a signed Counter or Accept out. The reply comes from the
  existing `SellerAgent` (LLM proposes, a hard floor clamp disposes), and a reply that would sell below the
  floor is turned into a counter at the floor. The model never sees the floor and cannot move it.
* `sign-contract`: the contract the buyer signed. Tidewater checks the buyer's signature against the key in the
  buyer's Agent Card, checks every term against what it accepted (buyer, price, good, deadline), and only then
  adds its own signature. It also registers itself, its principal and a `receive` mandate at Trustee.

After the escrow opens, the buyer tells Tidewater with `escrow-opened`. Tidewater confirms the deal exists at
Trustee, registers a push-notification webhook on the deal task, and waits. It does not poll. When Trustee posts
"funds held" (`INPUT_REQUIRED`) to `/a2a/push`, Tidewater sends the CSV to the buyer as an A2A FilePart
(`receive-delivery`) and then tells Trustee with a signed `record-delivery`. The push is authenticated by a
per-deal token that only Tidewater and Trustee know.

Run it alone: `python -m trustee.network.tidewater --port 8102 --trustee http://127.0.0.1:8000`.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import logging
import os
import secrets
import threading
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse

from ..a2a.client import A2AClient, artifact_data, push_token_matches, task_from_push
from ..a2a.models import TERMINAL_STATES, A2AError, Part, Task, TaskState
from ..agents.buyer import ArtifactStore
from ..agents.seller import SellerAgent
from ..identity import verify
from ..ledger import Ledger
from ..llm import LLM
from ..mandate import Mandate
from ..paypal import sandbox_emails
from ..protocol import Accept, Contract, Counter, Delivery, GoodSpec
from .backoffice import Backoffice, BookingResult
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
    discover_peer,
    move_is_signed_by,
    parse_move,
    refusal_of,
    register_with_trustee,
    state_name,
)

log = logging.getLogger(__name__)

MAX_DEALS = 1000


@dataclass
class _Terms:
    """What Tidewater has said and accepted on one deal."""

    buyer: str
    agent: SellerAgent
    standing: Decimal | None = None  # the price Tidewater last put on the table
    standing_msg: str = ""
    accepted: Decimal | None = None
    signed: Contract | None = None


def junk_ref(ref: str) -> str:
    """`returns-2026q3.csv` -> `returns-2026q3-junk.csv`, the corrupt twin the cheat variant ships."""
    p = Path(ref)
    return f"{p.stem}-junk{p.suffix}"


class Tidewater(NetworkAgent):
    card_name = "Tidewater Data Co. sales agent"
    card_description = (
        "Sells the Q3 2026 online-retail returns dataset (CSV) for Tidewater Data Co. Find it by this card, ask for a "
        "quote, negotiate over A2A with signed offers, sign the contract, then open an escrow at Trustee. Payment "
        "is held by Trustee; this agent ships when Trustee says the funds are held."
    )
    org_name = "Tidewater Data Co."

    def __init__(
        self,
        *,
        good: GoodSpec,
        ask: Decimal,
        floor: Decimal,
        store: ArtifactStore,
        llm: LLM,
        cheat: bool = False,
        trustee_url: str | None = None,
        operator_token: str | None = None,
        ledger: Ledger | None = None,
        receive_ceiling: Decimal = Decimal("500.00"),
        identity: AgentIdentity | None = None,
        http: httpx.Client | None = None,
        public_url: str | None = None,
        observer: HopObserver | None = None,
        backoffice: Backoffice | None = None,
    ):
        super().__init__(
            identity or AgentIdentity.generate("tidewater-sales", "tidewater-data-co", "seller", sandbox_emails()[1]),
            http=http,
            public_url=public_url,
            observer=observer,
        )
        # Zapier MCP bookings. None reads TIDEWATER_ZAPIER_* from the environment; without them this is a no-op.
        self.backoffice = backoffice or Backoffice.from_env("tidewater", on_result=self._booking_result)
        self.good = good
        self.ask = ask
        self.floor = floor
        self.store = store
        self.llm = llm
        self.cheat = cheat
        self.trustee_url = trustee_url.rstrip("/") if trustee_url else None
        self.operator_token = operator_token
        self.ledger = ledger
        self.receive_ceiling = receive_ceiling
        self.peers: dict[str, PeerKey] = {}  # agent id -> the key its Agent Card showed on first contact
        self.deals: OrderedDict[str, _Terms] = OrderedDict()
        self.watch_tokens: dict[str, str] = {}  # deal id -> the secret Trustee must echo in its push
        self.delivered: set[str] = set()
        self.pushes: list[tuple[str, str]] = []  # (deal id, task state) for every authenticated push
        self.deliveries: list[dict[str, str]] = []  # what was shipped, and what triggered it
        self._lock = threading.RLock()
        self._delivery_locks: dict[str, threading.Lock] = {}
        self._registered: set[str] = set()
        self._clients: dict[tuple[str, int, str], A2AClient] = {}
        for skill in (
            SkillDef(
                "quote",
                "Quote the returns dataset",
                "The good on offer (description, criteria, deliverable name, sha256) and the asking price.",
                ["sales", "quote"],
                ["How much for the Q3 returns dataset?"],
                self._quote,
            ),
            SkillDef(
                "negotiate",
                "Negotiate the price",
                'Input {"move": <signed Offer|Counter|Accept>, "buyer_card": <URL of the buyer\'s Agent Card>}. '
                "Replies with a signed Counter or Accept. Unsigned or forged moves are refused.",
                ["negotiation", "signed"],
                ["Offer 144.00 USD for the dataset"],
                self._negotiate,
            ),
            SkillDef(
                "sign-contract",
                "Countersign the contract",
                'Input {"contract": <Contract signed by the buyer>}. Tidewater checks every term against what it '
                "accepted and returns the contract with its own signature added.",
                ["contract", "signed"],
                ["Countersign contract deal_ab12"],
                self._sign_contract,
            ),
            SkillDef(
                "escrow-opened",
                "The escrow is open",
                'Input {"deal_id", "contract_digest"}. Tidewater confirms the deal at Trustee and registers a push '
                "webhook on it, so it hears when the funds are held.",
                ["escrow", "push"],
                ["Deal deal_ab12 is open at Trustee"],
                self._escrow_opened,
            ),
        ):
            self.add_skill(skill)
        self.app.add_api_route("/a2a/push", self._push_route, methods=["POST"], include_in_schema=False)

    # ------------------------------------------------------------------------------ plumbing

    def _trustee(self) -> PeerLink:
        if not self.trustee_url:
            raise SkillRefused(500, "this agent has no Trustee URL configured")
        auth = f"Bearer {self.operator_token}" if self.operator_token else None
        key = (self.trustee_url, id(self.http), auth or "")
        client = self._clients.get(key)
        if client is None:
            client = self._clients[key] = A2AClient(self.trustee_url, http=self.http, authorization=auth)
        return self.link("trustee", client)

    def _terms(self, deal_id: str, buyer: str) -> _Terms:
        with self._lock:
            terms = self.deals.get(deal_id)
            if terms is None:
                agent = SellerAgent(
                    agent_id=self.identity.agent_id,
                    llm=self.llm,
                    good=self.good,
                    floor_price=self.floor,
                    target_price=self.ask,
                    store=self.store,
                    ledger=self.ledger,
                )
                terms = self.deals[deal_id] = _Terms(buyer=buyer, agent=agent)
                while len(self.deals) > MAX_DEALS:
                    self.deals.popitem(last=False)
            elif terms.buyer != buyer:
                raise SkillRefused(409, f"deal {deal_id} belongs to another buyer")
            return terms

    def _peer_for(self, sender: str, card_url: str) -> PeerKey:
        """The key to check `sender`'s signature against: pinned on first contact from the sender's own card."""
        peer = self.peers.get(sender)
        if peer is not None:
            return peer
        if not card_url:
            raise SkillRefused(422, "buyer_card (the URL of your Agent Card) is required on first contact")
        try:
            found = discover_peer(self.http, card_url)
        except (PeerError, httpx.HTTPError) as exc:
            raise SkillRefused(401, f"could not read {sender}'s Agent Card: {exc}") from None
        if found.agent_id != sender:
            raise SkillRefused(401, f"the Agent Card at {card_url} belongs to {found.agent_id}, not {sender}")
        self.peers[sender] = found
        return found

    # --------------------------------------------------------------------------------- skills

    def _quote(self, call: Call) -> dict[str, Any]:
        return {
            "summary": f"{self.good.description}: asking {self.ask} USD.",
            "seller": self.identity.agent_id,
            "good": self.good.model_dump(mode="json"),
            "ask": str(self.ask),
            "currency": "USD",
        }

    def _negotiate(self, call: Call) -> dict[str, Any]:
        move = parse_move(call.input.get("move"))
        peer = self._peer_for(move.sender, str(call.input.get("buyer_card") or ""))
        if not move_is_signed_by(move, peer):
            raise SkillRefused(401, f"the move is not signed by the key {move.sender} published in its Agent Card")
        if move.good.model_dump(mode="json") != self.good.model_dump(mode="json"):
            raise SkillRefused(422, "the good in the move is not the good quoted")
        deal_id = str(move.deal_id)
        with self._lock:
            terms = self._terms(deal_id, move.sender)
            if terms.accepted is not None:
                raise SkillRefused(409, f"deal {deal_id} already has an accepted price")
            reply: Accept | Counter
            if isinstance(move, Accept):
                if terms.standing is None or move.price != terms.standing or move.responds_to != terms.standing_msg:
                    raise SkillRefused(409, "that accept does not answer the price Tidewater last quoted")
                reply = Accept(
                    sender=self.identity.agent_id,
                    responds_to=move.msg_id,
                    good=self.good,
                    price=move.price,
                    deadline=datetime.now() + timedelta(hours=24),
                    deal_id=deal_id,
                )
            else:
                answer = terms.agent.respond_to(move)
                # The floor is policy, not a suggestion: a model that says "accept" below it gets a counter instead.
                if isinstance(answer, Accept) and answer.price < self.floor:
                    later = datetime.now() + timedelta(hours=24)
                    answer = Counter(
                        sender=self.identity.agent_id,
                        deal_id=deal_id,
                        responds_to=move.msg_id,
                        good=self.good,
                        price=self.floor,
                        deadline=later,
                        valid_until=later,
                    )
                reply = answer.model_copy(update={"deal_id": deal_id})
            reply = reply.sign(self.identity.agent_key)
            if isinstance(reply, Accept):
                terms.accepted = reply.price
            else:
                terms.standing, terms.standing_msg = reply.price, reply.msg_id
        return {
            "summary": f"{reply.kind} at {reply.price} USD",
            "move": reply.model_dump(mode="json"),
        }

    def _sign_contract(self, call: Call) -> dict[str, Any]:
        try:
            contract = Contract.model_validate(call.input.get("contract"))
        except ValueError:
            raise SkillRefused(422, "input.contract is not a valid contract") from None
        with self._lock:
            terms = self.deals.get(contract.deal_id)
            if terms is None or terms.accepted is None:
                raise SkillRefused(409, f"no accepted price on record for deal {contract.deal_id}")
            if terms.signed is not None:
                if terms.signed.digest() == contract.digest():
                    return self._signed_reply(terms.signed)
                raise SkillRefused(409, f"deal {contract.deal_id} was already countersigned with different terms")
            self._check_terms(contract, terms)
            peer = self.peers[terms.buyer]
            if not verify(peer.public_key, contract.signatures.get(terms.buyer, ""), contract.digest().encode("ascii")):
                raise SkillRefused(401, f"the contract is not signed by the key {terms.buyer} published in its card")
            try:
                self._ensure_registered()
            except (PeerError, A2AError, httpx.HTTPError) as exc:
                raise SkillRefused(502, f"could not register with Trustee: {exc}") from None
            signed = contract.sign(self.identity.agent_id, self.identity.agent_key)
            terms.signed = signed
        return self._signed_reply(signed)

    @staticmethod
    def _signed_reply(contract: Contract) -> dict[str, Any]:
        return {"summary": "contract countersigned", "contract": contract.model_dump(mode="json")}

    def _check_terms(self, contract: Contract, terms: _Terms) -> None:
        problems = []
        if contract.seller != self.identity.agent_id:
            problems.append("seller is not this agent")
        if contract.buyer != terms.buyer:
            problems.append("buyer is not the agent that negotiated")
        if contract.price != terms.accepted:
            problems.append(f"price {contract.price} is not the accepted {terms.accepted}")
        if contract.currency != "USD":
            problems.append("currency is not USD")
        if contract.good.model_dump(mode="json") != self.good.model_dump(mode="json"):
            problems.append("the good differs from the quote")
        now = datetime.now()
        if contract.deadline <= now:
            problems.append("the deadline has passed")
        if contract.deadline > now + timedelta(days=30):
            problems.append("the deadline is more than 30 days away")
        if problems:
            raise SkillRefused(409, "terms differ from what Tidewater accepted: " + "; ".join(problems))

    def _ensure_registered(self) -> None:
        url = self.trustee_url or ""
        if url in self._registered:
            return
        mandate = Mandate(
            agent_id=self.identity.agent_id,
            principal=self.identity.principal,
            max_amount=self.receive_ceiling,
            allowed_categories=frozenset({self.good.category}),
            allowed_counterparties=None,
            grants="receive",
            version=1,
        )
        register_with_trustee(self._trustee(), self.identity, mandate)
        self._registered.add(url)

    def _escrow_opened(self, call: Call) -> dict[str, Any]:
        deal_id, digest = str(call.input.get("deal_id", "")), str(call.input.get("contract_digest", ""))
        with self._lock:
            terms = self.deals.get(deal_id)
            if terms is None or terms.signed is None or terms.signed.digest() != digest:
                raise SkillRefused(409, f"deal {deal_id} is not a contract Tidewater signed")
            if deal_id in self.watch_tokens:
                return {"summary": "already watching", "registered": True}
            if not self.public_url:
                raise SkillRefused(500, "this agent has no public URL, so Trustee cannot reach its push webhook")
            trustee = self._trustee()
            try:
                task = trustee.get_task(deal_id)
            except A2AError:
                raise SkillRefused(404, f"Trustee has no deal {deal_id}") from None
            if ((task.metadata or {}).get("trustee") or {}).get("contractDigest") != digest:
                raise SkillRefused(409, "the deal at Trustee has different terms than the contract Tidewater signed")
            token = secrets.token_urlsafe(24)
            # Known before Trustee can use it: the first push may arrive before set_push returns.
            self.watch_tokens[deal_id] = token
            try:
                trustee.set_push(deal_id, f"{self.public_url}/a2a/push", token, send_current_state=True)
            except A2AError as exc:
                self.watch_tokens.pop(deal_id, None)
                raise SkillRefused(502, f"Trustee refused the push webhook: {exc.message}") from None
        return {"summary": "watching the deal; will ship when funds are held", "registered": True}

    # ------------------------------------------------------------------------- push receiver

    async def _push_route(self, request: Request) -> JSONResponse:
        import asyncio

        try:
            task = task_from_push(await request.json())
        except Exception:  # noqa: BLE001 - anything unparseable is the sender's problem
            return JSONResponse({"detail": "not a task notification"}, status_code=400)
        token = self.watch_tokens.get(task.id)
        if token is None or not push_token_matches(request.headers, token):
            return JSONResponse({"detail": "unknown deal or bad notification token"}, status_code=401)
        try:
            await asyncio.to_thread(self._on_push, task)
        except Exception as exc:  # noqa: BLE001 - a 5xx makes Trustee retry; the delivery is idempotent
            log.exception("delivery for %s failed", task.id)
            return JSONResponse({"detail": f"delivery failed: {str(exc)[:200]}"}, status_code=500)
        return JSONResponse({"ok": True})

    def _on_push(self, task: Task) -> None:
        state = state_name(task)
        self.pushes.append((task.id, state))
        self.hop("trustee", self.identity.label, "push", "task", state)
        if task.status.state == TaskState.INPUT_REQUIRED:
            self._deliver(task.id, "push")
        if self.backoffice.enabled:
            self._book_settlement(task)

    def _book_settlement(self, task: Task) -> None:
        """On a finished deal, book the signed settlement receipt in Tidewater's own apps. Never raises."""
        try:
            receipt = artifact_data(task, "settlement-receipt")
            if receipt is None and task.status.state in TERMINAL_STATES:
                receipt = artifact_data(self._trustee().get_task(task.id), "settlement-receipt")
            if receipt is None:
                return
            terms = self.deals.get(task.id)
            buyer = terms.buyer if terms is not None else ""
            peer = self.peers.get(buyer)
            self.backoffice.book_receipt(receipt, (peer.name or peer.label) if peer else buyer)
        except Exception:  # noqa: BLE001 - a booking must never change the deal
            log.exception("settlement booking failed for deal %s", task.id)

    def _booking_result(self, result: BookingResult) -> None:
        """Show each back-office booking as a hop on the arena tape. Carries no arguments."""
        note = "booked" if result.ok else f"failed ({result.error})"
        self.hop(self.identity.agent_id, "zapier", "mcp", result.action, note)

    def _hand_to_trustee(self, deal_id: str, payload: bytes) -> None:
        """Give Trustee the same bytes, so it checks the file itself instead of relying on the buyer's word.
        Best effort: a Trustee without the upload route still settles on the buyer's check."""
        if not self.trustee_url:
            return
        try:
            resp = self.http.post(
                f"{self.trustee_url}/v1/deals/{deal_id}/deliverable",
                content=payload,
                headers={"Content-Type": "application/octet-stream"},
            )
            note = f"file handed to Trustee ({resp.status_code})"
        except httpx.HTTPError as exc:
            note = f"could not hand the file to Trustee ({type(exc).__name__})"
        self.hop(self.identity.agent_id, "trustee", "http", "upload-deliverable", note)

    def _deliver(self, deal_id: str, trigger: str) -> None:
        """Ship the CSV to the buyer, then tell Trustee. Safe to run twice: the second run does nothing."""
        with self._lock:
            gate = self._delivery_locks.setdefault(deal_id, threading.Lock())
        with gate:
            if deal_id in self.delivered:
                return
            terms = self.deals.get(deal_id)
            if terms is None or terms.signed is None:
                raise RuntimeError(f"no signed contract for deal {deal_id}")
            contract = terms.signed
            ref = contract.good.deliverable_ref
            source = self.store.goods_dir / (junk_ref(ref) if self.cheat else ref)
            if not source.is_file():
                raise RuntimeError(f"artifact missing from the store: {source.name}")
            payload = source.read_bytes()
            delivery = Delivery(
                sender=self.identity.agent_id,
                deal_id=deal_id,
                deliverable_ref=ref,
                content_hash=hashlib.sha256(payload).hexdigest(),
            ).sign(self.identity.agent_key)
            if self.ledger is not None:
                self.ledger.append(
                    deal_id,
                    self.identity.agent_id,
                    "delivery",
                    event="deliver_cheat" if self.cheat else "deliver",
                    ref=ref,
                    sha256=delivery.content_hash[:16],
                    note=(
                        "DEMO: corrupt deliverable shipped under the contracted name"
                        if self.cheat
                        else f"delivered {source.name} ({len(payload)} bytes) as an A2A file part"
                    ),
                )
            peer = self.peers[terms.buyer]
            buyer = self.link(peer.label, A2AClient(card_url=peer.card_url, http=self.http))
            media = "text/csv" if ref.endswith(".csv") else "application/octet-stream"
            file = Part(raw=base64.b64encode(payload).decode("ascii"), filename=ref, mediaType=media)
            sent = buyer.skill(
                "receive-delivery",
                {"delivery": delivery.model_dump(mode="json"), "contract_digest": contract.digest()},
                files=[file],
            )
            if not isinstance(sent, Task) or sent.status.state != TaskState.COMPLETED:
                detail = (refusal_of(sent) or {}).get("detail") if isinstance(sent, Task) else "no task"
                raise RuntimeError(f"the buyer did not accept the delivery: {detail}")
            self._hand_to_trustee(deal_id, payload)
            recorded = self._trustee().skill("record-delivery", delivery.model_dump(mode="json"), task_id=deal_id)
            refused = refusal_of(recorded) if isinstance(recorded, Task) else None
            if refused is not None:
                raise RuntimeError(f"Trustee refused the delivery: {refused.get('detail')}")
            self.delivered.add(deal_id)
            self.deliveries.append({"deal_id": deal_id, "trigger": trigger, "sha256": delivery.content_hash})


# ------------------------------------------------------------------------------------------ factory


def create_tidewater(**overrides: Any) -> Tidewater:
    """Tidewater with the demo goods and the honest scenario's prices; `overrides` replace any constructor argument."""
    from ..arena.scenario import FIXTURES, get_scenario
    from .common import default_llm

    scenario = get_scenario("honest")
    settings: dict[str, Any] = {
        "good": scenario.good,
        "ask": scenario.ask,
        "floor": scenario.floor,
        "store": ArtifactStore.load(FIXTURES),
        "llm": default_llm("auto"),
    }
    settings.update(overrides)
    return Tidewater(**settings)


def main(argv: list[str] | None = None) -> int:
    from .app import serve_forever

    parser = argparse.ArgumentParser(description="Run Tidewater's sales agent as a standalone A2A server.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8102)
    parser.add_argument("--trustee", default=os.environ.get("TRUSTEE_URL", "http://127.0.0.1:8000"))
    parser.add_argument(
        "--public-url", default=None, help="where Trustee can reach this agent (default http://HOST:PORT)"
    )
    parser.add_argument("--mode", choices=["auto", "live", "scripted"], default="auto")
    parser.add_argument("--cheat", action="store_true", help="ship the corrupt twin of the dataset")
    parser.add_argument("--operator-token", default=os.environ.get("TRUSTEE_OPERATOR_TOKEN"))
    args = parser.parse_args(argv)

    from .common import default_llm

    public = args.public_url or f"http://{args.host}:{args.port}"
    agent = create_tidewater(
        trustee_url=args.trustee,
        public_url=public,
        cheat=args.cheat,
        operator_token=args.operator_token,
        llm=default_llm(args.mode),
    )
    print(f"Tidewater {agent.identity.agent_id} on {public}  (card: {public}/.well-known/agent-card.json)")
    serve_forever(agent.app, args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

__all__ = ["Tidewater", "create_tidewater", "junk_ref"]
