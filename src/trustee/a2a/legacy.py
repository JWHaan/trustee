"""A2A v0.3 dialect, for clients that still speak it.

The v0.3 JSON-RPC binding differs from 1.0 in three ways that matter on the wire: method names are
`message/send`-style, objects carry a `kind` discriminator with lower-case enums (`input-required`,
`user`), and results are the bare Task or Message instead of a `{task}` / `{message}` wrapper.

This module is a thin translation layer around the 1.0 implementation. Requests in the old dialect are
rewritten to 1.0 parameters on the way in; results are rewritten on the way out. No behavior lives here.
Only the methods Trustee supports have aliases; streaming and resubscribe map to the same
UnsupportedOperation error as their 1.0 names.
"""

from __future__ import annotations

from typing import Any

from .models import Role, TaskState

METHOD_ALIASES: dict[str, str] = {
    "message/send": "SendMessage",
    "message/stream": "SendStreamingMessage",
    "tasks/get": "GetTask",
    "tasks/list": "ListTasks",
    "tasks/cancel": "CancelTask",
    "tasks/resubscribe": "SubscribeToTask",
    "tasks/pushNotificationConfig/set": "CreateTaskPushNotificationConfig",
    "tasks/pushNotificationConfig/get": "GetTaskPushNotificationConfig",
    "tasks/pushNotificationConfig/list": "ListTaskPushNotificationConfigs",
    "tasks/pushNotificationConfig/delete": "DeleteTaskPushNotificationConfig",
    "agent/getAuthenticatedExtendedCard": "GetExtendedAgentCard",
}

_STATE_TO_LEGACY = {
    TaskState.UNSPECIFIED: "unknown",
    TaskState.SUBMITTED: "submitted",
    TaskState.WORKING: "working",
    TaskState.COMPLETED: "completed",
    TaskState.FAILED: "failed",
    TaskState.CANCELED: "canceled",
    TaskState.INPUT_REQUIRED: "input-required",
    TaskState.REJECTED: "rejected",
    TaskState.AUTH_REQUIRED: "auth-required",
}
_LEGACY_TO_STATE = {legacy: state for state, legacy in _STATE_TO_LEGACY.items()}


# ---------------------------------------------------------------------------- inbound (0.3 -> 1.0)


def _part_in(part: Any) -> Any:
    if not isinstance(part, dict):
        return part
    kind = part.get("kind")
    out = {k: v for k, v in part.items() if k not in ("kind", "file")}
    if kind == "file" and isinstance(part.get("file"), dict):
        file = part["file"]
        if "bytes" in file:
            out["raw"] = file["bytes"]
        if "uri" in file:
            out["url"] = file["uri"]
        if file.get("mimeType"):
            out["mediaType"] = file["mimeType"]
        if file.get("name"):
            out["filename"] = file["name"]
    return out


def _message_in(message: Any) -> Any:
    if not isinstance(message, dict):
        return message
    out = {k: v for k, v in message.items() if k != "kind"}
    if isinstance(out.get("parts"), list):
        out["parts"] = [_part_in(p) for p in out["parts"]]
    return out


def _push_config_in(config: Any, task_id: str | None = None) -> Any:
    if not isinstance(config, dict):
        return config
    out = {k: v for k, v in config.items() if k != "authentication"}
    auth = config.get("authentication")
    if isinstance(auth, dict):
        schemes = auth.get("schemes") or []
        out["authentication"] = {"scheme": schemes[0] if schemes else "Bearer", "credentials": auth.get("credentials")}
    if task_id is not None:
        out["taskId"] = task_id
    return out


def request_in(canonical: str, params: Any) -> Any:
    """Rewrite 0.3 params as 1.0 params for `canonical` (the 1.0 method name)."""
    if not isinstance(params, dict):
        return params
    if canonical == "SendMessage":
        out: dict[str, Any] = {"message": _message_in(params.get("message")), "metadata": params.get("metadata")}
        cfg = params.get("configuration")
        if isinstance(cfg, dict):
            new_cfg = {k: v for k, v in cfg.items() if k not in ("blocking", "pushNotificationConfig")}
            if cfg.get("pushNotificationConfig") is not None:
                new_cfg["taskPushNotificationConfig"] = _push_config_in(cfg["pushNotificationConfig"])
            out["configuration"] = new_cfg
        return {k: v for k, v in out.items() if v is not None}
    if canonical == "CreateTaskPushNotificationConfig":
        # 0.3: {taskId, pushNotificationConfig: {...}}
        return _push_config_in(params.get("pushNotificationConfig"), params.get("taskId") or params.get("id"))
    if canonical in ("GetTaskPushNotificationConfig", "DeleteTaskPushNotificationConfig"):
        # 0.3: {id: <taskId>, pushNotificationConfigId?}
        return {"taskId": params.get("id"), "id": params.get("pushNotificationConfigId") or ""}
    if canonical == "ListTaskPushNotificationConfigs":
        return {"taskId": params.get("id")}
    return params


# --------------------------------------------------------------------------- outbound (1.0 -> 0.3)


def _part_out(part: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in part.items() if k not in ("raw", "url", "filename", "mediaType")}
    if "text" in part:
        out["kind"] = "text"
    elif "data" in part:
        out["kind"] = "data"
    else:
        file: dict[str, Any] = {}
        if "raw" in part:
            file["bytes"] = part["raw"]
        if "url" in part:
            file["uri"] = part["url"]
        if part.get("mediaType"):
            file["mimeType"] = part["mediaType"]
        if part.get("filename"):
            file["name"] = part["filename"]
        out.update({"kind": "file", "file": file})
    return out


def message_out(message: dict[str, Any]) -> dict[str, Any]:
    out = dict(message)
    out["kind"] = "message"
    out["role"] = "user" if message.get("role") == Role.USER else "agent"
    out["parts"] = [_part_out(p) for p in message.get("parts", [])]
    return out


def task_out(task: dict[str, Any]) -> dict[str, Any]:
    out = dict(task)
    out["kind"] = "task"
    status = dict(task["status"])
    status["state"] = _STATE_TO_LEGACY.get(TaskState(status["state"]), "unknown")
    if "message" in status:
        status["message"] = message_out(status["message"])
    out["status"] = status
    if "artifacts" in task:
        out["artifacts"] = [{**a, "parts": [_part_out(p) for p in a["parts"]]} for a in task["artifacts"]]
    if "history" in task:
        out["history"] = [message_out(m) for m in task["history"]]
    return out


def _push_config_out(config: dict[str, Any]) -> dict[str, Any]:
    inner = {k: v for k, v in config.items() if k not in ("taskId", "tenant", "authentication")}
    auth = config.get("authentication")
    if auth:
        inner["authentication"] = {"schemes": [auth["scheme"]], "credentials": auth.get("credentials")}
    return {"taskId": config.get("taskId"), "pushNotificationConfig": inner}


def result_out(canonical: str, result: Any) -> Any:
    """Rewrite a 1.0 result in the 0.3 shape."""
    if canonical == "SendMessage":
        if "task" in result:
            return task_out(result["task"])
        return message_out(result["message"])
    if canonical in ("GetTask", "CancelTask"):
        return task_out(result)
    if canonical == "ListTasks":
        return {**result, "tasks": [task_out(t) for t in result["tasks"]]}
    if canonical in ("CreateTaskPushNotificationConfig", "GetTaskPushNotificationConfig"):
        return _push_config_out(result)
    if canonical == "ListTaskPushNotificationConfigs":
        return [_push_config_out(c) for c in result["configs"]]
    if canonical == "DeleteTaskPushNotificationConfig":
        return None
    return result
