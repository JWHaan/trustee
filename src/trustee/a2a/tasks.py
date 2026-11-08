"""Deal -> A2A Task: how an escrow looks from outside as a unit of work.

A task is a *view* of the durable deal, rebuilt from the deal's snapshot on every read. Nothing about a
task is stored, so a restart cannot lose one and a task can never disagree with the money path.

State mapping (the deal state alone is not enough; the PayPal order status disambiguates two cases):

* CONTRACTED, order created, payer has not approved -> AUTH_REQUIRED. The payer must approve at PayPal; the
  link is in the status message.
* CONTRACTED, no order (the PayPal call failed) -> FAILED. Nothing is held; sending `open-escrow` again
  without a taskId retries safely.
* ESCROWED -> INPUT_REQUIRED. Funds are held and the seller must deliver.
* DELIVERED -> WORKING. The buyer is verifying; the next move is its signed attestation.
* DISPUTED -> WORKING. An arbiter, the dispute window or an operator decides next, and no client message
  can change that.
* SETTLED -> COMPLETED with a settlement-receipt artifact. The funds were captured to the seller.
* VOIDED after funds were held -> COMPLETED with a settlement-receipt artifact. The buyer was refunded,
  which is the escrow doing its job.
* VOIDED with nothing ever held -> CANCELED. The payer cancelled, or the deadline passed before approval.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ..protocol import Contract
from ..summary import summarize
from .models import (
    Artifact,
    Message,
    Role,
    Task,
    TaskState,
    TaskStatus,
    data_part,
    new_id,
    now_iso,
    text_part,
    to_iso,
)

RECEIPT_ARTIFACT_ID = "settlement-receipt"
METADATA_KEY = "trustee"


def a2a_state(snapshot: dict[str, Any]) -> TaskState:
    state = snapshot["state"]
    escrow = snapshot.get("escrow") or {}
    status = escrow.get("status")
    if state == "CONTRACTED":
        if escrow and status == "created":
            return TaskState.AUTH_REQUIRED
        if not escrow:
            return TaskState.FAILED
        return TaskState.WORKING
    if state == "ESCROWED":
        return TaskState.INPUT_REQUIRED
    if state in ("DELIVERED", "DISPUTED"):
        return TaskState.WORKING
    if state == "SETTLED":
        return TaskState.COMPLETED
    if state == "VOIDED":
        return TaskState.COMPLETED if status == "voided" else TaskState.CANCELED
    return TaskState.SUBMITTED  # DRAFT / NEGOTIATING: a deal that has not formed yet


def status_timestamp(updated_at: str | None) -> str:
    """The store's UTC write time as a spec timestamp; `now` when the deal has not been persisted yet."""
    if updated_at:
        try:
            moment = datetime.fromisoformat(updated_at)
        except ValueError:
            return now_iso()
        return to_iso(moment if moment.tzinfo else moment.replace(tzinfo=UTC))
    return now_iso()


def outcome(snapshot: dict[str, Any]) -> str | None:
    """settled | refunded for a finished escrow, None otherwise."""
    if snapshot["state"] == "SETTLED":
        return "settled"
    if snapshot["state"] == "VOIDED" and (snapshot.get("escrow") or {}).get("status") == "voided":
        return "refunded"
    return None


def receipt_data(
    snapshot: dict[str, Any], contract: dict[str, Any], record_receipt: dict[str, Any] | None = None
) -> dict[str, Any]:
    """The facts a counterparty needs to book the result. Money and ids come from the deal, never from a model."""
    escrow = snapshot.get("escrow") or {}
    details = escrow.get("details") or {}
    ruling = snapshot.get("ruling")
    history = snapshot.get("history") or []
    receipt: dict[str, Any] = {
        "dealId": contract["deal_id"],
        "outcome": outcome(snapshot),
        "amount": contract["price"],
        "currency": contract["currency"],
        "buyer": contract["buyer"],
        "seller": contract["seller"],
        "contractDigest": Contract.model_validate(contract).digest(),
        "orderId": escrow.get("order_id"),
        "authorizationId": escrow.get("authorization_id"),
        "closedBy": history[-1][0] if history else None,
        "ruling": None
        if ruling is None
        else {"decision": ruling["decision"], "clause": ruling["clause"], "reasoning": ruling["reasoning"]},
    }
    signed = record_receipt.get("signed") if record_receipt else None
    if signed:
        # The same Ed25519-signed document as GET /v1/deals/{id}/receipt, so a counterparty can verify it offline.
        receipt["humanPresent"] = signed.get("human_present")
        if signed.get("intent_mandate_digest"):
            receipt["intentMandateDigest"] = signed["intent_mandate_digest"]
        receipt["signedReceipt"] = signed
    capture_id = details.get("capture_id") or (details.get("id") if snapshot["state"] == "SETTLED" else None)
    if capture_id:
        receipt["captureId"] = capture_id
    settlement = snapshot.get("settlement")
    if settlement and snapshot["state"] == "SETTLED":
        receipt["amount"] = settlement["captured"]  # what actually moved
        receipt["contractAmount"] = contract["price"]
        receipt["released"] = settlement["released"]
        receipt["settlement"] = settlement
    if details.get("void_reason") is not None and outcome(snapshot) == "refunded":
        receipt["voidReason"] = details["void_reason"]
    return {k: v for k, v in receipt.items() if v is not None}


