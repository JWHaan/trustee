"""Deal state machine — the single source of truth for money movement.

States:
    DRAFT -> NEGOTIATING -> CONTRACTED -> ESCROWED -> DELIVERED -> SETTLED
    branches: ESCROWED/DELIVERED -> VOIDED (reject, timeout, refund)
              DELIVERED -> DISPUTED -> (arbiter) SETTLED | VOIDED

Rules:
  * Transitions are guarded and **idempotent**: repeating a completed
    transition is a no-op that returns (state, replayed=True), so webhook
    redeliveries can never double-settle.
  * Only ESCROWED may capture; only ESCROWED may void after settlement was
    pending; only VOIDED/SETTLED are terminal.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum


class DealState(StrEnum):
    DRAFT = "DRAFT"
    NEGOTIATING = "NEGOTIATING"
    CONTRACTED = "CONTRACTED"
    ESCROWED = "ESCROWED"
    DELIVERED = "DELIVERED"
    DISPUTED = "DISPUTED"
    SETTLED = "SETTLED"
    VOIDED = "VOIDED"


TERMINAL = frozenset({DealState.SETTLED, DealState.VOIDED})

_TRANSITIONS: dict[DealState, set[DealState]] = {
    DealState.DRAFT: {DealState.NEGOTIATING},
    DealState.NEGOTIATING: {DealState.CONTRACTED, DealState.DRAFT, DealState.VOIDED},
    DealState.CONTRACTED: {DealState.ESCROWED, DealState.VOIDED},
    DealState.ESCROWED: {DealState.DELIVERED, DealState.VOIDED},
    DealState.DELIVERED: {DealState.SETTLED, DealState.DISPUTED, DealState.VOIDED},
    DealState.DISPUTED: {DealState.SETTLED, DealState.VOIDED},
    DealState.SETTLED: set(),
    DealState.VOIDED: set(),
}


class IllegalTransition(Exception):
    def __init__(self, deal_id: str, from_state: DealState, to_state: DealState):
        self.deal_id, self.from_state, self.to_state = deal_id, from_state, to_state
        super().__init__(f"deal {deal_id}: {from_state} -> {to_state} not allowed")


class Deal:
    """One deal. Thread-safety: the Trustee serializes a deal with a lock.

    `on_transition(reason, from_state, to_state)` fires inside `transition()`,
    so an observer (the arena ledger) records each move at the moment it
    happens. Draining `history` afterwards would put every state row after the
    PayPal call it caused — the tape would read as if the money moved first.
    """

    def __init__(self, deal_id: str, on_transition: Callable[[str, str, str], None] | None = None):
        self.deal_id = deal_id
        self.state = DealState.DRAFT
        self.history: list[tuple[str, str, str]] = []  # (reason, from, to)
        self._on_transition = on_transition

    @classmethod
    def restore(
        cls,
        deal_id: str,
        state: DealState,
        history: list[tuple[str, str, str]] | None = None,
        on_transition: Callable[[str, str, str], None] | None = None,
    ) -> Deal:
        """Rebuild a deal at a persisted state without replaying (or re-announcing) transitions."""
        deal = cls(deal_id, on_transition=on_transition)
        deal.state = DealState(state)
        deal.history = [(r, f, t) for r, f, t in (history or [])]
        return deal

    def can(self, to: DealState) -> bool:
        return to in _TRANSITIONS[self.state]

    def transition(self, to: DealState, reason: str = "") -> tuple[DealState, bool]:
        """Move to `to`. Idempotent: to == current state -> (state, replayed=True)."""
        if to == self.state:
            return self.state, True
        if not self.can(to):
            raise IllegalTransition(self.deal_id, self.state, to)
        from_state = self.state
        self.state = to
        self.history.append((reason, from_state.value, to.value))
        if self._on_transition is not None:
            self._on_transition(reason, from_state.value, to.value)
        return self.state, False

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL
