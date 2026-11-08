# Trustee at PayPal scale

What problem this project addresses, what the demo proves and what it does not, and what it would take to turn it
into a PayPal product. Facts about this repository are checked against the code. Statements about PayPal's
products are marked **(confirm)** where the current limits or availability should be checked with PayPal before
anyone relies on them.

## 1. The problem

Software agents are starting to buy things from other software agents: datasets, API calls, generated code,
research and ad inventory. The protocols that exist answer three questions:

| Layer | Question | Examples |
|---|---|---|
| Authority | Is this agent allowed to spend, and how much? | AP2 mandates, card-network agent tokens |
| Checkout | How does an agent pay a merchant? | ACP, x402, PayPal's agent toolkit |
| Settlement | How does the money move? | Cards, PayPal, stablecoins |

None of them answers a fourth question: **should this payment complete, given what was delivered?** Between two
humans that is handled after the fact: someone opens a dispute, a person reviews it, and the money moves back weeks
later. Between two agents, nobody is watching, the volume is high and the goods are usually digital. A dispute
process built for humans is too slow and too costly for this.

Trustee is that fourth layer: **conditional settlement**.

1. **Authority before money.** Both parties' signed mandates, and optionally the human's signed intent, are checked
   by deterministic code before PayPal is called (`mandate.py`, `intent.py`). An LLM can propose, but it cannot
   move money.
2. **Hold, then decide.** The buyer's money is held as a PayPal authorization (`paypal.py`). It is captured only
   when delivery meets the signed acceptance criteria. Otherwise the hold is voided, or only the valid share is
   captured (`partial.py`).
3. **Evidence, not opinions.** Every step is signed (Ed25519), recorded on a per-deal hash-chained ledger
   (`ledger.py`) and summarised in a signed receipt (`receipts.py`) that anyone can verify offline. A disagreement
   is settled by comparing the deliverable to the contract, not by reading two stories.

The narrow claim: **for digital goods whose acceptance can be checked by a machine, the dispute can be resolved
before the money moves, in seconds, without a person.** That is a new product surface for PayPal, not a feature
of checkout.

## 2. What the demo proves, and what it does not

**Proven in code and tests (dry-run and mocked PayPal):**

- The mandate and intent gates refuse forged, revoked, expired, over-budget and wrong-counterparty deals before
  any PayPal request (`forged` and `overreach` scenarios).
- Authorize, capture, void, reauthorize and partial capture follow a guarded state machine (`state.py`). Repeated
  transitions and webhook redeliveries are no-ops, and PayPal requests carry deterministic idempotency keys
  (`trustee-<deal>-<op>`).
- Human-not-present payments: one PayPal Vault consent plus one signed intent mandate, with 14 rules checked on
  every purchase and re-checked at capture.
- Independent agents can use it over signed REST (`/v1`) and A2A JSON-RPC, with push notifications.
- Restarts do not strand holds: deals reload from SQLite and the sweeper voids any deal past its deadline.

**Not proven:**

- **No real PayPal run yet.** Every PayPal path is tested against mocked HTTP. Running `examples/sandbox_poc.py`
  and the webhook path against the sandbox is the first job (README, Status).
- **Delivery is not verified by a neutral party.** See §4.1. This is the main design issue for scale.
- One process, SQLite, in-memory locks. Fine for a demo; §4.5 covers what changes.

## 3. Why PayPal

- **PayPal holds the money, not Trustee.** An authorization is a hold on the buyer's PayPal funding, and a capture
  pays the payee directly. Trustee only tells PayPal what to do. That is the main reason this design can work
  without Trustee holding customer funds itself. Escrow and money-transmission rules still need legal review in
  each market **(confirm)**.
- **Both sides of the deal are already on PayPal.** Escrow needs a funded buyer and a payable seller. PayPal has
  consumer wallets and Vault for buyers, merchant accounts for sellers, and Payouts and Hyperwallet for many small
  sellers.
- **The building blocks exist.** Orders v2 AUTHORIZE, Vault v3 payment tokens, webhooks, the Disputes API, Partner
  Referrals for onboarding sellers, and platform fees for a take rate. Trustee uses the first three today.
- **Disputes are expensive.** Every deal resolved at delivery time, with signed evidence, is a dispute PayPal never
  has to handle by hand.

## 4. What breaks first at scale, in priority order

### 4.1 Fair exchange: the buyer gets the file before the decision

Today the seller sends the file to the buyer, the buyer runs the checks and reports the result (`Attest.evidence`),
and the arbiter treats a reported `sha256_check: mismatch` or a failed checker as an objective breach
(`agents/arbiter.py` `_objective_breach`, `agents/trustee.py` `_arbitrate`). Trustee never checks that report
against the hashes it already holds.

Reproduced against the code at the time: the contract commits to hash `H`, the seller delivers and signs hash `H`,
the buyer rejects with `{"sha256_check": "mismatch"}`, and the deal ends `VOIDED`. **The buyer keeps a correct file
and gets the money back.** In the demo the buyer is honest. At PayPal scale this is the first exploit anyone would
try.

Fix it in two steps:

1. **Done, small change:** before ruling on a hash-mismatch claim, Trustee compares the contract's `sha256` with the
   hash the seller signed in `Delivery` (`Trustee._contradicted_hash_claim`). If they match, the buyer's claim is
   contradicted: the deal goes to `DISPUTED` with the funds still held, the ruling is `uphold` and the deal is
   flagged `needs_attention` for a human. This closes the free-file-by-false-claim case for hash criteria.
   **Trade-off:** a seller who signs a false hash is now caught by that human review instead of automatically.
   If nobody acts within the dispute window, the existing rule for an upheld dispute applies and the seller is
   paid. Step 2 removes the need to choose.
2. **For scale: Trustee verifies the delivery.** The seller uploads to content-addressed storage that Trustee
   controls (or a presigned bucket). Trustee computes the hash and runs the contract's checks itself: schema, row
   rules, the Channel3 market check, a test suite or an API SLA probe. Trustee then releases the file to the buyer
   and the capture together. The buyer's verdict becomes advisory. The checks become a library of **verifiers**,
   each a pure function named in the signed acceptance criteria. `partial.py` and `pricefeed.py` already have this
   shape.

Goods that cannot be checked by a machine (subjective work) stay on the human-dispute path. Trustee should say so
up front rather than pretend.

### 4.2 Seller identity is an email address

The payee is `PAYPAL_SELLER_EMAIL` (`paypal.py`), and principal registration only proves possession of a key. At
scale:

- Onboard each seller principal as a PayPal merchant through **Partner Referrals**, and pay
  `payee.merchant_id`, not an email.
- Bind the chain **agent key → principal → PayPal merchant id** and store it with the agent record. A signature
  then identifies a verified business, not just a keypair.
- Require PayPal-verified onboarding before a principal can receive money. Replace the shared
  `TRUSTEE_OPERATOR_TOKEN` with per-tenant OAuth credentials and rate limits.

### 4.3 How long money can be held

An authorization has a 3-day honor period and can be held for up to 29 days. The sweeper already reauthorizes
after the honor period (`TRUSTEE_REAUTHORIZE=1`, `sweeper.py`). That covers digital goods, which settle in
minutes. For longer work (milestones, retainers), switch to the marketplace model: capture into the seller's
account with **delayed disbursement** and release on verified delivery **(confirm limits)**. Add the switch to
the contract terms so both parties sign which mode they get.

### 4.4 Unit economics of small deals

Agent purchases are often worth cents or a few dollars. Per-transaction fixed fees make one PayPal order per deal
uneconomic at that size. Options, in increasing effort:

- **Batching.** One authorization covers a budget, partial captures settle many small verified deals against it.
  The pro-rata capture code is the starting point.
- **Netting.** Settle net positions between frequent counterparties on a schedule. The hash-chained ledger already
  records every deal, so a net settlement can be proven.
- **PYUSD** for sub-dollar, high-frequency settlement, keeping the same mandate, verifier and receipt layer on
  top **(confirm product fit)**.

### 4.5 One process, one database

`DealService` keeps every open deal in an in-memory dict with per-deal `RLock`s, persists to SQLite and runs the
sweeper in-process (`platform/service.py`). For multiple instances:

- Postgres, with the deal row as the lock (`SELECT ... FOR UPDATE` or a version column).
- A **transactional outbox** for PayPal calls: write the intended operation and its idempotency key in the same
  transaction as the state change, and let a worker execute it. The deterministic request ids already make retries
  safe.
- Webhooks go into a queue first and are processed by the same workers. The sweeper becomes a partitioned
  scheduled job.
