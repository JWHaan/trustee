# Elastic: ledger index, reputation and the ledger analyst

Everything here is opt-in. With no `ELASTICSEARCH_URL` or `ELASTIC_CLOUD_ID`, Trustee behaves exactly as before.

Three pieces:

1. **Ledger shipping.** Every ledger event is bulk-indexed into Elasticsearch.
2. **Reputation.** `GET /v1/agents/{agent_id}/reputation`, and mandate rules that use it. Computed locally by default, or from the Elastic index with one ES|QL query.
3. **Ledger analyst.** An Elastic Agent Builder agent that answers questions about deals from ES|QL tools, reachable over A2A.

## Set up Elastic

1. Create an Elastic Cloud **Serverless Elasticsearch** project (or an Elastic Cloud 9.3+ deployment with Kibana). Agent Builder is GA on Serverless and on Stack 9.3+.
2. In Kibana, open **Agent Builder** and make sure a **LLM connector** is available (the Elastic-managed default LLM on Cloud, or one you add under Connectors). The analyst agent needs one to answer.
3. Create an API key (Kibana: *API keys*, or `POST /_security/api_key`) with these privileges. Replace the index pattern if you changed `ELASTIC_LEDGER_INDEX`:

```json
{
  "name": "trustee",
  "role_descriptors": {
    "trustee": {
      "cluster": ["monitor", "manage_index_templates", "monitor_inference"],
      "indices": [
        {"names": ["trustee-ledger*"], "privileges": ["auto_configure", "create_index", "create", "read", "view_index_metadata"]}
      ],
      "applications": [
        {
          "application": "kibana-.kibana",
          "privileges": ["feature_agentBuilder.all", "feature_actions.read"],
          "resources": ["space:default"]
        }
      ]
    }
  }
}
```

The same key does all three jobs (writing the ledger, running the setup script, calling the analyst). To split duties, give the running server a key with only the `indices` block, and the setup key the `applications` block. On Serverless the cluster privileges are ignored; create the key in the project's *API keys* page.

4. Run the setup once:

```bash
export ELASTICSEARCH_URL=https://<project>.es.<region>.<cloud>.elastic.cloud
export KIBANA_URL=https://<project>.kb.<region>.<cloud>.elastic.cloud
export ELASTIC_API_KEY=<encoded key>
uv run python scripts/elastic_setup.py --dry-run   # print every request first
uv run python scripts/elastic_setup.py
```

It is idempotent (re-running updates in place). It creates the index template and index, five ES|QL tools and the agent `trustee-ledger-analyst`, then prints the agent's A2A card URL. Use `--legacy-param-types` on Elastic Stack 9.2 to 9.3 (tool parameter types are `keyword` there, `string` on Serverless and 9.4+).

## Environment

| Variable | Used by | Meaning |
| --- | --- | --- |
| `ELASTICSEARCH_URL` | server, setup | Elasticsearch endpoint. `https://` only; `http://` is allowed for `localhost` and `127.0.0.1`. |
| `ELASTIC_CLOUD_ID` | server | Alternative to the URL; decoded to the Elasticsearch endpoint. |
| `ELASTIC_API_KEY` | all | Encoded API key, sent as `Authorization: ApiKey ...`. |
| `ELASTIC_LEDGER_INDEX` | all | Index name, default `trustee-ledger`. |
| `TRUSTEE_ELASTIC_ARENA` | server | `1` also ships arena demo runs (off by default, like `TRUSTEE_ALERT_ARENA`). |
| `TRUSTEE_REPUTATION_BACKEND` | server | `local` (default) or `elastic`. |
| `KIBANA_URL` | setup, analyst | Kibana base URL. |
| `ELASTIC_ANALYST_AGENT_ID` | analyst | Defaults to `trustee-ledger-analyst`. |

## Ledger shipping

`trustee.elastic.sink.ElasticSink` subscribes to the platform ledger (the same pattern as the Zapier `AlertSink`). The subscriber only builds a document and enqueues it. One daemon thread sends `_bulk` batches (100 events) with a timeout, exponential backoff, and a bounded queue (5000, oldest dropped first). Only failed items of a partly failed batch are retried. Each document has a stable `_id`, and `create` answering 409 counts as delivered, so a retry never duplicates. The money path never waits on Elasticsearch, and a dead cluster only costs log lines.

**What is indexed:** the fields a ledger row already shows on the arena tape: `@timestamp` (UTC), `deal_id`, `actor`, `kind`, plus promoted `event`, `op`, `state_from`, `state_to`, `decision`, `clause`, `override`, `ok`, a short `detail` (note, explanation or reason, cut to 500 characters), and the rest of the payload as a `flattened` field. From each deal's `contract_formed` row the sink adds `buyer`, `seller`, `category`, `amount` and `currency` to that deal's later rows, so queries need no joins.

**What is not indexed:** any payload key that looks like a credential (`signature`, `secret`, `token`, `password`, `email`, `authorization`, `api_key`, `private`, `cookie`, `public_key`) is dropped, strings are cut to 500 characters, and PayPal bodies are not part of ledger rows. Mandate and agent records, keys and payout addresses never reach the index.

