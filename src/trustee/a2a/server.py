"""A2A JSON-RPC server: `POST /a2a` in front of `DealService`.

Everything here is protocol translation. A `SendMessage` becomes a `DealService` call through a skill (see
`skills.py`), and every task is a view of the durable deal (see `tasks.py`). No money-moving code lives in
this module, and nothing it receives is trusted beyond the signatures the service already verifies.

How errors are split between JSON-RPC errors and tasks:

* A malformed request (bad JSON, unknown method, params that do not fit, an unknown skill, a `taskId` that
  does not exist, a message to a finished task, a mismatched `contextId`) is a JSON-RPC error with the
  spec's code. These are mistakes in the message, not answers about money.
* A well-formed message the service refuses (bad signature 401, mandate refused 403, conflict 409, bad
  delivery 422) is a *task*. If no deal exists (a refused contract is never stored) it is a one-off task in
  TASK_STATE_REJECTED. If the deal exists, the response is the deal's own task in its true state, with the
  refusal in `status.message` and `metadata.trustee.lastRequest`: a refused delivery must not make the
  whole deal look dead. A PayPal failure (502) is the same, plus `retryable`.
* One-off tasks (registration results, refusals) are kept in a small in-memory table so `GetTask` can
  fetch them for a while. Deal tasks are not stored at all: they are rebuilt from the deal.

Blocking work (SQLite, PayPal) runs in `asyncio.to_thread`. All JSON-RPC responses use HTTP 200.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import re
import threading
from collections import OrderedDict
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

from ..platform.service import DealService, ServiceError
from . import legacy
from .models import (
    TERMINAL_STATES,
    A2AError,
    CancelTaskRequest,
    GetTaskRequest,
    ListPushConfigsRequest,
    ListTasksRequest,
    PushConfigRef,
    SendMessageRequest,
    Task,
    TaskPushNotificationConfig,
    TaskState,
    content_type_not_supported,
    internal_error,
    invalid_params,
    invalid_params_from,
    invalid_request,
    method_not_found,
    new_id,
    parse_error,
    push_not_supported,
    task_not_cancelable,
    task_not_found,
    unauthorized,
    unsupported_operation,
    version_not_supported,
)
from .push import PushConfigError, PushNotifier
from .skills import Prepared, execute_skill, help_message, prepare_skill
from .tasks import a2a_state, annotate_refusal, deal_task, ephemeral_task

log = logging.getLogger(__name__)

MAX_BODY_BYTES = 1 << 20
MAX_EPHEMERAL = 1000
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100
_VERSION = re.compile(r"^(1\.0|0\.3)(\.\d+)?$")

METHODS = (
    "SendMessage",
    "SendStreamingMessage",
    "GetTask",
    "ListTasks",
    "CancelTask",
    "SubscribeToTask",
    "CreateTaskPushNotificationConfig",
    "GetTaskPushNotificationConfig",
    "ListTaskPushNotificationConfigs",
    "DeleteTaskPushNotificationConfig",
    "GetExtendedAgentCard",
)


class _Ephemeral:
    """Bounded in-memory table of one-off tasks (registration results and refusals)."""

    def __init__(self, cap: int = MAX_EPHEMERAL):
        self._tasks: OrderedDict[str, Task] = OrderedDict()
        self._cap = cap
        self._lock = threading.Lock()

    def put(self, task: Task) -> Task:
        with self._lock:
            self._tasks[task.id] = task
            while len(self._tasks) > self._cap:
                self._tasks.popitem(last=False)
        return task

    def get(self, task_id: str) -> Task | None:
        with self._lock:
            return self._tasks.get(task_id)


class A2AServer:
    def __init__(self, service: DealService, push: PushNotifier | None = None):
        self.service = service
        self.push = push
        self._ephemeral = _Ephemeral()
        self._handlers: dict[str, Callable[[Any, str | None, str], Any]] = {
            "SendMessage": self._send_message,
            "GetTask": self._get_task,
            "ListTasks": self._list_tasks,
            "CancelTask": self._cancel_task,
            "CreateTaskPushNotificationConfig": self._push_create,
            "GetTaskPushNotificationConfig": self._push_get,
            "ListTaskPushNotificationConfigs": self._push_list,
            "DeleteTaskPushNotificationConfig": self._push_delete,
        }

    # ------------------------------------------------------------------ dispatch

    def dispatch(self, method: str, params: Any, authorization: str | None, version: str | None) -> Any:
        """Run one JSON-RPC method. Blocking. Raises A2AError for protocol errors."""
        canonical = legacy.METHOD_ALIASES.get(method, method)
        dialect = "v03" if method in legacy.METHOD_ALIASES else "v1"
        if canonical not in METHODS:
            raise method_not_found(method)
        if version is not None and not _VERSION.match(version.strip()):
            raise version_not_supported(version)
        if canonical in ("SendStreamingMessage", "SubscribeToTask"):
            raise unsupported_operation(
                "streaming is not supported (capabilities.streaming is false); use GetTask or push"
            )
        if canonical == "GetExtendedAgentCard":
            raise unsupported_operation(
                "there is no authenticated extended Agent Card (capabilities.extendedAgentCard)"
            )
        if dialect == "v03":
            params = legacy.request_in(canonical, params)
        result = self._handlers[canonical](params, authorization, dialect)
        return legacy.result_out(canonical, result) if dialect == "v03" else result

    @staticmethod
    def _parse[M: BaseModel](model: type[M], params: Any) -> M:
        if not isinstance(params, dict):
            raise invalid_params("params must be an object")
        try:
            return model.model_validate(params)
        except ValidationError as exc:
            raise invalid_params_from(exc) from None

    # ------------------------------------------------------------------ tasks

    def _deal_record(self, task_id: str) -> dict[str, Any] | None:
        try:
            return self.service.record(task_id)
        except ServiceError:
            return None

    def build_task_dict(self, deal_id: str) -> dict[str, Any] | None:
        """Current Task for a deal as a dict (the push worker's builder)."""
        record = self._deal_record(deal_id)
        return None if record is None else deal_task(record).dump()

    def _require_task(self, task_id: str) -> Task:
        task = self._ephemeral.get(task_id)
        if task is not None:
            return task
        record = self._deal_record(task_id)
        if record is None:
            raise task_not_found(task_id)
        return deal_task(record)

    def _get_task(self, params: Any, auth: str | None, dialect: str) -> dict[str, Any]:
        req = self._parse(GetTaskRequest, params)
        return self._require_task(req.id).dump()

    # ---------------------------------------------------------------- SendMessage

    @staticmethod
    def _extract_skill(req: SendMessageRequest) -> tuple[str, Any] | None:
        """(skill id, input) from the message, or None for a message with no skill invocation."""
        msg = req.message
        data_parts = [p.data for p in msg.parts if p.data is not None]
        hinted = (msg.metadata or {}).get("skill")
        if data_parts:
            first = data_parts[0]
            if isinstance(first, dict) and "skill" in first:
                return str(first["skill"]), first.get("input", {})
            if hinted is not None:
                return str(hinted), first
            raise invalid_params('a DataPart must be {"skill": "<id>", "input": {...}}, or set message.metadata.skill')
        if hinted is not None:
            return str(hinted), {}
        if not any(p.text is not None for p in msg.parts):
            raise content_type_not_supported("send a TextPart, or a DataPart that invokes a skill")
        return None

    def _check_addressing(self, task_id: str | None, context_id: str | None, prepared: Prepared | None) -> None:
        """Validate message.taskId / message.contextId before anything runs (spec 3.4)."""
        if task_id:
            task = self._ephemeral.get(task_id)
            if task is not None:
                raise unsupported_operation(
                    f"task {task_id} is finished ({task.status.state.value}) and takes no more messages"
                )
            record = self._deal_record(task_id)
            if record is None:
                raise task_not_found(task_id)
            state = a2a_state(record["snapshot"])
            if state in TERMINAL_STATES:
                raise unsupported_operation(
                    f"task {task_id} is in terminal state {state.value} and takes no more messages"
                )
            if context_id and context_id != task_id:
                raise invalid_params("contextId does not match the task (a deal task's contextId is its deal id)")
            if prepared is not None and prepared.deal_id != task_id:
                raise invalid_params("taskId does not match the deal named in the skill input")
        elif context_id and prepared is not None and prepared.deal_id and context_id != prepared.deal_id:
            raise invalid_params("contextId must be the deal id for a deal skill (or omitted)")

    def _send_message(self, params: Any, auth: str | None, dialect: str) -> dict[str, Any]:
        req = self._parse(SendMessageRequest, params)
        msg = req.message
        push_cfg = req.configuration.taskPushNotificationConfig if req.configuration else None
        if push_cfg is not None:
            if self.push is None:
                raise push_not_supported()
            try:
                self.push.check(push_cfg)
            except PushConfigError as exc:
                raise invalid_params(str(exc)) from None
        invocation = self._extract_skill(req)
        prepared = prepare_skill(*invocation) if invocation is not None else None
        self._check_addressing(msg.taskId, msg.contextId, prepared)
        if prepared is None:
            return {"message": help_message(msg.contextId).dump()}
        outcome = execute_skill(self.service, prepared, auth)
        task = self._outcome_task(prepared, outcome.data, outcome.error, msg.contextId)
        if push_cfg is not None and prepared.deal_id and task.id == prepared.deal_id:
            task = self._register_push(task, push_cfg, dialect)
        return {"task": task.dump()}

    def _outcome_task(
        self, prepared: Prepared, data: dict[str, Any] | None, error: ServiceError | None, context_id: str | None
    ) -> Task:
        skill, deal_id = prepared.skill, prepared.deal_id
        if error is None:
            if deal_id is not None:
                return deal_task(self.service.record(deal_id))
            return self._ephemeral.put(
                ephemeral_task(
                    TaskState.COMPLETED,
                    f"{skill.id} done.",
                    context_id=context_id or new_id(),
                    task_id=new_id(),
                    data={"skill": skill.id, **(data or {})},
                    artifact_name="result",
                )
            )
        extra = {k: v for k, v in error.extra.items() if k == "violations"}
        if deal_id is not None and skill.id != "open-escrow":
            if error.status == 404:
                raise task_not_found(deal_id)
            return annotate_refusal(deal_task(self.service.record(deal_id)), error.status, error.detail, extra)
        if skill.id == "open-escrow" and error.status == 502 and "deal" in error.extra:
            # The deal is stored (CONTRACTED, nothing held): its task is FAILED and retryable.
            return annotate_refusal(deal_task(self.service.record(str(deal_id))), 502, error.detail, extra)
        state = TaskState.FAILED if error.status >= 500 else TaskState.REJECTED
        verb = "failed" if state == TaskState.FAILED else "was refused"
        return self._ephemeral.put(
            ephemeral_task(
                state,
                f"{skill.id} {verb} ({error.status}): {error.detail}",
                context_id=context_id or deal_id or new_id(),
                task_id=new_id(),
                data={"skill": skill.id, "ok": False, "status": error.status, "detail": error.detail, **extra},
            )
        )

    def _register_push(self, task: Task, cfg: TaskPushNotificationConfig, dialect: str) -> Task:
        assert self.push is not None
        note: dict[str, Any]
        try:
            created = self.push.create(task.id, cfg, dialect)
            note = {"registered": True, "id": created.id}
        except PushConfigError as exc:
            # The skill already ran, so do not fail the request over the webhook: say why it was not registered.
            note = {"registered": False, "reason": str(exc)}
        meta = dict(task.metadata or {})
        meta["trustee"] = {**meta.get("trustee", {}), "pushNotification": note}
        task.metadata = meta
        return task

    # --------------------------------------------------------------------- ListTasks

    def _list_tasks(self, params: Any, auth: str | None, dialect: str) -> dict[str, Any]:
        req = self._parse(ListTasksRequest, params if params is not None else {})
        if not req.contextId and self.service.operator_token is not None:
            # Without a filter this enumerates every deal on the platform, which GET /v1/deals/{id} never did.
            try:
                self.service.require_operator(auth)
            except ServiceError as exc:
                raise unauthorized("listing every task needs the operator bearer token; filter by contextId") from exc
        page_size = DEFAULT_PAGE_SIZE if req.pageSize is None else req.pageSize
        if not 1 <= page_size <= MAX_PAGE_SIZE:
            raise invalid_params(f"pageSize must be between 1 and {MAX_PAGE_SIZE}")
        after = _parse_time(req.statusTimestampAfter) if req.statusTimestampAfter else None
        cursor = _decode_cursor(req.pageToken) if req.pageToken else None

        matches: list[tuple[tuple[str, str], dict[str, Any]]] = []
        for row in self.service.store.list_deals(limit=1_000_000):
            if row["contract"] is None or (req.contextId and row["deal_id"] != req.contextId):
                continue
            if req.status is not None and a2a_state(row["snapshot"]) != req.status:
                continue
            if after is not None and _parse_time(row["updated_at"]) < after:
                continue
            matches.append(((row["updated_at"], row["deal_id"]), row))
        matches.sort(key=lambda item: item[0], reverse=True)  # most recently updated first
        window = [m for m in matches if cursor is None or m[0] < cursor]
        page = window[:page_size]
        tasks = [
            deal_task(
                {"snapshot": r["snapshot"], "contract": r["contract"], "updated_at": r["updated_at"]},
                include_artifacts=bool(req.includeArtifacts),
            ).dump()
            for _, r in page
        ]
        next_token = _encode_cursor(page[-1][0]) if len(window) > page_size else ""
        return {"tasks": tasks, "nextPageToken": next_token, "pageSize": page_size, "totalSize": len(matches)}

    # ---------------------------------------------------------------------- CancelTask

    def _cancel_task(self, params: Any, auth: str | None, dialect: str) -> dict[str, Any]:
        req = self._parse(CancelTaskRequest, params)
        if self._ephemeral.get(req.id) is not None:
            raise task_not_cancelable(req.id, "the task has already finished")
        record = self._deal_record(req.id)
        if record is None:
            raise task_not_found(req.id)
        state = a2a_state(record["snapshot"])
        if state != TaskState.AUTH_REQUIRED:
            if state in TERMINAL_STATES:
                raise task_not_cancelable(req.id, f"the task is already {state.value}")
            raise task_not_cancelable(
                req.id,
                "funds are held, and a held escrow ends by attestation, deadline or arbitration, never by cancel",
            )
        # The only cancelable moment is before the payer approves: nothing is held, so no PayPal call is made.
        order_id = record["snapshot"]["escrow"]["order_id"]
        try:
            self.service.approval_cancel(order_id)
        except ServiceError:
            raise task_not_found(req.id) from None
        task = deal_task(self.service.record(req.id))
        if task.status.state != TaskState.CANCELED:
            raise task_not_cancelable(req.id, "the payer approved first, so the funds are now held")
        return task.dump()

    # ------------------------------------------------------------- push notification configs

    def _push_task(self, task_id: str) -> PushNotifier:
        if self.push is None:
            raise push_not_supported()
        if self._ephemeral.get(task_id) is not None:
            raise unsupported_operation("push notifications apply to deal tasks only")
        if self._deal_record(task_id) is None:
            raise task_not_found(task_id)
        return self.push

    def _push_create(self, params: Any, auth: str | None, dialect: str) -> dict[str, Any]:
        cfg = self._parse(TaskPushNotificationConfig, params)
        if not cfg.taskId:
            raise invalid_params("taskId is required")
        push = self._push_task(cfg.taskId)
        try:
            return push.create(cfg.taskId, cfg, "v03" if dialect == "v03" else "v1").dump()
        except PushConfigError as exc:
            raise invalid_params(str(exc)) from None

    def _push_get(self, params: Any, auth: str | None, dialect: str) -> dict[str, Any]:
        ref = self._parse(PushConfigRef, params)
        found = self._push_task(ref.taskId).get(ref.taskId, ref.id)
        if found is None:
            raise task_not_found(ref.taskId)
        return found.dump()

    def _push_list(self, params: Any, auth: str | None, dialect: str) -> dict[str, Any]:
        req = self._parse(ListPushConfigsRequest, params)
        configs = self._push_task(req.taskId).list_for(req.taskId)
        return {"configs": [c.dump() for c in configs], "nextPageToken": ""}

    def _push_delete(self, params: Any, auth: str | None, dialect: str) -> dict[str, Any]:
        ref = self._parse(PushConfigRef, params)
        self._push_task(ref.taskId).delete(ref.taskId, ref.id)
        return {}


def _parse_time(text: str) -> datetime:
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise invalid_params(f"not an ISO 8601 timestamp: {text[:40]!r}") from None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _encode_cursor(key: tuple[str, str]) -> str:
    return base64.urlsafe_b64encode(json.dumps(list(key)).encode()).decode().rstrip("=")


def _decode_cursor(token: str) -> tuple[str, str]:
    try:
        stamp, deal_id = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
        return str(stamp), str(deal_id)
    except (ValueError, TypeError, binascii.Error):
        raise invalid_params("pageToken is not a token this server issued") from None


# ----------------------------------------------------------------------------------------- HTTP

_ID_TYPES = (str, int, type(None))


def _envelope(rpc_id: Any, *, result: Any = None, error: A2AError | None = None) -> JSONResponse:
    body: dict[str, Any] = {"jsonrpc": "2.0", "id": rpc_id}
    if error is not None:
        body["error"] = error.payload()
    else:
        body["result"] = result
    return JSONResponse(body)


JSONRPC_REQUEST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["jsonrpc", "id", "method"],
    "properties": {
        "jsonrpc": {"type": "string", "enum": ["2.0"]},
        "id": {"type": ["string", "integer"], "description": "Echoed in the response."},
        "method": {
            "type": "string",
            "description": "A2A 1.0 method name. The v0.3 names (`message/send`, `tasks/get`, `tasks/cancel`, "
            "`tasks/pushNotificationConfig/set|get|list|delete`) are accepted and answered in the v0.3 shape.",
            "enum": list(METHODS),
        },
        "params": {"type": "object", "description": "The method's request object (SendMessageRequest, ...)."},
    },
}
JSONRPC_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["jsonrpc", "id"],
    "description": "Exactly one of `result` and `error`. Protocol errors are JSON-RPC errors in an HTTP 200.",
    "properties": {
        "jsonrpc": {"type": "string", "enum": ["2.0"]},
        "id": {"type": ["string", "integer", "null"]},
        "result": {
            "description": "SendMessage: {task} or {message}. GetTask, CancelTask: a Task. ListTasks: {tasks, ...}."
        },
        "error": {
            "type": "object",
            "required": ["code", "message"],
            "properties": {
                "code": {"type": "integer", "description": "JSON-RPC -32700..-32603, or an A2A code -32001..-32009."},
                "message": {"type": "string"},
                "data": {"type": "array", "items": {"type": "object"}},
            },
        },
    },
}
_EXAMPLES = {
    "skill_help": {
        "summary": "Ask what Trustee can do (text only)",
        "value": {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "SendMessage",
            "params": {
                "message": {"messageId": "m-1", "role": "ROLE_USER", "parts": [{"text": "What can you do?"}]},
            },
        },
    },
    "get_task": {
        "summary": "Poll a deal task",
        "value": {"jsonrpc": "2.0", "id": 2, "method": "GetTask", "params": {"id": "deal-w12-honest-001"}},
    },
}


