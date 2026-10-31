"""Seller agent — counter toward target, never below floor, deliver with real hashes.

Policy-first, like the buyer: the LLM proposes the counter step, a hard clamp
keeps the price at or above the floor so a chatty model can never sell below
the walk-away. Delivery is hash-recorded against the artifact store; a
`cheat=True` flag ships a junk artifact's hash so demo run 2 is a one-line
setup, not a second code path.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..ledger import Ledger
from ..llm import LLM
from ..protocol import Accept, Contract, Counter, Delivery, GoodSpec
from .buyer import ArtifactStore
from .negotiator import Negotiator

log = logging.getLogger(__name__)

SELLER_HAGGLE_SYSTEM = """\
You are the seller in a price negotiation. The buyer keeps cutting your price.
For each of their offers, reply with ONLY a JSON object:
  counter: {"kind":"counter","price":<number>}
  accept:  {"kind":"accept","price":<number>}
Rules:
- Concede at most ~10% of the gap toward their price per turn.
- If their price is already at or above your floor, accept it.
- Never emit anything but the JSON object.
"""


def price_of(msg: Any) -> Decimal:
    """Pull a Decimal price off a protocol message or a raw dict."""
    raw = getattr(msg, "price", None)
    if raw is None and isinstance(msg, dict):
        raw = msg.get("price")
    return Decimal(str(raw))


class SellerAgent(Negotiator):
    def __init__(
        self,
        agent_id: str,
        llm: LLM,
        good: GoodSpec,
        floor_price: Decimal,
        target_price: Decimal,
        store: ArtifactStore,
        ledger: Ledger | None = None,
    ):
        super().__init__(agent_id=agent_id, role="seller", llm=llm, good=good)
        self.floor_price = floor_price
        self.target_price = target_price
        self.store = store
        self.ledger = ledger

    # -------------------------------------------------------- negotiation side

    def respond_to(self, their: Any) -> Accept | Counter:
        """Answer a buyer offer/counter. Accept when at-or-above floor, else counter."""
        their_price = price_of(their)

        # Policy gate first: at-or-above floor is a deal, no LLM needed.
        if their_price >= self.floor_price:
            return self._accept(their, their_price)

        move = self._propose_counter(their_price)
        if move is None:
            # LLM unresponsive: deterministic floor counter (max concession).
            log.info("%s: LLM unresponsive — countering at floor %s", self.agent_id, self.floor_price)
            return self._counter(their, their_price, self.floor_price)
        proposed, is_accept = move
        if is_accept:
            return self._accept(their, their_price)
        return self._counter(their, their_price, proposed)

    def _accept(self, their: Any, price: Decimal) -> Accept:
        accept = Accept(
            sender=self.agent_id,
            responds_to=msg_id_of(their),
            good=self.good,
            price=price,
            deadline=_future(),
            deal_id=deal_id_of(their),
        )
        self._log(
            "msg",
            deal_id=deal_id_of(their),
            event="accept",
            price=str(accept.price),
            note="at or above floor — deal accepted",
        )
        return accept

    def _counter(self, their: Any, their_price: Decimal, proposed: Decimal) -> Counter:
        # Hard clamp: the seller can never move below floor, even if the LLM asks.
        proposed = max(proposed, self.floor_price)
        counter = Counter(
            sender=self.agent_id,
            deal_id=deal_id_of(their),
            responds_to=msg_id_of(their),
            good=self.good,
            price=proposed.quantize(Decimal("0.01")),
            deadline=_future(),
            valid_until=_future(),
        )
        self._log(
            "msg",
            deal_id=deal_id_of(their),
            event="counter",
            price=str(counter.price),
            note=f"their {their_price} < floor {self.floor_price}",
        )
        return counter

    def _propose_counter(self, their_price: Decimal) -> tuple[Decimal, bool] | None:
        """Ask the LLM for a step; returns (price, is_accept) or None when it fails."""
        hint = {"their_price": float(their_price), "floor": float(self.floor_price), "target": float(self.target_price)}
        try:
            obj = self._propose(
                {
                    "instruction": "Respond to their latest offer.",
                    "their_message": hint,
                },
                system=SELLER_HAGGLE_SYSTEM,
            )
            if obj.get("kind") == "accept":
                return Decimal("0"), True
            return Decimal(str(obj.get("price", 0))), False
        except ValueError:
            return None

    # ----------------------------------------------------------- delivery side

    def deliver(self, contract: Contract, cheat: bool = False) -> Delivery:
        """Hand off the deliverable by actually moving bytes into the buyer's
        inbox. Honest: the contracted artifact. Cheat: its corrupt twin, sent
        under the contracted name."""
        ref = contract.good.deliverable_ref
        source = self.store.goods_dir / (_junk_ref(ref) if cheat else ref)
        if not source.exists():
            raise RuntimeError(f"artifact missing from store: {source.name}")
        content_hash = self.store.receive(ref, source.read_bytes())
        self._log(
            "delivery",
            deal_id=contract.deal_id,
            event="deliver_cheat" if cheat else "deliver",
            ref=ref,
            sha256=content_hash[:16],
            note=(
                "DEMO: corrupt deliverable shipped under the contracted name"
                if cheat
                else f"delivered {source.name} ({source.stat().st_size} bytes)"
            ),
        )
        delivery = Delivery(
            sender=self.agent_id,
            deal_id=contract.deal_id,
            deliverable_ref=ref,
            content_hash=content_hash,
        )
        if self.ledger is not None:
            dump = delivery.model_dump(mode="json")
            dump.pop("deal_id", None)
            dump.pop("kind", None)
            self.ledger.append(contract.deal_id, self.agent_id, "msg", **dump)
        return delivery

    # ------------------------------------------------------------ internals

    def _log(self, kind: str, deal_id: str | None = None, **payload: Any) -> None:
        if self.ledger is not None:
            self.ledger.append(deal_id, self.agent_id, kind, **payload)


def _junk_ref(ref: str) -> str:
    """`returns-2026q3.csv` -> `returns-2026q3-junk.csv`."""
    p = Path(ref)
    return f"{p.stem}-junk{p.suffix}"


def _future() -> datetime:
    return datetime.now() + timedelta(hours=24)


def msg_id_of(msg: Any) -> str:
    return str(getattr(msg, "msg_id", None) or (msg.get("msg_id", "prev") if isinstance(msg, dict) else "prev"))


def deal_id_of(msg: Any) -> str | None:
    val = getattr(msg, "deal_id", None)
    if val is None and isinstance(msg, dict):
        val = msg.get("deal_id")
    return val or None
