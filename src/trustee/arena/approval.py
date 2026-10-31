"""Waiting for a payer to approve an order at PayPal.

On the real sandbox an AUTHORIZE order holds no funds until the buyer approves it in
PayPal. The arena keeps the run alive for that: it polls PayPal on a fixed cadence until the
order is approved, the buyer cancels, or the timeout passes. Every time source is injectable
so tests never sleep.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

DEFAULT_POLL_SECONDS = 3.0
DEFAULT_TIMEOUT_SECONDS = 300.0


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass
class ApprovalWait:
    """How long to wait and how. `sleep=None` waits on the cancel event, so a cancel ends the wait at once."""

    poll_seconds: float = DEFAULT_POLL_SECONDS
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    sleep: Callable[[float], None] | None = None
    clock: Callable[[], float] = field(default=time.monotonic)

    @classmethod
    def from_env(cls) -> ApprovalWait:
        return cls(
            poll_seconds=max(0.0, _env_float("ARENA_APPROVAL_POLL_SECONDS", DEFAULT_POLL_SECONDS)),
            timeout_seconds=max(0.0, _env_float("ARENA_APPROVAL_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS)),
        )

    def pause(self, cancel: threading.Event) -> None:
        if self.sleep is not None:
            self.sleep(self.poll_seconds)
        else:
            cancel.wait(self.poll_seconds)


class ApprovalGate(Protocol):
    """What the runner needs to know about PayPal return/cancel redirects (implemented in api.py)."""

    def track(self, order_id: str) -> threading.Event: ...

    def release(self, order_id: str) -> None: ...


def wait_for_approval(attempt: Callable[[], bool], cancel: threading.Event, wait: ApprovalWait) -> str:
    """Call `attempt` until it returns True. Returns "approved", "cancelled" or "timeout"."""
    deadline = wait.clock() + wait.timeout_seconds
    while True:
        if cancel.is_set():
            return "cancelled"
        if attempt():
            return "approved"
        if wait.clock() >= deadline:
            return "timeout"
        wait.pause(cancel)
