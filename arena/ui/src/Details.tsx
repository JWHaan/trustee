import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { ChevronDown } from "lucide-react";
import type { Row, Snapshot } from "./api";
import { LedgerGrid } from "./LedgerGrid";
import { PolicyChecklist, failedChecks } from "./Policy";
import { Transcript } from "./Transcript";

type Tab = "conversation" | "checks" | "ledger";

const STORE_KEY = "trustee.details";

function load(): { open: boolean; tab: Tab } {
  try {
    const raw = window.localStorage.getItem(STORE_KEY);
    if (raw) {
      const v = JSON.parse(raw) as { open?: boolean; tab?: Tab };
      return { open: Boolean(v.open), tab: v.tab ?? "conversation" };
    }
  } catch {
    // storage blocked or unreadable: use the defaults
  }
  return { open: false, tab: "conversation" };
}

/**
 * Everything behind the verdict: what the agents said, every rule Trustee checked, and the raw ledger.
 * Closed by default; the choice is remembered in this browser only.
 */
export function Details({ rows, snap, running }: { rows: Row[]; snap: Snapshot | null; running: boolean }) {
  const [state, setState] = useState(load);
  const tabsRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    try {
      window.localStorage.setItem(STORE_KEY, JSON.stringify(state));
    } catch {
      // not persisted: fine
    }
  }, [state]);

  const checks = failedChecks(rows);
  const said = rows.filter((r) => r.kind !== "note").length;
  const tabs: { key: Tab; label: string; count: string }[] = [
    { key: "conversation", label: "Conversation", count: String(said) },
    {
      key: "checks",
      label: "Checks",
      count: !checks ? "–" : checks.failed ? `${checks.failed} failed` : "all passed",
    },
    { key: "ledger", label: "Ledger", count: String(rows.length) },
  ];

  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
    const i = tabs.findIndex((t) => t.key === state.tab);
    const next = tabs[(i + (e.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length].key;
    setState((s) => ({ ...s, tab: next }));
    tabsRef.current?.querySelector<HTMLButtonElement>(`#tab-${next}`)?.focus();
  };

  return (
    <section className="details" aria-label="the full record">
      <button
        type="button"
        className="details-toggle"
        aria-expanded={state.open}
        aria-controls="details-body"
        onClick={() => setState((s) => ({ ...s, open: !s.open }))}
      >
        <ChevronDown size={16} aria-hidden="true" className={state.open ? "flip" : ""} />
        {state.open ? "Hide how it happened" : "See how it happened"}
        <span className="details-hint">conversation, checks and ledger</span>
      </button>

      {state.open ? (
        <div id="details-body">
          <div className="rtabs" role="tablist" aria-label="record" ref={tabsRef} onKeyDown={onKey}>
            {tabs.map((t) => (
              <button
                key={t.key}
                id={`tab-${t.key}`}
                type="button"
                role="tab"
                aria-selected={state.tab === t.key}
                aria-controls={`panel-${t.key}`}
                tabIndex={state.tab === t.key ? 0 : -1}
                className={`rtab ${state.tab === t.key ? "on" : ""}`}
                onClick={() => setState((s) => ({ ...s, tab: t.key }))}
              >
                {t.label}
                <span className="count">{t.count}</span>
              </button>
            ))}
          </div>
          <div id={`panel-${state.tab}`} role="tabpanel" aria-labelledby={`tab-${state.tab}`} className="tabpanel">
            {state.tab === "conversation" ? (
              <div className="pane-conversation">
                <Transcript rows={rows} running={running} snap={snap} />
              </div>
            ) : state.tab === "checks" ? (
              <PolicyChecklist rows={rows} />
            ) : (
              <div className="pane-ledger">
                <LedgerGrid rows={rows} />
              </div>
            )}
          </div>
        </div>
      ) : null}
    </section>
  );
}
