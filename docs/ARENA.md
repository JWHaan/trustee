# The arena

The arena is Trustee's demo: a web page that runs a complete deal between two AI agents, with Trustee holding the
money, and shows what happened to the money and why. The hosted copy is at https://trustee-arena.onrender.com. To run
it yourself, see the [quickstart](../README.md#quickstart).

## Scenarios

There are ten, all in `src/trustee/arena/fixtures/scenarios.json`. The UI lists six of them; the other four are
always runnable by name and appear in the UI only when their flag is set:

| Scenario | What it proves | Ends |
|---|---|---|
| `honest` | Negotiated deal, machine-checked delivery, capture. | `SETTLED` |
| `cheat` | A junk delivery fails the hash and schema checks; the arbiter cites the clause and voids. | `VOIDED` |
| `forged` | Someone who is not Tidewater signs the contract as Tidewater (an unregistered key). Trustee rejects it with `rejected_identity` before PayPal is called: zero PayPal requests. | `outcome: rejected_identity` |
| `outside` | Northwind's and Tidewater's agents act as outside clients of this server's own public `/v1` API (registration, signed mandates, contract, delivery, attestation), each HTTP call shown as an `api` row next to the platform's own rows. | `SETTLED` |
| `a2a` | Northwind and Tidewater are separate A2A agents: they find each other by Agent Card, negotiate with signed offers, open the escrow at Trustee, and Tidewater ships when Trustee pushes "funds held". | `SETTLED` |
| `overreach` | The human said "at most $175"; the seller will not go below $190. The signed intent mandate refuses to pay unattended and steps up to a human approval (simulated in dry-run). | `SETTLED` after step-up |
| `partial` | 500 of 5,000 rows fail the row checks; a signed pro-rata term captures the valid 90% ($140.40 of $156.00) and releases the rest. Listed with `ARENA_PARTIAL_SCENARIO=1`. | `SETTLED` (partial) |
| `pricefeed` | A competitor price feed; the buyer samples 20 rows and checks them against live Channel3 offers before paying. Listed with `ARENA_PRICEFEED_SCENARIO=1`. | `SETTLED` |
| `pricefeed-stale` | The hash matches, but the prices are months old; the market check fails and the escrow is voided. | `VOIDED` |
| `pricefeed-mixed` | 8 of 40 rows are wrong; pro-rata capture of the sampled rows that checked out. | `SETTLED` (partial) |

`outside` calls the arena's own `/v1` over a real socket: `http://127.0.0.1:$PORT` by default (`PORT`, default
8000), or `ARENA_SELF_URL` if the server is reachable under another address. It sends `TRUSTEE_OPERATOR_TOKEN`
when one is set. Its agents get a fresh id and key per run (`northwind-planner-<run>`, `tidewater-sales-<run>`).

## The page

One answer leads: what happened to the money and why, in the words of the deal's `summary` (the same
text agents get from `GET /v1/deals/{id}` and the A2A task status). Under it come a line that shows where the
money is (buyer, PayPal hold, seller), a step-by-step progress bar and **How Trustee knows**: up to four questions
(was it really them, were they allowed to make this deal, is it the file the contract names, does it have the
promised columns and rows), each answered from the checks Trustee ran. A note says who decided: Trustee's code or,
in a dispute with no objective breach, an AI arbiter that can only cite clauses already in the contract. Last come
the PayPal order id and a link to the deal's signed evidence pack. The full record is one click away under
**See how it happened**:

- **Conversation:** what the agents said and did.
- **Checks:** signatures, spending rules and the delivery, failures first.
- **Ledger:** the raw tape in AG Grid, with `signer` and `check` columns, kind filters and search.

The header says whether PayPal is in the sandbox or a dry run.

## Modes

`auto` (default) and `live` negotiate with the live model when `LLM_API_KEY` is set. If the model fails, the run
falls back to the recorded transcript and says so on the tape; without a key, both replay the recording. `scripted`
always replays the recorded negotiation and makes no model calls. In every mode the money path runs for real:
against the PayPal sandbox when PayPal credentials are set, otherwise as a dry run. Pick the mode in the page's
**Agents** menu, or add `?mode=scripted` (or `?mode=live`) to the URL so the run that starts on load uses it too.

