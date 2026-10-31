"""Human alerts: tell a person when their agents' money needs a decision.

Trustee lets agents move money on their own, so the humans behind them have to hear about the few
moments that need a person: a dispute opens, PayPal reports something the deal state cannot follow, a
mandate fails its re-check, a forged signature hits the API, a payer approval lapses. This module turns
those ledger events into flat JSON and POSTs them to a webhook (built for Zapier's "Catch Hook").

Design constraints, all deliberate:

* The money path is untouched. Alerts are *derived* from ledger events through `Ledger.subscribe`; the
  subscriber only classifies and enqueues, so nothing here can block, delay or break a payment.
* Delivery runs on one daemon thread with a timeout, exponential backoff and a bounded queue (oldest
  dropped first). Every failure is logged and swallowed.
* Payloads are built from fixed templates plus ids, never from raw notes, PayPal bodies, signatures,
  keys or emails.
* Without a configured URL nothing is created and behavior is identical to before.

`python -m trustee.alerts --test` sends one sample alert so a Zap can be built from a real sample.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import sys
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from .ledger import Event, Ledger

log = logging.getLogger(__name__)

ALL_KINDS: tuple[str, ...] = (
    "dispute",
    "operator_resolved",
    "paypal_alert",
    "signature_rejected",
    "mandate_recheck",
    "approval_expired",
    "paypal_call_failed",
)
DEFAULT_KINDS = ALL_KINDS

_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_SEEN_CAP = 2000  # dedupe memory; bounded so a long-running server cannot grow it forever
_STATE_CAP = 2000


class AlertConfigError(ValueError):
    """The webhook configuration is unusable (for example a non-https URL)."""


def validate_url(url: str) -> str:
    parts = urlsplit(url.strip())
    if parts.scheme == "https" and parts.hostname:
        return url.strip()
    if parts.scheme == "http" and parts.hostname in _LOCAL_HOSTS:
        return url.strip()
    raise AlertConfigError("alert webhook URL must be https:// (http:// is only allowed for localhost/127.0.0.1)")


def parse_kinds(raw: str | None) -> tuple[str, ...]:
    """Comma list from TRUSTEE_ALERT_KINDS. Unknown names are logged and ignored; empty means all."""
    if not raw or not raw.strip():
        return DEFAULT_KINDS
    wanted = [k.strip() for k in raw.split(",") if k.strip()]
    unknown = [k for k in wanted if k not in ALL_KINDS]
    if unknown:
        log.warning("TRUSTEE_ALERT_KINDS: ignoring unknown kinds %s (known: %s)", unknown, ", ".join(ALL_KINDS))
    kept = tuple(k for k in wanted if k in ALL_KINDS)
    return kept or DEFAULT_KINDS


# ------------------------------------------------------------------ classification


def classify(ev: Event) -> tuple[str, str, str, str] | None:
    """Map one ledger event to (alert, title, summary, severity), or None when no human is needed."""
    p = ev.payload
    kind = ev.kind
    if kind == "paypal_alert":
        return (
            "paypal_alert",
            "PayPal reported something the deal can't follow",
            "PayPal sent an event that the deal state cannot follow; a person should look at this deal.",
            "critical",
        )
    if kind == "identity" and p.get("ok") is False:
        return (
            "signature_rejected",
            "Signature rejected",
            "A message for this deal was rejected because its signature was missing or invalid.",
            "warning",
        )
    if kind == "ruling":
        if p.get("override") is True:
            return (
                "operator_resolved",
                "Operator resolved a dispute",
                "An operator resolved a disputed deal and the funds moved accordingly.",
                "info",
            )
        if p.get("decision") == "uphold":
            deadline = p.get("dispute_deadline")
            tail = f" Decide before {deadline}." if deadline else ""
            return (
                "dispute",
                "Dispute opened — funds held",
                f"The arbiter upheld the delivery and the funds stay held until a person decides.{tail}",
                "warning",
            )
        if p.get("decision") == "hold" and p.get("clause") == "mandate recheck":
            return _mandate_failed("The mandate was no longer valid at capture, so the funds stay held.")
    if kind == "mandate" and p.get("ok") is False and p.get("when"):
        return _mandate_failed(f"The mandate failed its re-check at {p['when']}; no money moved.")
    if kind == "run" and ev.actor == "arena" and p.get("event") in ("approval_timeout", "approval_cancelled"):
        why = "was cancelled" if p["event"] == "approval_cancelled" else "timed out"
        return _approval_expired(f"The payer's approval at PayPal {why}; no funds were held.")
    # `_explanation` also logs notes under op "unwind" and "approval" (every refund narrates itself), so
    # match the two literal lines Trustee writes for these cases rather than the op alone.
    if kind == "note" and p.get("op") == "unwind" and p.get("explanation") == "deadline lapsed before funds were held":
        return _approval_expired("The deadline passed before the payer approved; no funds were held.")
    if kind == "note" and p.get("op") == "approval" and p.get("explanation") == "payer cancelled approval at PayPal":
        return _approval_expired("The payer cancelled the approval at PayPal; no funds were held.")
    if kind == "paypal" and p.get("error") and p.get("op") in ("capture", "void"):
        return (
            "paypal_call_failed",
            "PayPal call failed — funds still held, retry pending",
            f"PayPal rejected the {p['op']} call; the funds are still held and the same call can be retried.",
            "warning",
        )
    return None


def _mandate_failed(summary: str) -> tuple[str, str, str, str]:
    return ("mandate_recheck", "Mandate failed re-check", summary, "warning")


def _approval_expired(summary: str) -> tuple[str, str, str, str]:
    return ("approval_expired", "Payer approval expired", summary, "info")


# ------------------------------------------------------------------------- sink


class AlertSink:
    """Turn ledger events into webhook alerts without ever touching the caller's thread budget."""

    def __init__(
        self,
        url: str,
        secret: str | None = None,
        *,
        source: str,
        public_url: str | None = None,
        kinds: Iterable[str] = DEFAULT_KINDS,
        http: httpx.Client | None = None,
        max_queue: int = 200,
        timeout: float = 5.0,
        retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.url = validate_url(url)
        self.secret = secret or None
        self.source = source
        self.public_url = (public_url or "").rstrip("/")
        self.kinds = frozenset(kinds)
        self.max_queue = max(1, max_queue)
        self.retries = max(0, retries)
        self._sleep = sleep
        self._own_http = http is None
        self._http = http if http is not None else httpx.Client(timeout=timeout)
        self._timeout = timeout
        self._queue: deque[dict[str, Any]] = deque()
        self._cv = threading.Condition()
        self._busy = False
        self._closed = False
        self._seen: OrderedDict[tuple[str, str], None] = OrderedDict()
        self._states: OrderedDict[str, str] = OrderedDict()
        self._unsubs: list[Callable[[], None]] = []
        self._thread = threading.Thread(target=self._run, name=f"trustee-alerts-{source}", daemon=True)
        self._thread.start()

    # -- ledger side (runs on the appending thread: classify + enqueue only)
    def attach(self, ledger: Ledger) -> Callable[[], None]:
        unsub = ledger.subscribe(self.handle)
        self._unsubs.append(unsub)
        return unsub

    def handle(self, ev: Event) -> None:
        try:
            self._handle(ev)
        except Exception:  # noqa: BLE001 - alerting must never break the ledger
            log.exception("alert classification failed")

    def _handle(self, ev: Event) -> None:
        if ev.kind == "state" and ev.deal_id and ev.payload.get("state_to"):
            self._states[ev.deal_id] = str(ev.payload["state_to"])
            self._states.move_to_end(ev.deal_id)
            while len(self._states) > _STATE_CAP:
                self._states.popitem(last=False)
        hit = classify(ev)
        if hit is None:
            return
        alert, title, summary, severity = hit
        if alert not in self.kinds:
            return
        key = (ev.deal_id or "", alert)
        if key in self._seen:
            return
        self._seen[key] = None
        while len(self._seen) > _SEEN_CAP:
            self._seen.popitem(last=False)
        payload: dict[str, Any] = {
            "alert": alert,
            "title": title,
            "summary": summary,
            "severity": severity,
            "deal_id": ev.deal_id,
            "state": self._states.get(ev.deal_id or "", ""),
            "actor": ev.actor,
            "source": self.source,
            "occurred_at": _iso(ev.ts),
            "deal_url": self._deal_url(ev.deal_id),
            "arena_url": self.public_url,
        }
        if ev.payload.get("dispute_deadline"):
            payload["dispute_deadline"] = str(ev.payload["dispute_deadline"])
        self._enqueue(payload)

    def _deal_url(self, deal_id: str | None) -> str:
        if self.public_url and self.source == "platform" and deal_id:
            return f"{self.public_url}/v1/deals/{deal_id}"
        return ""

    def _enqueue(self, payload: dict[str, Any]) -> None:
        with self._cv:
            if self._closed:
                return
            if len(self._queue) >= self.max_queue:
                dropped = self._queue.popleft()
                log.warning("alert queue full; dropped oldest alert %s for %s", dropped["alert"], dropped["deal_id"])
            self._queue.append(payload)
            self._cv.notify()

    # -- delivery side
    def _run(self) -> None:
        while True:
            with self._cv:
                while not self._queue and not self._closed:
                    self._cv.wait()
                if self._closed:
                    return
                payload = self._queue.popleft()
                self._busy = True
            try:
                self._deliver(payload)
            except Exception:  # noqa: BLE001
                log.exception("alert delivery crashed")
            finally:
                with self._cv:
                    self._busy = False
                    self._cv.notify_all()

    def _deliver(self, payload: dict[str, Any]) -> bool:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.secret:
            digest = hmac.new(self.secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
            headers["X-Trustee-Signature"] = f"sha256={digest}"
            headers["X-Trustee-Timestamp"] = str(int(time.time()))
        attempts = self.retries + 1
        for attempt in range(attempts):
            if self._closed:
                return False
            try:
                resp = self._http.post(self.url, content=body, headers=headers, timeout=self._timeout)
            except Exception as exc:  # noqa: BLE001 - network errors are retried, never raised
                log.warning(
                    "alert %s attempt %d/%d failed: %s", payload["alert"], attempt + 1, attempts, type(exc).__name__
                )
            else:
                if resp.status_code < 300:
                    return True
                if resp.status_code < 500 and resp.status_code != 429:
                    log.warning(
                        "alert %s rejected by webhook (HTTP %d); not retrying", payload["alert"], resp.status_code
                    )
                    return False
                log.warning(
                    "alert %s attempt %d/%d got HTTP %d", payload["alert"], attempt + 1, attempts, resp.status_code
                )
            if attempt + 1 < attempts:
                self._sleep(min(2.0**attempt, 30.0))
        log.error(
            "alert %s for %s undeliverable after %d attempts; giving up", payload["alert"], payload["deal_id"], attempts
        )
        return False

    def send_now(self, payload: dict[str, Any]) -> bool:
        """Deliver synchronously (used by `--test`); returns whether the webhook accepted it."""
        return self._deliver(payload)

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
        self._thread.join(timeout=1.0)
        if self._own_http:
            try:
                self._http.close()
            except Exception:  # noqa: BLE001
                log.debug("alert http close failed", exc_info=True)


def _iso(ts: str) -> str:
    try:
        return datetime.fromisoformat(ts).isoformat()
    except ValueError:
        return ts


# -------------------------------------------------------------------- env wiring


def sink_from_env(
    source: str,
    public_url: str | None = None,
    env: Mapping[str, str] | None = None,
    http: httpx.Client | None = None,
) -> AlertSink | None:
    """Build a sink from TRUSTEE_ALERT_*; None when unset or unusable (the app runs without alerts)."""
    env = os.environ if env is None else env
    url = (env.get("TRUSTEE_ALERT_WEBHOOK_URL") or "").strip()
    if not url:
        return None
    try:
        return AlertSink(
            url,
            env.get("TRUSTEE_ALERT_WEBHOOK_SECRET") or None,
            source=source,
            public_url=public_url,
            kinds=parse_kinds(env.get("TRUSTEE_ALERT_KINDS")),
            http=http,
        )
    except AlertConfigError as exc:
        log.error("alerts disabled: %s", exc)
        return None


def arena_alerts_enabled(env: Mapping[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    return (env.get("TRUSTEE_ALERT_ARENA") or "0").strip().lower() in ("1", "true", "yes", "on")


# --------------------------------------------------------------------------- CLI


def sample_payload(public_url: str | None = None) -> dict[str, Any]:
    base = (public_url or "").rstrip("/")
    return {
        "alert": "dispute",
        "title": "Dispute opened — funds held",
        "summary": "Sample alert from `python -m trustee.alerts --test`; no real deal is involved.",
        "severity": "warning",
        "deal_id": "deal_sample",
        "state": "DISPUTED",
        "actor": "trustee",
        "source": "platform",
        "occurred_at": datetime.now().isoformat(timespec="seconds"),
        "deal_url": f"{base}/v1/deals/deal_sample" if base else "",
        "arena_url": base,
        "dispute_deadline": datetime.now().isoformat(timespec="seconds"),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m trustee.alerts", description="Trustee alert webhook tools")
    ap.add_argument("--test", action="store_true", help="send one sample alert to TRUSTEE_ALERT_WEBHOOK_URL")
    args = ap.parse_args(argv)
    if not args.test:
        ap.print_help()
        return 2
    url = (os.environ.get("TRUSTEE_ALERT_WEBHOOK_URL") or "").strip()
    if not url:
        print("TRUSTEE_ALERT_WEBHOOK_URL is not set", file=sys.stderr)
        return 1
    try:
        sink = AlertSink(
            url,
            os.environ.get("TRUSTEE_ALERT_WEBHOOK_SECRET") or None,
            source="platform",
            public_url=os.environ.get("TRUSTEE_PUBLIC_URL"),
        )
    except AlertConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        ok = sink.send_now(sample_payload(os.environ.get("TRUSTEE_PUBLIC_URL")))
    finally:
        sink.close()
    print(
        "sample alert delivered" if ok else "sample alert FAILED (see log above)",
        file=sys.stderr if not ok else sys.stdout,
    )
    return 0 if ok else 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    sys.exit(main())
