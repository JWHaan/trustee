"""Arena service — FastAPI + SSE.

The one non-obvious thing here is the threading model. The agents are
synchronous (blocking httpx, blocking OpenAI client, blocking `time.sleep` in
the PayPal retry loop), so a run executes in a worker thread via
`asyncio.to_thread`. Ledger callbacks therefore fire on *that* thread, and
every mutation of a run record is marshalled back onto the event loop with
`call_soon_threadsafe`. Doing it the other way round — an `asyncio.Queue`
written from the worker thread directly — is the classic way to get a
half-delivered stream under load, and it fails exactly when a judge clicks.

SSE is replay-first: a subscriber's queue is pre-filled with everything the
run has already emitted, then fed live. Refresh the page mid-run and you get
the same tape, in the same order, without a second code path.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import os
import threading
import uuid
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..a2a import A2ABinding
from ..alerts import AlertSink, arena_alerts_enabled, sink_from_env
from ..ledger import Ledger
from ..network import create_northwind, create_tidewater, mount_agents
from ..paypal import PayPalClient
from ..platform import DealService, build_router
from ..platform.service import operator_token_from_env
from ..store import Store
from .rows import to_row
from .runner import MODES, DealRunner, RunResult
from .scenario import get_scenario, load_scenarios

log = logging.getLogger(__name__)

REGISTRY_CAP = 50
DEFAULT_SWEEP_SECONDS = 30.0


# --------------------------------------------------------------------- records


class RunRecord:
    """One run's tape plus its live subscribers. Mutated on the event loop only."""

    def __init__(self, run_id: str, scenario: str, mode: str):
        self.run_id = run_id
        self.scenario = scenario
        self.mode = mode
        self.rows: list[dict[str, Any]] = []
        self.queues: list[asyncio.Queue[dict[str, Any] | None]] = []
        self.done = False
        self.result: RunResult | None = None
        self.error: str | None = None

    # -- loop-thread API ----------------------------------------------------
    def push(self, row: dict[str, Any]) -> None:
        if self.done:
            return
        self.rows.append(row)
        for q in self.queues:
            q.put_nowait(row)

    def finish(self, result: RunResult) -> None:
        self.result = result
        self.error = result.error
        self._close()

    def fail(self, message: str) -> None:
        self.error = message
        self._close()

    def _close(self) -> None:
        self.done = True
        for q in self.queues:
            q.put_nowait(None)

    def subscribe(self) -> asyncio.Queue[dict[str, Any] | None]:
        q: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        for row in self.rows:  # replay
            q.put_nowait(row)
        if self.done:
            q.put_nowait(None)
        else:
            self.queues.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any] | None]) -> None:
        if q in self.queues:
            self.queues.remove(q)

    def snapshot(self) -> dict[str, Any]:
        base: dict[str, Any] = (
            self.result.snapshot()
            if self.result is not None
            else {
                "run_id": self.run_id,
                "deal_id": None,
                "scenario": self.scenario,
                "mode": self.mode,
                "effective_mode": None,
                "state": "FAILED" if self.error else "RUNNING",
                "price": None,
                "turns": 0,
                "contract": None,
                "escrow": None,
                "ruling": None,
                "attest": None,
                "error": self.error,
                "fallback_from": None,
                "outcome": None,
                "receipt": None,
                "receipt_verified": None,
                "human_present": None,
            }
        )
        base["events"] = len(self.rows)
        if self.error and not base.get("error"):
            base["error"] = self.error
        return base


class Registry:
    def __init__(self, cap: int = REGISTRY_CAP):
        self._runs: OrderedDict[str, RunRecord] = OrderedDict()
        self._cap = cap

    def put(self, rec: RunRecord) -> None:
        self._runs[rec.run_id] = rec
        while len(self._runs) > self._cap:
            self._runs.popitem(last=False)

    def get(self, run_id: str) -> RunRecord | None:
        return self._runs.get(run_id)

    def require(self, run_id: str) -> RunRecord:
        rec = self._runs.get(run_id)
        if rec is None:
            raise HTTPException(status_code=404, detail=f"no such run: {run_id}")
        return rec

    def rows(self, limit: int) -> list[dict[str, Any]]:
        flat = [row for rec in self._runs.values() for row in rec.rows]
        return flat[-limit:]

    def __len__(self) -> int:
        return len(self._runs)


