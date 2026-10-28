"""Mandates — AP2-lite spend authorization, enforced before any money moves.

A mandate is the machine-readable permission a principal grants to an agent:
how much, with whom, in which categories, for how long. The Trustee checks
*both parties'* mandates before opening an escrow; a deal that fails its
mandate check can never touch PayPal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from .protocol import Contract


@dataclass(frozen=True)
class Mandate:
    agent_id: str
    principal: str  # the human/company who granted this mandate
    max_amount: Decimal
    allowed_categories: frozenset[str] | None = None  # None = any
    allowed_counterparties: frozenset[str] | None = None  # None = any
    valid_until: datetime | None = None
    grants: str = "spend"  # "spend" | "receive"
    notes: str = ""


@dataclass
class MandateReport:
    ok: bool
    violations: list[str] = field(default_factory=list)

    def summarize(self) -> str:
        return "mandates OK" if self.ok else "; ".join(self.violations)


class MandateEngine:
    """Policy gate ahead of the money path. Deterministic, no LLM."""

    def __init__(self, mandates: dict[str, Mandate] | None = None):
        self._mandates: dict[str, Mandate] = dict(mandates or {})

    def grant(self, mandate: Mandate) -> None:
        self._mandates[mandate.agent_id] = mandate

    def get(self, agent_id: str) -> Mandate | None:
        return self._mandates.get(agent_id)

    def check_deal(self, contract: Contract, now: datetime | None = None) -> MandateReport:
        """Both parties must hold a valid, matching mandate for this contract."""
        now = now or datetime.now()
        violations: list[str] = []
        for side, grant in (("buyer", "spend"), ("seller", "receive")):
            agent_id = getattr(contract, side)
            m = self._mandates.get(agent_id)
            if m is None:
                violations.append(f"{agent_id}: no {grant} mandate on file")
                continue
            if now > (m.valid_until or datetime.now()) and m.valid_until is not None:
                violations.append(f"{agent_id}: mandate expired {m.valid_until.isoformat()}")
            if m.grants != grant:
                violations.append(f"{agent_id}: mandate grants '{m.grants}', deal needs '{grant}'")
            if grant == "spend" and contract.price > m.max_amount:
                violations.append(f"{agent_id}: price {contract.price} exceeds cap {m.max_amount}")
            if m.allowed_categories and contract.good.category not in m.allowed_categories:
                violations.append(f"{agent_id}: category '{contract.good.category}' not allowed by mandate")
            counterparties = getattr(contract, ("seller" if side == "buyer" else "buyer"))
            if m.allowed_counterparties and counterparties not in m.allowed_counterparties:
                violations.append(f"{agent_id}: counterparty '{counterparties}' not allowlisted")
        return MandateReport(ok=not violations, violations=violations)


def demo_mandates(buyer: str = "buyer-agent-1", seller: str = "seller-agent-1") -> dict[str, Mandate]:
    """Sensible demo grants: agents can trade digital goods up to $500 with each other."""
    return {
        buyer: Mandate(
            agent_id=buyer,
            principal="demo-principal",
            max_amount=Decimal("500.00"),
            allowed_categories=frozenset({"code", "data", "design", "api-access"}),
            allowed_counterparties=frozenset({seller}),
            grants="spend",
        ),
        seller: Mandate(
            agent_id=seller,
            principal="demo-principal",
            max_amount=Decimal("500.00"),
            allowed_categories=frozenset({"code", "data", "design", "api-access"}),
            allowed_counterparties=frozenset({buyer}),
            grants="receive",
        ),
    }
