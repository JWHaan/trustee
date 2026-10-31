import type { Row, Snapshot } from "./api";
import { pendingApproval } from "./describe";

/**
 * Everything the timeline and the money panel show is computed here from the same ledger rows
 * the tape and the transcript read, so the four views cannot disagree about where a deal is.
 */

export type StepStatus = "todo" | "active" | "done" | "failed" | "refund" | "skipped";

export interface Step {
  key: string;
  label: string;
  status: StepStatus;
  detail: string;
  /** Why a skipped step was not reached; shown as a tooltip and read by screen readers. */
  reason?: string;
}

const isBuyerMsg = (r: Row, event: string) => r.kind === "msg" && r.event === event;

export function deriveTimeline(rows: Row[], snap: Snapshot | null, running: boolean): Step[] {
  const has = (pred: (r: Row) => boolean) => rows.some(pred);
  const contract = rows.find((r) => isBuyerMsg(r, "contract_formed"));
  const moves = rows.filter((r) => r.kind === "msg" && ["offer", "counter", "accept"].includes(r.event)).length;
  const agreedOffPlatform = has((r) => isBuyerMsg(r, "agreed"));

  const contractIds = rows.filter((r) => r.kind === "identity" && r.event === "verify_contract");
  const allIds = rows.filter((r) => r.kind === "identity");
  const rejection = allIds.find((r) => r.check === "rejected");
  const verified = allIds.filter((r) => r.check === "verified").length;

  const mandate = rows.find((r) => r.kind === "mandate");
  const mandateOk = mandate ? mandate.note.toLowerCase().includes("ok") : null;

  const authorize = rows.find((r) => r.kind === "paypal" && r.paypal_op === "authorize");
  const waiting = pendingApproval(rows, running) !== null;
  const delivery = rows.find((r) => r.kind === "delivery" && !r.actor.startsWith("trustee"));
  const delivered = delivery !== undefined || has((r) => r.kind === "state" && r.state_to === "DELIVERED");
  const attest =
    rows.find((r) => r.kind === "attest") ?? rows.find((r) => isBuyerMsg(r, "attestation"));
  const attestVerdict = (attest?.verdict || (attest?.note.includes("does not match") ? "reject" : attest ? "accept" : ""))
    .toLowerCase();

  const outcome = snap?.outcome ?? "";
  const noApproval = outcome === "approval_timeout" || outcome === "approval_cancelled" || outcome === "approval_failed";
  const settled = snap?.state === "SETTLED" || has((r) => r.kind === "state" && r.state_to === "SETTLED");
  const voided = snap?.state === "VOIDED" || has((r) => r.kind === "state" && r.state_to === "VOIDED");
  const forged = rejection !== undefined || outcome === "rejected_identity";

  const finalLabel = forged ? "Rejected" : settled ? "Settled" : voided ? "Refunded" : "Settled or refunded";
  const steps: Step[] = [
    {
      key: "negotiate",
      label: "Negotiate",
      status: moves > 0 || agreedOffPlatform || contract ? "done" : "todo",
      detail: moves > 0 ? `${moves} moves` : agreedOffPlatform || contract ? "agreed off-platform" : "",
    },
    {
      key: "contract",
      label: "Contract signed",
      status: contract ? "done" : "todo",
      detail: contract?.price ? `$${Number(contract.price).toFixed(2)}` : "",
    },
    {
      key: "identity",
      label: "Identity verified",
      status: forged ? "failed" : contractIds.filter((r) => r.check === "verified").length >= 2 ? "done" : "todo",
      detail: forged ? "forged signature" : verified ? `${verified} signatures` : "",
    },
    {
      key: "mandate",
      label: "Mandates checked",
      status: mandate ? (mandateOk ? "done" : "failed") : "todo",
      detail: mandate ? (mandateOk ? "both within limits" : "blocked") : "",
    },
    {
      key: "funds",
      label: "Funds held",
      status: noApproval ? "failed" : authorize && !waiting ? "done" : waiting ? "active" : "todo",
      detail: noApproval ? "buyer never approved" : waiting ? "awaiting approval" : authorize ? "at PayPal" : "",
    },
    {
      key: "delivered",
      label: "Delivered",
      status: delivered ? "done" : "todo",
      detail: delivery?.sha ? `sha ${delivery.sha.slice(0, 8)}` : "",
    },
    {
      key: "verified",
      label: "Verified",
      status: attest ? (attestVerdict === "reject" ? "failed" : "done") : "todo",
      detail: attest ? (attestVerdict === "reject" ? "check failed" : "check passed") : "",
    },
    {
      key: "final",
      label: finalLabel,
      status: forged ? "failed" : settled ? "done" : voided ? "refund" : "todo",
      detail: forged ? "no money moved" : settled ? "to seller" : voided ? "to buyer" : "",
    },
  ];

  const stopped = forged || mandateOk === false || noApproval;
  const finished = !running && snap !== null;
  const reason = forged
    ? "not reached: rejected before PayPal"
    : noApproval
      ? "not reached: buyer never approved"
      : mandateOk === false
        ? "not reached: mandate check failed"
        : "not reached";

  if (stopped || finished) {
    let blocked = stopped && !finished ? steps.findIndex((s) => s.status === "failed") : -1;
    if (finished) blocked = steps.findIndex((s) => s.status === "failed" && s.key !== "verified");
    steps.forEach((s, i) => {
      if (s.status === "todo" && (finished || (blocked >= 0 && i > blocked))) {
        s.status = "skipped";
        s.detail = "not reached";
        s.reason = reason;
      }
    });
    if (forged || noApproval) {
      // The failed step is the cause; the final step restates the result, not another failure.
      const last = steps[steps.length - 1];
      last.label = forged ? "Rejected" : "Voided";
      last.status = forged ? "failed" : "refund";
      last.detail = forged ? "no money moved" : "nothing charged";
    }
  } else if (running) {
    const next = steps.find((s) => s.status === "todo");
    if (next) next.status = "active";
  }
  return steps;
}

