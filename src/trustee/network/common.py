"""What the two network agents share: a small A2A server, peer discovery by Agent Card, and call helpers.

Northwind's planning agent and Tidewater's sales agent are ordinary A2A agents. Each has an Agent Card, a
JSON-RPC endpoint at `/a2a` and a handful of skills, and each calls the others through `A2AClient` over real
HTTP. This module holds the parts that are not specific to either of them:

* `NetworkAgent`: a FastAPI app with the card routes and a `SendMessage` / `GetTask` endpoint. Skills are plain
  functions. A refused skill (`SkillRefused`) becomes a task in `TASK_STATE_REJECTED`, as Trustee does it.
* Identity in the Agent Card. Each card declares the Ed25519 signed-messages extension and carries the agent's
  id, principal and public key in that extension's `params`. `params` is the A2A-defined free-form slot of an
  `AgentExtension`, so any client can read it and a client that ignores it still works. A peer that wants to
  check an offer or a contract fetches the card and verifies against that key, before Trustee is involved.
* `PeerLink`: one outbound conversation with one peer. It calls `A2AClient`, and tells an observer about every
  hop, which is how the arena puts each A2A call on the ledger tape and how the launcher prints it.
* `Waiter`: polling with an injectable clock and sleep, so tests never wait.

Nothing here can move money. Money moves only through Trustee, on messages signed by the parties.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import TypeAdapter, ValidationError

from ..a2a.client import A2AClient, artifact_data, last_refusal
from ..a2a.models import (
    A2AError,
    Message,
    Part,
    Role,
    SendMessageRequest,
    Task,
    TaskState,
    content_type_not_supported,
    data_part,
    internal_error,
    invalid_params,
    invalid_params_from,
    invalid_request,
    method_not_found,
    new_id,
    parse_error,
    task_not_found,
    text_part,
    unsupported_operation,
    version_not_supported,
)
from ..a2a.tasks import ephemeral_task
from ..identity import AgentKey, verify
from ..llm import LLM
from ..mandate import Mandate
from ..platform.wire import agent_signing_bytes, mandate_to_dict, principal_signing_bytes
from ..protocol import Accept, Counter, Msg, Offer

log = logging.getLogger(__name__)

SIGNING_EXTENSION_URI = "https://github.com/trustee/ext/ed25519-signed-messages/v1"
MAX_BODY_BYTES = 8 << 20  # a delivery travels inside a message; the Q3 dataset is well under 1 MB
MAX_CARD_BYTES = 256 << 10
MAX_TASKS = 500
_VERSION = re.compile(r"^1\.0(\.\d+)?$")
_move_adapter: TypeAdapter[Any] = TypeAdapter(Msg)


# ------------------------------------------------------------------------------------------ errors


class SkillRefused(Exception):
    """The skill ran and said no. `status` uses HTTP meanings (401 bad signature, 409 conflict, 422 bad input)."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


class PeerError(Exception):
    """A peer's Agent Card or reply was unusable."""


# --------------------------------------------------------------------------------- hops and waiting


@dataclass(frozen=True)
class Hop:
    """One A2A call, as an observer sees it. `caller` is an agent id; `peer` is a short name ("tidewater")."""

    caller: str
    peer: str
    method: str
    skill: str
    note: str
    price: str = ""

    def line(self) -> str:
        who = self.caller.split("-")[0]
        tail = f"{self.note} {self.price}".strip()
        return f"{who:<10} -> {self.peer:<10} {self.method} {self.skill}: {tail}"


HopObserver = Callable[[Hop], None]


@dataclass
class Waiter:
    """Poll `check` until it is true or the timeout passes. `sleep` and `clock` are injectable."""

    poll_seconds: float = 0.2
    timeout_seconds: float = 120.0
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic

    def until(self, check: Callable[[], bool], tick: Callable[[], None] | None = None) -> bool:
        deadline = self.clock() + self.timeout_seconds
        while True:
            if check():
                return True
            if self.clock() >= deadline:
                return False
            if tick is not None:
                tick()
            self.sleep(self.poll_seconds)


