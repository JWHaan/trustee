import { Gavel, Handshake, KeyRound, Loader, Network, Server, UserCheck } from "lucide-react";
import type { ReactNode } from "react";
import type { ScenarioInfo } from "./api";

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
    icon: <Handshake size={16} aria-hidden="true" />,
    blurb: "Ships the dataset Northwind paid for; the check passes.",
    outcome: "Settles",
    tone: "good",
  },
  cheat: {
    short: "Cheating seller",
    icon: <Gavel size={16} aria-hidden="true" />,
    blurb: "Ships junk under the right file name; the hash fails.",
    outcome: "Refunds",
    tone: "refund",
  },
  forged: {
    short: "Impostor seller",
    icon: <KeyRound size={16} aria-hidden="true" />,
    blurb: "An impostor signs as Tidewater; the signature is refused.",
    outcome: "Rejected before PayPal",
    tone: "bad",
  },
  outside: {
    short: "Outside agents (/v1)",
    icon: <Server size={16} aria-hidden="true" />,
    blurb: "Two outside agents settle through the public /v1 API.",
    outcome: "Settles via API",
    tone: "good",
  },
  overreach: {
    short: "Agent over-reaches",
    icon: <UserCheck size={16} aria-hidden="true" />,
    blurb: "The seller will not go below $190 but the human said $175 at most; the agent cannot pay alone.",
    outcome: "Steps up to a human",
    tone: "refund",
  },
  a2a: {
    short: "Three A2A agents",
    icon: <Network size={16} aria-hidden="true" />,
    blurb: "Northwind and Tidewater are A2A agents that find each other by Agent Card; Trustee holds the money.",
    outcome: "Settles over A2A",
    tone: "good",
  },
};

interface Props {
  scenarios: ScenarioInfo[];
  current: string | null;
  running: boolean;
  warming: boolean;
  onRun: (name: string) => void;
}

/** The selected scenario's one-line story, shown above the money rail. */
export function scenarioStory(scenarios: ScenarioInfo[], name: string | null): { title: string; blurb: string } | null {
  const s = scenarios.find((x) => x.name === name);
  if (!s) return null;
  const meta = META[s.name];
  return { title: meta?.short ?? s.title, blurb: meta?.blurb ?? s.blurb };
}

export function ScenarioPicker({ scenarios, current, running, warming, onRun }: Props) {
  return (
    <nav className="picker" aria-label="Choose a seller to run">
      {scenarios.length === 0 ? (
        <div className="tabs" aria-busy="true">
          {[0, 1, 2, 3, 4, 5].map((i) => (
            <div key={i} className="tab skeleton" aria-hidden="true">
              <span className="sk w60" />
              <span className="sk w40" />
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
        <div className="tabs">
          {scenarios.map((s) => {
            const meta = META[s.name];
            const selected = current === s.name;
            const busy = selected && running;
            return (
              <button
                key={s.name}
                type="button"
                className={`tab ${selected ? "selected" : ""} ${busy ? "busy" : ""}`}
                aria-pressed={selected}
                aria-describedby={`blurb-${s.name}`}
                onClick={() => onRun(s.name)}
                disabled={running}
                title={`${s.title}: ${meta?.blurb ?? s.blurb}`}
              >
                <span className={`tab-icon ${meta?.tone ?? "good"}`}>{meta?.icon}</span>
                <span className="tab-text">
                  <span className="tab-title">{meta?.short ?? s.title}</span>
                  <span className={`outcome ${meta?.tone ?? "good"}`}>{busy ? "Running…" : meta?.outcome}</span>
                </span>
              </button>
            );
          })}
          {/* Referenced by aria-describedby; hidden content still counts as a description. */}
          <div hidden>
            {scenarios.map((s) => (
              <span key={s.name} id={`blurb-${s.name}`}>
                {META[s.name]?.blurb ?? s.blurb}
              </span>
            ))}
          </div>
        </div>
      )}
    </nav>
  );
}
