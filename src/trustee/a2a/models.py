"""A2A wire models: the objects that cross the JSON-RPC binding, plus the protocol's error vocabulary.

Field names are the spec's camelCase names verbatim (`messageId`, `contextId`, `mediaType`, ...), so the
models serialize with `model_dump(mode="json", exclude_none=True)` and no alias layer. Enum values are
ProtoJSON names (`TASK_STATE_COMPLETED`, `ROLE_AGENT`). Unknown input fields are ignored, as the spec asks
of receivers, so a newer client does not break an older Trustee.

These classes are shared by `A2AServer` and `A2AClient`: nothing here knows about escrow.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

SPEC_VERSION = "1.0.0"
PROTOCOL_VERSION = "1.0"  # AgentInterface.protocolVersion: "major.minor", per the spec
LEGACY_PROTOCOL_VERSION = "0.3"
PUSH_CONTENT_TYPE = "application/a2a+json"
PUSH_TOKEN_HEADER = "X-A2A-Notification-Token"


def now_iso() -> str:
    """UTC timestamp with millisecond precision and a `Z` suffix, as the spec's data conventions require."""
    return to_iso(datetime.now(UTC))


def to_iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def new_id() -> str:
    return str(uuid.uuid4())


class TaskState(StrEnum):
    UNSPECIFIED = "TASK_STATE_UNSPECIFIED"
    SUBMITTED = "TASK_STATE_SUBMITTED"
    WORKING = "TASK_STATE_WORKING"
    COMPLETED = "TASK_STATE_COMPLETED"
    FAILED = "TASK_STATE_FAILED"
    CANCELED = "TASK_STATE_CANCELED"
    INPUT_REQUIRED = "TASK_STATE_INPUT_REQUIRED"
    REJECTED = "TASK_STATE_REJECTED"
    AUTH_REQUIRED = "TASK_STATE_AUTH_REQUIRED"


TERMINAL_STATES = frozenset({TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELED, TaskState.REJECTED})


class Role(StrEnum):
    UNSPECIFIED = "ROLE_UNSPECIFIED"
    USER = "ROLE_USER"
    AGENT = "ROLE_AGENT"


class _Wire(BaseModel):
    model_config = ConfigDict(extra="ignore")

    def dump(self) -> dict[str, Any]:
        """JSON-ready dict without unset optional fields (the spec's presence rules)."""
        return self.model_dump(mode="json", exclude_none=True)


class Part(_Wire):
    """One section of a message or artifact. Exactly one of `text`, `raw`, `url`, `data` is set."""

    text: str | None = None
    raw: str | None = None  # base64 of the file bytes
    url: str | None = None
    data: Any = None
    metadata: dict[str, Any] | None = None
    filename: str | None = None
    mediaType: str | None = None

    @model_validator(mode="after")
    def _exactly_one_content(self) -> Part:
        if sum(value is not None for value in (self.text, self.raw, self.url, self.data)) != 1:
            raise ValueError("a Part must carry exactly one of text, raw, url, data")
        return self


def text_part(text: str) -> Part:
    return Part(text=text, mediaType="text/plain")


def data_part(data: Any) -> Part:
    return Part(data=data, mediaType="application/json")


class Message(_Wire):
    messageId: str
    role: Role
    parts: list[Part] = Field(min_length=1)
    contextId: str | None = None
    taskId: str | None = None
    metadata: dict[str, Any] | None = None
    extensions: list[str] | None = None
    referenceTaskIds: list[str] | None = None

    @field_validator("role", mode="before")
    @classmethod
    def _lenient_role(cls, value: Any) -> Any:
        # v0.3 clients write "user" / "agent".
        if isinstance(value, str) and value.lower() in ("user", "agent"):
            return Role.USER if value.lower() == "user" else Role.AGENT
        return value


class TaskStatus(_Wire):
    state: TaskState
    message: Message | None = None
    timestamp: str | None = None


class Artifact(_Wire):
    artifactId: str
    name: str | None = None
    description: str | None = None
    parts: list[Part] = Field(min_length=1)
    metadata: dict[str, Any] | None = None
    extensions: list[str] | None = None


class Task(_Wire):
    id: str
    contextId: str | None = None
    status: TaskStatus
    artifacts: list[Artifact] | None = None
    history: list[Message] | None = None
    metadata: dict[str, Any] | None = None


class AuthenticationInfo(_Wire):
    scheme: str
    credentials: str | None = None


class TaskPushNotificationConfig(_Wire):
    tenant: str | None = None
    id: str | None = None
    taskId: str | None = None
    url: str
    token: str | None = None
    authentication: AuthenticationInfo | None = None


class SendMessageConfiguration(_Wire):
    acceptedOutputModes: list[str] | None = None
    taskPushNotificationConfig: TaskPushNotificationConfig | None = None
    historyLength: int | None = Field(default=None, ge=0)
    returnImmediately: bool | None = None


class SendMessageRequest(_Wire):
    tenant: str | None = None
    message: Message
    configuration: SendMessageConfiguration | None = None
    metadata: dict[str, Any] | None = None


class GetTaskRequest(_Wire):
    tenant: str | None = None
    id: str
    historyLength: int | None = Field(default=None, ge=0)


