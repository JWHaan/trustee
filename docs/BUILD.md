# Platform Build Plan — the Trustee Arena

What houses and drives the four agents (Trustee, Buyer, Seller, Arbiter) into
one running product: a **DealRunner**, a FastAPI **arena service**, a single-page
**arena UI** with an AG Grid ledger tape, demo **fixtures**, and a **Render**
hosting setup. Deadline: **Nov 12, 2026 15:00 EST**. Solo build.

> **Status:** P1 + P2 are built and green (45 tests, mypy + ruff clean).
> Corrections to this document, with evidence, are in
> [`BUILD-CRITIQUE.md`](./BUILD-CRITIQUE.md). Read §1 of that file before
> touching P3 — two calls in `paypal.py` could not have worked against real
> sandbox, and the "no creds ⇒ dry run" guarantee was inverted.

---

## 0. Amendments applied to this plan

| § | Was | Is now | Why |
|---|---|---|---|
| 3 | "absence of creds switches the adapter to dry-run" | true only after `paypal.py` was fixed | `client_id and client_secret` returns `""`, not `False` — it went to the network and 401'd |
| 4 | `uv run --extra all uvicorn …` | `uv run --no-sync python -m uvicorn …` | no `all` extra exists; bare `uvicorn` resolves globally, not from the venv |
| 2.3 | modes `live/scripted/auto` (undocumented) | `auto` (default) = live with scripted fallback, `live` = fail loudly, `scripted` = no network | three modes need three meanings |
| 2.4 | `goods/lambda-module-1.bin` | `lambda-module.zip` (+ `-junk` twin) | `deliverable_ref` is relative to `goods_dir`; the old name resolved to `goods/goods/…` |
| 5 | health-check ping keeps Render warm | external pinger on `/healthz` every 10 min | `healthCheckPath` is deploy-time only; free instances sleep after 15 min idle |
| 6 | sandbox POC at P3 (days 10–12) | **day 1**, before runner code | it is the only risk that can invalidate the pitch |


## 1. What exists today (do not rebuild)

| Piece | Where | Status |
|---|---|---|
| Protocol (Offer/Counter/Accept/Delivery/Attest/Contract) | `src/trustee/protocol.py` | done |
| Mandate engine (AP2-lite) | `src/trustee/mandate.py` | done |
| Deal state machine (guarded, idempotent) | `src/trustee/state.py` | done |
| PayPal adapter (authorize/capture/void, dry-run mode, retries) | `src/trustee/paypal.py` | done |
| LLM client (OpenAI-compatible; NIM default) | `src/trustee/llm.py` | done |
| Ledger (in-memory + JSONL) | `src/trustee/ledger.py` | done |
| Trustee agent (mandate gate → escrow → settle/unwind) | `agents/trustee.py` | done |
| Buyer agent (constrained negotiation + machine-verified attestation) | `agents/buyer.py` | done |
| Seller agent (floor-clamped counters + honest/cheat delivery) | `agents/seller.py` | done |
| Arbiter agent (policy-anchored rulings) | `agents/arbiter.py` | done |
| 24 tests, mypy + ruff clean | `tests/` | green |

The platform adds: **orchestration (DealRunner), service (FastAPI + SSE), UI
(AG Grid), fixtures, hosting**. No agent logic changes — the runner composes.

## 2. Architecture

```
                          ┌────────────────────────────────────────────┐
  Browser (arena UI)     │                arena service               │
  ┌──────────────┐       │  FastAPI (uvicorn)                        │
  │ transcript ◄─┼─SSE──┤  /api/runs POST {scenario, mode, live?}    │
  │ AG Grid   ◄──┼─poll/ │  /api/runs/{id}/events   (SSE)            │
  │ controls  ───┼─────► │  /api/runs/{id}          (state snapshot) │
  │ (honest/cheat)       │  /api/ledger?kind=…      (tape data)      │
  └──────────────┘       │  /webhooks/paypal (stretch, P3)           │
                          └──────────────┬─────────────────────────────┘
                                         │ in-process
                          ┌──────────────▼─────────────────────────────┐
                          │  DealRunner (one scenario = one run)       │
                          │  1. mandates + store + LLM wiring          │
                          │  2. buyer.open_deal ⇄ seller.respond_to    │
                          │     (alternating; capped at N turns)       │
                          │  3. Contract.from_accept                   │
                          │  4. Trustee.on_contract  (mandate+escrow)  │
                          │  5. seller.deliver(cheat=scenario)         │
                          │  6. buyer.verify_delivery → Attest         │
                          │  7. Trustee.on_attest (arbiter inside)     │
                          │  8. final state → SETTLED | VOIDED | …     │
                          └──────┬───────────┬───────────┬─────────────┘
                                 │            │           │
                          Trustee agent  Buyer/Seller  Arbiter
                          (mandate gate,  agents,      agent
                           PayPal adapter, (live or     (judge slot)
                           state machine)  scripted)
```

