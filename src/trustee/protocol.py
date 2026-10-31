"""Machine-readable offer protocol.

Buyer/seller agents negotiate over these small JSON messages. A Contract is
the only object the Trustee ever acts on — negotiation can be natural
language, but settlement is protocol-only.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import BaseModel, Field, field_validator, model_validator

from .identity import AgentKey, AgentRegistry, canonical_json

_SHA256_HEX = re.compile(r"[0-9a-f]{64}")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class _Msg(BaseModel):
    msg_id: str = Field(default_factory=lambda: new_id("msg"))
    sender: str
    ts: datetime = Field(default_factory=datetime.now)
    deal_id: str | None = None
    signature: str | None = Field(default=None, description="Sender's Ed25519 signature over signing_bytes()")

    @model_validator(mode="after")
    def _fill_deal_id(self) -> _Msg:
        if self.deal_id is None:
            self.deal_id = new_id("deal")
        return self

    def signing_bytes(self) -> bytes:
        """Canonical bytes of the whole message except the signature itself."""
        return canonical_json(self.model_dump(mode="json", exclude={"signature"}))

    def sign(self, key: AgentKey) -> Self:
        """A signed copy; the original is untouched."""
        return self.model_copy(update={"signature": key.sign(self.signing_bytes())})


def verify_signature(msg: _Msg, registry: AgentRegistry) -> bool:
    """True when `msg` carries a valid signature by the registered key of `msg.sender`."""
    return registry.verify(msg.sender, msg.signature, msg.signing_bytes())


class GoodSpec(BaseModel):
    """What is being bought. Digital goods only (deliverable end-to-end by agents)."""

    good_id: str
    description: str
    category: str = Field(default="digital", description="e.g. code, data, design, api-access")
    acceptance_criteria: list[str] = Field(
        min_length=1, description="Machine-checkable clauses, e.g. 'sha256 in manifest', 'tests pass'"
    )
    deliverable_ref: str = Field(
        description="Path under the goods store the seller must deliver, e.g. 'returns-2026q3.csv'"
    )
    sha256: str | None = Field(
        default=None,
        description=(
            "Expected hex sha256 of the deliverable, fixed at contract time. The buyer hashes the bytes it "
            "received and compares against this value, so neither side can change it after the deal forms."
        ),
    )

    @field_validator("sha256")
    @classmethod
    def _valid_sha256(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not _SHA256_HEX.fullmatch(value):
            raise ValueError("sha256 must be 64 lowercase hex characters")
        return value


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
    signatures: dict[str, str] = Field(
        default_factory=dict, description="agent_id -> Ed25519 signature over digest() bytes"
    )

    def digest(self) -> str:
        """sha256 hex of the agreed terms. Excludes `signatures` (they sign this) and
        `transcript_ref` (a pointer, not a term), so signing never changes the digest."""
        body = self.model_dump(mode="json", exclude={"signatures", "transcript_ref"})
        return hashlib.sha256(canonical_json(body)).hexdigest()

    def sign(self, agent_id: str, key: AgentKey) -> Contract:
        """A copy carrying `agent_id`'s signature over the digest."""
        sig = key.sign(self.digest().encode("ascii"))
        return self.model_copy(update={"signatures": {**self.signatures, agent_id: sig}})

    def verify_party(self, agent_id: str, registry: AgentRegistry) -> bool:
        return registry.verify(agent_id, self.signatures.get(agent_id), self.digest().encode("ascii"))

    def key_clauses(self) -> dict[str, str]:
        """Compact contract summary used by arbiter / ledger / judges.
        (No deal_id here — the ledger event already carries it.)"""
        clauses = {
            "buyer": self.buyer,
            "seller": self.seller,
            "good": self.good.description,
            "category": self.good.category,
            "price": f"{self.price} {self.currency}",
            "acceptance_criteria": "; ".join(self.good.acceptance_criteria),
            "deliverable_ref": self.good.deliverable_ref,
            "deadline": self.deadline.isoformat(),
            "digest": self.digest(),
        }
        if self.good.sha256 is not None:
            clauses["sha256"] = self.good.sha256
        return clauses

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
