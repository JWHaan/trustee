import type { Row, Snapshot } from "./api";

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

  const stages: Stage[] = [
    {
      key: "agree",
      label: "negotiate",
      status: has((r) => r.kind === "msg" && r.event === "offer") ? "done" : "todo",
      detail: snap?.turns ? `${snap.turns} moves` : "",
    },
    {
      key: "contract",
      label: "contract",
      status: has((r) => r.kind === "msg" && r.event === "contract_formed") ? "done" : "todo",
      detail: snap?.price ? `$${Number(snap.price).toFixed(2)}` : "",
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
      status: money("authorize") ? "done" : "todo",
      detail: escrow?.order_id ? escrow.order_id.slice(0, 14) : "",
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
  if (!running && terminal) {
    stages.forEach((s) => {
      if (s.status === "todo") s.status = "done";
    });
  }
  return stages;
}

export function TrustStrip({ rows, snap, running }: Props) {
  const stages = build(rows, snap, running);
  return (
    <div className="stepper" aria-label="money path">
      {stages.map((s) => (
        <div key={s.key} className={`step ${s.status}`} title={s.detail}>
          <span className="dot" />
          <span className="label">{s.label}</span>
          <span className="detail">{s.detail}</span>
        </div>
      ))}
    </div>
  );
}