def state_name(task: Task) -> str:
    return task.status.state.value.removeprefix("TASK_STATE_")


def result_data(task: Task) -> dict[str, Any]:
    """The `result` artifact of a task returned by one of the network agents' skills."""
    data = artifact_data(task, "result")
    if data is None:
        raise PeerError(f"task {task.id} ({state_name(task)}) carries no result")
    return data


def refusal_of(task: Task) -> dict[str, Any] | None:
    """`{ok: False, status, detail}` when the message that produced this task was refused, else None.

    Covers Trustee's two shapes (a deal task with `lastRequest`, or a one-off REJECTED/FAILED task whose status
    message carries a DataPart) and the network agents' own REJECTED tasks."""
    found = last_refusal(task)
    if found is not None:
        return found
    if task.status.state in (TaskState.REJECTED, TaskState.FAILED) and task.status.message is not None:
        for part in task.status.message.parts:
            if isinstance(part.data, dict) and part.data.get("ok") is False:
                return part.data
        text = " ".join(p.text for p in task.status.message.parts if p.text)
        return {"ok": False, "status": 0, "detail": text or state_name(task)}
    return None


# ----------------------------------------------------------------------------------------- identity


@dataclass
class AgentIdentity:
    """An agent's id, its principal's id and the two Ed25519 keys. Fresh at every start."""

    agent_id: str
    principal: str
    agent_key: AgentKey
    principal_key: AgentKey
    role: str
    paypal_email: str

    @classmethod
    def generate(cls, agent_base: str, principal_base: str, role: str, paypal_email: str) -> AgentIdentity:
        suffix = uuid.uuid4().hex[:8]  # ids are unique per start: Trustee keeps agents for good, and keys change
        return cls(
            f"{agent_base}-{suffix}",
            f"{principal_base}-{suffix}",
            AgentKey.generate(),
            AgentKey.generate(),
            role,
            paypal_email,
        )

    @property
    def public_key(self) -> str:
        return self.agent_key.public_key_b64

    @property
    def label(self) -> str:
        return self.agent_id.split("-")[0]


@dataclass(frozen=True)
class PeerKey:
    """What a peer's Agent Card says about who signs for it."""

    agent_id: str
    public_key: str
    principal: str
    card_url: str
    name: str = ""

    @property
    def label(self) -> str:
        return self.agent_id.split("-")[0]


def card_url_for(url: str) -> str:
    """A peer is named by its origin (or mount path) or by the card URL itself."""
    clean = url.strip()
    parts = urlsplit(clean)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        raise PeerError("an agent URL must be an absolute http(s) URL without credentials")
    if parts.path.endswith(".json"):
        return clean
    return clean.rstrip("/") + "/.well-known/agent-card.json"


def key_from_card(card: dict[str, Any], card_url: str) -> PeerKey:
    for ext in (card.get("capabilities") or {}).get("extensions") or []:
        if ext.get("uri") == SIGNING_EXTENSION_URI:
            params = ext.get("params") or {}
            agent_id, public_key = params.get("agentId"), params.get("publicKey")
            if isinstance(agent_id, str) and isinstance(public_key, str) and agent_id and public_key:
                principal, name = str(params.get("principal", "")), str(card.get("name", ""))
                return PeerKey(agent_id, public_key, principal, card_url, name)
    raise PeerError("the Agent Card does not declare an Ed25519 signing identity (capabilities.extensions)")


def discover_peer(http: httpx.Client, url: str) -> PeerKey:
    """Fetch an Agent Card and read the signing identity out of it. Redirects are not followed and the card is
    size-capped, because the URL may have come from a message."""
    card_url = card_url_for(url)
    resp = http.get(card_url, headers={"Accept": "application/json"}, follow_redirects=False)
    if resp.status_code != 200:
        raise PeerError(f"{card_url} answered HTTP {resp.status_code}")
    if len(resp.content) > MAX_CARD_BYTES:
        raise PeerError("the Agent Card is too large")
    try:
        card = resp.json()
    except ValueError:
        raise PeerError("the Agent Card is not JSON") from None
    if not isinstance(card, dict):
        raise PeerError("the Agent Card is not a JSON object")
    return key_from_card(card, card_url)


