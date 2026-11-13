# Trustee

[![CI](https://github.com/JWHaan/trustee/actions/workflows/ci.yml/badge.svg)](https://github.com/JWHaan/trustee/actions/workflows/ci.yml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-3776ab.svg)](pyproject.toml)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

**Escrow and arbitration for agent-to-agent commerce, on PayPal.** When one company's AI agent buys data or digital
work from another company's agent, Trustee holds the payment with PayPal, checks the delivered file itself, and pays
the seller only if the file matches the contract. Otherwise the buyer gets the money back.

**Try it:** the [live demo](https://trustee-arena.onrender.com) runs on the PayPal sandbox, so each payment there
waits for approval from a PayPal sandbox buyer account, and the free host can take a minute to wake up. The
[local dry run](#quickstart) needs no account and runs every deal to the end. Built for the PayPal AI Hackathon.

| Honest seller: paid | Cheating seller: refunded |
|---|---|
| ![The arena after an honest deal: the seller received $156.00 and every check passed](docs/arena-honest.png) | ![The arena after the seller shipped the wrong file: the buyer keeps $170.00](docs/arena-cheat.png) |

## Why Trustee

Two agents can negotiate anything and enforce nothing. Trustee is the trust layer between them. When a buyer agent
and a seller agent sign a machine contract, Trustee checks it against both parties' **mandates** (AP2-style signed
limits: budget caps, counterparty allowlists, expiry), holds the money in **PayPal escrow** (Orders authorize, then
capture or void), **checks the delivered file itself**, and settles or refunds on its own finding rather than on
either party's word. Every decision lands in a hash-chained **ledger**.

**Who it's for.** Any business whose agents buy data, API access or digital work from another company's agents. The
worked example in this repository: **Northwind Outfitters**, a mid-size online apparel retailer, has a
demand-planning agent that buys a Q3 returns dataset from **Tidewater Data Co.**, a retail-data vendor. Northwind's
finance ops lead grants the buying agent a $500 spend mandate: digital data only, this vendor only. Tidewater's sales
agent delivers the CSV. Trustee holds the payment until the file's hash and schema match the signed contract, then
settles it. If the file is wrong, the money goes back to Northwind.

## How it works

```
contract formed, signed by both agents
  -> mandate gate (no LLM: hard policy)        rejected? deal dies, money untouched
  -> PayPal Orders.create + authorize           funds HELD, not moved
  -> seller hands Trustee the file (signed upload), then records the delivery (signed sha256)
  -> Trustee checks those bytes itself: sha256, schema and row rules, market check
  -> buyer attests (advisory when Trustee holds the file; the fallback when it does not)
       checks pass -> capture (settled to seller)   buyer still rejects -> held for a person
       checks fail -> void (refund)   pro-rata term -> pay the valid share   deadline lapse -> auto-void
  -> signed receipt + signed dispute evidence pack
```

The model proposes and explains; only the policy layer can call PayPal. Terminal states are immutable and webhook
replays are idempotent no-ops, so funds can never settle twice or be stranded.

- **Mandate gate, no LLM:** signed spend and receive limits are checked before PayPal is called.
- **Trustee verifies the delivery:** the seller hands Trustee the file with a signed upload; Trustee runs the
  contract's hash, schema, row and market checks on those bytes, and the buyer gets the file once the payment settles.
- **Pro-rata settlement:** a partly valid delivery pays for its valid share, by Trustee's own count
  ([details](docs/PARTIAL_CAPTURE.md)).
- **Signed receipts and dispute evidence packs** that anyone can verify offline, plus a plain-language
  [`summary`](docs/API.md#what-a-deal-reports) on every deal.
- **AI-drafted acceptance criteria:** the model drafts checkable criteria from a buyer's request; code keeps only
  what the request says.
- **A2A platform:** independent agents discover Trustee by its Agent Card and run deals over signed REST or A2A
  JSON-RPC with push notifications ([details](docs/A2A.md)).
- **Autonomous payments:** a human consents once with an intent mandate; the agent then pays inside limits Trustee
  enforces, and a price over the signed cap steps up to a person ([details](docs/AUTONOMY.md)).
- **Market check for price feeds:** a delivered price feed can be checked against live Channel3 market data before
  the money is released ([details](docs/CHANNEL3.md)).

## What Trustee stops

Every row is an executable check in `tests/test_attacks.py`, run by CI. The table is generated from those same checks.

<!-- attack-table:start -->
| Attack | Trustee's defence | Result |
|---|---|---|
| Honest deal (control) | Pays the seller once | ✅ paid once |
| Someone signs the contract as the seller with an unregistered key | Refused before PayPal | ✅ refused (401), PayPal never called |
| The seller signs an "accept" as if it were the buyer | Refused; nothing captured | ✅ refused (401), nothing captured |
| The buyer's agent agrees a price above its spending limit | Refused before PayPal | ✅ refused (403), PayPal never called |
| The agent pays more than the human's signed instruction allows | Steps up to a person | ✅ stepped up to a person |
| The human revokes the agent's limit after funds are held | Held for a person, no capture | ✅ held for a person, nothing captured |
| Someone replays an older, looser signed limit | Refused | ✅ refused (409) |
| The seller ships a junk file | Refunded on Trustee's own check | ✅ refunded on Trustee's own check |
| The seller signs the contract's hash but hands over a different file | Refunded; the lie is caught | ✅ refunded, lie caught |
| The buyer uploads a junk file to frame a seller who delivered directly | Ignored: only the seller's signed upload counts | ✅ upload refused (401), seller not framed |
| The buyer gets a correct file and claims it failed | Held for a person; no free refund | ✅ held for a person, no free refund |
| The buyer under-reports valid rows to pay less | Pays by Trustee's own count | ✅ paid 90% (Trustee's count), not 52% |
| The seller sells a price feed with stale prices | Refunded on the market check | ✅ refunded on the market check |
| The buyer's accept arrives twice | Captured once | ✅ captured once |
| The same deal is opened twice | Held once | ✅ held once |
| The server dies right after PayPal holds the money | Finished on restart; one hold | ✅ finished on restart, one hold |
| The database fails just before a payment | No PayPal call | ✅ refused (503), PayPal never called |

17 of 17 defences held (2026-11-09, dry-run, `python scripts/attack_report.py`).
<!-- attack-table:end -->

## Quickstart

You need Python 3.12+ and [uv](https://docs.astral.sh/uv/); the arena UI also needs Node.js. No credentials are
needed: without PayPal keys Trustee runs a dry run with local order ids, and without a model key the agents replay
recorded moves.

```bash
uv sync --extra dev
uv run pytest                       # the full suite, dry-run: no credentials needed

# one deal with no UI: prints the full ledger and exits non-zero if the deal
# doesn't reach the expected terminal state. Other scenarios for run_honest:
# partial, overreach, pricefeed, pricefeed-stale, pricefeed-mixed
uv run python -m trustee.arena.fixtures.run_honest
uv run python -m trustee.arena.fixtures.run_cheat
uv run python -m trustee.arena.fixtures.run_honest partial

# the arena
cd arena/ui && npm install && npm run build && cd ../..
uv run python -m uvicorn trustee.arena.api:app --port 8000
open http://127.0.0.1:8000
```

The page runs the honest deal on load, then gives you one button per scenario. Use `python -m uvicorn`, not bare
`uvicorn`: the bare name resolves a global install and will not see this project's package.

More to run: [`examples/a2a_deal.py`](examples/a2a_deal.py) (two agents, a full escrowed deal over HTTP),
[`examples/a2a_network.py`](examples/a2a_network.py) (three A2A agents on their own ports) and, with PayPal sandbox
credentials, [a real escrowed payment](docs/API.md#real-paypal-sandbox).

## Documentation

| Guide | What it covers |
|---|---|
| [The arena](docs/ARENA.md) | Every scenario, the page, modes, approving in the PayPal sandbox, a three-minute demo script |
| [Platform API](docs/API.md) | Endpoints, signing, errors, durability, delivery checks, evidence packs, the PayPal sandbox flow |
| [A2A](docs/A2A.md) | Trustee over the Agent2Agent protocol, and Northwind and Tidewater as A2A agents |
| [Autonomous payments](docs/AUTONOMY.md) | Consent once; the agent then pays inside a signed intent mandate |
| [Client libraries](docs/SDKS.md) | The Python client, OpenAPI and the Postman collection |
| [Configuration](docs/CONFIGURATION.md) | Environment variables |
| [Trustee at PayPal scale](docs/PAYPAL_SCALE.md) | Why this matters for PayPal, and what it would take to become a product |
| [Speed, scale and accountability](docs/SCALE_ARCHITECTURE.md) | Running at high volume while keeping every payment verifiable |

The [docs index](docs/README.md) also covers partial capture, the Channel3 market check, the optional Elastic and
Zapier integrations, deployment, and [the integrations Trustee uses, and why](docs/INTEGRATIONS.md).

## Status

The money path, the platform API, A2A, autonomous payments, partial capture and the optional integrations are built
and covered by the test suite, which mocks PayPal's HTTP API. The real-sandbox path is verified end to end: the weekly
`sandbox-e2e` workflow runs `examples/sandbox_poc.py` with a browser approving as the sandbox buyer (latest green run:
authorization `7J3993247F013415U`, capture `9HY209711U9483505`), and hosted arena runs at
https://trustee-arena.onrender.com settle real sandbox orders with no human in the loop via the vaulted mandate
(e.g. run `run_c5d3882f4a8e`: order `3JT36509FL734773D`, $143.00 captured).

Trustee is a hackathon project, not production software. [SECURITY.md](SECURITY.md) explains how to report a problem,
and [CONTRIBUTING.md](CONTRIBUTING.md) how to work on the code.

## License

Apache-2.0. See [LICENSE](LICENSE).
