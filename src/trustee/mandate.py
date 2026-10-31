"""Mandates — AP2-lite spend authorization, enforced before any money moves.

A mandate is the machine-readable permission a principal grants to an agent:
how much, with whom, in which categories, for how long. The Trustee checks
*both parties'* mandates before opening an escrow; a deal that fails its
mandate check can never touch PayPal.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field, fields, replace
from datetime import datetime
from decimal import Decimal

from .identity import AgentKey, PrincipalRegistry, canonical_json
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
    principal_signature: str | None = None  # principal's Ed25519 signature over signing_bytes()
    max_total: Decimal | None = None  # cumulative budget across deals (spend side); None = unbounded
    revoked: bool = False
    version: int = 0  # strictly increases per agent; stops a replay of an older signed mandate

    def signing_bytes(self) -> bytes:
        """Canonical bytes of every field except the signature itself."""
        body = {f.name: getattr(self, f.name) for f in fields(self) if f.name != "principal_signature"}
        return canonical_json(body)

    def sign(self, key: AgentKey) -> Mandate:
        """A copy signed by the principal's key; the original is untouched."""
        return replace(self, principal_signature=key.sign(self.signing_bytes()))


class MandateReplay(ValueError):
    """A mandate whose version is not newer than the one on file (a stale or replayed grant)."""


@dataclass
class MandateReport:
    ok: bool
    violations: list[str] = field(default_factory=list)

    def summarize(self) -> str:
        return "mandates OK" if self.ok else "; ".join(self.violations)


class MandateEngine:
    """Policy gate ahead of the money path. Deterministic, no LLM.

    With a `PrincipalRegistry`, a mandate only counts if its principal signed
    it: a self-asserted mandate is a claim, not an authorization.

    Budgets (`max_total`) are tracked per spending agent as reserved (escrow
    held) plus spent (captured). reserve/release/commit are idempotent per
    deal_id, so a replayed webhook cannot double-count or double-free.
    """

    def __init__(
        self,
        mandates: dict[str, Mandate] | None = None,
        principals: PrincipalRegistry | None = None,
    ):
        self._mandates: dict[str, Mandate] = dict(mandates or {})
        self.principals = principals
        self._revoked: set[str] = set()
        self._reserved: dict[str, tuple[str, Decimal]] = {}  # deal_id -> (buyer, price)
        self._spent: dict[str, tuple[str, Decimal]] = {}
        self._lock = threading.RLock()

    def check_grant(self, mandate: Mandate) -> None:
        """Raise MandateReplay unless `mandate` is newer than the current one (or identical to it)."""
        with self._lock:
            current = self._mandates.get(mandate.agent_id)
            if current is None or current.signing_bytes() == mandate.signing_bytes():
                return
            if current.version >= mandate.version:
                raise MandateReplay(
                    f"{mandate.agent_id}: mandate version {mandate.version} is not newer than {current.version}"
                )

    def grant(self, mandate: Mandate) -> None:
        with self._lock:
            self.check_grant(mandate)
            self._mandates[mandate.agent_id] = mandate

    def get(self, agent_id: str) -> Mandate | None:
        return self._mandates.get(agent_id)

    def revoke(self, agent_id: str) -> None:
        """Withdraw an agent's authority. Applies to deals not yet captured."""
        with self._lock:
            self._revoked.add(agent_id)

    # ---------------------------------------------------------------- budgets

    def committed(self, agent_id: str, exclude_deal: str | None = None) -> Decimal:
        """spent + reserved for `agent_id`, optionally ignoring one deal."""
        with self._lock:
            total = Decimal(0)
            for book in (self._spent, self._reserved):
                total += sum((p for d, (a, p) in book.items() if a == agent_id and d != exclude_deal), Decimal(0))
            return total

    def reserve(self, contract: Contract) -> None:
        with self._lock:
            if contract.deal_id in self._reserved or contract.deal_id in self._spent:
                return
            self._reserved[contract.deal_id] = (contract.buyer, contract.price)

    def release(self, contract: Contract) -> None:
        """Escrow voided: give the reserved amount back. No-op once committed."""
        with self._lock:
            self._reserved.pop(contract.deal_id, None)

    def commit(self, contract: Contract, amount: Decimal | None = None) -> None:
        """Escrow captured: the reservation becomes spend.

        A partial capture passes the amount that actually moved: only that is spent, and the rest
        of the reservation goes back to the budget. `None` commits the full price, as before."""
        with self._lock:
            if contract.deal_id in self._spent:
                return
            self._reserved.pop(contract.deal_id, None)
            self._spent[contract.deal_id] = (contract.buyer, contract.price if amount is None else amount)

    # ---------------------------------------------------------------- checks

    def check_deal(self, contract: Contract, now: datetime | None = None) -> MandateReport:
        """Both parties must hold a valid, matching mandate for this contract."""
        now = now or datetime.now()
        violations: list[str] = []
        with self._lock:
            for side, grant in (("buyer", "spend"), ("seller", "receive")):
                agent_id = getattr(contract, side)
                m = self._mandates.get(agent_id)
                if m is None:
                    violations.append(f"{agent_id}: no {grant} mandate on file")
                    continue
                if m.revoked or agent_id in self._revoked:
                    violations.append(f"{agent_id}: mandate revoked")
                if self.principals is not None and not self.principals.verify(
                    m.principal, m.principal_signature, m.signing_bytes()
                ):
                    violations.append(f"{agent_id}: mandate lacks a valid signature from principal '{m.principal}'")
                if m.valid_until is not None and now > m.valid_until:
                    violations.append(f"{agent_id}: mandate expired {m.valid_until.isoformat()}")
                if m.grants != grant:
                    violations.append(f"{agent_id}: mandate grants '{m.grants}', deal needs '{grant}'")
                if grant == "spend" and contract.price > m.max_amount:
                    violations.append(f"{agent_id}: price {contract.price} exceeds cap {m.max_amount}")
                if grant == "spend" and m.max_total is not None:
                    used = self.committed(agent_id, exclude_deal=contract.deal_id)
                    if used + contract.price > m.max_total:
                        violations.append(
                            f"{agent_id}: budget exceeded ({used} used + {contract.price} > total cap {m.max_total})"
                        )
                if m.allowed_categories and contract.good.category not in m.allowed_categories:
                    violations.append(f"{agent_id}: category '{contract.good.category}' not allowed by mandate")
                counterparties = getattr(contract, ("seller" if side == "buyer" else "buyer"))
                if m.allowed_counterparties and counterparties not in m.allowed_counterparties:
                    violations.append(f"{agent_id}: counterparty '{counterparties}' not allowlisted")
        return MandateReport(ok=not violations, violations=violations)

    def recheck(self, contract: Contract, now: datetime | None = None) -> MandateReport:
        """The gate again, at the moment before capture: authority can lapse
        (expiry, revocation, budget spent elsewhere) between escrow and settlement.
        The deal's own reservation is not counted against itself."""
        return self.check_deal(contract, now)


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
