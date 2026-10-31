"""GET /v1/agents/{agent_id}/reputation: public, read-only. Kept out of routes.py so that file stays small."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from .routes import _errors
from .service import DealService

log = logging.getLogger(__name__)


def build_reputation_router(service: DealService) -> APIRouter:
    router = APIRouter()

    @router.get(
        "/v1/agents/{agent_id}/reputation",
        operation_id="get_agent_reputation",
        tags=["Agents"],
        summary="Read an agent's track record",
        description=(
            "Counts over every deal the agent was buyer or seller of: deals, settled, voided, disputed, "
            "arbiter rulings lost (seller side), dispute rate and median delivery time. `source` says whether "
            "it was computed from the platform store (`local`) or from the Elastic ledger index (`elastic`)."
        ),
        responses=_errors({404: "No agent with this id.", 502: "The reputation source (Elastic) did not answer."}),
    )
    async def get_agent_reputation(agent_id: str) -> JSONResponse:
        if service.registry.get(agent_id) is None:
            return JSONResponse(status_code=404, content={"detail": f"unknown agent {agent_id!r}"})
        try:
            rep = await asyncio.to_thread(service.reputation.reputation, agent_id)
        except Exception as exc:  # noqa: BLE001 - the source's failure is a 502, never a 500 with internals
            log.warning("reputation lookup for %s failed: %s", agent_id, type(exc).__name__)
            return JSONResponse(status_code=502, content={"detail": "the reputation source is unavailable"})
        body: dict[str, Any] = rep.to_dict()
        return JSONResponse(content=body)

    return router
