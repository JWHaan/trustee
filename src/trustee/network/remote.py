"""The arena's side of hosted agents: start a purchase on a remote Northwind and follow it.

`RemoteNetwork.from_env()` is None unless NORTHWIND_URL, TIDEWATER_URL and NORTHWIND_TRIGGER_SECRET are all set,
so the arena keeps running its in-process agents by default. See `trigger.py` for the routes this calls.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx


class RemoteError(Exception):
    """The hosted Northwind refused or did not answer."""


@dataclass
class RemoteNetwork:
    northwind_url: str
    tidewater_url: str
    secret: str = field(repr=False)
    http: httpx.Client = field(default_factory=lambda: httpx.Client(timeout=30.0), repr=False)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> RemoteNetwork | None:
        e = os.environ if env is None else env
        northwind = (e.get("NORTHWIND_URL") or "").strip().rstrip("/")
        tidewater = (e.get("TIDEWATER_URL") or "").strip().rstrip("/")
        secret = (e.get("NORTHWIND_TRIGGER_SECRET") or "").strip()
        if not (northwind and tidewater and secret):
            return None
        return cls(northwind, tidewater, secret)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.secret}"}

    def _call(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            resp = self.http.request(method, self.northwind_url + path, headers=self._headers(), **kwargs)
        except httpx.HTTPError as exc:
            raise RemoteError(f"the hosted Northwind did not answer: {type(exc).__name__}") from None
        if resp.status_code >= 400:
            detail = ""
            try:
                detail = str(resp.json().get("detail", ""))
            except (ValueError, AttributeError):
                pass
            raise RemoteError(f"the hosted Northwind answered {resp.status_code} {detail}".strip())
        body = resp.json()
        if not isinstance(body, dict):
            raise RemoteError("the hosted Northwind sent an unexpected reply")
        return body

    def start(self, deal_id: str) -> None:
        self._call("POST", "/operator/purchases", json={"deal_id": deal_id, "seller_url": self.tidewater_url})

    def status(self, deal_id: str) -> dict[str, Any]:
        return self._call("GET", f"/operator/purchases/{deal_id}")
