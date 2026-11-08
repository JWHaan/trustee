# Sponsor tools and how they help

The PayPal AI Hackathon lists ten optional sponsor and partner tools: AG Grid, APIMatic, Astropods, Bryntum, Channel3,
Elastic, KERNEL, Postman, Render and Zapier. Trustee already uses eight of them. This page says what each one does
for the project today and how it could help with the scaling and accountability plan in
[SCALE_ARCHITECTURE.md](./SCALE_ARCHITECTURE.md) and [PAYPAL_SCALE.md](./PAYPAL_SCALE.md).

Prize amounts and stacking rules below come from third-party listings, which disagree in places. Check the official
Devpost rules before planning around them.

## Summary

| Tool | In the repo today | Helps with | Value for scale and accountability |
|---|---|---|---|
| **Render** | Hosts the arena (`render.yaml`), free plan, in-memory store | Running the target architecture: web service, background worker, cron, managed Postgres | **High** |
| **Elastic** | Ledger shipping, reputation rules, A2A ledger analyst (`elastic/`) | Reconciliation, durability alerts, abuse detection, evidence search | **High** |
| **KERNEL** | Not used | Automating the PayPal sandbox approval in an end-to-end test; web-based delivery checks | **High** for closing the "never run against real PayPal" gap |
| **Zapier** | Human alerts (`alerts.py`), agents' back office over MCP (`network/backoffice.py`) | Human review of held disputes and storage incidents | Medium |
| **Postman** | Collection with the honest deal, refusals and A2A (`postman/`) | Load and contract tests, scheduled monitors | Medium |
| **AG Grid** | Ledger tape in the arena UI | An operator console: deals that need a person, reconciliation mismatches | Medium |
| **Channel3** | Market check for price-feed deliveries (`channel3.py`, `pricefeed.py`) | The first neutral verifier in a verifier library | Medium |
| **APIMatic** | SDK generation workflow (`.github/workflows/sdks.yml`) | SDKs that retry safely by `deal_id` and handle `503` and async opening | Low to medium |
| **Astropods** | Hosts Northwind and Tidewater (`deploy/astropods/`) | Many independent agents as realistic load | Low to medium |
| **Bryntum** | Not used | A timeline of every held authorization and its deadlines | Low for scale, useful for operators |

## Details, in order of value

### Render: run the design, not just the demo

The deployed arena runs on Render's free plan with no disk, so `TRUSTEE_DB_PATH` is unset and the store is in
memory (`render.yaml`). **The new write-ahead and resume protection therefore does nothing on the deployed demo:**
a restart forgets every deal. Render's other service types map onto the target architecture almost one to one:

| Target component | Render service |
|---|---|
| API tier | web service (scale out to more instances) |
| Deal store | managed Postgres (needs the Postgres port, step 5 of the order of work) |
| Money workers draining the outbox | background worker |
| Sweeper, reconciler, checkpoint publisher | cron jobs |

Cheapest useful step now: a paid instance with a persistent disk and `TRUSTEE_DB_PATH` set, so the demo is actually
durable. Render's hosting-credit prize would pay for it.

### Elastic: where accountability becomes visible

The ledger already ships to Elasticsearch. Build on that:

- **Durability and reconciliation alerts.** Kibana alerting rules on `paypal_alert`, `storage_unavailable` and, once
  the reconciler exists, `reconcile` rows. The goal from the design, "unreconciled money movements = 0", becomes a
  dashboard and an alarm.
- **Abuse detection.** Rejections per buyer whose claims the signed record contradicts (the new evidence check writes
  a `note` row with `op: evidence_check`) are the signal for buyers who try to keep files without paying. A query or
  an anomaly-detection job flags them **(confirm which Elastic tier includes anomaly detection)**.
- **Evidence search.** When a PayPal dispute arrives after capture, find the deal's contract, delivery hash,
  attestation and receipt in one query to build the evidence pack.

### KERNEL: get a real PayPal sandbox run into CI

KERNEL provides cloud browsers for AI agents, with persisted profiles and Playwright support. The biggest open risk in
this project is that no one has run the money path against the real PayPal sandbox; every test mocks PayPal. The one
step that needs a person is the buyer approving the order at PayPal. A hosted browser can do that step in an
end-to-end test: open the `approve_url`, log in as the sandbox buyer, approve, and let Trustee finish the hold and
capture. That turns "untested against real PayPal" into a scheduled check. Use sandbox test accounts only.

(This container also has Playwright and Chromium, so the same test can run locally first. KERNEL matters once it
runs on a schedule in CI.)

A second use: a verifier for deliverables that are web pages or live APIs, which a browser can check.

### Zapier: the human in the loop

Several outcomes now deliberately stop for a person: a buyer's claim that the signed record contradicts (funds held,
`DISPUTED`), a resume that cannot finish, storage refusing a money call. Zapier alerts already fire for each
(`dispute`, `paypal_alert`, `storage_unavailable`). Next step: route `dispute` alerts to the operator with the deal
link and the two hashes, and record the operator's decision through `POST /v1/deals/{id}/resolve`. Keep the operator
token out of Zapier unless the Zap runs in a trusted account.

### Postman: load and contract tests

Step 7 of the order of work is a load test. The existing collection already drives a full deal. Postman can run it
at volume and as a scheduled monitor **(confirm the performance-testing limits of the plan in use)**. Add requests
for the new behaviour: a `503` when storage is down, and a retried `POST /v1/deals` that resumes rather than opening
a second hold.

### AG Grid: an operator console

The ledger tape shows one run. Operators at scale need a different grid: every deal with `needs_attention`, every
`DISPUTED` deal with its dispute deadline, every outbox row older than a minute. AG Grid's server-side or infinite row
models page through large tables **(server-side row model is AG Grid Enterprise)**. AG Grid also has the largest
sponsor prizes.

### Channel3: the first verifier

`pricefeed.py` already checks a delivery against live market data. It is the model for the verifier library in
[PAYPAL_SCALE.md](./PAYPAL_SCALE.md) §4.1: a named, versioned check that Trustee runs itself, so the buyer's verdict
stops being the only evidence.

### APIMatic: SDKs that do the safe thing

Integrators will call the API from their own code. Generated SDKs can build in the behaviour that keeps money safe:
retry `POST /v1/deals` with the same `deal_id` after a timeout or `503`, and never invent a new id. Signing stays in
`trustee.client`, as `docs/SDKS.md` describes.

### Astropods: realistic load

Hosted Northwind and Tidewater agents can be multiplied into many buyer and seller pairs for the load test, so it
exercises real agents with real keys rather than one script.

### Bryntum: a hold timeline

Bryntum's scheduler and Gantt components could show every held authorization on a time axis: contract deadline,
PayPal's 3-day honor period, dispute window and the 29-day expiry. That makes "no hold is about to strand" visible at a
glance. It is an operator convenience, not a scaling tool.

## Recommendation

For the scaling and accountability goals: **Render** (make the deployed store durable, then host the worker and cron
pieces), **Elastic** (reconciliation and abuse alerts) and **KERNEL** (a real sandbox run in CI). For the hackathon
prizes, AG Grid offers the most, and the listings conflict on whether more than one sponsor prize can be won, so pick
one sponsor prize to aim for.
