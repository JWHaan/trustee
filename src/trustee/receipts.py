"""Signed settlement receipts and the Trustee's signing key.

When a deal ends (captured to the seller, or voided back to the buyer) Trustee issues a receipt: one JSON document
that says what was decided, under whose authority, and which ledger it came from, signed with Trustee's Ed25519 key.
Anyone holding the receipt, the public key and (optionally) the contract, the intent mandate and the deal's events
can check all of it offline with `python -m trustee.verify`; nothing needs to trust the server that issued it.

The receipt never carries a secret: no vault id, no funding reference, no payer details. It names PayPal order,
authorization and capture ids, which identify money movements but grant no access.
"""

from __future__ import annotations

import base64
import binascii
import logging
import os
from datetime import UTC, datetime
from typing import Any

from .identity import AgentKey, canonical_json
from .protocol import Contract
from .store import Store

log = logging.getLogger(__name__)

RECEIPT_TYPE = "trustee.settlement_receipt"
RECEIPT_VERSION = 1
SIGNING_KEY_ENV = "TRUSTEE_SIGNING_KEY"
_META_KEY = "trustee_signing_key"


def _decode_key(text: str) -> bytes:
    raw = text.strip()
    for decode in (base64.urlsafe_b64decode, base64.b64decode):
        try:
            data = decode(raw + "=" * (-len(raw) % 4))
        except (binascii.Error, ValueError):
            continue
        if len(data) == 32:
            return data
    raise ValueError(f"{SIGNING_KEY_ENV} must be the base64 of a 32-byte Ed25519 private key")


def load_signing_key(store: Store | None = None) -> AgentKey:
    """Trustee's signing key: `TRUSTEE_SIGNING_KEY` (base64 private key) if set, else one kept in the store.

    With neither an env key nor a store the key is generated for this process only (tests and local runs).
    A key kept in the store lives in the same database as the deals: protect that file like a credential, or set the
    env var from your platform's secret store instead."""
    env = os.environ.get(SIGNING_KEY_ENV)
    if env:
        return AgentKey.from_private_bytes(_decode_key(env))
    if store is None:
        return AgentKey.generate()
    existing = store.get_meta(_META_KEY)
    if existing:
        return AgentKey.from_private_bytes(_decode_key(existing))
    fresh = AgentKey.generate()
    stored = store.put_meta_if_absent(_META_KEY, base64.urlsafe_b64encode(fresh.private_bytes()).decode("ascii"))
    return AgentKey.from_private_bytes(_decode_key(stored))


def receipt_signing_bytes(receipt: dict[str, Any]) -> bytes:
    """Canonical bytes of the receipt without its signature."""
    return canonical_json({k: v for k, v in receipt.items() if k != "signature"})


def _paypal_ids(escrow: dict[str, Any] | None) -> dict[str, Any]:
    if not escrow:
        return {"order_id": None, "authorization_id": None, "capture_id": None, "voided": False, "funding": None}
    details = escrow.get("details") or {}
    status = escrow.get("status")
    capture_id = (details.get("capture_id") or details.get("id")) if status == "captured" else None
    return {
        "order_id": escrow.get("order_id"),
        "authorization_id": escrow.get("authorization_id"),
        "capture_id": capture_id,
        "voided": status == "voided",
        "funding": details.get("funding") or ("payer_approval" if escrow.get("approve_url") else "direct"),
    }


def build_receipt(
    snapshot: dict[str, Any],
    contract: Contract,
    *,
    key: AgentKey,
    ledger_head: str,
    ledger_events: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The signed receipt for a terminal deal, from the Trustee snapshot (see `Trustee.snapshot`)."""
    state = snapshot["state"]
    if state not in ("SETTLED", "VOIDED"):
        raise ValueError(f"deal {snapshot['deal_id']} is {state}, not terminal: no receipt yet")
    autonomy = snapshot.get("autonomy") or {}
    delivery = snapshot.get("last_delivery") or {}
    body: dict[str, Any] = {
        "type": RECEIPT_TYPE,
        "version": RECEIPT_VERSION,
        "deal_id": snapshot["deal_id"],
        "outcome": "settled" if state == "SETTLED" else "voided",
        "amount": str(contract.price),
        "currency": contract.currency,
        "buyer": contract.buyer,
        "seller": contract.seller,
        "contract_digest": contract.digest(),
        "intent_mandate_digest": autonomy.get("intent_digest"),
        "human_present": bool(autonomy.get("human_present", True)),
        "delivery_sha256": delivery.get("content_hash") or None,
        "policy": list(autonomy.get("policy") or []),
        "paypal": _paypal_ids(snapshot.get("escrow")),
        "ledger_head": ledger_head,
        "ledger_events": ledger_events,
        "issued_at": (now or datetime.now(UTC)).isoformat(timespec="seconds"),
        "trustee_key": key.public_key_b64,
    }
    return {**body, "signature": key.sign(receipt_signing_bytes(body))}
