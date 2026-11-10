# Configuration

Trustee reads its settings from environment variables only; it does not load a `.env` file by itself.
[`.env.example`](../.env.example) lists them with comments: copy it to `.env`, fill it in, and export it into the
shell that starts the server (`set -a; source .env; set +a`), as [DEPLOY.md](DEPLOY.md) does. Every value is
optional: empty means a dry run, with no network calls, local PayPal order ids and recorded agent replies.

| Var | Default | Meaning |
|---|---|---|
| `PAYPAL_CLIENT_ID` / `PAYPAL_CLIENT_SECRET` | empty | empty ⇒ dry-run with local order ids |
| `PAYPAL_ENV` | `sandbox` | `sandbox` \| `live` |
| `PAYPAL_RETURN_URL` / `PAYPAL_CANCEL_URL` | `{TRUSTEE_PUBLIC_URL}/v1/paypal/return` / `/cancel` | used by the payer-approval redirect |
| `LLM_BASE_URL` | NIM | any OpenAI-compatible endpoint |
| `LLM_API_KEY` | empty | empty ⇒ scripted mode |
| `LLM_MODEL` | `nvidia/nemotron-3.5-lightning-30b-a3b` | |
| `LLM_EXTRA_BODY` / `LLM_TIMEOUT` | empty / `30` | JSON merged into each request (Nemotron needs thinking off); seconds before falling back to the recorded moves |
| `TRUSTEE_LEDGER_PATH` | in-memory | JSONL durability when set |
| `ARENA_APPROVAL_POLL_SECONDS` / `ARENA_APPROVAL_TIMEOUT_SECONDS` | `3` / `300` | how the arena waits for the buyer to approve in PayPal |
| `ARENA_SELF_URL` | `http://127.0.0.1:$PORT` | where the `outside` scenario reaches the arena's own `/v1` API |
| `ARENA_PARTIAL_SCENARIO` / `ARENA_PRICEFEED_SCENARIO` | unset | `1` lists the `partial` / three `pricefeed` scenarios in the UI |
| `ARENA_UI_DIR` | `arena/ui/dist` | directory that serves the built UI |
| `PAYPAL_SELLER_EMAIL` | placeholder | the order's payee; against the real sandbox it must be one of your sandbox business accounts |
| `TRUSTEE_PUBLIC_URL`, `TRUSTEE_DB_PATH`, `TRUSTEE_OPERATOR_TOKEN`, `TRUSTEE_SWEEP_SECONDS`, `PAYPAL_WEBHOOK_ID`, `TRUSTEE_WEBHOOK_ALLOW_UNVERIFIED` | | the platform API: see [API.md](API.md#platform-api) |
| `TRUSTEE_SIGNING_KEY`, `PAYPAL_DEMO_VAULT_ID` | | autonomous payments: see [API.md](API.md#autonomous-payments-consent-once-then-no-clicks) |
| `PAYPAL_SANDBOX_BUYER_EMAIL` / `PAYPAL_SANDBOX_BUYER_PASSWORD`, `PAYPAL_E2E_CDP_URL`, `PAYPAL_E2E_SELECTORS`, `PAYPAL_E2E_CHROMIUM` | | the browser that approves sandbox orders for `sandbox_poc.py --auto-approve`: see [API.md](API.md#real-paypal-sandbox) |

Each optional integration documents its settings on its own page: [Elastic](ELASTIC.md), [Zapier alerts](ZAPIER.md),
[Zapier MCP](ZAPIER_MCP.md) and [Channel3](CHANNEL3.md).