export type MoneyPhase = "idle" | "agreed" | "approving" | "held" | "settled" | "refunded" | "blocked";

export interface Money {
  phase: MoneyPhase;
  amount: string;
  orderId: string;
  authId: string;
}

export function deriveMoney(rows: Row[], snap: Snapshot | null, running: boolean): Money {
  const contract = rows.find((r) => isBuyerMsg(r, "contract_formed"));
  const amount = contract?.price || snap?.price || "";
  const orderId = rows.find((r) => r.order_id)?.order_id ?? snap?.escrow?.order_id ?? "";
  const authId = snap?.escrow?.authorization_id ?? "";
  const paypal = (op: string) => rows.some((r) => r.kind === "paypal" && r.paypal_op === op);
  const state = (s: string) => rows.some((r) => r.kind === "state" && r.state_to === s);

  let phase: MoneyPhase = "idle";
  if (contract) phase = "agreed";
  if (pendingApproval(rows, running)) phase = "approving";
  else if (paypal("authorize") || state("ESCROWED")) phase = "held";
  if (paypal("capture") || state("SETTLED")) phase = "settled";
  else if (paypal("void") || state("VOIDED")) phase = "refunded";
  if (rows.some((r) => r.kind === "identity" && r.check === "rejected") || snap?.outcome === "rejected_identity") {
    phase = "blocked";
  }
  return { phase, amount, orderId, authId };
}

/** Which side of the conversation an actor sits on. Trustee, arbiter and arena are neutral. */
export function side(actor: string): "buyer" | "seller" | "system" {
  if (actor.startsWith("buyer") || actor.startsWith("northwind")) return "buyer";
  if (actor.startsWith("seller") || actor.startsWith("tidewater")) return "seller";
  return "system";
}
