"""Ship every ledger event to Elasticsearch with the _bulk API.

Same shape as `trustee.alerts.AlertSink`: the ledger subscriber only builds a document and enqueues it, one
daemon thread sends batches with a timeout, exponential backoff and a bounded queue (oldest dropped first),
and every failure is logged and swallowed. The money path never waits on Elasticsearch.

Each document gets a stable `_id`, so a batch that is retried after a lost response is not indexed twice
(`create` answers 409 for a document that is already there, which counts as delivered).
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Mapping
from typing import Any

import httpx

from ..ledger import Event, Ledger
from .config import ElasticConfig, ElasticConfigError, arena_elastic_enabled, config_from_env
from .mapping import build_doc, deal_facts

__all__ = ["ElasticSink", "arena_elastic_enabled", "sink_from_env"]

log = logging.getLogger(__name__)

_DEALS_CAP = 5000  # buyer/seller memory per deal; bounded so a long-running server cannot grow it forever


class ElasticSink:
    def __init__(
        self,
        config: ElasticConfig,
        *,
        source: str,
        http: httpx.Client | None = None,
        max_queue: int = 5000,
        batch_size: int = 100,
        timeout: float = 10.0,
        retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.config = config
        self.source = source
        self.max_queue = max(1, max_queue)
        self.batch_size = max(1, batch_size)
        self.retries = max(0, retries)
        self._sleep = sleep
        self._timeout = timeout
        self._own_http = http is None
        self._http = http if http is not None else httpx.Client(timeout=timeout)
        self._queue: deque[tuple[str, dict[str, Any]]] = deque()
        self._cv = threading.Condition()
        self._busy = False
        self._closed = False
        self._seq = 0
        self._deals: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._unsubs: list[Callable[[], None]] = []
        self.delivered = 0
        self.dropped = 0
        self._thread = threading.Thread(target=self._run, name=f"trustee-elastic-{source}", daemon=True)
        self._thread.start()

    # -- ledger side (runs on the appending thread: build a document + enqueue only)
    def attach(self, ledger: Ledger) -> Callable[[], None]:
        unsub = ledger.subscribe(self.handle)
        self._unsubs.append(unsub)
        return unsub

    def handle(self, ev: Event) -> None:
        try:
            self._handle(ev)
        except Exception:  # noqa: BLE001 - shipping must never break the ledger
            log.exception("elastic sink failed to build a document")

    def _handle(self, ev: Event) -> None:
        facts = deal_facts(ev)
        if facts is not None and ev.deal_id:
            self._deals[ev.deal_id] = facts
            self._deals.move_to_end(ev.deal_id)
            while len(self._deals) > _DEALS_CAP:
                self._deals.popitem(last=False)
        with self._cv:
            if self._closed:
                return
            self._seq += 1
            seq = self._seq
        doc = build_doc(ev, self.source, seq, self._deals.get(ev.deal_id or ""))
        digest = hashlib.sha256(json.dumps(doc, sort_keys=True, default=str).encode("utf-8")).hexdigest()
        self._enqueue((digest[:32], doc))

    def _enqueue(self, item: tuple[str, dict[str, Any]]) -> None:
        with self._cv:
            if self._closed:
                return
            if len(self._queue) >= self.max_queue:
                self._queue.popleft()
                self.dropped += 1
                log.warning("elastic queue full; dropped the oldest ledger event")
            self._queue.append(item)
            self._cv.notify()

    # -- delivery side
    def _run(self) -> None:
        while True:
            with self._cv:
                while not self._queue and not self._closed:
                    self._cv.wait()
                if self._closed:
                    return
                batch = [self._queue.popleft() for _ in range(min(self.batch_size, len(self._queue)))]
                self._busy = True
            try:
                self._deliver(batch)
            except Exception:  # noqa: BLE001
                log.exception("elastic delivery crashed")
            finally:
                with self._cv:
                    self._busy = False
                    self._cv.notify_all()

    def _bulk_body(self, batch: list[tuple[str, dict[str, Any]]]) -> bytes:
        lines: list[str] = []
        for doc_id, doc in batch:
            lines.append(json.dumps({"create": {"_index": self.config.index, "_id": doc_id}}))
            lines.append(json.dumps(doc, default=str, separators=(",", ":")))
        return ("\n".join(lines) + "\n").encode("utf-8")

    def _deliver(self, batch: list[tuple[str, dict[str, Any]]]) -> bool:
        """Send `batch`; retry transient failures (and only the items that failed). True when all landed."""
        pending = batch
        attempts = self.retries + 1
        headers = {**self.config.headers(), "Content-Type": "application/x-ndjson"}
        for attempt in range(attempts):
            if self._closed:
                return False
            try:
                resp = self._http.post(
                    f"{self.config.url}/_bulk", content=self._bulk_body(pending), headers=headers, timeout=self._timeout
                )
            except Exception as exc:  # noqa: BLE001 - network errors are retried, never raised
                log.warning("elastic bulk attempt %d/%d failed: %s", attempt + 1, attempts, type(exc).__name__)
            else:
                if resp.status_code == 429 or resp.status_code >= 500:
                    log.warning("elastic bulk attempt %d/%d got HTTP %d", attempt + 1, attempts, resp.status_code)
                elif resp.status_code >= 300:
                    log.error(
                        "elastic rejected the bulk request (HTTP %d); dropping %d events",
                        resp.status_code,
                        len(pending),
                    )
                    self.dropped += len(pending)
                    return False
                else:
                    pending = self._failed_items(pending, resp)
                    if not pending:
                        self.delivered += len(batch)
                        return True
            if attempt + 1 < attempts:
                self._sleep(min(2.0**attempt, 30.0))
        self.dropped += len(pending)
        log.error("elastic: %d ledger events undeliverable after %d attempts; giving up", len(pending), attempts)
        return False

    def _failed_items(
        self, pending: list[tuple[str, dict[str, Any]]], resp: httpx.Response
    ) -> list[tuple[str, dict[str, Any]]]:
        """Items worth retrying after a 200 response. Permanent per-item errors are logged and dropped."""
        try:
            body = resp.json()
        except ValueError:
            return []
        if not body.get("errors"):
            return []
        retry: list[tuple[str, dict[str, Any]]] = []
        for item, result in zip(pending, body.get("items", []), strict=False):
            outcome: dict[str, Any] = next(iter(result.values()), {}) if isinstance(result, dict) else {}
            status = int(outcome.get("status", 200))
            if status < 300 or status == 409:  # 409: this _id is already indexed
                continue
            if status == 429 or status >= 500:
                retry.append(item)
            else:
                self.dropped += 1
                reason = (outcome.get("error") or {}).get("type", "unknown")
                log.error("elastic refused a ledger event (HTTP %d, %s); not retrying", status, reason)
        return retry

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

    def close(self, flush_timeout: float = 2.0) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        self.flush(flush_timeout)
        with self._cv:
            self._closed = True
            self._cv.notify_all()
        self._thread.join(timeout=1.0)
        if self._own_http:
            try:
                self._http.close()
            except Exception:  # noqa: BLE001
                log.debug("elastic http close failed", exc_info=True)


def sink_from_env(
    source: str, env: Mapping[str, str] | None = None, http: httpx.Client | None = None
) -> ElasticSink | None:
    """Build a sink from ELASTICSEARCH_URL / ELASTIC_API_KEY; None when unset or unusable."""
    try:
        config = config_from_env(env)
    except ElasticConfigError as exc:
        log.error("elastic shipping disabled: %s", exc)
        return None
    if config is None:
        return None
    return ElasticSink(config, source=source, http=http)
