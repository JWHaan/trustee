"""A2A push notifications: tell the counterparty agent when a deal's state changes, instead of making it poll.

This is how a seller learns "the buyer's funds are held, ship now": it registers a webhook for the deal's
task, and Trustee POSTs the updated Task to it on every deal state transition.

Design constraints, all deliberate (the same ones `alerts.py` follows):

* The money path is untouched. Notifications derive from ledger events through `Ledger.subscribe`. The
  subscriber only checks a set and enqueues a deal id; the Task is built and sent later, on a worker thread.
* Delivery is at-least-once with a timeout, exponential backoff and a bounded queue. Every failure is
  logged and swallowed. A queued notification carries only the deal id, and the Task is rebuilt from the
  live deal when it is sent, so a notification that waited in a retry never reports stale state.
* Queued notifications for one deal collapse into one: the receiver wants the latest state, not each step.
  A config is not re-sent a state it already received.
* Configs are persisted in the store (`push_configs`), so they survive a restart.
* Webhook URLs are validated: https only, except 127.0.0.1 / localhost / ::1 for local agents. Literal
  private, link-local and reserved IPs are refused. DNS names are not resolved, so a name that points at a
  private address is not caught here; run Trustee where outbound traffic to private ranges is blocked.
* Redirects are not followed. Credentials and tokens are never logged.

Wire format (A2A 1.0, section 4.3.3): `POST <url>` with `Content-Type: application/a2a+json` and a body that
is a StreamResponse holding the Task: `{"task": {...}}`. If the config has `authentication`, the request carries
`Authorization: <scheme> <credentials>`. If it has a `token`, the request also carries it in
`X-A2A-Notification-Token`, the header the v0.3 spec and the reference SDKs use for it (1.0 does not name one).
Configs registered through a v0.3 method receive the v0.3 payload: the bare Task with `kind` fields.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import httpx

from ..ledger import Event, Ledger
from ..store import Store, StoreConflict
from . import legacy
from .models import PUSH_CONTENT_TYPE, PUSH_TOKEN_HEADER, AuthenticationInfo, TaskPushNotificationConfig, new_id

log = logging.getLogger(__name__)

LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
MAX_CONFIGS_PER_TASK = 5
_MAX_URL = 2048
_MAX_SECRET = 1024
_SCHEME = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]{1,32}$")
_CONFIG_ID = re.compile(r"^[A-Za-z0-9._:@-]{1,128}$")


class PushConfigError(ValueError):
    """The requested push configuration is unusable. Mapped to an Invalid params error."""


def validate_push_url(url: str) -> str:
    text = url.strip()
    if len(text) > _MAX_URL:
        raise PushConfigError("push notification URL is too long")
    parts = urlsplit(text)
    host = parts.hostname
    if not host or parts.scheme not in ("http", "https") or parts.username or parts.password:
        raise PushConfigError("push notification URL must be an absolute https:// URL without credentials")
    if host in LOCAL_HOSTS:
        return text
    if parts.scheme != "https":
        raise PushConfigError(
            "push notification URL must be https:// (http:// is only allowed for localhost/127.0.0.1)"
        )
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return text  # a DNS name; see the module docstring for what is not checked
    if not ip.is_global:
        raise PushConfigError("push notification URL must not point at a private, loopback or reserved address")
    return text


def _clean_secret(value: str | None, what: str) -> str | None:
    if value is None:
        return None
    if len(value) > _MAX_SECRET or any(ord(c) < 0x20 or ord(c) == 0x7F for c in value):
        raise PushConfigError(f"{what} must be printable text of at most {_MAX_SECRET} characters")
    return value


class PushNotifier:
    def __init__(
        self,
        store: Store,
        build_task: Callable[[str], dict[str, Any] | None],
        *,
        http: httpx.Client | None = None,
        timeout: float = 10.0,
        retries: int = 3,
        max_queue: int = 500,
        sleep: Callable[[float], None] = time.sleep,
    ):
        """`build_task(deal_id)` returns the current Task as a dict, or None if the deal is gone."""
        self.store = store
        self._build_task = build_task
        self._own_http = http is None
        self.http = http if http is not None else httpx.Client(timeout=timeout)  # replaceable, e.g. by a test transport
        self._timeout = timeout
        self.retries = max(0, retries)
        self.max_queue = max(1, max_queue)
        self._sleep = sleep
        self._watched: set[str] = store.push_config_task_ids()
        self._queue: deque[str] = deque()
        self._cv = threading.Condition()
        self._busy = False
        self._closed = False
        self._last_sent: dict[str, tuple[str, str]] = {}  # config id -> (deal state, a2a state) last delivered
        self._unsubs: list[Callable[[], None]] = []
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ config management

    @staticmethod
    def check(config: TaskPushNotificationConfig) -> tuple[str, str | None, str | None, str | None]:
        """Validate a config without storing it: (url, token, auth scheme, auth credentials)."""
        url = validate_push_url(config.url)
        token = _clean_secret(config.token, "token")
        scheme = credentials = None
        if config.authentication is not None:
            scheme = config.authentication.scheme
            if not _SCHEME.match(scheme):
                raise PushConfigError("authentication.scheme must be an HTTP authentication scheme name")
            credentials = _clean_secret(config.authentication.credentials, "authentication.credentials")
        return url, token, scheme, credentials

    def create(
        self, task_id: str, config: TaskPushNotificationConfig, dialect: str = "v1"
    ) -> TaskPushNotificationConfig:
        url, token, scheme, credentials = self.check(config)
        config_id = config.id or new_id()
        if not _CONFIG_ID.match(config_id):
            raise PushConfigError("push config id must be 1-128 characters of [A-Za-z0-9._:@-]")
        existing = self.store.list_push_configs(task_id)
        if config_id in {c["config_id"] for c in existing}:
            # Config calls carry no login, and List reveals ids: letting a create replace a config would let
            # anyone redirect another agent's webhook. Rotating a webhook is delete, then create.
            raise PushConfigError(f"push config {config_id!r} already exists on this task; delete it first")
        if len(existing) >= MAX_CONFIGS_PER_TASK:
            raise PushConfigError(f"a task can have at most {MAX_CONFIGS_PER_TASK} push notification configs")
        try:
            self.store.put_push_config(config_id, task_id, url, token, scheme, credentials, dialect)
        except StoreConflict as exc:
            raise PushConfigError(str(exc)) from None
        self._watched.add(task_id)
        self._last_sent.pop(config_id, None)
        return self._public(self.store.get_push_config(task_id, config_id) or {})

    def get(self, task_id: str, config_id: str) -> TaskPushNotificationConfig | None:
        row = self.store.get_push_config(task_id, config_id)
        return self._public(row) if row else None

    def list_for(self, task_id: str) -> list[TaskPushNotificationConfig]:
        return [self._public(r) for r in self.store.list_push_configs(task_id)]

    def delete(self, task_id: str, config_id: str) -> None:
        self.store.delete_push_config(task_id, config_id)  # idempotent, as the spec requires
        self._last_sent.pop(config_id, None)
        if not self.store.list_push_configs(task_id):
            self._watched.discard(task_id)

    @staticmethod
    def _public(row: dict[str, Any]) -> TaskPushNotificationConfig:
        """The config as the client sees it. Secrets the client sent are not echoed back."""
        return TaskPushNotificationConfig(
            id=row["config_id"],
            taskId=row["task_id"],
            url=row["url"],
            authentication=AuthenticationInfo(scheme=row["auth_scheme"]) if row.get("auth_scheme") else None,
        )

    # ------------------------------------------------------------------ ledger side

    def attach(self, ledger: Ledger) -> Callable[[], None]:
        unsub = ledger.subscribe(self.handle)
        self._unsubs.append(unsub)
        return unsub

    def handle(self, ev: Event) -> None:
        """Runs on the appending thread: a set lookup and an enqueue, nothing that can block or raise."""
        try:
            if ev.kind == "state" and ev.deal_id and ev.deal_id in self._watched:
                self.notify(ev.deal_id)
        except Exception:  # noqa: BLE001 - notifications must never break the ledger
            log.exception("push notification enqueue failed")

    def notify(self, deal_id: str) -> None:
        with self._cv:
            if self._closed or deal_id in self._queue:
                return  # a pending job already reads the latest state
            if len(self._queue) >= self.max_queue:
                dropped = self._queue.popleft()
                log.warning("push queue full; dropped the oldest notification (deal %s)", dropped)
            self._queue.append(deal_id)
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="trustee-a2a-push", daemon=True)
                self._thread.start()
            self._cv.notify()

    # ------------------------------------------------------------------ delivery side

    def _run(self) -> None:
        while True:
            with self._cv:
                while not self._queue and not self._closed:
                    self._cv.wait()
                if self._closed:
                    return
                deal_id = self._queue.popleft()
                self._busy = True
            try:
                self._deliver_deal(deal_id)
            except Exception:  # noqa: BLE001
                log.exception("push delivery crashed for deal %s", deal_id)
            finally:
                with self._cv:
                    self._busy = False
                    self._cv.notify_all()

    def _deliver_deal(self, deal_id: str) -> None:
        configs = self.store.list_push_configs(deal_id)
        if not configs:
            return
        task = self._build_task(deal_id)
        if task is None:
            return
        meta = (task.get("metadata") or {}).get("trustee") or {}
        fingerprint = (str(meta.get("dealState")), str(task["status"]["state"]))
        for cfg in configs:
            if self._last_sent.get(cfg["config_id"]) == fingerprint:
                continue
            if self._post(cfg, task):
                self._last_sent[cfg["config_id"]] = fingerprint

    def _post(self, cfg: dict[str, Any], task: dict[str, Any]) -> bool:
        legacy_dialect = cfg.get("dialect") == "v03"
        payload = legacy.task_out(task) if legacy_dialect else {"task": task}
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers = {"Content-Type": PUSH_CONTENT_TYPE}
        if cfg.get("auth_scheme"):
            headers["Authorization"] = f"{cfg['auth_scheme']} {cfg.get('auth_credentials') or ''}".strip()
        if cfg.get("token"):
            headers[PUSH_TOKEN_HEADER] = cfg["token"]
        attempts = self.retries + 1
        label = f"task {task['id']} -> config {cfg['config_id']}"
        for attempt in range(attempts):
            if self._closed:
                return False
            try:
                resp = self.http.post(cfg["url"], content=body, headers=headers, timeout=self._timeout)
            except Exception as exc:  # noqa: BLE001 - network errors are retried, never raised
                log.warning("push %s attempt %d/%d failed: %s", label, attempt + 1, attempts, type(exc).__name__)
            else:
                if resp.status_code < 300:
                    return True
                if resp.status_code < 500 and resp.status_code != 429:
                    log.warning("push %s rejected by the receiver (HTTP %d); not retrying", label, resp.status_code)
                    return False
                log.warning("push %s attempt %d/%d got HTTP %d", label, attempt + 1, attempts, resp.status_code)
            if attempt + 1 < attempts:
                self._sleep(min(2.0**attempt, 30.0))
        log.error("push %s undeliverable after %d attempts; giving up", label, attempts)
        return False

    # ------------------------------------------------------------------ lifecycle

    def flush(self, timeout: float = 5.0) -> bool:
        """Wait until the queue is drained and nothing is in flight. For tests and shutdown."""
        deadline = time.monotonic() + timeout
        with self._cv:
            while self._queue or self._busy:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cv.wait(remaining)
        return True

    def close(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        with self._cv:
            self._closed = True
            self._cv.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        if self._own_http:
            try:
                self.http.close()
            except Exception:  # noqa: BLE001
                log.debug("push http close failed", exc_info=True)
