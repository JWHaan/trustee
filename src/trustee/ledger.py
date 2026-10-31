"""Append-only event ledger. Every mandate check, state transition, PayPal
call, and agent message lands here as a JSON line. The arena's AG Grid tape
just tails this file — agents and UI share one truth."""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class Event:
    ts: str
    deal_id: str | None
    actor: str
    kind: str  # "run" | "msg" | "mandate" | "state" | "paypal" | "delivery" | "ruling" | "note"
    payload: dict = field(default_factory=dict)


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

    def __iter__(self) -> Iterator[Event]:
        return iter(self.tail())

    def __len__(self) -> int:
        return len(self._events)