Limit: the sink learns buyer and seller from `contract_formed` in the running process. Rows for a deal whose contract was formed before a restart have no `buyer`/`seller` until that deal ends; reputation counts only deals the index can attribute.

## Reputation

Per agent, over deals where it was buyer or seller:

* `deals_total`: deals with a contract.
* `settled`, `voided`: final state. Voided covers refunds, timeouts and rejected deliveries.
* `disputed`: deals that entered `DISPUTED` (a buyer rejected the delivery).
* `rulings_lost`: seller side, deals with a `ruling` row deciding `release` (money returned to the buyer, by the arbiter or an operator).
* `dispute_rate` = `disputed / deals_total`.
* `median_delivery_seconds`: seller side, `ESCROWED` to `DELIVERED`.
* `first_seen`, `last_seen`: earliest and latest ledger row of the agent's deals.

`GET /v1/agents/{agent_id}/reputation` is public and read-only. It returns these plus `source` (`local` or `elastic`) and `as_of`, 404 for an unknown agent and 502 if the Elastic backend does not answer. `GET /v1/agents/{agent_id}` is unchanged.

The local backend reads the platform SQLite store. The Elastic backend (`TRUSTEE_REPUTATION_BACKEND=elastic`) runs this query with named parameters (`?agent`, optionally `?exclude_deal`), cached for 30 seconds:

```esql
FROM trustee-ledger
| WHERE (buyer == ?agent OR seller == ?agent)
| EVAL f_settled = CASE(kind == "state" AND state_to == "SETTLED", 1, 0),
       f_voided = CASE(kind == "state" AND state_to == "VOIDED", 1, 0),
       f_disputed = CASE(kind == "state" AND state_to == "DISPUTED", 1, 0),
       f_lost = CASE(kind == "ruling" AND decision == "release" AND seller == ?agent, 1, 0),
       t_escrowed = CASE(kind == "state" AND state_to == "ESCROWED", @timestamp),
       t_delivered = CASE(kind == "state" AND state_to == "DELIVERED" AND seller == ?agent, @timestamp)
| STATS d_settled = MAX(f_settled), d_voided = MAX(f_voided), d_disputed = MAX(f_disputed), d_lost = MAX(f_lost),
        d_escrowed = MIN(t_escrowed), d_delivered = MAX(t_delivered),
        d_first = MIN(@timestamp), d_last = MAX(@timestamp) BY deal_id
| EVAL delivery_s = DATE_DIFF("seconds", d_escrowed, d_delivered)
| STATS deals_total = COUNT(*), settled = SUM(d_settled), voided = SUM(d_voided), disputed = SUM(d_disputed),
        rulings_lost = SUM(d_lost), median_delivery_s = MEDIAN(delivery_s),
        first_seen = MIN(d_first), last_seen = MAX(d_last)
```

If both arena and platform rows are shipped (`TRUSTEE_ELASTIC_ARENA=1`), they share the index and the `source` field tells them apart; the reputation query does not filter on it.

### Mandate reputation rules

A mandate can carry two optional rules about the **other** party of a deal:

* `min_counterparty_deals` (integer): refuse a counterparty with fewer deals on record.
* `max_counterparty_dispute_rate` (decimal, 0 to 1): refuse a counterparty whose dispute rate is higher.

Both are accepted by `POST /v1/mandates` and the A2A `publish-mandate` skill. They are part of what the principal signs, but only when set: a mandate signed before these fields existed has the same signing bytes and still verifies. The check runs in `MandateEngine.check_deal` when a deal opens, against the counterparty's reputation excluding the deal being opened. A mandate that sets a rule while no reputation source exists, or when the lookup fails, is refused (fail closed) with a reason. Every passing rule is added to the report (`mandates OK; buyer: counterparty 'seller' deals 12 >= 5; ...`), so the result lands on the ledger tape and in the Elastic index. The rules are not re-evaluated at capture: a counterparty's record changing after funds are held must not strand them, while expiry, revocation and budget are still re-checked.

A brand-new counterparty has 0 deals and a 0.0 dispute rate, so use `max_counterparty_dispute_rate` together with `min_counterparty_deals`.

## Ledger analyst

`scripts/elastic_setup.py` creates these Agent Builder ES|QL tools over the ledger index: `trustee-deal-history` (all events of a deal), `trustee-spend` and `trustee-earnings` (settled money by agent and currency since a date), `trustee-void-reasons` (recent voids with reasons), `trustee-agent-reputation`. The agent `trustee-ledger-analyst` has only those tools and is instructed to answer from tool results, cite deal ids, and say it cannot move money (it has no tool that could).

Agent Builder serves it over A2A, with API-key auth and no streaming: card at `GET {KIBANA_URL}/api/agent_builder/a2a/trustee-ledger-analyst.json`, JSON-RPC at `POST {KIBANA_URL}/api/agent_builder/a2a/trustee-ledger-analyst` using `message/send`.

```bash
KIBANA_URL=... ELASTIC_API_KEY=... uv run python -m trustee.analyst "why was deal deal_123 refunded?"
```

`trustee.analyst.LedgerAnalyst` is the client (`ask(question) -> str`). Trustee never calls the analyst from the payment path. Spend by principal is not a tool: the ledger rows carry agent ids, not principals.
