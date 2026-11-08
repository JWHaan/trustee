"""One signed bundle per deal with everything a person (or PayPal's dispute process) needs to decide a dispute.

A dispute is usually two stories. This pack replaces them with records that check themselves:

  * the contract, with both parties' signatures and its sha256 digest (which the PayPal order also carries);
  * the seller's signed delivery (the hash it claimed) and Trustee's own check of the delivered file;
  * the buyer's attestation (its verdict, reason and evidence) and the ruling, if there was one;
  * the plain-language summary, the signed settlement receipt, and the PayPal order and authorization ids;
  * every ledger event of the deal, with the result of re-checking its hash chain.

The pack is signed by Trustee's key over its canonical JSON, so it cannot be edited after the fact without the
signature failing. `verify_evidence_pack` checks the signature and the ledger chain again, offline.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from .identity import AgentKey, canonical_json, verify
from .ledger import verify_chain
from .protocol import Contract
from .summary import summarize

PACK_TYPE = "trustee.evidence-pack"
PACK_VERSION = 1


def _event_dict(ev: Any) -> dict[str, Any]:
    if isinstance(ev, dict):
        return dict(ev)
    return {k: getattr(ev, k, None) for k in ("seq", "ts", "deal_id", "actor", "kind", "payload", "prev_hash", "hash")}


def build_evidence_pack(
    *,
    contract: Contract,
    snapshot: dict[str, Any],
    events: list[Any],
    receipt: dict[str, Any] | None,
    key: AgentKey,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The signed pack for one deal, from the Trustee snapshot, its ledger events and its receipt (if finished)."""
    rows = [_event_dict(e) for e in events]
    chain_ok, chain_reason = verify_chain(rows)
    attest = next((r["payload"] for r in reversed(rows) if r.get("kind") == "attest"), None)
    escrow = snapshot.get("escrow") or {}
    authority = snapshot.get("autonomy") or {}
    pack: dict[str, Any] = {
        "type": PACK_TYPE,
        "version": PACK_VERSION,
        "deal_id": contract.deal_id,
        "generated_at": (now or datetime.now(UTC)).isoformat(timespec="seconds"),
        "state": snapshot.get("state"),
        "summary": summarize(
            state=str(snapshot.get("state")),
            price=contract.price,
            currency=contract.currency,
            escrow_status=escrow.get("status"),
            ruling=snapshot.get("ruling"),
            settlement=snapshot.get("settlement"),
            needs_attention=snapshot.get("needs_attention"),
            dispute_deadline=snapshot.get("dispute_deadline"),
            human_present=authority.get("human_present") if authority else None,
            authority_outcome=authority.get("outcome") if authority else None,
            verification=snapshot.get("verification"),
            buyer_reason=(attest or {}).get("reason"),
        ),
        "contract": {"digest": contract.digest(), "signed": contract.model_dump(mode="json")},
        "paypal": {
            "order_id": escrow.get("order_id"),
            "authorization_id": escrow.get("authorization_id"),
            "status": escrow.get("status"),
            "custom_id": (escrow.get("details") or {}).get("custom_id") or f"trustee:{contract.digest()}",
        },
        "delivery": snapshot.get("last_delivery"),  # signed by the seller: the hash it claimed
        "trustee_check": snapshot.get("verification"),  # Trustee's own check of the delivered bytes
        "attestation": attest,  # the buyer's verdict, reason and evidence
        "ruling": snapshot.get("ruling"),
        "needs_attention": snapshot.get("needs_attention"),
        "settlement": snapshot.get("settlement"),
        "authority": {
            "human_present": bool(authority.get("human_present", True)) if authority else True,
            "intent_mandate_digest": authority.get("intent_digest") if authority else None,
            "outcome": authority.get("outcome") if authority else None,
        },
        "receipt": receipt,
        "ledger": {
            "events": rows,
            "count": len(rows),
            "head": rows[-1].get("hash") if rows else None,
            "chain_ok": chain_ok,
            "chain_check": chain_reason,
        },
        "trustee_key": key.public_key_b64,
    }
    pack["signature"] = key.sign(canonical_json(pack))
    return pack


def verify_evidence_pack(pack: dict[str, Any], public_key: str | None = None) -> tuple[bool, str]:
    """Check the pack's signature (against `public_key`, or the key it names) and its ledger chain, offline."""
    body = {k: v for k, v in pack.items() if k != "signature"}
    key = public_key or str(pack.get("trustee_key") or "")
    if not verify(key, str(pack.get("signature") or ""), canonical_json(body)):
        return False, "the pack's signature does not match its content"
    ok, reason = verify_chain((pack.get("ledger") or {}).get("events") or [])
    if not ok:
        return False, f"ledger chain: {reason}"
    return True, "ok"
