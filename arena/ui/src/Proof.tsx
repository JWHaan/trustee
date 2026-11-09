import { Check, Cpu, Minus, Sparkles, UserCheck, X } from "lucide-react";
import type { Row, Snapshot } from "./api";
import { ruleLabel } from "./Policy";
import { deriveChecks, derivePolicy } from "./derive";

/**
 * "How Trustee knows": the evidence behind the verdict, next to it rather than three clicks down. Each line is one
 * question a judge would ask (is it the right file? did it have the right shape? was it really them? were they
 * allowed?), answered from the deal's own record: the snapshot's evidence and the ledger rows. Nothing is decided
 * here; it shows what was already decided and on what.
 */

/** ok passed, no failed, person passed only because a person approved it, skip never reached. */
type Mark = "ok" | "no" | "person" | "skip";

interface Item {
  key: string;
  mark: Mark;
  title: string;
  /** The plain answer. */
  line: string;
  /** The raw proof: hashes, column names. Shown in monospace under the answer. */
  proof?: { label: string; value: string; bad?: boolean }[];
}

const short = (sha: string | undefined) => (sha ? `${sha.slice(0, 12)}…` : "");

/** "190.00" -> "$190.00", "<= 175" -> "≤ $175": a rule's raw values, read as money when they are amounts. */
const amount = (v: string) =>
  v
    .trim()
    .replace("<=", "≤")
    .replace(">=", "≥")
    .replace(/(^|\s)(\d+(?:\.\d+)?)$/, "$1$$$2");

/** The column list the contract asks for, read from its acceptance criteria ("schema: a, b, c; ..."). */
function contractColumns(criteria: string | undefined): string[] {
  const m = /schema:\s*([^;]+)/i.exec(criteria ?? "");
  return m ? m[1].split(",").map((c) => c.trim()).filter(Boolean) : [];
}

function fileItem(snap: Snapshot): Item | null {
  const ev = snap.attest?.evidence;
  const expected = ev?.sha256_expected || snap.contract?.sha256;
  const received = ev?.sha256_received;
  if (!expected || !received) return null;
  const ok = (ev?.sha256_check ?? (expected === received ? "match" : "mismatch")) === "match";
  return {
    key: "file",
    mark: ok ? "ok" : "no",
    title: "Is it the file the contract names?",
    line: ok
      ? "Yes. Its fingerprint (SHA-256) is the one both agents signed."
      : "No. Its fingerprint (SHA-256) is not the one both agents signed, so it is a different file.",
    proof: ok
      ? [{ label: "contract and delivery", value: short(expected) }]
      : [
          { label: "contract", value: short(expected) },
          { label: "delivered", value: short(received), bad: true },
        ],
  };
}

function shapeItem(snap: Snapshot): Item | null {
  const raw = snap.attest?.evidence?.checker;
  if (!raw) return null;
  const text = raw.replace(/^failed:\s*/i, "");
  const failed = /^failed/i.test(raw) || /mismatch|missing|blank|not within/i.test(text);
  const title = "Does it have the promised columns and rows?";

  const rows = /(\d[\d,]*)\s+rows/i.exec(text)?.[1];
  const header = /header is\s+(.+)$/i.exec(text)?.[1];
  const want = contractColumns(snap.contract?.acceptance_criteria);

  if (!failed) {
    const n = rows ? Number(rows.replace(/,/g, "")).toLocaleString("en-US") : "";
    return {
      key: "shape",
      mark: "ok",
      title,
      line: want.length
        ? `Yes. All ${want.length} columns the contract lists${n ? `, and ${n} rows` : ""}.`
        : `Yes. ${text.charAt(0).toUpperCase() + text.slice(1)}.`,
    };
  }

  if (header && want.length) {
    const got = header.split(",").map((c) => c.trim());
    const missing = want.filter((c) => !got.includes(c));
    const extra = got.filter((c) => !want.includes(c));
    if (missing.length || extra.length) {
      return {
        key: "shape",
        mark: "no",
        title,
        line: `No. ${missing.length === 1 ? "A column the contract requires is missing" : "Columns the contract requires are missing"}.`,
        proof: [
          ...(missing.length ? [{ label: "contract needs", value: missing.join(", ") }] : []),
          ...(extra.length ? [{ label: "file has instead", value: extra.join(", "), bad: true }] : []),
        ],
      };
    }
  }
  return { key: "shape", mark: "no", title, line: `No. ${text.charAt(0).toUpperCase() + text.slice(1)}.` };
}

/** What a failed rule means, said as the problem rather than the rule's name. */
const BROKEN: Record<string, string> = {
  "price.max_price": "The price is over the limit the human signed",
  "budget.max_total": "It would go over the human's total budget",
  "seller.allowed": "This seller isn't the one the human named",
  "item.match": "This isn't the item the human asked for",
  "intent.not_revoked": "The human has revoked the agent's limits",
  "intent.not_expired": "The human's instruction has expired",
  "velocity.window": "The agent has made too many purchases",
  "currency.match": "The currency isn't the one the human allowed",
  "funding.active": "The payment method is no longer active",
  "criteria.required": "The contract doesn't commit to the file's fingerprint",
  "intent.require_escrow": "The payment wouldn't be held until delivery",
};

