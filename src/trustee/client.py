"""TrusteeClient — a small SDK for agents that use a Trustee service over HTTP.

It signs everything it can (registration proofs, mandates, revocations) and refuses to send
a contract, delivery or attestation that is not already signed by the right party. Private
keys are used locally to sign and never leave the process.

Pass `http=` to reuse an `httpx.Client` (a FastAPI `TestClient` works), which is how the tests
run it in-process.
"""

from __future__ import annotations

from typing import Any

import httpx

from .identity import AgentKey
from .mandate import Mandate
from .platform.wire import (
    agent_signing_bytes,
    mandate_to_dict,
    principal_signing_bytes,
    revoke_signing_bytes,
)
from .protocol import Attest, Contract, Delivery


class TrusteeError(Exception):
    """The service answered with an error status. `body` is the parsed JSON when there was one
    (a 502 carries the deal view, a 403 the mandate violations)."""

    def __init__(self, status_code: int, detail: str, body: dict[str, Any] | None = None):
        super().__init__(f"{status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail
        self.body = body or {}


class TrusteeClient:
    def __init__(
        self,
        base_url: str,
        http: httpx.Client | None = None,
        operator_token: str | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self._http = http if http is not None else httpx.Client(timeout=30.0)
        self._operator_token = operator_token

    def close(self) -> None:
        self._http.close()

    # ------------------------------------------------------------------ http

    def _request(self, method: str, path: str, json: Any = None, *, operator: bool = False) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._operator_token}"} if operator and self._operator_token else {}
        resp = self._http.request(method, f"{self.base_url}{path}", json=json, headers=headers)
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if resp.status_code >= 400:
            detail = body.get("detail", resp.text) if isinstance(body, dict) else resp.text
            raise TrusteeError(resp.status_code, str(detail), body if isinstance(body, dict) else None)
        result: dict[str, Any] = body
        return result

    # ------------------------------------------------------------ identities

    def agent_card(self) -> dict[str, Any]:
        return self._request("GET", "/.well-known/agent.json")

    def register_principal(self, name: str, key: AgentKey) -> dict[str, Any]:
        public_key = key.public_key_b64
        proof = key.sign(principal_signing_bytes(name, public_key))
        body = {"principal": name, "public_key": public_key, "proof": proof}
        # Gated by the operator token when the service sets one.
        return self._request("POST", "/v1/principals", body, operator=True)

    def register_agent(
        self,
        agent_id: str,
        key: AgentKey,
        principal: str,
        principal_key: AgentKey,
        roles: list[str],
        paypal_email: str | None = None,
    ) -> dict[str, Any]:
        public_key = key.public_key_b64
        body = agent_signing_bytes(agent_id, public_key, principal, roles, paypal_email)
        return self._request(
            "POST",
            "/v1/agents",
            {
                "agent_id": agent_id,
                "public_key": public_key,
                "principal": principal,
                "roles": sorted(set(roles)),
                "paypal_email": paypal_email,
                "proof": key.sign(body),
                "principal_signature": principal_key.sign(body),
            },
        )

    def get_agent(self, agent_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/agents/{agent_id}")

    # -------------------------------------------------------------- mandates

    def publish_mandate(self, mandate: Mandate, principal_key: AgentKey) -> dict[str, Any]:
        """Sign `mandate` with the principal's key and publish it."""
        return self._request("POST", "/v1/mandates", mandate_to_dict(mandate.sign(principal_key)))

    def revoke_mandate(self, agent_id: str, principal_key: AgentKey) -> dict[str, Any]:
        signature = principal_key.sign(revoke_signing_bytes(agent_id))
        return self._request("POST", f"/v1/mandates/{agent_id}/revoke", {"signature": signature})

    # ----------------------------------------------------------------- deals

    def open_deal(self, contract: Contract) -> dict[str, Any]:
        """Open escrow for a contract that both parties already signed."""
        missing = [p for p in (contract.buyer, contract.seller) if not contract.signatures.get(p)]
        if missing:
            raise ValueError(f"contract is missing signatures from: {', '.join(missing)}")
        return self._request("POST", "/v1/deals", contract.model_dump(mode="json"))

    def deliver(self, delivery: Delivery) -> dict[str, Any]:
        if not delivery.signature:
            raise ValueError("delivery must be signed by the seller (Delivery.sign)")
        return self._request("POST", f"/v1/deals/{delivery.deal_id}/delivery", delivery.model_dump(mode="json"))

    def attest(self, attest: Attest) -> dict[str, Any]:
        if not attest.signature:
            raise ValueError("attestation must be signed by the buyer (Attest.sign)")
        return self._request("POST", f"/v1/deals/{attest.deal_id}/attest", attest.model_dump(mode="json"))

    def get_deal(self, deal_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/deals/{deal_id}")

    def events(self, deal_id: str, after_seq: int = 0, limit: int = 200) -> dict[str, Any]:
        return self._request("GET", f"/v1/deals/{deal_id}/events?after_seq={after_seq}&limit={limit}")

    def resolve(self, deal_id: str, decision: str, operator: str, reason: str = "") -> dict[str, Any]:
        """Human override of a DISPUTED deal. Needs the operator token."""
        body = {"decision": decision, "operator": operator, "reason": reason}
        return self._request("POST", f"/v1/deals/{deal_id}/resolve", body, operator=True)
