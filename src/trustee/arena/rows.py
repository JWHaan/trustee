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
)


def _price(p: dict[str, Any]) -> str:
    """`key_clauses()` reports "156.00 USD"; the amount column wants the number."""
    raw = str(p.get("price") or p.get("amount") or "")
    return raw.split(" ")[0] if raw else ""


def to_row(ev: Event, seq: int) -> dict[str, Any]:
    p = ev.payload
    note = p.get("note") or p.get("reasoning") or p.get("explanation") or p.get("reason") or ""
    if not note and ev.kind == "mandate":
        note = p.get("report", "")
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
    }
