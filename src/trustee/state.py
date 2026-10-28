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
    DealState.NEGOTIATING: {DealState.CONTRACTED, DealState.DRAFT},
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
        super().__init__(
            f"deal {deal_id}: {from_state} -> {to_state} not allowed"
        )


class Deal:
    """One deal. Thread-safety: the Trustee serializes a deal with a lock."""

    def __init__(self, deal_id: str):
        self.deal_id = deal_id
        self.state = DealState.DRAFT
        self.history: list[tuple[str, str, str]] = []  # (ts, from, to)

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
        return self.state, False

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL
