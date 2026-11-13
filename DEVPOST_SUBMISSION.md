# Devpost submission copy — PayPal AI Hackathon

Paste each section into the matching Devpost field. Items marked `[confirm]` need your final choice or upload.

## Project name

Trustee

## Tagline

What escrow.com is to strangers, Trustee is to AI agents — except the escrow agent verifies the goods itself, and both parties are software.

## Elevator pitch / short description

Two companies' AI agents want to transact — and neither trusts the other. Trustee is the neutral third party: it holds the buyer's funds in a PayPal authorization, downloads and verifies the delivered bytes itself (SHA-256, schema, market price), then captures or voids automatically. No human approves the payment, no LLM decides it — only signed policy code moves money.

## Links and assets

- Live demo: https://trustee-arena.onrender.com
- Repository: https://github.com/JWHaan/trustee
- Demo video: `[confirm URL]`
- Screenshot 1: `docs/arena-honest.png`
- Screenshot 2: `docs/arena-cheat.png`
- Team members: `[confirm names/handles]` — solo build unless teammates are added.

## Inspiration

AI agents are starting to buy things from each other — data, API access, digital work — but the money layer was never built for them. If a buyer agent pays first, the seller can ship garbage. If the seller delivers first, the buyer can refuse to pay. Humans solved this with escrow.com and chargebacks; agents have neither trust nor recourse.

Most hackathon escrow projects still put a human in the loop — a client clicks approve, a merchant reviews a dashboard. Trustee removes the human entirely: the buyer and seller are both autonomous agents, and the only thing allowed to move money is deterministic, signed policy. The LLM negotiates and explains; it has no code path to PayPal.

We wanted a neutral third party both sides' agents could trust by construction: one that holds the funds, checks the deliverable itself, and settles or refunds automatically — on payment rails they already use. PayPal's authorize/capture/void primitives map cleanly onto escrow, but PayPal doesn't verify digital deliverables. That gap is Trustee.

## What it does

AI agents can negotiate, agree on a deal, and then have nowhere safe to settle it. If the buyer pays first, the seller can underdeliver. If the seller delivers first, the buyer can refuse to pay. Existing agent protocols cover discovery, identity, mandates, and checkout, but not conditional settlement between two parties that are both software.

Trustee is the missing trust layer — a neutral escrow agent for commerce where both counterparties are software. A buyer agent and seller agent form a signed, machine-readable contract. Before PayPal is ever called, Trustee checks that contract against both parties' signed mandates — budget caps, counterparty allowlists, expiry. A deal outside policy is rejected before a cent is held. If the deal is allowed, Trustee opens a PayPal authorization so the funds are held, not moved. The seller then hands Trustee the deliverable and records a signed delivery. Trustee checks the delivered bytes itself against the contract: SHA-256 hash, schema, row rules, and optional market-price checks. If the checks pass, Trustee captures payment to the seller. If they fail, it voids the hold and returns the money to the buyer. A partly valid delivery can settle pro-rata for the valid share.

Every contract, mandate, delivery, ruling, receipt, and ledger entry is signed. The ledger is hash-chained, and dispute evidence packs can be verified offline. The LLM can negotiate and explain, but only deterministic policy code can move money.

## How it works

1. Buyer and seller agents discover each other, negotiate terms, and sign a contract.
2. Trustee checks both parties' mandates: budget caps, counterparty allowlists, categories, expiry, and spend limits.
3. If the deal is outside policy, it is rejected before PayPal is called.
4. Trustee creates a PayPal Orders v2 authorization so the buyer's funds are held.
5. The seller uploads the deliverable to Trustee and records a signed delivery with its SHA-256 hash.
6. Trustee runs the contract's acceptance checks on those exact bytes.
7. Trustee captures to the seller, voids back to the buyer, or settles the valid share of a partial delivery.
8. A signed receipt and evidence pack record what happened and why.

## PayPal integration

Trustee uses PayPal's server-side REST APIs rather than a checkout button:

- OAuth client-credentials authentication.
- Orders v2 with `intent=AUTHORIZE` to create escrow.
- `/v2/checkout/orders/{id}/authorize` to hold funds.
- `/v2/checkout/orders/{id}/capture` to settle to the seller, including supported partial captures.
- `/v2/checkout/orders/{id}/void` to release funds back to the buyer.
- Vault v3 setup tokens and payment tokens for consented, human-not-present payments.
- Webhook signature verification for PayPal events.
- `PayPal-Request-Id` idempotency keys and deterministic retry handling so money operations are not duplicated.
- `custom_id` links each PayPal order back to the Trustee contract hash.

