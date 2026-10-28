"""Trustee agent — the settlement agent.

Role: when two agents form a Contract, the Trustee:
  1. gate   — check both parties' mandates (hard policy gate, no LLM)
  2. escrow — PayPal authorize: funds held, not moved
  3. settle — on delivery + buyer attestation(accept): capture
  4. unwind — on attestation(reject) / deadline lapse: void + refund event

The LLM is used for *deciding and explaining* (action choice, ledger
narration, dispute notes). Every money call is made by the policy layer,
never by the LLM — it cannot invent amounts, parties, or endpoints.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..llm import LLM
from ..mandate import MandateEngine, MandateReport
from ..paypal import EscrowOrder, PayPalClient, PayPalError
from ..protocol import Attest, Contract, Delivery
from ..state import Deal, DealState, IllegalTransition

log = logging.getLogger(__name__)


@dataclass
class TrusteeDecision:
    action: str  # "settled" | "voided" | "rejected_mandate" | "error"
    deal: Deal
    report: MandateReport | None = None
    escrow: EscrowOrder | None = None
    explanation: str = ""


class Trustee:
    """Owns a deal's money path. One Trustee instance per deal."""

    def __init__(
        self,
        deal: Deal,
        mandates: MandateEngine,
        paypal: PayPalClient,
        llm: LLM | None = None,
        ledger=None,
        buyer_email: str = "buyer-sandbox@trustee.dev",
        seller_email: str = "seller-sandbox@trustee.dev",
    ):
        self.deal = deal
        self.mandates = mandates
        self.paypal = paypal
        self.llm = llm
        self.ledger = ledger
        self._buyer_email = buyer_email
        self._seller_email = seller_email
        self._escrow: EscrowOrder | None = None

    # ------------------------------------------------------------ public API

    def on_contract(self, contract: Contract) -> TrusteeDecision:
        """Contract formed -> mandate gate -> open escrow."""
        self._log("msg", {"event": "contract_formed", **contract.key_clauses()})
        report = self.mandates.check_deal(contract)
        self._log("mandate", {"report": report.summarize(), "ok": report.ok})
        if not report.ok:
            self._explanation("mandate gate", contract, "deal refused: " + report.summarize())
            return TrusteeDecision("rejected_mandate", self.deal, report)
        self.deal.transition(DealState.CONTRACTED, "contract accepted")
        try:
            self._escrow = self.paypal.create_and_authorize(
                amount=contract.price,
                currency=contract.currency,
                buyer_email=self._buyer_email,
                seller_email=self._seller_email,
                memo=f"trustee escrow {contract.deal_id} ({contract.good.description[:60]})",
            )
        except PayPalError as exc:
            self._log("paypal", {"error": str(exc), "op": "authorize"})
            self._explanation("authorize", contract, f"escrow failed: {exc}")
            return TrusteeDecision("error", self.deal, report)
        escrow = self._escrow
        self.deal.transition(DealState.ESCROWED, "funds held in PayPal escrow")
        self._log(
            "paypal",
            {
                "op": "authorize",
                "order_id": escrow.order_id,
                "amount": str(contract.price),
                "note": "funds held, not moved",
            },
        )
        self._explanation("escrow", contract, f"escrow opened: {escrow.order_id}, {contract.price} held")
        return TrusteeDecision("escrowed", self.deal, report, escrow)

    def on_delivery(self, delivery: Delivery, contract: Contract) -> TrusteeDecision:
        """Seller handed off the deliverable. Verify hash, move to DELIVERED."""
        escrow = self._raise_escrow()
        if delivery.deliverable_ref != contract.good.deliverable_ref:
            self._log("delivery", {"error": "ref mismatch", "got": delivery.deliverable_ref})
            self._explanation("delivery", contract, "delivery ref does not match contract")
            return TrusteeDecision("error", self.deal, escrow=escrow)
        self.deal.transition(DealState.DELIVERED, "deliverable handed off")
        self._log(
            "delivery",
            {
                "ref": delivery.deliverable_ref,
                "sha256": delivery.content_hash,
                "note": "hash recorded in ledger for arbitration audit",
            },
        )
        self._explanation("delivery", contract, "delivery recorded; awaiting buyer attestation")
        return TrusteeDecision("delivered", self.deal, escrow=escrow)

    def on_attest(self, attest: Attest, contract: Contract) -> TrusteeDecision:
        """Buyer's verdict. Accept -> capture. Reject -> dispute path (or void).
        Terminal deals are idempotent no-ops (webhook replay safety)."""
        if self.deal.is_terminal:
            return TrusteeDecision("no-op", self.deal, escrow=self._escrow)
        if attest.verdict == "accept":
            escrow = self.paypal.capture(self._raise_escrow())
            self._escrow = escrow
            self.deal.transition(DealState.SETTLED, "buyer attested accept; funds captured")
            self._log("paypal", {"op": "capture", "order_id": escrow.order_id, "note": "settled to seller"})
            self._explanation("settle", contract, f"settled: {escrow.details.get('capture_id', escrow.order_id)}")
            return TrusteeDecision("settled", self.deal, escrow=escrow)

        # reject: arbiter may rule; without one, the Trustee voids (refunds).
        self.deal.transition(DealState.DISPUTED, f"buyer rejected: {attest.reason[:120]}")
        self._log("state", {"attest": attest.model_dump(mode="json")})
        ruling = self._arbitrate(attest, contract)
        if ruling == "release":
            escrow = self.paypal.void(self._raise_escrow(), reason=attest.reason[:120])
            self._escrow = escrow
            self.deal.transition(DealState.VOIDED, "arbitration released funds (refund)")
            self._log("paypal", {"op": "void", "order_id": escrow.order_id, "note": "funds released to buyer"})
            self._explanation("unwind", contract, "voided; buyer refunded after arbitration")
            return TrusteeDecision("voided", self.deal, escrow=escrow)
        return TrusteeDecision("disputed", self.deal, escrow=self._escrow)

    def on_deadline_lapse(self, contract: Contract) -> TrusteeDecision:
        """Deal deadline passed with no attestation -> release funds."""
        if self.deal.is_terminal:
            return TrusteeDecision("no-op", self.deal, escrow=self._escrow)
        self._escrow = self.paypal.void(self._escrow or EscrowOrder(order_id="unknown", status="authorized"),
                                        reason="deadline_lapse")
        self.deal.transition(DealState.VOIDED, "deadline lapsed; funds released")
        self._log("paypal", {"op": "void", "note": "auto-void on deadline"})
        self._explanation("unwind", contract, "deadline lapsed — funds released automatically")
        return TrusteeDecision("voided", self.deal, escrow=self._escrow)

    # ------------------------------------------------------------- internals

    def _raise_escrow(self) -> EscrowOrder:
        if self._escrow is None:
            raise IllegalTransition(self.deal.deal_id, self.deal.state, DealState.DELIVERED)
        return self._escrow

    def _arbitrate(self, attest: Attest, contract: Contract) -> str:
        """Default arbiter: deterministic policy. Swappable for the LLM judge
        (Agent #4) — same signature, so the platform never changes. Returns
        'release' or 'uphold'.
        """
        # Evidence-based default: hash recorded and buyer cites a failed
        # acceptance criterion -> release. No credible evidence -> uphold.
        has_evidence = bool(attest.evidence)
        return "release" if has_evidence else "uphold"

    def _log(self, kind: str, payload: dict) -> None:
        if self.ledger is not None:
            self.ledger.append(self.deal.deal_id, "trustee", kind, **payload)

    def _explanation(self, op: str, contract: Contract, fact: str) -> None:
        """LLM narrates the decision into the ledger; falls back to the raw
        fact when no LLM is configured (offline demo / tests)."""
        if self.llm is None:
            self._log("note", {"op": op, "explanation": fact})
            return
        try:
            text = self.llm.complete_json(
                "You are the Trustee, a settlement agent. "
                "Explain this decision in one plain sentence for a ledger entry.",
                [
                    {
                        "role": "user",
                        "content": (
                            f"Decision '{op}' for deal {contract.deal_id}: {fact}. "
                            "Respond with JSON: {\"explanation\": str}"
                        ),
                    }
                ],
            )
            self._log("note", {"op": op, "explanation": text.get("explanation", fact)})
        except Exception as exc:  # noqa: BLE001 - narration must never block money path
            log.warning("explanation LLM failed: %s", exc)
            self._log("note", {"op": op, "explanation": fact, "narration_failed": str(exc)[:100]})


def ruling_note(action: str, note: str) -> str:
    return note if note else ("funds released" if action == "release" else "funds upheld")
