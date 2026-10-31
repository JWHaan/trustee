"""Agent reputation: how an agent has behaved across the deals it was a party to.

Two interchangeable backends return the same `Reputation`:

* `LocalReputation` reads the platform `Store` (deals and ledger events). It is the default.
* `ElasticReputation` (trustee.elastic.reputation) runs one ES|QL query over the shipped ledger index.

Definitions, from the ledger's own event kinds (a deal counts when the agent is its buyer or seller):

* deals_total: deals that have a contract.
* settled / voided: deals whose last state is SETTLED / VOIDED (voided covers every refund and timeout).
* disputed: deals that entered DISPUTED (`state` row with state_to DISPUTED, after a buyer reject).
* rulings_lost: deals where the agent was the seller and a `ruling` row decided `release`, that is, the
  arbiter or an operator sent the money back to the buyer.
* dispute_rate: disputed / deals_total (0 when there are no deals).
* median_delivery_seconds: seller side, ESCROWED to DELIVERED, from the ledger timestamps.
* first_seen / last_seen: earliest and latest ledger event of the agent's deals, in UTC.
"""

from __future__ import annotations

import statistics
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from .store import Store


@dataclass(frozen=True)
class Reputation:
    agent_id: str
    deals_total: int = 0
    settled: int = 0
    voided: int = 0
    disputed: int = 0
    rulings_lost: int = 0
    dispute_rate: float = 0.0
    median_delivery_seconds: float | None = None
    first_seen: str | None = None
    last_seen: str | None = None
    source: str = "local"
    as_of: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ReputationProvider(Protocol):
    def reputation(self, agent_id: str, exclude_deal: str | None = None) -> Reputation:
        """Reputation of `agent_id`, optionally ignoring one deal (the one being checked)."""
        ...


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def _utc_iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


class LocalReputation:
    """Computed on demand from the platform store; the same facts the Elastic backend reads from the index."""

    def __init__(self, store: Store, max_deals: int = 100_000):
        self.store = store
        self.max_deals = max_deals

    def reputation(self, agent_id: str, exclude_deal: str | None = None) -> Reputation:
        total = settled = voided = disputed = lost = 0
        deliveries: list[float] = []
        seen: list[datetime] = []
        for row in self.store.list_deals(limit=self.max_deals):
            contract = row.get("contract") or {}
            buyer, seller = contract.get("buyer"), contract.get("seller")
            if agent_id not in (buyer, seller) or row["deal_id"] == exclude_deal:
                continue
            total += 1
            settled += row["state"] == "SETTLED"
            voided += row["state"] == "VOIDED"
            escrowed: datetime | None = None
            delivered: datetime | None = None
            was_disputed = was_lost = False
            for ev in self.store.events(deal_id=row["deal_id"], limit=10_000):
                moment = _parse(ev["ts"])
                if moment is not None:
                    seen.append(moment)
                payload = ev["payload"] or {}
                if ev["kind"] == "state" and moment is not None:
                    to = payload.get("state_to")
                    if to == "DISPUTED":
                        was_disputed = True
                    elif to == "ESCROWED" and escrowed is None:
                        escrowed = moment
                    elif to == "DELIVERED":
                        delivered = moment
                elif ev["kind"] == "ruling" and payload.get("decision") == "release" and agent_id == seller:
                    was_lost = True
            disputed += was_disputed
            lost += was_lost
            if agent_id == seller and escrowed is not None and delivered is not None:
                deliveries.append((delivered - escrowed).total_seconds())
        return Reputation(
            agent_id=agent_id,
            deals_total=total,
            settled=settled,
            voided=voided,
            disputed=disputed,
            rulings_lost=lost,
            dispute_rate=round(disputed / total, 4) if total else 0.0,
            median_delivery_seconds=round(statistics.median(deliveries), 3) if deliveries else None,
            first_seen=_utc_iso(min(seen)) if seen else None,
            last_seen=_utc_iso(max(seen)) if seen else None,
            source="local",
            as_of=now_iso(),
        )