def parse_move(raw: Any) -> Offer | Counter | Accept:
    """A negotiation move out of a skill input. Only offers, counters and accepts are moves."""
    try:
        move = _move_adapter.validate_python(raw)
    except ValidationError as exc:
        raise SkillRefused(422, f"not a valid negotiation move: {exc.errors()[0]['msg']}") from None
    if not isinstance(move, Offer | Counter | Accept):
        raise SkillRefused(422, f"a {move.kind} is not a negotiation move")
    return move


def move_is_signed_by(move: Offer | Counter | Accept, peer: PeerKey) -> bool:
    return (
        bool(move.signature)
        and move.sender == peer.agent_id
        and verify(peer.public_key, move.signature or "", move.signing_bytes())
    )


# ------------------------------------------------------------------------------------- outbound calls


class PeerLink:
    """One conversation with one peer. Every call is reported to the observer as a `Hop`."""

    def __init__(self, caller: str, peer: str, client: A2AClient, observe: HopObserver | None):
        self.caller = caller
        self.peer = peer
        self.client = client
        self._observe = observe
        self._seen: dict[str, str] = {}

    def _emit(self, method: str, skill: str, note: str, price: str = "") -> None:
        if self._observe is not None:
            try:
                self._observe(Hop(self.caller, self.peer, method, skill, note, price))
            except Exception:  # noqa: BLE001 - an observer must never break a deal
                log.exception("hop observer failed")

    def skill(
        self,
        skill: str,
        payload: dict[str, Any],
        *,
        task_id: str | None = None,
        files: Sequence[Part] = (),
        describe: Callable[[Task | Message], tuple[str, str]] | None = None,
    ) -> Task | Message:
        """Invoke a skill (`SendMessage` with one DataPart, plus any file parts)."""
        message = Message(
            messageId=new_id(),
            role=Role.USER,
            parts=[data_part({"skill": skill, "input": payload}), *files],
            taskId=task_id,
        )
        try:
            result = self.client.send_message(message)
        except A2AError as exc:
            self._emit("SendMessage", skill, f"ERROR {exc.code}")
            raise
        note, price = describe(result) if describe is not None else self.default_note(result)
        self._emit("SendMessage", skill, note, price)
        return result

    @staticmethod
    def default_note(result: Task | Message) -> tuple[str, str]:
        if isinstance(result, Task):
            refused = refusal_of(result)
            if refused is not None and result.status.state == TaskState.REJECTED:
                return f"REJECTED {refused.get('status', '')} {str(refused.get('detail', ''))[:80]}".strip(), ""
            return state_name(result), ""
        return "message", ""

    def get_task(self, task_id: str) -> Task:
        """GetTask. Reported only when the state differs from the last one seen, so polling stays quiet."""
        task = self.client.get_task(task_id)
        name = state_name(task)
        if self._seen.get(task_id) != name:
            self._seen[task_id] = name
            self._emit("GetTask", "task", name)
        return task

    def cancel_task(self, task_id: str) -> Task:
        task = self.client.cancel_task(task_id)
        self._emit("CancelTask", "task", state_name(task))
        return task

    def set_push(self, task_id: str, url: str, token: str, *, send_current_state: bool) -> None:
        # Reported first: with send_current_state the first push can land before this call returns.
        self._emit("CreateTaskPushNotificationConfig", "task", "webhook registered")
        try:
            self.client.set_push_config(task_id, url, token=token, send_current_state=send_current_state)
        except A2AError as exc:
            self._emit("CreateTaskPushNotificationConfig", "task", f"ERROR {exc.code}")
            raise

    def card_fetched(self, name: str) -> None:
        self._emit("GetAgentCard", "card", name)


# -------------------------------------------------------------------------------- the agent's server


@dataclass(frozen=True)
class Call:
    """What a skill handler is given."""

    skill: str
    input: dict[str, Any]
    message: Message

    def files(self) -> list[Part]:
        return [p for p in self.message.parts if p.raw is not None]


