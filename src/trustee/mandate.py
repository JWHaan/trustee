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
from typing import TYPE_CHECKING

from .identity import AgentKey, PrincipalRegistry, canonical_json
from .protocol import Contract

if TYPE_CHECKING:
    from .reputation import Reputation, ReputationProvider


_SIGNED_WHEN_SET = frozenset({"min_counterparty_deals", "max_counterparty_dispute_rate"})


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
    # Reputation rules about the *other* party of a deal; None = no rule. They need a reputation provider.
    min_counterparty_deals: int | None = None
    max_counterparty_dispute_rate: Decimal | None = None

    def signing_bytes(self) -> bytes:
        """Canonical bytes of every field except the signature itself. Optional fields added after the first
        release are signed only when set, so a mandate signed before they existed still verifies."""
        body = {
            f.name: getattr(self, f.name)
            for f in fields(self)
            if f.name != "principal_signature" and not (f.name in _SIGNED_WHEN_SET and getattr(self, f.name) is None)
        }
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
    checks: list[str] = field(default_factory=list)  # optional rules that passed, so the tape shows them

    def summarize(self) -> str:
        if not self.ok:
            return "; ".join(self.violations)
        return "; ".join(["mandates OK", *self.checks])


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
        reputation: ReputationProvider | None = None,
    ):
        self._mandates: dict[str, Mandate] = dict(mandates or {})
        self.principals = principals
        self.reputation = reputation  # needed only by mandates with counterparty reputation rules
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

    def commit(self, contract: Contract) -> None:
        """Escrow captured: the reservation becomes spend."""
        with self._lock:
            if contract.deal_id in self._spent:
                return
            self._reserved.pop(contract.deal_id, None)
            self._spent[contract.deal_id] = (contract.buyer, contract.price)

    # ---------------------------------------------------------------- checks

    def check_deal(
        self, contract: Contract, now: datetime | None = None, *, reputation_rules: bool = True
    ) -> MandateReport:
        """Both parties must hold a valid, matching mandate for this contract.

        `reputation_rules=False` skips the counterparty reputation rules: they gate *entering* a deal, and a
        counterparty's record drifting afterwards must not strand funds that are already held."""
        now = now or datetime.now()
        violations: list[str] = []
        checks: list[str] = []
        # Reputation may be an HTTP call, so fetch it before taking the lock.
        looked_up = self._lookup_reputations(contract) if reputation_rules else {}
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
                if reputation_rules:
                    self._check_reputation(
                        agent_id, m, counterparties, looked_up.get(counterparties), violations, checks
                    )
        return MandateReport(ok=not violations, violations=violations, checks=checks)

    def _lookup_reputations(self, contract: Contract) -> dict[str, Reputation | str]:
        """Reputation of each counterparty whose partner's mandate sets a rule; an error string on failure."""
        found: dict[str, Reputation | str] = {}
        for side, other in (("buyer", "seller"), ("seller", "buyer")):
            m = self._mandates.get(getattr(contract, side))
            if m is None or (m.min_counterparty_deals is None and m.max_counterparty_dispute_rate is None):
                continue
            counterparty = getattr(contract, other)
            if counterparty in found or self.reputation is None:
                continue
            try:
                found[counterparty] = self.reputation.reputation(counterparty, exclude_deal=contract.deal_id)
            except Exception as exc:  # noqa: BLE001 - an unreadable reputation must fail closed, not crash the gate
                found[counterparty] = f"lookup failed ({type(exc).__name__})"
        return found

    def _check_reputation(
        self,
        agent_id: str,
        m: Mandate,
        counterparty: str,
        rep: Reputation | str | None,
        violations: list[str],
        checks: list[str],
    ) -> None:
        if m.min_counterparty_deals is None and m.max_counterparty_dispute_rate is None:
            return
        if self.reputation is None:
            violations.append(
                f"{agent_id}: mandate sets counterparty reputation rules but no reputation source is configured"
            )
            return
        if rep is None or isinstance(rep, str):
            violations.append(f"{agent_id}: counterparty '{counterparty}' reputation unavailable ({rep or 'no data'})")
            return
        if m.min_counterparty_deals is not None:
            if rep.deals_total < m.min_counterparty_deals:
                violations.append(
                    f"{agent_id}: counterparty '{counterparty}' has {rep.deals_total} deals, "
                    f"mandate needs at least {m.min_counterparty_deals}"
                )
            else:
                checks.append(
                    f"{agent_id}: counterparty '{counterparty}' deals {rep.deals_total} >= {m.min_counterparty_deals}"
                )
        if m.max_counterparty_dispute_rate is not None:
            rate = Decimal(str(rep.dispute_rate))
            if rate > m.max_counterparty_dispute_rate:
                violations.append(
                    f"{agent_id}: counterparty '{counterparty}' dispute rate {rate} "
                    f"exceeds mandate cap {m.max_counterparty_dispute_rate}"
                )
            else:
                cap = m.max_counterparty_dispute_rate
                checks.append(f"{agent_id}: counterparty '{counterparty}' dispute rate {rate} <= {cap}")

    def recheck(self, contract: Contract, now: datetime | None = None) -> MandateReport:
        """The gate again, at the moment before capture: authority can lapse
        (expiry, revocation, budget spent elsewhere) between escrow and settlement.
        The deal's own reservation is not counted against itself."""
        return self.check_deal(contract, now, reputation_rules=False)


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