### 2.1 DealRunner (`src/trustee/arena/runner.py`)

- **One run = one deal.** No concurrency in v1 (scope cut), but the runner is
  pure composition: `run(scenario, mode, on_event)` where `on_event(ev)` is
  a callback the API layer bridges to SSE. Everything the agents emit (mandate
  checks, paypal ops, rulings, attestation) is already a Ledger event — the
  runner just subscribes the ledger + a run-scoped transcript.
- **Negotiation loop**: buyer `open_deal` → seller `respond_to` → alternate
  on counters, max **5 moves** (the cap fallback already exists in the agents).
  Stop on Accept. Deterministic seed: the runner picks the scenario's opening
  ask so live runs still land in realistic price ranges.
- **Three modes** (the demo-reliability decision — see §5):
  - `scripted` — a pre-recorded transcript (JSON fixture of LLM moves)
    replayed through the *same* agent code with a `ScriptedLLM` that serves
    the recorded moves. Full money path (mandates, escrow, delivery,
    attestation, arbitration) still runs for real; only the chat is canned.
    Instant, dependency-free, 100% reliable — the judge's safety net.
    Deterministic policy decisions (the seller accepting at or above floor)
    are *not* in the transcript, because they never touch the model.
  - `live` — real LLM calls (NIM by default). Fails loudly; this is the mode
    you run the reliability drill in.
  - `auto` (default) — live, falling back to scripted when the model fails.
    Fallback is **run-level**, not mid-run: a fresh deal id and a ledger note
    saying `live failed`. Mid-run hand-off means repositioning a scripted
    queue at the current turn — a day of work to save a 400ms re-run. The UI
    badge shows `LIVE`, `SCRIPTED`, or `LIVE FAILED → SCRIPTED`.
- **Scenario fixtures** drive it: `honest` (delivery clean) and `cheat`
  (seller `deliver(cheat=True)`). No code difference between them.

### 2.2 Arena service (`src/trustee/arena/api.py`)

FastAPI app, single process, no DB (ledger JSONL + in-memory run registry;
good enough for a demo, trivially stateless-per-run):

| Endpoint | Purpose |
|---|---|
| `POST /api/runs` `{scenario, mode}` → `{run_id}` | start a run (async task) |
| `GET /api/runs/{id}` | state snapshot: deal state, contract, escrow, ruling |
| `GET /api/runs/{id}/events` | SSE: transcript turns + ledger events as they happen |
| `GET /api/ledger` | flat JSON for AG Grid (all runs, tail N=500) |
| `POST /webhooks/paypal` | stretch (P3): forward sandbox webhook events into the ledger |
| `GET /` | serve the built UI (static mount) |

- SSE format: one `event: msg` per ledger/transcript event, JSON payloads;
  `event: done` at terminal state with the final snapshot.
- **Threading**: the agents are synchronous (blocking httpx, blocking OpenAI
  client, `time.sleep` in the PayPal retry loop), so a run executes via
  `asyncio.to_thread` and every mutation of the run record is marshalled back
  with `loop.call_soon_threadsafe`. Subscribers are replay-first: a new SSE
  connection is pre-filled with the run's existing rows, then fed live — so a
  mid-run refresh shows the same tape, in the same order.
- Error policy: run failures emit `event: error` + ledger note; the UI shows a
  "resumable" state, never a dead page. No request may take a run down.

### 2.3 Arena UI (`arena/ui/` — Vite + React + AG Grid Community)