Handler = Callable[[Call], dict[str, Any]]


@dataclass(frozen=True)
class SkillDef:
    id: str
    name: str
    description: str
    tags: list[str]
    examples: list[str]
    handler: Handler


class NetworkAgent:
    """An A2A agent: Agent Card, `/a2a` JSON-RPC endpoint, and skills. Subclasses supply the skills."""

    card_name = "agent"
    card_description = ""
    org_name = ""

    def __init__(
        self,
        identity: AgentIdentity,
        *,
        http: httpx.Client | None = None,
        public_url: str | None = None,
        observer: HopObserver | None = None,
    ):
        self.identity = identity
        self.http: httpx.Client = http if http is not None else httpx.Client(timeout=30.0)
        self.public_url = public_url.rstrip("/") if public_url else None
        self.observer = observer
        self._skills: dict[str, SkillDef] = {}
        self._tasks: OrderedDict[str, Task] = OrderedDict()
        self._tasks_lock = threading.Lock()
        self._session_lock = threading.RLock()
        self.app = FastAPI(title=self.card_name, docs_url=None, redoc_url=None, openapi_url=None)
        self._install_routes()

    # ------------------------------------------------------------------ configuration

    @contextmanager
    def session(self, **overrides: Any) -> Iterator[NetworkAgent]:
        """Run with some attributes swapped (a client, a model, a Trustee URL), then put them back.

        The mounted agents live as long as the server, so one run at a time may borrow them; a second caller
        waits on the lock. Only attributes that already exist can be set, so a typo fails loudly."""
        unknown = [k for k in overrides if not hasattr(self, k)]
        if unknown:
            raise AttributeError(f"{type(self).__name__} has no setting {', '.join(unknown)}")
        with self._session_lock:
            saved = {k: getattr(self, k) for k in overrides}
            for key, value in overrides.items():
                setattr(self, key, value)
            try:
                yield self
            finally:
                for key, value in saved.items():
                    setattr(self, key, value)

    def add_skill(self, skill: SkillDef) -> None:
        self._skills[skill.id] = skill

    def hop(self, caller: str, peer: str, method: str, skill: str, note: str, price: str = "") -> None:
        if self.observer is not None:
            try:
                self.observer(Hop(caller, peer, method, skill, note, price))
            except Exception:  # noqa: BLE001
                log.exception("hop observer failed")

    def link(self, peer: str, client: A2AClient) -> PeerLink:
        return PeerLink(self.identity.agent_id, peer, client, self.observer)

    # ------------------------------------------------------------------------------- the card

    def card(self, base_url: str) -> dict[str, Any]:
        base = base_url.rstrip("/")
        rpc = f"{base}/a2a"
        return {
            "name": self.card_name,
            "description": self.card_description,
            "version": "0.1.0",
            "provider": {"organization": self.org_name, "url": base},
            "supportedInterfaces": [{"url": rpc, "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}],
            # v0.3 field names, so a client written against that generation reads the same endpoint.
            "protocolVersion": "0.3.0",
            "url": rpc,
            "preferredTransport": "JSONRPC",
            "capabilities": {
                "streaming": False,
                "pushNotifications": False,
                "extensions": [
                    {
                        "uri": SIGNING_EXTENSION_URI,
                        "description": (
                            "This agent signs its offers, counters, accepts, contracts and deliveries with Ed25519 "
                            "(canonical JSON of the message without its signature; urlsafe-base64). params.publicKey "
                            "is the key to verify them with; Trustee holds the same key once the agent registers."
                        ),
                        "required": True,
                        "params": {
                            "algorithm": "ed25519",
                            "agentId": self.identity.agent_id,
                            "principal": self.identity.principal,
                            "publicKey": self.identity.public_key,
                        },
                    }
                ],
            },
            "defaultInputModes": ["application/json"],
            "defaultOutputModes": ["application/json"],
            "skills": [
                {
                    "id": s.id,
                    "name": s.name,
                    "description": (
                        f'{s.description} A2A: SendMessage with one DataPart {{"skill": "{s.id}", "input": {{...}}}}.'
                    ),
                    "tags": s.tags,
                    "examples": s.examples,
                    "inputModes": ["application/json"],
                    "outputModes": ["application/json"],
                }
                for s in self._skills.values()
            ],
        }

    def _base_url(self, request: Request) -> str:
        if self.public_url:
            return self.public_url
        scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
        host = request.headers.get("host") or request.url.netloc
        return f"{scheme}://{host}{request.scope.get('root_path', '')}"

    # ------------------------------------------------------------------------------- routes

    def _install_routes(self) -> None:
        async def card(request: Request) -> JSONResponse:
            return JSONResponse(self.card(self._base_url(request)))

        self.app.add_api_route("/.well-known/agent-card.json", card, methods=["GET"], include_in_schema=False)
        self.app.add_api_route("/.well-known/agent.json", card, methods=["GET"], include_in_schema=False)

        async def endpoint(request: Request) -> JSONResponse:
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
                or isinstance(body["id"], bool)
                or not isinstance(body["id"], str | int)
            ):
                rpc_id = body.get("id") if isinstance(body, dict) and isinstance(body.get("id"), str | int) else None
                return _envelope(rpc_id, error=invalid_request())
            try:
                result = await asyncio.to_thread(
                    self._dispatch, body["method"], body.get("params"), request.headers.get("a2a-version")
                )
            except A2AError as exc:
                return _envelope(body["id"], error=exc)
            except Exception:
                log.exception("%s: A2A method %s crashed", self.card_name, str(body["method"])[:60])
                return _envelope(body["id"], error=internal_error())
            return _envelope(body["id"], result=result)

        self.app.add_api_route("/a2a", endpoint, methods=["POST"], include_in_schema=False)

    # --------------------------------------------------------------------------- dispatch

    def _dispatch(self, method: str, params: Any, version: str | None) -> Any:
        if version is not None and not _VERSION.match(version.strip()):
            raise version_not_supported(version)
        if method == "SendMessage":
            return self._send_message(params)
        if method == "GetTask":
            if not isinstance(params, dict) or not isinstance(params.get("id"), str):
                raise invalid_params("params.id is required")
            with self._tasks_lock:
                task = self._tasks.get(params["id"])
            if task is None:
                raise task_not_found(params["id"])
            return task.dump()
        if method in ("ListTasks", "CancelTask", "SendStreamingMessage", "SubscribeToTask", "GetExtendedAgentCard") or (
            method.endswith("PushNotificationConfig") or method.endswith("PushNotificationConfigs")
        ):
            raise unsupported_operation(f"{self.card_name} answers SendMessage and GetTask only")
        raise method_not_found(method)

    def _send_message(self, params: Any) -> dict[str, Any]:
        if not isinstance(params, dict):
            raise invalid_params("params must be an object")
        try:
            req = SendMessageRequest.model_validate(params)
        except ValidationError as exc:
            raise invalid_params_from(exc) from None
        msg = req.message
        if msg.taskId:
            raise unsupported_operation("this agent's tasks finish in one reply; send a new message without a taskId")
        data = next((p.data for p in msg.parts if p.data is not None), None)
        if data is None:
            if any(p.text is not None for p in msg.parts):
                return {"message": self._help(msg.contextId).dump()}
            raise content_type_not_supported("send a TextPart, or a DataPart that invokes a skill")
        if not isinstance(data, dict) or "skill" not in data:
            raise invalid_params('a DataPart must be {"skill": "<id>", "input": {...}}')
        skill = self._skills.get(str(data["skill"]))
        if skill is None:
            raise invalid_params(f"unknown skill {str(data['skill'])[:60]!r}; known skills: {', '.join(self._skills)}")
        payload = data.get("input", {})
        if not isinstance(payload, dict):
            raise invalid_params("the skill input must be a JSON object")
        task_id = new_id()
        context = msg.contextId or (str(payload["deal_id"]) if isinstance(payload.get("deal_id"), str) else new_id())
        try:
            out = skill.handler(Call(skill.id, payload, msg))
        except SkillRefused as refusal:
            task = ephemeral_task(
                TaskState.REJECTED,
                f"{skill.id} was refused ({refusal.status}): {refusal.detail}",
                context_id=context,
                task_id=task_id,
                data={"skill": skill.id, "ok": False, "status": refusal.status, "detail": refusal.detail},
            )
        else:
            task = ephemeral_task(
                TaskState.COMPLETED,
                str(out.get("summary") or f"{skill.id} done."),
                context_id=context,
                task_id=task_id,
                data={"skill": skill.id, **out},
                artifact_name="result",
            )
        with self._tasks_lock:
            self._tasks[task.id] = task
            while len(self._tasks) > MAX_TASKS:
                self._tasks.popitem(last=False)
        return {"task": task.dump()}

    def _help(self, context_id: str | None) -> Message:
        catalog = {s.id: s.description for s in self._skills.values()}
        text = f"{self.card_name}. {self.card_description}\n" + "\n".join(f"- {k}: {v}" for k, v in catalog.items())
        return Message(
            messageId=new_id(),
            role=Role.AGENT,
            parts=[text_part(text), data_part({"skills": catalog})],
            contextId=context_id,
        )


def _envelope(rpc_id: Any, *, result: Any = None, error: A2AError | None = None) -> JSONResponse:
    body: dict[str, Any] = {"jsonrpc": "2.0", "id": rpc_id}
    if error is not None:
        body["error"] = error.payload()
    else:
        body["result"] = result
    return JSONResponse(body)


# ---------------------------------------------------------------------------- registering at Trustee


def _require_completed(skill: str, result: Task | Message) -> None:
    if not isinstance(result, Task) or result.status.state != TaskState.COMPLETED:
        refused = refusal_of(result) if isinstance(result, Task) else None
        detail = refused.get("detail") if refused else "no task came back"
        raise PeerError(f"Trustee refused {skill}: {detail}")


def publish_mandate(link: PeerLink, identity: AgentIdentity, mandate: Mandate) -> None:
    """Sign `mandate` with the principal key and publish it to Trustee as the `publish-mandate` skill."""
    signed = mandate.sign(identity.principal_key)
    _require_completed("publish-mandate", link.skill("publish-mandate", mandate_to_dict(signed)))


def register_with_trustee(link: PeerLink, identity: AgentIdentity, mandate: Mandate) -> None:
    """Principal, agent and a signed mandate, each as an A2A skill call to Trustee.

    Registering the same keys again is a no-op at Trustee. Anything but COMPLETED raises `PeerError` with
    Trustee's reason."""
    pub = identity.principal_key.public_key_b64
    _require_completed(
        "register-principal",
        link.skill(
            "register-principal",
            {
                "principal": identity.principal,
                "public_key": pub,
                "proof": identity.principal_key.sign(principal_signing_bytes(identity.principal, pub)),
            },
        ),
    )
    body = agent_signing_bytes(
        identity.agent_id, identity.public_key, identity.principal, [identity.role], identity.paypal_email
    )
    _require_completed(
        "register-agent",
        link.skill(
            "register-agent",
            {
                "agent_id": identity.agent_id,
                "public_key": identity.public_key,
                "principal": identity.principal,
                "roles": [identity.role],
                "paypal_email": identity.paypal_email,
                "proof": identity.agent_key.sign(body),
                "principal_signature": identity.principal_key.sign(body),
            },
        ),
    )
    publish_mandate(link, identity, mandate)


def default_llm(mode: str = "auto") -> LLM:
    """The model an agent negotiates with: a live one when a key is set (and the mode allows), else the recorded
    honest negotiation. A scripted model that runs out of moves makes the agents fall back to their policy."""
    from ..arena.fixtures import TRANSCRIPTS
    from ..arena.scripted import ScriptedLLM

    if mode == "live" or (mode == "auto" and os.environ.get("LLM_API_KEY")):
        return LLM()
    return ScriptedLLM.from_file(TRANSCRIPTS / "honest.json")