function limitsItem(rows: Row[]): Item | null {
  const title = "Were they allowed to make this deal?";
  const policy = derivePolicy(rows);
  const mandates = rows.filter((r) => r.kind === "mandate" && r.note);
  if (!policy.rules.length && !mandates.length) return null;

  const failedRule = policy.rules.find((r) => r.result === "fail");
  const failedMandate = mandates.find((r) => !r.note.startsWith("mandates OK"));
  if (!failedRule && !failedMandate) {
    return {
      key: "limits",
      mark: "ok",
      title,
      // Same count as the step bar and the Checks tab: each signed limit plus the both-sides mandate check.
      line: policy.rules.length
        ? `Yes. All ${policy.rules.length + mandates.length} spending checks passed, including every limit the human signed.`
        : "Yes. Both agents are within their spending limits.",
    };
  }
  if (failedRule) {
    const why = BROKEN[failedRule.rule] ?? `${ruleLabel(failedRule.rule)}: failed`;
    const m = /\(([^()]+?) vs ([^()]+)\)\s*$/.exec(failedRule.note);
    // Only the price and budget rules compare amounts; a purchase count must not grow a dollar sign.
    const isMoney = failedRule.rule === "price.max_price" || failedRule.rule === "budget.max_total";
    const fmt = (v: string) => (isMoney ? amount(v) : v.trim().replace("<=", "≤").replace(">=", "≥"));
    const proof = m
      ? [
          { label: "this deal", value: fmt(m[1]), bad: true },
          { label: "the limit", value: fmt(m[2]) },
        ]
      : undefined;
    if (policy.authority === "stepup") {
      return {
        key: "limits",
        mark: "person",
        title,
        line: `Only with a person's OK. ${why}, so Trustee asked a person to approve before any money was held.`,
        proof,
      };
    }
    return { key: "limits", mark: "no", title, line: `No. ${why}, so Trustee refused it.`, proof };
  }
  return { key: "limits", mark: "no", title, line: `No. ${failedMandate!.note.replace(/[.\s]*$/, ".")}` };
}

function groupItem(rows: Row[], group: string, key: string, title: string, noun: string): Item | null {
  const g = deriveChecks(rows, ruleLabel).find((x) => x.title === group);
  if (!g) return null;
  const bad = g.items.filter((i) => !i.ok);
  if (!bad.length) {
    return { key, mark: "ok", title, line: `Yes. ${g.items.length === 1 ? `The ${noun} check passed` : `All ${g.items.length} ${noun} checks passed`}.` };
  }
  // The raw note names internal agent ids; the label already says whose signature it was.
  const line =
    key === "sigs"
      ? `No. ${bad[0].label} doesn't match the key registered for that agent.`
      : `No. ${bad[0].label}: ${bad[0].detail}`.replace(/[.\s]*$/, ".");
  return { key, mark: "no", title, line };
}

function skipped(key: string, title: string): Item {
  return { key, mark: "skip", title, line: "Not checked: the deal stopped before this point." };
}

function MarkIcon({ mark }: { mark: Mark }) {
  if (mark === "ok") return <Check size={14} strokeWidth={3} aria-hidden="true" />;
  if (mark === "no") return <X size={14} strokeWidth={3} aria-hidden="true" />;
  if (mark === "person") return <UserCheck size={14} strokeWidth={2.5} aria-hidden="true" />;
  return <Minus size={14} aria-hidden="true" />;
}

const SPOKEN: Record<Mark, string> = { ok: "passed", no: "failed", person: "approved by a person", skip: "not checked" };

export function Proof({ rows, snap }: { rows: Row[]; snap: Snapshot }) {
  const stoppedEarly = snap.summary?.status === "refused" || snap.summary?.status === "cancelled";
  const orSkip = (item: Item | null, key: string, title: string) =>
    item ?? (stoppedEarly ? skipped(key, title) : null);
  const shown = [
    groupItem(rows, "Signatures", "sigs", "Was it really them?", "signature"),
    orSkip(limitsItem(rows), "limits", "Were they allowed to make this deal?"),
    orSkip(fileItem(snap), "file", "Is it the file the contract names?"),
    shapeItem(snap),
  ].filter((i): i is Item => i !== null);
  if (!shown.length) return null;

  return (
    <section className="proof" aria-labelledby="proof-title">
      <h3 id="proof-title">How Trustee knows</h3>
      <ul className="proof-list">
        {shown.map((it) => (
          <li key={it.key} className={`proof-item ${it.mark}`}>
            <span className="proof-mark">
              <MarkIcon mark={it.mark} />
            </span>
            <div className="proof-body">
              <p className="proof-q">
                {it.title}
                <span className="sr-only"> ({SPOKEN[it.mark]})</span>
              </p>
              <p className="proof-a">{it.line}</p>
              {it.proof ? (
                <dl className="proof-raw">
                  {it.proof.map((p) => (
                    <div key={p.label} className={p.bad ? "bad" : ""}>
                      <dt>{p.label}</dt>
                      <dd>{p.value}</dd>
                    </div>
                  ))}
                </dl>
              ) : null}
            </div>
          </li>
        ))}
      </ul>

    </section>
  );
}

/**
 * Who made the call, always on screen: while a deal runs it is the rule, once it finishes it is what happened. An
 * objective breach, a pass or a refusal is code; only a dispute with no objective breach reaches the model, and then
 * it can only cite clauses already in the contract.
 */
export function Decider({ snap }: { snap: Snapshot | null }) {
  const aiJudged = snap?.ruling?.objective === false;
  return (
    <p className={`decider ${aiJudged ? "ai" : "code"}`}>
      {aiJudged ? <Sparkles size={16} aria-hidden="true" /> : <Cpu size={16} aria-hidden="true" />}
      <span>
        {aiJudged ? (
          <>
            <b>An AI judged this dispute,</b> and it could only cite clauses already in the contract. Anything else
            keeps the money held for a person.
          </>
        ) : (
          <>
            <b>Trustee&apos;s code decides, not AI.</b> The AI agents only negotiate the price; nothing they say can
            move the money.
          </>
        )}
      </span>
    </p>
  );
}
