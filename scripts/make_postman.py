"""Generate the Postman collection and environment for the Trustee platform API.

Postman's scripting sandbox has no Ed25519, so every signed request body is produced here, in Python, with
the same `trustee.identity` and `trustee.platform.wire` helpers the server verifies with. The output is
deterministic: fixed timestamps, fixed ids, and demo keys derived from fixed seeds. Re-running the script
produces byte-identical files, which `tests/test_openapi.py` checks.

DEMO KEYS ARE PUBLIC. The seeds below are written in this repository. The keys they produce exist only to sign
the sample requests in the collection. Never give them money, mandates or a production registration.

Outputs:
    postman/Trustee.postman_collection.json
    postman/Trustee.postman_environment.json

Usage:
    .venv/bin/python scripts/make_postman.py
"""

from __future__ import annotations

import hashlib
import json
import sys
import uuid
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
POSTMAN_DIR = ROOT / "postman"
COLLECTION_PATH = POSTMAN_DIR / "Trustee.postman_collection.json"
ENVIRONMENT_PATH = POSTMAN_DIR / "Trustee.postman_environment.json"

DEMO_SEED_PREFIX = b"trustee-w12-demo-only:"
DEFAULT_BASE_URL = "http://127.0.0.1:8000"

# Fixed timestamps: a signed body must not change between runs.
FORMED_AT = datetime(2026, 10, 31, 9, 0, 0)
DELIVERED_AT = datetime(2026, 10, 31, 9, 5, 0)
ATTESTED_AT = datetime(2026, 10, 31, 9, 10, 0)
FAR_FUTURE = datetime(2027, 12, 31, 23, 59, 0)

DELIVERABLE = b"trustee w12 demo deliverable\n"
DELIVERABLE_REF = "returns-2026q3.csv"

BUYER_PRINCIPAL = "demo-buyer-org"
SELLER_PRINCIPAL = "demo-seller-org"
BUYER_AGENT = "demo-buyer-agent"
SELLER_AGENT = "demo-seller-agent"
HONEST_DEAL = "deal-w12-honest-001"
FORGED_DEAL = "deal-w12-forged-001"
OVERCAP_DEAL = "deal-w12-overcap-001"
A2A_DEAL = "deal-w14-a2a-001"
A2A_FORGED_DEAL = "deal-w14-a2a-forged-001"
AUTO_PRINCIPAL = "demo-auto-org"
AUTO_BUYER = "demo-auto-buyer"
AUTO_DEAL = "deal-w16-auto-001"
AUTO_FUNDING = "fund-w16-demo"
AUTO_INSTRUCTION = (
    "Buy the Q3 2026 returns extract; spend at most $175; only from demo-seller-agent; must match the sha256"
)
ISSUED_AT = datetime(2026, 10, 31, 8, 0, 0)
MANDATE_CAP = Decimal("250.00")
HONEST_PRICE = Decimal("100.00")
OVERCAP_PRICE = Decimal("300.00")

POSTMAN_NAMESPACE = uuid.UUID("6f1c2a1e-0b7e-4d57-9a53-7c1e0f2d4b10")


def _lazy_imports() -> dict[str, Any]:
    """Import the trustee package only when building, so reading this file stays cheap."""
    sys.path.insert(0, str(ROOT / "src"))
    from trustee.identity import AgentKey
    from trustee.intent import IntentMandate
    from trustee.mandate import Mandate
    from trustee.platform.wire import (
        agent_signing_bytes,
        funding_setup_signing_bytes,
        mandate_to_dict,
        principal_signing_bytes,
    )
    from trustee.protocol import Attest, Contract, Delivery, GoodSpec

    return {
        "AgentKey": AgentKey,
        "IntentMandate": IntentMandate,
        "funding_setup_signing_bytes": funding_setup_signing_bytes,
        "Mandate": Mandate,
        "agent_signing_bytes": agent_signing_bytes,
        "mandate_to_dict": mandate_to_dict,
        "principal_signing_bytes": principal_signing_bytes,
        "Attest": Attest,
        "Contract": Contract,
        "Delivery": Delivery,
        "GoodSpec": GoodSpec,
    }


