# Trustee

An escrow + arbitration agent for agent-to-agent commerce — PayPal AI Hackathon entry.

In an agent-to-agent world, two agents can negotiate anything and enforce nothing.
Trustee is the missing trust layer: when buyer and seller agents form a machine
contract, the Trustee gates it against both parties' **mandates** (AP2-lite:
budget caps, counterparty allowlists, TTLs), holds the money in **PayPal escrow**
(Orders authorize/capture/void, sandbox), settles on delivery + attestation, and
auto-unwinds (voids + refunds) when the deal fails. Every decision lands in an
append-only **ledger** that the arena UI tapes out in AG Grid.

## Who it's for

Any business whose agents buy data, API access or digital work from another
company's agents. The worked example in this repo: **Northwind Outfitters**, a
mid-size online apparel retailer, has a demand-planning agent that buys a Q3
returns dataset from **Tidewater Data Co.**, a retail-data vendor. Northwind's
finance ops lead grants the buying agent a $500 spend mandate: digital data
only, this vendor only. Tidewater's sales agent delivers the CSV. Trustee holds
the payment until the file's hash and schema match the signed contract, then
settles it. If the file is wrong, the money goes back to Northwind.

## Money path

```
contract formed
  -> mandate gate (no LLM: hard policy)        rejected? deal dies, money untouched
  -> PayPal Orders.create + authorize           funds HELD, not moved
  -> seller agent delivers (bytes + sha256)
  -> buyer agent attests (sha256 + CSV schema and row checks on the delivered file)
       accept -> capture  (settled to seller)
       reject -> arbiter -> void (refund)  |  deadline lapse -> auto-void
```

Contracts can opt in to pro-rata settlement (a rejected delivery pays for its valid share) and the sweeper can
renew held authorizations after PayPal's 3-day honor period: see [docs/PARTIAL_CAPTURE.md](docs/PARTIAL_CAPTURE.md).

The LLM proposes and explains; the policy layer is the only thing that can
touch PayPal. Terminal states are immutable; webhook replays are idempotent
no-ops — funds can never double-settle or strand.

| Honest seller — settled | Cheating seller — refunded |
|---|---|
| ![honest run](docs/arena-honest.png) | ![cheat run](docs/arena-cheat.png) |

> These two screenshots predate the arena redesign (scenario cards, deal timeline, money panel, chat-style
> transcript, restyled ledger tape). They are kept as-is and need re-shooting.

## Run it

```bash
uv sync --extra dev
uv run pytest                       # 259 tests, dry-run: no credentials needed

# the judge's no-UI path — full ledger to stdout, exits non-zero if the deal
# doesn't reach the expected terminal state
uv run python -m trustee.arena.fixtures.run_honest
uv run python -m trustee.arena.fixtures.run_cheat

# the arena
cd arena/ui && npm install && npm run build && cd ../..
uv run python -m uvicorn trustee.arena.api:app --port 8000
open http://127.0.0.1:8000
```

The page runs the honest deal on load, then gives you one button per scenario and a
mode selector. No account, no login, no setup.

> Use `python -m uvicorn`, not bare `uvicorn` — the bare name resolves a global
> install and will not see this project's package.

## The arena

| Endpoint | Purpose |
|---|---|
| `POST /api/runs` `{scenario, mode}` | start a run, returns `{run_id}` |
| `GET /api/runs/{id}` | state snapshot: deal state, contract, escrow, ruling |
| `GET /api/runs/{id}/events` | SSE: one `msg` per ledger row, then `done` |
| `GET /api/ledger?limit=500` | flat rows for the grid |
| `GET /api/scenarios` | fixture-driven scenario list |
| `GET /healthz` | liveness + whether PayPal is dry-run |

SSE is replay-first: a subscriber is pre-filled with everything the run has
already emitted, then fed live, so refreshing mid-run shows the same tape in
the same order. Runs execute on a worker thread (`asyncio.to_thread`) because
the agents are synchronous; ledger callbacks are marshalled back onto the event
loop with `call_soon_threadsafe`.

**Scenarios.** Four, all in `fixtures/scenarios.json`:

