import { useState } from "react";
import { ArrowRight, Check, Copy } from "lucide-react";
import type { Health, Row, Snapshot } from "./api";
import { deriveMoney, type MoneyPhase } from "./derive";
import { money } from "./describe";

const POS: Record<MoneyPhase, number> = {
  idle: 0,
  agreed: 0,
  approving: 0,
  blocked: 0,
  refunded: 0,
  held: 1,
  settled: 2,
};

function CopyId({ label, value }: { label: string; value: string }) {
  const [done, setDone] = useState(false);
  if (!value) return null;
  const copy = () => {
    void navigator.clipboard?.writeText(value).then(() => {
      setDone(true);
      setTimeout(() => setDone(false), 1400);
    });
  };
  return (
    <span className="idchip">
      <span className="idk">{label}</span>
      <code title={value}>{value.length > 18 ? `${value.slice(0, 8)}…${value.slice(-6)}` : value}</code>
      <button type="button" className="icon" onClick={copy} aria-label={`Copy ${label} ${value}`}>
        {done ? <Check size={13} aria-hidden="true" /> : <Copy size={13} aria-hidden="true" />}
      </button>
    </span>
  );
}

interface Props {
  rows: Row[];
  snap: Snapshot | null;
  running: boolean;
  health: Health | null;
}

export function MoneyPanel({ rows, snap, running, health }: Props) {
  const m = deriveMoney(rows, snap, running);
  const amt = m.amount ? money(m.amount) : "";
  const pos = POS[m.phase];

  const buyer: Record<MoneyPhase, string> = {
    idle: "negotiating",
    agreed: amt ? `will pay ${amt}` : "",
    approving: "approve in PayPal",
    held: `${amt} committed`,
    settled: `paid ${amt}`,
    refunded: `refunded ${amt}`,
    blocked: "no money moved",
  };
  const escrow: Record<MoneyPhase, string> = {
    idle: "empty",
    agreed: "empty",
    approving: "waiting for approval",
    held: `${amt} on hold`,
    settled: "released to seller",
    refunded: "hold voided",
    blocked: "never opened",
  };
  const seller: Record<MoneyPhase, string> = {
    idle: "awaiting a deal",
    agreed: "awaiting escrow",
    approving: "awaiting escrow",
    held: "paid on verified delivery",
    settled: `received ${amt}`,
    refunded: "not paid",
    blocked: "not paid",
  };

  const env = !health
    ? "Checking PayPal mode…"
    : health.paypal === "sandbox"
      ? "PayPal sandbox: test accounts, not real money"
      : "Dry-run: no PayPal call is made";

  const slot = (i: number, name: string, role: string, line: string) => (
    <div className={`slot ${pos === i ? "has" : ""} ${i === 1 ? "mid" : ""}`}>
      <div className="slot-role">{role}</div>
      <div className="slot-name">{name}</div>
      <div className="slot-line">{line}</div>
    </div>
  );

  return (
    <div className={`money phase-${m.phase}`} aria-label="where the money is">
      <div className="slots">
        {slot(0, "Northwind Outfitters", "Buyer", buyer[m.phase])}
        {slot(1, "Held at PayPal", "Escrow", escrow[m.phase])}
        {slot(2, "Tidewater Data Co.", "Seller", seller[m.phase])}
      </div>
      <div className="rail" aria-hidden="true">
        <span className="rail-line" />
        <span className="coin" style={{ left: `${[16.66, 50, 83.33][pos]}%` }}>
          {amt || "$—"}
        </span>
      </div>
      <p className="sr-only" aria-live="polite">
        {amt ? `${amt}: ` : ""}
        {m.phase === "held" ? "held at PayPal" : m.phase === "settled" ? "paid to seller" : m.phase === "refunded" ? "refunded to buyer" : m.phase}
      </p>
      <div className="money-foot">
        <span className="env">
          <ArrowRight size={12} aria-hidden="true" /> {env}
        </span>
        <CopyId label="order" value={m.orderId} />
        <CopyId label="authorization" value={m.authId} />
      </div>
    </div>
  );
}