def build_requests() -> list[dict[str, Any]]:
    """Every request in order, with bodies already signed. Returns plain dicts for the collection writer."""
    m = _lazy_imports()
    AgentKey = m["AgentKey"]

    def key(name: str) -> Any:
        return AgentKey.from_private_bytes(hashlib.sha256(DEMO_SEED_PREFIX + name.encode()).digest())

    buyer_org, seller_org, attacker = key(BUYER_PRINCIPAL), key(SELLER_PRINCIPAL), key("attacker")
    buyer, seller = key(BUYER_AGENT), key(SELLER_AGENT)
    digest = hashlib.sha256(DELIVERABLE).hexdigest()

    def principal_body(name: str, signer: Any) -> dict[str, Any]:
        pub = signer.public_key_b64
        return {
            "principal": name,
            "public_key": pub,
            "proof": signer.sign(m["principal_signing_bytes"](name, pub)),
        }

    def agent_body(agent_id: str, signer: Any, principal: str, principal_key: Any, roles: list[str]) -> dict[str, Any]:
        pub = signer.public_key_b64
        signing = m["agent_signing_bytes"](agent_id, pub, principal, roles, None)
        return {
            "agent_id": agent_id,
            "public_key": pub,
            "principal": principal,
            "roles": roles,
            "paypal_email": None,
            "proof": signer.sign(signing),
            "principal_signature": principal_key.sign(signing),
        }

    def mandate_body(agent_id: str, principal: str, principal_key: Any, grants: str) -> dict[str, Any]:
        mandate = m["Mandate"](
            agent_id=agent_id,
            principal=principal,
            max_amount=MANDATE_CAP,
            valid_until=FAR_FUTURE,
            grants=grants,
            notes="demo-only mandate",
            max_total=Decimal("1000.00") if grants == "spend" else None,
            version=1,
        ).sign(principal_key)
        return m["mandate_to_dict"](mandate)

    def contract(deal_id: str, price: Decimal, buyer_key: Any) -> dict[str, Any]:
        good = m["GoodSpec"](
            good_id="good-w12-returns-2026q3",
            description="Q3 2026 returns extract",
            category="digital",
            acceptance_criteria=["sha256 matches the contract", "CSV parses"],
            deliverable_ref=DELIVERABLE_REF,
            sha256=digest,
        )
        base = m["Contract"](
            deal_id=deal_id,
            buyer=BUYER_AGENT,
            seller=SELLER_AGENT,
            good=good,
            price=price,
            deadline=FAR_FUTURE,
            formed_at=FORMED_AT,
        )
        signed = base.sign(BUYER_AGENT, buyer_key).sign(SELLER_AGENT, seller)
        return signed.model_dump(mode="json")

    def delivery_body(deal_id: str, signer: Any) -> dict[str, Any]:
        msg = m["Delivery"](
            msg_id=f"msg-{deal_id}-delivery",
            sender=SELLER_AGENT,
            ts=DELIVERED_AT,
            deal_id=deal_id,
            deliverable_ref=DELIVERABLE_REF,
            content_hash=digest,
            delivered_at=DELIVERED_AT,
        ).sign(signer)
        return msg.model_dump(mode="json")

    def attest_body(deal_id: str) -> dict[str, Any]:
        msg = m["Attest"](
            msg_id=f"msg-{deal_id}-attest",
            sender=BUYER_AGENT,
            ts=ATTESTED_AT,
            deal_id=deal_id,
            verdict="accept",
            reason="sha256 of the delivered bytes matches the contract",
            evidence={"sha256": digest},
        ).sign(buyer)
        return msg.model_dump(mode="json")

    auto_org, auto_buyer = key(AUTO_PRINCIPAL), key(AUTO_BUYER)

    def auto_contract(deal_id: str, price: Decimal) -> dict[str, Any]:
        good = m["GoodSpec"](
            good_id="good-w12-returns-2026q3",
            description="Q3 2026 returns extract",
            category="digital",
            acceptance_criteria=["sha256 matches the contract", "CSV parses"],
            deliverable_ref=DELIVERABLE_REF,
            sha256=digest,
        )
        base = m["Contract"](
            deal_id=deal_id,
            buyer=AUTO_BUYER,
            seller=SELLER_AGENT,
            good=good,
            price=price,
            deadline=FAR_FUTURE,
            formed_at=FORMED_AT,
        )
        return base.sign(AUTO_BUYER, auto_buyer).sign(SELLER_AGENT, seller).model_dump(mode="json")

    def auto_attest(deal_id: str) -> dict[str, Any]:
        msg = m["Attest"](
            msg_id=f"msg-{deal_id}-attest",
            sender=AUTO_BUYER,
            ts=ATTESTED_AT,
            deal_id=deal_id,
            verdict="accept",
            reason="sha256 of the delivered bytes matches the contract",
            evidence={"sha256": digest},
        ).sign(auto_buyer)
        return msg.model_dump(mode="json")

    def funding_body() -> dict[str, Any]:
        nonce = "w16-demo-nonce-001"
        sig = auto_org.sign(m["funding_setup_signing_bytes"](AUTO_PRINCIPAL, nonce, AUTO_FUNDING))
        return {"nonce": nonce, "funding_ref": AUTO_FUNDING, "signature": sig}

    def intent_body() -> dict[str, Any]:
        intent = m["IntentMandate"](
            intent_id="intent-w16-demo-001",
            agent_id=AUTO_BUYER,
            principal=AUTO_PRINCIPAL,
            instruction=AUTO_INSTRUCTION,
            funding_ref=AUTO_FUNDING,
            max_price=Decimal("175.00"),
            allowed_sellers=[SELLER_AGENT],
            good_ids=["good-w12-returns-2026q3"],
            required_criteria=["sha256"],
            issued_at=ISSUED_AT,
            expires_at=FAR_FUTURE,
        ).sign(auto_org)
        return intent.public()

    def rpc(rpc_id: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params}

    def skill_message(
        message_id: str, skill: str, payload: dict[str, Any], task_id: str | None = None
    ) -> dict[str, Any]:
        message: dict[str, Any] = {
            "messageId": message_id,
            "role": "ROLE_USER",
            "parts": [{"data": {"skill": skill, "input": payload}}],
        }
        if task_id is not None:
            message["taskId"] = task_id
        return {"message": message}

    return [
        {
            "folder": "1 · Discover",
            "name": "Agent card",
            "method": "GET",
            "path": "/.well-known/agent.json",
            "description": "A2A Agent Card: skills, endpoints and the signing scheme.",
            "tests": [
                "pm.test('200 with an agent card', () => {",
                "  pm.response.to.have.status(200);",
                "  pm.expect(pm.response.json()).to.have.property('name');",
                "});",
            ],
        },
        {
            "folder": "1 · Discover",
            "name": "OpenAPI document",
            "method": "GET",
            "path": "/openapi.json",
            "description": "The full OpenAPI document the server exposes. Use it to generate clients.",
            "tests": [
                "pm.test('200 and the deal routes are present', () => {",
                "  pm.response.to.have.status(200);",
                "  pm.expect(pm.response.json().paths).to.have.property('/v1/deals');",
                "});",
            ],
        },
        {
            "folder": "1 · Discover",
            "name": "Health",
            "method": "GET",
            "path": "/healthz",
            "description": "Liveness probe.",
            "tests": ["pm.test('healthy', () => pm.response.to.have.status(200));"],
        },
        {
            "folder": "2 · Honest deal (pre-signed)",
            "name": "1. Register buyer principal",
            "method": "POST",
            "path": "/v1/principals",
            "body": principal_body(BUYER_PRINCIPAL, buyer_org),
            "description": "The buyer's organisation proves its key. 201 first time, 200 when repeated.",
            "tests": [
                "pm.test('registered (201) or already registered (200)', () => {",
                "  pm.expect([200, 201]).to.include(pm.response.code);",
                "});",
            ],
        },
        {
            "folder": "2 · Honest deal (pre-signed)",
            "name": "2. Register seller principal",
            "method": "POST",
            "path": "/v1/principals",
            "body": principal_body(SELLER_PRINCIPAL, seller_org),
            "description": "The seller's organisation proves its key.",
            "tests": [
                "pm.test('registered (201) or already registered (200)', () => {",
                "  pm.expect([200, 201]).to.include(pm.response.code);",
                "});",
            ],
        },
        {
            "folder": "2 · Honest deal (pre-signed)",
            "name": "3. Register buyer agent",
            "method": "POST",
            "path": "/v1/agents",
            "body": agent_body(BUYER_AGENT, buyer, BUYER_PRINCIPAL, buyer_org, ["buyer"]),
            "description": "The buyer agent, vouched for by its principal.",
            "tests": [
                "pm.test('buyer agent registered', () => {",
                "  pm.expect([200, 201]).to.include(pm.response.code);",
                "});",
            ],
        },
        {
            "folder": "2 · Honest deal (pre-signed)",
            "name": "4. Register seller agent",
            "method": "POST",
            "path": "/v1/agents",
            "body": agent_body(SELLER_AGENT, seller, SELLER_PRINCIPAL, seller_org, ["seller"]),
            "description": "The seller agent, vouched for by its principal.",
            "tests": [
                "pm.test('seller agent registered', () => {",
                "  pm.expect([200, 201]).to.include(pm.response.code);",
                "});",
            ],
        },
        {
            "folder": "2 · Honest deal (pre-signed)",
            "name": "5. Publish buyer spend mandate",
            "method": "POST",
            "path": "/v1/mandates",
            "body": mandate_body(BUYER_AGENT, BUYER_PRINCIPAL, buyer_org, "spend"),
            "description": (
                "Caps the buyer at 250 per deal and 1000 in total. Publishing the identical mandate again is "
                "idempotent (201). Only a different mandate at the same or an older version is a 409 replay."
            ),
            "tests": [
                "pm.test('mandate published', () => {",
                "  pm.response.to.have.status(201);",
                "});",
            ],
        },
        {
            "folder": "2 · Honest deal (pre-signed)",
            "name": "6. Publish seller receive mandate",
            "method": "POST",
            "path": "/v1/mandates",
            "body": mandate_body(SELLER_AGENT, SELLER_PRINCIPAL, seller_org, "receive"),
            "description": "The seller may receive payment. The gate needs a receive mandate on each side.",
            "tests": [
                "pm.test('mandate published', () => {",
                "  pm.response.to.have.status(201);",
                "});",
            ],
        },
        {
            "folder": "2 · Honest deal (pre-signed)",
            "name": "7. Open deal (contract signed by both parties)",
            "method": "POST",
            "path": "/v1/deals",
            "body": contract(HONEST_DEAL, HONEST_PRICE, buyer),
            "description": (
                "Both parties sign the contract digest. The mandate gate passes, and PayPal authorizes (holds) "
                "100.00. A repeat of the identical contract returns 200 with the current deal."
            ),
            "tests": [
                "pm.test('deal open (201) or identical deal already open (200)', () => {",
                "  pm.expect([200, 201]).to.include(pm.response.code);",
                "  pm.expect(pm.response.json().deal_id).to.eql('" + HONEST_DEAL + "');",
                "});",
            ],
        },
        {
            "folder": "2 · Honest deal (pre-signed)",
            "name": "8. Record delivery (signed by the seller)",
            "method": "POST",
            "path": f"/v1/deals/{HONEST_DEAL}/delivery",
            "body": delivery_body(HONEST_DEAL, seller),
            "description": (
                "The seller hands off the deliverable and records its hash. 409 if the deal already settled."
            ),
            "tests": [
                "pm.test('delivery recorded (200), or deal already settled (409)', () => {",
                "  pm.expect([200, 409]).to.include(pm.response.code);",
                "});",
            ],
        },
        {
            "folder": "2 · Honest deal (pre-signed)",
            "name": "9. Attest accept (signed by the buyer)",
            "method": "POST",
            "path": f"/v1/deals/{HONEST_DEAL}/attest",
            "body": attest_body(HONEST_DEAL),
            "description": "The buyer verifies the hash and accepts. Accept captures the held funds to the seller.",
            "tests": [
                "pm.test('attested (200); replays on a settled deal are no-ops (200)', () => {",
                "  pm.response.to.have.status(200);",
                "});",
            ],
        },
        {
            "folder": "2 · Honest deal (pre-signed)",
            "name": "10. Get deal",
            "method": "GET",
            "path": f"/v1/deals/{HONEST_DEAL}",
            "description": "The honest deal ends SETTLED.",
            "tests": [
                "pm.test('honest deal is SETTLED with a captured escrow', () => {",
                "  pm.response.to.have.status(200);",
                "  pm.expect(pm.response.json().state).to.eql('SETTLED');",
                "});",
            ],
        },
        {
            "folder": "2 · Honest deal (pre-signed)",
            "name": "11. List deal events",
            "method": "GET",
            "path": f"/v1/deals/{HONEST_DEAL}/events?after_seq=0&limit=50",
            "description": "The append-only audit trail for the deal.",
            "tests": [
                "pm.test('audit events are returned', () => {",
                "  pm.response.to.have.status(200);",
                "  pm.expect(pm.response.json().events.length).to.be.above(0);",
                "});",
            ],
        },
        {
            "folder": "3 · Refusals",
            "name": "Forged contract (buyer signed with an unregistered key)",
            "method": "POST",
            "path": "/v1/deals",
            "body": contract(FORGED_DEAL, HONEST_PRICE, attacker),
            "description": (
                "The buyer's signature is from a key the platform does not hold for that agent. Refused with 401."
            ),
            "tests": [
                "pm.test('forged contract refused with 401, nothing held', () => {",
                "  pm.response.to.have.status(401);",
                "  pm.expect(pm.response.json().detail).to.be.a('string');",
                "});",
            ],
        },
        {
            "folder": "3 · Refusals",
            "name": "Over-cap deal (price above the mandate cap)",
            "method": "POST",
            "path": "/v1/deals",
            "body": contract(OVERCAP_DEAL, OVERCAP_PRICE, buyer),
            "description": "Both signatures are valid, but 300.00 exceeds the buyer's 250.00 cap. Refused with 403.",
            "tests": [
                "pm.test('over-cap deal refused with 403 and a violation list', () => {",
                "  pm.response.to.have.status(403);",
                "  pm.expect(pm.response.json().violations).to.be.an('array').that.is.not.empty;",
                "});",
            ],
        },
        {
            "folder": "3 · Refusals",
            "name": "Delivery signed by the buyer instead of the seller",
            "method": "POST",
            "path": f"/v1/deals/{HONEST_DEAL}/delivery",
            "body": delivery_body(HONEST_DEAL, buyer),
            "description": "Only the seller can claim delivery. A buyer-signed delivery is refused with 401.",
            "tests": [
                "pm.test('buyer-signed delivery refused with 401', () => {",
                "  pm.response.to.have.status(401);",
                "});",
            ],
        },
        {
            "folder": "4 · A2A",
            "name": "1. Agent card lists the JSON-RPC interface",
            "method": "GET",
            "path": "/.well-known/agent-card.json",
            "description": (
                "A standard A2A client starts here. The card lists the JSON-RPC binding at /a2a and says push "
                "notifications are supported. Folder 4 uses the identities registered in folder 2, so run that first."
            ),
            "tests": [
                "pm.test('JSON-RPC interface and push notifications are advertised', () => {",
                "  pm.response.to.have.status(200);",
                "  const card = pm.response.json();",
                "  const rpc = card.supportedInterfaces.find(i => i.protocolBinding === 'JSONRPC');",
                "  pm.expect(rpc.url).to.match(/\\/a2a$/);",
                "  pm.expect(card.capabilities.pushNotifications).to.eql(true);",
                "});",
            ],
        },
        {
            "folder": "4 · A2A",
            "name": "2. SendMessage: open-escrow (contract signed by both parties)",
            "method": "POST",
            "path": "/a2a",
            "body": rpc(
                1, "SendMessage", skill_message("msg-w14-open", "open-escrow", contract(A2A_DEAL, HONEST_PRICE, buyer))
            ),
            "description": (
                "The same signed contract as POST /v1/deals, carried in a DataPart. The deal becomes a task whose id "
                "is the deal id. With dry-run PayPal the funds are held at once, so the task asks the seller for the "
                "delivery (INPUT_REQUIRED). With sandbox credentials it is AUTH_REQUIRED until the payer approves."
            ),
            "tests": [
                "pm.test('the deal is a task (id = deal id), waiting on a person or the seller', () => {",
                "  const task = pm.response.json().result.task;",
                "  pm.expect(task.id).to.eql('" + A2A_DEAL + "');",
                "  pm.expect(['TASK_STATE_INPUT_REQUIRED', 'TASK_STATE_AUTH_REQUIRED', 'TASK_STATE_COMPLETED'])",
                "    .to.include(task.status.state);",
                "});",
            ],
        },
        {
            "folder": "4 · A2A",
            "name": "3. GetTask",
            "method": "POST",
            "path": "/a2a",
            "body": rpc(2, "GetTask", {"id": A2A_DEAL}),
            "description": "The task is a view of the durable deal, rebuilt on every read.",
            "tests": [
                "pm.test('GetTask returns the deal task', () => {",
                "  pm.response.to.have.status(200);",
                "  const task = pm.response.json().result;",
                "  pm.expect(task.id).to.eql('" + A2A_DEAL + "');",
                "  pm.expect(task.contextId).to.eql('" + A2A_DEAL + "');",
                "});",
            ],
        },
        {
            "folder": "4 · A2A",
            "name": "4. SendMessage: record-delivery (signed by the seller)",
            "method": "POST",
            "path": "/a2a",
            "body": rpc(
                3,
                "SendMessage",
                skill_message("msg-w14-deliver", "record-delivery", delivery_body(A2A_DEAL, seller), A2A_DEAL),
            ),
            "description": (
                "The seller reports the delivery to the task. On a re-run the task has finished, and A2A refuses "
                "messages to a finished task with -32004."
            ),
            "tests": [
                "pm.test('delivery recorded (WORKING), or the task already finished (-32004)', () => {",
                "  const body = pm.response.json();",
                "  if (body.error) { pm.expect(body.error.code).to.eql(-32004); return; }",
                "  pm.expect(body.result.task.status.state).to.eql('TASK_STATE_WORKING');",
                "});",
            ],
        },
        {
            "folder": "4 · A2A",
            "name": "5. SendMessage: attest-delivery (signed by the buyer)",
            "method": "POST",
            "path": "/a2a",
            "body": rpc(
                4, "SendMessage", skill_message("msg-w14-attest", "attest-delivery", attest_body(A2A_DEAL), A2A_DEAL)
            ),
            "description": "The buyer verifies the hash and accepts. The held funds are captured to the seller.",
            "tests": [
                "pm.test('attested: the task completes, or it had already finished (-32004)', () => {",
                "  const body = pm.response.json();",
                "  if (body.error) { pm.expect(body.error.code).to.eql(-32004); return; }",
                "  pm.expect(body.result.task.status.state).to.eql('TASK_STATE_COMPLETED');",
                "});",
            ],
        },
        {
            "folder": "4 · A2A",
            "name": "6. GetTask: COMPLETED with a settlement receipt",
            "method": "POST",
            "path": "/a2a",
            "body": rpc(5, "GetTask", {"id": A2A_DEAL}),
            "description": "The result is an artifact: outcome, amount, order and capture ids, contract digest.",
            "tests": [
                "pm.test('COMPLETED with a settled settlement-receipt', () => {",
                "  const task = pm.response.json().result;",
                "  pm.expect(task.status.state).to.eql('TASK_STATE_COMPLETED');",
                "  const receipt = task.artifacts.find(a => a.artifactId === 'settlement-receipt').parts[0].data;",
                "  pm.expect(receipt.outcome).to.eql('settled');",
                "  pm.expect(receipt.amount).to.eql('" + str(HONEST_PRICE) + "');",
                "  pm.expect(receipt).to.have.property('captureId');",
                "});",
            ],
        },
        {
            "folder": "4 · A2A",
            "name": "Forged contract over A2A is REJECTED",
            "method": "POST",
            "path": "/a2a",
            "body": rpc(
                6,
                "SendMessage",
                skill_message("msg-w14-forged", "open-escrow", contract(A2A_FORGED_DEAL, HONEST_PRICE, attacker)),
            ),
            "description": "A buyer signature from an unregistered key. Nothing is held and the deal id is not taken.",
            "tests": [
                "pm.test('REJECTED, with the reason in the status message', () => {",
                "  const task = pm.response.json().result.task;",
                "  pm.expect(task.status.state).to.eql('TASK_STATE_REJECTED');",
                "  pm.expect(task.status.message.parts[0].text).to.include('401');",
                "});",
            ],
        },
        {
            "folder": "4 · A2A",
            "name": "Unknown method is a JSON-RPC error",
            "method": "POST",
            "path": "/a2a",
            "body": rpc(7, "DoesNotExist", {}),
            "description": "Protocol mistakes are JSON-RPC errors (-32601 here) inside an HTTP 200.",
            "tests": [
                "pm.test('-32601 method not found', () => {",
                "  pm.response.to.have.status(200);",
                "  pm.expect(pm.response.json().error.code).to.eql(-32601);",
                "});",
            ],
        },
        {
            "folder": "5 · Autonomous payment (intent mandate)",
            "name": "1. Register principal",
            "method": "POST",
            "path": "/v1/principals",
            "body": principal_body(AUTO_PRINCIPAL, auto_org),
            "description": "The human behind the buyer agent proves their key.",
            "tests": ["pm.test('registered', () => pm.expect([200, 201]).to.include(pm.response.code));"],
        },
        {
            "folder": "5 · Autonomous payment (intent mandate)",
            "name": "2. Register buyer agent",
            "method": "POST",
            "path": "/v1/agents",
            "body": agent_body(AUTO_BUYER, auto_buyer, AUTO_PRINCIPAL, auto_org, ["buyer"]),
            "description": "The agent that will pay on its own. Needs the seller from folder 2.",
            "tests": ["pm.test('registered', () => pm.expect([200, 201]).to.include(pm.response.code));"],
        },
        {
            "folder": "5 · Autonomous payment (intent mandate)",
            "name": "3. Publish spend mandate",
            "method": "POST",
            "path": "/v1/mandates",
            "body": mandate_body(AUTO_BUYER, AUTO_PRINCIPAL, auto_org, "spend"),
            "description": "The ordinary spend ceiling. The intent mandate can only narrow what happens inside it.",
            "tests": ["pm.test('published', () => pm.response.to.have.status(201));"],
        },
        {
            "folder": "5 · Autonomous payment (intent mandate)",
            "name": "4. Start the one-time funding consent",
            "method": "POST",
            "path": "/v1/principals/" + AUTO_PRINCIPAL + "/funding",
            "body": funding_body(),
            "description": (
                "Signed by the principal, with the funding_ref it chose. Without PayPal credentials the source is "
                "active at once. Against the sandbox the response carries `approve_url`: open it ONCE and approve, "
                "then PayPal returns to /v1/funding/return and the source turns active. The PayPal vault id is "
                "never returned."
            ),
            "tests": [
                "pm.test('funding source created', () => {",
                "  pm.expect([200, 201]).to.include(pm.response.code);",
                "  const body = pm.response.json();",
                "  pm.expect(body.funding_ref).to.eql('" + AUTO_FUNDING + "');",
                "  pm.expect(JSON.stringify(body)).to.not.include('vault_id');",
                "});",
            ],
        },
        {
            "folder": "5 · Autonomous payment (intent mandate)",
            "name": "5. Publish the signed intent mandate",
            "method": "POST",
            "path": "/v1/intents",
            "body": intent_body(),
            "description": (
                "The human's instruction and limits, signed once: at most 175.00, only from demo-seller-agent, the "
                "contract must commit to a sha256. Publishing the identical document again returns 200."
            ),
            "tests": [
                "pm.test('intent published', () => {",
                "  pm.expect([200, 201]).to.include(pm.response.code);",
                "  pm.expect(pm.response.json().intent_mandate_digest).to.have.lengthOf(64);",
                "});",
            ],
        },
        {
            "folder": "5 · Autonomous payment (intent mandate)",
            "name": "6. Open the deal: the agent pays by itself",
            "method": "POST",
            "path": "/v1/deals",
            "body": auto_contract(AUTO_DEAL, HONEST_PRICE),
            "description": (
                "Every intent rule passes, so the funds are held with no approval click. `authority` in the response "
                "says who authorized the payment and lists each rule's result."
            ),
            "tests": [
                "pm.test('escrowed under the intent mandate', () => {",
                "  pm.expect([200, 201]).to.include(pm.response.code);",
                "  const deal = pm.response.json();",
                "  pm.expect(deal.state).to.be.oneOf(['ESCROWED', 'DELIVERED', 'SETTLED']);",
                "  pm.expect(deal.authority.human_present).to.eql(false);",
                "  pm.expect(deal.authority.policy.length).to.be.above(10);",
                "});",
            ],
        },
        {
            "folder": "5 · Autonomous payment (intent mandate)",
            "name": "7. Record delivery (signed by the seller)",
            "method": "POST",
            "path": "/v1/deals/" + AUTO_DEAL + "/delivery",
            "body": delivery_body(AUTO_DEAL, seller),
            "description": "Same as folder 2. A repeat after settlement is a 409.",
            "tests": [
                "pm.test('recorded (or already settled)', () => pm.expect([200, 409]).to.include(pm.response.code));"
            ],
        },
        {
            "folder": "5 · Autonomous payment (intent mandate)",
            "name": "8. Attest (signed by the buyer)",
            "method": "POST",
            "path": "/v1/deals/" + AUTO_DEAL + "/attest",
            "body": auto_attest(AUTO_DEAL),
            "description": "Capture happens only now, after the intent is re-checked at the moment of capture.",
            "tests": [
                "pm.test('settled', () => {",
                "  pm.response.to.have.status(200);",
                "  pm.expect(pm.response.json().state).to.eql('SETTLED');",
                "});",
            ],
        },
        {
            "folder": "5 · Autonomous payment (intent mandate)",
            "name": "9. Signed settlement receipt",
            "method": "GET",
            "path": "/v1/deals/" + AUTO_DEAL + "/receipt",
            "description": (
                "Trustee's Ed25519-signed record of the outcome. Kept in the `receipt` variable for step 10."
            ),
            "tests": [
                "pm.test('signed receipt for an unattended payment', () => {",
                "  pm.response.to.have.status(200);",
                "  const r = pm.response.json();",
                "  pm.expect(r.outcome).to.eql('settled');",
                "  pm.expect(r.signature).to.be.a('string');",
                "  pm.expect(r.intent_mandate_digest).to.have.lengthOf(64);",
                "  pm.collectionVariables.set('receipt', JSON.stringify(r));",
                "});",
            ],
        },
        {
            "folder": "5 · Autonomous payment (intent mandate)",
            "name": "10. Verify the receipt",
            "method": "POST",
            "path": "/v1/receipts/verify",
            "raw_body": '{"receipt": {{receipt}}}',
            "description": (
                "Server-side check of the signature. For a check that trusts no server, run "
                "`python -m trustee.verify receipt.json --key <public key>`."
            ),
            "tests": [
                "pm.test('signature verifies', () => {",
                "  pm.response.to.have.status(200);",
                "  pm.expect(pm.response.json().ok).to.eql(true);",
                "});",
            ],
        },
        {
            "folder": "5 · Autonomous payment (intent mandate)",
            "name": "Trustee receipt-signing key",
            "method": "GET",
            "path": "/.well-known/trustee-key.json",
            "description": "The public key to pin when verifying receipts offline.",
            "tests": [
                "pm.test('an Ed25519 key', () => {",
                "  pm.response.to.have.status(200);",
                "  pm.expect(pm.response.json().algorithm).to.eql('Ed25519');",
                "});",
            ],
        },
    ]


