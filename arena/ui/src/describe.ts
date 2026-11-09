import type { Row } from "./api";

const ACTOR_LABEL: Record<string, string> = {
  "buyer-agent-1": "buyer",
  "seller-agent-1": "seller",
  trustee: "trustee",
  "arbiter-agent-1": "arbiter",
  arena: "arena",
};

// Outside agents get a per-run suffix (northwind-planner-1a2b3c4d), so they match on prefix.
const ACTOR_PREFIX: [string, string][] = [
  ["northwind-planner", "northwind"],
  ["tidewater-sales", "tidewater"],
];

export function actorName(actor: string): string {
  const exact = ACTOR_LABEL[actor];
  if (exact) return exact;
  return ACTOR_PREFIX.find(([prefix]) => actor.startsWith(prefix))?.[1] ?? actor;
}

function title(name: string): string {
  return name.charAt(0).toUpperCase() + name.slice(1);
}

/** Only an https link is ever rendered as an anchor. */
export function safeLink(url: string): string | null {
  return /^https:\/\//i.test(url) ? url : null;
}

/**
 * The run is standing at PayPal when its latest approval row has no later money or state row:
 * once the payer approves (authorize) or the wait ends (VOIDED) the tape moves on.
 */
export function pendingApproval(rows: Row[], running: boolean): Row | null {
  if (!running) return null;
  let at = -1;
  rows.forEach((r, i) => {
    if (r.kind === "run" && r.event === "awaiting_approval" && r.link) at = i;
  });
  if (at < 0) return null;
  return rows.slice(at + 1).some((r) => r.kind === "paypal" || r.kind === "state") ? null : rows[at];
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
    if (row.event === "run_finished") return { headline: "run finished", detail: note };
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

  if (row.kind === "identity") {
    const who = actorName(row.signer || row.actor);
    return row.check === "rejected"
      ? { headline: `signature rejected: ${who}`, detail: note }
      : { headline: `signature verified: ${who}`, detail: note };
  }

  if (row.kind === "a2a") {
    // event is "<peer> <method> <skill>"; the actor is the caller. "Northwind → Tidewater · negotiate: counter $168"
    const [peer = "", method = "", skill = ""] = row.event.split(" ");
    const subject = method === "SendMessage" || method === "push" ? skill : method;
    const what = row.note ? `${subject}: ${row.note.toLowerCase().replace(/_/g, " ")}` : subject;
    return { headline: `${title(actorName(row.actor))} → ${title(peer)} · ${what}${price ? ` ${price}` : ""}`, detail: "", price };
  }

  if (row.kind === "api") {
    const status = note.split(" ")[0];
    return { headline: `${row.event} → ${status}`, detail: note.slice(status.length).trim() };
  }

  if (row.kind === "mandate") {
    const ok = note.toLowerCase().includes("ok");
    return { headline: ok ? "mandate check passed" : "mandate check failed", detail: note };
  }

  if (row.kind === "paypal") {
    const op = row.paypal_op || row.event;
    const label =
      op === "authorize" ? "funds held" : op === "capture" ? "captured to seller" : op === "void" ? "hold released" : op;
    return { headline: `paypal ${op}: ${label}`, detail: note || (row.order_id ? `order ${row.order_id}` : "") };
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
    return { headline: `attestation on record: ${row.verdict}`, detail: note };
  }

  if (row.kind === "policy") {
    if (row.rule) return { headline: `${row.rule}: ${row.result}`, detail: note };
    switch (row.event) {
      case "intent_consent":
        return { headline: "one-time consent", detail: note };
      case "intent_limits":
        return { headline: "limits signed by the human", detail: note };
      case "funding_vaulted":
        return { headline: "payment method vaulted", detail: note };
      case "policy_decision":
        return {
          headline:
            row.result === "autonomous"
              ? "paying under the intent mandate, no human click"
              : row.result === "step_up"
                ? "outside the instruction: a human must approve"
                : "refused by the intent mandate",
          detail: note,
        };
      default:
        return { headline: row.event.replace(/_/g, " "), detail: note };
    }
  }

  if (row.kind === "receipt") {
    const how = row.authority === "intent_mandate" ? "authorized by the human's signed limits" : "approved by a person";
    return { headline: "signed receipt issued", detail: `${how} · ${note}` };
  }

  if (row.kind === "ruling") {
    const arbiter = row.actor.startsWith("arbiter");
    return arbiter
      ? { headline: `rules ${row.event.toUpperCase()}`, detail: note }
      : { headline: `applies the ruling: ${row.event}`, detail: note };
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
  if (row.kind === "policy" && row.rule !== "") return false; // the checklist panel shows each rule
  if (row.kind === "ruling" && !row.actor.startsWith("arbiter")) return false; // trustee applying it
  return true;
}