class ApprovalRegistry:
    """PayPal order ids that belong to arena runs, so the return/cancel redirects can find them.

    `track` is called by a run waiting for its buyer; `release` when the wait ends. A released
    order stays known (bounded), so a late redirect from PayPal still lands on the arena page."""

    def __init__(self, cap: int = 200):
        self._orders: OrderedDict[str, threading.Event] = OrderedDict()
        self._cap = cap
        self._lock = threading.Lock()

    def track(self, order_id: str) -> threading.Event:
        with self._lock:
            event = self._orders.setdefault(order_id, threading.Event())
            while len(self._orders) > self._cap:
                self._orders.popitem(last=False)
            return event

    def release(self, order_id: str) -> None:  # kept on purpose, see class docstring
        return None

    def owns(self, order_id: str) -> bool:
        with self._lock:
            return order_id in self._orders

    def cancel(self, order_id: str) -> None:
        with self._lock:
            event = self._orders.get(order_id)
        if event is not None:
            event.set()


def _default_self_client() -> httpx.Client:
    base = os.environ.get("ARENA_SELF_URL") or f"http://127.0.0.1:{os.environ.get('PORT', '8000')}"
    return httpx.Client(base_url=base, timeout=30.0)


# ---------------------------------------------------------------------- models


class RunRequest(BaseModel):
    scenario: str = "honest"
    mode: Literal["auto", "live", "scripted"] = "auto"


# ------------------------------------------------------------------- execution


def _execute(runner: DealRunner, rec: RunRecord, mode: str, loop: asyncio.AbstractEventLoop) -> None:
    """Runs in a worker thread. Everything that touches `rec` hops back to the loop."""
    seq = itertools.count()

    def on_event(ev: Any) -> None:
        loop.call_soon_threadsafe(rec.push, to_row(ev, next(seq)))

    try:
        result = runner.run(get_scenario(rec.scenario), mode=mode, on_event=on_event, run_id=rec.run_id)
    except Exception as exc:  # noqa: BLE001 - a run failure is data, not a 500
        log.exception("run %s crashed", rec.run_id)
        loop.call_soon_threadsafe(rec.fail, f"{type(exc).__name__}: {exc}")
        return
    loop.call_soon_threadsafe(rec.finish, result)


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


def _ui_dir() -> Path | None:
    override = os.environ.get("ARENA_UI_DIR")
    if override:
        return Path(override)
    # src/trustee/arena/api.py -> repo root
    candidate = Path(__file__).resolve().parents[3] / "arena" / "ui" / "dist"
    return candidate if candidate.exists() else None


# ------------------------------------------------------------------------- app


def _sweep_seconds() -> float:
    try:
        return max(0.05, float(os.environ.get("TRUSTEE_SWEEP_SECONDS", DEFAULT_SWEEP_SECONDS)))
    except ValueError:
        return DEFAULT_SWEEP_SECONDS


async def _sweep_loop(service: DealService, stop: asyncio.Event, interval: float) -> None:
    """Tick the deadline sweeper until `stop` is set. An error is logged, never fatal."""
    while not stop.is_set():
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval)
        if stop.is_set():
            return
        try:
            await asyncio.to_thread(service.sweep)
        except Exception:  # noqa: BLE001 - the sweep must outlive one bad tick
            log.exception("deadline sweep failed")