def _request_item(spec: dict[str, Any]) -> dict[str, Any]:
    path = spec["path"]
    raw_path, _, query = path.partition("?")
    segments = [s for s in raw_path.strip("/").split("/") if s]
    url: dict[str, Any] = {"raw": "{{baseUrl}}" + path, "host": ["{{baseUrl}}"], "path": segments}
    if query:
        url["query"] = [dict(zip(("key", "value"), pair.split("=", 1), strict=True)) for pair in query.split("&")]
    request: dict[str, Any] = {
        "method": spec["method"],
        "header": [{"key": "Accept", "value": "application/json"}],
        "url": url,
        "description": spec["description"],
    }
    if "raw_body" in spec:
        request["header"].append({"key": "Content-Type", "value": "application/json"})
        request["body"] = {"mode": "raw", "raw": spec["raw_body"], "options": {"raw": {"language": "json"}}}
    elif "body" in spec:
        request["header"].append({"key": "Content-Type", "value": "application/json"})
        request["body"] = {
            "mode": "raw",
            "raw": json.dumps(spec["body"], indent=2, sort_keys=True, ensure_ascii=False),
            "options": {"raw": {"language": "json"}},
        }
    return {
        "name": spec["name"],
        "request": request,
        "event": [{"listen": "test", "script": {"type": "text/javascript", "exec": spec["tests"]}}],
    }