class ListTasksRequest(_Wire):
    tenant: str | None = None
    contextId: str | None = None
    status: TaskState | None = None
    pageSize: int | None = None
    pageToken: str | None = None
    historyLength: int | None = Field(default=None, ge=0)
    statusTimestampAfter: str | None = None
    includeArtifacts: bool | None = None


class CancelTaskRequest(_Wire):
    tenant: str | None = None
    id: str
    metadata: dict[str, Any] | None = None


class PushConfigRef(_Wire):
    """Get / Delete: which configuration of which task."""

    tenant: str | None = None
    taskId: str
    id: str


class ListPushConfigsRequest(_Wire):
    tenant: str | None = None
    taskId: str
    pageSize: int | None = None
    pageToken: str | None = None


# ------------------------------------------------------------------------------------------ errors

# JSON-RPC 2.0 standard codes, then the A2A range (spec section 5.4).
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
TASK_NOT_FOUND = -32001
TASK_NOT_CANCELABLE = -32002
PUSH_NOTIFICATION_NOT_SUPPORTED = -32003
UNSUPPORTED_OPERATION = -32004
CONTENT_TYPE_NOT_SUPPORTED = -32005
VERSION_NOT_SUPPORTED = -32009
# Not in the A2A table: the spec says authentication failures use "JSON-RPC custom error".
UNAUTHORIZED = -32050

_ERROR_DOMAIN = "a2a-protocol.org"


class A2AError(Exception):
    """A JSON-RPC error. `data` follows the spec: a list of `@type`-tagged detail objects."""

    def __init__(self, code: int, message: str, data: list[dict[str, Any]] | None = None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.data = data

    def payload(self) -> dict[str, Any]:
        body: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data:
            body["data"] = self.data
        return body


def _error_info(reason: str, **metadata: str) -> dict[str, Any]:
    return {
        "@type": "type.googleapis.com/google.rpc.ErrorInfo",
        "reason": reason,
        "domain": _ERROR_DOMAIN,
        "metadata": {**metadata, "timestamp": now_iso()},
    }


def parse_error() -> A2AError:
    return A2AError(PARSE_ERROR, "Invalid JSON payload")


def internal_error() -> A2AError:
    return A2AError(INTERNAL_ERROR, "Internal error")


def invalid_request(detail: str = "Request payload validation error") -> A2AError:
    return A2AError(INVALID_REQUEST, detail)


def method_not_found(method: str) -> A2AError:
    return A2AError(METHOD_NOT_FOUND, "Method not found", [_error_info("METHOD_NOT_FOUND", method=method[:80])])


def invalid_params(detail: str, violations: list[tuple[str, str]] | None = None) -> A2AError:
    data: list[dict[str, Any]] = []
    if violations:
        data.append(
            {
                "@type": "type.googleapis.com/google.rpc.BadRequest",
                "fieldViolations": [{"field": f, "description": d} for f, d in violations],
            }
        )
    return A2AError(INVALID_PARAMS, f"Invalid parameters: {detail}" if detail else "Invalid parameters", data or None)


def invalid_params_from(exc: ValidationError, prefix: str = "") -> A2AError:
    """Field violations from a pydantic error. Only the location and the rule are reported, never the input."""
    violations = [(prefix + ".".join(str(p) for p in err["loc"]), err["msg"]) for err in exc.errors()]
    return invalid_params("; ".join(f"{f}: {d}" for f, d in violations[:3]), violations)


def task_not_found(task_id: str) -> A2AError:
    return A2AError(TASK_NOT_FOUND, "Task not found", [_error_info("TASK_NOT_FOUND", taskId=task_id[:128])])


def task_not_cancelable(task_id: str, why: str) -> A2AError:
    return A2AError(
        TASK_NOT_CANCELABLE,
        f"Task cannot be canceled: {why}",
        [_error_info("TASK_NOT_CANCELABLE", taskId=task_id[:128])],
    )


def push_not_supported() -> A2AError:
    return A2AError(
        PUSH_NOTIFICATION_NOT_SUPPORTED,
        "Push notifications are not supported",
        [_error_info("PUSH_NOTIFICATION_NOT_SUPPORTED")],
    )


def unsupported_operation(why: str) -> A2AError:
    return A2AError(UNSUPPORTED_OPERATION, f"Unsupported operation: {why}", [_error_info("UNSUPPORTED_OPERATION")])


def content_type_not_supported(why: str) -> A2AError:
    return A2AError(CONTENT_TYPE_NOT_SUPPORTED, f"Content type not supported: {why}")


def version_not_supported(version: str) -> A2AError:
    return A2AError(
        VERSION_NOT_SUPPORTED,
        "A2A protocol version not supported",
        [_error_info("VERSION_NOT_SUPPORTED", requested=version[:32], supported=f"{LEGACY_PROTOCOL_VERSION}, 1.0")],
    )


def unauthorized(why: str) -> A2AError:
    return A2AError(UNAUTHORIZED, f"Unauthorized: {why}", [_error_info("UNAUTHORIZED")])


def message_from_text(text: str, *, context_id: str | None = None, task_id: str | None = None) -> Message:
    """An agent-authored text message."""
    return Message(messageId=new_id(), role=Role.AGENT, parts=[text_part(text)], contextId=context_id, taskId=task_id)
