import { useCallback, useEffect, useRef, useState } from "react";
import { X } from "lucide-react";
import {
  fetchHealth,
  fetchScenarios,
  startRun,
  streamRun,
  type Health,
  type Mode,
  type Row,
  type ScenarioInfo,
  type Snapshot,
} from "./api";
import { Details } from "./Details";
import { Header } from "./Header";
import { ScenarioPicker } from "./ScenarioPicker";
import { Verdict } from "./Verdict";
import { pendingApproval } from "./describe";

const delay = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

// `?mode=scripted` makes the auto-started run a replay too: no model or PayPal call while demoing or testing.
function initialMode(): Mode {
  const m = new URLSearchParams(window.location.search).get("mode");
  return m === "live" || m === "scripted" ? m : "auto";
}

export default function App() {
  const [rows, setRows] = useState<Row[]>([]);
  const [snap, setSnap] = useState<Snapshot | null>(null);
  const [running, setRunning] = useState(false);
  const [scenarios, setScenarios] = useState<ScenarioInfo[]>([]);
  const [warming, setWarming] = useState(false);
  const [health, setHealth] = useState<Health | null>(null);
  const [mode, setMode] = useState<Mode>(initialMode);
  const [current, setCurrent] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const autoStarted = useRef(false);

  // A hosted free-tier server may be asleep: retry with a visible "warming" state before giving up.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      for (let attempt = 0; attempt < 30 && !cancelled; attempt++) {
        try {
          const list = await fetchScenarios();
          if (!cancelled) {
            setScenarios(list);
            setWarming(false);
          }
          return;
        } catch (e) {
          if (attempt === 1) setWarming(true);
          if (attempt === 29 && !cancelled) {
            setWarming(false);
            setError((e as Error).message);
          }
          await delay(2000);
        }
      }
    })();
    fetchHealth()
      .then(setHealth)
      .catch(() => setHealth(null));
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (health || !scenarios.length) return;
    fetchHealth()
      .then(setHealth)
      .catch(() => setHealth(null));
  }, [scenarios, health]);

  const run = useCallback(
    async (scenario: string) => {
      abortRef.current?.abort();
      const controller = new AbortController();
      abortRef.current = controller;

      setCurrent(scenario);
      setRows([]);
      setSnap(null);
      setError(null);
      setRunning(true);

      try {
        const runId = await startRun(scenario, mode);
        await streamRun(
          runId,
          {
            onRow: (row) => setRows((prev) => [...prev, row]),
            onDone: (final) => {
              setSnap(final);
              setRunning(false);
            },
          },
          controller.signal,
        );
      } catch (e) {
        if ((e as Error).name === "AbortError") return;
        setError((e as Error).message);
      } finally {
        if (!controller.signal.aborted) setRunning(false);
      }
    },
    [mode],
  );

  // Demonstrate on load: the URL alone should show the product working.
  useEffect(() => {
    if (autoStarted.current || !scenarios.length) return;
    autoStarted.current = true;
    const t = setTimeout(() => void run(scenarios[0].name), 250);
    return () => clearTimeout(t);
  }, [scenarios, run]);

  const approving = pendingApproval(rows, running);

  return (
    <div className="shell-v2">
      <Header health={health} />
      <main className="page">
        <ScenarioPicker
          scenarios={scenarios}
          current={current}
          running={running}
          warming={warming}
          mode={mode}
          onMode={setMode}
          onRun={(name) => void run(name)}
        />

        {error ? (
          <div className="toast" role="alert">
            <span>
              <b>The run could not start.</b> {error}
            </span>
            <button type="button" className="icon" onClick={() => setError(null)} aria-label="Dismiss error">
              <X size={14} aria-hidden="true" />
            </button>
          </div>
        ) : null}

        <Verdict rows={rows} snap={snap} running={running} scenario={current} approving={approving} />
        <Details rows={rows} snap={snap} running={running} />
      </main>
    </div>
  );
}
