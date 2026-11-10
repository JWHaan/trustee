# Contributing

Thanks for your interest in Trustee. Issues and pull requests are welcome. For a change to the money path (anything
that can hold, capture, void or refund), please open an issue first so the approach can be agreed before you write it.

## Set up

You need Python 3.12+ with [uv](https://docs.astral.sh/uv/), and Node.js 22 for the arena UI.

```bash
uv sync --extra dev
(cd arena/ui && npm ci)
```

No credentials are needed: the tests mock PayPal and the model.

## Checks

CI runs these on every push and pull request. Run them before you open one:

```bash
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run mypy
(cd arena/ui && npx tsc --noEmit -p . && npm run build)
```

## Generated files

Some committed files are generated. Regenerate them instead of editing them by hand:

| File | Regenerate with | Kept honest by |
|---|---|---|
| The attack table in `README.md`, between its `attack-table` markers | `uv run python scripts/attack_report.py --readme` | nothing yet: CI runs the checks in `tests/test_attacks.py`, but no test compares the table with them, so regenerate it whenever you change that file |
| `openapi/trustee-v1.json` | `uv run python scripts/export_openapi.py` | `tests/test_openapi.py` fails when it is stale |
| `postman/Trustee.postman_collection.json` and `postman/Trustee.postman_environment.json` | `uv run python scripts/make_postman.py` | `tests/test_openapi.py` fails when they are stale |

## Ground rules

- Only the deterministic policy layer may call PayPal. A model may propose and explain; it never decides where money
  goes.
- Every PayPal call must be safe to repeat, and a deal in a terminal state never changes again.
- A new way to cheat the deal belongs in `CASES` in `tests/test_attacks.py`. Then run
  `uv run python scripts/attack_report.py --readme` so it shows up in the README's table; nothing fails if you forget.
- Never commit secrets. Keys, tokens and passwords go in `.env` (gitignored) or the host's secret store.

## Code layout

```
src/trustee/
  protocol.py        offer/contract JSON protocol (the only thing settlement sees)
  identity.py        Ed25519 agent and principal keys, the registry, signature checks
  mandate.py         AP2-lite mandate engine (+ optional reputation rules)
  intent.py          intent mandates (human-not-present): signed instruction + limits, the rule engine, drafting
  funding.py         vaulted PayPal funding sources behind opaque funding_refs
  receipts.py        signed settlement receipts and the Trustee signing key
  summary.py         the plain-language verdict every deal reports (status, headline, reasons, next step, money)
  verification.py    Trustee checks the delivered file itself: content-addressed store, contract checks, counts
  criteria.py        AI-drafted acceptance criteria: the model proposes, code keeps only what the request says
  evidence.py        signed dispute evidence packs, and verify_evidence_pack to check one offline
  verify.py          `python -m trustee.verify`: check a receipt offline
  sandbox_approver.py  approve a PayPal sandbox order in a browser (local Chromium or remote over CDP)
  state.py           deal state machine (guarded, idempotent, transition hook)
  paypal.py          PayPal adapter (authorize/capture/void/reauthorize, approval flow, vault, webhooks, dry-run)
  partial.py         pro-rata settlement math (pure Decimal, no I/O, no LLM)
  sweeper.py         deadline sweeper: auto-void lapsed deals, renew aging authorizations
  ledger.py          append-only, hash-chained event log with subscribers
  store.py           durable SQLite store: principals, agents, mandates, deals, events, webhooks
  reputation.py      agent reputation across the deals it was a party to
  alerts.py          human alerts (Zapier Catch Hook) when money needs a decision
  channel3.py        Channel3 client; pricefeed.py checks a delivered price feed against it
  analyst.py         ask the Elastic Agent Builder ledger analyst over A2A
  llm.py             OpenAI-compatible client (NIM/OpenAI/Ollama)
  agents/            trustee, buyer, seller, negotiator, arbiter
  platform/          public /v1 API, reputation routes, A2A Agent Card, DealService (durable multi-deal host)
  a2a/               A2A JSON-RPC server + client, skills, deal tasks, push notifications
  network/           Northwind and Tidewater as A2A agents (cards, skills, signed negotiation, delivery,
                     Zapier MCP back office)
  elastic/           optional ledger shipping to Elasticsearch, ES|QL reputation
  client.py          TrusteeClient SDK
  arena/
    runner.py        DealRunner — composes one deal, emits the tape
    api.py           FastAPI + SSE
    scenario.py      scenarios are data, not code
    outside.py       the `outside` scenario (agents as HTTP clients of /v1)
    a2a_flow.py      the `a2a` scenario (three A2A agents)
    approval.py      waiting for a payer to approve at PayPal
    scripted.py      ScriptedLLM (recorded moves, no network)
    rows.py          ledger Event -> grid row
    fixtures/        goods store, manifest, scenarios, transcripts, Channel3 recordings, CLI runners
arena/ui/            Vite + React + AG Grid Community
openapi/ postman/    API description and a runnable collection (see docs/SDKS.md)
examples/            a2a_deal.py, a2a_network.py, sandbox_poc.py
tests/               agents, runner, API, SSE, PayPal request shapes, money path, identity,
                     mandates, intents, store, platform API, approval, webhooks, A2A, partial capture,
                     Elastic, Channel3, Zapier MCP, durability, arena scenarios
```