class A2ABinding:
    """The mounted pieces: router, server and push notifier. `close` stops the push worker."""

    def __init__(self, service: DealService, *, push_http: httpx.Client | None = None):
        self.service = service
        self.server = A2AServer(service)
        self.push = PushNotifier(service.store, self.server.build_task_dict, http=push_http)
        self.push.attach(service.ledger)
        self.server.push = self.push
        self.router = self._build_router()

    def close(self) -> None:
        self.push.close()

    def _build_router(self) -> APIRouter:
        router = APIRouter()
        server = self.server

        async def endpoint(request: Request) -> JSONResponse:
            declared = request.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
                return _envelope(None, error=invalid_request("request body too large"))
            raw = await request.body()
            if len(raw) > MAX_BODY_BYTES:
                return _envelope(None, error=invalid_request("request body too large"))
            try:
                body = json.loads(raw)
            except ValueError:
                return _envelope(None, error=parse_error())
            if (
                not isinstance(body, dict)
                or body.get("jsonrpc") != "2.0"
                or not isinstance(body.get("method"), str)
                or "id" not in body
                or not isinstance(body["id"], _ID_TYPES)
                or isinstance(body["id"], bool)
            ):
                rpc_id = body.get("id") if isinstance(body, dict) and isinstance(body.get("id"), str | int) else None
                return _envelope(rpc_id, error=invalid_request())
            rpc_id = body["id"]
            try:
                result = await asyncio.to_thread(
                    server.dispatch,
                    body["method"],
                    body.get("params"),
                    request.headers.get("authorization"),
                    request.headers.get("a2a-version"),
                )
            except A2AError as exc:
                return _envelope(rpc_id, error=exc)
            except Exception:
                log.exception("A2A method %s crashed", str(body["method"])[:60])
                return _envelope(rpc_id, error=internal_error())
            return _envelope(rpc_id, result=result)

        router.add_api_route(
            "/a2a",
            endpoint,
            methods=["POST"],
            response_model=None,
            operation_id="a2a_jsonrpc",
            tags=["A2A"],
            summary="A2A JSON-RPC endpoint (SendMessage, GetTask, ListTasks, CancelTask, push configs)",
            description=(
                "The Agent2Agent protocol (spec 1.0.0, JSON-RPC binding) in front of the same service as `/v1`. "
                'Invoke a Trustee skill with `SendMessage` and one DataPart `{"skill": "<id>", "input": {...}}`; '
                "the input is the REST body of the matching `/v1` operation. A deal is a task whose id is the deal id; "
                "`GetTask` rebuilds it from the durable deal. Register a webhook with "
                "`CreateTaskPushNotificationConfig` (or `configuration.taskPushNotificationConfig` in `SendMessage`) "
                "to be told when funds are held or released. Signatures inside the payloads authorize every change; "
                "A2A is only the transport. Streaming is not supported. Skills and states are described in "
                "`docs/A2A.md` and the Agent Card."
            ),
            openapi_extra={
                "requestBody": {
                    "required": True,
                    "content": {"application/json": {"schema": JSONRPC_REQUEST_SCHEMA, "examples": _EXAMPLES}},
                }
            },
            responses={
                200: {
                    "description": "A JSON-RPC response. Errors are reported in `error`, with HTTP 200.",
                    "content": {"application/json": {"schema": JSONRPC_RESPONSE_SCHEMA}},
                }
            },
        )
        return router
