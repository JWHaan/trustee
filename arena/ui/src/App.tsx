import { useCallback, useEffect, useRef, useState } from "react";
import { Check, X } from "lucide-react";
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
import { ScenarioPicker, scenarioStory } from "./ScenarioPicker";
import { PolicyChecklist } from "./Policy";
import { Timeline } from "./Timeline";
import { Transcript } from "./Transcript";
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

  const outcome = snap?.outcome ?? null;
  const unapproved = outcome === "approval_timeout" || outcome === "approval_cancelled" || outcome === "approval_failed";
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
      {snap?.receipt ? (
        <div className="clause receipt-line">
          {snap.receipt_verified ? (
            <span className="badge good">
              <Check size={11} strokeWidth={3} aria-hidden="true" /> Receipt verified
            </span>
          ) : (
            <span className="badge bad">Receipt not verified</span>
          )}{" "}
          {snap.human_present === false ? "paid under the intent mandate, no human click" : "approved by a human"} ·{" "}
          <a href={`/api/runs/${snap.run_id}/receipt`} target="_blank" rel="noopener noreferrer">
            signed receipt (JSON)
          </a>
        </div>
      ) : null}
    </div>
  ) : null;

  const status = running
    ? { text: "Running", tone: "run" }
    : verdict === "SETTLED"
      ? { text: "Settled", tone: "good" }
      : verdict === "REJECTED"
        ? { text: "Rejected", tone: "bad" }
        : verdict === "VOIDED"
          ? { text: unapproved ? "Voided" : "Refunded", tone: "refund" }
          : snap
            ? { text: (snap.outcome ?? snap.state).replace(/_/g, " "), tone: "idle" }
            : { text: "Waiting for a run", tone: "idle" };
  const model =
    snap?.fallback_from === "live"
      ? { text: "Live model failed, replayed the script", tone: "warn" }
      : snap?.effective_mode === "live"
        ? { text: "Live model", tone: "" }
        : snap?.effective_mode === "scripted"
          ? { text: "Scripted replay", tone: "" }
          : null;

  const footer = (
    <>
      {verdictCard}
      {approving ? <ApprovalBanner row={approving} /> : null}
    </>
  );

  return (
    <div className="shell">
      <Header health={health} mode={mode} onMode={setMode} running={running} />
      <ScenarioPicker
        scenarios={scenarios}
        current={current}
        running={running}
        warming={warming}
        onRun={(name) => void run(name)}
      />

      <section className="deal" aria-label="The deal">
        <div className="deal-money">
          <MoneyPanel rows={rows} snap={snap} running={running} story={scenarioStory(scenarios, current)} />
        </div>
        <div className="deal-steps">
          <div className="steps-head">
            <span className={`run-status ${status.tone}`} role="status">
              <span className="run-dot" aria-hidden="true" />
              {status.text}
            </span>
            {model ? <span className={`run-model ${model.tone}`}>{model.text}</span> : null}
            {snap?.deal_id ? (
              <span className="run-meta">
                Deal <code>{snap.deal_id.slice(-8)}</code>, {snap.turns} negotiation moves
              </span>
            ) : null}
          </div>
          <Timeline rows={rows} snap={snap} running={running} />
        </div>
      </section>

      {error ? (
        <div className="toast" role="alert">
          <span>
            <b>The run failed.</b> {error} Pick a scenario to try again.
          </span>
          <button type="button" className="icon" onClick={() => setError(null)} aria-label="Dismiss error">
            <X size={14} aria-hidden="true" />
          </button>
        </div>
      ) : null}

      <main>
        <div className="col-left">
          <PolicyChecklist rows={rows} />
          <section className="panel feed-panel" aria-label="Negotiation and settlement">
            <div className="panel-head">
              <h2>Negotiation and settlement</h2>
              <span className="count">{rows.filter((r) => r.kind !== "note").length} events</span>
            </div>
            <Transcript rows={rows} running={running} snap={snap} footer={footer} />
          </section>
        </div>

        <section className="panel ledger-panel" aria-label="Ledger tape">
          <div className="panel-head">
            <h2>Ledger tape</h2>
            <span className="count">Append-only, read from the server</span>
          </div>
          <LedgerGrid rows={rows} />
        </section>
      </main>
    </div>
  );
}
