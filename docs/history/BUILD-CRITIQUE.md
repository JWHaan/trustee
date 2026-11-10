> **Historical review.** Written against an early build (24 tests). The adapter defects in §1 have since been
> fixed and are covered by the current suite; the open item that remains is a real-sandbox run (README, Status).

# Critique of `BUILD.md`

Reviewed against the code it builds on (`src/trustee/*`, 24 tests green, mypy +
ruff clean), and against PayPal's Orders v2 / Payments v2 API reference.

Verdict up front: **the architecture is right and the sequencing is wrong.**
The doc's central bet — "the platform adds orchestration, service, UI, fixtures,
hosting; no agent logic changes" — is about 80% true, and the 20% that isn't is
where all the work is. The bigger problem is that the only existential risk
(does the real PayPal money path work?) is scheduled on day 10 of 14, while the
doc calls it "the day-1 risk" in the same paragraph.

Three defects would have blocked P3 outright. Two of them are in code the doc
marks `done`.

---

## 1. Blocking — the money path does not work against real sandbox

### 1.1 `capture` calls the wrong endpoint for an AUTHORIZE-intent order

`paypal.py` created the order with `intent=AUTHORIZE`, then captured with:

```
POST /v2/checkout/orders/{order_id}/capture
```

That endpoint is only valid for **CAPTURE**-intent orders. For an AUTHORIZE
order the money sits on the *authorization*, and PayPal requires:

```
POST /v2/payments/authorizations/{authorization_id}/capture
```

`void` already used the authorization endpoint correctly, so the adapter was
internally inconsistent — a strong hint this path was never exercised. P3's
DoD ("honest run settles against **real sandbox** escrow") would have failed on
the first capture, on day 10, with a 422.

### 1.2 An AUTHORIZE order cannot be authorized without payer approval

`create_and_authorize()` created the order and immediately authorized it. Real
sandbox returns `422 ORDER_NOT_APPROVED`: an AUTHORIZE-intent order must be
approved by the payer first (redirect to the `approve` link, or a vaulted
payer). There is no headless path from `create` straight to `authorize` for a
fresh payer.

This is not a bug so much as a missing design decision, and it's the one that
decides what the demo *is*:

- **(a) Approval flow** — create the order, hand the UI an `approve_url`, the
  buyer approves in a real PayPal sandbox window, then authorize. Slower, but
  the judge watches real PayPal move real sandbox money. This is what I built
  support for (`approval_flow`, `authorize_after_approval`, `approve_url` on
  `EscrowOrder`), and the trustee now refuses to enter `ESCROWED` unless the
  authorization is actually held.
- **(b) Vaulted/pre-approved payer** — keeps the run headless but needs a
  billing agreement, which is a bigger build than the rest of P3.

Either way it's a P3 *design* task, not a "wire the env vars" task, and §4/§6
budget it as the latter.

Also: `purchase_units[].payer` is not a field in Orders v2. `payer` lives at the
top level under `payment_source`; as written the create call is malformed
before it ever reaches authorization.

### 1.3 "No creds ⇒ dry run, so the hosted demo cannot 500" was inverted

```python
self.dry_run = client_id and client_secret if dry_run is None else bool(dry_run)
```

With both env vars empty this evaluates to `""` — falsy, but not `False`. So
`if self.dry_run:` was `False` and the adapter went to the network with no
credentials. Reproduced:

```
escrow failed: token endpoint failed (401)
```

§3's claim that "the hosted demo *cannot* 500 for lack of config" was exactly
backwards: with no config it was guaranteed to fail, and it would have failed
on the demo URL rather than on a laptop. Fixed to a real bool.

---

## 2. Serious — the 20% the "no agent logic changes" claim hides

Each of these is a day if you find it during P1/P2 instead of before.

### 2.1 There is no bridge from sync agents to async SSE

