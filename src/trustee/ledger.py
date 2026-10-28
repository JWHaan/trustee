"""Append-only event ledger. Every mandate check, state transition, PayPal
call, and agent message lands here as a JSON line. The arena's AG Grid tape
just tails this file — agents and UI share one truth."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass
class Event:
    ts: str
    deal_id: str | None
    actor: str
    kind: str  # "msg" | "mandate" | "state" | "paypal" | "delivery" | "ruling"
    payload: dict = field(default_factory=dict)


class Ledger:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        self._events: list[Event] = []
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)

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
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(ev.__dict__, default=str) + "\n")
        return ev

    def tail(self, deal_id: str | None = None, kinds: tuple[str, ...] | None = None) -> list[Event]:
        with self._lock:
            return [
                e
                for e in self._events
                if (deal_id is None or e.deal_id == deal_id)
                and (kinds is None or e.kind in kinds)
            ]

    def __iter__(self) -> Iterator[Event]:
        return iter(self.tail())

    def __len__(self) -> int:
        return len(self._events)
