"""Shared negotiation base for buyer/seller agents.

LLM handles the haggle (natural-language turns). Contract emission is
constrained: the LLM proposes fields, Pydantic validates, and we retry on a
validity error. A max-turns cap auto-accepts the last viable offer so a
lively haggle can never wedge the demo.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, cast

from pydantic import BaseModel

from ..llm import LLM
from ..protocol import Accept, Counter, GoodSpec, Offer

log = logging.getLogger(__name__)

NEGOTIATE_SYSTEM = """\
You are a professional negotiator for __ROLE__. You negotiate over a machine protocol:
each turn you emit ONLY a JSON object:
  offer:   {"kind":"offer","good":{...},"price":<number>}
  counter: {"kind":"counter","good":{...},"price":<number>,"responds_to":"<msg_id>"}
  accept:  {"kind":"accept","responds_to":"<msg_id>","price":<number>,"good":{...}}

Rules:
- Move price no more than ~10% per turn, same direction as your objective.
- Never emit a price below your walk-away (or, as a seller, above your ceiling).
- When the other side's last message is acceptable, accept it.
- Never emit anything but the JSON object.
"""


def negotiate_system(role: str) -> str:
    return NEGOTIATE_SYSTEM.replace("__ROLE__", role)


class Negotiator:
    def __init__(
        self,
        agent_id: str,
        role: str,
        llm: LLM,
        good: GoodSpec,
        transcript: list[dict[str, str]] | None = None,
    ):
        self.agent_id = agent_id
        self.role = role
        self.llm = llm
        self.good = good
        self.transcript: list[dict[str, str]] = transcript if transcript is not None else []
        self._last_emitted: dict[str, Any] = {}

    # ------------------------------------------------------------ public API

    def open_deal(self, opening_price: Decimal, other: str) -> Offer:
        """Buyer-side entry: first message is always an offer."""
        obj = self._propose(
            {
                "instruction": f"Open the deal with an offer to {other} for '{self.good.description}'.",
                "hint": {"kind": "offer", "good": self.good.model_dump(mode="json"), "price": float(opening_price)},
            }
        )
        return cast("Offer", self._coerce(Offer, obj))

    def respond(self, their_msg: dict[str, Any], open_price: Decimal, max_turns: int) -> Accept | Counter:
        """Continue the haggle; return a Counter, or Accept when we concede."""
        for _ in range(max_turns):
            try:
                obj = self._propose(
                    {
                        "instruction": "Respond to their message below. Concede (accept) when within your target.",
                        "their_message": their_msg,
                        "hint": {"open_price": float(open_price)},
                    }
                )
                if obj.get("kind") == "accept":
                    return cast("Accept", self._coerce(Accept, obj))
                return cast("Counter", self._coerce(Counter, obj))
            except ValueError:
                their_msg = self._nudge()
        # Cap hit: deterministically accept at the open price. The trust layer
        # (mandate + escrow) still vets the deal — the cap only ends the chat.
        return self._fallback_accept(open_price)

    # ------------------------------------------------------------ internals

    def _propose(self, extra: dict[str, Any], system: str | None = None) -> dict[str, Any]:
        ctx = json.dumps(extra, default=str)
        raw = self.llm.complete(
            system or negotiate_system(self.role),
            [
                *self.transcript[-6:],
                {"role": "user", "content": f"Context: {ctx}\nNow emit your JSON move."},
            ],
        )
        obj = self._parse_obj(raw)
        self._last_emitted = obj
        self.transcript.append({"role": "user", "content": ctx})
        self.transcript.append({"role": "assistant", "content": json.dumps(obj, default=str)})
        return obj

    def _nudge(self) -> dict[str, Any]:
        return {"note": "previous move was invalid", "retry_with": self._last_emitted}

    @staticmethod
    def _parse_obj(raw: str) -> dict[str, Any]:
        text = raw.strip()
        if text.startswith("```"):
            text = text.strip("`").removeprefix("json").strip()
        try:
            obj: dict[str, Any] = json.loads(text)
            return obj
        except json.JSONDecodeError as exc:
            raise ValueError(f"negotiation LLM returned invalid JSON: {text[:200]}") from exc

    def _coerce(self, model: type[BaseModel], obj: dict[str, Any]):
        payload = dict(obj)
        payload["sender"] = self.agent_id
        price = payload.get("price")
        if price is not None:
            payload["price"] = Decimal(str(price)).quantize(Decimal("0.01"))
        # The good is fixed before the first turn. Any "good" the LLM emits is
        # ignored, so it cannot change the deliverable, its hash or the
        # acceptance criteria. Price is the only term the model may move.
        payload["good"] = self.good
        for field in ("deadline", "valid_until"):
            payload.pop(field, None)
        now = _now()
        payload.setdefault("deadline", now)
        payload.setdefault("valid_until", now)
        if model is Counter or model is Accept:
            payload.setdefault("responds_to", obj.get("responds_to", "prev"))
        for attempt in (payload, _nudge_deals(payload)):
            try:
                return model.model_validate(attempt)
            except ValueError:
                continue
        raise ValueError(f"could not coerce LLM output to {model.__name__}: {obj}")

    def _fallback_accept(self, price: Decimal) -> Accept:
        log.info("%s: negotiation cap hit — auto-accepting at %s", self.agent_id, price)
        return Accept(
            sender=self.agent_id,
            responds_to="cap",
            good=self.good,
            price=price.quantize(Decimal("0.01")),
            deadline=_now(),
        )


def _nudge_deals(payload: dict[str, Any]) -> dict[str, Any]:
    """Second validation attempt: strip unknown keys that sometimes leak from LLM JSON."""
    kept = {
        k: v
        for k, v in payload.items()
        if k
        in {
            "kind",
            "good",
            "price",
            "currency",
            "responds_to",
            "sender",
            "deal_id",
            "ts",
            "deadline",
            "valid_until",
        }
    }
    return kept


def _now() -> datetime:
    return datetime.now() + timedelta(hours=24)