§2.1 says "the runner just subscribes the ledger". `Ledger.append` is
synchronous with no subscriber mechanism, and the agents are blocking
throughput (httpx, the OpenAI client, `time.sleep` inside the retry loop). A
run therefore has to execute on a worker thread, and every mutation of the run
record has to be marshalled back to the event loop with
`call_soon_threadsafe`. Writing to an `asyncio.Queue` from the worker thread
directly is the classic way to get a stream that stalls under load — and it
stalls exactly when a judge clicks. This is the hardest single piece of P1 and
the doc doesn't mention threading at all.

### 2.2 The grid's signature column had no data source

§2.3 specifies `state_from → state_to` in the ledger grid. `Deal.transition`
appends to `deal.history`; nothing ever writes a state transition to the
ledger. Worse, draining `history` *after* each phase puts the state row after
the PayPal call it caused, so the tape reads as if the money moved before the
state changed. Fixed with an `on_transition` callback fired inside
`transition()`.

### 2.3 `Contract.from_accept` swaps the parties when the seller accepts

```python
buyer = accept.sender  # only true when the buyer is the one accepting
```

The seller's `respond_to` accepts as soon as the price is at or above floor —
the *normal* outcome of the clamp — and it returns an `Accept` whose sender is
the seller. `Contract.from_accept` then sets `buyer="seller-agent-1"`, and the
mandate gate looks for a **spend** mandate on the seller, finds a *receive*
mandate, and refuses a perfectly good deal. The honest scenario dies at the
mandate step.

### 2.4 One deal, four deal ids

`_Msg` auto-generates `deal_id`; `Negotiator._coerce` keeps it only if the LLM
emitted one (and `_nudge_deals` strips anything unrecognised). So the offer,
each counter, the accept, and the contract can all carry different ids, and
`SellerAgent._log` hardcoded `deal_id=None` so seller events were
unattributable anyway. The grid groups by deal: one negotiation would render as
four groups plus an orphan. §2.1's "one run = one deal" needs explicit pinning.

### 2.5 A terse model fails validation, and `open_deal` doesn't retry

`_coerce` merges the LLM's `good` over the agent's known spec *only if the LLM
sent a `good` object*. Omit it and Pydantic raises `good: Field required` —
`open_deal` has no retry loop, unlike `respond`. Live mode would fail on turn
one, intermittently, in a way that looks like a network problem.

### 2.6 Criterion 2 never inspected the delivery

`BuyerAgent.verify_delivery` ran the registered checker against
`goods_dir/{good_id}.bin` — a fixture path — while `SellerAgent.deliver` only
computed a hash and never moved bytes. So "sha256 in manifest" was genuinely
machine-checked and "tests pass" was theatre: the checker read a file that had
nothing to do with what was delivered. Since "machine-verified attestation" is
the product's core claim, this is the one a judge is most likely to catch by
opening `buyer.py`.

Fixed by giving the seller an inventory and the buyer an inbox, so delivery is
a real byte transfer and the checker reads what arrived.

### 2.7 §2.4 lists fixtures but never defines the scenario

§2.1 says "scenario fixtures drive it" and "no code difference between them",
but no file holds ask / floor / budget / opening price. Without that, prices
live in the runner and the claim is false. Added `scenarios.json`.

---

## 3. Moderate