| Scenario | What it proves | Ends |
|---|---|---|
| `honest` | Negotiated deal, machine-checked delivery, capture. | `SETTLED` |
| `cheat` | A junk delivery fails the hash and schema checks; the arbiter cites the clause and voids. | `VOIDED` |
| `forged` | Someone who is not Tidewater signs the contract as Tidewater (an unregistered key). Trustee rejects it with `rejected_identity` before PayPal is called: zero PayPal requests. | `outcome: rejected_identity` |
| `outside` | Northwind's and Tidewater's agents act as outside clients of this server's own public `/v1` API (registration, signed mandates, contract, delivery, attestation), each HTTP call shown as an `api` row next to the platform's own rows. | `SETTLED` |

The grid has `signer` and `check` columns (`verified` / `rejected`) for every signature the Trustee checks,
kind filter chips and a search box. A forged signature turns its row red. The strip shows a `signatures` chip and
whether PayPal is `sandbox` or `dry-run`.

`outside` calls the arena's own `/v1` over a real socket: `http://127.0.0.1:$PORT` by default (`PORT`, default
8000), or `ARENA_SELF_URL` if the server is reachable under another address. It sends `TRUSTEE_OPERATOR_TOKEN`
when one is set. Its agents get a fresh id and key per run (`northwind-planner-<run>`, `tidewater-sales-<run>`).

**Approving in the PayPal sandbox.** With sandbox credentials, no scenario holds funds until the buyer approves. The
run stops at `CONTRACTED`, writes an `awaiting_approval` row carrying the approve link and shows an
"Approve in PayPal sandbox" button with a countdown. The arena polls PayPal every `ARENA_APPROVAL_POLL_SECONDS`
(default 3); once the order is `APPROVED` the run continues to delivery and attestation. No approval within
`ARENA_APPROVAL_TIMEOUT_SECONDS` (default 300), or a click on cancel in PayPal, voids the deal with no money call
(`outcome: approval_timeout` / `approval_cancelled`). PayPal's return and cancel redirects for an arena order
(`/v1/paypal/return`, `/v1/paypal/cancel`) answer `303` to `/`; cancel also ends that run's wait. Set
`TRUSTEE_PUBLIC_URL` (or `PAYPAL_RETURN_URL` / `PAYPAL_CANCEL_URL`) so PayPal can send the buyer back. This path is
tested against a mocked PayPal only, like the rest of the sandbox flow.

**Modes.** `auto` (default) tries the live model and falls back to a recorded
transcript, saying so on the tape. `live` fails loudly — that is the mode you
run a reliability drill in. `scripted` never touches the network: the full
money path still runs for real, only the chat is replayed.

## Platform API (A2A)

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
| `POST /v1/deals/{id}/delivery` | seller | report the deliverable and its hash |
| `POST /v1/deals/{id}/attest` | buyer | accept (capture) or reject (arbitration) |
| `POST /v1/deals/{id}/resolve` | operator bearer token | human override of a `DISPUTED` deal |
| `GET /v1/deals/{id}`, `GET /v1/deals/{id}/events`, `GET /v1/agents/{id}` | none | state, persisted ledger, public agent record |

**Signing.** Ed25519 keys; public keys and signatures are urlsafe base64. Signed bytes are canonical JSON
(sorted keys, no whitespace, UTF-8; Decimals as strings, datetimes ISO 8601) of the object without its
`signature` field. A contract is signed over its sha256 `digest()` by each party. The recipes live in
`src/trustee/platform/wire.py`; `trustee.client.TrusteeClient` does all of it for you.

**Errors.** Bad or missing signature `401`; mandate refused `403` (with `violations`); unknown deal or
agent `404`; illegal state or conflicting retry `409`; PayPal failure `502` (the body carries the deal view,
funds are where it says, and repeating the call is safe).

**Auth.** `POST /v1/principals` is open unless `TRUSTEE_OPERATOR_TOKEN` is set. That is fine for a
hackathon and not for the open internet. `POST /v1/deals/{id}/resolve` always needs that token and returns
`403` when none is configured. `GET` endpoints are public.

