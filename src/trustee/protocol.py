"""Machine-readable offer protocol.

Buyer/seller agents negotiate over these small JSON messages. A Contract is
the only object the Trustee ever acts on — negotiation can be natural
language, but settlement is protocol-only.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class _Msg(BaseModel):
    msg_id: str = Field(default_factory=lambda: new_id("msg"))
    sender: str
    ts: datetime = Field(default_factory=datetime.now)
    deal_id: str | None = None

    @model_validator(mode="after")
    def _fill_deal_id(self) -> _Msg:
        if self.deal_id is None:
            self.deal_id = new_id("deal")
        return self


class GoodSpec(BaseModel):
    """What is being bought. Digital goods only (deliverable end-to-end by agents)."""

    good_id: str
    description: str
    category: str = Field(default="digital", description="e.g. code, data, design, api-access")
    acceptance_criteria: list[str] = Field(
        min_length=1, description="Machine-checkable clauses, e.g. 'sha256 in manifest', 'tests pass'"
    )
    deliverable_ref: str = Field(
        description="Path under the goods store the seller must deliver, e.g. 'lambda-module.zip'"
    )


class Offer(_Msg):
    kind: Literal["offer"] = "offer"
    good: GoodSpec
    price: Decimal = Field(gt=0, max_digits=10, decimal_places=2)
    currency: str = "USD"
    deadline: datetime
    valid_until: datetime

    @classmethod
    def new(
        cls,
        sender: str,
        good: GoodSpec,
        price: Decimal,
        ttl: timedelta = timedelta(hours=1),
        deal_id: str | None = None,
    ) -> Offer:
        now = datetime.now()
        return cls(
            sender=sender,
            ts=now,
            deal_id=deal_id,
            good=good,
            price=price,
            deadline=now + ttl,
            valid_until=now + ttl,
        )


class Counter(_Msg):
    kind: Literal["counter"] = "counter"
    good: GoodSpec
    price: Decimal = Field(gt=0, max_digits=10, decimal_places=2)
    currency: str = "USD"
    responds_to: str
    deadline: datetime
    valid_until: datetime


class Accept(_Msg):
    kind: Literal["accept"] = "accept"
    responds_to: str
    good: GoodSpec
    price: Decimal
    currency: str = "USD"
    deadline: datetime


class Delivery(_Msg):
    """Seller-side handoff record. Hash recorded so the Trustee can verify."""

    kind: Literal["delivery"] = "delivery"
    deliverable_ref: str
    content_hash: str
    delivered_at: datetime = Field(default_factory=datetime.now)


class Attest(_Msg):
    """Post-delivery verification verdict from the buyer agent."""

    kind: Literal["attest"] = "attest"
    verdict: Literal["accept", "reject"]
    reason: str
    evidence: dict[str, str] = Field(default_factory=dict)


Msg = Annotated[
    Offer | Counter | Accept | Attest | Delivery,
    Field(discriminator="kind"),
]


class Contract(BaseModel):
    """The object both agents accepted. Immutable once formed."""

    deal_id: str
    buyer: str
    seller: str
    good: GoodSpec
    price: Decimal
    currency: str = "USD"
    deadline: datetime
    formed_at: datetime
    transcript_ref: str = Field(default="", description="Ref to full negotiation transcript")

    def key_clauses(self) -> dict[str, str]:
        """Compact contract summary used by arbiter / ledger / judges.
        (No deal_id here — the ledger event already carries it.)"""
        return {
            "buyer": self.buyer,
            "seller": self.seller,
            "good": self.good.description,
            "category": self.good.category,
            "price": f"{self.price} {self.currency}",
            "acceptance_criteria": "; ".join(self.good.acceptance_criteria),
            "deliverable_ref": self.good.deliverable_ref,
            "deadline": self.deadline.isoformat(),
        }

    @classmethod
    def from_accept(cls, accept: Accept, seller: str, formed_at: datetime | None = None) -> Contract:
        return cls(
            deal_id=accept.deal_id or new_id("deal"),
            buyer=accept.sender,
            seller=seller,
            good=accept.good,
            price=accept.price,
            currency=accept.currency,
            deadline=accept.deadline,
            formed_at=formed_at or accept.ts,
        )
