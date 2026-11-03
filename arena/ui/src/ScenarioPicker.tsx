import { Gavel, Handshake, KeyRound, Loader, Network, Server, UserCheck } from "lucide-react";
import type { ReactNode } from "react";
import type { Mode, ScenarioInfo } from "./api";

interface Meta {
  icon: ReactNode;
  /** Fits one card in a six-across row; the server's longer title stays in the tooltip. */
  short: string;
  blurb: string;
  outcome: string;
  tone: "good" | "refund" | "bad";
}

const META: Record<string, Meta> = {
  honest: {
    short: "Honest seller",
    icon: <Handshake size={18} aria-hidden="true" />,
    blurb: "Ships the dataset Northwind paid for; the check passes.",
    outcome: "Settles",
    tone: "good",
  },
  cheat: {
    short: "Cheating seller",
    icon: <Gavel size={18} aria-hidden="true" />,
    blurb: "Ships junk under the right file name; the hash fails.",
    outcome: "Refunds",
    tone: "refund",
  },
  forged: {
    short: "Impostor seller",
    icon: <KeyRound size={18} aria-hidden="true" />,
    blurb: "An impostor signs as Tidewater; the signature is refused.",
    outcome: "Rejected before PayPal",
    tone: "bad",
  },
  outside: {
    short: "Outside agents (/v1)",
    icon: <Server size={18} aria-hidden="true" />,
    blurb: "Two outside agents settle through the public /v1 API.",
    outcome: "Settles via API",
    tone: "good",
  },
  overreach: {
    short: "Agent over-reaches",
    icon: <UserCheck size={18} aria-hidden="true" />,
    blurb: "The seller will not go below $190 but the human said $175 at most; the agent cannot pay alone.",
    outcome: "Steps up to a human",
    tone: "refund",
  },
  a2a: {
    short: "Three A2A agents",
    icon: <Network size={18} aria-hidden="true" />,
    blurb: "Northwind and Tidewater are A2A agents that find each other by Agent Card; Trustee holds the money.",
    outcome: "Settles over A2A",
    tone: "good",
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
          {[0, 1, 2, 3, 4, 5].map((i) => (
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
                title={`${s.title}: ${meta?.blurb ?? s.blurb}`}
              >
                <span className="card-top">
                  <span className={`card-icon ${meta?.tone ?? "good"}`}>{meta?.icon}</span>
                  <span className="card-head">
                    <span className="card-title">{meta?.short ?? s.title}</span>
                    <span className={`outcome ${meta?.tone ?? "good"}`}>{busy ? "running…" : meta?.outcome}</span>
                  </span>
                </span>
                <span className="card-blurb">{meta?.blurb ?? s.blurb}</span>
              </button>
            );
          })}
        </div>
      )}
    </section>
  );
}