def create_app(
    ledger: Ledger | None = None,
    paypal: PayPalClient | None = None,
    ui_dir: Path | None = None,
    service: DealService | None = None,
    self_client: Callable[[], httpx.Client] | None = None,
) -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI) -> Any:
        stop = asyncio.Event()
        task = asyncio.create_task(_sweep_loop(app.state.service, stop, _sweep_seconds()))
        try:
            yield
        finally:
            stop.set()
            await task
            for sink in app.state.alert_sinks:
                sink.close()
            app.state.a2a.close()

    app = FastAPI(title="Trustee Arena", version="0.1.0", docs_url="/api/docs", redoc_url=None, lifespan=lifespan)

    ledger = ledger if ledger is not None else Ledger(path=os.environ.get("TRUSTEE_LEDGER_PATH") or None)
    # With a public URL the payer's browser can come back to us; explicit PAYPAL_*_URL still wins.
    public = (os.environ.get("TRUSTEE_PUBLIC_URL") or "").rstrip("/")
    return_url = os.environ.get("PAYPAL_RETURN_URL") or (f"{public}/v1/paypal/return" if public else "")
    cancel_url = os.environ.get("PAYPAL_CANCEL_URL") or (f"{public}/v1/paypal/cancel" if public else "")
    paypal = (
        paypal
        if paypal is not None
        else PayPalClient(
            client_id=os.environ.get("PAYPAL_CLIENT_ID", ""),
            client_secret=os.environ.get("PAYPAL_CLIENT_SECRET", ""),
            env=os.environ.get("PAYPAL_ENV", "sandbox"),
            return_url=return_url or "https://example.com/paypal/return",
            cancel_url=cancel_url or "https://example.com/paypal/cancel",
        )
    )
    if service is None:
        # The platform keeps its own ledger so arena replays never land in the durable store.
        service = DealService(
            Store(os.environ.get("TRUSTEE_DB_PATH") or ":memory:"),
            paypal,
            Ledger(),
            operator_token=operator_token_from_env(),
        )
    approvals = ApprovalRegistry()
    # The two A2A agents of the `a2a` scenario, with fresh keys. Each has its own Agent Card and /a2a endpoint,
    # mounted below next to Trustee's; the scenario borrows them (see arena/a2a_flow.py).
    northwind = create_northwind()
    tidewater = create_tidewater()
    runner = DealRunner(
        ledger=ledger,
        paypal=paypal,
        # The `outside` scenario calls this app's own /v1 API over HTTP; tests inject a TestClient.
        self_client=self_client if self_client is not None else _default_self_client,
        close_self_client=self_client is None,
        platform_ledger=service.ledger,
        operator_token=service.operator_token,
        approvals=approvals,
        network=(northwind, tidewater),
        # Receipts of in-process runs verify against the same published key as the platform's.
        signing_key=service.signing_key,
        demo_funding=service.funding.register_demo,
    )
    # Human alerts derive from ledger events (money path untouched). Unset URL => no sink, no subscriber.
    alert_sinks: list[AlertSink] = []
    platform_sink = sink_from_env("platform", public)
    if platform_sink is not None:
        platform_sink.attach(service.ledger)
        alert_sinks.append(platform_sink)
        if arena_alerts_enabled():  # arena runs are demos: opt in, or every judge click would page Slack
            arena_sink = sink_from_env("arena", public)
            if arena_sink is not None:
                arena_sink.attach(ledger)
                alert_sinks.append(arena_sink)
    registry = Registry()
    tasks: set[asyncio.Task[None]] = set()

    app.state.ledger = ledger
    app.state.paypal = paypal
    app.state.runner = runner
    app.state.registry = registry
    app.state.service = service
    app.state.approvals = approvals
    app.state.alert_sinks = alert_sinks
    app.include_router(build_router(service, approvals))
    # A2A JSON-RPC (POST /a2a) over the same service; its push notifier follows the platform ledger.
    a2a = A2ABinding(service)
    app.state.a2a = a2a
    app.include_router(a2a.router)
    app.state.northwind = northwind
    app.state.tidewater = tidewater
    mount_agents(app, northwind, tidewater)  # before the UI's catch-all mount at "/"

    # ---------------------------------------------------------------- routes

    @app.post("/api/runs")
    async def create_run(req: RunRequest) -> dict[str, Any]:
        try:
            scenario = get_scenario(req.scenario)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        rec = RunRecord(f"run_{uuid.uuid4().hex[:10]}", scenario.name, req.mode)
        registry.put(rec)
        loop = asyncio.get_running_loop()
        task = asyncio.create_task(asyncio.to_thread(_execute, runner, rec, req.mode, loop))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return {"run_id": rec.run_id, "scenario": scenario.name, "mode": req.mode}

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str) -> dict[str, Any]:
        return registry.require(run_id).snapshot()

    @app.get("/api/runs/{run_id}/receipt")
    async def get_run_receipt(run_id: str) -> dict[str, Any]:
        rec = registry.require(run_id)
        if rec.result is None or rec.result.receipt is None:
            raise HTTPException(status_code=404, detail="this run has no settlement receipt (yet)")
        return rec.result.receipt

    @app.get("/api/runs/{run_id}/events")
    async def run_events(run_id: str) -> StreamingResponse:
        rec = registry.require(run_id)
        queue = rec.subscribe()

        async def stream() -> Any:
            try:
                while True:
                    try:
                        item = await asyncio.wait_for(queue.get(), timeout=10.0)
                    except TimeoutError:
                        yield ": keepalive\n\n"  # keeps proxies from closing an idle stream
                        continue
                    if item is None:
                        yield _sse("done", rec.snapshot())
                        return
                    yield _sse("msg", item)
            finally:
                rec.unsubscribe(queue)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/ledger")
    async def get_ledger(limit: int = 500) -> dict[str, Any]:
        limit = max(1, min(limit, 5000))
        rows = registry.rows(limit)
        return {"rows": rows, "count": len(rows)}

    @app.get("/api/scenarios")
    async def get_scenarios() -> dict[str, Any]:
        return {
            "scenarios": [
                {
                    "name": s.name,
                    "title": s.title,
                    "blurb": s.blurb,
                    "ask": str(s.ask),
                    "floor": str(s.floor),
                    "budget": str(s.budget),
                    "opening": str(s.opening_price),
                    "cheat": s.cheat,
                    "attack": s.attack,
                    "flow": s.flow,
                    "instruction": s.instruction,
                }
                # `partial` is listed (and so shown in the UI) only with ARENA_PARTIAL_SCENARIO=1.
                for s in load_scenarios().values()
                if s.partial is None or os.environ.get("ARENA_PARTIAL_SCENARIO") == "1"
            ],
            "modes": list(MODES),
        }

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {
            "ok": True,
            "runs": len(registry),
            "paypal": "dry-run" if paypal.dry_run else "sandbox",
            "ledger": str(ledger.path) if ledger.path else "in-memory",
            "alerts": "webhook" if alert_sinks else "off",
            "store": service.store.path if service.store.path != ":memory:" else "in-memory",
        }

    # ------------------------------------------------------------------- UI

    ui = ui_dir if ui_dir is not None else _ui_dir()
    if ui is not None and (ui / "index.html").exists():
        app.mount("/", StaticFiles(directory=str(ui), html=True), name="ui")
    else:

        @app.get("/", response_class=HTMLResponse)
        async def no_ui() -> str:
            return (
                "<body style='background:#0a0b0d;color:#e6e6e6;font:15px/1.6 ui-monospace,monospace;padding:48px'>"
                "<h1>Trustee Arena</h1><p>The UI bundle is not built.</p>"
                "<pre style='color:#f5b544'>cd arena/ui &amp;&amp; npm install &amp;&amp; npm run build</pre>"
                "<p>API is live: <a style='color:#6ea8fe' href='/api/docs'>/api/docs</a></p></body>"
            )

    return app


app = create_app()
