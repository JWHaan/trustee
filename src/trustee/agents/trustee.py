"""Trustee agent — the settlement agent.

Role: when two agents form a Contract, the Trustee:
  1. gate   — check both parties' mandates (hard policy gate, no LLM)
  2. escrow — PayPal authorize: funds held, not moved
  3. settle — on delivery + buyer attestation(accept): capture
  4. unwind — on attestation(reject) / deadline lapse: void + refund event

The LLM is used for *deciding and explaining* (action choice, ledger
narration, dispute notes). Every money call is made by the policy layer,
never by the LLM — it cannot invent amounts, parties, or endpoints.

Reliability rules for the money path:
  * every PayPal call carries a deterministic request id (`trustee-<deal>-<op>`),
    so repeating a call after a timeout or crash is deduplicated by PayPal;
  * a PayPal failure never raises out of a public method: it is ledgered and
    returned as TrusteeDecision("error"), the deal keeps its non-terminal state
    and the funds stay held, and calling the same method again is the retry;
  * identity is a key, not a name: when an `AgentRegistry` is set, the contract needs
    both parties' signatures and every delivery/attestation the sender's, checked
    before the mandate gate and before any PayPal call;
  * authority is re-checked at capture (`MandateEngine.recheck`): a mandate that
    was revoked, expired, or out of budget since escrow parks the deal in DISPUTED
    with funds held for a human, rather than moving money without authority or
    silently refunding a seller who delivered;
  * funds are never stranded: `tick()` voids past the deal deadline, resolves
    before the PayPal authorization expires, and captures an upheld dispute that
    no human overrode within the dispute window; `resolve_dispute()` is the
    human override.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from ..identity import AgentRegistry
from ..llm import LLM
from ..mandate import MandateEngine, MandateReport
from ..paypal import EscrowOrder, PayPalClient, PayPalError
from ..protocol import Attest, Contract, Delivery, verify_signature
from ..state import Deal, DealState, IllegalTransition
from .arbiter import Judge, Ruling

log = logging.getLogger(__name__)

# PayPal authorizations are valid for 29 days (3-day honor period inside that).
AUTH_VALIDITY = timedelta(days=29)


@dataclass
class TrusteeDecision:
    action: str  # "settled" | "voided" | "disputed" | "rejected_mandate" | "error" | "no-op" | ...
    deal: Deal
    report: MandateReport | None = None
    escrow: EscrowOrder | None = None
    explanation: str = ""


class Trustee:
    """Owns a deal's money path. One Trustee instance per deal.

    Every public method runs under a per-deal re-entrant lock, so concurrent
    webhook deliveries and the deadline sweeper cannot interleave a capture
    with a void.
    """

    def __init__(
        self,
        deal: Deal,
        mandates: MandateEngine,
        paypal: PayPalClient,
        llm: LLM | None = None,
        ledger=None,
        buyer_email: str = "buyer-sandbox@trustee.dev",
        seller_email: str = "seller-sandbox@trustee.dev",
        judge: Judge | None = None,
        dispute_window: timedelta = timedelta(hours=72),
        auth_expiry_margin: timedelta = timedelta(hours=24),
        registry: AgentRegistry | None = None,
    ):
        self.deal = deal
        self.mandates = mandates
        self.paypal = paypal
        self.llm = llm
        self.ledger = ledger
        self._buyer_email = buyer_email
        self._seller_email = seller_email
        self.judge = judge
        self.registry = registry
        self.dispute_window = dispute_window
        self.auth_expiry_margin = auth_expiry_margin
        self.authorized_at: datetime | None = None
        self.dispute_deadline: datetime | None = None
        self._ruling: Ruling | None = None  # arbiter ruling for the open dispute (decided once)
        self._escrow: EscrowOrder | None = None
        self._last_delivery: Delivery | None = None
        self._capture_block: str | None = None  # last reason a capture was held for mandate reasons
        self.needs_attention: str | None = None  # PayPal says something our state cannot follow; a human looks
        self._lock = threading.RLock()

    # ------------------------------------------------------------ public API

    def on_contract(self, contract: Contract, now: datetime | None = None) -> TrusteeDecision:
        """Contract formed -> mandate gate -> open escrow."""
        with self._lock:
            return self._on_contract(contract, now)

    def _on_contract(self, contract: Contract, now: datetime | None) -> TrusteeDecision:
        self._log("msg", {"event": "contract_formed", **contract.key_clauses()})
        if self.registry is not None:
            refused = self._check_contract_identity(contract)
            if refused is not None:
                return refused
        report = self.mandates.check_deal(contract)
        self._log("mandate", {"report": report.summarize(), "ok": report.ok})
        if not report.ok:
            self._explanation("mandate gate", contract, "deal refused: " + report.summarize())
            return TrusteeDecision("rejected_mandate", self.deal, report)
        self.deal.transition(DealState.CONTRACTED, "contract accepted")
        rid = self._request_id("authorize-create")
        try:
            self._escrow = self.paypal.create_and_authorize(
                amount=contract.price,
                currency=contract.currency,
                buyer_email=self._buyer_email,
                seller_email=self._seller_email,
                memo=f"trustee escrow {contract.deal_id} ({contract.good.description[:60]})",
                request_id=rid,
            )
        except PayPalError as exc:
            self._log("paypal", {"error": str(exc)[:300], "op": "authorize", "request_id": rid})
            self._explanation("authorize", contract, f"escrow failed: {exc}")
            return TrusteeDecision("error", self.deal, report)
        escrow = self._escrow
        if escrow.status != "authorized":
            # Approval flow: the order exists but no funds are held yet. Never
            # claim ESCROWED on an un-held order — that is the one lie that
            # would make the whole tape untrustworthy.
            self._log(
                "paypal",
                {
                    "op": "authorize",
                    "order_id": escrow.order_id,
                    "status": escrow.status,
                    "approve_url": escrow.approve_url,
                    "note": "buyer approval required before funds are held",
                },
            )
            self._explanation("approval", contract, "awaiting buyer approval in PayPal")
            return TrusteeDecision("awaiting_approval", self.deal, report, escrow)
        self._log(
            "paypal",
            {
                "op": "authorize",
                "order_id": escrow.order_id,
                "amount": str(contract.price),
                "note": "funds held, not moved",
            },
        )
        self.authorized_at = now or datetime.now()
        self.deal.transition(DealState.ESCROWED, "funds held in PayPal escrow")
        self.mandates.reserve(contract)
        self._explanation("escrow", contract, f"escrow opened: {escrow.order_id}, {contract.price} held")
        return TrusteeDecision("escrowed", self.deal, report, escrow)

    def on_delivery(self, delivery: Delivery, contract: Contract) -> TrusteeDecision:
        """Seller handed off the deliverable. Verify hash, move to DELIVERED."""
        with self._lock:
            refused = self._check_message_identity(delivery, contract.seller, "seller", "delivery")
            if refused is not None:
                return refused
            escrow = self._raise_escrow()
            if delivery.deliverable_ref != contract.good.deliverable_ref:
                self._log("delivery", {"error": "ref mismatch", "got": delivery.deliverable_ref})
                self._explanation("delivery", contract, "delivery ref does not match contract")
                return TrusteeDecision("error", self.deal, escrow=escrow)
            self.deal.transition(DealState.DELIVERED, "deliverable handed off")
            self._last_delivery = delivery
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

    def on_attest(self, attest: Attest, contract: Contract, now: datetime | None = None) -> TrusteeDecision:
        """Buyer's verdict. Accept -> capture. Reject -> dispute path (or void).
        Terminal deals are idempotent no-ops (webhook replay safety). If PayPal
        fails, the deal stays put (DELIVERED, or DISPUTED after a reject) and
        calling this again retries the same PayPal operation."""
        with self._lock:
            return self._on_attest(attest, contract, now or datetime.now())

    def _on_attest(self, attest: Attest, contract: Contract, now: datetime) -> TrusteeDecision:
        refused = self._check_message_identity(attest, contract.buyer, "buyer", "attest")
        if refused is not None:
            return refused
        if self.deal.is_terminal:
            return TrusteeDecision("no-op", self.deal, escrow=self._escrow)
        self._log("attest", {"verdict": attest.verdict, "reason": attest.reason, "evidence": attest.evidence})
        if attest.verdict == "accept":
            return self._settle(contract, "buyer attested accept; funds captured", now=now)

        # reject: the arbiter rules; without one, the Trustee applies its
        # built-in policy. Ruling decides whether the escrow is voided.
        self.deal.transition(DealState.DISPUTED, f"buyer rejected: {attest.reason[:120]}")
        if self._ruling is None:
            # Decided once: a retry after a failed void must not re-roll the ruling.
            _, self._ruling = self._arbitrate(attest, contract)
            ruling = self._ruling
            payload = {
                "decision": ruling.decision,
                "clause": ruling.clause,
                "note": ruling.reasoning,
                "objective": ruling.objective,
                "llm_assisted": ruling.llm_assisted,
            }
            if ruling.decision != "release":
                self.dispute_deadline = now + self.dispute_window
                payload["dispute_deadline"] = self.dispute_deadline.isoformat()
            self._log("ruling", payload)
        ruling = self._ruling
        if ruling.decision == "release":
            return self._refund(contract, ruling.clause, attest.reason[:120])
        self._explanation("dispute", contract, f"escrow held pending resolution — clause: {ruling.clause}")
        return TrusteeDecision("disputed", self.deal, escrow=self._escrow)

    def on_deadline_lapse(self, contract: Contract) -> TrusteeDecision:
        """Deal deadline passed with no attestation -> release funds."""
        with self._lock:
            if self.deal.is_terminal:
                return TrusteeDecision("no-op", self.deal, escrow=self._escrow)
            if not self._funds_held():
                # Nothing was ever held at PayPal: there is nothing to void, and
                # voiding a made-up order fails against the real API.
                if not self.deal.can(DealState.VOIDED):
                    return TrusteeDecision("no-op", self.deal, escrow=self._escrow)
                self._log("note", {"op": "unwind", "explanation": "deadline lapsed before funds were held"})
                self.deal.transition(DealState.VOIDED, "deadline lapsed; no funds were held")
                return TrusteeDecision("voided", self.deal, escrow=self._escrow)
            return self._refund(contract, "deadline_lapse", "deadline_lapse", "deadline lapsed; funds released")

    def tick(self, contract: Contract, now: datetime | None = None) -> TrusteeDecision:
        """Time-driven progress; safe to call repeatedly (e.g. from DeadlineSweeper).

        Acts only while funds are held at PayPal:
          * ESCROWED/DELIVERED past the contract deadline -> void ("deadline_lapse");
          * a decided "release" whose void failed -> retry the void;
          * an upheld dispute past its dispute window with no human override ->
            capture to the seller;
          * within `auth_expiry_margin` of the 29-day authorization expiry ->
            resolve now (capture if an upheld dispute is pending, else void),
            because an expired authorization would strand the funds.
        """
        now = now or datetime.now()
        with self._lock:
            noop = TrusteeDecision("no-op", self.deal, escrow=self._escrow)
            if self.awaiting_approval and now > contract.deadline:
                return self.on_deadline_lapse(contract)  # nothing held: VOIDED without a PayPal call
            if self.deal.is_terminal or not self._funds_held():
                return noop
            expiring = (
                self.authorized_at is not None and now >= self.authorized_at + AUTH_VALIDITY - self.auth_expiry_margin
            )
            if self.deal.state == DealState.DISPUTED:
                ruling = self._ruling
                if ruling is not None and ruling.decision == "release":
                    return self._refund(contract, ruling.clause, "retry after failed void")
                window_over = self.dispute_deadline is not None and now > self.dispute_deadline
                if ruling is not None and (window_over or expiring):
                    why = "dispute window lapsed" if window_over else "authorization about to expire"
                    return self._settle(contract, f"{why} with an upheld dispute; funds captured to seller", now=now)
                return noop
            if self.deal.state in (DealState.ESCROWED, DealState.DELIVERED):
                if now > contract.deadline:
                    return self.on_deadline_lapse(contract)
                if expiring:
                    return self._refund(
                        contract, "auth_expiry", "auth_expiry", "authorization expiring; funds released"
                    )
            return noop

    def on_approved(self, contract: Contract, now: datetime | None = None) -> TrusteeDecision:
        """The payer approved the order at PayPal -> re-check authority -> hold the funds.

        Valid only for a CONTRACTED deal whose order is still `created`. Anything later is
        a replay and a no-op, so a double redirect or a redelivered webhook cannot authorize twice.
        On a PayPal failure the state is unchanged and calling this again is the retry."""
        with self._lock:
            noop = TrusteeDecision("no-op", self.deal, escrow=self._escrow)
            if not self.awaiting_approval:
                return noop
            assert self._escrow is not None
            report = self.mandates.recheck(contract, now)  # time has passed since the contract formed
            if not report.ok:
                self._log("mandate", {"report": report.summarize(), "ok": False, "when": "approval"})
                self._explanation("approval", contract, "approval received but mandate invalid: " + report.summarize())
                return TrusteeDecision("rejected_mandate", self.deal, report, self._escrow, report.summarize())
            order_id = self._escrow.order_id
            rid = self._request_id("authorize")
            try:
                escrow = self.paypal.authorize_after_approval(order_id, request_id=rid)
            except PayPalError as exc:
                self._paypal_failed("authorize", rid, exc)
                self._explanation("approval", contract, f"authorize after approval failed: {exc}")
                return TrusteeDecision("error", self.deal, report, self._escrow, f"authorize failed: {exc}")
            escrow.approve_url = self._escrow.approve_url
            self._escrow = escrow
            self._log(
                "paypal",
                {
                    "op": "authorize",
                    "order_id": order_id,
                    "amount": str(contract.price),
                    "note": "payer approved; funds held",
                },
            )
            self.authorized_at = now or datetime.now()
            self.deal.transition(DealState.ESCROWED, "payer approved; funds held in PayPal escrow")
            self.mandates.reserve(contract)
            self._explanation("escrow", contract, f"escrow opened after approval: {order_id}, {contract.price} held")
            return TrusteeDecision("escrowed", self.deal, report, escrow)

    def on_approval_cancelled(self, contract: Contract) -> TrusteeDecision:
        """The payer backed out at PayPal. Nothing was held, so there is nothing to call or refund."""
        with self._lock:
            if not self.awaiting_approval:
                return TrusteeDecision("no-op", self.deal, escrow=self._escrow)
            self._log("note", {"op": "approval", "explanation": "payer cancelled approval at PayPal"})
            self.deal.transition(DealState.VOIDED, "payer cancelled approval; no funds were held")
            self._explanation("unwind", contract, "approval cancelled by payer; nothing to refund")
            return TrusteeDecision("voided", self.deal, escrow=self._escrow)

    # -- PayPal's own events are ground truth for money movement ---------------

    def on_external_void(self, contract: Contract, source: str) -> TrusteeDecision:
        """PayPal says the authorization is voided (e.g. our void call timed out but landed)."""
        with self._lock:
            if self.deal.state == DealState.VOIDED:
                return TrusteeDecision("no-op", self.deal, escrow=self._escrow)
            if self.deal.is_terminal or not self.deal.can(DealState.VOIDED):
                return self._alert(
                    contract,
                    "void",
                    f"PayPal voided the authorization ({source}) but the deal is {self.deal.state.value}",
                )
            if self._escrow is not None:
                self._escrow.status = "voided"
                self._escrow.details["void_reason"] = f"reconciled from {source}"
            self.mandates.release(contract)
            self._log("reconcile", {"op": "void", "source": source, "note": "PayPal reports the hold voided"})
            self.deal.transition(DealState.VOIDED, f"reconciled: PayPal voided the authorization ({source})")
            self._explanation("reconcile", contract, "PayPal confirms the hold was voided; buyer keeps funds")
            return TrusteeDecision("voided", self.deal, escrow=self._escrow)

    def on_external_capture(self, contract: Contract, capture_id: str, source: str) -> TrusteeDecision:
        """PayPal says the funds were captured (e.g. our capture call timed out but landed)."""
        with self._lock:
            if self.deal.state == DealState.SETTLED:
                return TrusteeDecision("no-op", self.deal, escrow=self._escrow)
            if self.deal.is_terminal or not self.deal.can(DealState.SETTLED):
                return self._alert(
                    contract,
                    "capture",
                    f"PayPal captured {capture_id} ({source}) but the deal is {self.deal.state.value}",
                )
            if self._escrow is not None:
                self._escrow.status = "captured"
                self._escrow.details["capture_id"] = capture_id
            self.mandates.commit(contract)
            self._log(
                "reconcile",
                {"op": "capture", "capture_id": capture_id, "source": source, "note": "PayPal reports funds captured"},
            )
            self.deal.transition(DealState.SETTLED, f"reconciled: PayPal captured the funds ({source})")
            self._explanation("reconcile", contract, f"PayPal confirms capture {capture_id}; settled to seller")
            return TrusteeDecision("settled", self.deal, escrow=self._escrow)

    def on_paypal_alert(self, contract: Contract, event_type: str, detail: str) -> TrusteeDecision:
        """A PayPal event (denied/reversed/refunded capture) that no state transition models: flag it."""
        with self._lock:
            return self._alert(contract, event_type, detail)

    def _alert(self, contract: Contract, what: str, detail: str) -> TrusteeDecision:
        self.needs_attention = detail[:300]
        self._log("paypal_alert", {"event": what, "detail": detail[:300], "state": self.deal.state.value})
        self._explanation("alert", contract, f"needs attention: {detail}")
        return TrusteeDecision("alert", self.deal, escrow=self._escrow, explanation=detail)

    def resolve_dispute(
        self,
        contract: Contract,
        decision: Literal["release", "settle"],
        operator: str,
        reason: str,
    ) -> TrusteeDecision:
        """Human override of a DISPUTED deal. release -> void/refund, settle -> capture."""
        if decision not in ("release", "settle"):
            raise ValueError(f"decision must be 'release' or 'settle', got {decision!r}")
        if not operator.strip():
            raise ValueError("an override must name its operator")
        with self._lock:
            if self.deal.is_terminal:
                return TrusteeDecision("no-op", self.deal, escrow=self._escrow)
            if self.deal.state != DealState.DISPUTED:
                target = DealState.VOIDED if decision == "release" else DealState.SETTLED
                raise IllegalTransition(self.deal.deal_id, self.deal.state, target)
            if decision == "release":
                result = self._refund(
                    contract, f"operator {operator}", reason[:120], "operator override: funds released"
                )
            else:
                # The operator is the human authority here: a failed recheck is recorded
                # on the ruling below rather than blocking the override they chose.
                result = self._settle(contract, f"operator override by {operator}: funds captured", recheck=False)
            if result.action in ("voided", "settled"):
                self._log(
                    "ruling",
                    {
                        "decision": decision,
                        "clause": "operator override",
                        "note": reason,
                        "operator": operator,
                        "override": True,
                        **self._override_recheck(contract),
                        "objective": False,
                        "llm_assisted": False,
                    },
                )
            return result

    # ------------------------------------------------------------- internals

    def _request_id(self, op: str) -> str:
        """Deterministic per deal+operation, so every retry is the same PayPal request."""
        return f"trustee-{self.deal.deal_id}-{op}"[:107]

    def _require_reachable(self, target: DealState) -> None:
        """Money must never move when the state machine cannot follow: check BEFORE any PayPal call."""
        if not self.deal.can(target):
            raise IllegalTransition(self.deal.deal_id, self.deal.state, target)

    @property
    def awaiting_approval(self) -> bool:
        """An order exists at PayPal but the payer has not approved it, so no funds are held."""
        return self.deal.state == DealState.CONTRACTED and self._escrow is not None and self._escrow.status == "created"

    def _funds_held(self) -> bool:
        return self._escrow is not None and self._escrow.status == "authorized"

    def _paypal_failed(self, op: str, rid: str, exc: PayPalError) -> None:
        self._log(
            "paypal",
            {
                "op": op,
                "error": str(exc)[:300],
                "status_code": exc.status_code,
                "request_id": rid,
                "note": "funds remain held; retrying the same call is safe",
            },
        )

    def _settle(
        self, contract: Contract, reason: str, *, now: datetime | None = None, recheck: bool = True
    ) -> TrusteeDecision:
        """Capture to the seller and move to SETTLED. PayPal failure -> "error", state unchanged.
        A failed mandate recheck holds the funds instead (see `_hold_for_mandate`)."""
        self._require_reachable(DealState.SETTLED)
        escrow = self._raise_escrow()
        if recheck:
            report = self.mandates.recheck(contract, now)
            if not report.ok:
                return self._hold_for_mandate(contract, report)
        rid = self._request_id("capture")
        try:
            escrow = self.paypal.capture(escrow, request_id=rid)
        except PayPalError as exc:
            self._paypal_failed("capture", rid, exc)
            self._explanation("settle", contract, f"capture failed, funds still held: {exc}")
            return TrusteeDecision("error", self.deal, escrow=self._escrow, explanation=f"capture failed: {exc}")
        self._escrow = escrow
        self._log("paypal", {"op": "capture", "order_id": escrow.order_id, "note": "settled to seller"})
        self.mandates.commit(contract)
        self.deal.transition(DealState.SETTLED, reason)
        self._explanation("settle", contract, f"settled: {escrow.details.get('capture_id', escrow.order_id)}")
        return TrusteeDecision("settled", self.deal, escrow=escrow)

    def _refund(
        self, contract: Contract, clause: str, void_reason: str, transition_reason: str = ""
    ) -> TrusteeDecision:
        """Void the hold (buyer keeps funds) and move to VOIDED. PayPal failure -> "error", state unchanged."""
        self._require_reachable(DealState.VOIDED)
        escrow = self._raise_escrow()
        rid = self._request_id("void")
        try:
            escrow = self.paypal.void(escrow, reason=void_reason, request_id=rid)
        except PayPalError as exc:
            self._paypal_failed("void", rid, exc)
            self._explanation("unwind", contract, f"void failed, funds still held: {exc}")
            return TrusteeDecision("error", self.deal, escrow=self._escrow, explanation=f"void failed: {exc}")
        self._escrow = escrow
        self._log("paypal", {"op": "void", "order_id": escrow.order_id, "note": "funds released to buyer"})
        self.mandates.release(contract)
        self.deal.transition(DealState.VOIDED, transition_reason or "arbitration released funds (refund)")
        self._explanation("unwind", contract, f"voided; buyer refunded — clause: {clause}")
        return TrusteeDecision("voided", self.deal, escrow=escrow)

    def _hold_for_mandate(self, contract: Contract, report: MandateReport) -> TrusteeDecision:
        """Authority lapsed between escrow and capture. Do not capture (no authority) and do not
        refund (the seller may have delivered): park in DISPUTED, funds held, for an operator."""
        note = f"mandate no longer valid at capture: {report.summarize()}"
        if self.deal.state != DealState.DISPUTED and self.deal.can(DealState.DISPUTED):
            self.deal.transition(DealState.DISPUTED, note[:120])
        if self._capture_block != note:
            self._capture_block = note
            self._log(
                "ruling",
                {
                    "decision": "hold",
                    "clause": "mandate recheck",
                    "note": note,
                    "objective": True,
                    "llm_assisted": False,
                },
            )
            self._explanation("settle", contract, f"capture withheld, funds still held: {note}")
        action = "disputed" if self.deal.state == DealState.DISPUTED else "rejected_mandate"
        return TrusteeDecision(action, self.deal, report, self._escrow, explanation=note)

    def _override_recheck(self, contract: Contract) -> dict[str, Any]:
        report = self.mandates.recheck(contract)
        return {} if report.ok else {"mandate_recheck": report.summarize()}

    # ------------------------------------------------------------- identity

    def _check_contract_identity(self, contract: Contract) -> TrusteeDecision | None:
        """Both parties must be registered in the right role and have signed this exact digest."""
        assert self.registry is not None
        digest = contract.digest()
        for role, agent_id in (("buyer", contract.buyer), ("seller", contract.seller)):
            record = self.registry.get(agent_id)
            if record is None:
                problem = "agent not registered"
            elif role not in record.roles:
                problem = f"agent is not registered as a {role}"
            elif not contract.verify_party(agent_id, self.registry):
                problem = "missing or invalid contract signature"
            else:
                self._log(
                    "identity",
                    {
                        "event": "verify_contract",
                        "signer": agent_id,
                        "ok": True,
                        "note": f"{role} signed {digest[:12]}",
                    },
                )
                continue
            return self._reject_identity("verify_contract", agent_id, f"{role} {agent_id}: {problem}")
        # Payee: a registered PayPal address beats the constructor default.
        buyer = self.registry.get(contract.buyer)
        seller = self.registry.get(contract.seller)
        if buyer is not None and buyer.paypal_email:
            self._buyer_email = buyer.paypal_email
        if seller is not None and seller.paypal_email:
            self._seller_email = seller.paypal_email
        return None

    def _check_message_identity(
        self, msg: Attest | Delivery, expected: str, role: str, subject: str
    ) -> TrusteeDecision | None:
        """`msg` must come from the contract's `role` party and carry that agent's valid signature."""
        if self.registry is None:
            return None
        if msg.sender != expected:
            return self._reject_identity(
                f"verify_{subject}", msg.sender, f"{subject} sender is not the contract {role}"
            )
        if not verify_signature(msg, self.registry):
            return self._reject_identity(
                f"verify_{subject}", msg.sender, f"{subject} from {msg.sender}: missing or invalid signature"
            )
        self._log(
            "identity",
            {"event": f"verify_{subject}", "signer": msg.sender, "ok": True, "note": f"{role} signature valid"},
        )
        return None

    def _reject_identity(self, event: str, signer: str, note: str) -> TrusteeDecision:
        self._log("identity", {"event": event, "signer": signer, "ok": False, "note": note})
        return TrusteeDecision("rejected_identity", self.deal, escrow=self._escrow, explanation=note)

    # ------------------------------------------------------------ persistence

    def snapshot(self) -> dict[str, Any]:
        """JSON-serializable state of this deal's money path, for a store to persist.
        Collaborators (mandates, paypal, ledger, llm, judge, registry) are not state:
        `from_snapshot` takes them again."""
        with self._lock:
            return {
                "deal_id": self.deal.deal_id,
                "state": self.deal.state.value,
                "history": [list(h) for h in self.deal.history],
                "escrow": asdict(self._escrow) if self._escrow is not None else None,
                "authorized_at": _iso(self.authorized_at),
                "dispute_deadline": _iso(self.dispute_deadline),
                "ruling": asdict(self._ruling) if self._ruling is not None else None,
                "last_delivery": self._last_delivery.model_dump(mode="json") if self._last_delivery else None,
                "capture_block": self._capture_block,
                "needs_attention": self.needs_attention,
                "buyer_email": self._buyer_email,
                "seller_email": self._seller_email,
                "dispute_window_s": self.dispute_window.total_seconds(),
                "auth_expiry_margin_s": self.auth_expiry_margin.total_seconds(),
            }

    @classmethod
    def from_snapshot(
        cls,
        snapshot: dict[str, Any],
        *,
        mandates: MandateEngine,
        paypal: PayPalClient,
        llm: LLM | None = None,
        ledger=None,
        judge: Judge | None = None,
        registry: AgentRegistry | None = None,
        on_transition=None,
        buyer_email: str | None = None,
        seller_email: str | None = None,
        dispute_window: timedelta | None = None,
        auth_expiry_margin: timedelta | None = None,
    ) -> Trustee:
        deal = Deal.restore(
            snapshot["deal_id"],
            DealState(snapshot["state"]),
            [tuple(h) for h in snapshot["history"]],
            on_transition=on_transition,
        )
        trustee = cls(
            deal,
            mandates,
            paypal,
            llm=llm,
            ledger=ledger,
            buyer_email=buyer_email or snapshot["buyer_email"],
            seller_email=seller_email or snapshot["seller_email"],
            judge=judge,
            dispute_window=dispute_window or timedelta(seconds=snapshot["dispute_window_s"]),
            auth_expiry_margin=auth_expiry_margin or timedelta(seconds=snapshot["auth_expiry_margin_s"]),
            registry=registry,
        )
        trustee._escrow = EscrowOrder(**snapshot["escrow"]) if snapshot["escrow"] else None
        trustee.authorized_at = _parse(snapshot["authorized_at"])
        trustee.dispute_deadline = _parse(snapshot["dispute_deadline"])
        trustee._ruling = Ruling(**snapshot["ruling"]) if snapshot["ruling"] else None
        trustee._last_delivery = Delivery(**snapshot["last_delivery"]) if snapshot["last_delivery"] else None
        trustee._capture_block = snapshot.get("capture_block")
        trustee.needs_attention = snapshot.get("needs_attention")
        return trustee

    def _raise_escrow(self) -> EscrowOrder:
        if self._escrow is None:
            raise IllegalTransition(self.deal.deal_id, self.deal.state, DealState.DELIVERED)
        return self._escrow

    def _arbitrate(self, attest: Attest, contract: Contract) -> tuple[str, Ruling]:
        """Delegate to the judge when one is set; otherwise apply the
        Trustee's built-in deterministic policy. Returns (decision, ruling).
        """
        delivery = self._last_delivery or Delivery(
            sender="trustee",
            deal_id=contract.deal_id,
            deliverable_ref=contract.good.deliverable_ref,
            content_hash="",
        )
        if self.judge is not None:
            ruling = self.judge.rule(contract, delivery, attest)
            return ruling.decision, ruling
        # Built-in policy: objective evidence of breach -> release, else uphold.
        evidence = attest.evidence
        breached = evidence.get("sha256_check") in ("mismatch", "unknown_ref") or "failed" in evidence.get(
            "checker", ""
        )
        return (
            (
                "release",
                Ruling("release", "evidence clause", attest.reason[:160], objective=True),
            )
            if breached
            else (
                "uphold",
                Ruling("uphold", "evidence clause", attest.reason[:160], objective=True),
            )
        )

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
                            'Respond with JSON: {"explanation": str}'
                        ),
                    }
                ],
            )
            self._log("note", {"op": op, "explanation": text.get("explanation", fact)})
        except Exception as exc:  # noqa: BLE001 - narration must never block money path
            log.warning("explanation LLM failed: %s", exc)
            self._log("note", {"op": op, "explanation": fact, "narration_failed": str(exc)[:100]})


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _parse(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def ruling_note(action: str, note: str) -> str:
    return note if note else ("funds released" if action == "release" else "funds upheld")
