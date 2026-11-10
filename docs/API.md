# Platform API

Besides the arena, Trustee runs as a service that independent agents on other machines can discover and
use. Every mutating call is signed, so it is attributable to a key.

| Endpoint | Who signs | Purpose |
|---|---|---|
| `GET /.well-known/agent.json` (also `agent-card.json`) | n/a | A2A Agent Card: skills, interfaces, signing scheme |
| `POST /a2a` | the signed payloads inside | A2A JSON-RPC: the same operations as skills, deals as tasks, push notifications ([A2A](#a2a-json-rpc-and-push-notifications)) |
| `POST /v1/principals` | principal key (proof of possession) | register a principal (the human/company behind agents) |
| `POST /v1/agents` | agent key + principal | register an agent; the principal vouches for it |
| `POST /v1/mandates`, `POST /v1/mandates/{agent_id}/revoke` | principal | publish / revoke a spend or receive mandate |
| `POST /v1/deals` | buyer and seller (contract digest) | check both mandates, hold funds; idempotent on `deal_id` |
| `POST /v1/deals/{id}/deliverable` | seller (`X-Trustee-Signature` over `trustee-upload:{deal_id}:{sha256}`) | hand Trustee the file so it checks it itself; only the seller's signed upload counts |
| `POST /v1/deals/{id}/delivery` | seller | report the deliverable and its hash |
| `GET /v1/deals/{id}/deliverable` | buyer (`X-Trustee-Signature`) | download the file, once the payment has settled |
| `GET /v1/deals/{id}/evidence` | none | signed dispute evidence pack: contract, delivery, Trustee's check, attestation, ruling, receipt, ledger |
| `POST /v1/criteria/draft` | none | AI drafts checkable acceptance criteria from a buyer's request; code keeps only what the request says |
| `POST /v1/deals/{id}/attest` | buyer | accept (capture) or reject (arbitration) |
| `POST /v1/deals/{id}/resolve` | operator bearer token | human override of a `DISPUTED` deal |
| `GET /v1/deals/{id}`, `GET /v1/deals/{id}/events`, `GET /v1/agents/{id}` | none | state, persisted ledger, public agent record |

**Signing.** Ed25519 keys; public keys and signatures are urlsafe base64. Signed bytes are canonical JSON
(sorted keys, no whitespace, UTF-8; Decimals as strings, datetimes ISO 8601) of the object without its
`signature` field. A contract is signed over its sha256 `digest()` by each party. The recipes live in
`src/trustee/platform/wire.py`; `trustee.client.TrusteeClient` does all of it for you.

**Errors.** Bad or missing signature `401`; mandate refused `403` (with `violations`); unknown deal or
agent `404`; illegal state or conflicting retry `409`; PayPal failure `502` (the body carries the deal view,
funds are where it says, and repeating the call is safe); storage unable to record the payment intent `503` (no
PayPal call was made, nothing moved, and repeating the call is safe).

**Auth.** `POST /v1/principals` is open unless `TRUSTEE_OPERATOR_TOKEN` is set. That is fine for a
demo and not for the open internet. `POST /v1/deals/{id}/resolve` always needs that token and returns
`403` when none is configured. `GET` endpoints are public.

**Durability.** With `TRUSTEE_DB_PATH` set, principals, agents, mandates, deals and the event tape live in
SQLite. Before every PayPal call that can hold or move money, the deal and the pending operation (with its request
id) are written first; if that write fails, PayPal is not called. On start the service reloads open deals,
re-reserves their budgets and resumes sweeping them, and finishes any escrow opening that was recorded but never
answered with the same request id, so a crash or restart cannot strand a PayPal hold. Ledger events that fail to
save are retried in order, never dropped. The default (`:memory:`) persists nothing.

**Trustee checks the file itself.** When the seller uploads the file (signed with its agent key) before recording the delivery, Trustee runs the
contract's checks on those exact bytes (hash, schema and row rules, the Channel3 market check) and rules on its own
result: a failed check refunds the buyer, a passed check that the buyer rejects is held for a person, and a pro-rata
payment uses Trustee's count of valid items. The buyer gets the file from Trustee once the payment settles. All arena
scenarios hand Trustee the file. Without an upload, the buyer's check is the fallback (`src/trustee/verification.py`).

**AI-drafted acceptance criteria.** `POST /v1/criteria/draft` turns a request such as "at least 5,000 orders with
columns order_id, sku and refund_usd; no blank refund_usd" into contract criteria Trustee can check: a schema, a row
count with required fields, the file hash, a market check for price feeds. The model proposes; a column or a number is
kept only if the request contains it, and anything else comes back as a criterion a person decides. Trustee then runs
those same criteria on the delivered file, so a data product with no hand-written checker is still verified
(`src/trustee/criteria.py`).

**Evidence for disputes.** Every deal has a signed evidence pack (`GET /v1/deals/{id}/evidence`, and a link on the
arena page): the contract with both signatures, the seller's signed delivery, Trustee's own check of the file, the
buyer's attestation, the ruling, the summary, the receipt, the PayPal ids and every ledger event with a re-check of the
hash chain. `trustee.evidence.verify_evidence_pack` checks it offline; editing anything breaks the signature. Each
PayPal order also carries `trustee:<contract sha256>` as its `custom_id`, so the PayPal record and Trustee's record
find each other.

**Contradicted claims.** A buyer who rejects with a hash mismatch when the seller's signed delivery hash equals the
contract's does not get an automatic refund: the deal is `DISPUTED`, funds stay held, and it is flagged for a human
(`/resolve`). See [PAYPAL_SCALE.md](PAYPAL_SCALE.md) §4.1.

| Var | Default | Meaning |
|---|---|---|
| `TRUSTEE_DB_PATH` | `:memory:` | SQLite file; set it to survive restarts |
| `TRUSTEE_OPERATOR_TOKEN` | unset | gates principal registration; enables `/resolve` |
| `TRUSTEE_SWEEP_SECONDS` | `30` | deadline sweep interval |
| `TRUSTEE_PUBLIC_URL` | request URL | base URL advertised in the Agent Card; also the default host of the PayPal return/cancel URLs |
| `PAYPAL_WEBHOOK_ID` | unset | id of the PayPal webhook; without it live webhooks cannot be verified and are rejected |
| `TRUSTEE_WEBHOOK_ALLOW_UNVERIFIED` | off | `1` lets a dry-run server accept unsigned webhooks. Dev only |

Two agents, two principals, a full escrowed deal over HTTP:

```bash
python -m uvicorn trustee.arena.api:app --host 127.0.0.1 --port 8000
python examples/a2a_deal.py --base-url http://127.0.0.1:8000
```

## What a deal reports

Every deal carries a `summary`: in `GET /v1/deals/{id}`, in the A2A task status (data part) and in each arena run.

```json
{
  "status": "refunded",
  "tone": "neutral",
  "headline": "Refunded: the buyer keeps $170.00.",
  "reasons": [
    "The delivery failed the contract: it isn't the file the contract names (its fingerprint is different).",
    "Trustee checked the file itself, so this didn't rest on the buyer's word."
  ],
  "next_step": null,
  "money": {"paid_to_seller": "0.00", "returned_to_buyer": "170.00", "held": "0.00"},
  "currency": "USD"
}
```

`status` is one of `paid`, `partly_paid`, `refunded`, `cancelled`, `refused`, `on_hold`, `awaiting_approval`,
`in_progress`; agents branch on it, people read the headline. It describes decisions already made and never makes one
(`src/trustee/summary.py`).

## Real PayPal sandbox

**Untested against real PayPal until someone runs the POC below with sandbox credentials.** Everything in
this section is covered by tests that mock PayPal's HTTP, not by a live run.

With credentials set (`PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`), an AUTHORIZE order holds no funds until the
payer approves it. The deal stays `CONTRACTED`; its view carries `escrow.approve_url`. Then:

- **Approval.** PayPal redirects the payer to `GET /v1/paypal/return?token=<order_id>`. Trustee re-checks both
  mandates, authorizes the order (one call, request id `trustee-<deal>-authorize`) and moves to `ESCROWED`.
  Repeating the return is a no-op. `GET /v1/paypal/cancel?token=<order_id>` voids the deal; nothing was held.
  Set `TRUSTEE_PUBLIC_URL` and these become the default `PAYPAL_RETURN_URL` / `PAYPAL_CANCEL_URL`
  (an explicit value wins). Send `Accept: application/json` to get JSON instead of the HTML page.
- **No approval by the deadline** voids the deal without a PayPal call.
- **Polling fallback.** Each sweep also reads up to 20 awaiting orders from PayPal and authorizes the ones that
  are `APPROVED`. This is what makes local development work without a public webhook URL.
- **Webhooks.** Point a PayPal webhook at `POST /v1/paypal/webhook` (needs a public URL) and subscribe to
  `CHECKOUT.ORDER.APPROVED`, `PAYMENT.AUTHORIZATION.VOIDED`, `PAYMENT.CAPTURE.COMPLETED`,
  `PAYMENT.CAPTURE.DENIED`, `PAYMENT.CAPTURE.REVERSED` and `PAYMENT.CAPTURE.REFUNDED`. Set `PAYPAL_WEBHOOK_ID`
  to its id. Every delivery is verified with PayPal's verify-webhook-signature API (failure: `400`), then
  deduplicated on the event id. A void or capture whose HTTP call timed out is settled by the matching
  event, so the deal state ends up where the money is (ledger kind `reconcile`). A capture event the deal cannot
  follow (deal `ESCROWED` or `VOIDED`), and denied, reversed or refunded captures, change no state. They are
  ledgered as `paypal_alert` and the deal view shows `needs_attention`.
- **Mandate versions.** `Mandate.version` is signed. Publishing a mandate whose version is not newer than the
  one on file is refused (`409`), so an old signed mandate cannot be replayed to undo a newer one.

Proof of concept (sandbox only; refuses `PAYPAL_ENV=live`):

```bash
python examples/sandbox_poc.py --settle      # or --refund; open the printed approve URL as a sandbox buyer
python examples/sandbox_poc.py --dry-run     # same steps with no network or credentials
python examples/sandbox_poc.py --settle --auto-approve   # a browser approves as the sandbox buyer
```

`--auto-approve` (`uv sync --extra e2e`) logs in as `PAYPAL_SANDBOX_BUYER_EMAIL` / `PAYPAL_SANDBOX_BUYER_PASSWORD` in
local Chromium, or in a remote browser such as one started by KERNEL when `PAYPAL_E2E_CDP_URL` is set, and approves.
Success means PayPal redirected to the return URL. The `sandbox-e2e` workflow runs this weekly once the sandbox secrets
are set. The selectors for PayPal's pages are a best guess, not yet checked against the live sandbox; override them
with `PAYPAL_E2E_SELECTORS` (`src/trustee/sandbox_approver.py`).

## A2A: JSON-RPC and push notifications

Trustee implements the Agent2Agent protocol **spec 1.0.0, JSON-RPC binding** at `POST /a2a`, so any A2A agent can use
it as its escrow service and be told when money moves instead of polling. Full reference: [A2A.md](A2A.md).

- **Skills.** `SendMessage` with one DataPart `{"skill": "open-escrow", "input": {...}}` runs the same operation as the
  matching REST call, with the same signed payload: `register-principal`, `register-agent`, `publish-mandate`,
  `revoke-mandate`, `open-escrow`, `record-delivery`, `attest-delivery`, `get-deal`. A text-only message gets a help
  message listing them. The Ed25519 signatures inside the payloads authorize every change; A2A is only the transport.
- **A deal is a task** (`taskId = deal_id`), rebuilt from the durable deal on every `GetTask`, so it survives restarts:
  `AUTH_REQUIRED` (payer must approve at PayPal) -> `INPUT_REQUIRED` (funds held, seller must deliver) -> `WORKING`
  (delivered, buyer verifying; also disputes) -> `COMPLETED` with a `settlement-receipt` artifact (captured or
  refunded). A payer-cancelled deal is `CANCELED`; a refused contract is `REJECTED`.
  `ListTasks` and `CancelTask` (only before the payer approves, no PayPal call) are supported.
- **Push.** `CreateTaskPushNotificationConfig` (or `configuration.taskPushNotificationConfig` in `SendMessage`) makes
  Trustee `POST {"task": ...}` to your webhook on every state change, with your token in `X-A2A-Notification-Token`:
  a seller learns "funds are held, ship now" without polling. Delivery runs on a background worker with retries and
  never blocks the money path. URLs must be https (127.0.0.1/localhost allowed); configs are stored in SQLite.
- **Client.** `trustee.a2a.A2AClient` is a generic JSON-RPC A2A client (`fetch_card`, `send_message`, `call_skill`,
  `get_task`, `list_tasks`, `cancel_task`, `set_push_config`), usable against any A2A agent.
- **Also accepted:** the v0.3 method names (`message/send`, `tasks/get`, ...), answered in the v0.3 shape.
- **Not implemented:** streaming (`SendStreamingMessage`, `SubscribeToTask`), the authenticated extended card, the gRPC and
  HTTP+JSON A2A bindings, task `history`, tenants, Agent Card signatures. Trustee's `/v1` REST API is listed in the card
  but is not the A2A HTTP+JSON binding.

Call it from Python:

```python
from trustee.a2a import A2AClient

c = A2AClient("http://127.0.0.1:8000")
task = c.call_skill("open-escrow", signed_contract_dict)  # then record-delivery / attest-delivery with task_id=task.id
```

## The A2A network: three agents

Northwind's planning agent and Tidewater's sales agent are real A2A agents too, each with its own Agent Card and
JSON-RPC endpoint (`/agents/northwind`, `/agents/tidewater` on the arena server, or standalone). Given only Tidewater's
address, Northwind reads its card, gets a quote, negotiates over A2A with Ed25519-signed offers (each side checks the
other's key from its card), has Tidewater countersign the contract, registers at Trustee and opens the escrow. Trustee
pushes "funds held" to Tidewater, which sends the CSV to Northwind as an A2A file part; Northwind verifies the bytes and
attests. The arena scenario **Agent to agent** (`a2a`) shows every hop as an `a2a` row on the tape.

```bash
python examples/a2a_network.py            # Trustee + Northwind + Tidewater on three 127.0.0.1 ports, one purchase
python examples/a2a_network.py --cheat    # Tidewater ships junk: refunded, nothing captured
```

Details, skills and the identity-in-card convention: [A2A.md](A2A.md#the-a2a-network-northwind-and-tidewater-as-agents).

## Autonomous payments: consent once, then no clicks

"Buy this for me" should not mean "approve every payment". Trustee implements the AP2 human-not-present pattern
([AUTONOMY.md](AUTONOMY.md)): the human consents **once**, and the agent pays by itself inside limits that
Trustee enforces deterministically.

1. **One PayPal consent.** The principal approves a PayPal Vault consent a single time. Trustee keeps the resulting
   payment token server side and hands out only an opaque `funding_ref`. The vault id is never returned, logged or
   written to a receipt.
2. **One signed intent mandate.** The principal signs the human's own sentence ("Buy Tidewater's Q3 2026 returns
   dataset for demand planning; spend at most $175; only from Tidewater; must match the published sha256") together
   with the limits derived from it. A model may *propose* those limits from the sentence; code checks the proposal
   against the words, and only the principal's key makes it binding.
3. **Every purchase is checked.** Fourteen rules run on each contract (signature chain, expiry, revocation, seller,
   item, price, budget, velocity, required criteria, currency, funding). Each result is a `policy` row on the ledger.
   All pass: the funds are held with no click (PayPal `vault_id`, still `AUTHORIZE`: capture waits for verified
   delivery, and the intent is re-checked at capture). A hard violation (forged, revoked, expired, wrong seller) is
   refused before PayPal. A soft overrun (price above the signed cap, unknown item) **steps up** to a human approval.
4. **Everything is provable.** The ledger is hash-chained per deal and every finished deal gets an Ed25519-signed
   settlement receipt. Check one offline, trusting no server:

```bash
curl -s $TRUSTEE/v1/deals/$DEAL/receipt > receipt.json
curl -s "$TRUSTEE/v1/deals/$DEAL/events?limit=1000" > events.json
python -m trustee.verify receipt.json --key $TRUSTEE/.well-known/trustee-key.json --events events.json
```

| Endpoint | Who signs | Purpose |
|---|---|---|
| `POST /v1/principals/{p}/funding` | principal | start the one-time PayPal consent; returns `funding_ref` and the link to open once |
| `GET /v1/funding/return` | n/a (PayPal redirect) | vaults the payment method after the consent |
| `POST /v1/principals/{p}/funding/{ref}/revoke` | principal | revoke a funding source; later purchases step up |
| `POST /v1/intents`, `POST /v1/intents/{agent_id}/revoke` | principal | publish / revoke the signed intent mandate |
| `GET /v1/deals/{id}/receipt`, `POST /v1/receipts/verify` | n/a | the signed settlement receipt, and a server-side check of one |
| `GET /.well-known/trustee-key.json` | n/a | the public key that signs receipts (also in the Agent Card) |

A2A skills: `register-funding`, `publish-intent`, `revoke-intent`. Arena: scenarios `honest`, `cheat`, `outside` and
`a2a` pay under the intent mandate (dry-run: always; sandbox: with `PAYPAL_DEMO_VAULT_ID`), and in `overreach` a price
above the signed cap steps up to a person. Intent mandates are optional: a deal without one waits for the payer to
approve in PayPal, as described under [Real PayPal sandbox](#real-paypal-sandbox).

| Var | Default | Meaning |
|---|---|---|
| `TRUSTEE_SIGNING_KEY` | generated, kept in the store | base64 Ed25519 private key that signs receipts |
| `PAYPAL_DEMO_VAULT_ID` | unset | a **secret** Vault payment-token id so sandbox arena runs are autonomous. Never commit it |

## Client libraries and Postman

- **OpenAPI.** `openapi/trustee-v1.json` describes every `/v1` route, every `/.well-known` route and the `/a2a` endpoint. Regenerate it with
  `python scripts/export_openapi.py` after an API change. `tests/test_openapi.py` fails when the file is stale.
- **Postman.** `postman/Trustee.postman_collection.json` runs the honest deal and three refusals end to end, then the same
  deal over A2A (folder 4).
  Import it with the environment file in the same folder. Its signed bodies use public demo keys, so do not
  point it at a server that holds money.
- **Python client.** `trustee.client.TrusteeClient` signs registrations, mandates and revocations, and refuses to
  send a contract, delivery or attestation that the right party has not signed.

The Python client, retry rules and the signing boundary are in [SDKS.md](SDKS.md).
