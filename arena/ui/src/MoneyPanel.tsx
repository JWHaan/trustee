import { useState } from "react";
import { Check, Copy } from "lucide-react";
import type { Row, Snapshot } from "./api";
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
      <button type="button" className="icon" onClick={copy} aria-label={`Copy ${label} ID`}>
        {done ? <Check size={13} aria-hidden="true" /> : <Copy size={13} aria-hidden="true" />}
      </button>
    </span>
  );
}

interface Props {
  rows: Row[];
  snap: Snapshot | null;
  running: boolean;
  /** The selected scenario in one line, so the rail says who is buying what. */
  story: { title: string; blurb: string } | null;
}

export function MoneyPanel({ rows, snap, running, story }: Props) {
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

  const parties: [string, string, string][] = [
    ["Buyer agent", "Northwind Outfitters", buyer[m.phase]],
    ["Escrow", "Held at PayPal", escrow[m.phase]],
    ["Seller agent", "Tidewater Data Co.", seller[m.phase]],
  ];

  return (
    <div className={`money phase-${m.phase}`}>
      {story ? (
        <p className="deal-story">
          <b>{story.title}.</b> {story.blurb}
        </p>
      ) : null}
      <div className="parties names">
        {parties.map(([role, name], i) => (
          <div key={role} className={`party ${pos === i ? "has" : ""}`}>
            <span className="party-role">{role}</span>
            <span className="party-name">{name}</span>
          </div>
        ))}
      </div>
      <div className="rail" aria-hidden="true">
        <span className="rail-line" />
        {[0, 1, 2].map((i) => (
          <span key={i} className={`rail-node ${pos === i ? "has" : ""}`} style={{ left: `${[16.66, 50, 83.33][i]}%` }} />
        ))}
        <span className="coin" style={{ left: `${[16.66, 50, 83.33][pos]}%` }}>
          {amt || "$—"}
        </span>
      </div>
      <div className="parties lines">
        {parties.map(([role, , line], i) => (
          <span key={role} className={`party-line ${pos === i ? "has" : ""}`}>
            {line}
          </span>
        ))}
      </div>
      <p className="sr-only" aria-live="polite">
        {amt ? `${amt}: ` : ""}
        {m.phase === "held" ? "held at PayPal" : m.phase === "settled" ? "paid to seller" : m.phase === "refunded" ? "refunded to buyer" : m.phase}
      </p>
      {/* Always one line tall, so the panels below do not jump when the PayPal ids arrive. */}
      <div className="money-foot">
        {m.orderId || m.authId ? (
          <>
            <CopyId label="PayPal order" value={m.orderId} />
            <CopyId label="Authorization" value={m.authId} />
          </>
        ) : (
          <span className="idk">
            {m.phase === "blocked" ? "No PayPal order: Trustee never called PayPal" : "The PayPal order appears once funds are held"}
          </span>
        )}
      </div>
    </div>
  );
}
