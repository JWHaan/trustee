import { useEffect, useState } from "react";
import { fetchHealth, type Health, type Row, type Snapshot } from "./api";
import { pendingApproval } from "./describe";

type Status = "todo" | "active" | "done" | "failed";

interface Stage {
  key: string;
  label: string;
  status: Status;
  detail: string;
}

interface Props {
  rows: Row[];
  snap: Snapshot | null;
  running: boolean;
}

function build(rows: Row[], snap: Snapshot | null, running: boolean): Stage[] {
  const has = (pred: (r: Row) => boolean) => rows.some(pred);
  const money = (op: string) => has((r) => r.kind === "paypal" && r.paypal_op === op);
  const stateTo = (s: string) => has((r) => r.kind === "state" && r.state_to === s);

  const mandate = rows.find((r) => r.kind === "mandate");
  const mandateOk = mandate ? mandate.note.toLowerCase().includes("ok") : null;
  const escrow = rows.find((r) => r.kind === "paypal" && r.paypal_op === "authorize");
  const delivery = rows.find((r) => r.kind === "delivery");
  const attest =
    rows.find((r) => r.kind === "attest") ??
    rows.find((r) => r.kind === "msg" && r.event === "attestation");

  const terminal = snap?.state === "SETTLED" || snap?.state === "VOIDED";
  const forged = snap?.outcome === "rejected_identity";
  const waiting = pendingApproval(rows, running) !== null;

  const stages: Stage[] = [
    {
      key: "agree",
      label: "negotiate",
      status: has((r) => r.kind === "msg" && (r.event === "offer" || r.event === "agreed")) ? "done" : "todo",
      detail: snap?.turns ? `${snap.turns} moves` : "",
    },
    {
      key: "contract",
      label: "contract",
      status: forged ? "failed" : has((r) => r.kind === "msg" && r.event === "contract_formed") ? "done" : "todo",
      detail: forged ? "forged signature" : snap?.price ? `$${Number(snap.price).toFixed(2)}` : "",
    },
    {
      key: "mandate",
      label: "mandate",
      status: mandate ? (mandateOk ? "done" : "failed") : "todo",
      detail: mandate ? (mandateOk ? "both parties" : "blocked") : "",
    },
    {
      key: "escrow",
      label: "escrow",
      status: waiting ? "active" : money("authorize") && !pendingApprovalOpen(rows) ? "done" : "todo",
      detail: waiting ? "awaiting approval" : escrow?.order_id ? escrow.order_id.slice(0, 14) : "",
    },
    {
      key: "deliver",
      label: "delivery",
      status: delivery ? "done" : stateTo("DELIVERED") ? "done" : "todo",
      detail: delivery?.sha ? `${delivery.sha.slice(0, 8)}…` : "",
    },
    {
      key: "attest",
      label: "attest",
      status: attest ? "done" : "todo",
      detail: attest ? (attest.verdict || (attest.note.includes("does not match") ? "reject" : "accept")) : "",
    },
    {
      key: "settle",
      label: snap?.state === "VOIDED" ? "refund" : "settle",
      status: snap?.state === "SETTLED" ? "done" : snap?.state === "VOIDED" ? "failed" : "todo",
      detail: snap?.state === "SETTLED" || snap?.state === "VOIDED" ? snap.state : "",
    },
  ];

  // The first stage still open is the one the run is standing on.
  if (running) {
    const next = stages.find((s) => s.status === "todo");
    if (next) next.status = "active";
  }
  if (!running && terminal && !snap?.outcome?.startsWith("approval_")) {
    stages.forEach((s) => {
      if (s.status === "todo") s.status = "done";
    });
  }
  return stages;
}

/** The Trustee's authorize row exists as soon as the order does; with approval it holds nothing yet. */
function pendingApprovalOpen(rows: Row[]): boolean {
  return rows.some((r) => r.kind === "run" && r.event === "awaiting_approval") && !rows.some((r) => r.kind === "state" && (r.state_to === "ESCROWED" || r.state_to === "VOIDED"));
}

function signatures(rows: Row[]): { text: string; tone: "ok" | "bad" | "idle" } {
  const ids = rows.filter((r) => r.kind === "identity");
  const bad = ids.filter((r) => r.check === "rejected").length;
  const ok = ids.filter((r) => r.check === "verified").length;
  if (bad) return { text: "forged — rejected", tone: "bad" };
  if (ok) return { text: `${ok}/4 verified`, tone: "ok" };
  return { text: "—", tone: "idle" };
}

export function TrustStrip({ rows, snap, running }: Props) {
  const stages = build(rows, snap, running);
  const sig = signatures(rows);
  const [health, setHealth] = useState<Health | null>(null);
  useEffect(() => {
    fetchHealth()
      .then(setHealth)
      .catch(() => setHealth(null));
  }, []);

  return (
    <div className="stepper" aria-label="money path">
      {stages.map((s) => (
        <div key={s.key} className={`step ${s.status}`} title={s.detail}>
          <span className="dot" />
          <span className="label">{s.label}</span>
          <span className="detail">{s.detail}</span>
        </div>
      ))}
      <div className="strip-chips">
        <span className={`chip-static sig ${sig.tone}`} title="Ed25519 signatures the Trustee checked">
          signatures: {sig.text}
        </span>
        {health ? (
          <span className={`chip-static paypal ${health.paypal}`} title="PayPal mode of this server">
            PayPal: {health.paypal}
          </span>
        ) : null}
      </div>
    </div>
  );
}
