# SDKs and Postman

Trustee's platform API is described by [`openapi/trustee-v1.json`](../openapi/trustee-v1.json). Two tools
build on that file:

- **Postman collection** for trying the API without writing code:
  [`postman/Trustee.postman_collection.json`](../postman/Trustee.postman_collection.json) and its
  environment file.
- **Generated SDKs** in Python and TypeScript, built by APIMatic from the same spec.

The generated code only sends requests. Signing happens outside it. See [Signing](#signing).

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

## Get an APIMatic key

1. Create an APIMatic account at [apimatic.io](https://www.apimatic.io) and check the current plan for trial
   or hackathon access. Plans change, so confirm the terms there.
2. Create an API key in the APIMatic dashboard.
3. In this repository on GitHub, open **Settings → Secrets and variables → Actions → New repository secret**.
   Name it `APIMATIC_API_KEY` and paste the key.

Until that secret exists, the `sdks` workflow prints a notice and finishes green without generating anything.

## Generate the SDKs

The workflow is [`.github/workflows/sdks.yml`](../.github/workflows/sdks.yml). It runs in two ways:

- **Manually:** Actions → **sdks** → **Run workflow**.
- **Automatically:** on any push that changes `openapi/`, `apimatic/`, `scripts/export_openapi.py`, or the
  workflow itself.

Each run does the following:

1. Copies `openapi/trustee-v1.json` into `apimatic/src/spec/`, where the APIMatic CLI reads it.
2. Validates the spec with `apimatic api validate`.
3. Generates the Python and TypeScript SDKs with `apimatic sdk generate`.
4. Uploads each SDK as a workflow artifact: `trustee-sdk-python` and `trustee-sdk-typescript`. Artifacts
   are kept for 30 days.

Open the finished run and download the artifacts from its summary page. The SDKs are not committed to this
repository. Regenerate them from the spec whenever the API changes.

The CLI version is pinned in the workflow. It needs Node 24 or newer, and the workflow sets that up.

### Generate locally

Run these from the repository root with Node 24 or newer and an APIMatic key. Output goes to
`apimatic/sdk/`, which is git-ignored:

```sh
npm install -g @apimatic/cli@2.0.0
mkdir -p apimatic/src/spec && cp openapi/trustee-v1.json apimatic/src/spec/
apimatic sdk generate --language=python --input=apimatic --auth-key "$APIMATIC_API_KEY"
apimatic sdk generate --language=typescript --input=apimatic --auth-key "$APIMATIC_API_KEY"
```

Generation runs on APIMatic's service, so an account and key are required. Without one, the CLI cannot
produce SDKs.

## Package names

These are the intended package names. Check that they are free on each registry before the first publish:

| Language   | Package       | Registry |
|------------|---------------|----------|
| Python     | `trustee-sdk` | PyPI     |
| TypeScript | `@trustee/sdk`| npm      |

Publishing is a separate step (`apimatic sdk publish`). It reads the publishing profile, and the CLI writes the
package configuration from it. Keep `apimatic/src/apimatic.json` in step with what you publish.

## Usage sketches

These sketches are illustrative. The client class and method names come from APIMatic's default naming for
the operation ids in the spec (`open_deal`, `record_delivery`, and so on). Check them against the generated
code before you rely on them.

**Python**

```python
import os
from trustee_sdk import TrusteeClient  # illustrative name until generated

client = TrusteeClient(base_url="https://trustee-arena.onrender.com")
agent = client.get_agent("demo-buyer-agent")
print(agent.roles, agent.has_paypal_email)

# Build the contract with trustee.protocol.Contract, sign it with trustee.identity.AgentKey,
# then send the signed JSON through the SDK.
deal = client.open_deal(body=signed_contract_json)  # signed_contract_json: see the Signing section
print(deal.state)
```

**TypeScript**

```typescript
import { TrusteeClient } from "@trustee/sdk"; // illustrative name until generated

const client = new TrusteeClient({ baseUrl: "https://trustee-arena.onrender.com" });
const agent = await client.getAgent({ agentId: "demo-buyer-agent" });
console.log(agent.roles, agent.hasPaypalEmail);

// signedContract is canonical JSON with two Ed25519 signatures: see the Signing section.
const deal = await client.openDeal({ body: signedContract });
console.log(deal.state);
```

## Signing

Identities, mandates, contracts and deal messages carry signatures. The platform checks each one against the
registered public key. Resolve needs the operator bearer token, and PayPal webhooks are verified against PayPal's
own signature headers.

- The SDK sends the body you give it. It never signs.
- You sign the canonical bytes of each message with your Ed25519 key. Canonical bytes means sorted keys, no
  whitespace, UTF-8, Decimals as strings, and datetimes as ISO 8601.
- The byte recipes are in `src/trustee/platform/wire.py` and `src/trustee/identity.py`. The
  [Agent Card](../README.md#platform-api-a2a) and `GET /openapi.json` describe the same scheme.

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
body = delivery.model_dump(mode="json")  # pass this to the SDK's record_delivery call
```

TypeScript agents must reproduce the same canonical JSON and Ed25519 signature. The Python signer is the
reference. Check a TypeScript signature against it before you rely on the output.

Store signing keys in an environment variable or a secret manager. Never commit them, and never put them
in a Postman environment.
