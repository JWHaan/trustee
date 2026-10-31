"""Append-only event ledger. Every mandate check, state transition, PayPal
call, and agent message lands here as a JSON line. The arena's AG Grid tape
just tails this file — agents and UI share one truth."""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .identity import canonical_json

log = logging.getLogger(__name__)

# The "previous hash" of the first event in a chain. One chain per deal (and one for events with no deal).
GENESIS_HASH = "0" * 64


def event_hash(prev_hash: str, ts: str, deal_id: str | None, actor: str, kind: str, payload: dict[str, Any]) -> str:
    """sha256 over the canonical bytes of an event's content plus the hash of the event before it.

    The payload is normalised through JSON first (`default=str`, as the ledger file and the store write it), so the
    hash computed when the event is appended equals the hash recomputed from the persisted row later.
    """
    body = {
        "prev_hash": prev_hash,
        "ts": ts,
        "deal_id": deal_id,
        "actor": actor,
        "kind": kind,
        "payload": json.loads(json.dumps(payload, default=str)),
    }
    return hashlib.sha256(canonical_json(body)).hexdigest()


def verify_chain(events: Iterable[Any]) -> tuple[bool, str]:
    """Check that `events` (dicts or Events, oldest first, one deal) form an unbroken hash chain.

    Returns (ok, reason). An event with no hash, a hash that does not match its content, or a `prev_hash` that is not
    the previous event's hash fails the check, so editing, dropping, inserting or reordering any event is detected.
    """
    prev = GENESIS_HASH
    for index, raw in enumerate(events):
        get = raw.get if isinstance(raw, dict) else lambda k, d=None, _r=raw: getattr(_r, k, d)
        if not get("hash"):
            return False, f"event {index} has no hash"
        if get("prev_hash") != prev:
            return False, f"event {index}: prev_hash does not match the previous event"
        expected = event_hash(
            prev, str(get("ts")), get("deal_id"), str(get("actor")), str(get("kind")), dict(get("payload") or {})
        )
        if get("hash") != expected:
            return False, f"event {index}: content does not match its hash"
        prev = str(get("hash"))
    return True, "ok"


@dataclass
class Event:
    ts: str
    deal_id: str | None
    actor: str
    kind: str  # "run" | "msg" | "mandate" | "state" | "paypal" | "delivery" | "ruling" | "note"
    payload: dict = field(default_factory=dict)
    # Hash chain (additive). `hash` covers this event's content and `prev_hash`, the hash of the deal's previous event.
    prev_hash: str = ""
    hash: str = ""


class Ledger:
    """Append-only event log.

    Subscribers get every event as it lands, so the API layer can bridge to
    SSE without polling this object. Callbacks run on the appending thread —
    a bridge that needs a different thread must marshal (see arena/api.py).
    """

    def __init__(self, path: Path | str | None = None, on_event: Callable[[Event], None] | None = None):
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        self._events: list[Event] = []
        self._heads: dict[str | None, str] = {}  # chain key (deal id) -> hash of its latest event
        self._subscribers: list[Callable[[Event], None]] = []
        if on_event is not None:
            self._subscribers.append(on_event)
        if self.path:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:  # read-only FS (hosted demo) must not kill a run
                log.warning("ledger path %s unusable (%s); running in-memory only", self.path, exc)
                self.path = None

    def subscribe(self, cb: Callable[[Event], None]) -> Callable[[], None]:
        """Register `cb`; returns an unsubscribe callable."""
        with self._lock:
            self._subscribers.append(cb)

        def unsubscribe() -> None:
            with self._lock:
                if cb in self._subscribers:
                    self._subscribers.remove(cb)

        return unsubscribe

    def append(self, deal_id: str | None, actor: str, kind: str, **payload: object) -> Event:
        ev = Event(
            ts=datetime.now().isoformat(timespec="milliseconds"),
            deal_id=deal_id,
            actor=actor,
            kind=kind,
            payload=payload,
        )
        with self._lock:
            ev.prev_hash = self._heads.get(deal_id, GENESIS_HASH)
            ev.hash = event_hash(ev.prev_hash, ev.ts, ev.deal_id, ev.actor, ev.kind, ev.payload)
            self._heads[deal_id] = ev.hash
            self._events.append(ev)
            if self.path:
                try:
                    with self.path.open("a", encoding="utf-8") as fh:
                        fh.write(json.dumps(ev.__dict__, default=str) + "\n")
                except OSError as exc:  # durability is best-effort; the tape must not stall
                    log.warning("ledger write failed (%s); continuing in-memory", exc)
                    self.path = None
            subscribers = list(self._subscribers)
        # Outside the lock: a slow subscriber must not block other writers.
        for cb in subscribers:
            try:
                cb(ev)
            except Exception:  # noqa: BLE001 - a bad subscriber cannot break the money path
                log.exception("ledger subscriber failed")
        return ev

    def tail(self, deal_id: str | None = None, kinds: tuple[str, ...] | None = None) -> list[Event]:
        with self._lock:
            return [
                e
                for e in self._events
                if (deal_id is None or e.deal_id == deal_id) and (kinds is None or e.kind in kinds)
            ]

    def head(self, deal_id: str | None) -> str:
        """Hash of the latest event of a deal's chain (GENESIS_HASH when it has none)."""
        with self._lock:
            return self._heads.get(deal_id, GENESIS_HASH)

    def verify_chain(self, deal_id: str | None = None) -> tuple[bool, str]:
        """Re-derive every hash of one deal's chain. A deal id of None checks the events that carry no deal."""
        return verify_chain(
            self.tail(deal_id) if deal_id is not None else [e for e in self.tail() if e.deal_id is None]
        )

    def __iter__(self) -> Iterator[Event]:
        return iter(self.tail())

    def __len__(self) -> int:
        return len(self._events)
