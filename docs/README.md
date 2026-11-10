# Documentation

Start with the [README](../README.md): what Trustee is, how to run it, and what it stops.

## Using Trustee

| Page | What it covers |
|---|---|
| [ARENA.md](ARENA.md) | The demo arena: every scenario, the page, modes, approving in the PayPal sandbox, a three-minute demo script and the arena's own API |
| [API.md](API.md) | The platform API: endpoints, signing, errors, auth, durability, how Trustee checks deliveries, evidence packs, the real PayPal sandbox flow |
| [A2A.md](A2A.md) | Trustee over the Agent2Agent protocol (spec 1.0.0, JSON-RPC binding), and Northwind and Tidewater as A2A agents |
| [AUTONOMY.md](AUTONOMY.md) | Human-not-present payments: one PayPal consent, a signed intent mandate, signed receipts |
| [SDKS.md](SDKS.md) | The Python client, retry rules, the OpenAPI description and the Postman collection |
| [CONFIGURATION.md](CONFIGURATION.md) | Environment variables |

## Features

| Page | What it covers |
|---|---|
| [PARTIAL_CAPTURE.md](PARTIAL_CAPTURE.md) | Pro-rata settlement of a partly valid delivery, and renewing held authorizations |
| [CHANNEL3.md](CHANNEL3.md) | Checking a delivered price feed against live market data |
| [ELASTIC.md](ELASTIC.md) | Optional: the ledger in Elasticsearch, agent reputation and a ledger analyst |
| [ZAPIER.md](ZAPIER.md) | Optional: alerts when an agent's money needs a person |
| [ZAPIER_MCP.md](ZAPIER_MCP.md) | Optional: Northwind and Tidewater book settled deals in their own apps |
| [INTEGRATIONS.md](INTEGRATIONS.md) | Which partner tools Trustee uses, and why |

## Product and scale

| Page | What it covers |
|---|---|
| [PAYPAL_SCALE.md](PAYPAL_SCALE.md) | The problem, what the demo proves, and what it would take to become a PayPal product |
| [SCALE_ARCHITECTURE.md](SCALE_ARCHITECTURE.md) | Running Trustee at PayPal volumes while keeping every payment verifiable |

## Operations

| Page | What it covers |
|---|---|
| [DEPLOY.md](DEPLOY.md) | One-time runbook for the hosted arena on Render with the PayPal sandbox |

## History

How the project was specified, planned and reviewed during the hackathon. Kept for reference; the code and the pages
above describe what Trustee does today.

| Page | What it covers |
|---|---|
| [history/TRUSTEE_SPEC.md](history/TRUSTEE_SPEC.md) | The original specification |
| [history/BUILD.md](history/BUILD.md) | The platform build plan |
| [history/BUILD-CRITIQUE.md](history/BUILD-CRITIQUE.md) | A review of that plan against an early build |
