"""A2A Agent Card — how another agent discovers what Trustee does and how to talk to it.

Field names follow the current A2A specification (`supportedInterfaces`, `securityRequirements`)
and, side by side, the v0.3 names (`protocolVersion`, `url`, `preferredTransport`, `security`) so
clients written against either generation can read the card. Only the Agent Card and a plain
HTTP+JSON surface are implemented; see the README for what is not.
"""

from __future__ import annotations

from typing import Any

VERSION = "0.1.0"

SIGNING_SCHEME = (
    "Every mutating call is attributable to an Ed25519 key. Keys are 32-byte raw public keys, urlsafe-base64. "
    "Signed bytes are canonical JSON (sorted keys, no whitespace, UTF-8; Decimals as strings, datetimes as ISO 8601) "
    "of the message without its signature field. Signatures are urlsafe-base64. A principal proves its key by "
    "signing {principal, public_key}; an agent proves possession by signing its registration body "
    "{agent_id, public_key, principal, roles (sorted), paypal_email} and is vouched for by the principal "
    "signing the same bytes; a mandate is signed by its principal over every field except principal_signature; "
    "a contract carries one signature per party over its sha256 digest; deliveries are signed by the seller and "
    "attestations by the buyer. Operator endpoints use an HTTP bearer token."
)


def _skill(skill_id: str, name: str, description: str, tags: list[str], examples: list[str]) -> dict[str, Any]:
    return {
        "id": skill_id,
        "name": name,
        "description": description,
        "tags": tags,
        "examples": examples,
        "inputModes": ["application/json"],
        "outputModes": ["application/json"],
    }


SKILLS: list[dict[str, Any]] = [
    _skill(
        "register-agent",
        "Register an agent",
        "POST /v1/principals then POST /v1/agents: bind an Ed25519 key to an agent id under a principal "
        "that vouches for it.",
        ["identity", "registration"],
        ["Register buyer-agent-1 under principal acme with role buyer"],
    ),
    _skill(
        "publish-mandate",
        "Publish a spending mandate",
        "POST /v1/mandates: a principal-signed limit (cap, categories, counterparties, expiry, total budget) "
        "for one agent. POST /v1/mandates/{agent_id}/revoke withdraws it.",
        ["mandate", "authorization"],
        ["Allow buyer-agent-1 to spend up to 100 USD on code from seller-agent-1"],
    ),
    _skill(
        "open-escrow",
        "Open an escrow",
        "POST /v1/deals with a contract signed by both parties. Both mandates are checked, then PayPal funds "
        "are held (not moved). Idempotent on deal_id.",
        ["escrow", "paypal", "payments"],
        ["Escrow 25.00 USD for deal deal_ab12 once both agents signed the contract"],
    ),
    _skill(
        "record-delivery",
        "Record a delivery",
        "POST /v1/deals/{id}/delivery: the seller reports the deliverable and its sha256, signed.",
        ["delivery"],
        ["Report delivery of returns-2026q3.csv with its content hash"],
    ),
    _skill(
        "attest-delivery",
        "Attest to a delivery",
        "POST /v1/deals/{id}/attest: the buyer accepts (funds captured to the seller) or rejects (arbitration, "
        "possible refund), signed.",
        ["attestation", "settlement", "arbitration"],
        ["Accept the delivery for deal deal_ab12 and release payment"],
    ),
    _skill(
        "get-deal",
        "Get deal state",
        "GET /v1/deals/{id} for state, escrow and ruling; GET /v1/deals/{id}/events for the persisted ledger.",
        ["status", "audit"],
        ["What is the state of deal deal_ab12?"],
    ),
]


def build_agent_card(base_url: str) -> dict[str, Any]:
    base = base_url.rstrip("/")
    api = f"{base}/v1"
    return {
        "name": "Trustee",
        "description": (
            "Escrow and arbitration for agent-to-agent payments on PayPal. Two agents sign a contract, Trustee "
            "checks both mandates, holds the buyer's funds with a PayPal authorization, and captures or voids "
            "them from the buyer's signed attestation, a deadline, or an arbiter's ruling. " + SIGNING_SCHEME
        ),
        "version": VERSION,
        "provider": {"organization": "Trustee", "url": base},
        "documentationUrl": f"{base}/api/docs",
        # Current spec
        "supportedInterfaces": [{"url": api, "protocolBinding": "HTTP+JSON", "protocolVersion": "0.3"}],
        # v0.3 compatibility
        "protocolVersion": "0.3.0",
        "url": api,
        "preferredTransport": "HTTP+JSON",
        "additionalInterfaces": [{"url": api, "transport": "HTTP+JSON"}],
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
            "extensions": [
                {
                    "uri": "https://github.com/trustee/ext/ed25519-signed-messages/v1",
                    "description": SIGNING_SCHEME,
                    "required": True,
                }
            ],
        },
        "securitySchemes": {
            "ed25519Signature": {
                "type": "http",
                "scheme": "ed25519-signature",
                "description": SIGNING_SCHEME,
            },
            "operatorBearer": {
                "type": "http",
                "scheme": "bearer",
                "description": "Operator token for POST /v1/deals/{id}/resolve (and registration when configured).",
            },
        },
        "securityRequirements": [{"ed25519Signature": []}],
        "security": [{"ed25519Signature": []}],
        "defaultInputModes": ["application/json"],
        "defaultOutputModes": ["application/json"],
        "skills": SKILLS,
    }
