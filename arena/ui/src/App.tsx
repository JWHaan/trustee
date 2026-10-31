import { useCallback, useEffect, useRef, useState } from "react";
import {
  fetchScenarios,
  startRun,
  streamRun,
  type Mode,
  type Row,
  type ScenarioInfo,
  type Snapshot,
} from "./api";
import { LedgerGrid } from "./LedgerGrid";
import { Transcript } from "./Transcript";
import { TrustStrip } from "./TrustStrip";
import { ApprovalBanner } from "./Approval";
import { money, pendingApproval } from "./describe";

const MODE_LABEL: Record<string, string> = {
  auto: "auto (live → scripted)",
  live: "live only",
  scripted: "scripted",
};

export default function App() {
  const [rows, setRows] = useState<Row[]>([]);
  const [snap, setSnap] = useState<Snapshot | null>(null);
  const [running, setRunning] = useState(false);
  const [scenarios, setScenarios] = useState<ScenarioInfo[]>([]);
  const [mode, setMode] = useState<Mode>("auto");
  const [error, setError] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const autoStarted = useRef(false);

  useEffect(() => {
    fetchScenarios()
      .then(setScenarios)
      .catch((e: Error) => setError(e.message));
  }, []);

  const run = useCallback(
    async (scenario: string) => {
      abortRef.current?.abort();
      const controller = new AbortController();
      abortRef.current = controller;

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

  const active = scenarios.find((s) => s.name === (snap?.scenario ?? scenarios[0]?.name));
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

  return (
    <div className="shell">
      <header>
        <div className="wordmark">
          <h1>Trustee</h1>
          <span>agent-to-agent commerce, settled</span>
        </div>
        <div className="header-right">
          {snap?.fallback_from === "live" ? (
            <span className="pill fallback">live failed → scripted</span>
          ) : snap?.effective_mode ? (
            <span className={`pill ${snap.effective_mode}`}>{snap.effective_mode}</span>
          ) : null}
          <span className="pill">{running ? "running" : snap?.outcome ?? snap?.state ?? "idle"}</span>
        </div>
      </header>

      <TrustStrip rows={rows} snap={snap} running={running} />

      <main>
        <section className="panel">
          <div className="panel-head">
            <span>negotiation &amp; settlement</span>
            <span className="count">{rows.filter((r) => r.kind !== "note").length} events</span>
          </div>

          {verdict ? (
            <div className={`verdict ${verdict === "SETTLED" ? "good" : "bad"}`}>
              <div className="big">
                {verdict === "SETTLED"
                  ? "settled — funds captured to seller"
                  : verdict === "REJECTED"
                    ? "rejected — forged signature, no money moved"
                    : unapproved
                      ? "voided — the buyer never approved, nothing was charged"
                      : "voided — buyer refunded"}
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
          ) : null}

          {approving ? <ApprovalBanner row={approving} /> : null}

          {error ? (
            <div className="verdict bad">
              <div className="big">run failed</div>
              <div className="why">{error}</div>
            </div>
          ) : null}

          <Transcript rows={rows} running={running} />
        </section>

        <section className="panel">
          <div className="panel-head">
            <span>ledger tape</span>
            <span className="count">{rows.length} rows</span>
          </div>
          <LedgerGrid rows={rows} />
        </section>
      </main>

      <footer className="controls">
        {scenarios.map((s) => (
          <button
            key={s.name}
            className={s.cheat || s.attack ? "danger" : "primary"}
            onClick={() => void run(s.name)}
            disabled={running}
            title={s.blurb}
          >
            <span className="play">▶</span>
            {s.title}
          </button>
        ))}

        <select value={mode} onChange={(e) => setMode(e.target.value as Mode)} disabled={running} aria-label="mode">
          {(Object.keys(MODE_LABEL) as Mode[]).map((m) => (
            <option key={m} value={m}>
              {MODE_LABEL[m]}
            </option>
          ))}
        </select>

        <span className="spacer" />

        {active && !snap ? (
          <span className="meta">
            ask <b>{money(active.ask)}</b> · floor <b>{money(active.floor)}</b> · budget{" "}
            <b>{money(active.budget)}</b>
          </span>
        ) : null}
        {snap ? (
          <span className="meta">
            <b>{snap.price ? money(snap.price) : "—"}</b> · {snap.deal_id?.slice(-8) ?? ""} · {snap.turns} moves
          </span>
        ) : null}
      </footer>
    </div>
  );
}
