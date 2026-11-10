# Trustee — an escrow + arbitration agent for agent-to-agent commerce

PayPal AI Hackathon entry (deadline 2026-11-12). Solo build.

## Problem

In an agent-to-agent world, agents can negotiate anything in natural language
and settle nothing: there is no enforcement layer between "agents agree" and
"money moves." Existing protocols (AP2 mandates, x402, ACP, Visa TAP,
Mastercard Agent Pay) cover permission → commerce → settlement, but the trust
layer — escrow, delivery attestation, auto-arbitration — is the missing piece
when neither party is a human.

## Product

Three agent roles plus a platform that houses them:

1. **Buyer / Seller agents** — negotiate a digital good over a machine-readable
   offer protocol. Emission of a contract is constrained (JSON mode);
   negotiation is LLM but *never* touches money directly.
2. **Trustee agent** — the settlement agent. Checks both parties' **mandates**
   (max amount, counterparty allowlist, category, TTL) → opens escrow via
   PayPal Orders (`authorize` holds funds, does not move them) → on delivery
   + buyer attestation, `capture` settles; on reject/timeout, `void` unwinds.
   LLM proposes actions; the policy engine (mandates + state machine) is the
   hard gate — money can only move through gated tools.
3. **Arbiter agent** (stretch) — on contested delivery, an LLM judge diffs
   contract vs. deliverable and rules; human override visible but unused in demo.

**Platform (arena)** — FastAPI + one-page UI: live SSE agent transcript on the
left, AG Grid ledger tape on the right (contract/payment/state events), deal
controls. Hosted on Render → zero-setup demo URL.

## Money path (PayPal sandbox, client-credentials REST)

`Orders.create(intent=AUTHORIZE)` → `authorize` (funds held) →
`capture` (settle to seller) **or** `void` (release). Webhooks
(`PAYMENT.CAPTURE.COMPLETED` etc.) drive state transitions; every transition
is idempotent + replay-safe; deal timeout → auto-void so funds never strand;
mandate check happens *before* authorize so no money can touch a forbidden deal.

## Demo (the whole pitch, ≤3 min)

- **Run 1 (honest):** agents negotiate → contract → escrow → delivery +
  attestation → settled. Funds moved, zero human intervention.
- **Run 2 (cheat):** seller ships a junk returns export under the contracted file
  name (renamed column, 312 rows, duplicate order ids) → buyer attests reject →
  arbiter cites the contract's sha256 criterion → auto-void + refund event. Shows
  *why the trust layer matters*. The honest run's schema check (header, row count,
  unique order ids, valid refunds and dates) is the second half of the same story.

## Build order (agents first, platform last)

1. **Trustee** (this agent) — kernel: protocol, mandate engine, state
   machine, PayPal adapter, ledger. Tests green, sandbox POC.
2. **Buyer** — negotiation agent (emit constrained offers/counters, attest delivery).
3. **Seller** — negotiation agent (counter offers, deliver + attest hash).
4. **Arbiter** (stretch).
5. **Platform** — arena UI, SSE, AG Grid, Render hosting.

## Scope cuts (solo deadline)

One good at a time (no multi-deal concurrency); no Payouts/fee-split; no
Elastic/Zapier; arbiter = stretch; AG Grid in one ledger panel.

> **Since overtaken.** The build went past these cuts: `platform/service.py` hosts many concurrent deals, the
> arbiter is built, and Elastic and Zapier are optional integrations. Still cut: Payouts and fee splits. See the
> [README](../../README.md) for the current scope and [docs/PAYPAL_SCALE.md](../PAYPAL_SCALE.md) for what a PayPal
> product would add.

## Rubric mapping

- *Best Use of Agentic Commerce* — the entry is an A2A trust layer.
- *Best Use of PayPal + AI* — Orders/authorize-capture-void + webhooks +
  disputes, not a checkout button.
- *AG Grid prizes* — ledger tape.
- *Design* — coherent product, not a PoC script.
