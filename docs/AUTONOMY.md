# Autonomous payments: human-not-present purchases under a signed intent mandate

Until now every real-PayPal deal stopped at `AUTH_REQUIRED` until a person approved the order in a browser. That is the
AP2 *human-present* pattern. This document describes the *human-not-present* pattern Trustee adds: the human consents
**once**, and after that the buyer's agent pays by itself, inside limits Trustee enforces and can prove.

- [The flow](#the-flow)
- [What was adopted from AP2](#what-was-adopted-from-ap2)
- [The intent mandate](#the-intent-mandate)
- [The rules](#the-rules)
- [The record: hash chain and signed receipts](#the-record-hash-chain-and-signed-receipts)
- [Threat model](#threat-model)
- [What stays a human decision](#what-stays-a-human-decision)
- [One-time consent on the PayPal sandbox](#one-time-consent-on-the-paypal-sandbox)
- [Verifying a receipt](#verifying-a-receipt)
- [Configuration](#configuration)

## The flow

```
 once                                                        every purchase
 ----                                                        --------------
 human --signs--> intent mandate (words + limits + funding_ref)
 human --approves--> PayPal Vault consent  ->  Trustee stores the vault id (secret)

 buyer agent signs contract  ->  Trustee runs 14 rules (one `policy` ledger row each)
     all pass            -> order with payment_source.paypal.vault_id, AUTHORIZE: funds held, no click
     hard violation      -> refused, PayPal never called
     soft overrun        -> step-up: today's human approval, with the failing rule explained
 seller delivers -> buyer verifies sha256 + schema -> signed attestation
 Trustee re-checks the intent -> capture (or void) -> signed settlement receipt
```

Escrow is unchanged. Autonomy removes the *click*, not the *check*: the money is still only authorized (held) until the
buyer's own verification of the delivery passes, and a bad delivery is still voided and refunded.

## What was adopted from AP2

[AP2](https://ap2-protocol.org) (Agent Payments Protocol) defines a *human-present* and a *human-not-present* flow, and
an Intent Mandate: a signed statement of the user's constraints that lets an agent complete a purchase later without the
user. Trustee's `IntentMandate` follows that model. AP2's field names changed between its drafts and it is aimed at card
payments, so Trustee keeps the concepts and maps the names rather than copying a schema:

| AP2 Intent Mandate | Trustee `IntentMandate` |
|---|---|
| `natural_language_description` | `instruction`: the human's words, verbatim, inside the signature |
| `merchants` | `allowed_sellers` |
| `skus` | `good_ids` (plus `allowed_categories`, `keywords`) |
| `intent_expiry` | `expires_at` |
| `user_cart_confirmation_required: false` | `human_present: false`, fixed |
| `requires_refundability` | `require_escrow: true`, fixed: authorize / capture / void |
| (payment mandate / credential provider) | `funding_ref` + the PayPal Vault payment token behind it |

Added because the PayPal money path needs them: `max_price`, `max_total`, `max_purchases` per `window_seconds`,
`required_criteria` (for example the contract must commit to a deliverable `sha256`), `currency`, `issued_at`,
`version`, `intent_id`.

It is a sibling of `Mandate`, not a subclass. A `Mandate` is a per-agent spend ceiling replayed by version; an intent is
a statement about one purchase intent that must carry the human's words and name a funding source. They compose: the
classic mandate gate still runs on every deal, and the intent can only narrow what happens inside it.

## The intent mandate

Signed by the principal over canonical JSON (sorted keys, no whitespace, UTF-8, Decimals as strings, datetimes ISO 8601)
of every field except `principal_signature`. `digest()` is the sha256 of those bytes; receipts and tests refer to it.

```json
{
  "intent_id": "intent_9c1f…", "version": 1,
  "agent_id": "northwind-planner", "principal": "northwind-outfitters",
  "instruction": "Buy Tidewater's Q3 2026 returns dataset for demand planning; spend at most $175; only from Tidewater; must match the published sha256",
  "funding_ref": "fund_Qm3…",
  "currency": "USD", "max_price": "175.00", "max_total": null,
  "allowed_sellers": ["tidewater-sales"], "allowed_categories": ["data"], "good_ids": ["returns-dataset-2026q3"],
  "keywords": null, "required_criteria": ["sha256"],
  "max_purchases": null, "window_seconds": null,
  "issued_at": "2026-10-31T09:00:00", "expires_at": "2026-11-07T09:00:00",
  "require_escrow": true, "human_present": false,
  "principal_signature": "…"
}
```

**Drafting from words.** `draft_intent(instruction, llm, …)` lets a model *propose* structured limits from the sentence.
Code then keeps only what it can tie to the sentence: a price cap must be an amount written in the instruction (a
model's invented `9999` is dropped and noted), a seller must be one the caller knows, the instruction is stored
verbatim, `require_escrow` and `human_present` are forced, and the funding reference is the caller's. The result is an
unsigned `IntentDraft`: show it to the principal, who signs or discards it. The model never signs and cannot widen a
signed intent: any edit changes the digest and breaks the signature, which `POST /v1/intents` answers with `401`. With
no model (or a scripted one) the same limits are read from the sentence by plain rules. In the arena the demo principal
signs on the human's behalf and the tape says so: "one-time consent signed by the human".

## The rules

Deterministic, no model, no clock but the one passed in. `evaluate_intent` returns one result per rule; the Trustee writes
each as a `policy` ledger row with the observed and the allowed value.

| Rule | Severity | Fails when |
|---|---|---|
| `signature.principal_intent` | hard | the intent is unsigned or not signed by its principal's registered key |
| `signature.principal_agent` | hard | the buyer is not registered as a buyer under that principal, or is not the intent's agent |
| `signature.agent_contract` | hard | the buyer's signature on this contract digest is missing or invalid |
| `intent.not_revoked` | hard | the principal revoked the intent |
| `intent.not_expired` | hard | `expires_at` has passed |
| `intent.require_escrow` | hard | the intent asks for anything but escrow with no human present |
| `seller.allowed` | hard | the seller is not in `allowed_sellers` |
| `item.match` | soft | category, good id or description keywords do not match |
| `price.max_price` | soft | the price is above the intent's cap |
| `budget.max_total` | soft | spent + reserved + this price is above `max_total` |
| `velocity.window` | soft | more than `max_purchases` in `window_seconds` |
| `criteria.required` | soft | a required clause is missing, for example the contract commits to no `sha256` |
| `currency.match` | soft | the contract currency differs |
| `funding.active` | soft | the funding source is missing, not yet vaulted, revoked, or another principal's |

- All pass: **autonomous**. The order is created with `payment_source.paypal.vault_id`, no `experience_context`, and
  authorized with no payer action. No approve link exists for such an order.
- Any hard failure: **reject**. The deal is refused (`403`, the failed rules listed) and PayPal is never called.
- Only soft failures: **step-up**. The purchase falls back to today's human-present approval and the tape says which
  rule failed. The agent is never silently allowed to pay. In dry-run there is no payer, so the approval is simulated and
  the tape says that too.

At capture the Trustee re-runs the rules (funding aside, the funds are already held). An intent revoked or expired
between escrow and delivery parks the deal in `DISPUTED` with the funds held, for an operator. It neither captures
without authority nor refunds a seller who delivered.

## The record: hash chain and signed receipts

**Hash-chained ledger.** Every event carries `prev_hash` and `hash = sha256(canonical({prev_hash, ts, deal_id, actor,
kind, payload}))`. There is one chain per deal, starting at `0000…`. `Ledger.verify_chain(deal_id)` and
`GET /v1/deals/{id}/events` (which returns both hashes per event) let anyone re-derive the chain; editing, dropping,
inserting or reordering an event is detected. The chain is tamper-*evident*, not tamper-proof: whoever controls the
database can rewrite a whole chain, which is why the receipt below is signed and why you should pin its head (see the
threat model).

**Signed settlement receipt.** Issued when a deal is `SETTLED` or `VOIDED`: deal id, outcome, amount, currency, buyer,
seller, contract digest, intent-mandate digest, `human_present`, delivery sha256, every policy rule result, the PayPal
order / authorization / capture ids, the deal's ledger chain head and event count at settlement, `issued_at`, Trustee's
public key and its Ed25519 signature. It contains no vault id, funding reference or payer detail. Served at
`GET /v1/deals/{id}/receipt` and as the A2A `settlement-receipt` artifact.

Trustee's signing key comes from `TRUSTEE_SIGNING_KEY` (base64 of the 32-byte private key), else is generated once and
kept in the store. Its public half is at `GET /.well-known/trustee-key.json` and in the Agent Card.

## Threat model

Assets: the buyer's PayPal funds; the seller's goods; the integrity of the record. Trust anchors: the principal's key
(consent), the buyer's key (contract), the Trustee key (receipt).

| Threat | What stops it | What does not |
|---|---|---|
| **Compromised buyer agent** (its key or process is taken over) | It can only create contracts that pass the intent: capped price, named seller, named item, required sha256, count and total limits, expiry. Funds are held and captured only after the buyer's attestation verifies the bytes; the principal revokes the intent (`/v1/intents/{agent}/revoke`) and the capture-time re-check holds anything pending. | It can still spend *up to the limits* to the named seller, and it can attest "accept" for a bad delivery, since the buyer's verdict is the settlement trigger. Keep limits small, set `max_total` and a velocity limit, and pay attention to alerts. |
| **Malicious seller** | Wrong item, wrong hash or junk data: the delivery is verified against the contract sha256 and schema, the arbiter rules on the clause, the hold is voided and the buyer keeps the money (the `cheat` scenario). A seller counter above the cap steps up instead of being paid (`overreach`). A seller outside `allowed_sellers` is refused. | A seller can still deliver something that satisfies the contract's machine-checkable clauses but is not what the human wanted. Write criteria that a check can decide. |
| **Prompt injection into the negotiating model** | The model never moves money, picks amounts, parties or endpoints, or overrides a policy decision. The price a contract carries is checked by `price.max_price` whatever the chat said; the contract is signed by code. A model proposing limits in `draft_intent` is validated against the human's sentence and cannot sign. | An injected buyer model can still *negotiate badly* inside the cap. The cap is the loss bound. |
| **Replayed mandate** | `issued_at` must be newer than the intent on file (`409`), a revoked intent's exact digest is refused, intents expire, and the contract digest (which includes `deal_id`) is what the buyer signs. An old signed intent cannot be re-published after a newer one. | A *valid, unexpired, unrevoked* intent is a bearer grant to its agent: protect `expires_at`. |
| **Stolen agent key** | Same as a compromised agent. The attacker is bounded by the intent, and the principal can revoke the intent or the funding source without the agent key. Registration binds the agent to its principal, so the stolen key cannot widen its own intent. | Until revoked, the thief can spend within the limits. |
| **Stolen principal key** | None beyond detection: this key *is* consent. The thief can sign a new intent for any registered buyer agent of that principal and name the principal's funding. Keep this key offline or in an HSM; use short expiries. | This is the root of trust. Revoke the funding source at PayPal (the buyer's PayPal account, outside Trustee) if it leaks. |
| **Trustee operator** | The operator cannot forge a receipt without the Trustee key, cannot change a signed intent or contract, and a deal's events are hash-chained, so a later edit of the history is detectable by anyone who kept a receipt (its `ledger_head` pins the chain). The vault id is never returned by any endpoint, log line, ledger row or receipt. | The operator holds the vault id and the receipt key, runs the sweeper and decides disputes. A malicious operator could create orders against the vault within PayPal's limits, or rewrite the database. Mitigate with PayPal-side limits on the saved payment method, pinning receipt heads externally, and running your own Trustee. |
| **Vault id exposure** | Server-side only, behind `Store.get_vault_id`; funding sources are bound to their principal; responses and receipts carry the opaque `funding_ref`. | The id sits in the SQLite file in clear text. Protect the disk; the id only works for the PayPal app that vaulted it. |

## What stays a human decision

- **The consent itself**: signing the intent and approving the PayPal Vault screen. Both are the principal's act.
- **Anything outside the instruction**: a price above the cap, an unnamed item, too many purchases, a lapsed funding
  source. These step up to a human; the agent cannot talk its way past them.
- **Disputes the arbiter cannot decide objectively**: held for an operator (`POST /v1/deals/{id}/resolve`).
- **Whether the criteria are good enough**: Trustee enforces what the contract says, not what the human meant.
- **Revocation and key custody.**

## One-time consent on the PayPal sandbox

Prerequisites: a sandbox app with the **Vault** feature (the `vault` scope), a sandbox *personal* (buyer) account, a
sandbox *business* (seller) account in `PAYPAL_SELLER_EMAIL`, and the server reachable from your browser with
`TRUSTEE_PUBLIC_URL` set (PayPal sends the payer back to `{TRUSTEE_PUBLIC_URL}/v1/funding/return`).

1. Start Trustee with `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`, `TRUSTEE_PUBLIC_URL` and `TRUSTEE_DB_PATH` set.
2. Register the principal and the buyer agent (see `examples/a2a_deal.py` or `TrusteeClient`).
3. Ask for a funding source. The principal signs; Trustee asks PayPal for a setup token and returns the link:
   ```python
   client.start_funding("northwind-outfitters", principal_key, funding_ref="fund-northwind")
   # {"funding_ref": "fund-northwind", "status": "consent_required", "approve_url": "https://www.sandbox.paypal.com/agreements/approve?…"}
   ```
4. Open `approve_url` in a browser, sign in as the sandbox **buyer** and approve. PayPal redirects to
   `/v1/funding/return`; Trustee exchanges the setup token for a payment token and stores it. `GET /v1/funding/fund-northwind`
   now says `active`. This is the only click.
5. Publish the intent: `client.publish_intent(IntentMandate(… funding_ref="fund-northwind" …), principal_key)`.
6. Open deals as usual. They are escrowed with no approval step; the tape shows the 14 `policy` rows.

The arena's own sandbox runs use a pre-registered source: obtain a payment token id once (steps 3 and 4 above, or the
PayPal Vault API directly with your own credentials) and set it as `PAYPAL_DEMO_VAULT_ID` in your host's secret store.
It is a secret: never put it in a file, a log or a commit. Unset, sandbox arena runs ask for approval as before, and
dry-run never needs it (its funding source is fake end to end).

## Verifying a receipt

```bash
python -m trustee.verify receipt.json \
    --key https://your-trustee/.well-known/trustee-key.json \   # or the base64 public key, pinned
    --events events.json \                                      # GET /v1/deals/{id}/events?limit=1000
    --contract contract.json --intent intent.json --principal-key <principal public key>
```

It checks the receipt signature, that the contract and intent documents hash to the digests the receipt commits to, that
the intent carries the principal's signature, that the events form an unbroken hash chain, that the receipt's
`ledger_head` is the hash of the event at position `ledger_events`, and that an unattended payment recorded no failed
rule. Exit status `0` only when every requested check passes. `POST /v1/receipts/verify` runs the same checks on the
server, which is convenient but asks you to trust that server; prefer the offline command.

## Configuration

| Var | Default | Meaning |
|---|---|---|
| `TRUSTEE_SIGNING_KEY` | generated, kept in the store | base64 Ed25519 private key for receipts; set it on any host whose store is not persistent |
| `PAYPAL_DEMO_VAULT_ID` | unset | secret Vault payment-token id so the arena's sandbox runs are autonomous |
| `TRUSTEE_PUBLIC_URL` | request URL | host PayPal returns the payer to after the vault consent |
