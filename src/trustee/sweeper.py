"""Deadline sweeper — the clock that keeps escrowed funds from being stranded.

A Trustee only acts when a message arrives. If the buyer never attests, the
seller never delivers, a dispute sits unanswered, or the 29-day PayPal
authorization is about to lapse, nothing would ever call it again and the money
would stay held (or silently expire). `DeadlineSweeper` is the periodic caller:
it keeps the (trustee, contract) pairs that still have funds at stake and runs
`Trustee.tick()` on each. It owns no thread; whoever hosts it (API process,
cron, test) calls `tick()` on a schedule.
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime

from .agents.trustee import Trustee, TrusteeDecision
from .protocol import Contract

log = logging.getLogger(__name__)


class DeadlineSweeper:
    """`reauthorize` (default: env TRUSTEE_REAUTHORIZE=1, else off) also renews held authorizations
    after PayPal's 3-day honor period, for deals whose contract deadline is still ahead."""

    def __init__(self, reauthorize: bool | None = None) -> None:
        self.reauthorize = os.environ.get("TRUSTEE_REAUTHORIZE") == "1" if reauthorize is None else bool(reauthorize)
        self._lock = threading.Lock()
        self._deals: dict[str, tuple[Trustee, Contract]] = {}

    def register(self, trustee: Trustee, contract: Contract) -> None:
        with self._lock:
            self._deals[contract.deal_id] = (trustee, contract)

    def unregister(self, deal_id: str) -> None:
        with self._lock:
            self._deals.pop(deal_id, None)

    def __len__(self) -> int:
        with self._lock:
            return len(self._deals)

    def tick(self, now: datetime | None = None) -> list[tuple[str, TrusteeDecision]]:
        """Tick every registered deal; return only the ones that did something.

        Terminal deals are dropped. A deal whose tick raises is logged and kept,
        so one bad deal cannot stop the sweep of the others.
        """
        now = now or datetime.now()
        with self._lock:
            items = list(self._deals.items())
        results: list[tuple[str, TrusteeDecision]] = []
        for deal_id, (trustee, contract) in items:
            try:
                decision = trustee.tick(contract, now)
            except Exception:  # noqa: BLE001 - isolate deals from each other
                log.exception("sweeper: tick failed for %s", deal_id)
                continue
            if decision.action == "no-op" and self.reauthorize:
                try:
                    decision = trustee.reauthorize(contract, now)
                except Exception:  # noqa: BLE001 - a renewal problem must never stop the sweep
                    log.exception("sweeper: reauthorize failed for %s", deal_id)
            if decision.action != "no-op":
                results.append((deal_id, decision))
            if trustee.deal.is_terminal:
                self.unregister(deal_id)
        return results
