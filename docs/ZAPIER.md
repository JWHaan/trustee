# Zapier alerts: hear about it when your agents' money needs a person

Trustee lets agents move money on their own. The few moments that need a human are sent to a
[Webhooks by Zapier → Catch Hook](https://zapier.com/apps/webhooks/integrations) trigger, which you route to
Slack, email or anything else Zapier supports. Alerts are derived from the ledger. They never sit on the money
path, and a slow or broken webhook cannot delay or fail a payment.

## Set up the Zap

1. In Zapier, create a Zap. Trigger: **Webhooks by Zapier → Catch Hook**. Copy the hook URL.
2. Set it on the server (Render dashboard, or `.env` locally):

   ```
   TRUSTEE_ALERT_WEBHOOK_URL=https://hooks.zapier.com/hooks/catch/<id>/<key>/
   TRUSTEE_ALERT_WEBHOOK_SECRET=<optional random string>
   ```

   The URL must be `https://` (plain `http://` is accepted only for `localhost` / `127.0.0.1`). A bad URL
   is logged and the server runs without alerts.
3. Send a sample so Zapier can learn the fields:

   ```
   python -m trustee.alerts --test
   ```

   It exits non-zero if the webhook did not accept the request. In Zapier choose **Test trigger** and pick the sample.
4. Add an action: **Slack → Send Channel Message** or **Gmail → Send Email**. Map `title`, `summary`,
   `severity`, `deal_id`, `deal_url`, and for disputes `dispute_deadline`.
5. Optional: add a **Filter** step before the action, for example `severity` *(Text) Exactly matches* `critical`
   to page only on the urgent ones.

Unset `TRUSTEE_ALERT_WEBHOOK_URL` and nothing changes: no subscriber, and `/healthz` reports `"alerts": "off"`.
When it is set, `/healthz` reports `"alerts": "webhook"`.

## Payload

Flat JSON, one POST per alert:

| field | meaning |
| --- | --- |
| `alert` | machine name, see the table below |
| `title`, `summary` | one-line headline and one plain sentence |
| `severity` | `info`, `warning` or `critical` |
| `deal_id`, `state` | the deal and its last known state |
| `actor` | who wrote the ledger event (for example `trustee`) |
| `source` | `platform` (the public `/v1` API) or `arena` (demo runs) |
| `occurred_at` | ISO timestamp of the ledger event |
| `deal_url` | `{TRUSTEE_PUBLIC_URL}/v1/deals/{id}`; empty for arena alerts or without a public URL |
| `arena_url` | `TRUSTEE_PUBLIC_URL` |
| `dispute_deadline` | only on `dispute`: when the dispute window ends |

Payloads never contain secrets, signatures, keys, emails or raw PayPal bodies.

## What each alert means

| `alert` | Title | What to do |
| --- | --- | --- |
| `dispute` | Dispute opened — funds held | Funds are held at PayPal. Review the deal and resolve it before `dispute_deadline` with `POST /v1/deals/{id}/resolve` (operator token). |
| `operator_resolved` | Operator resolved a dispute | Nothing. This is the audit trail of a human decision. |
| `paypal_alert` | PayPal reported something the deal can't follow | A denied, reversed or refunded capture that no deal state models. Check the order in PayPal. |
| `signature_rejected` | Signature rejected | A message was missing a valid signature. Usually a misconfigured agent. Repeated alerts may mean someone is probing. No money moved. |
| `mandate_recheck` | Mandate failed re-check | The agent's authority lapsed before approval or capture. Funds are not captured. Renew or revoke the mandate, then resolve the deal. |
| `approval_expired` | Payer approval expired | The payer did not approve (or cancelled) at PayPal. Nothing was held. Re-open the deal if it is still wanted. |
| `paypal_call_failed` | PayPal call failed — funds still held, retry pending | A capture or void failed. Funds are still held and the same call is safe to retry. Check PayPal status if it repeats. |

Each deal sends at most one alert of each type. `TRUSTEE_ALERT_KINDS` (comma list of the names above) limits
which types are sent.

## Verify the signature

If `TRUSTEE_ALERT_WEBHOOK_SECRET` is set, every request carries:

* `X-Trustee-Signature: sha256=<hex>`, the HMAC-SHA256 of the raw request body keyed with the secret
* `X-Trustee-Timestamp`: unix seconds when the request was sent

Zapier's Catch Hook does not check headers on its own. To verify, use the **Catch Raw Hook** variant and a
**Code by Zapier** step:

```python
import hmac, hashlib

expected = "sha256=" + hmac.new(SECRET.encode(), input_data["raw_body"].encode(), hashlib.sha256).hexdigest()
if not hmac.compare_digest(expected, input_data["signature"]):
    raise Exception("bad signature")
return {"ok": True}
```

(map `raw_body` and `signature` from the trigger). Otherwise, treat the secret hook URL itself as the credential.

## Delivery behavior

Delivery runs on one background thread: 5 second timeout, up to 3 retries with 1s/2s/4s backoff on network
errors, HTTP 5xx and 429. Other 4xx are not retried. A bounded queue (200) drops the oldest alert with a warning
when the webhook is down for a long time. Failures are logged and never raised.

## Arena alerts for the video

Arena runs are demos, and every judge click on "cheat" would page Slack, so they are off by default. For a recording
set `TRUSTEE_ALERT_ARENA=1`; arena events then alert with `"source": "arena"`. A correctly refunded cheat is an
objective breach, so it opens no dispute and sends no alert. The `forged` scenario sends `signature_rejected`. A
payer who never approves a live PayPal sandbox order sends `approval_expired`.

## Zapier MCP back office

This page covers Trustee's alert webhooks. Northwind and Tidewater can also call Zapier MCP themselves to book settled deals in their own apps (invoices, CRM, bills, finance notes, and a step-up message to the principal). That setup lives in [ZAPIER_MCP.md](ZAPIER_MCP.md).
