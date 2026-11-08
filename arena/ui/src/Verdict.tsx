import { Check, CircleDashed, Hourglass, ShieldX, Undo2 } from "lucide-react";
import type { ReactNode } from "react";
import type { Row, Snapshot, Summary } from "./api";
import { ApprovalBanner } from "./Approval";
import { deriveMoney, deriveTimeline } from "./derive";
import { money } from "./describe";
import { Timeline } from "./Timeline";

/**
 * The answer to "what happened to the money, and why", for the deal on screen. When the run is finished it reads the
 * server's summary (the same one agents get from the API); while it runs it says which step is under way.
 */

type Spot = "buyer" | "hold" | "seller";

interface Party {
  key: Spot;
  name: string;
  line: string;
}

const TONE_ICON: Record<Summary["tone"], ReactNode> = {
  good: <Check size={20} strokeWidth={2.5} aria-hidden="true" />,
  bad: <Undo2 size={20} strokeWidth={2.25} aria-hidden="true" />,
  warn: <Hourglass size={20} strokeWidth={2.25} aria-hidden="true" />,
  neutral: <CircleDashed size={20} strokeWidth={2.25} aria-hidden="true" />,
};

const amt = (v: string, currency: string) => `${money(v) || v} ${currency === "USD" ? "" : currency}`.trim();

function finishedParties(s: Summary, buyer: string, seller: string): { parties: Party[]; spot: Spot; split: boolean } {
  const m = s.money;
  const paid = Number(m.paid_to_seller) > 0;
  const back = Number(m.returned_to_buyer) > 0;
  const held = Number(m.held) > 0;
  const c = s.currency;
  const buyerLine = back ? (paid ? `got ${amt(m.returned_to_buyer, c)} back` : `keeps ${amt(m.returned_to_buyer, c)}`) : paid ? `paid ${amt(m.paid_to_seller, c)}` : "charged nothing";
  const holdLine = held ? `holding ${amt(m.held, c)}` : paid || back ? "released" : "never held";
  const sellerLine = paid ? `received ${amt(m.paid_to_seller, c)}` : "not paid";
  const spot: Spot = held ? "hold" : paid ? "seller" : "buyer";
  return {
    parties: [
      { key: "buyer", name: buyer, line: buyerLine },
      { key: "hold", name: "PayPal hold", line: holdLine },
      { key: "seller", name: seller, line: sellerLine },
    ],
    spot,
    split: paid && back,
  };
}

function liveParties(rows: Row[], snap: Snapshot | null, running: boolean, buyer: string, seller: string) {
  const m = deriveMoney(rows, snap, running);
  const a = m.amount ? money(m.amount) : "";
  const held = m.phase === "held";
  const spot: Spot = held ? "hold" : m.phase === "settled" ? "seller" : "buyer";
  return {
    parties: [
      { key: "buyer" as Spot, name: buyer, line: m.phase === "idle" ? "agreeing a price" : a ? `agreed ${a}` : "agreed" },
      {
        key: "hold" as Spot,
        name: "PayPal hold",
        line: m.phase === "approving" ? "waiting for approval" : held ? `holding ${a}` : "not yet",
      },
      { key: "seller" as Spot, name: seller, line: m.phase === "settled" ? `received ${a}` : "waiting" },
    ],
    spot,
    split: false,
  };
}

function names(snap: Snapshot | null, scenario: string | null): [string, string] {
  if (scenario === "forged") return ["Northwind", "Impostor"];
  const seller = snap?.contract?.seller ?? "";
  return ["Northwind", seller.startsWith("tidewater") || seller.startsWith("seller") || !seller ? "Tidewater" : seller];
}

interface Props {
  rows: Row[];
  snap: Snapshot | null;
  running: boolean;
  scenario: string | null;
  approving: Row | null;
}

export function Verdict({ rows, snap, running, scenario, approving }: Props) {
  const [buyer, seller] = names(snap, scenario);
  const summary = !running ? snap?.summary ?? null : null;
  const steps = deriveTimeline(rows, snap, running);
  const active = steps.find((s) => s.status === "active");

  let tone: Summary["tone"] = "neutral";
  let headline: ReactNode;
  let reasons: string[] = [];
  let next: string | null = null;
  if (summary) {
    tone = summary.tone;
    headline = summary.headline;
    reasons = summary.reasons;
    next = summary.next_step;
  } else if (running) {
    headline = active ? `${active.label}…` : "Starting the deal…";
  } else if (snap?.error) {
    tone = "bad";
    headline = "The run stopped with an error.";
    reasons = [snap.error];
  } else {
    headline = "Pick a deal above to watch where the money goes.";
  }

  const flow = summary ? finishedParties(summary, buyer, seller) : liveParties(rows, snap, running, buyer, seller);
  const ref = snap?.escrow?.order_id;

  return (
    <section className={`verdict-v2 tone-${tone}`} aria-label="what happened" aria-live="polite">
      <div className="v-head">
        <span className="v-icon">{summary ? TONE_ICON[tone] : running ? <CircleDashed size={20} className="spin" aria-hidden="true" /> : <ShieldX size={20} aria-hidden="true" />}</span>
        <h2 className="v-headline">{headline}</h2>
      </div>

      {reasons.length ? (
        <ul className="v-reasons">
          {reasons.map((r) => (
            <li key={r}>{r}</li>
          ))}
        </ul>
      ) : null}

      <ol className={`ml spot-${flow.spot} ${flow.split ? "split" : ""}`} aria-label="where the money is">
        {flow.parties.map((p) => (
          <li key={p.key} className={`ml-stop ${p.key} ${flow.spot === p.key ? "here" : ""}`}>
            <span className="ml-dot" aria-hidden="true" />
            <span className="ml-name">{p.name}</span>
            <span className="ml-line">{p.line}</span>
          </li>
        ))}
      </ol>

      {approving ? <ApprovalBanner row={approving} /> : null}
      {next ? <p className="v-next">{next}</p> : null}

      <Timeline rows={rows} snap={snap} running={running} />

      {ref ? (
        <p className="v-ref">
          PayPal order <span>{ref}</span>
        </p>
      ) : null}
    </section>
  );
}
