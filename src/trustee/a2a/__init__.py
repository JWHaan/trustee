"""Agent2Agent (A2A) protocol binding for Trustee: JSON-RPC server, client and push notifications.

Implemented: A2A specification **1.0.0** (https://a2a-protocol.org/latest/specification/), JSON-RPC protocol
binding (section 9), served at `POST /a2a`.

* Methods: `SendMessage`, `GetTask`, `ListTasks`, `CancelTask`, `CreateTaskPushNotificationConfig`,
  `GetTaskPushNotificationConfig`, `ListTaskPushNotificationConfigs`, `DeleteTaskPushNotificationConfig`.
* The v0.3 method names (`message/send`, `tasks/get`, `tasks/cancel`, `tasks/pushNotificationConfig/*`) are
  accepted and answered in the v0.3 wire shape (`kind` fields, lower-case states, bare results); see `legacy.py`.
* Data model: Task, TaskStatus, TaskState, Message, Role, Part (text / raw / url / data), Artifact,
  TaskPushNotificationConfig, AuthenticationInfo, with the spec's camelCase field names and ProtoJSON enums.
* Push notifications: the Task is POSTed to the configured webhook on every deal state change
  (`push.py`).
* The `A2A-Version` header is checked (1.0 and 0.3 are accepted; anything else is VersionNotSupported).

Left out, and answered with the spec's error where the spec has one:

* **Streaming** (`SendStreamingMessage`, `SubscribeToTask`): `capabilities.streaming` is false and both
  return UnsupportedOperationError (-32004). Use `GetTask` or push notifications.
* **GetExtendedAgentCard**: not offered (`capabilities.extendedAgentCard` is false) -> UnsupportedOperationError.
* **The gRPC and HTTP+JSON/REST A2A bindings.** Trustee's own `/v1` REST API is not the A2A HTTP+JSON binding.
* **Task `history`**: Trustee keeps no per-message conversation, so `history` is never returned and any
  `historyLength` is satisfied trivially. The deal's transition log is at `GET /v1/deals/{id}/events`.
* **Tenants** (`tenant` is accepted and ignored), **Agent Card signatures**, **extension negotiation**
  (the card declares the Ed25519 extension; a client that does not mention it is not turned away, because
  the signatures inside each payload are verified regardless), and **message de-duplication on `messageId`**
  (the service is idempotent on deal ids and signed messages instead).
* **Client-chosen task ids**: a task id is always the deal id the signed contract names; a `taskId` in a
  message must refer to an existing task.

A2A is only the transport here. Every contract, delivery and attestation carries an Ed25519 signature that
`DealService` verifies exactly as it does over REST, and a skill cannot move money by any path the REST
API does not already have. See `docs/A2A.md`.
"""

from __future__ import annotations

from .client import A2AClient
from .models import A2AError, Message, Part, Task, TaskState
from .server import A2ABinding, A2AServer

__all__ = ["A2ABinding", "A2AClient", "A2AError", "A2AServer", "Message", "Part", "Task", "TaskState"]
