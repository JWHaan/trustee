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
import { ApprovalBanner } from "./Approval";
import { Header } from "./Header";
import { LedgerGrid } from "./LedgerGrid";
import { MoneyPanel } from "./MoneyPanel";
import { ScenarioPicker } from "./ScenarioPicker";
import { Timeline } from "./Timeline";
import { Transcript } from "./Transcript";
import { pendingApproval } from "./describe";

const delay = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

export default function App() {
  const [rows, setRows] = useState<Row[]>([]);
  const [snap, setSnap] = useState<Snapshot | null>(null);
  const [running, setRunning] = useState(false);
  const [scenarios, setScenarios] = useState<ScenarioInfo[]>([]);
  const [warming, setWarming] = useState(false);
  const [health, setHealth] = useState<Health | null>(null);
  const [mode, setMode] = useState<Mode>("auto");
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

  const outcome = snap?.outcome ?? null;
  const unapproved = outcome === "approval_timeout" || outcome === "approval_cancelled";
  const verdict =
    outcome === "rejected_identity"
      ? "REJECTED"
      : snap && (snap.state === "SETTLED" || snap.state === "VOIDED")
        ? snap.state
        : null;
  const rejection = rows.find((r) => r.kind === "identity" && r.check === "rejected");
  const approving = pendingApproval(rows, running);

  const verdictCard = verdict ? (
    <div className={`verdict ${verdict === "SETTLED" ? "good" : "bad"}`}>
      <div className="big">
        {verdict === "SETTLED"
          ? "Settled: funds captured to seller"
          : verdict === "REJECTED"
            ? "Rejected: forged signature, no money moved"
            : unapproved
              ? "Voided: the buyer never approved, nothing was charged"
              : "Voided: buyer refunded"}
      </div>
      <div className="why">
        {verdict === "SETTLED"
          ? snap?.attest?.reason ?? "delivery attested against the contract clauses"
          : verdict === "REJECTED"
            ? rejection?.note ?? "the signature did not match the registered key"
            : snap?.ruling?.reasoning ?? snap?.attest?.reason ?? "escrow released"}
      </div>
      {verdict === "REJECTED" ? (
        <div className="clause">PayPal was never called · signer: {rejection?.signer ?? "unknown"}</div>
      ) : snap?.ruling ? (
        <div className="clause">
          clause: {snap.ruling.clause} · ruling: {snap.ruling.decision}
          {snap.ruling.objective ? " (objective)" : " (judged)"}
        </div>
      ) : null}
    </div>
  ) : null;

  const footer = (
    <>
      {verdictCard}
      {approving ? <ApprovalBanner row={approving} /> : null}
    </>
  );

  return (
    <div className="shell">
      <Header health={health} snap={snap} rows={rows} running={running} />
      <ScenarioPicker
        scenarios={scenarios}
        current={current}
        running={running}
        warming={warming}
        mode={mode}
        onMode={setMode}
        onRun={(name) => void run(name)}
      />

      <section className="timeline-wrap" aria-label="deal progress">
        <div className="section-bar">
          <span>Deal timeline</span>
          <span className="meta">
            {snap
              ? `deal ${snap.deal_id?.slice(-8) ?? ""} · ${snap.turns} negotiation moves`
              : running
                ? "live"
                : "waiting for a run"}
          </span>
        </div>
        <Timeline rows={rows} snap={snap} running={running} />
      </section>

      {error ? (
        <div className="toast" role="alert">
          <span>
            <b>Run failed.</b> {error}
          </span>
          <button type="button" className="icon" onClick={() => setError(null)} aria-label="Dismiss error">
            <X size={14} aria-hidden="true" />
          </button>
        </div>
      ) : null}

      <main>
        <div className="col-left">
          <section className="panel money-panel" aria-label="money">
            <div className="panel-head">
              <span>Where the money is</span>
            </div>
            <MoneyPanel rows={rows} snap={snap} running={running} health={health} />
          </section>
          <section className="panel feed-panel" aria-label="transcript">
            <div className="panel-head">
              <span>Negotiation &amp; settlement</span>
              <span className="count">{rows.filter((r) => r.kind !== "note").length} events</span>
            </div>
            <Transcript rows={rows} running={running} snap={snap} footer={footer} />
          </section>
        </div>

        <section className="panel ledger-panel" aria-label="ledger tape">
          <div className="panel-head">
            <span>Ledger tape</span>
            <span className="count">append-only</span>
          </div>
          <LedgerGrid rows={rows} />
        </section>
      </main>
    </div>
  );
}
