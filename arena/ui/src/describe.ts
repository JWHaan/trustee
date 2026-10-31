import type { Row } from "./api";

const ACTOR_LABEL: Record<string, string> = {
  "buyer-agent-1": "buyer",
  "seller-agent-1": "seller",
  trustee: "trustee",
  "arbiter-agent-1": "arbiter",
  arena: "arena",
};

export function actorName(actor: string): string {
  return ACTOR_LABEL[actor] ?? actor;
}

export function money(value: string): string {
  const n = Number(value);
  return Number.isFinite(n) && value !== "" ? `$${n.toFixed(2)}` : "";
}

interface Line {
  headline: string;
  detail: string;
  price?: string;
}

/**
 * One ledger row -> one readable sentence. The grid is the audit trail; this
 * is the story. Both read the same rows, so they can never disagree.
 */
export function describe(row: Row): Line {
  const price = money(row.price);
  const note = row.note;

  if (row.kind === "run") {
    if (row.event === "run_started") return { headline: "run started", detail: note };
    if (row.event === "run_finished") return { headline: `run finished — ${row.price ? money(row.price) : ""}`, detail: note };
    if (row.event === "cap_reached") return { headline: "move cap reached", detail: note, price };
    return { headline: row.event.replace(/_/g, " "), detail: note };
  }

  if (row.kind === "state") {
    return { headline: `${row.state_from} → ${row.state_to}`, detail: note };
  }

  if (row.kind === "msg") {
    switch (row.event) {
      case "offer":
        return { headline: `offers ${price}`, detail: note, price };
      case "counter":
        return { headline: `counters ${price}`, detail: note, price };
      case "accept":
        return { headline: `accepts ${price}`, detail: note, price };
      case "contract_formed":
        return { headline: "contract formed", detail: note || `${price} · both parties bound` };
      case "attestation":
        return { headline: `attests ${row.verdict.toUpperCase()}`, detail: note };
      case "delivery":
        return { headline: "delivery recorded", detail: `sha256 ${row.sha}…` };
      default:
        return { headline: row.event || "message", detail: note };
    }
  }

  if (row.kind === "mandate") {
    const ok = note.toLowerCase().includes("ok");
    return { headline: ok ? "mandate check passed" : "mandate check failed", detail: note };
  }

  if (row.kind === "paypal") {
    const op = row.paypal_op || row.event;
    const label =
      op === "authorize" ? "funds held" : op === "capture" ? "captured to seller" : op === "void" ? "hold released" : op;
    return { headline: `paypal ${op} — ${label}`, detail: note || (row.order_id ? `order ${row.order_id}` : "") };
  }

  if (row.kind === "delivery") {
    // Two actors log a delivery: the seller ships, the trustee records. Same
    // kind, different meaning — say which.
    return row.actor.startsWith("seller")
      ? { headline: "ships the deliverable", detail: note }
      : { headline: "delivery recorded", detail: row.sha ? `sha256 ${row.sha}… written to the ledger` : note };
  }

  if (row.kind === "attest") {
    // The trustee's copy of the buyer's verdict. The buyer's own words are the
    // msg/attestation row above it, so this one states receipt, not opinion.
    return { headline: `attestation on record — ${row.verdict}`, detail: note };
  }

  if (row.kind === "ruling") {
    const arbiter = row.actor.startsWith("arbiter");
    return arbiter
      ? { headline: `rules ${row.event.toUpperCase()}`, detail: note }
      : { headline: `applies the ruling — ${row.event.toUpperCase()}`, detail: note };
  }

  return { headline: row.event || row.kind, detail: note };
}

/**
 * The transcript is the story; the grid is the audit trail. Both read the same
 * rows, so the only difference is what gets *repeated*: the trustee's copies of
 * the buyer's verdict and the arbiter's ruling are on the tape but they are not
 * new beats, and repeating them makes one decision look like four.
 */
export function isNarrative(row: Row): boolean {
  if (row.kind === "note") return false;
  if (row.kind === "msg" && row.event === "") return false;
  if (row.kind === "attest") return false; // trustee's receipt of the buyer's verdict
  if (row.kind === "ruling" && !row.actor.startsWith("arbiter")) return false; // trustee applying it
  return true;
}