The hosted demo uses the PayPal sandbox. A sandbox payer may need to approve the payment once unless the optional Vault flow is configured.

## AI integration

Trustee uses an OpenAI-compatible LLM endpoint, defaulting to NVIDIA NIM (`nvidia/nemotron-3.5-lightning-30b-a3b`) with a scripted fallback for reproducible demos.

The model helps agents negotiate offers and counters, draft checkable acceptance criteria from a buyer's request, and explain decisions. It does not decide whether money moves. Every payment action is gated by deterministic policy code: signed mandates, contract checks, delivery verification, and the deal state machine.

## How I built it

Trustee is a single Python service with a React frontend, deployed as one Docker container on Render.

The core is a deterministic deal state machine. Contracts, mandates, deliveries, attestations, and settlement receipts are all Ed25519-signed, and every state transition is checked against signed policy before PayPal is ever called. The PayPal adapter (`src/trustee/paypal.py`) is the only module that talks to PayPal: Orders v2 authorize holds funds, capture settles to the seller (including pro-rata partial captures), and void releases them back to the buyer. Every money POST carries a stable `PayPal-Request-Id` so retries and webhook replays can't move money twice.

Delivery verification is deliberately not LLM-based. The seller uploads the file bytes with a signed delivery record; Trustee hashes them (SHA-256), checks CSV schema and row-level rules, and can cross-check prices against Channel3 market data. Only after those checks does the state machine capture, void, or settle the valid share.

The LLM layer (`src/trustee/llm.py`) is an OpenAI-compatible client defaulting to NVIDIA NIM (Nemotron). It negotiates offers, drafts acceptance criteria, and explains rulings — with a scripted fallback so demos are reproducible. It has no path to the PayPal client.

Around the core: a hash-chained JSONL ledger, a SQLite store for durable deal recovery, a public `/v1` REST API, an A2A JSON-RPC server with Agent Cards, an arena UI (React/TypeScript/Vite/AG Grid) that replays scenarios live, and optional integrations for Elastic analytics, Zapier alerts/back-office via MCP, and Playwright/CDP sandbox approval.

Testing was adversarial-first: `tests/test_attacks.py` runs 17 attack scenarios (forged signatures, mandate overreach, replayed approvals, buyer framing) in CI, plus mocked-HTTP PayPal tests and an optional real-sandbox Playwright e2e.

## Technical implementation

- Python 3.12 backend with FastAPI and Pydantic.
- React + TypeScript + Vite arena UI with AG Grid for the live ledger.
- SQLite-backed platform store and durable deal recovery.
- Ed25519 signatures for contracts, mandates, deliveries, attestations, receipts, and evidence packs.
- Hash-chained JSONL ledger.
- Public signed REST API under `/v1`.
- Agent2Agent JSON-RPC support with Agent Cards and task handling.
- Optional integrations: Channel3 market checks, Elastic ledger analytics/reputation, Zapier alerts, Postman collection, and KERNEL/CDP sandbox approval.
- Dockerized deployment on Render.

## Demo

The hosted arena is at https://trustee-arena.onrender.com. It is connected to the PayPal sandbox. The free Render instance can sleep, so the first request may take about a minute.

Suggested judge path:

1. Open the demo URL.
2. Run the `honest` scenario: the agents negotiate, escrow opens, the file is verified, and the seller is paid.
3. Run the `cheat` scenario: the seller ships an invalid file, Trustee's own checks fail, and the buyer keeps the money.
4. Optionally run `partial`, `overreach`, `forged`, `a2a`, or the Channel3 price-feed scenarios.
5. Inspect the ledger and evidence pack for the PayPal order ID, checks, decision, and settlement result.

For a no-credentials path, the repository can run the same scenarios locally in dry-run mode:

```bash
uv sync --extra dev
uv run pytest
cd arena/ui && npm install && npm run build && cd ../..
uv run python -m uvicorn trustee.arena.api:app --port 8000
```

## Challenges we ran into

- **LLM nondeterminism:** models can propose invalid or unsafe moves. Trustee constrains outputs and lets only policy code touch PayPal.
- **PayPal authorization semantics:** sandbox AUTHORIZE orders may require payer approval before funds can be held. Trustee supports both approval links and Vault-backed consented payments.
- **Idempotency:** payment retries, webhook redelivery, duplicate accepts, and process restarts must not capture or void twice. Trustee uses stable request IDs, signed state transitions, and durable deal recovery.
- **Independent verification:** a buyer's claim cannot be the only evidence. Trustee checks the exact delivered bytes itself and prevents a buyer from framing an honest seller with unrelated uploads.
- **Partial delivery:** a file can be partly valid. Trustee counts valid units itself and settles only the contractually valid share.
- **Honest demo constraints:** the hosted free tier has no persistent disk, so the demo uses in-memory state and can lose platform deals on restart. That limitation is documented rather than hidden.

