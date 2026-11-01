"""Northwind and Tidewater book settled deals in their own apps, through Zapier MCP.

After Trustee settles or voids a deal, the business's own agent records the result in its own tools: Tidewater
writes the invoice and updates the CRM deal; Northwind records the bill and posts a finance note. When Northwind's
intent mandate steps up to the human, Northwind asks Zapier to message the principal with the approval link. The
message only informs. Approval still needs the principal's signature through Trustee.

The agents call Zapier MCP themselves. Trustee never holds those app credentials, and no LLM picks a tool or an
argument.

Rules, all deliberate:

* Opt-in. Without `<AGENT>_ZAPIER_MCP_TOKEN` (plus a mapped tool) nothing is created. Without the `zapier` extra
  (the `mcp` package) the feature stays off and logs one line.
* Allowlist. Only actions in `<AGENT>_ZAPIER_TOOLS` run, and each agent can use only its own actions. A tool whose
  name suggests money movement (pay, refund, transfer, charge, capture, ...) is refused even when configured.
* Arguments come from code, built from the signed settlement receipt or the contract. Nothing else.
* One booking per (agent, deal, action). A repeated push does not book twice.
* Delivery runs on one daemon thread with a bounded queue, a timeout and retries. Failures are logged and recorded;
  they never change a deal.
* The token is never logged, and booking arguments are never logged.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import os
import queue
import re
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

DEFAULT_URL = "https://mcp.zapier.com/api/v1/connect"
_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_TOOL_NAME = re.compile(r"[A-Za-z0-9_.\-]{1,128}")
_SEEN_CAP = 5000
_RESULTS_CAP = 200

ACTIONS_BY_AGENT: dict[str, frozenset[str]] = {
    "tidewater": frozenset({"record_invoice", "update_crm"}),
    "northwind": frozenset({"record_bill", "post_finance_note", "notify_principal"}),
}
MONEY_TOKENS = frozenset(
    {
        "pay",
        "pays",
        "paypal",
        "payment",
        "payments",
        "payout",
        "payouts",
        "refund",
        "refunds",
        "transfer",
        "transfers",
        "charge",
        "charges",
        "capture",
        "captures",
        "withdraw",
        "withdrawal",
    }
)


class BackofficeConfigError(ValueError):
    """The back-office configuration is unusable. The feature stays off."""


class ToolError(Exception):
    """Zapier answered a tool call with an error. The text is not kept, because it may echo business data."""


class ToolSession(Protocol):
    """What the back office needs from an MCP connection. Tests supply a fake; production uses `ZapierMcpSession`."""

    def list_tools(self) -> list[str]: ...

    def call_tool(self, name: str, arguments: dict[str, Any]) -> None: ...


def is_money_tool(name: str) -> bool:
    """True when a tool name contains a word that means money moves (matched as whole words, so "display" is fine)."""
    words = [w for w in re.split(r"[^a-z0-9]+", name.lower()) if w]
    return any(w in MONEY_TOKENS for w in words)


def validate_url(url: str) -> str:
    parts = urlsplit(url.strip())
    if parts.scheme == "https" and parts.hostname:
        return url.strip()
    if parts.scheme == "http" and parts.hostname in _LOCAL_HOSTS:
        return url.strip()
    raise BackofficeConfigError("the Zapier MCP URL must be https:// (http:// only for localhost)")


def parse_tool_map(raw: str | None, agent: str) -> dict[str, str]:
    """Parse `<AGENT>_ZAPIER_TOOLS` into {action: tool name}. Unknown actions and money tools are dropped and logged."""
    if raw is None or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        raise BackofficeConfigError(f"{agent.upper()}_ZAPIER_TOOLS must be a JSON object") from None
    if not isinstance(data, dict):
        raise BackofficeConfigError(f"{agent.upper()}_ZAPIER_TOOLS must be a JSON object")
    allowed = ACTIONS_BY_AGENT[agent]
    out: dict[str, str] = {}
    for action, tool in data.items():
        if action not in allowed:
            log.warning("zapier back office: ignoring action %r, which %s does not use", action, agent)
            continue
        if not isinstance(tool, str) or not _TOOL_NAME.fullmatch(tool):
            log.error("zapier back office: the tool for %s is not a valid tool name; action off", action)
            continue
        if is_money_tool(tool):
            log.error("zapier back office: refusing tool %s for %s; its name suggests money movement", tool, action)
            continue
        out[action] = tool
    return out


def actions_for_receipt(agent: str, outcome: str) -> tuple[str, ...]:
    """Which actions a settlement means for one agent. Tidewater books a sale or closes a lost deal; Northwind books
    the bill and a note either way."""
    if agent == "tidewater":
        return ("record_invoice", "update_crm") if outcome == "settled" else ("update_crm",)
    return ("record_bill", "post_finance_note")


# ------------------------------------------------------------------------------- facts and arguments


@dataclass(frozen=True)
class Facts:
    """What a booking says about one deal. Built from a signed receipt or a step-up, never from model output."""

    deal_id: str
    counterparty: str
    amount: str
    currency: str
    outcome: str  # "settled" | "voided" | "step_up"
    capture_id: str | None = None
    delivery_sha256: str | None = None
    approve_url: str | None = None


def facts_from_receipt(receipt: Mapping[str, Any], counterparty: str) -> Facts:
    """Build facts from a Trustee settlement receipt. A partial settlement books the captured amount."""
    outcome = receipt.get("outcome")
    if outcome not in ("settled", "voided"):
        raise ValueError(f"receipt outcome {outcome!r} is not a settlement")
    deal_id = str(receipt.get("deal_id") or "")
    amount = str(receipt.get("amount") or "")
    currency = str(receipt.get("currency") or "")
    if not deal_id or not amount or not currency:
        raise ValueError("receipt lacks deal_id, amount or currency")
    paypal = receipt.get("paypal") or {}
    capture = paypal.get("capture_id") if isinstance(paypal, Mapping) else None
    delivery = receipt.get("delivery_sha256")
    return Facts(
        deal_id=deal_id,
        counterparty=counterparty,
        amount=amount,
        currency=currency,
        outcome=str(outcome),
        capture_id=str(capture) if capture else None,
        delivery_sha256=str(delivery) if delivery else None,
    )


def build_arguments(action: str, facts: Facts) -> dict[str, Any]:
    """The arguments for one action. Fixed templates plus ids and amounts only: no notes, bodies or personal data."""
    settled = facts.outcome == "settled"
    capture = facts.capture_id or "none"
    if action == "record_invoice":
        return {
            "deal_id": facts.deal_id,
            "customer": facts.counterparty,
            "amount": facts.amount,
            "currency": facts.currency,
            "memo": f"Trustee deal {facts.deal_id}; PayPal capture {capture}",
        }
    if action == "update_crm":
        return {
            "deal_id": facts.deal_id,
            "account": facts.counterparty,
            "stage": "closed_won" if settled else "closed_lost",
            "amount": facts.amount,
            "currency": facts.currency,
        }
    if action == "record_bill":
        return {
            "deal_id": facts.deal_id,
            "vendor": facts.counterparty,
            "amount": facts.amount,
            "currency": facts.currency,
            "status": "paid" if settled else "voided",
            "paypal_capture_id": capture,
        }
    if action == "post_finance_note":
        verdict = "paid and delivered" if settled else "voided, funds returned"
        return {
            "deal_id": facts.deal_id,
            "text": (
                f"Trustee deal {facts.deal_id} with {facts.counterparty}: {verdict}. "
                f"{facts.amount} {facts.currency}. PayPal capture {capture}."
            ),
        }
    if action == "notify_principal":
        return {
            "deal_id": facts.deal_id,
            "counterparty": facts.counterparty,
            "amount": facts.amount,
            "currency": facts.currency,
            "approve_url": facts.approve_url or "",
            "text": (
                f"Your purchasing agent wants to pay {facts.amount} {facts.currency} to {facts.counterparty}, "
                f"which is above what you pre-approved. Approve or decline here: {facts.approve_url or 'see Trustee'}"
            ),
        }
    raise ValueError(f"unknown action {action!r}")


@dataclass(frozen=True)
class BookingResult:
    agent: str
    deal_id: str
    action: str
    tool: str
    ok: bool
    error: str | None
    at: str


# ------------------------------------------------------------------------------- the back office


class Backoffice:
    """One agent's booking queue. Construct with `from_env`; a disabled instance accepts calls and does nothing."""

    def __init__(
        self,
        agent: str,
        *,
        session: ToolSession | None = None,
        tools: Mapping[str, str] | None = None,
        on_result: Callable[[BookingResult], None] | None = None,
        max_queue: int = 100,
        retries: int = 2,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if agent not in ACTIONS_BY_AGENT:
            raise ValueError(f"unknown agent {agent!r}")
        self.agent = agent
        self._session = session
        # Second guard for maps built in code (`from_env` already filters): a money-moving tool never runs.
        allowed = ACTIONS_BY_AGENT[agent]
        self._tools = {a: t for a, t in dict(tools or {}).items() if a in allowed and not is_money_tool(t)}
        self._on_result = on_result
        self._retries = max(0, retries)
        self._sleep = sleep
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._seen: OrderedDict[tuple[str, str, str], None] = OrderedDict()
        self._pending = 0
        self.results: deque[BookingResult] = deque(maxlen=_RESULTS_CAP)
        self._queue: _BoundedQueue | None = None
        if self.enabled:
            self._queue = _BoundedQueue(max(1, max_queue))
            threading.Thread(target=self._run, name=f"zapier-backoffice-{agent}", daemon=True).start()

    @property
    def enabled(self) -> bool:
        return self._session is not None and bool(self._tools)

    @property
    def actions(self) -> frozenset[str]:
        return frozenset(self._tools)

    @classmethod
    def from_env(
        cls,
        agent: str,
        env: Mapping[str, str] | None = None,
        on_result: Callable[[BookingResult], None] | None = None,
    ) -> Backoffice:
        env = os.environ if env is None else env
        prefix = agent.upper()
        token = (env.get(f"{prefix}_ZAPIER_MCP_TOKEN") or "").strip()
        if not token:
            return cls(agent, on_result=on_result)
        try:
            tools = parse_tool_map(env.get(f"{prefix}_ZAPIER_TOOLS"), agent)
            url = validate_url(env.get("ZAPIER_MCP_URL") or DEFAULT_URL)
        except BackofficeConfigError as exc:
            log.error("zapier back office off for %s: %s", agent, exc)
            return cls(agent, on_result=on_result)
        if not tools:
            log.error("zapier back office off for %s: %s_ZAPIER_TOOLS maps no usable action", agent, prefix)
            return cls(agent, on_result=on_result)
        try:
            session: ToolSession = ZapierMcpSession(url, token)
        except ImportError:
            log.error("zapier back office off for %s: the zapier extra is not installed", agent)
            return cls(agent, on_result=on_result)
        return cls(agent, session=session, tools=tools, on_result=on_result)

    # -- public calls (never block, never raise)

    def book(self, action: str, facts: Facts) -> bool:
        """Queue one booking. False when it is off, not mapped, a duplicate, or the queue is full."""
        if not self.enabled or action not in self._tools:
            return False
        key = (self.agent, facts.deal_id, action)
        with self._lock:
            if key in self._seen:
                return False
            self._seen[key] = None
            while len(self._seen) > _SEEN_CAP:
                self._seen.popitem(last=False)
        try:
            args = build_arguments(action, facts)
        except ValueError:
            log.error("zapier back office: cannot build %s", action)
            return self._forget(key)
        if self._queue is None or not self._queue.put((action, facts.deal_id, args)):
            log.warning("zapier back office queue full; dropped %s for deal %s", action, facts.deal_id)
            return self._forget(key)
        with self._lock:
            self._pending += 1
        return True

    def book_receipt(self, receipt: Mapping[str, Any], counterparty: str) -> list[str]:
        """Book what a settlement receipt means for this agent. Returns the actions queued."""
        if not self.enabled:
            return []
        try:
            facts = facts_from_receipt(receipt, counterparty)
        except ValueError as exc:
            log.warning("zapier back office: receipt not bookable (%s)", exc)
            return []
        return [a for a in actions_for_receipt(self.agent, facts.outcome) if self.book(a, facts)]

    def notify_step_up(self, facts: Facts) -> bool:
        """Tell the principal that a purchase needs their approval. The approval itself stays in Trustee."""
        return self.book("notify_principal", replace(facts, outcome="step_up"))

    def flush(self, timeout: float = 5.0) -> bool:
        """Wait until queued bookings have finished. For tests and shutdown."""
        deadline = time.monotonic() + timeout
        with self._cv:
            while self._pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cv.wait(remaining)
        return True

    def close(self) -> None:
        if self._queue is not None:
            self._queue.close()

    # -- worker

    def _forget(self, key: tuple[str, str, str]) -> bool:
        with self._lock:
            self._seen.pop(key, None)
        return False

    def _run(self) -> None:
        assert self._queue is not None
        while True:
            item = self._queue.get()
            if item is None:
                return
            try:
                self._execute(*item)
            except Exception:  # noqa: BLE001 - a booking must never take the worker down
                log.exception("zapier back office worker error")
            finally:
                with self._cv:
                    self._pending -= 1
                    self._cv.notify_all()

    def _execute(self, action: str, deal_id: str, args: dict[str, Any]) -> None:
        tool = self._tools[action]
        attempts = self._retries + 1
        error: str | None = None
        for attempt in range(attempts):
            try:
                assert self._session is not None
                self._session.call_tool(tool, args)
            except Exception as exc:  # noqa: BLE001 - recorded, never raised into the deal
                error = type(exc).__name__
                log.warning(
                    "zapier %s for deal %s failed (attempt %d/%d): %s", action, deal_id, attempt + 1, attempts, error
                )
                if attempt + 1 < attempts:
                    self._sleep(min(2.0**attempt, 10.0))
                continue
            self._record(action, deal_id, tool, ok=True, error=None)
            return
        self._record(action, deal_id, tool, ok=False, error=error or "unknown")

    def _record(self, action: str, deal_id: str, tool: str, *, ok: bool, error: str | None) -> None:
        result = BookingResult(
            agent=self.agent,
            deal_id=deal_id,
            action=action,
            tool=tool,
            ok=ok,
            error=error,
            at=datetime.now(UTC).isoformat(timespec="seconds"),
        )
        self.results.append(result)
        if self._on_result is not None:
            try:
                self._on_result(result)
            except Exception:  # noqa: BLE001
                log.exception("zapier back office result observer failed")


class _BoundedQueue:
    """A FIFO with a hard cap. `put` never blocks: a full queue refuses the item."""

    def __init__(self, maxsize: int):
        self._q: queue.Queue[tuple[str, str, dict[str, Any]] | None] = queue.Queue(maxsize=maxsize)

    def put(self, item: tuple[str, str, dict[str, Any]]) -> bool:
        try:
            self._q.put_nowait(item)
        except queue.Full:
            return False
        return True

    def get(self) -> tuple[str, str, dict[str, Any]] | None:
        return self._q.get()

    def close(self) -> None:
        try:
            self._q.put_nowait(None)
        except queue.Full:
            pass


# ------------------------------------------------------------------------------- Zapier MCP transport


class ZapierMcpSession:
    """Streamable HTTP MCP client for one Zapier MCP server, authenticated with a connection token.

    Each call opens a short connection, makes its request and closes it. Calls are rare (a few per deal), so this
    keeps the state simple. The official `mcp` package is imported lazily, so this class only fails at construction
    when the `zapier` extra is missing.
    """

    def __init__(self, url: str, token: str, *, timeout: float = 30.0):
        mcp = importlib.import_module("mcp")
        client_mod = importlib.import_module("mcp.client.streamable_http")
        self._client_session = mcp.ClientSession
        self._streamable = client_mod.streamablehttp_client
        self._url = url
        self._headers = {"Authorization": f"Bearer {token}"}
        self._timeout = timeout

    async def _with_session(self, fn: Callable[[Any], Any]) -> Any:
        async with self._streamable(self._url, headers=self._headers) as (read, write, _):
            async with self._client_session(read, write) as session:
                await session.initialize()
                return await fn(session)

    def _run(self, fn: Callable[[Any], Any]) -> Any:
        async def bounded() -> Any:
            return await asyncio.wait_for(self._with_session(fn), self._timeout)

        return asyncio.run(bounded())

    def list_tools(self) -> list[str]:
        async def fn(session: Any) -> list[str]:
            result = await session.list_tools()
            return [str(t.name) for t in result.tools]

        names: list[str] = self._run(fn)
        return names

    def call_tool(self, name: str, arguments: dict[str, Any]) -> None:
        async def fn(session: Any) -> None:
            result = await session.call_tool(name, arguments)
            if getattr(result, "isError", False):
                raise ToolError("Zapier reported an error for the call")

        self._run(fn)
