"""Pro-rata settlement math. Pure, Decimal-only, no I/O and no LLM.

A contract that carries `PartialTerms` lets a rejected delivery settle for the valid share of the
price. The share comes from counts the buyer's checker measured on the bytes it received
(`units_valid`, `units_total` in the attestation evidence), never from a model. Anything missing,
malformed or out of range returns None, which means "no partial settlement": the caller follows the
ordinary reject path.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from decimal import ROUND_DOWN, Decimal

from .protocol import Contract

CENT = Decimal("0.01")
MAX_UNITS = 10**12  # a sanity bound on counts; far above any real deliverable
EVIDENCE_VALID = "units_valid"
EVIDENCE_TOTAL = "units_total"


@dataclass(frozen=True)
class PartialSettlement:
    """What a pro-rata settlement pays. All amounts are strings so the record is JSON-safe as is."""

    basis: str
    units_valid: int
    units_total: int
    fraction: str  # valid / total as a percentage-free decimal, e.g. "0.9"
    price: str
    captured: str
    released: str
    currency: str

    def as_dict(self) -> dict[str, str | int]:
        return asdict(self)

    def label(self) -> str:
        """e.g. "settled 90% ($140.40 of $156.00)" for tapes and status text."""
        pct = (Decimal(self.fraction) * 100).quantize(Decimal("0.1"), rounding=ROUND_DOWN).normalize()
        return f"settled {pct:f}% ({self.captured} of {self.price} {self.currency})"


def floor_to_cent(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_DOWN)


def prorata_amount(price: Decimal, units_valid: int, units_total: int) -> Decimal:
    """`price * units_valid / units_total`, rounded DOWN to the cent, in exact integer-cent arithmetic."""
    cents = (price * 100 * units_valid) // units_total
    return floor_to_cent(Decimal(cents) / 100)


def _count(raw: object) -> int | None:
    """A non-negative integer from an ASCII digit string, else None (no signs, spaces, floats)."""
    if not isinstance(raw, str) or not raw.isascii() or not raw.isdigit():
        return None
    value = int(raw)
    return value if value <= MAX_UNITS else None


def compute_partial(contract: Contract, evidence: Mapping[str, str]) -> PartialSettlement | None:
    """The pro-rata payout the contract allows for this evidence, or None.

    None when: the contract has no partial term; counts are absent or malformed; the total is zero;
    valid exceeds total; nothing is valid; every unit is valid (a reject over something other than
    the counted units is not a partial case); the fraction is below `min_fraction`; or the amount
    rounds down to zero.
    """
    terms = contract.partial
    if terms is None:
        return None
    valid = _count(evidence.get(EVIDENCE_VALID))
    total = _count(evidence.get(EVIDENCE_TOTAL))
    if valid is None or total is None or total == 0 or valid == 0 or valid >= total:
        return None
    if Decimal(valid) < terms.min_fraction * total:  # exact: Decimal times int
        return None
    amount = prorata_amount(contract.price, valid, total)
    if amount <= 0 or amount >= contract.price:
        return None
    fraction = (Decimal(valid) / Decimal(total)).quantize(Decimal("0.000001"), rounding=ROUND_DOWN).normalize()
    return PartialSettlement(
        basis=terms.basis,
        units_valid=valid,
        units_total=total,
        fraction=f"{fraction:f}",
        price=str(contract.price),
        captured=str(amount),
        released=str(contract.price - amount),
        currency=contract.currency,
    )
