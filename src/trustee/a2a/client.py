"""A generic A2A client for the JSON-RPC binding. Nothing in here is specific to Trustee.

`A2AClient` discovers an agent from its Agent Card, picks the JSON-RPC interface, and speaks the 1.0 methods
(`SendMessage`, `GetTask`, `ListTasks`, `CancelTask`, the push-notification-config methods). `call_skill` is
a convenience for agents that publish skills as `{"skill", "input"}` DataParts, which is how Trustee does it.

Pass `http=` to reuse an `httpx.Client` (a FastAPI `TestClient` works), which is how the tests run it
in-process. The client never signs anything: if the agent you talk to needs signed payloads, build them
first (for Trustee, with `trustee.protocol` and `trustee.identity`) and pass them as the skill input.

The module also has two helpers for the *receiving* side of push notifications, so an agent that registers a
webhook can authenticate and parse what arrives: `push_token_matches` and `task_from_push`.
"""

from __future__ import annotations

import hmac
from collections.abc import Mapping
from typing import Any

import httpx

from .models import (
    PROTOCOL_VERSION,
    PUSH_TOKEN_HEADER,
    A2AError,
    AuthenticationInfo,
    Message,
    Role,
    Task,
    TaskPushNotificationConfig,
    TaskState,
    data_part,
    new_id,
    text_part,
)

CARD_PATHS = ("/.well-known/agent-card.json", "/.well-known/agent.json")


