# Speed, scale and accountability

How to run Trustee at PayPal transaction volumes without losing what makes it worth running: every money movement
is authorized, recorded before it happens, and checkable afterwards by someone who does not trust the operator.

This builds on [PAYPAL_SCALE.md](./PAYPAL_SCALE.md) §4.5 to §4.7. Numbers in §1 were measured on this repository;
targets in §5 are estimates to validate with load tests and with PayPal.

## 1. Where the time goes today (measured)

One deal through the signed `/v1` API (open, deliver, attest), dry-run PayPal, SQLite on disk, one thread:

| Measure | Value |
|---|---|
| Trustee's own work per deal | about 15 ms in total (about 5 ms each for open, deliver and attest) |
| Ledger events per deal | 19, each committed as its own SQLite transaction |
| Share of Trustee time spent writing ledger events | about 57% |
| Ed25519 signature checks per deal | 8, about 0.13 ms each (about 7% of the time) |
| Event hashing | about 6% |
| End-to-end through the test HTTP client | about 27 deals/s, p99 under 20 ms per call |

Batching one request's events into a single commit cuts event-write time about 9 times in a micro-benchmark (2.7 ms
to 0.3 ms per deal with `synchronous=FULL`). The absolute numbers depend on the disk; the ratio is the point.

Three conclusions:

1. **Trustee's own logic is not the bottleneck.** PayPal is. A deal makes about three PayPal calls (create and
   authorize, then capture or void), each an internet round trip. Those dominate latency, and PayPal's rate limits
   bound throughput **(confirm limits)**.
2. **The write pattern is.** One commit per event, behind one process-wide lock (`Store._lock`), serialises every
   deal on the server.
3. **The design already partitions well.** The ledger is hash-chained per deal, the state machine is per deal, and
   PayPal requests carry deterministic idempotency keys (`trustee-<deal>-<op>`). Nothing needs a global order. That
   is what makes horizontal scale possible.

## 2. Two accountability gaps to close first

Speed must not be bought by weakening the record. Two gaps existed, and both get worse under load. **Both are now
fixed** (§6, steps 1 and 2); the descriptions are kept because the same rule carries into the target design.

**2.1 The money call happens before the deal is durable.** `DealService.open_deal` calls PayPal through
`Trustee.on_contract` and writes the deal row only afterwards (`_persist`). If the process dies after PayPal
authorizes but before that write, the buyer's funds are held at PayPal with no deal row to sweep or reconcile. The
events up to `CONTRACTED` are stored, and a client retry of the same `deal_id` recovers safely thanks to the
deterministic request id, but nothing on the server side recovers it alone. The README's claim that "a restart cannot
strand a PayPal hold" held only for deals that were already written.

*Fixed:* before every PayPal call that can hold or move money, the Trustee records the operation and its request id
(`pending_money`) and the service writes the deal row with it (`DealService._write_ahead`). A deal whose
escrow-opening call was recorded but never answered is finished by the next sweep, or by a client retry, with the
same request id (`Trustee.resume_pending`). `tests/test_durability.py` crashes the process right after PayPal
authorizes and checks that the restarted service ends with one hold, not zero or two.

**2.2 A failed ledger write is logged and ignored.** `Store.attach` swallows persistence errors on purpose so the
money path never stalls. The consequence is a PayPal capture with no durable record of it. Under load, with full
disks or a failing database, that becomes likely.

*Fixed:* a ledger event that cannot be written joins an ordered backlog and is retried before newer events, so each
deal's chain lands intact. No PayPal call is made while the backlog or the intent cannot be written: the request
answers `503` and nothing moves. A deal state that fails to save after money moved is rewritten on every sweep.
`/healthz` reports both as `durability: {events_waiting, deals_waiting}`, which should stay at zero.

The rule that fixes both: **write the intent, then move the money, then write the result.** No PayPal call without a
committed record that it is about to happen; no state change without its events in the same transaction.

## 3. Target architecture

```
              agents (REST /v1, A2A JSON-RPC)
                          │
         ┌────────────────▼─────────────────┐
         │ API tier (stateless, N replicas)  │  signature checks, schema, idempotency lookup,
         │                                   │  mandate + intent rules (pure, cached inputs)
         └────────────────┬─────────────────┘
                          │ one transaction per request:
                          │   deal row (version++) + ledger events + outbox row
         ┌────────────────▼─────────────────┐
         │ Deal store, partitioned by deal   │  Postgres; per-deal row lock or version check;
         │ (deals, events, outbox, receipts) │  no global lock
         └───────┬──────────────────┬───────┘
                 │ outbox           │ events (async copy)
   ┌─────────────▼──────────┐   ┌───▼──────────────────────────┐
   │ Money workers          │   │ Evidence tier                 │
   │ PayPal calls with the  │   │ checkpoints over chain heads, │
   │ deal's request id;     │   │ signed and published; Elastic │
   │ result written back in │   │ read models; reputation       │
   │ a new transaction      │   └──────────────────────────────┘
   └─────────────┬──────────┘
                 │
           PayPal Orders / Payments ── webhooks ──► webhook queue ──► same workers
                 ▲
   ┌─────────────┴──────────┐
   │ Reconciler (continuous)│  Trustee ledger ↔ outbox ↔ PayPal; any mismatch is an incident
   └────────────────────────┘
```