## What we accomplished

- Built an end-to-end agent-to-agent escrow and arbitration system on PayPal.
- Implemented conditional settlement: authorize, verify, capture or void.
- Added signed mandates, contracts, deliveries, receipts, and dispute evidence.
- Added a hash-chained ledger and offline-verifiable receipts.
- Added a public REST API and A2A agent interface so external agents can use Trustee.
- Added pro-rata settlement, stale-price checks, forged-identity rejection, mandate overreach handling, and human step-up approvals.
- Verified all 10 arena scenarios end-to-end on the live PayPal sandbox — 7 settle with real captured orders, 3 void as designed — plus a CI workflow (`sandbox-e2e`) that completes a real authorize→capture on every push (e.g. authorization `7J3993247F013415U`, capture `9HY209711U9483505`) and a hosted run that settled with `human_present: false` via vaulted mandate (run `run_c5d3882f4a8e`, $143.00 captured).
- Added a hosted demo arena showing the conversation, verification checks, PayPal order state, decision, and ledger.
- Added an executable attack suite covering 17 adversarial cases.

## What we learned

The important boundary is not "AI versus rules." It is which layer is allowed to make a consequential decision. LLMs are useful for negotiation and explanation, but money needs deterministic policy, verifiable evidence, and idempotent execution. PayPal's authorize/capture/void primitives map naturally to escrow, but the trust layer still has to decide when each transition is valid.

## What's next

- Move the hosted demo to durable storage for production-like operation (in-memory state on the free tier loses platform deals on restart — documented, not hidden).
- Expand verifier types beyond CSV/schema/hash and Channel3 market checks.
- Add more agent frameworks and marketplace integrations.
- Publish the contract, attestation, receipt, and evidence formats as an open protocol extension.
- Explore PayPal Partner Referrals, merchant onboarding, platform fees, and dispute evidence workflows.
- Measure auto-resolution rate, false release/capture rate, settlement time, stranded holds, and cost per deal.

## Built with

Python, FastAPI, Pydantic, Uvicorn, HTTPX, SQLite, cryptography, OpenAI SDK, NVIDIA NIM, Nemotron, React, TypeScript, Vite, AG Grid, Node.js, Docker, Render, PayPal, A2A, MCP, Zapier, Elasticsearch, Channel3, Postman, Playwright.

Devpost tag list: `Python`, `FastAPI`, `Pydantic`, `Uvicorn`, `HTTPX`, `SQLite`, `cryptography`, `OpenAI SDK`, `NVIDIA NIM`, `Nemotron`, `React`, `TypeScript`, `Vite`, `AG Grid`, `Node.js`, `Docker`, `Render`, `PayPal`, `A2A`, `MCP`, `Zapier`, `Elasticsearch`, `Channel3`, `Postman`, `Playwright`.

## Notes for the submission form

- Select categories that match the visible Devpost form. The strongest fit is a project that meaningfully uses both PayPal and AI.
- If sponsor-tool prizes are listed, choose only tools actually used: Render, AG Grid, Channel3, Zapier, Elastic, Postman, and KERNEL/CDP where applicable.
- Do not paste sandbox credentials, `.env` contents, client secrets, webhook secrets, or operator tokens anywhere in Devpost.
- The real-sandbox money path is verified: the `sandbox-e2e` GitHub Action completes a real authorize→capture against PayPal sandbox (e.g. authorization `7J3993247F013415U`, capture `9HY209711U9483505`), and hosted arena runs settle with `human_present: false` via the vaulted mandate (e.g. run `run_c5d3882f4a8e`, order `3JT36509FL734773D`, $143.00 captured). Zapier alerting is configured via a Catch Hook; check Zapier task history for delivery confirmation.
- Full hosted scenario matrix verified on the PayPal sandbox (each run reached a terminal state): `honest`, `outside`, `a2a`, `partial`, `pricefeed`, `pricefeed-mixed`, and `overreach` (after human step-up approval) all `SETTLED` with real captured orders; `cheat`, `forged`, and `pricefeed-stale` all `VOIDED` as designed.
