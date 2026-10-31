"""HTTP surface of the platform: /v1/* and the A2A Agent Card.

Handlers are thin. Each one hands a validated body to `DealService` on a worker thread
(PayPal and SQLite are blocking) and turns `ServiceError` into a JSON error response.
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from ..protocol import Attest, Contract, Delivery
from .agent_card import build_agent_card
from .service import DealService, ServiceError
from .wire import AgentIn, MandateIn, PrincipalIn, ResolveIn, RevokeIn, mandate_from_in

log = logging.getLogger(__name__)


def error_response(exc: ServiceError) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content={"detail": exc.detail, **exc.extra})


def _page(headline: str, detail: str) -> str:
    return (
        "<!doctype html><meta charset=utf-8><title>Trustee</title>"
        f"<body style='font-family:sans-serif;max-width:32em;margin:3em auto'><h1>{html.escape(headline)}</h1>"
        f"<p>{html.escape(detail)}</p></body>"
    )


def build_router(service: DealService) -> APIRouter:
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

    @router.post("/v1/principals")
    async def register_principal(body: PrincipalIn, authorization: str | None = Header(default=None)) -> JSONResponse:
        try:
            service.check_registration_token(authorization)
        except ServiceError as exc:
            return error_response(exc)
        return await call_created(service.register_principal, body.principal, body.public_key, body.proof)

    @router.post("/v1/agents")
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

    @router.get("/v1/agents/{agent_id}")
    async def get_agent(agent_id: str) -> JSONResponse:
        return await call(service.get_agent, agent_id)

    # -------------------------------------------------------------- mandates

    @router.post("/v1/mandates")
    async def publish_mandate(body: MandateIn) -> JSONResponse:
        return await call(service.publish_mandate, mandate_from_in(body), status=201)

    @router.post("/v1/mandates/{agent_id}/revoke")
    async def revoke_mandate(agent_id: str, body: RevokeIn) -> JSONResponse:
        return await call(service.revoke_mandate, agent_id, body.signature)

    # ----------------------------------------------------------------- deals

    @router.post("/v1/deals")
    async def open_deal(contract: Contract) -> JSONResponse:
        return await call_created(service.open_deal, contract)

    @router.get("/v1/deals/{deal_id}")
    async def get_deal(deal_id: str) -> JSONResponse:
        return await call(service.view, deal_id)

    @router.get("/v1/deals/{deal_id}/events")
    async def deal_events(deal_id: str, after_seq: int = 0, limit: int = 200) -> JSONResponse:
        return await call(service.events, deal_id, after_seq, limit)

    @router.post("/v1/deals/{deal_id}/delivery")
    async def delivery(deal_id: str, body: Delivery) -> JSONResponse:
        if body.deal_id != deal_id:
            raise HTTPException(status_code=400, detail="deal_id in body does not match the path")
        return await call(service.on_delivery, deal_id, body)

    @router.post("/v1/deals/{deal_id}/attest")
    async def attest(deal_id: str, body: Attest) -> JSONResponse:
        if body.deal_id != deal_id:
            raise HTTPException(status_code=400, detail="deal_id in body does not match the path")
        return await call(service.on_attest, deal_id, body)

    @router.post("/v1/deals/{deal_id}/resolve")
    async def resolve(deal_id: str, body: ResolveIn, authorization: str | None = Header(default=None)) -> JSONResponse:
        return await call(service.resolve, deal_id, body.decision, body.operator, body.reason, authorization)

    # ---------------------------------------------------- PayPal: approval + webhooks

    async def approval(request: Request, token: str, cancelled: bool) -> JSONResponse | HTMLResponse:
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

    @router.get("/v1/paypal/return", response_model=None)
    async def paypal_return(request: Request, token: str) -> JSONResponse | HTMLResponse:
        return await approval(request, token, cancelled=False)

    @router.get("/v1/paypal/cancel", response_model=None)
    async def paypal_cancel(request: Request, token: str) -> JSONResponse | HTMLResponse:
        return await approval(request, token, cancelled=True)

    @router.post("/v1/paypal/webhook")
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

    router.add_api_route("/.well-known/agent.json", agent_card, methods=["GET"])
    router.add_api_route("/.well-known/agent-card.json", agent_card, methods=["GET"])
    return router
