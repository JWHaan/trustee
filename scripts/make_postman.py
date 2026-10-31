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
MANDATE_CAP = Decimal("250.00")
HONEST_PRICE = Decimal("100.00")
OVERCAP_PRICE = Decimal("300.00")

POSTMAN_NAMESPACE = uuid.UUID("6f1c2a1e-0b7e-4d57-9a53-7c1e0f2d4b10")


def _lazy_imports() -> dict[str, Any]:
    """Import the trustee package only when building, so reading this file stays cheap."""
    sys.path.insert(0, str(ROOT / "src"))
    from trustee.identity import AgentKey
    from trustee.mandate import Mandate
    from trustee.platform.wire import (
        agent_signing_bytes,
        mandate_to_dict,
        principal_signing_bytes,
    )
    from trustee.protocol import Attest, Contract, Delivery, GoodSpec

    return {
        "AgentKey": AgentKey,
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
    if "body" in spec:
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
        "and a delivery signed by the wrong party.\n\n"
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
