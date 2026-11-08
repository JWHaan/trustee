# Sponsor tools: what we keep and why

The PayPal AI Hackathon lists ten optional sponsor and partner tools. Trustee keeps the ones that make the money path
safer, more accountable or provably working against PayPal, and has dropped the rest.

Prize amounts and stacking rules come from third-party listings, which disagree in places. Check the official Devpost
rules before planning around them.

## Focus

| Tool | What it does for Trustee | Where |
|---|---|---|
| **Elastic** | Every ledger row ships to Elasticsearch. The ledger analyst answers "what needs a person?" (`trustee-money-incidents`) and "which buyers keep claiming mismatches the signed record contradicts?" (`trustee-contradicted-claims`), next to reputation, spend and void reasons. Kibana rules can page on the same query. | `elastic/`, `scripts/elastic_setup.py`, [ELASTIC.md](./ELASTIC.md) |
| **KERNEL** | Remote browsers for agents. `trustee.sandbox_approver` approves a PayPal sandbox order in a browser, local Chromium or any remote browser over CDP such as a KERNEL one, so a real escrowed payment can run with no person. That closes the biggest gap: nothing had been run against the real PayPal sandbox. | `sandbox_approver.py`, `examples/sandbox_poc.py --auto-approve`, `.github/workflows/sandbox-e2e.yml` |
| **Render** | Hosts the arena. Its paid services map onto the target design: web service (API), persistent disk or Postgres (deal store), background worker (money workers), cron (sweeper, reconciler). See [Render: making the demo durable](#render-making-the-demo-durable). | `render.yaml`, [DEPLOY.md](./DEPLOY.md) |

## Kept, supporting

| Tool | Why it stays |
|---|---|
| **AG Grid** | The ledger tape is the product's main view. Next: an operator grid of deals that need a person. |
| **Zapier** | Human alerts (`dispute`, `paypal_alert`, `storage_unavailable`) reach a person. The Zapier MCP back office (agents booking invoices and bills) is optional and off by default. |
| **Channel3** | The market check on price-feed deliveries is the first example of Trustee-side verification ([PAYPAL_SCALE.md](./PAYPAL_SCALE.md) §4.1). |
| **Postman** | The collection runs the deal end to end; it is the starting point for the load test. |

## Dropped

| Tool | Why |
|---|---|
| **Astropods** | Hosted Northwind and Tidewater containers, a remote arena mode and an AI Gateway fallback: extra surface that did not touch the money path, its scale or its accountability. The agents still run in the arena and standalone. |
| **APIMatic** | Generated SDKs could not sign anything and needed a paid key to build. The OpenAPI file, the Postman collection and the Python `TrusteeClient` cover the same ground. |
| **Bryntum** | Never added. A hold timeline would be an operator convenience, not a scaling tool. |

## KERNEL: how to use a remote browser

`approve(..., cdp_url=...)` and `PAYPAL_E2E_CDP_URL` take the Chrome DevTools Protocol WebSocket URL of a running
browser. Start a browser with KERNEL (its SDK or CLI returns that URL for each browser) and pass the URL; the
approver connects, uses the browser's first context, and leaves the browser running for whoever started it. The KERNEL
API calls themselves are not in this repository: its documentation could not be reached from the environment this was
built in, so the integration point is the standard CDP URL rather than guessed SDK calls. The remote path is tested
against a locally started Chromium over CDP.

## Render: making the demo durable

The deployed arena runs on the free plan, which has no disk, so `TRUSTEE_DB_PATH` is unset and the store is in memory.
The write-before-money and crash-resume protections therefore do nothing on the deployed demo: a restart forgets every
deal. Making it durable needs a paid instance with a persistent disk, which costs money, so `render.yaml` still uses
the free plan. The change is:

```yaml
    plan: starter
    disk:
      name: trustee-data
      mountPath: /var/data
      sizeGB: 1
    envVars:
      - key: TRUSTEE_DB_PATH
        value: /var/data/trustee.db
```

Render's hosting-credit prize would cover it.