## Approving in the PayPal sandbox

With sandbox credentials, no scenario holds funds until the buyer approves. The
run stops at `CONTRACTED`, writes an `awaiting_approval` row carrying the approve link and shows an
"Approve in PayPal sandbox" button with a countdown. The arena polls PayPal every `ARENA_APPROVAL_POLL_SECONDS`
(default 3); once the order is `APPROVED` the run continues to delivery and attestation. No approval within
`ARENA_APPROVAL_TIMEOUT_SECONDS` (default 300), or a click on cancel in PayPal, voids the deal with no money call
(`outcome: approval_timeout` / `approval_cancelled`). PayPal's return and cancel redirects for an arena order
(`/v1/paypal/return`, `/v1/paypal/cancel`) answer `303` to `/`; cancel also ends that run's wait. Set
`TRUSTEE_PUBLIC_URL` (or `PAYPAL_RETURN_URL` / `PAYPAL_CANCEL_URL`) so PayPal can send the buyer back. This path is
tested against a mocked PayPal only, like the rest of the sandbox flow.

The exception is `PAYPAL_DEMO_VAULT_ID`: with it set, `honest`, `cheat`, `outside` and `a2a` pay under the
buyer's signed intent mandate with no approval click ([AUTONOMY.md](AUTONOMY.md)). The hosted demo at
https://trustee-arena.onrender.com uses the sandbox without it, so approving a run there needs a PayPal sandbox
buyer account.

## Demo script (about three minutes)

Run it on a local dry run (see the [quickstart](../README.md#quickstart)), where no step waits for a PayPal approval.
On the hosted demo, every deal that reaches PayPal first waits for a sandbox buyer to approve the payment.

1. **Honest run** — Northwind's agent and Tidewater's agent negotiate 144 → 168 → 156
   for the Q3 returns dataset. Then the contract, escrow, delivery, machine-checked
   attestation (hash matches, schema passes, 5,000 rows), capture. Zero human money-touch.
2. **Cheat run** — the buyer caves at 170. Tidewater ships a junk export under the
   contracted file name: a renamed column, 312 rows, reused order ids. The sha256
   fails, the arbiter cites "sha256 matches contract", escrow voids. Northwind keeps
   its money. *That* is why the trust layer exists.
3. **Impostor** (20 s) — someone signs as Tidewater with a key nobody registered. The
   page says "Stopped before payment. No money moved." and the Checks tab shows the one
   failed signature: the Trustee refuses before PayPal is called.
4. **Outside agents** (30 s) — two agents that live on their own servers register,
   sign mandates and a contract and settle through `/v1`. In the Ledger tab, filter to
   `api` to see each HTTP call, then to `identity` to see the four verified signatures.

With sandbox credentials, run the honest scenario once more for the real money path: the page shows "Waiting for
the buyer to approve the payment in PayPal" and an **Approve in PayPal sandbox** button. Approve as the sandbox
buyer; the hold, delivery check and capture follow on the tape. It is test money, and the page says so.

## Arena API

| Endpoint | Purpose |
|---|---|
| `POST /api/runs` `{scenario, mode}` | start a run, returns `{run_id}` |
| `GET /api/runs/{id}` | state snapshot: deal state, contract, escrow, ruling |
| `GET /api/runs/{id}/receipt` | the run's signed settlement receipt |
| `GET /api/runs/{id}/evidence` | the run's signed dispute evidence pack (the page's "Evidence pack" link) |
| `GET /api/runs/{id}/events` | SSE: one `msg` per ledger row, then `done` |
| `GET /api/ledger?limit=500` | flat rows for the grid |
| `GET /api/scenarios` | fixture-driven scenario list (the ones the UI shows) |
| `GET /healthz` | liveness, whether PayPal is dry-run, and `durability` (ledger events and deal states not yet on disk; both should be 0) |

SSE is replay-first: a subscriber is pre-filled with everything the run has
already emitted, then fed live, so refreshing mid-run shows the same tape in
the same order. Runs execute on a worker thread (`asyncio.to_thread`) because
the agents are synchronous; ledger callbacks are marshalled back onto the event
loop with `call_soon_threadsafe`.
