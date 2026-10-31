"""Wire format shared by the platform API and the client SDK.

Both sides must produce byte-identical signing input, so the byte recipes live in one
place. Everything is `canonical_json` (sorted keys, no whitespace, utf-8) over a small
dict, and signatures are urlsafe-b64 Ed25519 (see `trustee.identity`).
"""

from __future__ import annotations

import json
from dataclasses import fields
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..identity import canonical_json
from ..mandate import Mandate

# Ids end up in URL paths and ledger rows: keep them boring.
ID_PATTERN = r"^[A-Za-z0-9._:@-]{1,128}$"


def principal_signing_bytes(principal: str, public_key: str) -> bytes:
    """What a principal's key signs to prove it controls `public_key`."""
    return canonical_json({"principal": principal, "public_key": public_key})


def agent_signing_bytes(
    agent_id: str,
    public_key: str,
    principal: str,
    roles: list[str] | set[str] | frozenset[str],
    paypal_email: str | None,
) -> bytes:
    """The agent registration body without its two signatures. Roles are sorted so order never matters."""
    return canonical_json(
        {
            "agent_id": agent_id,
            "public_key": public_key,
            "principal": principal,
            "roles": sorted(set(roles)),
            "paypal_email": paypal_email,
        }
    )


def revoke_signing_bytes(agent_id: str) -> bytes:
    return canonical_json({"revoke": agent_id})


class MandateIn(BaseModel):
    """A Mandate as JSON. Decimals travel as strings so the signed bytes survive the round trip."""

    agent_id: str = Field(description="Agent the mandate covers.", examples=["buyer-agent"])
    principal: str = Field(description="Principal who signs the mandate.", examples=["acme-procurement"])
    max_amount: Decimal = Field(description="Largest single deal, in the deal currency.", examples=["250.00"])
    allowed_categories: list[str] | None = Field(
        default=None, description="Deal categories allowed. Null means any.", examples=[["digital"]]
    )
    allowed_counterparties: list[str] | None = Field(
        default=None, description="Agent ids the agent may deal with. Null means any."
    )
    valid_until: datetime | None = Field(default=None, description="Mandate expiry. Null means no expiry.")
    grants: Literal["spend", "receive"] = Field(
        default="spend", description="`spend` for a buyer, `receive` for a seller."
    )
    notes: str = Field(default="", description="Free text the principal signs along with the rest.")
    principal_signature: str | None = Field(
        default=None, description="Principal's Ed25519 signature over every other field, urlsafe-base64."
    )
    max_total: Decimal | None = Field(
        default=None, description="Cumulative budget across deals. Null means unbounded.", examples=["1000.00"]
    )
    revoked: bool = Field(default=False, description="Send false. Revoke with the revoke endpoint instead.")
    version: int = Field(
        default=0, ge=0, description="Strictly increasing per agent. Replaying an older version is a 409."
    )


def mandate_from_in(body: MandateIn) -> Mandate:
    return Mandate(
        agent_id=body.agent_id,
        principal=body.principal,
        max_amount=body.max_amount,
        allowed_categories=None if body.allowed_categories is None else frozenset(body.allowed_categories),
        allowed_counterparties=None if body.allowed_counterparties is None else frozenset(body.allowed_counterparties),
        valid_until=body.valid_until,
        grants=body.grants,
        notes=body.notes,
        principal_signature=body.principal_signature,
        max_total=body.max_total,
        revoked=body.revoked,
        version=body.version,
    )


def mandate_to_dict(mandate: Mandate) -> dict[str, Any]:
    """JSON-safe dict of every Mandate field (signature included)."""
    raw = {f.name: getattr(mandate, f.name) for f in fields(mandate)}
    result: dict[str, Any] = json.loads(canonical_json(raw))
    return result


def mandate_from_dict(data: dict[str, Any]) -> Mandate:
    return mandate_from_in(MandateIn.model_validate(data))


class PrincipalIn(BaseModel):
    principal: str = Field(
        pattern=ID_PATTERN,
        description="Principal name, 1-128 chars of [A-Za-z0-9._:@-].",
        examples=["acme-procurement"],
    )
    public_key: str = Field(description="Raw 32-byte Ed25519 public key, urlsafe-base64.")
    proof: str = Field(description="Principal's signature over canonical JSON {principal, public_key}.")


class AgentIn(BaseModel):
    agent_id: str = Field(pattern=ID_PATTERN, description="Agent id, unique platform-wide.", examples=["buyer-agent"])
    public_key: str = Field(description="Raw 32-byte Ed25519 public key, urlsafe-base64.")
    principal: str = Field(pattern=ID_PATTERN, description="Principal that owns this agent.")
    roles: list[str] = Field(
        min_length=1, description="Role names the agent holds, e.g. `buyer` or `seller`.", examples=[["buyer"]]
    )
    paypal_email: str | None = Field(
        default=None, description="Payout or payer address. Stored, never returned in full."
    )
    proof: str = Field(description="The agent's signature over its registration body (proves key possession).")
    principal_signature: str = Field(description="The principal's signature over the same registration body.")


class RevokeIn(BaseModel):
    signature: str = Field(description="Principal's signature over canonical JSON {revoke: agent_id}.")


class ResolveIn(BaseModel):
    decision: Literal["release", "settle"] = Field(
        description="`release` refunds and voids; `settle` captures to the seller."
    )
    operator: str = Field(min_length=1, description="Name of the human making the ruling. Recorded in the ledger.")
    reason: str = Field(default="", description="Why. Recorded in the ledger, truncated to 120 characters on refunds.")
