import { useState } from "react";
import { Check, ChevronDown, FileSignature, ShieldCheck, UserCheck, X } from "lucide-react";
import type { Row } from "./api";
import { derivePolicy } from "./derive";

const LABEL: Record<string, string> = {
  "signature.principal_intent": "Human signed the intent",
  "signature.principal_agent": "Human vouches for this agent",
  "signature.agent_contract": "Agent signed this contract",
  "intent.not_revoked": "Intent not revoked",
  "intent.not_expired": "Intent not expired",
  "intent.require_escrow": "Funds held until delivery is verified",
  "seller.allowed": "Seller is the one named",
  "item.match": "Item matches the instruction",
  "price.max_price": "Price within the limit",
  "budget.max_total": "Within the total budget",
  "velocity.window": "Not too many purchases",
  "criteria.required": "Contract commits to the sha256",
  "currency.match": "Currency matches",
  "funding.active": "Funding source still active",
};

/**
 * The intent mandate's rules as a checklist: one line per rule the Trustee evaluated, read from the same
 * `policy` ledger rows the tape shows. Nothing here is decided in the browser.
 *
 * Collapsed it is one verdict line, so the transcript keeps its height; it opens by itself when a rule fails.
 */
export function PolicyChecklist({ rows }: { rows: Row[] }) {
  const [toggled, setToggled] = useState<boolean | null>(null);
  // A new run clears the rows; forget the last run's open/closed choice with it.
  if (!rows.length && toggled !== null) setToggled(null);
  const p = derivePolicy(rows);
  if (!p.consent && !p.rules.length) return null;
  const open = toggled ?? p.failed > 0;
  const verdict =
    p.authority === "intent"
      ? { icon: <ShieldCheck size={15} aria-hidden="true" />, text: "Paid with no human click", tone: "good" }
      : p.authority === "stepup"
        ? { icon: <UserCheck size={15} aria-hidden="true" />, text: "Outside the instruction — a human approves", tone: "warn" }
        : p.decision?.result === "reject"
          ? { icon: <X size={15} aria-hidden="true" />, text: "Refused by the intent mandate", tone: "bad" }
          : { icon: <FileSignature size={15} aria-hidden="true" />, text: "Waiting for the contract", tone: "idle" };
  return (
    <section className="panel policy-panel" aria-label="intent mandate checks">
      <button
        type="button"
        className="panel-head policy-head"
        aria-expanded={open}
        aria-controls="policy-body"
        onClick={() => setToggled(!open)}
      >
        <span>Intent mandate</span>
        <span className={`policy-verdict ${verdict.tone}`}>
          {verdict.icon}
          <span className="vtext">{verdict.text}</span>
        </span>
        <span className="count">{p.rules.length ? `${p.passed}/${p.rules.length} rules pass` : "signed, not yet used"}</span>
        <ChevronDown size={14} aria-hidden="true" className={open ? "flip" : ""} />
      </button>
      {open ? (
        <div className="policy-body" id="policy-body">
          {p.consent ? (
            <p className="policy-consent">
              <FileSignature size={14} aria-hidden="true" /> {p.consent.note}
              {p.limits ? <em> {p.limits.note}</em> : null}
            </p>
          ) : null}
          {p.rules.length ? (
            <ul className="policy-list">
              {p.rules.map((r) => (
                <li key={r.id} className={r.result === "pass" ? "ok" : "no"} title={r.note}>
                  {r.result === "pass" ? <Check size={13} strokeWidth={3} aria-hidden="true" /> : <X size={13} strokeWidth={3} aria-hidden="true" />}
                  <span className="rname">{LABEL[r.rule] ?? r.rule}</span>
                  <span className="rnote">{r.note.replace(/^[^:]+: (pass|FAIL) /, "")}</span>
                  <span className="sr-only">{r.result === "pass" ? "passed" : "failed"}</span>
                </li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}
