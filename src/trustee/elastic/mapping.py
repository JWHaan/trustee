"""Index mapping and the document shape of one ledger event.

Only what the ledger already shows on the arena tape is indexed. Keys that look like credentials are dropped
from the free-form `payload`, and long strings are cut.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from ..ledger import Event

_SECRETISH = re.compile(r"signature|secret|token|password|email|authorization|api[_-]?key|private|cookie|public_key")
_MAX_STR = 500
_DETAIL_KEYS = ("note", "explanation", "reason", "reasoning", "report", "error")

MAPPING: dict[str, Any] = {
    "dynamic": False,
    "properties": {
        "@timestamp": {"type": "date"},
        "source": {"type": "keyword"},
        "seq": {"type": "long"},
        "deal_id": {"type": "keyword"},
        "actor": {"type": "keyword"},
        "kind": {"type": "keyword"},
        "event": {"type": "keyword"},
        "op": {"type": "keyword"},
        "buyer": {"type": "keyword"},
        "seller": {"type": "keyword"},
        "category": {"type": "keyword"},
        "amount": {"type": "double"},
        "currency": {"type": "keyword"},
        "state_from": {"type": "keyword"},
        "state_to": {"type": "keyword"},
        "decision": {"type": "keyword"},
        "clause": {"type": "keyword"},
        "override": {"type": "boolean"},
        "ok": {"type": "boolean"},
        "detail": {"type": "text"},
        "payload": {"type": "flattened"},
    },
}


def index_template(index: str) -> dict[str, Any]:
    """Composable index template body covering `index` (and `index-*`)."""
    return {
        "index_patterns": [index, f"{index}-*"],
        "priority": 200,
        "template": {"mappings": MAPPING},
        "_meta": {"managed_by": "trustee", "description": "Trustee escrow ledger events"},
    }


def scrub(value: Any, depth: int = 0) -> Any:
    """A copy of `value` safe to index: credential-looking keys removed, strings cut, depth bounded."""
    if depth > 4:
        return None
    if isinstance(value, dict):
        return {str(k): scrub(v, depth + 1) for k, v in value.items() if not _SECRETISH.search(str(k).lower())}
    if isinstance(value, list | tuple):
        return [scrub(v, depth + 1) for v in list(value)[:20]]
    if isinstance(value, str):
        return value if len(value) <= _MAX_STR else value[:_MAX_STR] + "..."
    if value is None or isinstance(value, bool | int | float):
        return value
    return str(value)[:_MAX_STR]


def to_utc_iso(ts: str) -> str:
    """Ledger timestamps are naive local time; Elasticsearch needs an explicit offset."""
    try:
        moment = datetime.fromisoformat(ts)
    except ValueError:
        return datetime.now(UTC).isoformat(timespec="milliseconds")
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_price(raw: Any) -> tuple[float | None, str | None]:
    """'12.50 USD' -> (12.5, 'USD')."""
    parts = str(raw or "").split()
    if not parts:
        return None, None
    try:
        amount = float(Decimal(parts[0]))
    except (InvalidOperation, ValueError):
        return None, None
    return amount, (parts[1] if len(parts) > 1 else None)


def build_doc(ev: Event, source: str, seq: int, deal: dict[str, Any] | None) -> dict[str, Any]:
    """One ledger event as an index document. `deal` carries buyer/seller/amount learned from contract_formed."""
    p = ev.payload
    doc: dict[str, Any] = {
        "@timestamp": to_utc_iso(ev.ts),
        "source": source,
        "seq": seq,
        "deal_id": ev.deal_id,
        "actor": ev.actor,
        "kind": ev.kind,
    }
    if deal:
        doc.update({k: v for k, v in deal.items() if v is not None})
    for key in ("event", "op", "decision", "clause", "state_from", "state_to"):
        if isinstance(p.get(key), str):
            doc[key] = p[key]
    for key in ("override", "ok"):
        if isinstance(p.get(key), bool):
            doc[key] = p[key]
    for key in _DETAIL_KEYS:
        if p.get(key):
            doc["detail"] = str(p[key])[:_MAX_STR]
            break
    doc["payload"] = scrub(p)
    return doc


def deal_facts(ev: Event) -> dict[str, Any] | None:
    """Parties and price from the `contract_formed` row; None for every other event."""
    p = ev.payload
    if ev.kind != "msg" or p.get("event") != "contract_formed" or not ev.deal_id:
        return None
    amount, currency = parse_price(p.get("price"))
    return {
        "buyer": p.get("buyer"),
        "seller": p.get("seller"),
        "category": p.get("category"),
        "amount": amount,
        "currency": currency,
    }
