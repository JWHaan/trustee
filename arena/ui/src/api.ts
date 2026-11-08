export type Mode = "auto" | "live" | "scripted";

export interface Row {
  id: string;
  seq: number;
  ts: string;
  deal: string;
  actor: string;
  kind: string;
  event: string;
  state_from: string;
  state_to: string;
  price: string;
  verdict: string;
  paypal_op: string;
  order_id: string;
  sha: string;
  note: string;
  signer: string;
  check: "verified" | "rejected" | "";
  link: string;
  wait_s: string;
  /** Policy rows: the intent-mandate rule id and its result ("pass" | "fail", or the decision on a summary row). */
  rule: string;
  result: string;
  /** Receipt rows: who authorized the payment ("intent_mandate" = no human click, "human" = approved by a person). */
  authority: "" | "intent_mandate" | "human";
}

export interface Snapshot {
  run_id: string;
  deal_id: string | null;
  scenario: string;
  mode: Mode;
  effective_mode: "live" | "scripted" | null;
  state: string;
  price: string | null;
  turns: number;
  contract: Record<string, string> | null;
  escrow: { order_id: string; authorization_id: string | null; status: string; approve_url: string | null } | null;
  ruling: { decision: string; clause: string; reasoning: string; objective?: boolean } | null;
  attest: { verdict: string; reason: string; evidence: Record<string, string> } | null;
  error: string | null;
  fallback_from: string | null;
  outcome: string | null;
  events: number;
  /** Trustee's Ed25519-signed settlement receipt, once the deal is finished. */
  receipt?: Receipt | null;
  /** The receipt checked offline (signature + ledger hash chain) at the end of the run. */
  receipt_verified?: boolean | null;
  human_present?: boolean | null;
  summary?: Summary;
}

/** What happened, in plain words: written by the server (trustee.summary), the same text agents get from the API. */
export interface Summary {
  status: "paid" | "partly_paid" | "refunded" | "cancelled" | "refused" | "on_hold" | "awaiting_approval" | "in_progress";
  tone: "good" | "bad" | "warn" | "neutral";
  headline: string;
  reasons: string[];
  next_step: string | null;
  money: { paid_to_seller: string; returned_to_buyer: string; held: string };
  currency: string;
}

export interface Receipt {
  deal_id: string;
  outcome: "settled" | "voided";
  amount: string;
  currency: string;
  human_present: boolean;
  intent_mandate_digest: string | null;
  ledger_head: string;
  signature: string;
}

export interface ScenarioInfo {
  name: string;
  title: string;
  blurb: string;
  ask: string;
  floor: string;
  budget: string;
  opening: string;
  cheat: boolean;
  attack: string | null;
  flow: string;
  instruction?: string | null;
}

export interface Health {
  ok: boolean;
  paypal: "sandbox" | "dry-run";
}

export async function fetchHealth(): Promise<Health> {
  const res = await fetch("/healthz");
  if (!res.ok) throw new Error(`healthz: ${res.status}`);
  return (await res.json()) as Health;
}

export async function fetchScenarios(): Promise<ScenarioInfo[]> {
  const res = await fetch("/api/scenarios");
  if (!res.ok) throw new Error(`scenarios: ${res.status}`);
  const body = await res.json();
  return body.scenarios as ScenarioInfo[];
}

export async function startRun(scenario: string, mode: Mode): Promise<string> {
  const res = await fetch("/api/runs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ scenario, mode }),
  });
  if (!res.ok) throw new Error(`start: ${res.status}`);
  const body = await res.json();
  return body.run_id as string;
}

interface StreamHandlers {
  onRow: (row: Row) => void;
  onDone: (snap: Snapshot) => void;
}

/**
 * Consume the run's SSE stream with fetch rather than EventSource: the server
 * closes the stream after `done`, and EventSource treats a close as a reason
 * to reconnect, which would replay the whole tape a second time.
 */
export async function streamRun(runId: string, { onRow, onDone }: StreamHandlers, signal: AbortSignal) {
  const res = await fetch(`/api/runs/${runId}/events`, { signal });
  if (!res.ok || !res.body) throw new Error(`stream: ${res.status}`);

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    let split: number;
    while ((split = buffer.indexOf("\n\n")) >= 0) {
      const frame = buffer.slice(0, split);
      buffer = buffer.slice(split + 2);

      let name = "msg";
      let data = "";
      for (const line of frame.split("\n")) {
        if (line.startsWith("event: ")) name = line.slice(7).trim();
        else if (line.startsWith("data: ")) data += line.slice(6);
      }
      if (!data) continue; // keepalive comment
      const payload = JSON.parse(data);
      if (name === "done") onDone(payload as Snapshot);
      else onRow(payload as Row);
    }
  }
}

export async function fetchSnapshot(runId: string): Promise<Snapshot> {
  const res = await fetch(`/api/runs/${runId}`);
  if (!res.ok) throw new Error(`snapshot: ${res.status}`);
  return (await res.json()) as Snapshot;
}
