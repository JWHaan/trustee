"""Asking a hosted Northwind to buy, from outside.

A hosted Northwind (on Astropods) does nothing until someone starts a purchase. The arena's "Three A2A agents"
scenario is that someone, and this is the smallest door it can use: two operator routes on Northwind's own app.

    POST /operator/purchases        {"deal_id": "...", "seller_url": "..."}   -> 202, the purchase runs in a thread
    GET  /operator/purchases/{id}   -> state, the PayPal approval link, the A2A hops so far, and the outcome

Both need `Authorization: Bearer <secret>`. The routes exist only when a secret is configured (`install` is not
called otherwise), so with no secret they answer 404 like any unknown path. The secret is compared in constant
time. A purchase can only go to a seller on the allow-list the operator configured, so the secret does not turn
Northwind into a request relay. One purchase runs at a time. The trigger chooses neither the price nor the good:
those are Northwind's own configuration, and the money still moves only through Trustee.
"""

from __future__ import annotations

import hmac
import logging
import threading
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from .common import Hop
from .northwind import Northwind

log = logging.getLogger(__name__)

MAX_RUNS = 50
MAX_DEAL_ID = 80


def _hop_dict(hop: Hop) -> dict[str, str]:
    return {
        "caller": hop.caller,
        "peer": hop.peer,
        "method": hop.method,
        "skill": hop.skill,
        "note": hop.note,
        "price": hop.price,
    }


class PurchaseRuns:
    """The purchases asked for through the trigger, newest last. One at a time."""

    def __init__(self) -> None:
        self._runs: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._lock = threading.Lock()
        self._current: str | None = None

    def current(self) -> dict[str, Any] | None:
        return self._runs.get(self._current) if self._current else None

    def observe(self, hop: Hop) -> None:
        with self._lock:
            run = self.current()
            if run is not None and run["state"] == "running":
                run["hops"].append(_hop_dict(hop))

    def approval(self, deal_id: str, order_id: str, approve_url: str) -> None:
        with self._lock:
            run = self._runs.get(deal_id)
            if run is not None:
                run["approval"] = {"order_id": order_id, "approve_url": approve_url}

    def start(self, deal_id: str) -> dict[str, Any] | None:
        """A fresh record, or None when a purchase is already running."""
        with self._lock:
            running = self.current()
            if running is not None and running["state"] == "running":
                return None
            record: dict[str, Any] = {"deal_id": deal_id, "state": "running", "approval": None, "hops": []}
            self._runs[deal_id] = record
            self._runs.move_to_end(deal_id)
            self._current = deal_id
            while len(self._runs) > MAX_RUNS:
                self._runs.popitem(last=False)
            return record

    def finish(self, deal_id: str, **fields: Any) -> None:
        with self._lock:
            run = self._runs.get(deal_id)
            if run is not None:
                run.update(fields)

    def view(self, deal_id: str) -> dict[str, Any] | None:
        with self._lock:
            run = self._runs.get(deal_id)
            if run is None:
                return None
            return {**run, "approval": dict(run["approval"]) if run["approval"] else None, "hops": list(run["hops"])}


def authorized(header: str | None, secret: str) -> bool:
    if not header or not header.lower().startswith("bearer "):
        return False
    return hmac.compare_digest(header[7:].strip().encode(), secret.encode())


def install(
    agent: Northwind,
    runs: PurchaseRuns,
    secret: str,
    allowed_sellers: list[str],
    *,
    spawn: Callable[[Callable[[], None]], None] | None = None,
) -> None:
    """Add the two operator routes to `agent.app`. Call only when `secret` is set."""
    if not secret:
        raise ValueError("a trigger needs a secret")
    allowed = {u.rstrip("/") for u in allowed_sellers}

    def deny() -> JSONResponse:
        return JSONResponse({"detail": "unauthorized"}, status_code=401)

    def work(deal_id: str, seller_url: str) -> None:
        try:
            purchase = agent.buy(seller_url, deal_id=deal_id)
        except Exception as exc:  # noqa: BLE001 - a failed purchase is reported, not raised
            log.warning("purchase %s failed: %s", deal_id, type(exc).__name__)
            runs.finish(deal_id, state="failed", error=str(exc)[:300])
            return
        runs.finish(
            deal_id,
            state="finished",
            seller=purchase.seller,
            buyer=agent.identity.agent_id,
            outcome=purchase.outcome,
            turns=purchase.turns,
            price=str(purchase.price) if purchase.price is not None else None,
            receipt=purchase.receipt,
            attest=purchase.attest,
            settled=purchase.settled,
            contract=purchase.contract.key_clauses() if purchase.contract is not None else None,
        )

    async def start(request: Request) -> JSONResponse:
        if not authorized(request.headers.get("authorization"), secret):
            return deny()
        try:
            body = await request.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return JSONResponse({"detail": "send a JSON object"}, status_code=422)
        deal_id = body.get("deal_id")
        seller_url = str(body.get("seller_url") or "").rstrip("/")
        if not isinstance(deal_id, str) or not deal_id or len(deal_id) > MAX_DEAL_ID or not deal_id.isascii():
            return JSONResponse({"detail": "deal_id is required"}, status_code=422)
        if seller_url not in allowed:
            return JSONResponse({"detail": "that seller is not allowed"}, status_code=403)
        if runs.start(deal_id) is None:
            return JSONResponse({"detail": "a purchase is already running"}, status_code=409)
        launch = spawn or (lambda fn: threading.Thread(target=fn, name=f"purchase-{deal_id}", daemon=True).start())
        launch(lambda: work(deal_id, seller_url))
        return JSONResponse({"deal_id": deal_id, "state": "running"}, status_code=202)

    async def status(deal_id: str, request: Request) -> JSONResponse:
        if not authorized(request.headers.get("authorization"), secret):
            return deny()
        view = runs.view(deal_id)
        if view is None:
            return JSONResponse({"detail": "unknown purchase"}, status_code=404)
        return JSONResponse(view)

    agent.app.add_api_route("/operator/purchases", start, methods=["POST"], include_in_schema=False)
    agent.app.add_api_route("/operator/purchases/{deal_id}", status, methods=["GET"], include_in_schema=False)