def _status_text(state: TaskState, snapshot: dict[str, Any], contract: dict[str, Any]) -> str:
    amount = f"{contract['price']} {contract['currency']}"
    escrow = snapshot.get("escrow") or {}
    deal = snapshot["state"]
    if state == TaskState.AUTH_REQUIRED:
        return (
            f"Payer approval required: no funds are held yet. The payer must approve {amount} at PayPal: "
            f"{escrow.get('approve_url')}. The task moves on by itself once PayPal reports the approval."
        )
    if state == TaskState.FAILED:
        return (
            "PayPal could not open the hold, so nothing is held and nothing was charged. This is safe to retry: "
            "send the same open-escrow message again without a taskId."
        )
    if state == TaskState.INPUT_REQUIRED:
        return (
            f"Funds are held ({amount}). Seller {contract['seller']}: deliver {contract['good']['deliverable_ref']} "
            "and send skill record-delivery with a signed Delivery."
        )
    if state == TaskState.WORKING and deal == "DELIVERED":
        return (
            f"Delivery recorded. Buyer {contract['buyer']}: verify it against the contract sha256 and send skill "
            "attest-delivery with a signed accept or reject. Funds stay held until then."
        )
    if state == TaskState.WORKING and deal == "DISPUTED":
        ruling = snapshot.get("ruling") or {}
        clause = f" Clause: {ruling['clause']}." if ruling.get("clause") else ""
        deadline = snapshot.get("dispute_deadline")
        tail = f" Unless an operator decides first, the hold resolves after {deadline}." if deadline else ""
        return f"The buyer rejected the delivery and the dispute is open. Funds stay held.{clause}{tail}"
    if state == TaskState.COMPLETED:
        if deal == "SETTLED":
            part = snapshot.get("settlement")
            if part:
                cur = contract["currency"]
                return (
                    f"Settled pro-rata: {part['captured']} {cur} of {part['price']} {cur} "
                    f"({part['units_valid']} of {part['units_total']} {part['basis']} valid) was captured to seller "
                    f"{contract['seller']}; {part['released']} {cur} went back to buyer {contract['buyer']}."
                )
            return f"Settled: {amount} was captured to seller {contract['seller']}."
        return f"Refunded: the hold on {amount} was released; buyer {contract['buyer']} keeps the funds."
    if state == TaskState.CANCELED:
        return "Canceled before any funds were held. Nothing was charged."
    return f"Deal is {deal}."


def _summary(snapshot: dict[str, Any], contract: dict[str, Any]) -> dict[str, Any]:
    """The same plain-language summary the REST deal view and the arena carry (trustee.summary)."""
    escrow = snapshot.get("escrow") or {}
    authority = snapshot.get("autonomy") or {}
    return summarize(
        state=snapshot["state"],
        price=contract.get("price"),
        currency=contract.get("currency") or "USD",
        escrow_status=escrow.get("status"),
        ruling=snapshot.get("ruling"),
        settlement=snapshot.get("settlement"),
        needs_attention=snapshot.get("needs_attention"),
        dispute_deadline=snapshot.get("dispute_deadline"),
        human_present=authority.get("human_present") if authority else None,
        authority_outcome=authority.get("outcome") if authority else None,
    )


