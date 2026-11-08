import { Gavel, Handshake, KeyRound, Loader, Network, Server, UserCheck } from "lucide-react";
import type { ReactNode } from "react";
import type { Mode, ScenarioInfo } from "./api";

const SHORT: Record<string, { label: string; icon: ReactNode; line: string }> = {
  honest: {
    label: "Honest seller",
    icon: <Handshake size={16} aria-hidden="true" />,
    line: "Tidewater ships the dataset Northwind paid for.",
  },
  cheat: {
    label: "Cheating seller",
    icon: <Gavel size={16} aria-hidden="true" />,
    line: "Tidewater ships a junk file under the right name.",
  },
  forged: {
    label: "Impostor",
    icon: <KeyRound size={16} aria-hidden="true" />,
    line: "Someone signs the contract as Tidewater with a key nobody registered.",
  },
  overreach: {
    label: "Over the limit",
    icon: <UserCheck size={16} aria-hidden="true" />,
    line: "The seller wants $190; the human told the agent $175 at most.",
  },
  outside: {
    label: "Outside agents",
    icon: <Server size={16} aria-hidden="true" />,
    line: "Two agents on their own servers settle through the public API.",
  },
  a2a: {
    label: "Agent to agent",
    icon: <Network size={16} aria-hidden="true" />,
    line: "Northwind and Tidewater find each other and trade over A2A.",
  },
};

const MODE_LABEL: Record<Mode, string> = {
  auto: "Agents: live AI, replay if offline",
  live: "Agents: live AI only",
  scripted: "Agents: recorded replay",
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
  const selected = scenarios.find((s) => s.name === current);
  return (
    <section className="picker-v2" aria-label="choose a deal">
      <div className="picker-row">
        <span className="picker-label" id="picker-label">
          Try a deal
        </span>
        {scenarios.length === 0 ? (
          <span className="picker-wait" role="status" aria-busy="true">
            <Loader size={14} className="spin" aria-hidden="true" />
            {warming ? "Waking the demo server. A cold start can take up to a minute." : "Loading deals"}
          </span>
        ) : (
          <div className="pills" role="group" aria-labelledby="picker-label">
            {scenarios.map((s) => {
              const meta = SHORT[s.name];
              const on = current === s.name;
              return (
                <button
                  key={s.name}
                  type="button"
                  className={`pill ${on ? "on" : ""}`}
                  aria-pressed={on}
                  onClick={() => onRun(s.name)}
                  disabled={running}
                  title={s.blurb}
                >
                  {meta?.icon}
                  {meta?.label ?? s.title}
                </button>
              );
            })}
          </div>
        )}
      </div>
      <div className="picker-sub">
        <p className="picker-line">{selected ? SHORT[selected.name]?.line ?? selected.blurb : ""}</p>
        <label className="modesel">
          <span className="sr-only">How the agents negotiate</span>
          <select value={mode} onChange={(e) => onMode(e.target.value as Mode)} disabled={running}>
            {(Object.keys(MODE_LABEL) as Mode[]).map((m) => (
              <option key={m} value={m}>
                {MODE_LABEL[m]}
              </option>
            ))}
          </select>
        </label>
      </div>
    </section>
  );
}