def build_collection() -> dict[str, Any]:
    folders: dict[str, list[dict[str, Any]]] = {}
    for spec in build_requests():
        folders.setdefault(spec["folder"], []).append(_request_item(spec))
    description = (
        "Runs the Trustee platform API end to end without writing code.\n\n"
        "**Demo-only keys.** Request bodies are signed in advance with Ed25519 keys derived from seeds that are "
        "published in `scripts/make_postman.py`. These keys exist only to sign these samples. Do not register "
        "them on a server that holds money, and do not reuse them as real identities.\n\n"
        "**Run it.** Start the API (`uvicorn trustee.arena.api:app --port 8000`) and run the collection. "
        "Folder 2 is the honest path: the deal settles and PayPal captures the held funds (dry-run when no PayPal "
        "credentials are set). Folder 3 proves three refusals: a forged signature, a price over the mandate cap, "
        "and a delivery signed by the wrong party. Folder 4 runs the same deal over the A2A JSON-RPC binding "
        "(`POST /a2a`): it needs the identities from folder 2, so run the folders in order.\n\n"
        "**Re-running.** A fresh server gives a clean pass. On the same server, registrations return 200, the "
        "identical mandates and deal return 201 and 200, the delivery returns 409 (the deal has already settled) and "
        "the attestation returns 200 as a no-op.\n\n"
        "The API contract is `openapi/trustee-v1.json`. The SDK workflow is documented in `docs/SDKS.md`."
    )
    return {
        "info": {
            "_postman_id": str(uuid.uuid5(POSTMAN_NAMESPACE, "trustee-platform-api")),
            "name": "Trustee Platform API",
            "description": description,
            "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
        },
        "item": [{"name": name, "item": items} for name, items in folders.items()],
        "variable": [
            {"key": "baseUrl", "value": DEFAULT_BASE_URL, "type": "string"},
            {"key": "operatorToken", "value": "", "type": "string"},
            {"key": "receipt", "value": "{}", "type": "string"},
        ],
    }


def build_environment() -> dict[str, Any]:
    return {
        "id": str(uuid.uuid5(POSTMAN_NAMESPACE, "trustee-local-environment")),
        "name": "Trustee local",
        "values": [
            {"key": "baseUrl", "value": DEFAULT_BASE_URL, "type": "default", "enabled": True},
            {"key": "operatorToken", "value": "", "type": "secret", "enabled": True},
        ],
        "_postman_variable_scope": "environment",
    }


def render(obj: dict[str, Any]) -> str:
    return json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main() -> int:
    POSTMAN_DIR.mkdir(parents=True, exist_ok=True)
    COLLECTION_PATH.write_text(render(build_collection()), encoding="utf-8")
    ENVIRONMENT_PATH.write_text(render(build_environment()), encoding="utf-8")
    print(f"wrote {COLLECTION_PATH}")
    print(f"wrote {ENVIRONMENT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
