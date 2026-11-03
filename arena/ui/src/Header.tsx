import { useEffect, useRef, useState } from "react";
import { BookOpen, ChevronDown, Github, IdCard, ShieldCheck } from "lucide-react";
import type { Health, Row, Snapshot } from "./api";

const REPO = "https://github.com/JWHaan/trustee";

const HOW: [string, string][] = [
  ["Negotiate", "Two agents agree a price. A language model proposes; it never touches money."],
  ["Sign", "Both sign the contract with Ed25519 keys. Trustee checks them against the registered keys."],
  ["Mandate check", "Each side's spending limits are enforced by deterministic code."],
  ["Escrow held at PayPal", "The buyer's funds are authorized and held. Nothing moves yet."],
  ["Verify delivery", "The buyer's own check, not the seller's word, decides if the file matches the contract."],
  ["Settle or refund", "Pass: capture to the seller. Fail: an arbiter cites the clause and the hold is voided."],
];

interface Props {
  health: Health | null;
  snap: Snapshot | null;
  rows: Row[];
  running: boolean;
}

function chipTone(snap: Snapshot | null): { text: string; cls: string } | null {
  if (snap?.fallback_from === "live") return { text: "model: live failed, scripted", cls: "warn" };
  if (snap?.effective_mode === "live") return { text: "model: live", cls: "ok" };
  if (snap?.effective_mode === "scripted") return { text: "model: scripted replay", cls: "info" };
  return null;
}

export function Header({ health, snap, rows, running }: Props) {
  const [open, setOpen] = useState(false);
  const wrap = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    const onClick = (e: MouseEvent) => {
      if (wrap.current && !wrap.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("keydown", onKey);
    document.addEventListener("mousedown", onClick);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("mousedown", onClick);
    };
  }, [open]);

  const ids = rows.filter((r) => r.kind === "identity");
  const bad = ids.filter((r) => r.check === "rejected").length;
  const ok = ids.filter((r) => r.check === "verified").length;
  const sig = bad
    ? { text: "signature forged: rejected", cls: "bad" }
    : ok
      ? { text: `${ok} signatures verified`, cls: "ok" }
      : { text: "signatures: none yet", cls: "idle" };
  const model = chipTone(snap);
  const status = (running ? "running" : snap?.outcome ?? snap?.state ?? "idle").replace(/_/g, " ");

  return (
    <header className="top">
      <div className="brand">
        <span className="logo" aria-hidden="true">
          <ShieldCheck size={22} strokeWidth={2} />
        </span>
        <div>
          <h1>Trustee</h1>
          <p>Escrow and trust for payments between AI agents, on PayPal</p>
        </div>
      </div>

      <div className="top-right">
        <div className="chips-row" aria-label="run status">
          {health ? (
            <span className={`tag ${health.paypal === "sandbox" ? "gold" : "info"}`}>
              {health.paypal === "sandbox" ? "PayPal sandbox" : "PayPal dry-run"}
            </span>
          ) : (
            <span className="tag idle">PayPal: checking</span>
          )}
          {model ? <span className={`tag ${model.cls}`}>{model.text}</span> : null}
          <span className={`tag ${sig.cls}`}>{sig.text}</span>
          <span className={`tag status ${running ? "run" : "idle"}`}>{status}</span>
        </div>

        <nav className="links" aria-label="project links">
          <a href={REPO} target="_blank" rel="noopener noreferrer">
            <Github size={14} aria-hidden="true" /> GitHub
          </a>
          <a href="/api/docs" target="_blank" rel="noopener noreferrer">
            <BookOpen size={14} aria-hidden="true" /> API docs
          </a>
          <a href="/.well-known/agent.json" target="_blank" rel="noopener noreferrer">
            <IdCard size={14} aria-hidden="true" /> Agent Card
          </a>
        </nav>

        <div className="how" ref={wrap}>
          <button
            type="button"
            className="ghost"
            aria-expanded={open}
            aria-controls="how-panel"
            onClick={() => setOpen((v) => !v)}
          >
            How it works
            <ChevronDown size={14} aria-hidden="true" className={open ? "flip" : ""} />
          </button>
          {open ? (
            <div id="how-panel" className="popover" role="region" aria-label="How Trustee works">
              <p className="lede">
                Businesses are starting to let AI agents buy data, API access and digital work from other
                companies&apos; agents. Trustee sits between them so neither side has to trust the other.
              </p>
              <ol>
                {HOW.map(([title, text]) => (
                  <li key={title}>
                    <b>{title}</b>
                    <span>{text}</span>
                  </li>
                ))}
              </ol>
            </div>
          ) : null}
        </div>
      </div>
    </header>
  );
}
