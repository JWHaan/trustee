import { Gavel, Handshake, KeyRound, Loader, Network, Server, UserCheck } from "lucide-react";
import type { ReactNode } from "react";
import type { Mode, ScenarioInfo } from "./api";
import { money } from "./describe";

interface Meta {
  icon: ReactNode;
  blurb: string;
  outcome: string;
  tone: "good" | "refund" | "bad";
  who: string;
}

const META: Record<string, Meta> = {
  honest: {
    icon: <Handshake size={18} aria-hidden="true" />,
    blurb: "Ships the dataset Northwind paid for; the check passes.",
    outcome: "Settles",
    tone: "good",
    who: "Northwind + Tidewater",
  },
  cheat: {
    icon: <Gavel size={18} aria-hidden="true" />,
    blurb: "Ships junk under the right file name; the hash fails.",
    outcome: "Refunds",
    tone: "refund",
    who: "Northwind + Tidewater + arbiter",
  },
  forged: {
    icon: <KeyRound size={18} aria-hidden="true" />,
    blurb: "An impostor signs as Tidewater; the signature is refused.",
    outcome: "Rejected before PayPal",
    tone: "bad",
    who: "Northwind + impostor",
  },
  outside: {
    icon: <Server size={18} aria-hidden="true" />,
    blurb: "Two outside agents settle through the public /v1 API.",
    outcome: "Settles via API",
    tone: "good",
    who: "External agents over HTTP",
  },
  overreach: {
    icon: <UserCheck size={18} aria-hidden="true" />,
    blurb: "The seller will not go below $190 but the human said $175 at most; the agent cannot pay alone.",
    outcome: "Steps up to a human",
    tone: "refund",
    who: "Northwind + Tidewater + human approval",
  },
  a2a: {
    icon: <Network size={18} aria-hidden="true" />,
    blurb: "Northwind and Tidewater are A2A agents that find each other by Agent Card; Trustee holds the money.",
    outcome: "Settles over A2A",
    tone: "good",
    who: "Northwind + Tidewater + Trustee over A2A",
  },
};

const MODE_LABEL: Record<Mode, string> = {
  auto: "Auto (live, else scripted)",
  live: "Live model only",
  scripted: "Scripted replay",
};

interface Props {
  scenarios: ScenarioInfo[];
  current: string | null;
  running: boolean;
  warming: boolean;
  mode: Mode;
  onMode: (m: Mode) => void;
  onRun: (name: string) => void;
}

export function ScenarioPicker({ scenarios, current, running, warming, mode, onMode, onRun }: Props) {
  return (
    <section className="picker" aria-label="scenarios">
      <div className="picker-head">
        <p>
          <b>Demo:</b> Northwind Outfitters&apos; planning agent buys a Q3 returns dataset from Tidewater Data
          Co.&apos;s sales agent. Choose a seller and watch where the money goes.
        </p>
        <label className="modesel">
          <span>Model</span>
          <select value={mode} onChange={(e) => onMode(e.target.value as Mode)} disabled={running}>
            {(Object.keys(MODE_LABEL) as Mode[]).map((m) => (
              <option key={m} value={m}>
                {MODE_LABEL[m]}
              </option>
            ))}
          </select>
        </label>
      </div>

      {scenarios.length === 0 ? (
        <div className="cards" aria-busy="true">
          {[0, 1, 2, 3].map((i) => (
            <div key={i} className="card skeleton" aria-hidden="true">
              <span className="sk w40" />
              <span className="sk w90" />
              <span className="sk w60" />
            </div>
          ))}
          {warming ? (
            <p className="warming" role="status">
              <Loader size={13} className="spin" aria-hidden="true" /> Waking the demo server. A cold start can take
              up to a minute.
            </p>
          ) : null}
        </div>
      ) : (
        <div className="cards">
          {scenarios.map((s) => {
            const meta = META[s.name];
            const selected = current === s.name;
            const busy = selected && running;
            return (
              <button
                key={s.name}
                type="button"
                className={`card ${selected ? "selected" : ""} ${busy ? "busy" : ""}`}
                aria-pressed={selected}
                onClick={() => onRun(s.name)}
                disabled={running}
                title={s.blurb}
              >
                <span className="card-top">
                  <span className={`card-icon ${meta?.tone ?? "good"}`}>{meta?.icon}</span>
                  <span className="card-title">{s.title}</span>
                  <span className={`outcome ${meta?.tone ?? "good"}`}>{busy ? "running…" : meta?.outcome}</span>
                </span>
                <span className="card-blurb">{meta?.blurb ?? s.blurb}</span>
                <span className="card-foot">
                  <span>{meta?.who}</span>
                  <span className="terms">ask {money(s.ask)}</span>
                </span>
              </button>
            );
          })}
        </div>
      )}
    </section>
  );
}
