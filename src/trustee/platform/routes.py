"""HTTP surface of the platform: /v1/* and the A2A Agent Card.

Handlers are thin. Each one hands a validated body to `DealService` on a worker thread
(PayPal and SQLite are blocking) and turns `ServiceError` into a JSON error response.

The OpenAPI metadata (operation ids, tags, summaries, documented error codes) lives on the decorators
below. It is what `scripts/export_openapi.py` publishes as `openapi/trustee-v1.json`, so keep it accurate
when a handler's status codes change.
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
from collections.abc import Callable
from typing import Any, Protocol

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ..protocol import Attest, Contract, Delivery
from .agent_card import build_agent_card
from .service import DealService, ServiceError
from .wire import AgentIn, MandateIn, PrincipalIn, ResolveIn, RevokeIn, mandate_from_in

log = logging.getLogger(__name__)

# Every error the platform returns is `{"detail": ...}`. ServiceError adds extra keys (e.g. `violations`,
# `deal`); request-validation failures (422) carry FastAPI's list of field errors instead of a string.
_ERROR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["detail"],
    "properties": {
        "detail": {
            "description": "Human-readable reason. A list of field errors for request-validation failures (422)."
        },
    },
    "additionalProperties": True,
}


def _errors(codes: dict[int, str]) -> dict[int | str, dict[str, Any]]:
    return {
        status: {"description": text, "content": {"application/json": {"schema": _ERROR_SCHEMA}}}
        for status, text in codes.items()
    }


def error_response(exc: ServiceError) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content={"detail": exc.detail, **exc.extra})


def _page(headline: str, detail: str) -> str:
    return (
        "<!doctype html><meta charset=utf-8><title>Trustee</title>"
        f"<body style='font-family:sans-serif;max-width:32em;margin:3em auto'><h1>{html.escape(headline)}</h1>"
        f"<p>{html.escape(detail)}</p></body>"
    )


class ArenaApprovals(Protocol):
    """Orders that belong to an arena run rather than a platform deal (see arena/api.py)."""

    def owns(self, order_id: str) -> bool: ...

    def cancel(self, order_id: str) -> None: ...


def build_router(service: DealService, arena_approvals: ArenaApprovals | None = None) -> APIRouter:
    router = APIRouter()

    async def call(fn: Callable[..., Any], *args: Any, status: int = 200) -> JSONResponse:
        try:
            result = await asyncio.to_thread(fn, *args)
        except ServiceError as exc:
            return error_response(exc)
        return JSONResponse(status_code=status, content=result)

    async def call_created(fn: Callable[..., tuple[dict[str, Any], bool]], *args: Any) -> JSONResponse:
        try:
            body, created = await asyncio.to_thread(fn, *args)
        except ServiceError as exc:
            return error_response(exc)
        return JSONResponse(status_code=201 if created else 200, content=body)

    # ------------------------------------------------------------ identities

    @router.post(
        "/v1/principals",
        operation_id="register_principal",
        tags=["Principals"],
        summary="Register a principal (a human or company that owns agents)",
        description=(
            "Binds a principal name to an Ed25519 public key. `proof` is the principal's signature over "
            "canonical JSON `{principal, public_key}`. Re-posting the same key is idempotent and returns 200; "
            "a new key for an existing name is a 409. If the operator has set a bearer token, registration "
            "requires it; otherwise registration is open."
        ),
        responses={
            **_errors(
                {
                    401: "Missing operator bearer token (when one is configured) or invalid proof of possession.",
                    409: "The principal already exists with a different key.",
                }
            ),
        },
    )
    async def register_principal(body: PrincipalIn, authorization: str | None = Header(default=None)) -> JSONResponse:
        try:
            service.check_registration_token(authorization)
        except ServiceError as exc:
            return error_response(exc)
        return await call_created(service.register_principal, body.principal, body.public_key, body.proof)

    @router.post(
        "/v1/agents",
        operation_id="register_agent",
        tags=["Agents"],
        summary="Register an agent under a principal",
        description=(
            "Registers an agent key. Two signatures are required: `proof` (the agent proves it holds its key) and "
            "`principal_signature` (the principal vouches for this agent). Both sign the same canonical bytes "
            "`{agent_id, public_key, principal, roles (sorted), paypal_email}`. No bearer token: the principal's "
            "signature is the authorization. Returns 201 on first registration and 200 when the identical "
            "registration is repeated."
        ),
        responses={
            **_errors(
                {
                    401: "Invalid agent proof of possession, or the principal did not vouch for this agent.",
                    409: "The agent id is registered with a different key or under a different principal.",
                    422: "Unknown principal (register it first), or a malformed body.",
                }
            ),
        },
    )
    async def register_agent(body: AgentIn) -> JSONResponse:
        # No bearer token: a registered principal's signature is the authorization.
        return await call_created(
            service.register_agent,
            body.agent_id,
            body.public_key,
            body.principal,
            body.roles,
            body.paypal_email,
            body.proof,
            body.principal_signature,
        )

    @router.get(
        "/v1/agents/{agent_id}",
        operation_id="get_agent",
        tags=["Agents"],
        summary="Read a registered agent's public record",
        description=(
            "Returns the agent's public key, principal and roles. The PayPal payout address is never returned; "
            "only `has_paypal_email` says whether one is set."
        ),
        responses=_errors({404: "No agent with this id."}),
    )
    async def get_agent(agent_id: str) -> JSONResponse:
        return await call(service.get_agent, agent_id)

    # -------------------------------------------------------------- mandates

    @router.post(
        "/v1/mandates",
        operation_id="publish_mandate",
        tags=["Mandates"],
        summary="Publish a spending or receiving mandate for an agent",
        description=(
            "A mandate caps what an agent may spend (`grants: spend`) or receive (`grants: receive`): "
            "`max_amount` per deal, optional `max_total` across deals, category and counterparty allow-lists, "
            "and `valid_until`. The principal signs every field except `principal_signature` (see the "
            "signing scheme in `info.description`). `version` must increase on every publish for the same agent, "
            "which blocks replay of an older signed grant. Mandates are enforced when a contract is opened, "
            "not when it is published."
        ),
        status_code=201,
        responses=_errors(
            {
                401: "The mandate is not signed by its principal.",
                403: "The agent belongs to a different principal.",
                404: "Unknown agent.",
                409: "The agent's mandate was revoked (register a new agent id), or `version` is not newer.",
                422: "Malformed mandate body.",
            }
        ),
    )
    async def publish_mandate(body: MandateIn) -> JSONResponse:
        return await call(service.publish_mandate, mandate_from_in(body), status=201)

    @router.post(
        "/v1/mandates/{agent_id}/revoke",
        operation_id="revoke_mandate",
        tags=["Mandates"],
        summary="Revoke an agent's mandate",
        description=(
            "The principal signs the canonical bytes `{revoke: agent_id}`. Revocation is permanent for that "
            "agent id: deals opened after it are refused by the mandate gate."
        ),
        responses=_errors(
            {
                401: "The revocation is not signed by the agent's principal.",
                404: "Unknown agent, or no mandate on file for it.",
            }
        ),
    )
    async def revoke_mandate(agent_id: str, body: RevokeIn) -> JSONResponse:
        return await call(service.revoke_mandate, agent_id, body.signature)

    # ----------------------------------------------------------------- deals

    @router.post(
        "/v1/deals",
        operation_id="open_deal",
        tags=["Deals"],
        summary="Open a deal from a contract signed by both parties",
        description=(
            "`contract.signatures` must hold a valid Ed25519 signature by the buyer and by the seller over the "
            "contract digest (sha256 of the canonical terms, excluding `signatures` and `transcript_ref`). The "
            "mandate gate then checks the buyer's spending mandate and the seller's receiving mandate. Passing the "
            "gate opens an escrow: PayPal Orders v2 `AUTHORIZE`, so the buyer's funds are held and not captured. "
            "Returns 201 for a new deal. Repeating the identical contract returns 200 with the current view. A "
            "different contract under the same `deal_id` is a 409. A 502 means PayPal failed; the body includes the "
            "deal view, and retrying the same request is safe."
        ),
        status_code=201,
        responses=_errors(
            {
                401: "A party's signature on the contract is missing or invalid.",
                403: "The mandate gate refused the deal. The body's `violations` lists each failed check.",
                409: "The deal id exists with different terms, or a store conflict.",
                422: "Invalid `deal_id`, or a malformed contract.",
                502: "PayPal failed to authorize the escrow. The body's `deal` holds the current view; retry is safe.",
            }
        ),
    )
    async def open_deal(contract: Contract) -> JSONResponse:
        return await call_created(service.open_deal, contract)

    @router.get(
        "/v1/deals/{deal_id}",
        operation_id="get_deal",
        tags=["Deals"],
        summary="Read a deal's state, escrow, ruling and history",
        description=(
            "`state` is the deal state machine position (for example CONTRACTED, ESCROWED, DELIVERED, DISPUTED, "
            "SETTLED, VOIDED). `escrow` carries the PayPal order and authorization ids and the `approve_url` the "
            "payer must visit. `history` lists every transition in order."
        ),
        responses=_errors({404: "No deal with this id."}),
    )
    async def get_deal(deal_id: str) -> JSONResponse:
        return await call(service.view, deal_id)

    @router.get(
        "/v1/deals/{deal_id}/events",
        operation_id="list_deal_events",
        tags=["Deals"],
        summary="Page through a deal's audit events",
        description=(
            "Append-only event log for the deal. Pass the last `seq` you saw as `after_seq` to read the next page; "
            "`next_after_seq` in the response is the cursor for it."
        ),
        responses=_errors({404: "No deal with this id."}),
    )
    async def deal_events(deal_id: str, after_seq: int = 0, limit: int = 200) -> JSONResponse:
        return await call(service.events, deal_id, after_seq, limit)

    @router.post(
        "/v1/deals/{deal_id}/delivery",
        operation_id="record_delivery",
        tags=["Deals"],
        summary="Record the seller's delivery of the deliverable",
        description=(
            "The seller signs the delivery with its agent key. `content_hash` is the sha256 of the bytes it "
            "delivered. The buyer checks that hash against `good.sha256` when it attests. `deliverable_ref` must "
            "match the contract. A delivery signed by anyone other than the seller is a 401."
        ),
        responses=_errors(
            {
                400: "`deal_id` in the body does not match the path.",
                401: "The delivery is not signed by the seller.",
                404: "No deal with this id.",
                409: "The deal is not in a state that accepts a delivery.",
                422: "`deliverable_ref` does not match the contract.",
            }
        ),
    )
    async def delivery(deal_id: str, body: Delivery) -> JSONResponse:
        if body.deal_id != deal_id:
            raise HTTPException(status_code=400, detail="deal_id in body does not match the path")
        return await call(service.on_delivery, deal_id, body)

    @router.post(
        "/v1/deals/{deal_id}/attest",
        operation_id="attest_delivery",
        tags=["Deals"],
        summary="Buyer's verdict on the delivery (accept or reject)",
        description=(
            "The buyer signs `accept` or `reject`. `accept` captures the held funds and settles the deal to the "
            "seller. `reject` opens a dispute: the arbiter rules, and the ruling decides whether the escrow is "
            "captured or voided. Attesting before delivery is a 409. Replaying an accept on a terminal deal is a "
            "no-op. A 502 means PayPal failed; retrying the same attestation is safe."
        ),
        responses=_errors(
            {
                400: "`deal_id` in the body does not match the path.",
                401: "The attestation is not signed by the buyer.",
                404: "No deal with this id.",
                409: "The deal is not delivered or disputed yet, or the state transition is illegal.",
                502: "PayPal failed to capture or void. The body's `deal` holds the current view.",
            }
        ),
    )
    async def attest(deal_id: str, body: Attest) -> JSONResponse:
        if body.deal_id != deal_id:
            raise HTTPException(status_code=400, detail="deal_id in body does not match the path")
        return await call(service.on_attest, deal_id, body)

    @router.post(
        "/v1/deals/{deal_id}/resolve",
        operation_id="resolve_dispute",
        tags=["Deals"],
        summary="Operator ruling on a disputed deal",
        description=(
            "Operator override for a DISPUTED deal. `release` refunds the buyer and voids the escrow; `settle` "
            "captures the held funds to the seller. Terminal deals return a no-op. Needs the operator bearer token "
            "(`TRUSTEE_OPERATOR_TOKEN`). Without that variable configured, every call is refused."
        ),
        responses=_errors(
            {
                401: "Missing or wrong operator bearer token.",
                403: "Operator endpoints are disabled: no operator token is configured.",
                404: "No deal with this id.",
                409: "The deal is not in a state that can be resolved.",
                422: "Unsupported decision, or the operator reason is malformed.",
            }
        ),
    )
    async def resolve(deal_id: str, body: ResolveIn, authorization: str | None = Header(default=None)) -> JSONResponse:
        return await call(service.resolve, deal_id, body.decision, body.operator, body.reason, authorization)

    # ---------------------------------------------------- PayPal: approval + webhooks

    async def approval(request: Request, token: str, cancelled: bool) -> JSONResponse | HTMLResponse | RedirectResponse:
        if arena_approvals is not None and arena_approvals.owns(token):
            # An arena run polls PayPal itself; the payer just goes back to the arena page.
            if cancelled:
                arena_approvals.cancel(token)
            return RedirectResponse("/", status_code=303)
        fn = service.approval_cancel if cancelled else service.approval_return
        wants_json = "application/json" in request.headers.get("accept", "")
        try:
            result = await asyncio.to_thread(fn, token)
        except ServiceError as exc:
            if wants_json:
                return error_response(exc)
            return HTMLResponse(_page("Not found", exc.detail), status_code=exc.status)
        action = result["decision"]
        status = {"error": 502, "rejected_mandate": 403}.get(action, 200)
        if wants_json:
            return JSONResponse(status_code=status, content=result)
        deal = result["deal"]
        headline = {
            "escrowed": "Payment approved. Funds are held in escrow.",
            "no-op": "Nothing to do. This payment was already handled.",
            "voided": "Payment cancelled. Nothing was charged.",
            "error": "PayPal could not finish the hold. Reload this page to retry.",
            "rejected_mandate": "The agent's spending authority is no longer valid. Nothing was held.",
        }.get(action, action)
        return HTMLResponse(_page(headline, f"Deal {deal['deal_id']} is {deal['state']}."), status_code=status)

    @router.get(
        "/v1/paypal/return",
        operation_id="paypal_return",
        tags=["PayPal"],
        summary="Payer returns from PayPal approval (browser redirect)",
        description=(
            "PayPal redirects the payer here after approving the order. The request holds funds in escrow. "
            "Replays are no-ops. Browsers get an HTML page; clients sending `Accept: application/json` get the "
            "decision as JSON."
        ),
        response_model=None,
        responses=_errors({404: "Unknown PayPal order.", 502: "PayPal failed to finish the hold."}),
    )
    async def paypal_return(request: Request, token: str) -> JSONResponse | HTMLResponse | RedirectResponse:
        return await approval(request, token, cancelled=False)

    @router.get(
        "/v1/paypal/cancel",
        operation_id="paypal_cancel",
        tags=["PayPal"],
        summary="Payer cancels PayPal approval (browser redirect)",
        description=(
            "PayPal redirects the payer here when they abandon approval. The deal moves to VOIDED. Nothing was "
            "held, so nothing is refunded."
        ),
        response_model=None,
        responses=_errors({404: "Unknown PayPal order.", 502: "PayPal failed to void the order."}),
    )
    async def paypal_cancel(request: Request, token: str) -> JSONResponse | HTMLResponse | RedirectResponse:
        return await approval(request, token, cancelled=True)

    @router.post(
        "/v1/paypal/webhook",
        operation_id="handle_paypal_webhook",
        tags=["PayPal"],
        summary="PayPal webhook receiver",
        description=(
            "Receives PayPal events (order approved, authorization voided, capture completed or failed). The "
            "request is verified against PayPal's signature headers first. Unverified requests are rejected and "
            "recorded nowhere. Verified events are de-duplicated by event id."
        ),
        responses=_errors(
            {
                400: "Body is not a JSON object, or PayPal's signature verification failed.",
                500: "Handling crashed. The event is not recorded, so PayPal's retry is processed.",
            }
        ),
    )
    async def paypal_webhook(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse(status_code=400, content={"detail": "webhook body must be JSON"})
        if not isinstance(body, dict):
            return JSONResponse(status_code=400, content={"detail": "webhook body must be a JSON object"})
        status, content = await asyncio.to_thread(service.handle_webhook, dict(request.headers), body)
        return JSONResponse(status_code=status, content=content)

    # ------------------------------------------------------------ agent card

    async def agent_card(request: Request) -> JSONResponse:
        base = os.environ.get("TRUSTEE_PUBLIC_URL") or str(request.base_url)
        return JSONResponse(build_agent_card(base), headers={"Cache-Control": "public, max-age=300"})

    router.add_api_route(
        "/.well-known/agent.json",
        agent_card,
        methods=["GET"],
        operation_id="get_agent_card",
        tags=["Discovery"],
        summary="A2A Agent Card",
        description=(
            "Describes Trustee's skills, endpoints and security schemes for A2A clients. The base URL comes from "
            "`TRUSTEE_PUBLIC_URL`, or from the request host when that is unset."
        ),
    )
    router.add_api_route(
        "/.well-known/agent-card.json",
        agent_card,
        methods=["GET"],
        operation_id="get_agent_card_alias",
        tags=["Discovery"],
        summary="A2A Agent Card (current spec path)",
        description="Same document as `/.well-known/agent.json`, served at the path the A2A spec now uses.",
    )
    return router
