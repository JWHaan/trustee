import { useEffect, useRef, type ReactNode } from "react";
import { Gavel, KeyRound, ShieldAlert, ShieldCheck, Vault } from "lucide-react";
import type { Row, Snapshot } from "./api";
import { actorName, describe, isNarrative, money } from "./describe";
import { side } from "./derive";

const WHO: Record<string, string> = {
  buyer: "Northwind",
  seller: "Tidewater",
  northwind: "Northwind",
  tidewater: "Tidewater",
};

function Bubble({ row, children }: { row: Row; children: ReactNode }) {
  const s = side(row.actor);
  const name = actorName(row.actor);
  return (
    <div className={`bubble-row ${s}`}>
      <div className={`bubble ${s}`}>
        <div className={`bubble-who actor-${name}`}>
          {WHO[name] ?? name}
          <span>{s} agent</span>
        </div>
        {children}
      </div>
    </div>
  );
}

function Message({ row, snap }: { row: Row; snap: Snapshot | null }) {
  const line = describe(row);
  const s = side(row.actor);
  const price = money(row.price);

  if (row.kind === "msg" && ["offer", "counter", "accept"].includes(row.event)) {
    return (
      <Bubble row={row}>
        <div className="bubble-head">
          <span className="verb">{row.event === "offer" ? "Offers" : row.event === "counter" ? "Counters" : "Accepts"}</span>
          <span className="price-chip">{price}</span>
        </div>
        {row.note ? <p>{row.note}</p> : null}
      </Bubble>
    );
  }

  if (row.kind === "msg" && row.event === "attestation") {
    const reject = row.verdict.toLowerCase() === "reject";
    return (
      <Bubble row={row}>
        <div className="bubble-head">
          <span className="verb">Checks the delivery</span>
          <span className={`verdict-chip ${reject ? "bad" : "good"}`}>{reject ? "Reject" : "Accept"}</span>
        </div>
        {row.note ? <p>{row.note}</p> : null}
      </Bubble>
    );
  }

  if (row.kind === "delivery" && s === "seller") {
    return (
      <Bubble row={row}>
        <div className="bubble-head">
          <span className="verb">Ships the deliverable</span>
        </div>
        {row.note ? <p className="mono">{row.note}</p> : null}
      </Bubble>
    );
  }

  if (row.kind === "ruling") {
    const clause = snap?.ruling?.clause;
    return (
      <div className="ruling">
        <div className="ruling-head">
          <Gavel size={15} aria-hidden="true" /> Arbiter ruling: <b>{row.event.charAt(0).toUpperCase() + row.event.slice(1)}</b>
          {snap?.ruling ? <span className="tag info">{snap.ruling.objective ? "objective" : "judged"}</span> : null}
        </div>
        <p>{row.note}</p>
        {clause ? <code>clause: {clause}</code> : null}
      </div>
    );
  }

  if (row.kind === "identity") {
    const bad = row.check === "rejected";
    return (
      <div className={`sysline ${bad ? "bad" : "good"}`}>
        {bad ? <ShieldAlert size={14} aria-hidden="true" /> : <ShieldCheck size={14} aria-hidden="true" />}
        <span>
          <b>{line.headline}</b>
          {line.detail ? <em> {line.detail}</em> : null}
        </span>
      </div>
    );
  }

  if (row.kind === "paypal") {
    return (
      <div className="sysline gold">
        <Vault size={14} aria-hidden="true" />
        <span>
          <b>{line.headline}</b>
          {price ? <em> {price}</em> : null}
        </span>
      </div>
    );
  }

  if (row.kind === "state") {
    return (
      <div className="divider">
        <span>
          {row.state_from} <i>→</i> {row.state_to}
        </span>
      </div>
    );
  }

  if (row.kind === "api") {
    return (
      <div className="sysline api">
        <KeyRound size={14} aria-hidden="true" />
        <span>
          <code>{line.headline}</code>
          {line.detail ? <em> {line.detail}</em> : null}
        </span>
      </div>
    );
  }

  return (
    <div className="sysline">
      <span className="dot" aria-hidden="true" />
      <span>
        <b>{line.headline}</b>
        {line.detail ? <em> {line.detail}</em> : null}
      </span>
    </div>
  );
}

interface Props {
  rows: Row[];
  running: boolean;
  snap: Snapshot | null;
  footer?: ReactNode;
}

export function Transcript({ rows, running, snap, footer }: Props) {
  const feedRef = useRef<HTMLDivElement>(null);
  const visible = rows.filter(isNarrative);
  const lastId = visible.length ? visible[visible.length - 1].id : null;

  // Follow the tape; rAF because the new row is not laid out yet when the effect runs.
  useEffect(() => {
    const feed = feedRef.current;
    if (!feed) return;
    const frame = requestAnimationFrame(() => {
      feed.scrollTop = feed.scrollHeight;
    });
    return () => cancelAnimationFrame(frame);
  }, [visible.length, footer]);

  if (!visible.length) {
    return (
      <div className="empty">
        <h2>{running ? "Starting the deal…" : "No deal running"}</h2>
        <p>
          Two agents negotiate a price, Trustee holds the money at PayPal, and the seller either delivers the dataset or
          ships a fake.
        </p>
      </div>
    );
  }

  return (
    <div className="feed" ref={feedRef} role="log" aria-label="Conversation" tabIndex={0}>
      {visible.map((row) => (
        <div key={row.id} className={`msgwrap ${row.id === lastId && running ? "last" : ""}`}>
          <Message row={row} snap={snap} />
        </div>
      ))}
      {footer}
    </div>
  );
}