def deal_task(record: dict[str, Any], *, include_artifacts: bool = True) -> Task:
    """Build the Task for one deal from `DealService.record` (or an equivalent store row)."""
    snapshot, contract = record["snapshot"], record["contract"]
    deal_id = contract["deal_id"]
    state = a2a_state(snapshot)
    stamp = status_timestamp(record.get("updated_at"))
    message = Message(
        messageId=f"status-{deal_id}-{len(snapshot.get('history') or [])}",
        role=Role.AGENT,
        parts=[
            text_part(_status_text(state, snapshot, contract)),
            data_part(
                {"dealState": snapshot["state"], "a2aState": state.value, "summary": _summary(snapshot, contract)}
            ),
        ],
        contextId=deal_id,
        taskId=deal_id,
    )
    escrow = snapshot.get("escrow") or {}
    meta: dict[str, Any] = {
        "dealId": deal_id,
        "dealState": snapshot["state"],
        "buyer": contract["buyer"],
        "seller": contract["seller"],
        "amount": contract["price"],
        "currency": contract["currency"],
        "contractDigest": Contract.model_validate(contract).digest(),
        "escrowStatus": escrow.get("status"),
        "orderId": escrow.get("order_id"),
        "authorizationId": escrow.get("authorization_id"),
    }
    if state == TaskState.AUTH_REQUIRED:
        meta["approveUrl"] = escrow.get("approve_url")
    if state == TaskState.FAILED:
        meta["retryable"] = True
    if snapshot.get("needs_attention"):
        meta["needsAttention"] = snapshot["needs_attention"]
    if snapshot.get("dispute_deadline"):
        meta["disputeDeadline"] = snapshot["dispute_deadline"]
    if snapshot.get("settlement"):
        meta["settlement"] = snapshot["settlement"]
    task = Task(
        id=deal_id,
        contextId=deal_id,
        status=TaskStatus(state=state, message=message, timestamp=stamp),
        metadata={METADATA_KEY: {k: v for k, v in meta.items() if v is not None}},
    )
    if include_artifacts and outcome(snapshot) is not None:
        task.artifacts = [
            Artifact(
                artifactId=RECEIPT_ARTIFACT_ID,
                name="settlement-receipt",
                description="What happened to the money, with the ids to reconcile it.",
                parts=[data_part(receipt_data(snapshot, contract, {"signed": record.get("receipt")}))],
            )
        ]
    # Trustee keeps no per-message conversation, so `history` is never returned (any historyLength is honored
    # trivially). The deal's own transition log is at GET /v1/deals/{id}/events.
    return task


def annotate_refusal(task: Task, status: int, detail: str, extra: dict[str, Any] | None = None) -> Task:
    """Mark that the *last message sent to this task* was refused, without changing the task's true state.

    A refused delivery leaves the deal ESCROWED: the task is still waiting for a valid delivery. Reporting
    REJECTED would tell the client the whole deal is dead. The refusal travels in status.message and in
    metadata.trustee.lastRequest instead."""
    text = f"Your last message was refused ({status}): {detail}"
    parts = [text_part(text), data_part({"ok": False, "status": status, "detail": detail, **(extra or {})})]
    task.status.message = Message(
        messageId=new_id(),
        role=Role.AGENT,
        parts=parts,
        contextId=task.contextId,
        taskId=task.id,
    )
    meta = (task.metadata or {}).setdefault(METADATA_KEY, {})
    meta["lastRequest"] = {"ok": False, "status": status, "detail": detail, **(extra or {})}
    if status == 502:
        meta["retryable"] = True
    task.metadata = {**(task.metadata or {}), METADATA_KEY: meta}
    return task


def ephemeral_task(
    state: TaskState,
    text: str,
    *,
    context_id: str | None,
    task_id: str,
    data: dict[str, Any] | None = None,
    artifact_name: str | None = None,
) -> Task:
    """A task that is not backed by a deal: the result of a one-shot skill (registration, a refusal)."""
    message = Message(
        messageId=f"status-{task_id}",
        role=Role.AGENT,
        parts=[text_part(text), *([data_part(data)] if data is not None and artifact_name is None else [])],
        contextId=context_id,
        taskId=task_id,
    )
    task = Task(
        id=task_id,
        contextId=context_id,
        status=TaskStatus(state=state, message=message, timestamp=now_iso()),
    )
    if artifact_name is not None and data is not None:
        task.artifacts = [Artifact(artifactId=artifact_name, name=artifact_name, parts=[data_part(data)])]
    return task
