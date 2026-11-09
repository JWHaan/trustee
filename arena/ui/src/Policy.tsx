import { useState } from "react";
import { Check, Minus, X } from "lucide-react";
import type { Row } from "./api";
import { deriveChecks, derivePolicy, type CheckItem } from "./derive";

const LABEL: Record<string, string> = {
  "signature.principal_intent": "Human signed the instruction",
  "signature.principal_agent": "Human vouches for this agent",
  "signature.agent_contract": "Agent signed this contract",
  "intent.not_revoked": "Instruction not revoked",
  "intent.not_expired": "Instruction not expired",
  "intent.require_escrow": "Funds held until delivery is checked",
  "seller.allowed": "Seller is the one named",
  "item.match": "Item matches the instruction",
  "price.max_price": "Price within the limit",
  "budget.max_total": "Within the total budget",
  "velocity.window": "Not too many purchases",
  "criteria.required": "Contract commits to the file's hash",
  "currency.match": "Currency matches",
  "funding.active": "Payment method still active",
};

function Line({ item }: { item: CheckItem }) {
  return (
    <li className={item.advisory ? "adv" : item.ok ? "ok" : "no"}>
      {item.advisory ? (
        <Minus size={14} strokeWidth={3} aria-hidden="true" />
      ) : item.ok ? (
        <Check size={14} strokeWidth={3} aria-hidden="true" />
      ) : (
        <X size={14} strokeWidth={3} aria-hidden="true" />
      )}
      <span className="rname">{item.label}</span>
      <span className="rnote" title={item.detail}>
        {item.detail}
      </span>
      <span className="sr-only">{item.advisory ? "advice only, not counted" : item.ok ? "passed" : "failed"}</span>
    </li>
  );
}

export const ruleLabel = (rule: string) => LABEL[rule] ?? rule;

/** How many checks failed, for the tab label; null before anything was checked. */
export function failedChecks(rows: Row[]): { failed: number; total: number } | null {
  const items = deriveChecks(rows, ruleLabel)
    .flatMap((g) => g.items)
    .filter((i) => !i.advisory);
  return items.length ? { failed: items.filter((i) => !i.ok).length, total: items.length } : null;
}

/**
 * What Trustee checked, in three groups. Failures are always shown; passing checks in a group fold into one line
 * until asked for.
 */
export function PolicyChecklist({ rows }: { rows: Row[] }) {
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const groups = deriveChecks(rows, ruleLabel);
  const consent = derivePolicy(rows).consent;
  if (!groups.length) {
    return <p className="checks-empty">Nothing has been checked yet. Run a deal to see each check.</p>;
  }
  return (
    <div className="checks">
      {consent ? <p className="checks-consent">{consent.note}</p> : null}
      {groups.map((g) => {
        const failed = g.items.filter((i) => !i.ok && !i.advisory);
        const passed = g.items.filter((i) => i.ok && !i.advisory);
        const advice = g.items.filter((i) => i.advisory);
        const shown = open[g.title];
        return (
          <div key={g.title} className="checks-group">
            <h3>{g.title}</h3>
            {failed.length ? (
              <ul className="check-list">
                {failed.map((i) => (
                  <Line key={i.id} item={i} />
                ))}
              </ul>
            ) : null}
            {passed.length === 1 && !failed.length ? (
              <ul className="check-list">
                <Line item={passed[0]} />
              </ul>
            ) : passed.length ? (
              <>
                <button
                  type="button"
                  className="linkish"
                  aria-expanded={Boolean(shown)}
                  onClick={() => setOpen((o) => ({ ...o, [g.title]: !o[g.title] }))}
                >
                  {failed.length ? `${passed.length} other checks passed` : `All ${passed.length} checks passed`}
                  {shown ? " (hide)" : " (show)"}
                </button>
                {shown ? (
                  <ul className="check-list">
                    {passed.map((i) => (
                      <Line key={i.id} item={i} />
                    ))}
                  </ul>
                ) : null}
              </>
            ) : null}
            {advice.length ? (
              <ul className="check-list">
                {advice.map((i) => (
                  <Line key={i.id} item={i} />
                ))}
              </ul>
            ) : null}
          </div>
        );
      })}
    </div>
  );
}
