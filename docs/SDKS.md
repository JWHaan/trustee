# Client libraries and Postman

Trustee's platform API is described by [`openapi/trustee-v1.json`](../openapi/trustee-v1.json). Two ways to call it:

- **`trustee.client.TrusteeClient`**, the Python client in this repository. It signs registrations, mandates and
  revocations itself, and refuses to send a contract, delivery or attestation that the right party has not signed
  (`Contract.sign`, `Delivery.sign`, `Attest.sign`).
- **Postman collection** for trying the API without writing code:
  [`postman/Trustee.postman_collection.json`](../postman/Trustee.postman_collection.json) and its
  environment file.

Other languages can generate a client from the OpenAPI file with any generator. A generated client only sends
requests; signing happens outside it. See [Signing](#signing).

## Postman

1. Start the API: `.venv/bin/uvicorn trustee.arena.api:app --host 127.0.0.1 --port 8000`.
2. In Postman, import `postman/Trustee.postman_collection.json` and `postman/Trustee.postman_environment.json`.
3. Select the **Trustee local** environment and run the collection.

Folder 2 walks the honest path: two principals, two agents, two mandates, a deal, a delivery, an accept, and
the settled deal. Folder 3 shows three refusals. The request bodies are signed in advance with demo-only
keys. Those keys exist only in the sample requests, so do not register them on a server that holds money.

To run the same collection from the command line, use [newman](https://github.com/postmanlabs/newman):

```sh
npx --yes newman@6 run postman/Trustee.postman_collection.json \
  -e postman/Trustee.postman_environment.json --env-var baseUrl=http://127.0.0.1:8000
```

The collection is generated. Edit `scripts/make_postman.py` and run `.venv/bin/python scripts/make_postman.py`.
Do not edit the JSON by hand.

## Python client

```python
from trustee.client import TrusteeClient

client = TrusteeClient("https://trustee-arena.onrender.com")
deal = client.open_deal(contract)  # contract signed by both parties; a retry with the same deal_id is safe
```

`examples/a2a_deal.py` runs a full escrowed deal with it: two principals, two agents, mandates, a contract,
delivery and attestation.

**Retries.** Opening a deal is idempotent on `deal_id`. After a timeout, a `502` or a `503`, send the same request
again with the same `deal_id`; never make up a new one, or a second hold could be opened. A `503` means Trustee
could not record the payment intent and made no PayPal call.

## Signing

Identities, mandates, contracts and deal messages carry signatures. The platform checks each one against the
registered public key. Resolve needs the operator bearer token, and PayPal webhooks are verified against PayPal's
own signature headers.

- A generated client sends the body you give it. It never signs.
- You sign the canonical bytes of each message with your Ed25519 key. Canonical bytes means sorted keys, no
  whitespace, UTF-8, Decimals as strings, and datetimes as ISO 8601.
- The byte recipes are in `src/trustee/platform/wire.py` and `src/trustee/identity.py`. The
  [Agent Card](API.md#platform-api) and `GET /openapi.json` describe the same scheme.

Python agents can use the library directly:

```python
import base64, os
from trustee.identity import AgentKey
from trustee.protocol import Delivery

key = AgentKey.from_private_bytes(base64.urlsafe_b64decode(os.environ["SELLER_AGENT_KEY"]))
delivery = Delivery(
    sender="demo-seller-agent",
    deal_id="deal-123",
    deliverable_ref="report.csv",
    content_hash="<sha256 hex of the delivered bytes>",
).sign(key)
body = delivery.model_dump(mode="json")  # the body of POST /v1/deals/{id}/delivery
```

TypeScript agents must reproduce the same canonical JSON and Ed25519 signature. The Python signer is the
reference. Check a TypeScript signature against it before you rely on the output.

Store signing keys in an environment variable or a secret manager. Never commit them, and never put them
in a Postman environment.