One page, three zones (the judge's 60-second comprehension test):

```
┌────────────────────────────────────────────────────────────────────┐
│ TRUSTEE · agent-to-agent commerce trust layer        [LIVE mode]  │
├──────────────────────────────┬─────────────────────────────────────┤
│ NEGOTIATION                  │ LEDGER (AG Grid)                   │
│ buyer:  "I offer 120 …"      │ ts   actor    kind   state   note  │
│ seller: "counter 150 …"      │ 10:02 buyer    msg    NEGOTIATING  │
│ buyer:  "accept 132 …"       │ 10:02 trustee  mandate CONTRACTED  │
│ [deal state: ESCROWED]       │ 10:02 trustee  paypal  ESCROWED    │
├────────────────────────────────────────────────────────────────────┤
│ ▶ Run honest deal    ▶ Run cheat deal    mode: live ▾   [reset]   │
└────────────────────────────────────────────────────────────────────┘
```

- **Left: transcript** — negotiation turns (who, price, one-line intent),
  then settlement events (escrow open, delivery, attestation, ruling,
  capture/void). Color-coded by actor; the *last* event highlighted.
- **Right: AG Grid** — the money tape: one row per ledger event, columns
  `ts · deal · actor · kind · state_from→to · price · paypal_op · order_id ·
  note`. Row color by kind: mandate=gray, paypal=gold, ruling=red/green,
  state=blue. Group toggle by deal. This is also the "Best Use of AG Grid"
  artifact.
- **Bottom: controls** — the two one-click scenarios, mode selector
  (live/scripted/auto), reset.
- Build: `vite build` → `dist/`, served by FastAPI at `/`. No auth, no
  account — the judge's path is URL → click → watch.

### 2.4 Fixtures (`src/trustee/arena/fixtures/`)

- `goods/lambda-module-1.bin` (+ manifest hash) and
  `lambda-module-1-junk.bin` — the demo deliverable and its corrupt twin.
- `mandates.json` — demo grants (from `demo_mandates()`), buyer/seller
  sandbox email pairs for PayPal (`buyer-sandbox@trustee.dev`, etc.).
- `transcript-honest.json`, `transcript-cheat.json` — recorded negotiation
  moves for scripted mode (generated once from a live run, committed).
- `run_honest.py` / `run_cheat.py` — CLI entry points that print the ledger
  to stdout: **the judge's no-UI fallback**, and our own reliability check.

## 3. Money-path details that must hold

- Mandate check **before** any PayPal call (already enforced; runner must not
  bypass it — no flag to "skip mandate" exists in the API).
- `authorize` = funds held; only `capture` (accept attestation) or `void`
  (reject/timeout/arbiter release) moves money. Terminal states immutable;
  SSE replay safe.
- For an AUTHORIZE-intent order the money lives on the **authorization**:
  capture is `POST /v2/payments/authorizations/{id}/capture`, void is
  `…/{id}/void`. `/v2/checkout/orders/{id}/capture` is CAPTURE-intent only and
  returns 422 here.
- An AUTHORIZE order must be **payer-approved** before `/authorize` succeeds
  (`422 ORDER_NOT_APPROVED`). The adapter therefore supports a two-phase flow
  (`approve_url` → `authorize_after_approval`) and the Trustee refuses to enter
  `ESCROWED` unless the authorization is actually held.
- Real sandbox creds live in Render env vars; **absence of creds
  automatically switches the adapter to dry-run** (already built) — so the
  hosted demo *cannot* 500 for lack of config.

## 4. Hosting (Render)

- Web service: two-stage `Dockerfile` (node builds the UI, python runs it),
  start command `uv run --no-sync python -m uvicorn trustee.arena.api:app`.
  There is no `all` extra; and bare `uvicorn` resolves the global install, not
  the project venv — always `python -m uvicorn`.
- Env: `LLM_BASE_URL/LLM_API_KEY/LLM_MODEL` (NIM), `PAYPAL_CLIENT_ID/SECRET`,
  `PAYPAL_ENV=sandbox`.
- Free-tier reality: instance sleeps → first request slow. Mitigations:
  (a) render.yaml health-check ping every 30 min; (b) UI shows a "warming
  up" state instead of a spinner-loop; (c) the CLI fixtures mean the demo
  works from a laptop even if Render is down.
- Webhooks (stretch): Render URL + PayPal webhook subscription for
  `PAYMENT.CAPTURE.COMPLETED`; sandbox IP-allowlist note in README.

## 5. Reliability doctrine (from the earlier bar-setting)

| Risk | Counter |
|---|---|
| LLM nondeterminism stalls a live demo | move cap (5), JSON-only coercion, `scripted` mode, auto-fallback, `run_*.py` CLI fallback |
| NIM rate limits during judging | scripted default in hosted UI? No — **live default, scripted on failure** (badge shown); documented |
| Sandbox flakiness | day-1 POC with real creds (P3, before UI polish is locked), retries already in adapter, dry-run fallback |
| Render cold start | **external** pinger on `/healthz` every 10 min (`healthCheckPath` is deploy-time only, it does not keep a free instance awake) + warming state + laptop fallback |
| Demo scope creep | v1 = one deal, two scenarios; arbiter already in; webhooks = stretch; no multi-deal, no Payouts |

## 6. Milestones & dates

> Today 10/28 → deadline 11/12 (EST). Each milestone has a **definition of
> done** — don't start the next one until the prior's DoD passes.

### P1 — Runner + service (10/29–11/1, 4 days) — **done**
- [x] `arena/runner.py`: `run(scenario, mode, on_event)`; tests:
      honest run ends SETTLED, cheat run ends VOIDED, both emit full event
      streams; scripted replay parity test (event sequence identical, minus
      timestamps/ids).
- [x] `ScriptedLLM` + `transcripts/*.json` — hand-authored, because a
      transcript should record *LLM moves only*: the seller accepting at or
      above floor is policy, not chat, and recording it would fake evidence.
- [x] `arena/api.py`: `/api/runs` POST + SSE + `/api/ledger`; httpx-ASGI
      tests: run to completion over the API, SSE stream ends with `done`,
      and a finished run replays identically on reconnect.
- **DoD:** met. `run_honest` exits `SETTLED`, `run_cheat` exits `VOIDED`, both
      without an LLM key. 51 tests green.

### P2 — UI + AG Grid (11/2–11/5, 4 days) — **done**
- [x] Vite + React scaffold in `arena/ui/`, `ag-grid-community@32.3.9` pinned,
      dark via the Theming API (v32 ships no `ag-theme-quartz-dark.css`).
- [x] Transcript panel (SSE-fed, follows the tape, last-event highlight).
- [x] Ledger grid: 8 columns + kind colour coding + quick filter; SSE-fed with
      `getRowId` so rows update in place.
- [x] Controls: run honest / run cheat / mode selector; mode badge including
      `live failed → scripted`.
- [x] `vite build` → served by FastAPI at `/`; the page auto-runs the honest
      deal on load, so the URL alone demonstrates the product.
- [x] Added: a 7-stage trust strip above the panels (negotiate → contract →
      mandate → escrow → delivery → attest → settle). §2.3's layout had no
      progress affordance, which is the thing the 60-second test actually needs.
- **DoD:** met by inspection at 1440×900. A stranger sees the money path
      complete, then clicks the red button and watches it unwind.

### P3 — Real sandbox + hosting (11/6–11/8, 3 days)
- [ ] Sandbox POC with real creds: `authorize → capture` and
      `→ void` observed in the PayPal dashboard (the day-1 risk, finally
      fired against the real API).
- [ ] Render deploy (`render.yaml`), env wired, public URL live;
      dry-run-when-no-creds confirmed on a second clean service.
- [ ] Stretch (only if P3 core green by 11/7 EOD): webhook endpoint +
      `PAYMENT.CAPTURE.COMPLETED` ledgering.
- [ ] `render.yaml` health pings; warming-up UI state.
- **DoD:** demo URL works from a clean browser/incognito, honest run
      settles against **real sandbox** escrow; ledger shows real order ids.

### P4 — Hardening + submission (11/9–11/11, 3 days)
- [ ] **10/10 reliability drill**: 10 consecutive hosted runs per scenario
      (5 live, 5 scripted). Any failure → fix + re-drill.
- [ ] README: <10-min judge setup (clone → `uv sync` → `uv run pytest` →
      `run_honest.py`), hosted URL, architecture diagram, sandbox creds
      note, license visible.
- [ ] Tools writeup: NIM model + how used; PayPal APIs used (Orders
      create/authorize/capture/void, webhooks if wired, mandates design
      referencing AP2 as prior art); AG Grid; Render.
- [ ] Demo video ≤3 min: problem (30s) → honest run → cheat run →
      "why it matters" (20s). Real runs, no fake screens.
- [ ] Submit to Devpost **11/11**, 12h buffer before the 11/12 15:00 EST
      deadline (repo public, license in About section, video uploaded).
- **DoD:** submission complete, all four rubric boxes ticked on the form.

## 7. Open decisions (answer before P1 starts)

1. **LLM for live mode** — NIM (free, OpenAI-compatible, your SignalFoundry
   provider) vs. any key you already have. Default: NIM
   `meta/llama-3.1-70b-instruct`; the `ScriptedLLM` fallback makes the choice
   low-risk either way.
2. **Demo good** — "Lambdas math module, tested" (code category, trivial to
   fake, checker = file contains expected marker). Sticking with it unless
   you'd rather sell something more visual (a generated design asset is
   prettier in the UI but same engineering).
3. **AG Grid theming** — Community edition, dark theme to match the page.
   No license needed. Fine to just build?

## 8. Non-goals (v1, explicit)

- Multi-deal concurrency, Payouts/fee splits, Elastic, Zapier, Subscriptions,
  Invoicing API (Orders-only money path), multi-user, auth, real-money mode,
  mobile layout. Anything here shows up in the pitch as "next", not in the demo.