class A2AClient:
    def __init__(
        self,
        base_url: str | None = None,
        *,
        card_url: str | None = None,
        http: httpx.Client | None = None,
        authorization: str | None = None,
        version: str = PROTOCOL_VERSION,
    ):
        """Give `base_url` (the agent's origin) or `card_url` (the full Agent Card URL).
        `authorization` is sent as the HTTP `Authorization` header, e.g. `Bearer <token>`."""
        if base_url is None and card_url is None:
            raise ValueError("A2AClient needs a base_url or a card_url")
        self.base_url = (base_url or "").rstrip("/")
        self.card_url = card_url
        self._http = http if http is not None else httpx.Client(timeout=30.0)
        self._authorization = authorization
        self._version = version
        self._card: dict[str, Any] | None = None
        self._endpoint: str | None = None
        self._next_id = 0

    def close(self) -> None:
        self._http.close()

    # ------------------------------------------------------------------ discovery

    def fetch_card(self, refresh: bool = False) -> dict[str, Any]:
        """The agent's Agent Card as a dict. Tries the current well-known path, then the older one."""
        if self._card is not None and not refresh:
            return self._card
        urls = [self.card_url] if self.card_url else [self.base_url + path for path in CARD_PATHS]
        last: str = "no URL tried"
        for url in urls:
            assert url is not None
            resp = self._http.get(url)
            if resp.status_code == 200:
                card: dict[str, Any] = resp.json()
                self._card = card
                self._endpoint = None
                return card
            last = f"{url} answered HTTP {resp.status_code}"
        raise RuntimeError(f"could not fetch the Agent Card: {last}")

    def endpoint(self) -> str:
        """URL of the card's JSON-RPC interface (1.0 `supportedInterfaces`, then v0.3 `url`)."""
        if self._endpoint is not None:
            return self._endpoint
        card = self.fetch_card()
        for iface in card.get("supportedInterfaces") or []:
            if str(iface.get("protocolBinding", "")).upper() == "JSONRPC":
                self._endpoint = str(iface["url"])
                return self._endpoint
        if str(card.get("preferredTransport", "")).upper() == "JSONRPC" and card.get("url"):
            self._endpoint = str(card["url"])
            return self._endpoint
        for iface in card.get("additionalInterfaces") or []:
            if str(iface.get("transport", "")).upper() == "JSONRPC":
                self._endpoint = str(iface["url"])
                return self._endpoint
        raise RuntimeError("the Agent Card lists no JSON-RPC interface")

    def skill_ids(self) -> list[str]:
        return [str(s["id"]) for s in self.fetch_card().get("skills", [])]

    # ------------------------------------------------------------------ JSON-RPC

    def rpc(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Call one JSON-RPC method and return `result`. Raises A2AError for a JSON-RPC error."""
        self._next_id += 1
        body: dict[str, Any] = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        if params is not None:
            body["params"] = params
        headers = {"A2A-Version": self._version}
        if self._authorization:
            headers["Authorization"] = self._authorization
        resp = self._http.post(self.endpoint(), json=body, headers=headers)
        try:
            payload = resp.json()
        except ValueError:
            raise RuntimeError(f"{method}: HTTP {resp.status_code} with a non-JSON body") from None
        if isinstance(payload, dict) and payload.get("error"):
            err = payload["error"]
            raise A2AError(int(err.get("code", -32603)), str(err.get("message", "")), err.get("data"))
        if not isinstance(payload, dict) or "result" not in payload:
            raise RuntimeError(f"{method}: HTTP {resp.status_code} without a JSON-RPC result")
        return payload["result"]

    # ------------------------------------------------------------------ methods

    def send_message(
        self,
        message: Message | str | dict[str, Any],
        *,
        configuration: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Task | Message:
        """Send a message. A string becomes a one-TextPart user message. Returns a Task or a Message."""
        if isinstance(message, str):
            msg = Message(messageId=new_id(), role=Role.USER, parts=[text_part(message)])
        elif isinstance(message, dict):
            msg = Message.model_validate(message)
        else:
            msg = message
        params: dict[str, Any] = {"message": msg.dump()}
        if configuration:
            params["configuration"] = configuration
        if metadata:
            params["metadata"] = metadata
        result = self.rpc("SendMessage", params)
        if "task" in result:
            return Task.model_validate(result["task"])
        return Message.model_validate(result["message"])

    def call_skill(
        self,
        skill: str,
        input: dict[str, Any] | None = None,  # noqa: A002 - mirrors the wire field name
        *,
        task_id: str | None = None,
        context_id: str | None = None,
        push: TaskPushNotificationConfig | None = None,
    ) -> Task | Message:
        """Invoke a skill: `SendMessage` with one DataPart `{"skill": ..., "input": ...}`.
        Pass `task_id` to address an existing task. `push` registers a webhook in the same call."""
        msg = Message(
            messageId=new_id(),
            role=Role.USER,
            parts=[data_part({"skill": skill, "input": input or {}})],
            taskId=task_id,
            contextId=context_id,
        )
        configuration = {"taskPushNotificationConfig": push.dump()} if push is not None else None
        return self.send_message(msg, configuration=configuration)

    def get_task(self, task_id: str, history_length: int | None = None) -> Task:
        params: dict[str, Any] = {"id": task_id}
        if history_length is not None:
            params["historyLength"] = history_length
        return Task.model_validate(self.rpc("GetTask", params))

    def list_tasks(
        self,
        *,
        context_id: str | None = None,
        status: TaskState | None = None,
        page_size: int | None = None,
        page_token: str | None = None,
        include_artifacts: bool = False,
    ) -> dict[str, Any]:
        """One page: `{"tasks": [Task...], "nextPageToken", "pageSize", "totalSize"}`."""
        params: dict[str, Any] = {"includeArtifacts": include_artifacts}
        if context_id:
            params["contextId"] = context_id
        if status is not None:
            params["status"] = status.value
        if page_size is not None:
            params["pageSize"] = page_size
        if page_token:
            params["pageToken"] = page_token
        result = self.rpc("ListTasks", params)
        result["tasks"] = [Task.model_validate(t) for t in result["tasks"]]
        return result  # type: ignore[no-any-return]

    def cancel_task(self, task_id: str) -> Task:
        return Task.model_validate(self.rpc("CancelTask", {"id": task_id}))

    def set_push_config(
        self,
        task_id: str,
        url: str,
        token: str | None = None,
        authentication: AuthenticationInfo | None = None,
        config_id: str | None = None,
    ) -> TaskPushNotificationConfig:
        """Ask the agent to POST task updates to `url`. `token` is echoed back in `X-A2A-Notification-Token`."""
        cfg = TaskPushNotificationConfig(
            taskId=task_id, url=url, token=token, authentication=authentication, id=config_id
        )
        return TaskPushNotificationConfig.model_validate(self.rpc("CreateTaskPushNotificationConfig", cfg.dump()))

    def get_push_config(self, task_id: str, config_id: str) -> TaskPushNotificationConfig:
        result = self.rpc("GetTaskPushNotificationConfig", {"taskId": task_id, "id": config_id})
        return TaskPushNotificationConfig.model_validate(result)

    def list_push_configs(self, task_id: str) -> list[TaskPushNotificationConfig]:
        result = self.rpc("ListTaskPushNotificationConfigs", {"taskId": task_id})
        return [TaskPushNotificationConfig.model_validate(c) for c in result.get("configs", [])]

    def delete_push_config(self, task_id: str, config_id: str) -> None:
        self.rpc("DeleteTaskPushNotificationConfig", {"taskId": task_id, "id": config_id})


# ---------------------------------------------------------------------------------- conveniences


def artifact_data(task: Task, artifact_id: str) -> dict[str, Any] | None:
    """The first DataPart of the named artifact, or None."""
    for artifact in task.artifacts or []:
        if artifact.artifactId == artifact_id:
            for part in artifact.parts:
                if isinstance(part.data, dict):
                    return part.data
    return None


def status_text(task: Task) -> str:
    """The text of the task's status message ('' if none)."""
    message = task.status.message
    if message is None:
        return ""
    return "\n".join(p.text for p in message.parts if p.text is not None)


def last_refusal(task: Task) -> dict[str, Any] | None:
    """If the message you just sent to this task was refused, `{ok: False, status, detail, ...}`; else None."""
    meta = (task.metadata or {}).get("trustee") or {}
    refusal = meta.get("lastRequest")
    return refusal if isinstance(refusal, dict) and refusal.get("ok") is False else None


def push_token_matches(headers: Mapping[str, str], expected: str) -> bool:
    """Constant-time check of the `X-A2A-Notification-Token` header against the token you registered."""
    lowered = {k.lower(): v for k, v in headers.items()}
    got = lowered.get(PUSH_TOKEN_HEADER.lower())
    return got is not None and hmac.compare_digest(got.encode(), expected.encode())


def task_from_push(body: dict[str, Any]) -> Task:
    """Parse a push notification body (`{"task": {...}}`, or a bare v0.3 Task) into a Task."""
    inner = body.get("task", body)
    status = dict(inner["status"])
    state = status.get("state", "")
    if not str(state).startswith("TASK_STATE_"):  # v0.3 payload
        status["state"] = f"TASK_STATE_{str(state).upper().replace('-', '_')}"
        inner = {**inner, "status": status}
    return Task.model_validate(inner)


__all__ = [
    "A2AClient",
    "artifact_data",
    "last_refusal",
    "push_token_matches",
    "status_text",
    "task_from_push",
]
