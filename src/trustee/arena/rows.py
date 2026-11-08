"""Flatten a ledger Event into one row the UI can render directly.

One row shape feeds both panels: the grid takes every column, the transcript
takes (actor, kind, price, note). Flattening here — not in React — means the
SSE payload and the `/api/ledger` payload are the same object, so a row that
lands live is byte-identical to one replayed after a refresh.
"""

from __future__ import annotations

from typing import Any

from ..ledger import Event

COLUMNS = (
    "id",
    "seq",
    "ts",
    "deal",
    "actor",
    "kind",
    "event",
    "state_from",
    "state_to",
    "price",
    "verdict",
    "paypal_op",
    "order_id",
    "sha",
    "note",
    "signer",
    "check",
    "link",
    "wait_s",
    "rule",
    "result",
    "authority",
)


def _price(p: dict[str, Any]) -> str:
    """`key_clauses()` reports "156.00 USD"; the amount column wants the number."""
    raw = str(p.get("price") or p.get("amount") or "")
    return raw.split(" ")[0] if raw else ""


def _policy_result(p: dict[str, Any]) -> str:
    if p.get("rule"):
        return "pass" if p.get("ok") else "fail"
    return str(p.get("outcome") or "")


def _authority(p: dict[str, Any]) -> str:
    present = p.get("human_present")
    if present is None:
        return ""
    return "human" if present else "intent_mandate"


def to_row(ev: Event, seq: int) -> dict[str, Any]:
    p = ev.payload
    note = p.get("note") or p.get("reasoning") or p.get("explanation") or p.get("reason") or ""
    if not note and ev.kind == "mandate":
        note = p.get("report", "")
    identity = ev.kind == "identity"
    return {
        "id": f"{ev.deal_id or 'run'}:{seq}",
        "seq": seq,
        "ts": ev.ts,
        "deal": ev.deal_id or "",
        "actor": ev.actor,
        "kind": ev.kind,
        "event": str(p.get("event") or p.get("op") or p.get("decision") or ""),
        "state_from": str(p.get("state_from") or ""),
        "state_to": str(p.get("state_to") or ""),
        "price": _price(p),
        "verdict": str(p.get("verdict") or ""),
        "paypal_op": str(p.get("op") or "") if ev.kind == "paypal" else "",
        "order_id": str(p.get("order_id") or ""),
        "sha": str(p.get("sha256") or "")[:16],
        "note": str(note)[:220],
        # Additive columns. `check` is the Trustee's verdict on a signature; `link` is a
        # payer-approval URL; `wait_s` is how long the arena will wait for that approval.
        "signer": str(p.get("signer") or "") if identity else "",
        "check": ("verified" if p.get("ok") else "rejected") if identity else "",
        "link": str(p.get("approve_url") or p.get("link") or ""),
        "wait_s": str(p.get("wait_s") or ""),
        # Policy rows: `rule` is the intent-mandate rule id, `result` is pass / fail (or the decision for a summary
        # row). `authority` marks a receipt row: who authorized the payment.
        "rule": str(p.get("rule") or "") if ev.kind == "policy" else "",
        "result": _policy_result(p)
        if ev.kind == "policy"
        else ("pass" if p.get("ok") else "fail")
        if ev.kind == "verification"
        else "",
        "authority": _authority(p) if ev.kind == "receipt" else "",
    }
