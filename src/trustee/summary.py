"""What happened to a deal, in a few plain sentences: the same words for the agents that call the API and the people
who look at the arena.

`summarize()` turns the facts a deal already has (state, price, escrow status, ruling, partial settlement, refusal)
into:

    status      a stable word to branch on: paid | partly_paid | refunded | cancelled | refused | on_hold |
                awaiting_approval | in_progress
    tone        good | bad | warn | neutral, for colour
    headline    one sentence, the answer to "what happened to my money?"
    reasons     at most three short sentences saying why
    next_step   what happens next, or who has to act; None when the deal is finished
    money       where the money ended up: paid_to_seller, returned_to_buyer, held (decimal strings)

It reads facts and never decides anything: the state machine and the arbiter have already done that.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

MAX_REASON = 160
_ZERO = "0.00"


def _amount(value: Any) -> str:
    try:
        return f"{Decimal(str(value)):.2f}"
    except (InvalidOperation, ValueError):
        return _ZERO


def _clip(text: str | None) -> str | None:
    """One readable sentence: first line, trimmed at a word boundary, no trailing punctuation clutter."""
    if not text:
        return None
    line = " ".join(str(text).split())
    for prefix in ("objective breach: ", "failed: "):  # internal labels, not words a person needs
        if line.lower().startswith(prefix):
            line = line[len(prefix) :]
    if len(line) > MAX_REASON:
        line = line[:MAX_REASON].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    if not line:
        return None
    first = line.split(" ", 1)[0]
    if first.isalpha():  # leave identifiers such as sha256 or order_id as written
        line = line[0].upper() + line[1:]
    return line


def _show(amount: str, currency: str) -> str:
    """$156.00 for US dollars, 156.00 EUR otherwise: the same form the arena's money line uses."""
    return f"${amount}" if currency == "USD" else f"{amount} {currency}"


def _plain_failure(clause: str | None) -> str:
    """What a failed acceptance criterion means, in words a person reads without knowing the contract format."""
    text = (clause or "").lower()
    if "sha256" in text or "hash" in text:
        return "it isn't the file the contract names (its fingerprint is different)"
    if "schema" in text or "column" in text:
        return "its columns don't match what the contract specifies"
    if "row" in text or "at least" in text:
        return "rows are missing or have blank values"
    if "market" in text:
        return "its prices don't match live market prices"
    return f"\u201c{clause}\u201d" if clause else "a contract check failed"


def _money(paid: str = _ZERO, returned: str = _ZERO, held: str = _ZERO) -> dict[str, str]:
    return {"paid_to_seller": paid, "returned_to_buyer": returned, "held": held}


