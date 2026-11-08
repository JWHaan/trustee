import { useEffect, useRef, useState } from "react";
import { BookOpen, ChevronDown, Github, IdCard, ShieldCheck } from "lucide-react";
import type { Health } from "./api";

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
}

export function Header({ health }: Props) {
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

  return (
    <header className="top">
      <div className="brand">
        <span className="logo" aria-hidden="true">
          <ShieldCheck size={22} strokeWidth={2} />
        </span>
        <div>
          <h1>Trustee</h1>
          <p>Holds the payment until the delivery matches the deal</p>
        </div>
      </div>

      <div className="top-right">
        {health ? (
          <span className={`env-pill ${health.paypal === "sandbox" ? "sandbox" : ""}`}>
            {health.paypal === "sandbox" ? "PayPal sandbox: test money" : "Dry run: no PayPal calls"}
          </span>
        ) : null}

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