### 3.1 Decision path: stateless and parallel

- Everything that decides whether money may move is pure code over inputs the request brings or the store holds:
  signature checks, the mandate gate and the 14 intent rules. Run it on any number of API replicas.
- Signature checks cost about 0.13 ms on one core. Eight per deal at 1,000 deals per second is about one core.
- Keep inputs that live elsewhere off the hot path. The Elastic reputation lookup already caches for 30 seconds and
  fails closed (`elastic/reputation.py`, `mandate.py`). Do the same for every external input.
- The LLM is never on this path. It negotiates before the contract and explains afterwards. Keep it that way.

### 3.2 State path: one writer per deal, one transaction per request

- Move from SQLite to Postgres. Each deal row carries a `version`. A request reads the row, applies the state
  machine, and commits with `WHERE version = :read_version`. A conflict retries. No process-wide lock.
- Write the deal row, every ledger event the request produced and any outbox entry in **one transaction**. The
  events of one request are batched, which is the measured 9x saving. The chain hash is computed before commit from
  the previous head stored on the deal row, so the chain cannot fork under concurrency.
- Partition tables by deal (hash of `deal_id`), or by principal for tenant isolation. Deals never join each other on
  the hot path, so partitions scale out.
- Keep the deterministic PayPal request ids. They make every retry, from any worker, safe.

### 3.3 Money path: a transactional outbox

- The request that decides "authorize" or "capture" commits an outbox row: `{deal_id, op, request_id, amount}`.
  Then it returns, or waits a short time for the result.
- Money workers take outbox rows, call PayPal with `PayPal-Request-Id = request_id`, and commit the result with its
  events. A crash at any point is retried with the same id, and PayPal returns the first result instead of moving
  money twice **(confirm the idempotency window)**.
- This closes §2.1: an authorization is always preceded by a durable intent, so the sweeper and the reconciler can
  always find it.
- Webhooks go into a queue and are applied by the same workers under the same per-deal versioning. They are already
  verified and deduplicated by event id (`paypal.py`, `platform/routes.py`).

### 3.4 Asynchronous by default

Agents care about throughput and certainty, not about holding an HTTP connection open while PayPal answers.

- `POST /v1/deals` returns `202 Accepted` with the deal in `CONTRACTED` and an escrow status of `pending`. A2A
  already fits this: a deal is a long-running task (`a2a/tasks.py`) and Trustee pushes every state change to the
  parties (`a2a/push.py`), so a seller learns "funds held" without waiting on the open call.
- Keep a synchronous mode with a timeout for simple clients: wait up to N seconds for the worker, then fall back to
  `202`.

### 3.5 Fewer PayPal calls per deal

PayPal round trips dominate latency and cost, so the biggest speed gain is calling PayPal less.

- **Budget holds for repeat partners.** One authorization covers a signed budget, and each verified deal is a partial
  capture against it (`partial.py` already does the arithmetic). A new deal then needs no PayPal call to start, only
  a reservation in Trustee's store **(confirm limits on captures per authorization)**.
- **Batched capture.** Capture many small verified deals in one call on a short timer, with the per-deal breakdown in
  the ledger and receipts.
- **Netting and PYUSD** for very small, very frequent deals ([PAYPAL_SCALE.md](./PAYPAL_SCALE.md) §4.4).

### 3.6 Evidence path: verifiable without trusting the operator

Per-deal hash chains (`ledger.py`) and signed receipts (`receipts.py`) already let anyone check one deal offline.
At scale, also prove that the operator shows everyone the same history and never rewrites it:

- **Checkpoints.** Every few seconds, build a Merkle tree over the current head hash of every deal that changed, sign
  the root with the Trustee key, and publish it as an append-only log, the way Certificate Transparency logs work.
- **Inclusion proofs in receipts.** A receipt carries its deal's final head hash, the checkpoint id and the Merkle
  path. `python -m trustee.verify` then checks the signature, the chain and the inclusion, all offline.
- **Consistency.** Each checkpoint commits to the previous one. Partners, PayPal or an auditor keep copies, so a
  rewritten history would not match theirs.
