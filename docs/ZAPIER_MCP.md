# Zapier MCP back office: Northwind and Tidewater book deals in their own apps

After Trustee settles or voids a deal, each business's agent records the result in the apps that business already
uses. The agents call Zapier MCP themselves. Trustee never sees those app credentials, and no model chooses a tool
or an argument.

| Agent | When | Actions |
|---|---|---|
| Tidewater (seller) | Settled | `record_invoice` (sales invoice), `update_crm` (deal won) |
| Tidewater (seller) | Voided | `update_crm` (deal lost); no invoice |
| Northwind (buyer) | Settled or voided | `record_bill` (bill or expense), `post_finance_note` (note to the finance channel) |
| Northwind (buyer) | Intent purchase steps up to the human | `notify_principal` (message with the approval link) |

`notify_principal` only informs. The principal still approves through Trustee's own signed flow.

This is separate from [Zapier alerts](ZAPIER.md). Those are webhooks Trustee posts to. These are tool calls the
agents make.

## Safety rules

- **Opt-in.** With no `<AGENT>_ZAPIER_MCP_TOKEN` nothing runs. Without the `zapier` extra the feature stays off and
  logs one line.
- **Allowlist.** An agent calls only the actions mapped in `<AGENT>_ZAPIER_TOOLS`. Tidewater cannot book Northwind's
  actions, and the reverse.
- **No money tools.** A tool whose name contains a money word (pay, payment, payout, refund, transfer, charge,
  capture, withdraw, paypal) is refused even when mapped.
- **Code builds the arguments** from the signed settlement receipt: deal id, counterparty name, the amount that
  actually moved (the captured amount for a partial settlement), currency, and the PayPal capture id. Arguments
  carry no contract text, delivery bytes, notes or personal data.
- **Once per deal.** Each (agent, deal, action) books once, however many pushes arrive.
- **Failures stay contained.** Calls run on a background thread with a bounded queue, a 30 s timeout per call and
  two retries. A failure is logged and shown as a failed hop on the arena tape. It never changes the deal.

## 1. Create two managed-mode servers

Do this once for each agent, so each business has its own apps and credentials.

1. Open [mcp.zapier.com](https://mcp.zapier.com) and click **Add MCP server**. Name one `Tidewater back office`
   and the other `Northwind back office`.
2. Choose the apps each one uses, for example an accounting app for invoices and bills, a CRM for deals, and a chat
   or email app for notes and the principal message.
3. In **managed** mode, add only the actions the agent may call. Leave everything else off.
4. Note each action's tool name. Use `scripts/zapier_tools.py` (step 3) to read the exact names.

## 2. Generate a connection token per server

In each server, open the **Settings** tab, click **Generate token** under **API token**, and copy it. Zapier shows
it once. Treat it as a password: anyone holding it can run that server's tools.

## 3. Map the actions to tool names

List the tools on each connection. The script prints names only, never the token or tool output:

```bash
TIDEWATER_ZAPIER_MCP_TOKEN=... uv run python scripts/zapier_tools.py --agent tidewater
NORTHWIND_ZAPIER_MCP_TOKEN=... uv run python scripts/zapier_tools.py --agent northwind
```

Then map each action you want to a tool name, and preview the calls for a sample deal with no network:

```bash
TIDEWATER_ZAPIER_TOOLS='{"record_invoice": "quickbooks_create_invoice", "update_crm": "hubspot_update_deal"}' \
  uv run python scripts/zapier_tools.py --agent tidewater --dry-run
```

Map only what you want booked. An unmapped action is skipped.

## 4. Set the environment

| Variable | Agent | Purpose |
|---|---|---|
| `TIDEWATER_ZAPIER_MCP_TOKEN` | Tidewater | Tidewater's connection token |
| `TIDEWATER_ZAPIER_TOOLS` | Tidewater | JSON map of `record_invoice`, `update_crm` to tool names |
| `NORTHWIND_ZAPIER_MCP_TOKEN` | Northwind | Northwind's connection token |
| `NORTHWIND_ZAPIER_TOOLS` | Northwind | JSON map of `record_bill`, `post_finance_note`, `notify_principal` to tool names |
| `ZAPIER_MCP_URL` | both | Optional. Default `https://mcp.zapier.com/api/v1/connect`. Must be https. |

Set these as secrets on the platform that runs each agent:

- **Render or another host for Trustee's own process:** these variables are not needed there. Trustee never calls
  Zapier MCP.
- **Northwind or Tidewater running standalone** (`python -m trustee.network.northwind`, `... tidewater`): set the
  variables for that agent in its environment.
- **Local arena:** export them in the shell that starts the arena. Each agent reads its own variables when it is
  built. Without them the arena behaves as before.

The `zapier` extra must be installed where the agent runs:

```bash
uv sync --extra zapier
```

Dependency: the `mcp` package, pinned to `>=1.8,<2` (the 1.x Streamable HTTP client).

## Cost per deal

Each successful Zapier MCP call costs two Zapier tasks. A retry after a timeout can book the same record twice, so
the deal id appears in every record text, and you can dedupe in Zapier.

| Deal | Calls | Tasks |
|---|---|---|
| Tidewater, settled | 2 | 4 |
| Tidewater, voided | 1 | 2 |
| Northwind, settled or voided | 2 | 4 |
| Northwind, step-up notice | 1 | 2 |

## Rotation

Click **Rotate token** on the server's Settings tab. The old token stops working straight away. Update the variable
on each host the same moment, or bookings will fail until you do. Failures are logged as `zapier ... failed` with the
exception type only, so a rotation shows up in the logs as a run of failures.

## What the arena shows

Each booking attempt appears as a hop from the agent to `zapier` on the arena tape, with the action name and
`booked` or `failed (<error type>)`. Arguments are not shown.

## Known limits

- A deal whose receipt is never pushed or fetched is not booked. Tidewater books from its push and, if the push lacks
  the receipt, by fetching the task from Trustee. Northwind books when its purchase returns the receipt.
- Trustee's receipt signature is not verified by the agents. They trust Trustee's own A2A reply, as they do for every
  other Trustee message.
- Zapier MCP tool names and argument schemas differ per app. The argument names this code sends are fixed. Add a
  Zap step that maps them if a tool needs other names.