| # | Issue | Consequence |
|---|---|---|
| 3.1 | §5's cold-start mitigation (`render.yaml` health-check every 30 min) doesn't work | `healthCheckPath` is a **deploy-time** check. Free instances sleep after 15 min of no inbound traffic. You need an external pinger (UptimeRobot / cron-job.org / scheduled Action). |
| 3.2 | §2.2 implies ledger durability on Render | Free tier has no persistent disk. JSONL is ephemeral and the run registry is in-memory, so a restart loses history. Fine — but say so, and make the ledger write failure-tolerant (a read-only FS must not kill a run). |
| 3.3 | §4 start command `uv run --extra all uvicorn …` | There is no `all` extra (`pyproject.toml` has only `dev`). Also `uv run uvicorn` resolves the **global** uvicorn on a normal machine, not the project venv — use `python -m uvicorn`. |
| 3.4 | Three contradictory mode models | §2.1 has live/scripted + implicit fallback; §2.3's UI offers `live/scripted/auto`; the diagram says `{scenario, mode, live?}`. Pick three with defined semantics. |
| 3.5 | "Auto-fallback after 2 consecutive LLM failures **per run**" | Implies mid-run surgery: swapping the LLM mid-negotiation means repositioning a scripted queue at the current turn. Run-level fallback is ~20 lines and observable on the tape; mid-run is a day and fragile. |
| 3.6 | AG Grid specifics absent | v32+ ships no `ag-theme-quartz-dark.css` (dark mode is the Theming API or `data-ag-theme-mode`), and the CSS-import path is legacy (pre-v33). "Community, pinned" without a version is not a decision — and the wrong guess renders a blank grid. |
| 3.7 | Test plan misses every bug above | No test that a mandate refusal never touches PayPal (the headline claim), none that capture happens exactly once, none for the fallback path, none for the thread→async bridge. "Parity minus timestamps/ids" is undefined, so it's flaky by construction. |
| 3.8 | P3 on days 10–12 of 14 | Contains the only existential risk. The doc calls sandbox integration "the day-1 risk" and then schedules it at P3. If §1.1/§1.2 are wrong — they were — there is no runway. |

### Minor

- §2.4's `goods/lambda-module-1.bin` conflicts with the code's convention:
  `deliverable_ref` is relative to `goods_dir`, so it would resolve to
  `goods/goods/lambda-module-1.bin`.
- `Trustee._explanation` makes one LLM call **per state transition** (~6/run).
  Invisible in scripted mode, ~6 extra round-trips of demo latency in live.
- "24 tests" is accurate, but the suite needs a writable `TMPDIR` (pytest
  `tmp_path`); worth a CI note.

---

## 4. What I'd change in the plan itself

1. **Move the sandbox POC to day 1.** Before any runner code. It is the only
   thing that can invalidate the pitch, and it takes an afternoon with creds in
   hand. Everything else is recoverable in a day; a wrong money path is not.
2. **Decide the PayPal story explicitly** (§1.2). Approval flow is the honest
   one and it makes better theatre — a judge watching the PayPal sandbox window
   is worth more than a spinner. But it changes the UI, so decide before P2
   locks the layout.
3. **Restate the "no agent changes" claim** as "no policy changes". The runner
   needs deal-id pinning, party naming, a transition hook, and a ledger
   subscriber. All mechanical, none of it policy — but it's real work.
4. **Replace "subscribe the ledger" with the threading model.** One paragraph
   saves a day.
5. **Give P1 a DoD that fails loudly.** "Prints the ledger and exits SETTLED"
   is good. Add: "a mandate refusal never constructs a PayPal call" and
   "exactly one capture per settled deal".
6. **Default the hosted UI to `auto`, not `live`.** Same intent as §5, one less
   state to explain, and it degrades on a rate limit instead of failing.

---

## 5. What was built (and what wasn't)

Built, tested, and running: DealRunner + scenario/transcript fixtures + CLI
runners; `ScriptedLLM`; FastAPI service with replay-safe SSE, snapshot, ledger
rows, scenarios, health; the React + AG Grid UI (trust strip, transcript, tape,
controls); Docker + `render.yaml`; 45 tests; mypy + ruff clean.

Deliberately **not** built, because they need something I don't have:

- **P3 real-sandbox POC** — needs live sandbox credentials. The adapter is
  fixed and approval-aware, but the redirect round-trip is unverified against
  the real API. This is still the top risk and still the first thing to do.
- **Webhook ingestion** (`PAYMENT.CAPTURE.COMPLETED`) — P3 stretch. The state
  machine is already idempotent, so it's a receiver plus a signature check.
- **Demo video, Devpost copy, licence-in-About** — P4, human work.
