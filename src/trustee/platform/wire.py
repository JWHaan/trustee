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

    agent_id: str
    principal: str
    max_amount: Decimal
    allowed_categories: list[str] | None = None
    allowed_counterparties: list[str] | None = None
    valid_until: datetime | None = None
    grants: Literal["spend", "receive"] = "spend"
    notes: str = ""
    principal_signature: str | None = None
    max_total: Decimal | None = None
    revoked: bool = False
    version: int = Field(default=0, ge=0)


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
    principal: str = Field(pattern=ID_PATTERN)
    public_key: str
    proof: str


class AgentIn(BaseModel):
    agent_id: str = Field(pattern=ID_PATTERN)
    public_key: str
    principal: str = Field(pattern=ID_PATTERN)
    roles: list[str] = Field(min_length=1)
    paypal_email: str | None = None
    proof: str
    principal_signature: str


class RevokeIn(BaseModel):
    signature: str


class ResolveIn(BaseModel):
    decision: Literal["release", "settle"]
    operator: str = Field(min_length=1)
    reason: str = ""
