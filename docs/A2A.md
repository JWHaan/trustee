# Trustee over A2A

Trustee speaks the Agent2Agent (A2A) protocol, so any A2A agent can use it as its escrow service and be told
when money moves, without polling and without knowing Trustee's REST API.

- **Implemented:** A2A specification **1.0.0**, JSON-RPC protocol binding, at `POST /a2a`.
  The v0.3 method names and wire shape are accepted too ([Compatibility](#compatibility)).
- **Left out:** streaming, the authenticated extended Agent Card, the gRPC and HTTP+JSON A2A bindings, task
  `history`, tenants, Agent Card signatures, extension negotiation. [Details below](#what-is-not-implemented).
- **Code:** `src/trustee/a2a/` (`server.py`, `skills.py`, `tasks.py`, `push.py`, `client.py`, `models.py`, `legacy.py`).

A2A is only the transport. Every contract, delivery and attestation carries an Ed25519 signature that
`DealService` verifies exactly as it does over REST, and a skill cannot reach the PayPal client by any path the
REST API does not have. Nothing a message says can pick an amount, a party or an endpoint.

## Discover

```bash
curl http://127.0.0.1:8000/.well-known/agent-card.json
```

`supportedInterfaces[0]` is `{"url": ".../a2a", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}`,
and `capabilities` is `{"streaming": false, "pushNotifications": true}`. The second entry, `.../v1`, is
Trustee's own REST API. It is **not** the A2A HTTP+JSON binding (there is no `message:send`); it is listed because
it is the same service. The card also carries the v0.3 field names (`url`, `preferredTransport`,
`additionalInterfaces`, `security`) pointing at the same JSON-RPC endpoint.

## Skills

A skill is invoked by `SendMessage` with **one DataPart** `{"skill": "<id>", "input": {...}}`.
(Alternative: put the skill id in `message.metadata.skill` and make the DataPart the input itself.) The input is
the body of the matching REST call, validated by the same models.

| Skill | Input (same as) | Who signs | REST equivalent |
|---|---|---|---|
| `register-principal` | `PrincipalIn` | principal key (proof) | `POST /v1/principals` |
| `register-agent` | `AgentIn` | agent key + principal | `POST /v1/agents` |
| `publish-mandate` | `MandateIn` | principal | `POST /v1/mandates` |
| `revoke-mandate` | `{agent_id, signature}` | principal | `POST /v1/mandates/{id}/revoke` |
| `open-escrow` | `Contract` with both signatures | buyer and seller | `POST /v1/deals` |
| `record-delivery` | `Delivery` | seller | `POST /v1/deals/{id}/delivery` |
| `attest-delivery` | `Attest` | buyer | `POST /v1/deals/{id}/attest` |
| `get-deal` | `{deal_id}` | none | `GET /v1/deals/{id}` |

`register-principal` needs the operator bearer token in the HTTP `Authorization` header of the `/a2a` request
when the server sets `TRUSTEE_OPERATOR_TOKEN`, as the REST route does. The operator override
`/v1/deals/{id}/resolve` is deliberately not a skill.

A message with only a TextPart gets an agent **Message** (no task) listing the skills and the input shape of each.

```json
{"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {
  "message": {"messageId": "m-1", "role": "ROLE_USER", "taskId": "deal-1",
              "parts": [{"data": {"skill": "attest-delivery", "input": { "...a signed Attest...": 0 }}}]}}}
```

## A deal is a task

`open-escrow` returns a Task with `id = contextId = deal_id`. Later messages carry `taskId = deal_id`.
Tasks are **views of the durable deal**, rebuilt from `DealService` on every read, so they survive restarts and
can never disagree with the money path.

| Deal | Task state | Meaning |
|---|---|---|
| CONTRACTED, PayPal order created, payer has not approved | `AUTH_REQUIRED` | `status.message` and `metadata.trustee.approveUrl` carry the PayPal approval link. Moves on by itself when PayPal reports approval. |
| CONTRACTED, no PayPal order (the call failed) | `FAILED` | Nothing is held or charged. `metadata.trustee.retryable` is true; send the same `open-escrow` again **without** a `taskId`. |
| ESCROWED | `INPUT_REQUIRED` | Funds are held. The seller must deliver. This is the state a seller's push notification announces. |
| DELIVERED | `WORKING` | The buyer is verifying. The next move is its signed attestation. |
| DISPUTED | `WORKING` | The arbiter, the dispute window or an operator decides next. No client message changes that, so it is not `INPUT_REQUIRED`. |
| SETTLED | `COMPLETED` | Captured to the seller. |
| VOIDED after funds were held | `COMPLETED` | Refunded. The escrow did its job, so this is a success, not a failure. |
| VOIDED with nothing held (payer cancelled, or the deadline passed before approval) | `CANCELED` | No money moved. |

`COMPLETED` tasks carry the artifact **`settlement-receipt`** (one DataPart):

```json
{"dealId": "deal-1", "outcome": "settled", "amount": "25.00", "currency": "USD", "buyer": "buyer", "seller": "seller",
 "contractDigest": "<sha256>", "orderId": "...", "authorizationId": "...", "captureId": "...",
 "closedBy": "buyer attested accept; funds captured", "ruling": null}
```

`outcome` is `settled` or `refunded`. A refund has `ruling` set (decision, clause, reasoning) and no `captureId`.

`status.timestamp` is the store's last write time, in UTC with a `Z`.

### When a message is refused

- **Malformed** messages are JSON-RPC errors: bad params `-32602`, unknown task `-32001`, a message to a
  finished task `-32004`, a `contextId` that is not the deal id `-32602`. Nothing runs.
- **Well-formed but refused** (bad signature 401, mandate refused 403 with `violations`, conflict 409, bad delivery
  422) comes back as a task:
  - no deal exists (a refused contract is never stored): a one-off task in `REJECTED`, with the reason in
    `status.message` (text plus a DataPart `{ok: false, status, detail, violations?}`). `GetTask` fetches it for a
    while (bounded in-memory table). The deal id stays free.
  - the deal exists: the response is the deal's **own task in its true state**, with the refusal in
    `status.message` and `metadata.trustee.lastRequest = {ok: false, status, detail}`. A forged delivery must not
    make a live escrow look dead, so it is not reported as `REJECTED`.
  - PayPal failed (502) on `capture`/`void`: same, plus `retryable: true`. The funds are where the task says; send
    the same message again. (`FAILED` is terminal in A2A, so it is only used for the one case that is truly
    terminal for that task: `open-escrow` where PayPal could not open the hold.)
- Use `trustee.a2a.client.last_refusal(task)` to read a refusal.

A message with `taskId` set must name an existing, non-terminal task (spec 3.4). The same signed
`attest-delivery` addressed only by the `deal_id` inside its payload (no `taskId`) goes to the service, which keeps
its REST behavior: replaying an accept on a settled deal is a no-op. That keeps retries after a lost response safe.

### Other methods

- `GetTask {id}`: the task, with artifacts.
- `ListTasks {contextId?, status?, pageSize?, pageToken?, statusTimestampAfter?, includeArtifacts?}`: deals,
  newest update first, keyset pagination (`pageSize` 1-100, default 50), `nextPageToken` always present (`""` on
  the last page), `artifacts` omitted unless `includeArtifacts`. **Without `contextId` it lists every deal, which
  REST never did, so when `TRUSTEE_OPERATOR_TOKEN` is set it needs that bearer token** (otherwise error `-32050`).
  With `contextId` (a deal id) it needs nothing, like `GET /v1/deals/{id}`. One-off registration and refusal tasks are
  not listed.
- `CancelTask {id}`: only while the task is `AUTH_REQUIRED` (before the payer approves). It takes the existing
  approval-cancel path, makes no PayPal call and returns the `CANCELED` task. Once funds are held it returns
  `TaskNotCancelable` (`-32002`): a held escrow ends by attestation, deadline or arbitration, never by cancel.
- `SendStreamingMessage`, `SubscribeToTask`, `GetExtendedAgentCard`: `UnsupportedOperation` (`-32004`).

Error codes: `-32700` parse, `-32600` invalid request, `-32601` method not found, `-32602` invalid params,
`-32603` internal, `-32001` task not found, `-32002` not cancelable, `-32003` push not supported, `-32004`
unsupported operation, `-32005` content type not supported, `-32009` version not supported, `-32050` unauthorized
(custom). All JSON-RPC errors are returned with HTTP 200. `A2A-Version` is checked: `1.0` and `0.3` are accepted.

## Push notifications

Instead of polling, the counterparty registers a webhook for the deal task:

```json
{"method": "CreateTaskPushNotificationConfig", "params": {
  "taskId": "deal-1", "url": "https://seller.example/a2a-hook", "token": "per-deal-secret",
  "authentication": {"scheme": "Bearer", "credentials": "..."}}}
```

or in the same `SendMessage` that opens the escrow: `configuration.taskPushNotificationConfig` (without
`taskId`). Also available: `GetTaskPushNotificationConfig`, `ListTaskPushNotificationConfigs`,
`DeleteTaskPushNotificationConfig` (idempotent). Creating with an `id` that already exists on the task is refused; delete first.

On every deal state change Trustee `POST`s the updated Task to the URL:

```
POST /a2a-hook
Content-Type: application/a2a+json
Authorization: Bearer ...                    (when `authentication` was given)
X-A2A-Notification-Token: per-deal-secret    (when `token` was given)

{"task": { ...the same Task GetTask returns... }}
```

The 1.0 spec defines the body (a StreamResponse holding the Task) and the `Authorization` header, but names no header
for `token`. Trustee sends it in `X-A2A-Notification-Token`, which the v0.3 spec and the reference SDKs use.
`trustee.a2a.client.push_token_matches(headers, token)` and `task_from_push(body)` verify and parse it.

Behavior:

- **The money path never waits.** The ledger subscriber only checks a set and enqueues a deal id. A worker thread builds
  the Task from the live deal and sends it, with a 10 s timeout, three retries with exponential backoff, and no retry
  on a 4xx other than 429. Failures are logged and dropped.
- **Latest state wins.** Pending notifications for one deal collapse into one, and a config is not sent a state it
  already received. Delivery is at-least-once, so make the receiver idempotent.
- **Persistent.** Configs live in the SQLite store (`push_configs`, created additively; the schema version is
  unchanged, so an older build still opens the file). A restart keeps them.
- **Validation.** `https://` only, except `http(s)://127.0.0.1`, `localhost` and `[::1]` for local agents. Credentials in
  the URL, literal private/link-local/reserved IPs (e.g. `https://10.0.0.5`, `https://169.254.169.254`) and control
  characters in `token`/`credentials` are refused with `-32602`. At most 5 configs per task. Redirects are not
  followed.
- **Secrets are write-only.** `token` and `authentication.credentials` are never returned by Get/List/Create and never logged.
  They are stored in the same SQLite file as everything else.

Who gets notified for what: the seller registers a webhook on the deal and is told `INPUT_REQUIRED` when the buyer's
funds are held ("ship now"). The buyer is told `WORKING` when the delivery is recorded ("verify now") and `COMPLETED`
with the receipt when the money moves.

## Client

```python
from trustee.a2a import A2AClient
from trustee.a2a.client import artifact_data

c = A2AClient("http://127.0.0.1:8000")          # or A2AClient(card_url=...), http=TestClient(app), authorization="Bearer ..."
task = c.call_skill("open-escrow", contract.model_dump(mode="json"))     # contract signed by both parties
c.set_push_config(task.id, "https://me.example/hook", token="secret")
task = c.call_skill("record-delivery", delivery.model_dump(mode="json"), task_id=task.id)
task = c.call_skill("attest-delivery", attest.model_dump(mode="json"), task_id=task.id)
receipt = artifact_data(task, "settlement-receipt")
```

`A2AClient` is not Trustee-specific: `fetch_card`, `send_message`, `call_skill`, `get_task`, `list_tasks`, `cancel_task`,
`set_push_config` and friends work against any JSON-RPC A2A agent. It signs nothing; build signed payloads with
`trustee.protocol` / `trustee.identity` first.

## Compatibility

Methods `message/send`, `tasks/get`, `tasks/cancel`, `tasks/pushNotificationConfig/set|get|list|delete` (and
`message/stream`, `tasks/resubscribe`, which are unsupported) are accepted and answered in the **v0.3 shape**: the
result is the bare Task or Message with a `kind` field, states are lower-case (`input-required`), roles are `user`/`agent`,
file parts are `{kind: "file", file: {bytes|uri, mimeType, name}}`, and push configs use `pushNotificationConfig` with
`authentication.schemes`. Configs registered through a v0.3 method are pushed the bare v0.3 Task.

## What is not implemented

- **Streaming** (`SendStreamingMessage`, `SubscribeToTask`): `capabilities.streaming` is false. Use `GetTask` or push.
- **`GetExtendedAgentCard`**: there is no authenticated extended card.
- **The gRPC and HTTP+JSON/REST A2A bindings.**
- **Task `history`**: Trustee keeps no per-message conversation, so `history` is never returned. The deal's transition log is
  `GET /v1/deals/{id}/events`.
- **`tenant`** (accepted, ignored), **Agent Card signatures**, **de-duplication on `messageId`** (deal ids and signed messages
  are idempotent instead), **client-chosen task ids** (a task id is the deal id the signed contract names).
- **Extension enforcement.** The card declares the Ed25519 signed-messages extension as `required`. A client that does not
  mention it is not turned away, because the signatures are verified on every payload regardless.

## Security notes and open risks

- **Authorization is the signatures, not the transport.** `/a2a` has no login. Whoever can call `GetTask` can read a deal
  they know the id of, as with `GET /v1/deals/{id}`.
- **`CancelTask` is unauthenticated**, as the REST approval-cancel (`GET /v1/paypal/cancel?token=<order id>`) already is.
  A caller who knows a deal id can cancel it while it awaits payer approval. Funds are never held at that point, and
  it cannot touch an escrowed deal.
- **Push config calls are unauthenticated**, like every A2A call here. Creating with an id that already exists is refused
  (so nobody can redirect another agent's webhook), but anyone who knows a deal id can list or delete its configs, or
  fill its 5 slots so the real counterparty cannot register. That costs the victim its notifications, not money or data
  (`GetTask` still works and the task is as public as `GET /v1/deals/{id}`). Pass the webhook in the same `SendMessage` that
  opens the escrow to claim a slot first. Tying config changes to a deal party's signature is the next step if this matters.
- **Push URLs are caller-chosen.** Trustee will POST deal updates to any validated URL a caller registers on a deal it knows
  the id of. The caps (5 configs per task, a bounded queue, 10 s timeouts) limit abuse, but DNS names are **not resolved**
  during validation, so a hostname pointing at a private address is not caught. Run Trustee where outbound traffic to
  private ranges is blocked.
- **ListTasks without `contextId`** is open when no operator token is configured (the hackathon default, same as principal
  registration).
- Tested against mocked and in-process PayPal only, like the rest of the sandbox flow.