**Durability.** With `TRUSTEE_DB_PATH` set, principals, agents, mandates, deals and the event tape live in
SQLite. On start the service reloads open deals, re-reserves their budgets and resumes sweeping them, so a
restart cannot strand a PayPal hold. The default (`:memory:`) persists nothing.

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

### Real PayPal sandbox

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
```

### A2A: JSON-RPC and push notifications

Trustee implements the Agent2Agent protocol **spec 1.0.0, JSON-RPC binding** at `POST /a2a`, so any A2A agent can use
it as its escrow service and be told when money moves instead of polling. Full reference: [docs/A2A.md](docs/A2A.md).

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
task = c.call_skill("open-escrow", signed_contract_dict)   # then record-delivery / attest-delivery with task_id=task.id
```

### The A2A network: three agents

Northwind's planning agent and Tidewater's sales agent are real A2A agents too, each with its own Agent Card and
JSON-RPC endpoint (`/agents/northwind`, `/agents/tidewater` on the arena server, or standalone). Given only Tidewater's
address, Northwind reads its card, gets a quote, negotiates over A2A with Ed25519-signed offers (each side checks the
other's key from its card), has Tidewater countersign the contract, registers at Trustee and opens the escrow. Trustee
pushes "funds held" to Tidewater, which sends the CSV to Northwind as an A2A file part; Northwind verifies the bytes and
attests. The arena scenario **Three A2A agents** (`a2a`) shows every hop as an `a2a` row on the tape.

```bash
python examples/a2a_network.py            # Trustee + Northwind + Tidewater on three 127.0.0.1 ports, one purchase
python examples/a2a_network.py --cheat    # Tidewater ships junk: refunded, nothing captured
```

Details, skills and the identity-in-card convention: [docs/A2A.md](docs/A2A.md#the-a2a-network-northwind-and-tidewater-as-agents).

### SDKs and Postman

- **OpenAPI.** `openapi/trustee-v1.json` describes every `/v1` route, every `/.well-known` route and the `/a2a` endpoint. Regenerate it with
  `python scripts/export_openapi.py` after an API change. `tests/test_openapi.py` fails when the file is stale.
- **Postman.** `postman/Trustee.postman_collection.json` runs the honest deal and three refusals end to end, then the same
  deal over A2A (folder 4).
  Import it with the environment file in the same folder. Its signed bodies use public demo keys, so do not
  point it at a server that holds money.
- **Generated SDKs.** The `sdks` workflow builds Python and TypeScript SDKs with APIMatic. It needs the
  `APIMATIC_API_KEY` secret and skips with a notice until that secret is set.

Setup, usage sketches and the signing boundary are in [docs/SDKS.md](docs/SDKS.md).

## Autonomous payments: consent once, then no clicks

"Buy this for me" should not mean "approve every payment". Trustee implements the AP2 human-not-present pattern
([docs/AUTONOMY.md](docs/AUTONOMY.md)): the human consents **once**, and the agent pays by itself inside limits that
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
`a2a` now pay under the intent mandate (dry-run: always; sandbox: with `PAYPAL_DEMO_VAULT_ID`), and `overreach` shows
the agent being refused permission to spend above its instruction. Without an intent mandate every flow behaves as
before.

| Var | Default | Meaning |
|---|---|---|
| `TRUSTEE_SIGNING_KEY` | generated, kept in the store | base64 Ed25519 private key that signs receipts |
| `PAYPAL_DEMO_VAULT_ID` | unset | a **secret** Vault payment-token id so sandbox arena runs are autonomous. Never commit it |

## Layout

```
src/trustee/
  protocol.py        offer/contract JSON protocol (the only thing settlement sees)
  mandate.py         AP2-lite mandate engine
  intent.py          intent mandates (human-not-present): signed instruction + limits, the rule engine, drafting
  funding.py         vaulted PayPal funding sources behind opaque funding_refs
  receipts.py        signed settlement receipts and the Trustee signing key
  verify.py          `python -m trustee.verify`: check a receipt offline
  state.py           deal state machine (guarded, idempotent, transition hook)
  paypal.py          PayPal adapter (authorize/capture/void, approval flow, dry-run)
  ledger.py          append-only, hash-chained event log with subscribers
  llm.py             OpenAI-compatible client (NIM/OpenAI/Ollama)
  agents/            trustee, buyer, seller, arbiter
  platform/          public /v1 API, A2A Agent Card, DealService (durable multi-deal host)
  a2a/               A2A JSON-RPC server + client, skills, deal tasks, push notifications
  network/           Northwind and Tidewater as A2A agents (cards, skills, signed negotiation, delivery)
  client.py          TrusteeClient SDK
  arena/
    runner.py        DealRunner — composes one deal, emits the tape
    api.py           FastAPI + SSE
    scenario.py      scenarios are data, not code
    scripted.py      ScriptedLLM (recorded moves, no network)
    rows.py          ledger Event -> grid row
    fixtures/        goods store, manifest, scenarios, transcripts, CLI runners
arena/ui/            Vite + React + AG Grid Community
tests/               340+ tests: agents, runner, API, SSE, PayPal request shapes, money path,
                     identity, mandates, store, platform API, approval, webhooks, A2A, arena scenarios
```

## Configuration

`.env` (all optional — empty means dry-run, no network):

| Var | Default | Meaning |
|---|---|---|
| `PAYPAL_CLIENT_ID` / `PAYPAL_CLIENT_SECRET` | empty | empty ⇒ dry-run with local order ids |
| `PAYPAL_ENV` | `sandbox` | `sandbox` \| `live` |
| `PAYPAL_RETURN_URL` / `PAYPAL_CANCEL_URL` | `{TRUSTEE_PUBLIC_URL}/v1/paypal/return` / `/cancel` | used by the payer-approval redirect |
| `LLM_BASE_URL` | NIM | any OpenAI-compatible endpoint |
| `LLM_API_KEY` | empty | empty ⇒ scripted mode |
| `LLM_MODEL` | `meta/llama-3.1-70b-instruct` | |
| `TRUSTEE_LEDGER_PATH` | in-memory | JSONL durability when set |
| `ARENA_APPROVAL_POLL_SECONDS` / `ARENA_APPROVAL_TIMEOUT_SECONDS` | `3` / `300` | how the arena waits for the buyer to approve in PayPal |
| `ARENA_SELF_URL` | `http://127.0.0.1:$PORT` | where the `outside` scenario reaches the arena's own `/v1` API |
| `TRUSTEE_DB_PATH` etc. | | see [Platform API](#platform-api-a2a) |

## Demo plan (≤3 min)

1. **Honest run** — Northwind's agent and Tidewater's agent negotiate 144 → 168 → 156
   for the Q3 returns dataset. Then the contract, escrow, delivery, machine-checked
   attestation (hash matches, schema passes, 5,000 rows), capture. Zero human money-touch.
2. **Cheat run** — the buyer caves at 170. Tidewater ships a junk export under the
   contracted file name: a renamed column, 312 rows, reused order ids. The sha256
   fails, the arbiter cites "sha256 matches contract", escrow voids. Northwind keeps
   its money. *That* is why the trust layer exists.
3. **Forged run** (20 s) — "Impostor seller". Someone signs as Tidewater with a key
   nobody registered. Point at the red `SIGNATURE REJECTED` row and the
   `signatures: forged — rejected` chip: the Trustee refuses before any money moves,
   and the tape has no PayPal rows.
4. **Outside agents** (30 s) — two agents that live on their own servers register,
   sign mandates and a contract and settle through `/v1`. Filter the grid to `api` to
   see each HTTP call, then to `identity` to see the four verified signatures.

With sandbox credentials, run the honest scenario once more live for the real-money-path
step: the page shows "waiting for buyer approval" and the **Approve in PayPal sandbox**
button. Approve as the sandbox buyer; the hold, delivery check and capture follow on the
tape. It is test money, and the page says so.

## Status

P1 (runner + service) and P2 (UI) are built and green. The payer-approval flow, PayPal webhooks and
reconciliation are implemented and tested against a mocked PayPal. P3, the real-sandbox run, is still open:
nobody has run `examples/sandbox_poc.py` or the webhook path with credentials, so the real-PayPal behavior is
unverified. `docs/BUILD-CRITIQUE.md` §1 lists what was broken in the adapter.

## License

Apache-2.0 (see LICENSE).