- **Keys.** Sign checkpoints with a key held in an HSM or KMS (low rate, high value). Sign receipts with short-lived
  keys that the HSM key certifies, and add a `kid` to every receipt so keys can rotate.
- **Neutral delivery checks.** Verifiers run on content-addressed deliveries and record their name, version and
  input hash with the result. Anyone can re-run them and get the same answer
  ([PAYPAL_SCALE.md](./PAYPAL_SCALE.md) §4.1).

None of this is on the request path. Checkpointing reads committed events and costs one hash per changed deal.

### 3.7 Reconciliation: proving the ledger matches the money

- Continuously compare three records: Trustee's ledger, the outbox and PayPal's view of each order and
  authorization (order lookups plus webhooks; transaction reporting for a daily full pass).
- Every difference is an incident with an owner: a hold with no deal, a capture with no event, a deal `ESCROWED`
  whose authorization expired.
- The headline accountability metric: **unreconciled money movements = 0**, checked daily and alarmed in real time.

### 3.8 Reads, history and analytics

- Serve deal views and event pages from read replicas. Serve search, reputation and dashboards from the Elastic copy
  of the ledger (`elastic/`), which is already off the money path.
- Archive old partitions to object storage with their checkpoint proofs, so they stay verifiable after deletion
  from the hot database.

### 3.9 Regions

- Home each deal in one region (encode it in `deal_id`) and keep all of its writes there. No cross-region
  transaction on the hot path.
- Each region publishes checkpoints; a global checkpoint covers the regional roots.

## 4. Failure modes and what happens

| Failure | Today | Target |
|---|---|---|
| Crash after PayPal authorizes, before the result is written | Fixed: the intent was written first; the next sweep or a client retry finishes the call with the same id | Outbox row existed first; a worker retries with the same id and records the result |
| Store write fails around a money call | Fixed: no PayPal call while the intent or earlier events cannot be written (`503`); late events are retried in order | State, events and outbox share one transaction; nothing moves without its record |
| PayPal timeout on capture | Deal stays put; retry is safe; webhook reconciles (`reconcile` rows) | Same, plus the reconciler checks every open outbox row against PayPal |
| Two requests for one deal at once | Per-deal lock in one process | Version check in the database, across all replicas |
| Operator edits history | Detected per deal by `verify_chain` if someone holds a receipt | Detected globally: checkpoints held by others stop matching |
| Signing key leaked | One key, no id, manual replacement | Rotate: new `kid`, old receipts still verify against the published key history |

## 5. Capacity, worked example

Assume 10 million deals a day: about 116 per second on average, about 1,200 per second at a 10x peak.

| Resource | Per deal | At 1,200 deals/s | How |
|---|---|---|---|
| API requests | 3 | 3,600/s | stateless replicas |
| Signature checks | 8 | 9,600/s, about 1.3 cores | any replica |
| Database transactions | 3 (one per request) | 3,600/s | partitioned Postgres |
| Ledger rows | 19 | about 23,000 rows/s, written in batches | same transactions |
| PayPal calls | about 3, fewer with budget holds and batching | up to 3,600/s | needs a capacity agreement with PayPal **(confirm)** |
| Checkpoints | one hash per changed deal | about 1,200 hashes/s, one signature per interval | evidence tier |

Today's single process does Trustee's part at roughly 65 deals per second per thread (about 15 ms per deal) behind one
lock. The target is about 20 times that at peak, reached by removing the lock and the per-event commits and running
more replicas, not by making any single step faster.

## 6. Order of work in this repository

1. **Done. Write before the money call (§2.1).** The deal and its pending PayPal operation are written before the
   call; an unanswered escrow opening is resumed with the same id. Crash-injection tests in
   `tests/test_durability.py`.
2. **Done. Fail closed before money (§2.2).** No PayPal call while the intent or the ledger backlog cannot be
   written; failed writes are retried, not dropped, and reported by `/healthz`.
3. **One transaction per request.** Buffer a request's ledger events and commit them with the deal row. Keep the
   chain head on the deal row.
4. **Receipt key ids and a checkpoint endpoint.** Add `kid` to receipts, `GET /v1/checkpoints/latest` and inclusion
   proofs in receipts, and teach `trustee.verify` to check them.
5. **Outbox and workers.** Move PayPal calls behind an outbox table and a worker loop. SQLite can host it at first;
   the move to Postgres then changes storage, not design.
6. **Reconciler.** A job that compares open outbox rows and `ESCROWED` deals with PayPal and writes `reconcile` or
   `paypal_alert` rows.
7. **Load test.** A Locust or k6 run against the API with a PayPal mock that adds realistic latency and errors, to
   replace the estimates in §5 with measurements.

Steps 3 and 4 are where speed and verifiability improve together. Which hackathon sponsor tools help with each
step is in [SPONSORS.md](./SPONSORS.md).
