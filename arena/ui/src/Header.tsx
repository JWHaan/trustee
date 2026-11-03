import { useEffect, useRef, useState } from "react";
import { BookOpen, ChevronDown, Github, IdCard, ShieldCheck } from "lucide-react";
import type { Health, Mode } from "./api";

const REPO = "https://github.com/JWHaan/trustee";

const HOW: [string, string][] = [
  ["Negotiate", "Two agents agree a price. A language model proposes; it never touches money."],
  ["Sign", "Both sign the contract with Ed25519 keys. Trustee checks them against the registered keys."],
  ["Mandate check", "Each side's spending limits are enforced by deterministic code."],
  ["Escrow held at PayPal", "The buyer's funds are authorized and held. Nothing moves yet."],
  ["Verify delivery", "The buyer's own check, not the seller's word, decides if the file matches the contract."],
  ["Settle or refund", "Pass: capture to the seller. Fail: an arbiter cites the clause and the hold is voided."],
];

const MODE_LABEL: Record<Mode, string> = {
  auto: "Auto: live, else replay",
  live: "Live model only",
  scripted: "Scripted replay",
};

interface Props {
  health: Health | null;
  mode: Mode;
  onMode: (m: Mode) => void;
  running: boolean;
}

export function Header({ health, mode, onMode, running }: Props) {
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

  const env = !health
    ? { text: "PayPal: checking", cls: "idle", title: "Checking which PayPal environment the server uses" }
    : health.paypal === "sandbox"
      ? { text: "PayPal sandbox", cls: "sandbox", title: "PayPal sandbox: test accounts, not real money" }
      : { text: "PayPal dry-run", cls: "dry", title: "Dry-run: Trustee makes no PayPal call" };

  return (
    <header className="top">
      <div className="brand">
        <span className="logo" aria-hidden="true">
          <ShieldCheck size={20} strokeWidth={2.2} />
        </span>
        <div>
          <h1>Trustee</h1>
          <p>Escrow and trust for payments between AI agents, on PayPal</p>
        </div>
      </div>

      <div className="top-right">
        <span className={`env-badge ${env.cls}`} title={env.title}>
          <span className="env-dot" aria-hidden="true" />
          {env.text}
        </span>

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

        <a className="toplink" href={REPO} target="_blank" rel="noopener noreferrer" aria-label="Trustee on GitHub">
          <Github size={15} aria-hidden="true" /> <span className="toplink-text">GitHub</span>
        </a>

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
                companies&apos; agents. Trustee sits between them so neither side has to trust the other. In this demo,
                Northwind Outfitters&apos; planning agent buys a Q3 returns dataset from Tidewater Data Co.&apos;s sales
                agent.
              </p>
              <ol>
                {HOW.map(([title, text]) => (
                  <li key={title}>
                    <b>{title}</b>
                    <span>{text}</span>
                  </li>
                ))}
              </ol>
              <nav className="pop-links" aria-label="developer links">
                <a href="/api/docs" target="_blank" rel="noopener noreferrer">
                  <BookOpen size={14} aria-hidden="true" /> API docs
                </a>
                <a href="/.well-known/agent.json" target="_blank" rel="noopener noreferrer">
                  <IdCard size={14} aria-hidden="true" /> Agent Card
                </a>
                <a href={REPO} target="_blank" rel="noopener noreferrer">
                  <Github size={14} aria-hidden="true" /> Source on GitHub
                </a>
              </nav>
            </div>
          ) : null}
        </div>
      </div>
    </header>
  );
}