- Push notifications (`a2a/push.py`) and Elastic shipping are already off the money path; keep them there.

The full design, with measurements, failure modes and a capacity estimate, is in
[SCALE_ARCHITECTURE.md](./SCALE_ARCHITECTURE.md).

### 4.6 Signing keys

The receipt key comes from `TRUSTEE_SIGNING_KEY` or is generated into the store, and receipts carry no key id. At
scale: keep keys in a KMS or HSM, add a `kid` to receipts, publish several keys at
`/.well-known/trustee-key.json`, and rotate. Periodically publish a root hash of all deal chains so the operator
cannot rewrite history unnoticed.

### 4.7 Disputes after capture

A buyer's human owner can still open a PayPal dispute after a capture. Today `PAYMENT.CAPTURE.REVERSED` and
`REFUNDED` only raise an alert (`paypal_alert`). Handle `CUSTOMER.DISPUTE.*` webhooks and answer through the
**Disputes API** with the evidence Trustee already has: the signed contract, the delivered hash, the verifier
results, the attestation, the receipt and the ledger chain. This is where Trustee saves PayPal the most work.

### 4.8 Risk on agent-initiated payments

- Mark human-not-present payments as merchant-initiated against a stored credential, and pass risk context where
  PayPal supports it (Set Transaction Context) **(confirm)**.
- The intent rules already enforce velocity and budget. Add reputation rules (already optional via Elastic) and
  detect collusion: agents that trade only with each other to build up reputation.
- Alert on mismatch rates per buyer: many rejections of deliveries that pass the verifiers indicates abuse (§4.1).

## 5. Product shape

| Option | What it is | Pros | Cons |
|---|---|---|---|
| **A. Partner app** | Trustee as a PayPal Commerce Platform partner: onboards sellers, takes a platform fee | Ships on today's APIs; this repo is most of it | Trustee is a third party; trust and liability sit with it |
| **B. PayPal feature** | "Conditional capture" in the Orders API: an order carries signed acceptance criteria and a verifier, PayPal captures or voids | Native, trusted by default, every PayPal merchant can use it | PayPal's roadmap, not ours |
| **C. Open protocol** | Publish the contract, attestation and receipt formats as an extension for AP2 and A2A, with PayPal as the reference settlement | Any agent framework can ask for "escrow" as a skill | Needs adoption |

**Recommendation:** build A to earn proof (real volume, dispute rates, verifier coverage), publish C in parallel
so agent frameworks integrate against a standard, and use the A data to pitch B. Trustee's Agent Card and A2A
skills already make it callable as option C.

## 6. Roadmap

| Phase | Goal | Work | Done when |
|---|---|---|---|
| 0. Validate | Real money path | Run `sandbox_poc.py` and the webhook path in sandbox; fix the hash cross-check (§4.1 step 1) | Honest and cheat deals settle and void against real sandbox |
| 1. Pilot | 3 to 5 design partners selling data or APIs | Partner Referrals onboarding, `merchant_id` payee, platform fee, Trustee-side verification, dispute evidence, Postgres and outbox, KMS keys | Every deal is verified by Trustee; no stranded holds; dispute evidence sent automatically |
| 2. Platform | Self-serve, multi-tenant | Verifier library, delayed disbursement for long deals, batching for small ones, tenant auth and rate limits, reputation | A new seller onboards and settles without help from us |
| 3. Standard | Ecosystem | Publish the protocol extension; PayPal-native conditional capture | Third-party agent frameworks open escrows without custom code |

## 7. What to measure

- **Auto-resolution rate:** share of deals settled or voided with no human.
- **False-release and false-capture rates:** checked by sampling and by later PayPal disputes.
- **Disputes per 1,000 deals** compared with ordinary digital-goods checkout.
- **Time from delivery to settlement.**
- **Holds that expired or needed reauthorization:** should be close to zero.
- **Cost per deal:** PayPal fees, verification compute, LLM calls (negotiation only, never settlement).

## 8. Open questions for PayPal

1. Current limits and availability of delayed disbursement for platforms, by market.
2. Whether vaulted, agent-initiated payments need a specific flag or risk context.
3. Whether a conditional-capture product needs anything beyond Partner Referrals for the operator (licensing,
   liability for wrong releases).
4. Pricing options for high-volume, low-value agent deals, and whether PYUSD fits.
5. How Disputes API evidence from a signed, hash-chained record is weighed against a buyer's claim.