def summarize(
    *,
    state: str,
    price: Any = None,
    currency: str = "USD",
    escrow_status: str | None = None,
    ruling: dict[str, Any] | None = None,
    settlement: dict[str, Any] | None = None,
    needs_attention: str | None = None,
    outcome: str | None = None,
    refusal: str | None = None,
    buyer_reason: str | None = None,
    dispute_deadline: str | None = None,
    human_present: bool | None = None,
    authority_outcome: str | None = None,
    verification: dict[str, Any] | None = None,
) -> dict[str, Any]:
    amount = _amount(price)
    cur = currency or "USD"
    reasons: list[str] = []

    def done(status: str, tone: str, headline: str, money: dict[str, str], next_step: str | None = None) -> dict:
        kept = [r for r in (_clip(x) for x in reasons) if r][:3]
        return {
            "status": status,
            "tone": tone,
            "headline": headline,
            "reasons": kept,
            "next_step": next_step,
            "money": money,
            "currency": cur,
        }

    # Stopped before any money was held.
    if outcome in ("rejected_identity", "rejected_mandate"):
        if outcome == "rejected_identity":
            # The raw refusal names internal agent ids; the person needs what it means.
            reasons.append("A signature on the contract doesn't match the key registered for that agent.")
        else:
            reasons.append(refusal or "The deal is outside what the buyer or seller is allowed to do.")
        reasons.append("PayPal was never called.")
        return done("refused", "bad", "Stopped before payment. No money moved.", _money())
    if outcome in ("approval_timeout", "approval_cancelled", "approval_failed"):
        why = {
            "approval_timeout": "The payer did not approve at PayPal in time.",
            "approval_cancelled": "The payer cancelled at PayPal.",
            "approval_failed": refusal or "PayPal refused to hold the approved payment.",
        }[outcome]
        reasons.append(why)
        return done("cancelled", "neutral", "Cancelled. Nothing was charged.", _money())

    if state == "SETTLED":
        if settlement:
            paid = _amount(settlement.get("captured"))
            back = _amount(settlement.get("released"))
            valid, total = settlement.get("units_valid"), settlement.get("units_total")
            if valid is not None and total:
                reasons.append(f"{valid} of {total} items passed the checks; the contract pays for the valid share.")
            if verification:
                reasons.append("Trustee counted the valid items itself, not only the buyer's word.")
            return done(
                "partly_paid",
                "warn",
                f"Partly paid: the seller received {_show(paid, cur)}, the buyer got {_show(back, cur)} back.",
                _money(paid=paid, returned=back),
            )
        if ruling and ruling.get("decision") in ("uphold", "settle"):
            reasons.append(str(ruling.get("reasoning") or "The dispute was decided for the seller."))
        else:
            reasons.append(buyer_reason or "The delivery passed every check in the contract.")
        if verification and verification.get("passed"):
            reasons.append("Trustee checked the file itself; it matches the contract.")
        if authority_outcome == "step_up":
            reasons.append("A person approved it, because it was outside the buyer's signed instruction.")
        elif human_present is False:
            reasons.append("Paid under the buyer's signed instruction, with no approval click.")
        return done("paid", "good", f"Paid: the seller received {_show(amount, cur)}.", _money(paid=amount))

    if state == "VOIDED":
        held_before = escrow_status in ("voided", "authorized") or bool(ruling)
        if ruling:
            # Plain words here; the raw evidence (hashes, column lists) is in the deal's checks and ledger.
            reasons.append(f"The delivery failed the contract: {_plain_failure(ruling.get('clause'))}.")
            if verification and not verification.get("passed"):
                reasons.append("Trustee checked the file itself, so this didn't rest on the buyer's word.")
            else:
                reasons.append("The seller isn't paid for a delivery that fails the contract.")
        elif refusal:
            reasons.append(refusal)
        else:
            reasons.append("The deal ended before it could complete, so the hold was released.")
        if held_before:
            # Blue, not red: the money was protected, which is the system working.
            return done(
                "refunded", "neutral", f"Refunded: the buyer keeps {_show(amount, cur)}.", _money(returned=amount)
            )
        return done("cancelled", "neutral", "Cancelled. Nothing was charged.", _money())

    if state == "DISPUTED":
        reasons.append(needs_attention or (ruling or {}).get("reasoning") or buyer_reason or "The buyer rejected it.")
        when = f" by {dispute_deadline[:16].replace('T', ' ')}" if dispute_deadline else ""
        return done(
            "on_hold",
            "warn",
            f"On hold: {_show(amount, cur)} stays held until a person decides.",
            _money(held=amount),
            f"An operator releases the money to the buyer or pays the seller{when}. "
            "If nobody does, the seller is paid when the dispute window ends.",
        )

    if state == "CONTRACTED" and escrow_status == "created":
        return done(
            "awaiting_approval",
            "neutral",
            "Waiting for the payer to approve at PayPal.",
            _money(),
            "Nothing is held until the payer approves.",
        )

    progress = {
        "ESCROWED": ("Funds held: waiting for the seller to deliver.", "The seller delivers the file."),
        "DELIVERED": ("Delivered: waiting for the buyer's checks.", "The buyer checks the file against the contract."),
    }
    if state in progress:
        headline, next_step = progress[state]
        if needs_attention:
            reasons.append(needs_attention)
            return done("on_hold", "warn", headline, _money(held=amount), next_step)
        return done("in_progress", "neutral", headline, _money(held=amount), next_step)

    if needs_attention:
        reasons.append(needs_attention)
        return done("on_hold", "warn", "Needs attention before it can continue.", _money(), None)
    return done(
        "in_progress", "neutral", "Setting up the deal.", _money(), "Trustee checks both sides, then holds the funds."
    )
